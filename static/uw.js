/* Property Deal Analyzer v1.5 — reactive underwriting engine + autosave.
   Mirrors compute_underwriting() in app.py formula-for-formula. No Calculate button:
   every keystroke recomputes section totals, the sidebar outcome, and the Co-Living
   bottom summary in place, then debounce-autosaves a server draft per property+strategy
   (localStorage mirror as backup) so tab switches and reloads never lose edits. */
(function () {
  "use strict";
  var UW = window.UW || {};
  function N(v) { var x = parseFloat(String(v == null ? "" : v).replace(/[$,]/g, "")); return isNaN(x) ? 0 : x; }
  function M(v) { if (v === null || v === undefined) return "—"; return (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-US"); }
  function PC(v) { return (v === null || v === undefined) ? "—" : (Math.round(v * 10) / 10) + "%"; }
  function clamp(x, a, b) { return Math.max(a, Math.min(b, x)); }
  function loanPayment(amt, rate, yrs) {
    amt = N(amt); if (amt <= 0) return 0;
    rate = N(rate); yrs = N(yrs);
    if (yrs > 0) { var r = rate / 100 / 12, n = Math.floor(yrs * 12); if (r > 0) return amt * r / (1 - Math.pow(1 + r, -n)); return amt / n; }
    return amt * rate / 100 / 12;
  }
  function termMonths(f) { var tv = N(f.term_value); return (f.term_unit || "Months") === "Months" ? tv : tv * 12; }
  /* v2.3 TWO-TERM MODEL (ReiSearch pass 2026-11-03, measured): the card carries
     a Loan Term and an Amortization Term. The AMORTIZATION term drives the
     payment; the LOAN term never touches the payment — it only sets the
     amortization table's horizon (balloon). Legacy cards saved with only the
     single term keep it as BOTH (server _norm_funding migrates on load; the
     undefined-check here is the same rule for in-flight states). */
  function amortMonths(f) {
    if (!f) return 0;
    if (f.amort_value === undefined || f.amort_value === null) return termMonths(f);
    var tv = N(f.amort_value); return (f.amort_unit || "Months") === "Months" ? tv : tv * 12;
  }
  function rowPayment(f) {
    /* ReiSearch funding-card payment model. Returns {pay (P&I or fixed), esc (taxes+insurance)}. */
    if (!f || f.type === "Cash" || f.type === "Equity") return { pay: 0, esc: 0 };
    var rate = N(f.rate), months = amortMonths(f), pt = f.payment_type || "amortized", pay = 0;
    /* v1.8b: a Fixed payment IS the payment — it must not require a loan amount
       (Subject-To cards have a blank amount by design; blank cards start at 0). */
    if (pt === "fixed") return { pay: N(f.fixed_payment), esc: N(f.taxes_mo) + N(f.insurance_mo) };
    var amt = N(f.amount); if (amt <= 0) return { pay: 0, esc: 0 };
    /* v2.0 (P0-3 root cause): an Amortized/Principal-Only card with NO
       amortization term typed silently priced as interest-only (loanPayment's
       yrs<=0 fallback), so the term field looked dead and a loan number existed
       that the user never agreed to. Now: no amortization term -> payment
       "not set", $0 debt — the same honest state as a blank Fixed payment.
       No term is ever invented. (v2.3: the rule keys on the AMORTIZATION term;
       clearing the Loan Term alone never unsets a payment — ReiSearch.) */
    if ((pt === "amortized" || pt === "principal_only") && !months)
      return { pay: 0, esc: N(f.taxes_mo) + N(f.insurance_mo), notset: true };
    if (pt === "interest_only") pay = amt * rate / 100 / 12;
    else if (pt === "principal_only") pay = amt * rate / 100 / 12 + (months ? amt / months : 0); // ReiSearch: interest + straight-line principal over the AMORTIZATION term
    else pay = loanPayment(amt, rate, months / 12);
    return { pay: pay, esc: N(f.taxes_mo) + N(f.insurance_mo) };
  }
  function paydownY1For(f) {
    /* Year-1 principal paydown by payment type (mirrors app.py _paydown_y1).
       Interest Only pays NO principal — balance flat (v1.6 fix F: the old generic
       amortization table booked IO balances as Year-1 paydown). */
    if (!f || f.type === "Cash" || f.type === "Equity") return 0;
    var amt = N(f.amount); if (amt <= 0) return 0;
    var rate = N(f.rate), months = amortMonths(f), pt = f.payment_type || "amortized";
    if (pt === "interest_only") return 0;
    if (pt === "principal_only") return months ? Math.min(amt, amt / months * 12) : 0;
    if (pt === "amortized" && !months) return 0; /* v2.0: no amortization term -> no payment -> no paydown (never invent 360) */
    var pay = pt === "fixed" ? N(f.fixed_payment) : loanPayment(amt, rate, months / 12);
    if (pay <= 0) return 0;
    var r = rate / 100 / 12, bal = amt, prin = 0;
    for (var i = 0; i < 12 && bal > 0; i++) { var p = Math.min(Math.max(pay - bal * r, 0), bal); prin += p; bal -= p; }
    return prin;
  }
  function loanSchedule(f) {
    /* Full schedule simulated by the card's OWN payment type (mirrors the sidebar
       view): IO = interest only, $0 principal, balance flat.
       v2.3 TWO-TERM: the payment comes from the AMORTIZATION term (rowPayment);
       the table's HORIZON is the LOAN term — a loan term shorter than the
       amortization term ends the table with a balloon balance, exactly as
       ReiSearch's table behaves (measured 2026-11-03). */
    var amt = N(f.amount), rate = N(f.rate), pt = f.payment_type || "amortized";
    /* v2.0: an Amortized/Principal-Only card with no AMORTIZATION term has NO
       schedule — the old || 360 invented a 30-year term the user never entered.
       (Fixed cards keep a display horizon only; their payment is the user's amount.) */
    if ((pt === "amortized" || pt === "principal_only") && !amortMonths(f) && amt > 0)
      return { payment: 0, rows: [], notset: true };
    var aMonths = amortMonths(f);
    var months = termMonths(f) || aMonths || 360;
    var rp = rowPayment(f);
    var r = rate / 100 / 12, bal = amt, rows = [];
    var pmtAm = aMonths ? loanPayment(amt, rate, aMonths / 12) : 0;
    var maxY = Math.min(Math.ceil(months / 12), 40);
    for (var y = 1; y <= maxY; y++) {
      var ti = 0, tp = 0;
      var mInYear = Math.min(12, months - (y - 1) * 12);
      for (var i = 0; i < mInYear; i++) {
        var inter = bal * r, prin = 0;
        if (pt === "interest_only") { inter = amt * r; prin = 0; }
        else if (pt === "principal_only") prin = aMonths ? Math.min(amt / aMonths, bal) : 0;
        else if (pt === "fixed") prin = Math.min(Math.max(rp.pay - inter, 0), bal);
        else prin = Math.min(Math.max(pmtAm - inter, 0), bal);
        ti += inter; tp += prin; bal -= prin; if (bal < 0) bal = 0;
      }
      rows.push({ year: y, interest: ti, principal: tp, balance: Math.max(bal, 0) });
      if (pt === "interest_only") { if (y >= 10) break; }
      else if (bal <= 0.5) break;
    }
    return { payment: rp.pay + rp.esc, rows: rows };
  }
  /* v2.3: label vocabulary + bands measured on ReiSearch (pass 2026-11-03):
     93/83/81 Strong, 74/73/70 Good, 63 Fair, 44/38/28/15/12 Weak.
     Band edges 75/65/55 are the round boundaries consistent with all 13
     labeled ReiSearch observations. */
  function scoreLabel(t) { return t >= 75 ? "Strong" : (t >= 65 ? "Good" : (t >= 55 ? "Fair" : "Weak")); }
  /* v2.4: Deal Score = ReiSearch's MEASURED model (pass 2026-11-03, Addenda
     2+3). Score = weighted average over the SCORED factors only (a "no data"
     factor is EXCLUDED from the denominator), minus penalties, rounded
     half-up. Per-factor curves are piecewise-linear through ReiSearch's
     measured points; between points the simplest monotone interpolation,
     beyond the end points the end segment's slope, all clamped 0..100.
     The total is computed from UNROUNDED factor scores — Senisa proves it:
     its displayed factors average exactly 84.5 yet ReiSearch shows 84,
     because its CF/room sits between knots at 64.6 unrounded. Cap rate
     scores percent x 10 capped at 100 (measured 8.9->89, 8.6->86,
     14.9->100); Cash-on-cash uses the same percent x 10 line — its only
     measured point (-27.7% -> 0) sits on it, so the line beyond that point
     is an extrapolation on the cap-rate pattern, flagged unverified.
     v2.8 REFIT (evening pass 2026-11-03): piecewise-linear through EVERY
     published (value -> displayed score) point — v2.4 knots plus R1-R8
     and the Electric probes. Senisa's CF/room is NOT a knot (it
     interpolates to 64.6, which its total of 84 requires). The CF knot
     at (571.15, 100) is DERIVED from the probe totals (see app.py note).
     All 17 fixtures reproduce exactly; residual table in the v28 log. */
  var SCORE_CURVES = {
    cf: [[-219.17, 0], [-56.45, 0], [-36.85, 0], [18.98, 8], [33.07, 13],
         [68.98, 28], [125.15, 46], [194.42, 64], [195.44, 64], [241.64, 73],
         [571.15, 100], [658.65, 100], [696.15, 100]],
    dscr: [[0.7312, 0], [0.925, 12], [0.9497, 15], [1.0303, 25], [1.0527, 29],
           [1.11, 40], [1.1996, 56], [1.3903, 79], [1.5316, 91], [1.8265, 97]],
    be: [[0, 100], [4.58, 100], [55.5, 95], [61.14, 89], [63.68, 86],
         [70.86, 78], [76.58, 67], [80.74, 58], [82.68, 52], [89.5, 32],
         [91.89, 24], [111.76, 0]],
    ds: [[-288.78, 0], [-126.07, 0], [-106.47, 0], [-50.63, 0], [-32.95, 14],
         [-0.63, 39], [49.92, 57], [124.8, 81], [125.83, 81], [130.62, 83],
         [172.02, 93], [589.04, 100], [626.54, 100]]
  };
  function pwCurve(x, knots) {
    var n = knots.length, i = 0;
    if (x <= knots[0][0]) i = 0;
    else if (x >= knots[n - 1][0]) i = n - 2;
    else { for (i = 0; i < n - 1; i++) { if (x >= knots[i][0] && x <= knots[i + 1][0]) break; } }
    var x0 = knots[i][0], y0 = knots[i][1], x1 = knots[i + 1][0], y1 = knots[i + 1][1];
    return clamp(y0 + (x - x0) * (y1 - y0) / (x1 - x0), 0, 100);
  }
  function roundHalfUp(x) { return Math.floor(x + 0.5); }
  function fmtMoney0(x) {
    return (x < 0 ? "−$" : "$") + roundHalfUp(Math.abs(x)).toLocaleString("en-US") + "/mo";
  }
  function fmtPct1(x) {
    if (!x) return "0%";
    return (x < 0 ? "−" : "") + (roundHalfUp(Math.abs(x) * 10) / 10).toFixed(1) + "%";
  }
  function scoreFactors(m) {
    /* m: cf_per_room, dscr_val (null = no debt -> scored 100, "No debt"),
       coc_val (fraction, null = no data -> EXCLUDED), cap_rate (fraction),
       breakeven (fraction), downside_cf_per_room. */
    return [
      { key: "cf", name: "Cash flow per room", weight: 25, value: fmtMoney0(m.cf_per_room),
        score: pwCurve(m.cf_per_room, SCORE_CURVES.cf) },
      { key: "dscr", name: "DSCR", weight: 20,
        value: m.dscr_val === null ? "No debt" : (Math.round(m.dscr_val * 100) / 100).toFixed(2),
        score: m.dscr_val === null ? 100 : pwCurve(m.dscr_val, SCORE_CURVES.dscr) },
      { key: "coc", name: "Cash-on-cash", weight: 20,
        value: m.coc_val === null ? "No data" : fmtPct1(m.coc_val * 100),
        score: m.coc_val === null ? null : clamp(m.coc_val * 1000, 0, 100) },
      { key: "be", name: "Break-even occupancy", weight: 12, value: fmtPct1(m.breakeven * 100),
        score: pwCurve(m.breakeven * 100, SCORE_CURVES.be) },
      { key: "ds", name: "Downside cash flow per room", weight: 8, value: fmtMoney0(m.downside_cf_per_room),
        score: pwCurve(m.downside_cf_per_room, SCORE_CURVES.ds) },
      { key: "cap", name: "Cap rate", weight: 15, value: fmtPct1(m.cap_rate * 100),
        score: clamp(m.cap_rate * 1000, 0, 100) }
    ];
  }
  function scoreRental(m, penalties) {
    var factors = scoreFactors(m);
    var sw = 0, ss = 0, parts = {};
    factors.forEach(function (fc) {
      if (fc.score === null) return;
      sw += fc.weight; ss += fc.weight * fc.score;
      parts[fc.name + " (" + fc.weight + "%)"] = Math.round(fc.score * 10) / 10;
    });
    var total = sw ? ss / sw : 0;
    penalties.forEach(function (p) { total -= p.points; });
    total = clamp(roundHalfUp(total), 1, 99);
    return { score: total, label: scoreLabel(total), parts: parts, factors: factors, penalties: penalties };
  }
  /* v2.4 note logic, derived from ReiSearch's five measured notes (Addenda
     2+3): strongest = highest factor score, ties broken by the higher
     weight (Cash: CF/room wins a four-way tie at 100 on weight 25;
     Conventional: Cap rate beats Break-even at 89 on weight 15 > 12;
     Hard Money weakest: CF/room wins a three-way tie at 0 on weight 25).
     A "Weakest" is named only when the lowest scored factor is below 65
     (Conventional's 64 is named; Senisa's 65 is not). */
  function insightFromFactors(factors) {
    /* Comparisons use the DISPLAYED (rounded) factor scores, as ReiSearch
       does: Conventional's Break-even (88.96 unrounded) and Cap rate (88.80)
       both display 89, and ReiSearch names Cap rate — the tie is broken by
       weight; Senisa's CF/room (64.6 unrounded) displays 65 and ReiSearch
       reports "No weak spots", so the <65 weak test also reads the
       displayed value. */
    var scored = (factors || []).filter(function (f) { return f.score !== null; });
    if (!scored.length) return "";
    function rs(f) { return roundHalfUp(f.score); }
    var strong = scored.slice().sort(function (a, b) { return (rs(b) - rs(a)) || (b.weight - a.weight); })[0];
    var weak = scored.slice().sort(function (a, b) { return (rs(a) - rs(b)) || (b.weight - a.weight); })[0];
    return "Strongest: " + strong.name + ". " +
      (rs(weak) < 65 ? "Weakest: " + weak.name + "." : "No weak spots.");
  }
  function factorRow(fc) {
    /* ReiSearch "View Details" row: factor value + integer factor score;
       an excluded factor reads "No data". */
    return fc.score === null ? "No data" : fc.value + " · " + roundHalfUp(fc.score);
  }
  function insightFromParts(parts) {
    function frac(item) { var m = /\((\d+)%\)/.exec(item[0]); var w = m ? parseFloat(m[1]) : 0; return w ? item[1] / w : 0; }
    var arr = Object.keys(parts).map(function (k) { return [k, parts[k]]; });
    arr.sort(function (a, b) { return frac(a) - frac(b); });
    if (!arr.length) return "";
    var weak = arr[0], strong = arr[arr.length - 1];
    return "Strongest: " + strong[0].split(" (")[0] + ". " + (frac(weak) >= 0.8 ? "No weak spots." : "Weak spot: " + weak[0].split(" (")[0] + ".");
  }

  /* ---------------- compute (mirror of app.py) ---------------- */
  function compute(strat, s) {
    var arv = N(s.arv), price = N(s.purchase_price);
    function rowsTotal(rows, kind) {
      var t = 0;
      (rows || []).forEach(function (r) {
        if (kind === "ml") t += (N(r.material) + N(r.labor));
        else t += N(r.qty === undefined ? 1 : r.qty) * N(r.unit);
      });
      return t;
    }
    var rehabLines = 0;
    (s.rehab_rooms || []).forEach(function (r) { rehabLines += N(r.material) + N(r.labor); });
    Object.keys(s.rehab_flats || {}).forEach(function (k) { rehabLines += N(s.rehab_flats[k].material) + N(s.rehab_flats[k].labor); });
    var rehabCalc = rehabLines;
    var rehabMode = s.rehab_mode || "calc";
    var rehab = (rehabMode === "est" && N(s.rehab_est_total)) ? N(s.rehab_est_total) : rehabCalc;
    var appliances = rowsTotal(s.appliances, "unit");
    var furnishing = strat === "Long Term" ? 0 : rowsTotal(s.furnishing, "unit");
    var conversion = strat === "Co-Living" ? rowsTotal(s.conversion, "unit") : 0;
    var totalProject = price + rehab + appliances + furnishing + conversion;
    var hPeriod = N(s.holding_period), hUnit = s.holding_unit || "Months";
    var hMonths = hUnit === "Months" ? hPeriod : hPeriod / 30;
    var holdingMo = N(s.holding_mo), holdingTotal = holdingMo * hMonths;
    var funded = 0, cashEquity = 0, debtAmt = 0, debtRows = 0, paydownY1 = 0, pointsFees = 0;
    var carryMo = 0, pitiIncluded = false; /* v2.1 (mirror of app.py) */
    var debtLines = [];
    (s.funding || []).forEach(function (f) {
      var amt = N(f.amount); funded += amt;
      /* v2.8 (A8): Amount-only cards (Private Money Partner / JV Equity /
         Reserves) contribute their Amount to Funding Added ONLY — no
         financing costs, no cash-equity bucket, no debt service. */
      var kind = f.kind || (AMOUNT_ONLY_TYPES.indexOf(f.type) >= 0 ? "amount" : (ASSUMP_TYPES.indexOf(f.type) >= 0 ? "assump" : "loan"));
      if (kind === "amount") return;
      pointsFees += amt * N(f.points) / 100 + N(f.fees);
      if (f.type === "Cash" || f.type === "Equity") cashEquity += amt;
      else {
        debtAmt += amt;
        var rr = f;
        /* v2.8 (A8, FLAGGED INFERENCE — see v28 build log): Loan Assumption /
           Wraparound cards carry ONE Term; price the payment with the
           standard engine using that Term as the amortization term. */
        if (kind === "assump" && !N(f.amort_value) && N(f.term_value))
          rr = Object.assign({}, f, { amort_value: f.term_value, amort_unit: f.term_unit || "Years" });
        var rp = rowPayment(rr); debtRows += rp.pay + rp.esc;
        if (rp.esc > 0 || (f.payment_type === "fixed" && rp.pay > 0)) pitiIncluded = true;
        if (f.payment_type === "fixed") carryMo += rp.pay + rp.esc;
        else carryMo += amt * N(f.rate) / 100 / 12 + rp.esc;
        paydownY1 += paydownY1For(rr);
        var lab = f.loan_type || f.type || "Loan";
        if (f.preset && f.preset !== lab) lab += " · " + f.preset;
        debtLines.push({ label: lab, payment: Math.round(rp.pay + rp.esc) });
      }
    });
    /* v1.8a PITI wiring (mirror of app.py): base = typed Monthly Payment, or
       Principal+Interest when no total is typed; Taxes/Insurance/PMI/Escrow
       are escrow add-ons counted on top. */
    var entryFee = 0, mdPayment = 0, mdBase = 0, mdEscrow = 0, mdAddons = {};
    if (strat !== "Fix & Flip") {
      entryFee = N(s.md_entry_fee);
      var prin = N(s.md_piti_principal), intr = N(s.md_piti_interest);
      mdBase = N(s.md_monthly_payment);
      if (mdBase <= 0) mdBase = prin + intr;
      [["md_piti_taxes", "Taxes"], ["md_piti_insurance", "Insurance"], ["md_piti_pmi", "PMI"], ["md_piti_escrow", "Escrow"]].forEach(function (kv) {
        var v = N(s[kv[0]]); if (v) mdAddons[kv[1]] = Math.round(v); mdEscrow += v;
      });
      mdPayment = mdBase + mdEscrow;
      if (mdPayment > 0) pitiIncluded = true;
      paydownY1 += prin * 12;
    }
    var debtService = debtRows + mdPayment;
    /* v2.3: ANY loan payment retires the OpEx taxes/insurance auto-estimates —
       the rule the panel's own help text documents ("Skipped when a loan
       payment exists: they live inside PITI then") and the only rule under
       which ReiSearch's measured fixtures reproduce (Conventional: NOI $2,785,
       NCF $782, DSCR 1.39 — ReiSearch's OpEx has no taxes/insurance lines in
       any measured state). The v2.1 narrowing (escrow-on-card only) left the
       estimates charged on top of a plain P&I loan, which ReiSearch never does. */
    if (debtService > 0) pitiIncluded = true;
    var closing = N(s.closing_costs);
    var financingCosts = closing + pointsFees;
    var uses = totalProject + holdingTotal;
    var gap = uses - funded;
    /* Cash Needed (ReiSearch, re-measured 2026-11-03): cash contributions +
       points/fees + closing + entry fee. A loan-only funding GAP does NOT
       inflate Cash Needed on rentals — ReiSearch's Hard Money state shows
       Cash Needed $6,397 = its financing costs alone against a $56,445 gap
       (and its Total ROI −27.7% = −$1,769 / $6,397 confirms the denominator).
       Fix & Flip keeps the gap. (The v2.1 "gap for every strategy" rule is
       superseded by this measured evidence; mirrors app.py.) */
    var cashIn = cashEquity + pointsFees + closing + entryFee + (strat === "Fix & Flip" ? Math.max(gap, 0) : 0);
    /* v2.9 (Pass #2, 2026-11-03 evening; supersedes the block above AND
       the 2026-11-01 behavior spec's "moves with cash contributions"
       claim): Cash Needed = cash costs OUTSIDE Total Uses = financing
       costs (points + fees) + Subject-To entry fee. Measured across 8
       funding states: Cash-only / empty / Conventional / Private /
       Seller / Conv+$50k-cash / Cash+Private all read $0 — contributions
       and gaps move NOTHING; Hard Money reads exactly $6,397 = points.
       cashIn stays as the total-cash-in figure (refi remaining_in_deal,
       scenarios "Cash in" column, flip ROI) but feeds no Cash Needed /
       CoC surface. Fix & Flip keeps its published fixture: cashIn
       (gap-inclusive) IS the flip's Cash Needed (fixtures v15: $61,684).
       Mirrors app.py. */
    var cashNeeded = strat === "Fix & Flip" ? cashIn : pointsFees + entryFee;
    var fundingStatus = funded <= 0 ? "Not Funded Yet" : (gap < -0.05 * Math.max(uses, 1) ? "Over Funded" : (gap <= 0 ? "Fully Funded" : "Partially Funded"));
    /* v2.8 (A10, evening pass 2026-11-03): Override Total Uses — while set,
       the Funding Snapshot's Total Uses / Gap / status read the OVERRIDE;
       Cash Needed, debt service and everything else keep the REAL uses
       (measured: they did not move under a $400,000 override). Mirrors app.py. */
    var usesOverride = N(s.uses_override) || 0;
    var snapUses = uses, snapGap = gap, snapStatus = fundingStatus;
    if (usesOverride) {
      snapUses = usesOverride; snapGap = snapUses - funded;
      snapStatus = funded <= 0 ? "Not Funded Yet" : (snapGap < -0.05 * Math.max(snapUses, 1) ? "Over Funded" : (snapGap <= 0 ? "Fully Funded" : "Partially Funded"));
    }
    var out = { strategy: strat, arv: arv, purchase_price: price, rehab: rehab, rehab_calc: rehabCalc,
      rehab_mode: rehabMode, appliances: appliances, furnishing: furnishing, conversion: conversion,
      total_project: totalProject, holding_cost: holdingTotal, uses: uses, funded: funded, gap: gap,
      cash_in: cashIn, cash_needed: cashNeeded, financing_costs: financingCosts, funding_status: fundingStatus,
      uses_override: usesOverride || null, snap_uses: snapUses, snap_gap: snapGap, snap_status: snapStatus,
      debt_service_mo: debtService, entry_fee: entryFee,
      cash_contrib: cashEquity, points_fees: pointsFees, closing_costs: closing,
      debt_lines: debtLines, md_payment_mo: mdPayment, md_base_mo: mdBase, md_escrow_mo: mdEscrow, md_addons: mdAddons };

    if (strat === "Fix & Flip") {
      var sale = N(s.sale_price) || arv;
      var commPct = N(s.commission_pct) || 0; if (s.commission_pct === "" || s.commission_pct == null) commPct = 6;
      var closePct = N(s.closing_pct) || 0; if (s.closing_pct === "" || s.closing_pct == null) closePct = 2;
      var commAmt = sale * commPct / 100, closeAmt = sale * closePct / 100;
      var net = sale - commAmt - closeAmt;
      var financingCarry = carryMo * hMonths;
      var financingTotal = financingCosts + financingCarry; /* v2.1: profit now includes financing */
      var profit = net - totalProject - holdingTotal - financingTotal;
      var allIn = totalProject + holdingTotal + financingTotal + commAmt + closeAmt;
      var roi = cashIn ? profit / cashIn * 100 : null;
      var margin = sale ? profit / sale * 100 : null;
      var ann = (cashIn && hMonths) ? profit / cashIn * 100 * 12 / hMonths : null;
      function flip(sale2, rehab2, hold2) {
        var n2 = sale2 * (1 - commPct / 100 - closePct / 100);
        var proj = price + rehab2 + appliances + furnishing;
        var fin2 = financingCosts + carryMo * (holdingMo ? hold2 / holdingMo : hMonths);
        return { sale: sale2, all_in: proj + hold2 + fin2 + sale2 * (commPct + closePct) / 100, net: n2 - proj - hold2 - fin2 };
      }
      var scen = { "Downside (-10%)": flip(sale * 0.9, rehab, holdingTotal),
        "Base Case (0%)": flip(sale, rehab, holdingTotal),
        "Upside (+10%)": flip(sale * 1.1, rehab, holdingTotal),
        "Fully Booked (0% vacancy)": flip(sale, rehab, holdingTotal),
        "Overrun (rehab +20%, +3 mo)": flip(sale, rehab * 1.2, holdingMo * (hMonths + 3)) };
      function cs2(x, t) { return t ? clamp((x / t) * 100, 0, 100) : 0; }
      var parts = { "Profit margin (50%)": Math.round(0.5 * cs2((margin || 0) / 100, 0.30) * 10) / 10,
                    "ROI on cash (50%)": Math.round(0.5 * cs2((roi || 0) / 100, 0.50) * 10) / 10 };
      var fscore = clamp(Math.round(parts["Profit margin (50%)"] + parts["ROI on cash (50%)"]), 0, 99);
      out.sale_price = sale; out.commission_amt = commAmt; out.closing_amt = closeAmt;
      out.net_proceeds = net; out.all_in = allIn; out.profit = profit; out.roi = roi;
      out.financing_carry = financingCarry; out.financing_total = financingTotal;
      out.annualized_roi = ann; out.margin_pct = margin; out.rule70_mao = arv * 0.70 - rehab;
      out.scenarios = scen; out.score = fscore; out.label = scoreLabel(fscore);
      out.score_parts = parts; out.penalties = [];
      out.insight = profit < 0 ? "The flip loses money at these numbers." : insightFromParts(parts);
      return out;
    }

    /* rentals — v1.5: $0 taxes/insurance sanity defaults fire ONLY when the deal has no
       loan payment (they live inside PITI when one exists — ReiSearch/Senisa behavior). */
    var penalties = [], flags = [];
    var taxesMo = N(s.taxes_mo), insMo = N(s.insurance_mo);
    /* v1.6 standard-numbers rule (Sherif): standard costs AUTO-POPULATE visibly in
       their fields as editable defaults — real annual tax from the property record
       / 12 where known, else a labeled estimate (mirrors app.py). writeOutputs
       writes the estimate into the field (flagged) so fields and totals tie out;
       the estimate withdraws when a loan payment exists (taxes/insurance then live
       inside PITI) and never touches a value the user typed. */
    var propTaxMo = N(UW.propTaxMo), taxesEst = false, insEst = false;
    function estFlag(k) { return String(s[k]) === "1" || s[k] === 1; }
    /* v1.7 (mirror of app.py): populated, flagged auto-estimates carry NO score
       penalty — they are labeled data in the field, not missing data (Garland
       clean-state root-cause: 80 vs ReiSearch 93; flags remain for transparency). */
    if (!pitiIncluded && taxesMo === 0) {
      taxesMo = propTaxMo || ((price || arv) ? Math.round((price || arv) * 0.011 / 12) : 0);
      if (taxesMo > 0) {
        taxesEst = true;
        flags.push("Taxes estimated — verify with county");
      }
    } else if (!pitiIncluded && estFlag("taxes_est") && taxesMo > 0) {
      taxesEst = true;
      flags.push("Taxes estimated — verify with county");
    }
    if (!pitiIncluded && insMo === 0) {
      insMo = 180; insEst = true;
      flags.push("Insurance estimated");
    } else if (!pitiIncluded && estFlag("insurance_est") && insMo > 0) {
      insEst = true;
      flags.push("Insurance estimated");
    }
    var gross = 0, vacLoss = 0, other = 0, roomsN = 1, cleaningIncome = 0, amenityRows = 0;
    if (strat === "Co-Living") {
      var rows = s.rooms_rows || [];
      rows.forEach(function (r) {
        var mo = (r.rent_mo === "" || r.rent_mo === null || r.rent_mo === undefined) ? N(r.rent_wk) * 52 / 12 : N(r.rent_mo);
        gross += mo; vacLoss += mo * (N(r.vacancy) || 0) / 100; amenityRows += N(r.amenity_mo);
      });
      roomsN = rows.length || 4;
      var laundryMo = N(s.laundry_amt) * ((s.laundry_period || "wk") === "wk" ? 52 / 12 : 1);
      other = amenityRows + N(s.amenities_mo) + N(s.late_fees_mo) + laundryMo + N(s.misc_income_mo);
      out.laundry_mo = laundryMo;
    } else if (strat === "Short Term") {
      var units = N(s.units) || 1; roomsN = units;
      gross = N(s.nightly_rate) * 30 * (N(s.occupancy_pct) / 100) * units;
      var stays = (30 / (N(s.avg_stay_nights) || 3)) * units;
      cleaningIncome = stays * N(s.cleaning_fee);
      other = N(s.pet_fees_mo) + N(s.parking_mo) + N(s.misc_mo) + cleaningIncome;
    } else {
      var u2 = N(s.units) || 1; roomsN = u2;
      gross = N(s.rent_per_unit) * u2;
      vacLoss = gross * (N(s.vacancy_pct) / 100);
      other = N(s.pet_mo) + N(s.parking_mo) + N(s.storage_mo) + N(s.laundry_mo) + N(s.misc_mo);
    }
    var egi = gross - vacLoss + other;
    var daysDef = strat === "Co-Living" ? 10 : 0;
    var bookingDays = (s.booking_days === "" || s.booking_days == null) ? daysDef : N(s.booking_days);
    var bookingFee;
    if (strat === "Co-Living") {
      /* PadSplit first-days fee: (weekly rent / 7) x days, per room, spread over the year */
      bookingFee = 0;
      (s.rooms_rows || []).forEach(function (r) { bookingFee += N(r.rent_wk) / 7 * bookingDays; });
      bookingFee = bookingFee / 12;
    } else bookingFee = gross ? bookingDays * (gross / 30) / 12 : 0;
    var serviceFee = egi * (N(s.service_fee_pct) / 100);
    var mgmtFee = ((s.mgmt_fee_basis || "gross") === "gross" ? gross : egi) * (N(s.mgmt_fee_pct) / 100);
    var platformFee = strat === "Short Term" ? gross * (N(s.platform_pct) / 100) : 0;
    var feesCat = bookingFee + serviceFee + mgmtFee + platformFee;
    var util = N(s.electric_mo) + N(s.water_mo) + N(s.gas_mo) + N(s.internet_mo);
    var cleaning = N(s.cleaning_mo);
    var maint = N(s.landscaping_mo) + N(s.pest_mo) + N(s.handyman_mo);
    var mgmtLines = N(s.property_manager_mo) + N(s.turnover_reserve_mo) + N(s.additional_insurance_mo);
    var hoa = N(s.hoa_mo);
    var opexCats = { "Booking & Service Fees": feesCat, "Utilities": util, "Cleaning Service": cleaning,
      "Maintenance": maint, "Management": mgmtLines, "Taxes, Insurance & HOA": taxesMo + insMo + hoa };
    var opex = 0; Object.keys(opexCats).forEach(function (k) { opex += opexCats[k]; });
    /* v2.3: the old -10 "no utilities/maintenance" penalty is REMOVED — an
       app-side invention contradicted by ReiSearch's measured states (blank
       utilities/maintenance at score 93 Cash / 73 Conventional). Mirrors app.py. */
    var noi = egi - opex, cf = noi - debtService;
    var value = arv || price;
    var metrics = { cf_per_room: roomsN ? cf / roomsN : 0,
      dscr: debtService ? noi / debtService : (noi > 0 ? 1.5 : 0),
      coc: cashNeeded ? cf * 12 / cashNeeded : 0,
      cap_rate: value ? noi * 12 / value : 0,
      breakeven: (gross + other) ? (opex + debtService) / (gross + other) : 1.3,
      downside_cf: egi * 0.9 - opex - debtService };
    /* v2.9 score-factor inputs (mirrors app.py): Cash-on-cash is scored
       on ReiSearch's Cash Needed (v2.9: points/fees + entry fee only)
       and is "no data" — excluded from the weighted average — when that
       is $0 (Pass #2, 8 states). SUPERSEDES the v2.4 reading
       (cashIn - cashEquity): on the shipped saved state the old score
       denominator was the $3,000 closing costs (CoC 603.9%) while the
       summary showed 4.2% on $428,900 — one definition feeds both now
       (Hard Money: both -27.7% on $6,397). */
    metrics.dscr_val = debtService ? metrics.dscr : null;
    metrics.coc_val = cashNeeded > 0 ? cf * 12 / cashNeeded : null;
    metrics.downside_cf_per_room = roomsN ? metrics.downside_cf / roomsN : 0;
    function scenR(mult, noVac) {
      var e2;
      if (noVac && strat === "Short Term") { var occ = N(s.occupancy_pct); e2 = (occ ? gross / (occ / 100) : gross) + other; }
      else if (noVac) e2 = gross + other;
      else e2 = (gross - vacLoss) * mult + other;
      var noiS = e2 - opex;
      return { revenue: e2, expenses: opex, loan: debtService, net: e2 - opex - debtService,
        dscr: debtService ? noiS / debtService : (noiS > 0 ? 1.5 : 0),
        cap_rate_pct: value ? noiS * 12 / value * 100 : 0,
        coc_pct: cashNeeded ? (e2 - opex - debtService) * 12 / cashNeeded * 100 : null };
    }
    var scen2 = { "Downside (-10%)": scenR(0.9), "Base Case (0%)": scenR(1.0), "Upside (+10%)": scenR(1.1) };
    scen2["Fully Booked (0% vacancy)"] = scenR(1.0, true);
    /* v2.4 penalty (measured, Addendum 2): ReiSearch docks 5 points when
       operating expenses are an unrealistically low share of revenue, with
       the note below. v2.8 (evening pass 2026-11-03): the threshold is now
       MEASURED to fire at opex/revenue 0%, 3.7%, 5.4%, 7.2%, 10.8%, 14.4%
       and 18.0%, and ABSENT at Senisa's 35% — the true threshold lies in
       (18%, 35%]; 20% is the narrowest round bound consistent with every
       anchor, flagged as still unverified within that interval. The note
       renders the ratio rounded to the nearest INTEGER (measured: "only
       5%" at 5.4%, "7%" at 7.2%, "11%" at 10.8%, "14%" at 14.4%, "18%"
       at 18.0%). Mirrors app.py. */
    if (egi > 0 && opex / egi < 0.20) {
      var _or = opex / egi * 100;
      penalties.push({ points: 5, reason: "Operating expenses are only " +
        roundHalfUp(_or) +
        "% of revenue, which looks unrealistically low." });
    }
    var sc = scoreRental(metrics, penalties);
    var refiGross = arv ? Math.round(0.75 * arv) : 0;
    var refiNet = Math.max(0, refiGross - debtAmt);
    out.gross_mo = gross; out.vacancy_loss_mo = vacLoss; out.other_income_mo = other;
    out.cleaning_income_mo = cleaningIncome; out.fees_mo = feesCat; out.booking_fee_mo = bookingFee;
    out.service_fee_mo = serviceFee; out.mgmt_fee_mo = mgmtFee; out.platform_fee_mo = platformFee;
    out.egi_mo = egi; out.opex_mo = opex; out.opex_cats = opexCats; out.taxes_mo = taxesMo; out.insurance_mo = insMo;
    out.taxes_est = taxesEst; out.insurance_est = insEst;
    out.noi_mo = noi; out.cashflow_mo = cf; out.cf_per_room = metrics.cf_per_room;
    out.cash_per_room = roomsN ? cashNeeded / roomsN : null;
    out.dscr = debtService ? Math.round(metrics.dscr * 100) / 100 : null; /* v2.1: N/A with no debt */
    out.coc_pct = cashNeeded ? metrics.coc * 100 : null; out.cap_rate_pct = metrics.cap_rate * 100;
    out.breakeven_pct = metrics.breakeven * 100;
    out.payback_yrs = metrics.coc > 0 ? Math.round(10 / metrics.coc) / 10 : null;
    out.scenarios = scen2; out.rooms = roomsN;
    out.refi_proceeds = refiGross; out.remaining_in_deal = Math.max(0, cashIn - refiNet);
    out.paydown_y1 = paydownY1; out.cashflow_y1 = cf * 12;
    out.total_return_y1 = cf * 12 + paydownY1;
    out.total_roi_y1 = cashNeeded ? (cf * 12 + paydownY1) / cashNeeded * 100 : null;
    out.score = sc.score; out.label = sc.label; out.score_parts = sc.parts; out.score_factors = sc.factors;
    out.penalties = sc.penalties; out.flags = flags;
    out.insight = insightFromFactors(sc.factors);
    out.amenity_mo = amenityRows + N(s.amenities_mo);
    out.expenses_total = opex + debtService;
    return out;
  }

  /* ---------------- DOM: rows ---------------- */
  var FUNDING_TYPES = ["Conventional", "DSCR", "Hard Money", "Private Money", "Bridge", "HELOC", "Seller Financing", "Subject-To", "Cash", "Equity"];
  /* v2.8 (A8, evening pass 2026-11-03): Add Funding Source dialog card kinds —
     Amount-only types (single Amount field; contribute to Funding Added only)
     and single-Term assumption types (Loan Position + one Term, no
     Amortization Term). Mirrors app.py. */
  var AMOUNT_ONLY_TYPES = ["Private Money Partner", "JV Equity", "Reserves"];
  var ASSUMP_TYPES = ["Loan Assumption", "Wraparound"];
  function cardKind(f) {
    if (f.kind) return f.kind;
    if (AMOUNT_ONLY_TYPES.indexOf(f.type) >= 0) return "amount";
    if (ASSUMP_TYPES.indexOf(f.type) >= 0) return "assump";
    return "loan";
  }
  var LOAN_TYPES = ["FHA", "Conventional", "DSCR", "Hard Money", "Private Money", "Seller Financing", "VA", "USDA", "Bridge", "HELOC", "Cash", "Other"];
  var PTYPES = [["amortized", "Amortized"], ["interest_only", "Interest Only"], ["principal_only", "Principal Only"], ["fixed", "Fixed Payment"]];
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function numInput(cls, val) { var i = el("input", cls); i.type = "number"; i.step = "any"; i.value = (val === undefined || val === null) ? "" : val; return i; }
  function delBtn() { var b = el("button", "btn sm danger r-del", "✕"); b.type = "button"; return b; }
  function selOf(cls, opts, val) {
    var sel = el("select", cls);
    opts.forEach(function (t) { var o = el("option", "", t); o.value = t; sel.appendChild(o); });
    if (val && opts.indexOf(val) < 0) { var o2 = el("option", "", val); o2.value = val; sel.appendChild(o2); }
    sel.value = val || opts[0];
    return sel;
  }

  function rehabRoomRow(r) {
    var d = el("div", "rrow"); d.dataset.kind = "rehab_room";
    var lab = el("input", "r-label"); lab.type = "text"; lab.value = r.label || "";
    d.appendChild(lab); d.appendChild(numInput("r-mat", r.material)); d.appendChild(numInput("r-lab", r.labor));
    d.appendChild(el("span", "r-tot", "$0")); d.appendChild(delBtn()); return d;
  }
  function rehabFlatRow(key, label, r) {
    var d = el("div", "rrow"); d.dataset.kind = "rehab_flat"; d.dataset.flat = key;
    d.appendChild(el("span", "r-flabel", label));
    d.appendChild(numInput("r-mat", r ? r.material : 0)); d.appendChild(numInput("r-lab", r ? r.labor : 0));
    d.appendChild(el("span", "r-tot", "$0")); d.appendChild(el("span", "", "")); return d;
  }
  function unitRow(kind, r) {
    var d = el("div", "rrow"); d.dataset.kind = kind; if (r.group) d.dataset.group = r.group;
    var lab = el("input", "r-label"); lab.type = "text"; lab.value = r.label || "";
    d.appendChild(lab); d.appendChild(numInput("r-qty", r.qty)); d.appendChild(numInput("r-unit", r.unit));
    d.appendChild(el("span", "r-tot", "$0")); d.appendChild(delBtn()); return d;
  }
  /* v2.3: ReiSearch term control — a small bordered value box (~45-60px) with a
     two-button Months|Years segmented control immediately to its right. The
     hidden input keeps the state machine (and older tooling) reading one value. */
  function unitSeg(hiddenCls, unit) {
    var w = el("span", "funit");
    ["Months", "Years"].forEach(function (u) {
      var b = el("button", "fu" + (u === unit ? " on" : ""), u);
      b.type = "button"; b.dataset.u = u; w.appendChild(b);
    });
    var h = el("input", hiddenCls); h.type = "hidden"; h.value = unit;
    w.appendChild(h);
    return w;
  }
  function setUnitSeg(cardEl, hiddenCls, unit) {
    var h = cardEl.querySelector("." + hiddenCls); if (h) h.value = unit;
    var w = h ? h.closest(".funit") : null;
    if (w) w.querySelectorAll(".fu").forEach(function (b) { b.classList.toggle("on", b.dataset.u === unit); });
  }
  function termControl(valCls, unitCls, val, unit) {
    var w = el("div", "fterm");
    w.appendChild(numInput(valCls, val));
    w.appendChild(unitSeg(unitCls, unit));
    return w;
  }
  function fundingCard(f) {
    /* v2.3 anatomy (ReiSearch pass 2026-11-03, top to bottom): title + lender
       name, Loan Position, Open Calculator, Remove; Amount; Use % of; Rate;
       Interest Type Simple/Compound; Loan Term (payment-invariant); Amortization
       Term (drives the payment); Points; Fees; escrow Taxes/Insurance (the
       Subject-To card carries them on ReiSearch); Payment Type as a full-width
       segmented control with the fixed-amount box INSIDE the Fixed segment;
       bottom line "Monthly payment" + amount or "Monthly payment not set". */
    var d = el("div", "fcard"); d.dataset.kind = "funding";
    d.dataset.type = f.type || "Conventional"; d.dataset.preset = f.preset || f.type || "Conventional";
    d.dataset.ptype = f.payment_type || "amortized";
    d.dataset.itype = f.interest_type || "simple";
    var kind = cardKind(f); d.dataset.cardkind = kind;
    if (kind === "amount") {
      /* v2.8 (A8, measured): Private Money Partner / JV Equity / Reserves —
         ONE field only: Amount. No position, rate, terms, or payment type. */
      var ahead = el("div", "fcard-h");
      ahead.appendChild(el("span", "f-title", ""));
      ahead.appendChild(delBtn());
      d.appendChild(ahead);
      var agrid = el("div", "fgrid");
      var af = el("div", "ffield"); af.appendChild(el("label", "", "Amount $"));
      af.appendChild(numInput("f-amt", f.amount)); agrid.appendChild(af);
      d.appendChild(agrid);
      return d;
    }
    if (kind === "assump") {
      /* v2.8 (A8, measured): Loan Assumption / Wraparound — Loan Position
         dropdown ("Select position" / 1st / 2nd / 3rd / Unrecorded), Amount,
         Interest Rate 0%, Simple/Compound, ONE Term (Months/Years, Years
         default), Points 0%, Fees, Payment Type segmented, Monthly payment
         line. NO Amortization Term field. */
      var uhead = el("div", "fcard-h");
      uhead.appendChild(el("span", "f-title", ""));
      uhead.appendChild(delBtn());
      d.appendChild(uhead);
      var ugrid = el("div", "fgrid");
      function ufield(lab, node, cls) { var w = el("div", "ffield" + (cls ? " " + cls : "")); w.appendChild(el("label", "", lab)); w.appendChild(node); return w; }
      var posSel = el("select", "f-pos");
      [["", "Select position"], ["1st", "1st"], ["2nd", "2nd"], ["3rd", "3rd"], ["Unrecorded", "Unrecorded"]].forEach(function (o) {
        var op = el("option", "", o[1]); op.value = o[0]; posSel.appendChild(op);
      });
      posSel.value = f.position || "";
      ugrid.appendChild(ufield("Loan Position", posSel));
      ugrid.appendChild(ufield("Amount $", numInput("f-amt", f.amount)));
      ugrid.appendChild(ufield("Interest Rate %", numInput("f-rate", (f.rate === undefined || f.rate === null || f.rate === "") ? 0 : f.rate)));
      var uitW = el("div", "fitype");
      [["simple", "Simple"], ["compound", "Compound"]].forEach(function (it) {
        var bb = el("button", "f-it" + (d.dataset.itype === it[0] ? " on" : ""), it[1]);
        bb.type = "button"; bb.dataset.it = it[0]; uitW.appendChild(bb);
      });
      ugrid.appendChild(ufield("Interest Type", uitW));
      ugrid.appendChild(ufield("Term", termControl("f-termval", "f-termunit", f.term_value, f.term_unit || "Years"), "fterm-field"));
      ugrid.appendChild(ufield("Points %", numInput("f-points", (f.points === undefined || f.points === null || f.points === "") ? 0 : f.points)));
      ugrid.appendChild(ufield("Fees $", numInput("f-fees", f.fees)));
      d.appendChild(ugrid);
      var useg = el("div", "fseg");
      useg.appendChild(el("span", "fseg-lab", "Payment Type"));
      PTYPES.forEach(function (pt) {
        if (pt[0] === "fixed") {
          var ufb = el("div", "f-pt f-pt-fixed" + (d.dataset.ptype === "fixed" ? " on" : ""));
          ufb.dataset.pt = "fixed";
          ufb.appendChild(el("span", "", pt[1]));
          var ufx = numInput("f-fixed", f.fixed_payment); ufx.placeholder = "$/mo";
          ufb.appendChild(ufx);
          useg.appendChild(ufb);
        } else {
          var ub = el("button", "f-pt" + (d.dataset.ptype === pt[0] ? " on" : ""), pt[1]);
          ub.type = "button"; ub.dataset.pt = pt[0]; useg.appendChild(ub);
        }
      });
      d.appendChild(useg);
      var upay = el("div", "fpay", "Monthly payment ");
      upay.appendChild(el("b", "f-pay", "$0"));
      d.appendChild(upay);
      return d;
    }
    var head = el("div", "fcard-h");
    head.appendChild(el("span", "f-title", ""));
    var lender = el("input", "f-lender"); lender.type = "text"; lender.placeholder = "Lender name";
    lender.value = f.lender || ""; head.appendChild(lender);
    var posLab = el("label", "f-poslab", "Loan Position ");
    posLab.appendChild(selOf("f-pos", ["1st Position", "2nd Position"], f.position || "1st Position"));
    head.appendChild(posLab);
    var calc = el("a", "btn sm sec", "Open Calculator"); calc.href = "#amortcard"; head.appendChild(calc);
    head.appendChild(delBtn());
    d.appendChild(head);
    var grid = el("div", "fgrid");
    function field(lab, node, cls) { var w = el("div", "ffield" + (cls ? " " + cls : "")); w.appendChild(el("label", "", lab)); w.appendChild(node); return w; }
    grid.appendChild(field("Existing Loan Type", selOf("f-loantype", LOAN_TYPES, f.loan_type || f.type || "Conventional")));
    grid.appendChild(field("Loan Balance $", numInput("f-amt", f.amount)));
    var useW = el("div", "fuse");
    useW.appendChild(numInput("f-pct", f.pct));
    var basis = selOf("f-basis", ["of Purchase Price", "of ARV"], f.basis === "arv" ? "of ARV" : "of Purchase Price");
    basis.dataset.basis = f.basis === "arv" ? "arv" : "price";
    useW.appendChild(basis);
    grid.appendChild(field("Use % of", useW));
    grid.appendChild(field("Interest Rate %", numInput("f-rate", f.rate)));
    var itW = el("div", "fitype");
    [["simple", "Simple"], ["compound", "Compound"]].forEach(function (it) {
      var b = el("button", "f-it" + (d.dataset.itype === it[0] ? " on" : ""), it[1]);
      b.type = "button"; b.dataset.it = it[0]; itW.appendChild(b);
    });
    grid.appendChild(field("Interest Type", itW));
    /* legacy single-term states arrive migrated server-side (amort = term);
       the same rule is applied here for any in-flight unmigrated state. */
    var aVal = (f.amort_value === undefined || f.amort_value === null) ? f.term_value : f.amort_value;
    var aUnit = (f.amort_value === undefined || f.amort_value === null) ? (f.term_unit || "Months") : (f.amort_unit || "Months");
    grid.appendChild(field("Loan Term", termControl("f-termval", "f-termunit", f.term_value, f.term_unit || "Months"), "fterm-field"));
    grid.appendChild(field("Amortization Term", termControl("f-amortval", "f-amortunit", aVal, aUnit), "fterm-field"));
    grid.appendChild(field("Points %", numInput("f-points", f.points)));
    grid.appendChild(field("Fees $", numInput("f-fees", f.fees)));
    grid.appendChild(field("Taxes $/mo (escrow)", numInput("f-taxes", f.taxes_mo)));
    grid.appendChild(field("Insurance $/mo (escrow)", numInput("f-ins", f.insurance_mo)));
    d.appendChild(grid);
    var seg = el("div", "fseg");
    seg.appendChild(el("span", "fseg-lab", "Payment Type"));
    PTYPES.forEach(function (pt) {
      if (pt[0] === "fixed") {
        var fb = el("div", "f-pt f-pt-fixed" + (d.dataset.ptype === "fixed" ? " on" : ""));
        fb.dataset.pt = "fixed";
        fb.appendChild(el("span", "", pt[1]));
        var fx = numInput("f-fixed", f.fixed_payment); fx.placeholder = "$/mo";
        fb.appendChild(fx);
        seg.appendChild(fb);
      } else {
        var b = el("button", "f-pt" + (d.dataset.ptype === pt[0] ? " on" : ""), pt[1]);
        b.type = "button"; b.dataset.pt = pt[0]; seg.appendChild(b);
      }
    });
    d.appendChild(seg);
    var pay = el("div", "fpay", "Monthly payment ");
    pay.appendChild(el("b", "f-pay", "$0"));
    d.appendChild(pay);
    return d;
  }
  function readCard(d) {
    /* v2.8 (A8): variant cards read only the fields they render. */
    if (d.dataset.cardkind === "amount") {
      return { kind: "amount", type: d.dataset.type, preset: d.dataset.preset || d.dataset.type,
        loan_type: d.dataset.type, amount: N(d.querySelector(".f-amt").value) };
    }
    if (d.dataset.cardkind === "assump") {
      var utv = d.querySelector(".f-termval").value;
      var utu = d.querySelector(".f-termunit").value;
      var ufx2 = d.querySelector(".f-fixed");
      return { kind: "assump", type: d.dataset.type, preset: d.dataset.preset || d.dataset.type,
        loan_type: d.dataset.type, position: d.querySelector(".f-pos").value,
        amount: N(d.querySelector(".f-amt").value), rate: N(d.querySelector(".f-rate").value),
        interest_type: d.dataset.itype || "simple",
        /* the single Term is stored as BOTH term fields so the standard
           engine prices it (the flagged inference, mirrored in app.py) */
        term_value: utv === "" ? "" : N(utv), term_unit: utu,
        amort_value: utv === "" ? "" : N(utv), amort_unit: utu,
        points: N(d.querySelector(".f-points").value), fees: N(d.querySelector(".f-fees").value),
        payment_type: d.dataset.ptype || "amortized",
        fixed_payment: (ufx2 && ufx2.value !== "") ? N(ufx2.value) : "",
        taxes_mo: 0, insurance_mo: 0 };
    }
    var tv = d.querySelector(".f-termval").value;
    var av = d.querySelector(".f-amortval").value;
    var basisEl = d.querySelector(".f-basis");
    return { type: d.dataset.type || "Conventional", preset: d.dataset.preset || "Conventional",
      loan_type: d.querySelector(".f-loantype").value, position: d.querySelector(".f-pos").value,
      lender: d.querySelector(".f-lender").value,
      amount: N(d.querySelector(".f-amt").value), rate: N(d.querySelector(".f-rate").value),
      pct: d.querySelector(".f-pct").value === "" ? "" : N(d.querySelector(".f-pct").value),
      basis: basisEl && basisEl.value === "of ARV" ? "arv" : "price",
      interest_type: d.dataset.itype || "simple",
      term_value: tv === "" ? "" : N(tv), term_unit: d.querySelector(".f-termunit").value,
      amort_value: av === "" ? "" : N(av), amort_unit: d.querySelector(".f-amortunit").value,
      points: N(d.querySelector(".f-points").value), fees: N(d.querySelector(".f-fees").value),
      payment_type: d.dataset.ptype || "amortized",
      fixed_payment: d.querySelector(".f-fixed").value === "" ? "" : N(d.querySelector(".f-fixed").value),
      taxes_mo: N(d.querySelector(".f-taxes").value), insurance_mo: N(d.querySelector(".f-ins").value) };
  }
  function roomRow(r, idx) {
    var d = el("div", "rrow r15"); d.dataset.kind = "rooms_rows";
    d.appendChild(el("span", "r-roomno", "Bed " + (idx + 1)));
    d.appendChild(selOf("r-bath", ["Private", "Shared"], r.bath || "Shared"));
    d.appendChild(selOf("r-bathgroup", ["A", "B"], r.bath_group || "A"));
    d.appendChild(numInput("r-rentwk", r.rent_wk));
    d.appendChild(numInput("r-rentmo", r.rent_mo));
    var vw = el("span", "r-vacwrap");
    var minus = el("button", "r-vacstep", "−"); minus.type = "button"; minus.dataset.vacstep = "-1";
    var plus = el("button", "r-vacstep", "+"); plus.type = "button"; plus.dataset.vacstep = "1";
    vw.appendChild(minus); vw.appendChild(numInput("r-vac", r.vacancy)); vw.appendChild(plus);
    d.appendChild(vw);
    var ew = numInput("r-effwk", ""); ew.readOnly = true; ew.tabIndex = -1; d.appendChild(ew);
    d.appendChild(el("span", "r-effmo", "$0"));
    var am = el("span", "r-amenwrap");
    var ab = el("button", "btn sm sec r-amen", "+ Amenities"); ab.type = "button";
    var ai = numInput("r-amenmo", r.amenity_mo || 0); ai.placeholder = "$/mo";
    if (!(N(r.amenity_mo) > 0)) ai.style.display = "none";
    am.appendChild(ab); am.appendChild(ai);
    d.appendChild(am);
    d.appendChild(delBtn());
    return d;
  }
  function readRoom(d) {
    return { bath: d.querySelector(".r-bath").value, bath_group: d.querySelector(".r-bathgroup").value,
      rent_wk: N(d.querySelector(".r-rentwk").value), rent_mo: N(d.querySelector(".r-rentmo").value),
      vacancy: N(d.querySelector(".r-vac").value), amenity_mo: N(d.querySelector(".r-amenmo").value) };
  }

  function setRows(panel, kind, rows) {
    var box = panel.querySelector('[data-rows="' + kind + '"]'); if (!box) return;
    box.innerHTML = "";
    if (kind === "rehab_flats") {
      Object.keys(UW.flatLabels || {}).forEach(function (k) {
        if (rows && rows[k]) box.appendChild(rehabFlatRow(k, UW.flatLabels[k], rows[k]));
      });
      return;
    }
    (rows || []).forEach(function (r, i) {
      if (kind === "rehab_rooms") box.appendChild(rehabRoomRow(r));
      else if (kind === "funding") box.appendChild(fundingCard(r));
      else if (kind === "rooms_rows") box.appendChild(roomRow(r, i));
      else if (kind === "furnishing") {
        var prev = box.lastElementChild;
        if (!prev || prev.dataset.group !== (r.group || "")) {
          var h = el("div", "fgroup", r.group || "General"); h.dataset.group = r.group || ""; box.appendChild(h);
        }
        box.appendChild(unitRow(kind, r));
      }
      else box.appendChild(unitRow(kind, r));
    });
  }
  function readRows(panel, kind) {
    var box = panel.querySelector('[data-rows="' + kind + '"]'); if (!box) return kind === "rehab_flats" ? {} : [];
    var out = [];
    if (kind === "funding") { box.querySelectorAll(".fcard").forEach(function (d) { out.push(readCard(d)); }); return out; }
    if (kind === "rooms_rows") { box.querySelectorAll(".rrow").forEach(function (d) { out.push(readRoom(d)); }); return out; }
    box.querySelectorAll(".rrow").forEach(function (d) {
      var k = d.dataset.kind;
      if (k === "rehab_room") out.push({ label: d.querySelector(".r-label").value, material: N(d.querySelector(".r-mat").value), labor: N(d.querySelector(".r-lab").value) });
      else if (k === "rehab_flat") { /* handled below */ }
      else out.push({ group: d.dataset.group || undefined, label: d.querySelector(".r-label").value, qty: N(d.querySelector(".r-qty").value), unit: N(d.querySelector(".r-unit").value) });
    });
    if (kind === "rehab_flats") {
      var o = {};
      box.querySelectorAll(".rrow").forEach(function (d) { o[d.dataset.flat] = { material: N(d.querySelector(".r-mat").value), labor: N(d.querySelector(".r-lab").value) }; });
      return o;
    }
    return out;
  }
  function readState(panel) {
    var s = {};
    panel.querySelectorAll("input[name], select[name]").forEach(function (e) {
      if (e.type === "hidden") return; s[e.name] = e.value;
    });
    /* auto-estimate flags ride along (hidden inputs the main loop skips) */
    panel.querySelectorAll("input[data-estflag]").forEach(function (e) { s[e.name] = e.value; });
    /* v2.8 (A10): state-carrying hidden inputs (e.g. uses_override) */
    panel.querySelectorAll("input[data-stateval]").forEach(function (e) { s[e.name] = e.value; });
    s.rehab_rooms = readRows(panel, "rehab_rooms");
    s.rehab_flats = readRows(panel, "rehab_flats");
    s.appliances = readRows(panel, "appliances");
    s.conversion = readRows(panel, "conversion");
    s.furnishing = readRows(panel, "furnishing");
    s.funding = readRows(panel, "funding");
    s.rooms_rows = readRows(panel, "rooms_rows");
    return s;
  }

  /* ---------------- outputs ---------------- */
  var PCTKEYS = { coc_pct: 1, cap_rate_pct: 1, breakeven_pct: 1, margin_pct: 1, roi: 1, annualized_roi: 1, total_roi_y1: 1 };
  function fmtFor(key, r, s) {
    var v = r[key];
    if (key === "score") return (v === null || v === undefined) ? "—" : String(v);
    if (key === "score_label") return r.label || "";
    if (key === "dscr") return (v === null || v === undefined) ? "N/A" : (Math.round(v * 100) / 100).toFixed(2);
    if (PCTKEYS[key]) return PC(v);
    if (key === "rooms") return (v === null || v === undefined) ? "—" : String(v);
    return M(v);
  }
  function barsHtml(rows) {
    var max = 0; rows.forEach(function (r) { max = Math.max(max, Math.abs(r[1])); });
    if (!max) max = 1;
    return rows.map(function (r) {
      var w = Math.max(2, Math.abs(r[1]) / max * 100);
      return '<div class="bar-row"><span>' + r[0] + '</span><div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + r[2] + '"></div></div><b>' + M(r[1]) + '</b></div>';
    }).join("");
  }
  function amortSchedule(amt, rate, yrs) {
    var pmt = loanPayment(amt, rate, yrs), r = rate / 100 / 12, bal = amt, rows = [];
    var n = Math.min(Math.ceil(yrs), 40);
    for (var y = 1; y <= n && bal > 0.5; y++) {
      var ti = 0, tp = 0;
      for (var i = 0; i < 12 && bal > 0; i++) { var inter = bal * r, prin = Math.min(pmt - inter, bal); ti += inter; tp += prin; bal -= prin; }
      rows.push({ year: y, interest: ti, principal: tp, balance: Math.max(bal, 0) });
    }
    return { payment: pmt, rows: rows };
  }
  function writeOutputs(panel, s, r) {
    var strat = panel.dataset.strategy;
    function hMonthsOf() { var hp = N(s.holding_period); return (s.holding_unit || "Months") === "Months" ? hp : hp / 30; }
    var raw = {
      cat_fees: r.opex_cats ? r.opex_cats["Booking & Service Fees"] : 0,
      cat_utilities: r.opex_cats ? r.opex_cats["Utilities"] : 0,
      cat_cleaning: r.opex_cats ? r.opex_cats["Cleaning Service"] : 0,
      cat_maintenance: r.opex_cats ? r.opex_cats["Maintenance"] : 0,
      cat_management: r.opex_cats ? r.opex_cats["Management"] : 0,
      cat_taxes: r.opex_cats ? r.opex_cats["Taxes, Insurance & HOA"] : 0,
      rev_other_fees: N(s.late_fees_mo) + N(s.misc_income_mo),
      rev_amenity: (r.amenity_mo || 0) + (r.laundry_mo || 0)
    };
    function rawFor(key) { return key in raw ? raw[key] : r[key]; }
    var special = {
      rehab_total: M(r.rehab), rehab_mode_label: r.rehab_mode === "est" ? "ESTIMATED TOTAL" : "CALCULATED TOTAL",
      rehab_interior_total: M((s.rehab_rooms || []).reduce(function (a, x) { return a + N(x.material) + N(x.labor); }, 0)),
      appliances_total: M(r.appliances), conversion_total: M(r.conversion), furnishing_total: M(r.furnishing),
      cat_fees: M(raw.cat_fees), cat_utilities: M(raw.cat_utilities), cat_cleaning: M(raw.cat_cleaning),
      cat_maintenance: M(raw.cat_maintenance), cat_management: M(raw.cat_management), cat_taxes: M(raw.cat_taxes),
      rev_other_fees: M(raw.rev_other_fees), rev_amenity: M(raw.rev_amenity),
      rev_rent_yr: M((r.gross_mo || 0) * 12), rev_vac_yr: M(-(r.vacancy_loss_mo || 0) * 12), rev_egi_yr: M((r.egi_mo || 0) * 12),
      egi_wk: M((r.egi_mo || 0) * 12 / 52), amenity_total: M(r.amenity_mo || 0), amenity_wk: M((r.amenity_mo || 0) * 12 / 52),
      funding_status: r.snap_status || r.funding_status || "",
      uses: M(r.snap_uses !== undefined && r.snap_uses !== null ? r.snap_uses : r.uses),
      /* v2.9 (Pass #2): the DISPLAYED Remaining Funding Gap floors at $0
         when over-funded (ReiSearch State 7: $677,340 added vs $376,300
         uses, gap shown $0). The raw r.gap stays signed — the funding
         snapshot's gapalert and the flip cash breakdown use the real gap. */
      gap: M(Math.max(0, r.snap_gap !== undefined && r.snap_gap !== null ? r.snap_gap : r.gap)),
      score_insight: r.insight || "",
      payback_full: r.payback_yrs ? (r.payback_yrs + " yrs") : "—",
      cf_bar_legend: (function () {
        if (!r.egi_mo) return "";
        var ep = Math.round((r.opex_mo + r.debt_service_mo) / r.egi_mo * 100);
        return "Expenses " + ep + "% · Cash Flow " + (100 - ep) + "% of revenue";
      })(),
      rev_other_plain: M(N(s.pet_fees_mo) + N(s.parking_mo) + N(s.misc_mo)),
      expenses_total: M(r.expenses_total || 0), sale_price_out: M(r.sale_price || 0),
      blended_rate: r.egi_mo ? (((r.booking_fee_mo + r.service_fee_mo) / r.egi_mo * 100).toFixed(1) + "%") : "—",
      payback: r.payback_yrs ? ("Payback " + r.payback_yrs + " yrs") : "",
      breakeven_line: "Break-even occupancy: " + PC(r.breakeven_pct) + " — that share of gross rent must be collected to cover expenses + loan payments.",
      holding_desc: (s.holding_period || 0) + " " + (s.holding_unit || "Months") + " × " + M(N(s.holding_mo)) + "/mo ≈ " + M(r.holding_cost) + " total holding costs",
      md_monthly_payment_label: r.md_payment_mo ? (M(r.md_payment_mo) + "/mo") : "",
      profit_full: strat === "Fix & Flip" ? (M(r.profit) + (r.margin_pct != null ? " (" + PC(r.margin_pct) + " of sale price)" : "") + (hMonthsOf() === 0 ? " · no holding period set" : "")) : "",
      rule70_full: r.rule70_mao !== undefined ? (M(r.rule70_mao - N(s.purchase_price)) + " over max offer " + M(r.rule70_mao)) : "",
      rehab_range: (function () {
        var lv = UW.levels[s.rehab_level || "cosmetic"] || UW.levels.cosmetic;
        var sq = N(s.postreno_sqft) || UW.sqft || 0;
        return sq ? (sq.toLocaleString("en-US") + " sqft × $" + lv.low + "–$" + lv.high + "/sqft → " + M(sq * lv.low) + " – " + M(sq * lv.high) + " (midpoint " + M(sq * lv.mid) + " at $" + lv.mid + "/sqft)") : "Enter the property sqft to see the range.";
      })()
    };
    panel.querySelectorAll("[data-out]").forEach(function (elm) {
      var key = elm.dataset.out;
      var txt = (key in special) ? special[key] : fmtFor(key, r, s);
      if (elm.tagName === "INPUT") elm.value = txt; else elm.textContent = txt;
      if (key === "funding_status") {
        var fst = r.snap_status || r.funding_status;
        elm.className = "fbadge " + (fst === "Fully Funded" ? "full" : (fst === "Over Funded" ? "over" : (fst === "Partially Funded" ? "part" : "none")));
      }
    });
    /* Month/Year summary panels (ReiSearch side panels) */
    panel.querySelectorAll("[data-pv]").forEach(function (elm) {
      var key = elm.dataset.pv;
      var sp = elm.closest(".sumpanel");
      var per = "mo";
      if (sp) { var onb = sp.querySelector("[data-ptog].on"); if (onb) per = onb.dataset.ptog; }
      var v = rawFor(key); v = (v === null || v === undefined) ? 0 : v;
      var val = v * (per === "yr" ? 12 : 1);
      if (elm.dataset.neg && val > 0) val = -val;
      elm.textContent = M(val);
      var row = elm.closest(".srow");
      if (row && row.dataset.hidezero !== undefined) row.style.display = N(rawFor(row.dataset.hidezero)) ? "" : "none";
    });
    panel.querySelectorAll("[data-outyr]").forEach(function (elm) {
      var v = rawFor(elm.dataset.outyr); elm.textContent = M((v || 0) * 12);
    });
    /* sidebar scenario chips (Co-Living Overview) */
    var scenKey = panel.dataset.scenKey || "Base Case (0%)";
    var sc = (r.scenarios || {})[scenKey] || (r.scenarios || {})["Base Case (0%)"];
    panel.querySelectorAll("[data-scen]").forEach(function (elm) {
      var k = elm.dataset.scen;
      elm.textContent = k === "rev" ? M(sc ? sc.revenue : r.egi_mo)
        : (k === "exp" ? M(sc ? sc.expenses : r.opex_mo)
        : M(sc && sc.loan !== undefined ? sc.loan : r.debt_service_mo));
    });
    /* per-scenario Key Metrics block (sidebar Scenarios pane) */
    panel.querySelectorAll("[data-scenm]").forEach(function (elm) {
      var k = elm.dataset.scenm;
      if (k === "name") {
        var chip = panel.querySelector('[data-scenchip="' + scenKey + '"]');
        elm.textContent = chip ? chip.textContent.trim() : scenKey.replace(/\s*\(.*\)\s*$/, "");
        return;
      }
      if (!sc) { elm.textContent = "—"; return; }
      if (k === "net") elm.textContent = (sc.revenue !== undefined || sc.expenses !== undefined) ? M(sc.net) + "/mo" : M(sc.net);
      else if (k === "sale") elm.textContent = M(sc.sale);
      else if (k === "allin") elm.textContent = M(sc.all_in);
      else if (k === "coc") elm.textContent = PC(sc.coc_pct);
      else if (k === "dscr") elm.textContent = (sc.dscr === null || sc.dscr === undefined) ? "N/A" : (Math.round(sc.dscr * 100) / 100).toFixed(2);
      else if (k === "cap") elm.textContent = PC(sc.cap_rate_pct);
    });
    var ncfBase = sc ? sc.net : r.cashflow_mo;
    panel.querySelectorAll("[data-ncfdisp]").forEach(function (elm) {
      elm.textContent = M(panel.dataset.ncfMode === "yr" ? (ncfBase || 0) * 12 : ncfBase);
    });
    panel.querySelectorAll("[data-ncfunit]").forEach(function (elm) {
      elm.textContent = panel.dataset.ncfMode === "yr" ? "/yr" : "/mo";
    });
    panel.querySelectorAll("[data-cfseg]").forEach(function (elm) {
      var tot = Math.max(r.egi_mo || 0, 1);
      var v = elm.dataset.cfseg === "exp" ? (sc ? sc.expenses : r.opex_mo)
        : (elm.dataset.cfseg === "debt" ? (sc && sc.loan !== undefined ? sc.loan : r.debt_service_mo) : Math.max(ncfBase || 0, 0));
      elm.style.width = clamp(v / tot * 100, 0, 100) + "%";
    });
    /* v1.6 standard-numbers: write auto-estimates INTO their fields (flagged), and
       withdraw them when a loan payment takes over — never touch user-typed values. */
    function estField(name, flagName, effVal, estOn) {
      var inp = panel.querySelector('[name="' + name + '"]'); if (!inp) return;
      var flag = panel.querySelector('[name="' + flagName + '"]');
      if (estOn) {
        if (document.activeElement !== inp && !N(inp.value) && effVal > 0) {
          inp.value = Math.round(effVal);
          if (flag) flag.value = "1";
        }
        inp.classList.toggle("est", !!(flag && flag.value === "1"));
        inp.title = "Auto-estimate — edit to replace with the real number";
      } else {
        if (flag && flag.value === "1" && document.activeElement !== inp) { inp.value = ""; flag.value = ""; }
        inp.classList.remove("est"); inp.title = "";
      }
    }
    if (strat !== "Fix & Flip") {
      estField("taxes_mo", "taxes_est", r.taxes_mo || 0, !!r.taxes_est);
      estField("insurance_mo", "insurance_est", r.insurance_mo || 0, !!r.insurance_est);
    }
    /* preset buttons show an active/added state while their card is in the stack */
    var inUse = {};
    (s.funding || []).forEach(function (f) { inUse[f.preset || f.type] = 1; });
    panel.querySelectorAll("[data-preset]").forEach(function (b) { b.classList.toggle("on", !!inUse[b.dataset.preset]); });
    panel.querySelectorAll("[data-yr]").forEach(function (elm) {
      var inp = panel.querySelector('[name="' + elm.dataset.yr + '"]');
      elm.textContent = inp ? (M(N(inp.value) * 12) + "/yr") : "";
    });
    panel.querySelectorAll("[data-mirror]").forEach(function (elm) {
      var inp = panel.querySelector('[name="' + elm.dataset.mirror + '"]');
      if (inp) elm.textContent = inp.value || "0";
    });
    /* late fees $/wk <-> $/mo sync (the non-focused field follows) */
    var lfw = panel.querySelector('[name="late_fees_wk"]'), lfm = panel.querySelector('[name="late_fees_mo"]');
    if (lfw && lfm) {
      if (document.activeElement === lfw) lfm.value = Math.round(N(lfw.value) * 52 / 12 * 100) / 100;
      else if (document.activeElement === lfm) lfw.value = Math.round(N(lfm.value) * 12 / 52 * 100) / 100;
      else if (lfw.value === "" && N(lfm.value)) lfw.value = Math.round(N(lfm.value) * 12 / 52 * 100) / 100;
    }
    /* row totals + per-row displays */
    panel.querySelectorAll(".rrow").forEach(function (d) {
      var k = d.dataset.kind;
      if (k === "rehab_room" || k === "rehab_flat") {
        var tot = d.querySelector(".r-tot"); if (tot) tot.textContent = M(N(d.querySelector(".r-mat").value) + N(d.querySelector(".r-lab").value));
      } else if (k === "rooms_rows") {
        var wkEl = d.querySelector(".r-rentwk"), moEl = d.querySelector(".r-rentmo"), vacEl = d.querySelector(".r-vac");
        var wk = N(wkEl.value), mo = N(moEl.value), vac = N(vacEl.value);
        if (document.activeElement === wkEl) { mo = wk * 52 / 12; moEl.value = Math.round(mo * 100) / 100; }
        else if (document.activeElement === moEl) { wk = mo * 12 / 52; wkEl.value = Math.round(wk * 100) / 100; }
        d.querySelector(".r-effwk").value = Math.round(wk * (1 - vac / 100));
        d.querySelector(".r-effmo").textContent = M(mo * (1 - vac / 100));
        var no = d.querySelector(".r-roomno");
        if (no) no.textContent = "Bed " + (Array.prototype.indexOf.call(d.parentNode.querySelectorAll(".rrow"), d) + 1);
      } else if (k !== "funding") {
        var t2 = d.querySelector(".r-tot"); if (t2) t2.textContent = M(N(d.querySelector(".r-qty").value) * N(d.querySelector(".r-unit").value));
      }
    });
    panel.querySelectorAll(".fcard").forEach(function (d, i) {
      var f = (s.funding || [])[i]; if (!f) return;
      var rp = rowPayment(f);
      var pay = d.querySelector(".f-pay");
      if (pay) pay.textContent = (rp.notset || (f.payment_type === "fixed" && (f.fixed_payment === "" || f.fixed_payment === null || f.fixed_payment === undefined)))
        ? "not set" : M(rp.pay + rp.esc);
      var t = d.querySelector(".f-title");
      if (t) t.textContent = f.loan_type === f.preset ? (f.loan_type || "") : ((f.loan_type || "") + " · " + (f.preset || ""));
    });
    /* v2.8 (A10): the override note — "$X of funding requirement is not yet
       covered." — shows only while an override is set AND leaves a gap. */
    var usesNote = panel.querySelector("[data-uses-note]");
    if (usesNote) {
      if (r.uses_override && r.snap_gap > 0) {
        usesNote.hidden = false;
        usesNote.textContent = M(r.snap_gap) + " of funding requirement is not yet covered.";
      } else { usesNote.hidden = true; usesNote.textContent = ""; }
    }
    /* sidebar amortization mini-view: the first real loan card ONLY.
       v1.6: the schedule is simulated by the card's OWN payment type — an
       interest-only loan shows interest payments, $0 principal, balance flat.
       v2.0 (Sherif clarification): the old fallback invented a Conventional
       loan from the property's stored loan fields with a HARDCODED 30-year
       term when no card qualified — removed. No card (or a card with no term)
       -> no schedule, only the empty-state note. */
    var am = panel.querySelector("[data-amort='side']");
    if (am) {
      var loan = null;
      (s.funding || []).forEach(function (f) { if (!loan && f.type !== "Cash" && f.type !== "Equity" && AMOUNT_ONLY_TYPES.indexOf(f.type) < 0 && N(f.amount) > 0) loan = f; });
      var amt = 0, rate = 0;
      if (loan) { amt = N(loan.amount); rate = N(loan.rate); }
      if (amt > 0 && loanSchedule(loan).notset) {
        am.innerHTML = '<p class="small muted">This loan has no amortization term yet — enter the Amortization Term on its Funding card to see its amortization here.</p>';
      } else if (amt > 0) {
        var sch = loanSchedule(loan);
        var maxPI = 1; sch.rows.slice(0, 15).forEach(function (x) { maxPI = Math.max(maxPI, x.interest + x.principal); });
        var bars = sch.rows.slice(0, 15).map(function (x) {
          return '<div class="am-bar" title="Year ' + x.year + '"><div class="am-i" style="height:' + Math.max(2, x.interest / maxPI * 44) + 'px"></div><div class="am-p" style="height:' + Math.max(2, x.principal / maxPI * 44) + 'px"></div></div>';
        }).join("");
        var trows = sch.rows.slice(0, 10).map(function (x) {
          return "<tr><td>" + x.year + "</td><td>" + M(x.interest) + "</td><td>" + M(x.principal) + "</td><td>" + M(x.balance) + "</td></tr>";
        }).join("");
        am.innerHTML = '<p class="small muted" style="margin:0 0 4px">Balance ' + M(amt) + " · " + rate + "% · payment " + M(sch.payment) + '/mo</p>' +
          '<div class="am-chart">' + bars + '</div>' +
          '<details class="am-det"><summary class="small">Show Annual Amortization Table</summary><table class="am-tab"><thead><tr><th>Year</th><th>Interest</th><th>Principal</th><th>Loan Balance</th></tr></thead><tbody>' + trows + "</tbody></table></details>";
      } else {
        am.innerHTML = '<p class="small muted">Add a loan in Funding to see its amortization here.</p>';
      }
    }
    /* sidebar visual / scenarios / details */
    var vis = panel.querySelector("[data-visual='main']");
    if (vis) {
      if (strat === "Fix & Flip") vis.innerHTML = barsHtml([["Sale Price", r.sale_price, "#16a34a"], ["All-in Cost", r.all_in, "#dc2626"], ["Net Profit", r.profit, "#2563eb"]]);
      else vis.innerHTML = barsHtml([["Revenue (EGI)", r.egi_mo, "#16a34a"], ["Operating Expenses", r.opex_mo, "#dc2626"], ["Loan Payments", r.debt_service_mo, "#d97706"], ["Net Cash Flow", r.cashflow_mo, "#2563eb"]]);
    }
    var vex = panel.querySelector("[data-visual='exp']");
    if (vex) {
      if (strat === "Fix & Flip") vex.innerHTML = barsHtml([["Purchase", r.purchase_price, "#64748b"], ["Rehab", r.rehab, "#d97706"], ["Holding", r.holding_cost, "#7c3aed"]]);
      else vex.innerHTML = barsHtml(Object.keys(r.opex_cats || {}).map(function (k) { return [k, r.opex_cats[k], "#64748b"]; }));
    }
    var scn = panel.querySelector("[data-scenarios]");
    if (scn && r.scenarios) {
      var isFlip = strat === "Fix & Flip";
      var h = '<table><thead><tr><th>Scenario</th><th>' + (isFlip ? "Sale Price" : "Revenue") + '</th><th>' + (isFlip ? "All-in" : "Expenses") + '</th>' + (isFlip ? "" : "<th>Loan Payments</th>") + '<th>Net</th></tr></thead><tbody>';
      Object.keys(r.scenarios).forEach(function (k) {
        var v = r.scenarios[k];
        h += "<tr><td>" + k + "</td><td>" + M(isFlip ? v.sale : v.revenue) + "</td><td>" + M(isFlip ? v.all_in : v.expenses) + "</td>"
          + (isFlip ? "" : "<td>" + M(v.loan || 0) + "</td>") + "<td><b>" + M(v.net) + "</b></td></tr>";
      });
      scn.innerHTML = h + "</tbody></table>";
    }
    /* on-screen breakdowns: Monthly Debt Service per line, Cash Needed per component */
    var dbd = panel.querySelector('[data-breakdown="debt"]');
    if (dbd) {
      var dl = (r.debt_lines || []).map(function (l) {
        return '<div class="srow"><span>' + l.label + '</span><b>' + M(l.payment) + '</b></div>';
      }).join("");
      if (N(r.md_payment_mo)) {
        dl += '<div class="srow"><span>Existing mortgage (PITI)</span><b>' + M(r.md_payment_mo) + '</b></div>';
        if (N(r.md_base_mo)) dl += '<div class="srow"><span>· base payment (P&amp;I)</span><b>' + M(r.md_base_mo) + '</b></div>';
        Object.keys(r.md_addons || {}).forEach(function (k) {
          dl += '<div class="srow"><span>· of which ' + k.toLowerCase() + ' (escrow)</span><b>' + M(r.md_addons[k]) + '</b></div>';
        });
      }
      dbd.innerHTML = dl ? '<h4 class="det-h">Monthly Debt Service</h4>' + dl : "";
    }
    /* v1.8a: the PITI breakdown line inside Existing Mortgage Data — every
       component named, the counted total shown (nothing in the block is inert). */
    var mpl = panel.querySelector("[data-md-piti-line]");
    if (mpl) {
      var pp = [];
      if (N(s.md_piti_principal)) pp.push("Principal " + M(N(s.md_piti_principal)));
      if (N(s.md_piti_interest)) pp.push("Interest " + M(N(s.md_piti_interest)));
      if (N(s.md_piti_taxes)) pp.push("Taxes " + M(N(s.md_piti_taxes)));
      if (N(s.md_piti_insurance)) pp.push("Insurance " + M(N(s.md_piti_insurance)));
      if (N(s.md_piti_pmi)) pp.push("PMI " + M(N(s.md_piti_pmi)));
      if (N(s.md_piti_escrow)) pp.push("Escrow " + M(N(s.md_piti_escrow)));
      mpl.textContent = pp.length
        ? "PITI breakdown — " + pp.join(" + ") + " = " + M(r.md_payment_mo || 0) + "/mo counted in debt service. Base = Monthly Payment" +
          (N(s.md_monthly_payment) ? "" : " (here: Principal + Interest)") + "; Taxes / Insurance / PMI / Escrow are escrow add-ons on top of the base."
        : "PITI breakdown — every field counts: Monthly Payment (or Principal + Interest) is the base payment; Taxes, Insurance, PMI and Escrow add on top as escrow.";
    }
    /* v1.7: Cash Needed ALWAYS shows its derivation — every component line,
       $0 lines included (a loan-only state must read "$0" WITH the math visible,
       not as a bare unexplained number). Rendered into the Funding Snapshot
       (data-breakdown="cash") and the sidebar Overview (data-breakdown="cashside").
       v2.9: the RENTAL lines are ReiSearch's measured components only —
       points/fees + entry fee (Pass #2: contributions and closing costs are
       NOT part of Cash Needed; they still show in the snapshot's own rows).
       Fix & Flip keeps its gap-inclusive lines (published fixture). */
    var cashLines = [];
    if (strat === "Fix & Flip") {
      if (r.gap > 0) cashLines.push(["Funding gap (cash at closing)", r.gap]);
      cashLines.push(["Cash contributions", N(r.cash_contrib)]);
      cashLines.push(["Points & fees", N(r.points_fees)]);
      cashLines.push(["Closing costs", N(r.closing_costs)]);
      cashLines.push(["Entry fee", N(r.entry_fee)]);
    } else {
      cashLines.push(["Points & fees", N(r.points_fees)]);
      cashLines.push(["Entry fee", N(r.entry_fee)]);
    }
    var cashRowsHtml = cashLines.map(function (x) {
      return '<div class="srow"><span>' + x[0] + '</span><b>' + M(x[1]) + '</b></div>';
    }).join("") + '<div class="srow tot"><span>Cash Needed</span><b>' + M(r.cash_needed) + '</b></div>';
    var cbd = panel.querySelector('[data-breakdown="cash"]');
    if (cbd) cbd.innerHTML = '<h4 class="det-h">Cash Needed — breakdown</h4>' + cashRowsHtml;
    var csb = panel.querySelector('[data-breakdown="cashside"]');
    if (csb) csb.innerHTML = cashRowsHtml;
    var det = panel.querySelector("[data-details]");
    if (det) {
      var rows = [];
      function H(t) { rows.push(["H", t]); }
      function R(a, b) { rows.push(["R", a, b]); }
      H("Deal Score");
      R("Deal Score", r.score + " (" + (r.label || "") + ")");
      if (r.score_factors && r.score_factors.length) {
        r.score_factors.forEach(function (fc) { R("· " + fc.name + " (" + fc.weight + "%)", factorRow(fc)); });
      } else {
        Object.keys(r.score_parts || {}).forEach(function (k) { R("· " + k, r.score_parts[k]); });
      }
      (r.penalties || []).forEach(function (p) { R("· Penalty −" + p.points, p.reason); });
      if (r.insight) R("Insight", r.insight);
      if (strat === "Fix & Flip") {
        H("Sale & Exit");
        R("Sale Price", M(r.sale_price)); R("Commission", M(r.commission_amt));
        R("Seller Closing Costs", M(r.closing_amt)); R("Net Sale Proceeds", M(r.net_proceeds));
        H("Costs");
        R("Purchase Price", M(r.purchase_price)); R("Rehab", M(r.rehab));
        R("Holding Costs", M(r.holding_cost)); R("Financing (closing, points, interest)", M(r.financing_total || 0)); R("All-in Cost", M(r.all_in));
        H("Cash & Capital");
        R("Total Uses", M(r.uses)); R("Funding Added", M(r.funded)); R("Cash Needed", M(r.cash_needed));
        H("Return Metrics");
        R("Net Profit", M(r.profit)); R("ROI", PC(r.roi)); R("Annualized ROI", PC(r.annualized_roi));
        R("Profit Margin", PC(r.margin_pct)); R("70% Rule Max Offer", M(r.rule70_mao));
      } else {
        H("Revenue");
        R("Scheduled Rent", M(r.gross_mo)); R("Other Income", M(r.other_income_mo));
        R("Vacancy Loss", "−" + M(r.vacancy_loss_mo)); R("Effective Gross Income", M(r.egi_mo));
        H("Operating Expenses");
        Object.keys(r.opex_cats || {}).forEach(function (k) { R("· " + k, M(r.opex_cats[k])); });
        R("Total Operating Expenses", M(r.opex_mo));
        H("Loan Payments");
        R("Loan Payments", M(r.debt_service_mo));
        H("Cash & Capital");
        R("Total Uses", M(r.uses)); R("Funding Added", M(r.funded)); R("Cash Needed", M(r.cash_needed));
        H("Return Metrics");
        R("Net Cash Flow", M(r.cashflow_mo) + "/mo"); R("Cash-on-Cash", PC(r.coc_pct));
        R("DSCR", (r.dscr === null || r.dscr === undefined) ? "N/A" : (Math.round(r.dscr * 100) / 100).toFixed(2));
        R("Cap Rate", PC(r.cap_rate_pct));
        H("Break-Even");
        R("Break-Even Occupancy", PC(r.breakeven_pct));
      }
      det.innerHTML = rows.map(function (x) {
        return x[0] === "H" ? '<h4 class="det-h">' + x[1] + "</h4>"
          : '<div class="srow"><span>' + x[1] + "</span><b>" + x[2] + "</b></div>";
      }).join("") + ((r.flags && r.flags.length) ? '<p class="small muted">Flags: ' + r.flags.join("; ") + "</p>" : "");
    }
    /* v2.0: the Insights rail pane shows the SAME computed insight the Details
       pane carries (strongest/weak spot from the score parts) plus the parts
       and penalties themselves — it was a static "Coming Soon" stub while the
       numbers already existed. */
    var ins = panel.querySelector("[data-insights]");
    if (ins) {
      var ih = '<h4 class="det-h">Deal Score — ' + r.score + " (" + (r.label || "") + ")</h4>";
      if (r.insight) ih += '<p style="margin:4px 0 8px">' + r.insight + "</p>";
      if (r.score_factors && r.score_factors.length) {
        r.score_factors.forEach(function (fc) {
          ih += '<div class="srow"><span>' + fc.name + " (" + fc.weight + "%)</span><b>" + factorRow(fc) + "</b></div>";
        });
      } else {
        Object.keys(r.score_parts || {}).forEach(function (k) {
          ih += '<div class="srow"><span>' + k + "</span><b>" + r.score_parts[k] + "</b></div>";
        });
      }
      (r.penalties || []).forEach(function (p) {
        ih += '<div class="srow"><span>Penalty −' + p.points + "</span><b>" + (p.reason || "") + "</b></div>";
      });
      if (r.flags && r.flags.length) ih += '<p class="small muted">Flags: ' + r.flags.join("; ") + "</p>";
      ins.innerHTML = ih;
    }
    var ga = panel.querySelector("[data-gapalert]");
    if (ga) {
      if (r.gap > 0) {
        ga.style.display = "block";
        ga.textContent = strat === "Fix & Flip"
          ? "⚠ Funding gap: Total Uses exceed funding added by " + M(r.gap) + " — the gap is counted in Cash Needed."
          : "⚠ Funding gap: Total Uses exceed funding added by " + M(r.gap) + " — a loan-only gap is NOT counted in Cash Needed; add cash or another funding line to fund it.";
      }
      else ga.style.display = "none";
    }
  }

  /* ---------------- recalc + autosave ---------------- */
  var suppressSave = false;
  function recalc(panel) {
    var s = readState(panel);
    var r = compute(panel.dataset.strategy, s);
    /* v2.7 (audit lap): ReiSearch's measured funding EMPTY STATE — the
       "No funding sources yet" note shows exactly when the stack is empty.
       recalc is the funnel every funding mutation passes through (preset
       add, per-card remove, Clear all, state load), so toggling here
       keeps the note correct on every path. */
    var fEmpty = panel.querySelector("[data-funding-empty]");
    if (fEmpty) {
      var fBox = panel.querySelector('[data-rows="funding"]');
      fEmpty.style.display = (fBox && fBox.querySelector(".fcard")) ? "none" : "";
    }
    /* v1.8b: writeOutputs withdraws flagged auto-estimates when a loan payment
       takes over (taxes/insurance then live inside PITI). If it cleared a field,
       recompute in the SAME interaction — otherwise totals land one input late:
       fields show empty while totals still include the withdrawn estimates
       (gate_v19 P3 root cause: P2 showed cf -$190, P3 then showed $35). */
    var preTax = panel.querySelector('[name="taxes_mo"]');
    var preIns = panel.querySelector('[name="insurance_mo"]');
    var preT = (preTax && preTax.value) || "", preI = (preIns && preIns.value) || "";
    writeOutputs(panel, s, r);
    var postTax = panel.querySelector('[name="taxes_mo"]');
    var postIns = panel.querySelector('[name="insurance_mo"]');
    var cleared = (preT !== "" && postTax && postTax.value === "") ||
                  (preI !== "" && postIns && postIns.value === "");
    if (cleared) {
      s = readState(panel);
      r = compute(panel.dataset.strategy, s);
      writeOutputs(panel, s, r);
    }
    if (!suppressSave) scheduleAutosave(panel);
    return r;
  }
  function asNum(v) {
    if (v === "" || v === null || v === undefined) return "";
    var t = String(v).trim();
    return /^[-0-9.]+$/.test(t) && isFinite(parseFloat(t)) ? parseFloat(t) : v;
  }
  function buildCanonical(s) {
    /* Panel state -> stored scenario-input shape (what /underwrite saves and uw_state reads). */
    var inp = {};
    var skip = { rehab_rooms: 1, rehab_flats: 1, appliances: 1, conversion: 1, furnishing: 1, funding: 1, rooms_rows: 1, save_name: 1 };
    Object.keys(s).forEach(function (k) { if (!skip[k]) inp[k] = asNum(s[k]); });
    var rehabLines = [];
    (s.rehab_rooms || []).forEach(function (r) { rehabLines.push({ label: r.label, material: r.material, labor: r.labor }); });
    Object.keys(s.rehab_flats || {}).forEach(function (k) { rehabLines.push({ label: (UW.flatLabels || {})[k] || k, flat: k, material: s.rehab_flats[k].material, labor: s.rehab_flats[k].labor }); });
    inp.rehab_lines = rehabLines;
    function unitJson(rows) { return (rows || []).map(function (r) { var o = { label: r.label, qty: r.qty, material: r.unit, labor: 0 }; if (r.group) o.group = r.group; return o; }); }
    inp.appliance_lines = unitJson(s.appliances);
    inp.conversion_lines = unitJson(s.conversion);
    inp.furnishing_lines = unitJson(s.furnishing);
    inp.funding_stack = s.funding || [];
    inp.rooms_rows = s.rooms_rows || [];
    return inp;
  }
  function canonToPanel(inp) {
    /* Stored shape -> panel state (localStorage backup restore path only;
       server drafts arrive pre-converted through uw_state). */
    var st = {};
    Object.keys(inp || {}).forEach(function (k) { var v = inp[k]; if (v === null || typeof v === "object") return; st[k] = v; });
    var rooms = [], flats = {};
    (inp.rehab_lines || []).forEach(function (l) {
      if (l.flat) flats[l.flat] = { material: l.material || 0, labor: l.labor || 0 };
      else rooms.push({ label: l.label, material: l.material, labor: l.labor });
    });
    st.rehab_rooms = rooms; st.rehab_flats = flats;
    function toUnit(lines) { return (lines || []).map(function (l) { var o = { label: l.label, qty: l.qty, unit: (l.material || 0) + (l.labor || 0) }; if (l.group) o.group = l.group; return o; }); }
    st.appliances = toUnit(inp.appliance_lines);
    st.conversion = toUnit(inp.conversion_lines);
    st.furnishing = toUnit(inp.furnishing_lines);
    st.funding = inp.funding_stack || [];
    st.rooms_rows = inp.rooms_rows || [];
    return st;
  }
  function lsKey(strat) { return "pda-uw-" + UW.pid + "-" + strat; }
  function setStatus(panel, txt) {
    var elm = panel.querySelector("[data-autosave-status]");
    if (elm) elm.textContent = txt;
  }
  var saveTimers = {}, lastSaved = {};
  function scheduleAutosave(panel) {
    var strat = panel.dataset.strategy;
    try { localStorage.setItem(lsKey(strat), JSON.stringify({ state: buildCanonical(readState(panel)), ts: Date.now() })); } catch (e) {}
    setStatus(panel, "Saving…");
    clearTimeout(saveTimers[strat]);
    saveTimers[strat] = setTimeout(function () { postDraft(panel); }, 900);
  }
  function postDraft(panel) {
    var strat = panel.dataset.strategy;
    fetch("/property/" + UW.pid + "/uw-autosave", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy: strat, state: buildCanonical(readState(panel)) })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j && j.ok) { lastSaved[strat] = Date.now(); setStatus(panel, "Last saved just now"); }
      else setStatus(panel, "Autosave failed — edits kept in this browser");
    }).catch(function () { setStatus(panel, "Autosave failed — edits kept in this browser"); });
  }
  setInterval(function () {
    document.querySelectorAll(".uw-panel").forEach(function (panel) {
      var t = lastSaved[panel.dataset.strategy]; if (!t) return;
      var sec = Math.round((Date.now() - t) / 1000);
      if (sec > 4) setStatus(panel, "Last saved " + (sec < 60 ? sec + "s ago" : Math.round(sec / 60) + "m ago"));
    });
  }, 5000);

  /* ---------------- state apply / serialize ---------------- */
  function applyState(panel, state) {
    if (!state) return;
    suppressSave = true;
    Object.keys(state).forEach(function (k) {
      var v = state[k];
      if (v === null || typeof v === "object") return;
      var e = panel.querySelector('[name="' + k + '"]');
      if (e && (e.type !== "hidden" || e.hasAttribute("data-estflag") || e.hasAttribute("data-stateval"))) e.value = v;
    });
    setRows(panel, "rehab_rooms", state.rehab_rooms);
    setRows(panel, "rehab_flats", state.rehab_flats);
    setRows(panel, "appliances", state.appliances);
    setRows(panel, "conversion", state.conversion);
    setRows(panel, "furnishing", state.furnishing);
    setRows(panel, "funding", state.funding);
    setRows(panel, "rooms_rows", state.rooms_rows);
    recalc(panel);
    suppressSave = false;
  }
  function serialize(form) {
    var panel = form.closest(".uw-panel");
    var inp = buildCanonical(readState(panel));
    function set(name, obj) { var e = form.querySelector('[name="' + name + '"]'); if (e) e.value = JSON.stringify(obj); }
    set("rehab_lines_json", inp.rehab_lines);
    set("appliance_lines_json", inp.appliance_lines);
    set("conversion_lines_json", inp.conversion_lines);
    set("furnishing_lines_json", inp.furnishing_lines);
    set("funding_stack_json", inp.funding_stack);
    set("rooms_rows_json", inp.rooms_rows);
  }

  /* ---------------- global actions (called from inline onclick) ---------------- */
  window.uwSeg = function (strat) {
    document.querySelectorAll(".uw-panel").forEach(function (p) { p.style.display = p.dataset.strategy === strat ? "" : "none"; });
    document.querySelectorAll("#segbar .seg").forEach(function (b) { b.classList.toggle("on", b.dataset.seg === strat); });
    return false;
  };
  window.uwSideTab = function (btn, name) {
    var panel = btn.closest(".uw-panel");
    panel.querySelectorAll(".sidetabs button").forEach(function (b) { b.classList.toggle("on", b === btn); });
    panel.querySelectorAll(".sidepane").forEach(function (p) { p.classList.toggle("on", p.dataset.spane === name); });
    return false;
  };
  window.uwGoDetails = function (link) {
    var panel = link.closest(".uw-panel");
    var btn = panel.querySelector(".sidetabs button[data-stab='details']");
    if (btn) return window.uwSideTab(btn, "details");
    return false;
  };
  window.uwNcfMode = function (btn, mode) {
    var panel = btn.closest(".uw-panel");
    panel.dataset.ncfMode = mode;
    panel.querySelectorAll(".moyrtoggle button").forEach(function (b) { b.classList.toggle("on", b === btn); });
    recalc(panel);
    return false;
  };
  window.uwPanelPeriod = function (btn, per) {
    var sp = btn.closest(".sumpanel");
    sp.querySelectorAll("[data-ptog]").forEach(function (b) { b.classList.toggle("on", b === btn); });
    var panel = btn.closest(".uw-panel");
    recalc(panel);
    return false;
  };
  window.uwScenChip = function (btn) {
    var panel = btn.closest(".uw-panel");
    panel.dataset.scenKey = btn.dataset.scenchip;
    panel.querySelectorAll("[data-scenchip]").forEach(function (b) { b.classList.toggle("on", b === btn); });
    recalc(panel);
    return false;
  };
  function blankCard(type) {
    return { type: type, preset: type, loan_type: type, position: "1st Position", lender: "", amount: 0, rate: 0,
      pct: "", basis: "price", interest_type: "simple",
      term_value: "", term_unit: "Years", amort_value: "", amort_unit: "Years",
      points: 0, fees: 0, payment_type: "amortized", fixed_payment: "", taxes_mo: 0, insurance_mo: 0 };
  }
  window.uwAddRow = function (btn, kind) {
    var panel = btn.closest(".uw-panel");
    var box = panel.querySelector('[data-rows="' + kind + '"]'); if (!box) return false;
    if (kind === "rehab_rooms") box.appendChild(rehabRoomRow({ label: "", material: 0, labor: 0 }));
    else if (kind === "funding") box.appendChild(fundingCard(blankCard("Conventional")));
    else if (kind === "rooms_rows") {
      /* ReiSearch behavior: a new room inherits the rent/vacancy pattern of the existing rooms */
      var rows = readRows(panel, "rooms_rows");
      var t = rows.length ? rows[rows.length - 1] : { bath: "Shared", rent_wk: 0, rent_mo: 0, vacancy: 15, amenity_mo: 0 };
      var idx = rows.length;
      box.appendChild(roomRow({ bath: t.bath || "Shared",
        bath_group: (Math.floor(idx / 2) % 2 === 0) ? "A" : "B",
        rent_wk: t.rent_wk, rent_mo: t.rent_mo, vacancy: t.vacancy, amenity_mo: t.amenity_mo || 0 }, idx));
    }
    else box.appendChild(unitRow(kind, { label: "", qty: 1, unit: 0, group: kind === "furnishing" ? "General" : undefined }));
    recalc(panel);
    return false;
  };
  window.uwLoadPicked = function (sel) {
    var rec = (UW.saved || {})[sel.value];
    if (!rec) return;
    window.uwSeg(rec.strategy);
    var panel = document.querySelector('.uw-panel[data-strategy="' + rec.strategy + '"]');
    if (panel) { applyState(panel, rec.state); scheduleAutosave(panel); }
  };
  window.uwCreateNew = function () {
    var panel = document.querySelector('.uw-panel:not([style*="none"])') || document.querySelector(".uw-panel");
    if (panel && UW.fresh) { applyState(panel, UW.fresh[panel.dataset.strategy]); scheduleAutosave(panel); }
    var pk = document.getElementById("uw-picker"); if (pk) pk.value = "";
    return false;
  };
  window.uwUseMidpoint = function (btn) {
    var panel = btn.closest(".uw-panel");
    var lvSel = panel.querySelector('[name="rehab_level"]');
    var lv = UW.levels[lvSel.value] || UW.levels.cosmetic;
    var sq = N(panel.querySelector('[name="postreno_sqft"]').value) || UW.sqft || 0;
    panel.querySelector('[name="rehab_est_total"]').value = Math.round(lv.mid * sq);
    panel.querySelector('[name="rehab_mode"]').value = "est";
    recalc(panel);
    return false;
  };
  window.uwUseCompArv = function (btn, avg) {
    var panel = btn.closest(".uw-panel");
    var sp = panel.querySelector('[name="sale_price"]'); if (sp) sp.value = avg;
    var av = panel.querySelector('[name="arv"]'); if (av) av.value = avg;
    recalc(panel);
    return false;
  };
  /* v1.6 funding model (ReiSearch semantics): presets are ADDITIVE stacked cards,
     defaults are % of the purchase price. The in-card loan-type dropdown re-applies
     the same defaults (LOAN_DEFAULTS) — it is no longer a dead control. */
  var LOAN_DEFAULTS = {
    "Conventional": { pct: 0.80, rate: 7, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "DSCR": { pct: 0.75, rate: 7.5, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "Hard Money": { pct: 0.85, rate: 11, tv: 12, unit: "Months", pt: "interest_only", points: 2 },
    "Private Money": { pct: 0.80, rate: 10, tv: 12, unit: "Months", pt: "interest_only", points: 0 },
    "Seller Financing": { pct: 0.90, rate: 5, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "FHA": { pct: 0.80, rate: 6.5, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "VA": { pct: 0.80, rate: 6.5, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "USDA": { pct: 0.80, rate: 6.5, tv: 30, unit: "Years", pt: "amortized", points: 0 },
    "Bridge": { pct: 0.70, rate: 9, tv: 12, unit: "Months", pt: "interest_only", points: 1 },
    "HELOC": { pct: 0.70, rate: 8, tv: 10, unit: "Years", pt: "interest_only", points: 0 }
  };
  function setPtypeUI(cardEl, pt) {
    cardEl.dataset.ptype = pt;
    cardEl.querySelectorAll(".f-pt").forEach(function (b) { b.classList.toggle("on", b.dataset.pt === pt); });
    /* v2.3: the fixed-amount box lives INSIDE the Fixed segment (ReiSearch) —
       always visible, no show/hide. */
  }
  function subjectToCard(panel) {
    var pl = UW.propLoan || {};
    if (panel.dataset.strategy === "Fix & Flip" && N(pl.balance) > 0) {
      /* ReiSearch Flip behavior: the existing loan becomes a funding card (PITI = fixed payment) */
      var c = blankCard("Subject-To");
      c.amount = N(pl.balance); c.rate = N(pl.rate); c.payment_type = "fixed";
      c.fixed_payment = N(pl.piti); c.loan_type = pl.type || "FHA";
      return c;
    }
    /* Rentals (and Flip with no loan on file): ReiSearch's Subject-To card — blank
       amount, 0%, Fixed Payment, 1st Position, editable Taxes/Insurance escrow fields. */
    var c2 = blankCard("Subject-To");
    c2.amount = ""; c2.rate = 0; c2.payment_type = "fixed"; c2.fixed_payment = "";
    c2.loan_type = pl.type || "Conventional"; c2.position = "1st Position";
    return c2;
  }
  function applyLoanDefaults(sel) {
    /* The in-card "Existing Loan Type" dropdown: re-apply that type's defaults to
       this card, exactly as if its preset had been clicked. */
    var cardEl = sel.closest(".fcard"); var panel = sel.closest(".uw-panel");
    var name = sel.value;
    if (name === "Other") { cardEl.dataset.preset = "Other"; return; }
    var s = readState(panel); var r = compute(panel.dataset.strategy, s); var price = N(s.purchase_price);
    function set(cls, v) { var e = cardEl.querySelector(cls); if (e) e.value = v; }
    if (name === "Cash") {
      cardEl.dataset.type = "Cash"; cardEl.dataset.preset = "Cash";
      set(".f-amt", Math.round(r.uses)); set(".f-rate", 0); setPtypeUI(cardEl, "amortized");
      return;
    }
    var d = LOAN_DEFAULTS[name]; if (!d) return;
    cardEl.dataset.type = name; cardEl.dataset.preset = name;
    set(".f-amt", Math.round(d.pct * price)); set(".f-rate", d.rate);
    set(".f-pct", Math.round(d.pct * 100)); set(".f-points", d.points || 0);
    /* v2.3: a preset applies its term to BOTH term controls (ReiSearch presets
       carry Loan Term == Amortization Term: 30/30, 12mo/12mo). */
    set(".f-termval", d.tv); setUnitSeg(cardEl, "f-termunit", d.unit);
    set(".f-amortval", d.tv); setUnitSeg(cardEl, "f-amortunit", d.unit);
    setPtypeUI(cardEl, d.pt);
  }
  window.uwClearFunding = function (btn) {
    /* ReiSearch "Clear all": empties the funding stack (its confirm dialog is
       ReiSearch-internal; the stack is autosaved and presets restore in one click). */
    var panel = btn.closest(".uw-panel");
    var box = panel.querySelector('[data-rows="funding"]'); if (!box) return false;
    while (box.firstChild) box.removeChild(box.firstChild);
    recalc(panel);
    return false;
  };
  /* v2.8 (A8, evening pass 2026-11-03): the Add Funding Source DIALOG.
     ReiSearch's button opens this dialog (it does not add a card directly);
     every type in it adds a BLANK card — no prefilled defaults, unlike the
     preset row above it, which stays exactly as it was. */
  function blankDialogCard(type, loanType) {
    var c = blankCard(type);
    c.loan_type = loanType; c.preset = loanType;
    c.amount = ""; c.rate = ""; c.points = ""; c.pct = "";
    return c;
  }
  window.uwDlgOpen = function (btn) {
    var dlg = btn.closest(".uw-panel").querySelector(".srcdlg");
    if (dlg) dlg.hidden = false;
    return false;
  };
  window.uwDlgClose = function (btn) {
    var dlg = btn.closest(".srcdlg");
    if (dlg) dlg.hidden = true;
    return false;
  };
  window.uwDlgAdd = function (btn, label) {
    var panel = btn.closest(".uw-panel");
    var box = panel.querySelector('[data-rows="funding"]'); if (!box) return false;
    var card = null;
    if (label === "Conventional Loan") card = fundingCard(blankDialogCard("Conventional", "Conventional Loan"));
    else if (label === "DSCR Loan") card = fundingCard(blankDialogCard("DSCR", "DSCR Loan"));
    else if (label === "Hard Money Loan") card = fundingCard(blankDialogCard("Hard Money", "Hard Money Loan"));
    else if (label === "Private Money Loan") card = fundingCard(blankDialogCard("Private Money", "Private Money Loan"));
    else if (label === "Bridge Loan") card = fundingCard(blankDialogCard("Bridge", "Bridge Loan"));
    else if (label === "HELOC") card = fundingCard(blankDialogCard("HELOC", "HELOC"));
    else if (label === "Cash Contribution") card = fundingCard(blankDialogCard("Cash", "Cash"));
    else if (label === "Seller Financing") card = fundingCard(blankDialogCard("Seller Financing", "Seller Financing"));
    else if (label === "Subject-To") { var stc = blankDialogCard("Subject-To", "Subject-To"); stc.payment_type = "fixed"; card = fundingCard(stc); }
    else if (AMOUNT_ONLY_TYPES.indexOf(label) >= 0) {
      var ac = blankCard(label); ac.kind = "amount"; ac.amount = "";
      card = fundingCard(ac);
    } else if (ASSUMP_TYPES.indexOf(label) >= 0) {
      var wc = blankCard(label); wc.kind = "assump";
      wc.amount = ""; wc.rate = 0; wc.points = 0; wc.fees = "";
      wc.position = ""; wc.term_unit = "Years"; wc.payment_type = "amortized";
      card = fundingCard(wc);
    }
    if (card) box.appendChild(card);
    var dlg = panel.querySelector(".srcdlg"); if (dlg) dlg.hidden = true;
    recalc(panel);
    return false;
  };
  /* v2.8 (A10, evening pass 2026-11-03): Override Total Uses — the pencil
     opens an editor prefilled with the snapshot's current Total Uses;
     Done stores the override in panel state (it persists like any other
     UW state); Reset total uses clears it. Only the Funding Snapshot
     reads the override (see compute). */
  window.uwUsesEdit = function (btn) {
    var panel = btn.closest(".uw-panel");
    var ed = panel.querySelector("[data-uses-editor]"); if (!ed) return false;
    var inp = panel.querySelector("[data-uses-input]");
    var disp = panel.querySelector(".snapcard [data-out='uses']");
    if (inp && disp) inp.value = String(parseFloat(String(disp.textContent).replace(/[^0-9.]/g, "")) || "");
    ed.hidden = false;
    if (inp) { inp.focus(); if (inp.select) inp.select(); }
    return false;
  };
  window.uwUsesDone = function (btn) {
    var panel = btn.closest(".uw-panel");
    var inp = panel.querySelector("[data-uses-input]");
    var hid = panel.querySelector("input[name='uses_override']");
    if (hid) {
      hid.value = (inp && inp.value !== "") ? String(Math.max(0, N(inp.value))) : "";
      hid.dispatchEvent(new Event("input", { bubbles: true }));
    }
    var ed = panel.querySelector("[data-uses-editor]"); if (ed) ed.hidden = true;
    recalc(panel);
    return false;
  };
  window.uwUsesReset = function (btn) {
    var panel = btn.closest(".uw-panel");
    var hid = panel.querySelector("input[name='uses_override']");
    if (hid) { hid.value = ""; hid.dispatchEvent(new Event("input", { bubbles: true })); }
    var ed = panel.querySelector("[data-uses-editor]"); if (ed) ed.hidden = true;
    recalc(panel);
    return false;
  };
  window.uwPreset = function (btn, name) {
    var panel = btn.closest(".uw-panel");
    var s = readState(panel);
    var r = compute(panel.dataset.strategy, s);
    var price = N(s.purchase_price), uses = r.uses;
    var box = panel.querySelector('[data-rows="funding"]'); if (!box) return false;
    /* v2.3: presets ADD to the stack (the v1.6 model, restored). ReiSearch's
       funding model is additive — "You can combine multiple funding sources
       to match the deal" — and its preset row is the empty-state starting
       point for exactly that stack. The v2.1 replace-on-click behavior made
       preset clicks destroy existing cards, which ReiSearch never does;
       "Clear all" (uwClearFunding) is the explicit empty-the-stack action. */
    function card(type, amt, rate, tv, unit, ptype, extra) {
      var c = blankCard(type); c.amount = Math.round(amt); c.rate = rate;
      c.term_value = tv; c.term_unit = unit; c.payment_type = ptype;
      /* v2.3: presets set the Amortization Term equal to the Loan Term
         (ReiSearch preset cards carry both, equal). */
      c.amort_value = tv; c.amort_unit = unit;
      var dd = LOAN_DEFAULTS[type]; if (dd) { c.pct = Math.round(dd.pct * 100); c.basis = "price"; }
      if (extra) Object.keys(extra).forEach(function (k) { c[k] = extra[k]; });
      return c;
    }
    if (name === "Cash") box.appendChild(fundingCard(card("Cash", uses, 0, "", "Months", "amortized", { loan_type: "Cash" })));
    else if (name === "Subject-To") box.appendChild(fundingCard(subjectToCard(panel)));
    else if (name === "Custom Stack") {
      var loan = Math.round(0.7 * price);
      box.appendChild(fundingCard(card("Conventional", loan, 7, 30, "Years", "amortized")));
      box.appendChild(fundingCard(card("Cash", Math.max(0, uses - loan), 0, "", "Months", "amortized", { loan_type: "Cash" })));
    } else {
      var d = LOAN_DEFAULTS[name];
      if (d) box.appendChild(fundingCard(card(name, d.pct * price, d.rate, d.tv, d.unit, d.pt, { points: d.points || 0 })));
    }
    recalc(panel);
    return false;
  };

  /* ---------------- init + wiring ---------------- */
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".uw-panel").forEach(function (panel) {
      var strat = panel.dataset.strategy;
      if (UW.current && UW.current[strat]) applyState(panel, UW.current[strat]);
      else recalc(panel);
      /* localStorage backup: only when the server has no draft for this strategy */
      if (!(UW.drafts || {})[strat]) {
        try {
          var saved = JSON.parse(localStorage.getItem(lsKey(strat)) || "null");
          if (saved && saved.state) { applyState(panel, canonToPanel(saved.state)); postDraft(panel); }
        } catch (e) {}
      }
      if ((UW.drafts || {})[strat]) setStatus(panel, "Last saved " + UW.drafts[strat]);
      var timer = null;
      panel.addEventListener("input", function (e) {
        /* v1.6 reactivity fix (D): sync the paired rent/late-fee fields AT INPUT TIME
           so the recalc that follows reads fresh values — totals used to land one
           interaction late because readState ran before writeOutputs synced them. */
        var t = e.target;
        if (t && t.classList) {
          if (t.classList.contains("r-rentwk")) {
            var row = t.closest(".rrow"), mo = row && row.querySelector(".r-rentmo");
            if (mo) mo.value = Math.round(N(t.value) * 52 / 12 * 100) / 100;
          } else if (t.classList.contains("r-rentmo")) {
            var row2 = t.closest(".rrow"), wk = row2 && row2.querySelector(".r-rentwk");
            if (wk) wk.value = Math.round(N(t.value) * 12 / 52 * 100) / 100;
          }
          if (t.name === "late_fees_wk") { var m1 = panel.querySelector('[name="late_fees_mo"]'); if (m1) m1.value = Math.round(N(t.value) * 52 / 12 * 100) / 100; }
          else if (t.name === "late_fees_mo") { var w1 = panel.querySelector('[name="late_fees_wk"]'); if (w1) w1.value = Math.round(N(t.value) * 12 / 52 * 100) / 100; }
          if (t.name === "taxes_mo") { var fl = panel.querySelector('[name="taxes_est"]'); if (fl) fl.value = ""; t.classList.remove("est"); }
          if (t.name === "insurance_mo") { var fl2 = panel.querySelector('[name="insurance_est"]'); if (fl2) fl2.value = ""; t.classList.remove("est"); }
          /* v2.3 "Use % of" (ReiSearch): pct x basis drives the Amount; editing
             the Amount re-derives the pct. Synced at input time, like the rent pair. */
          if (t.classList.contains("f-pct") || t.classList.contains("f-basis") || t.classList.contains("f-amt")) {
            var cel = t.closest(".fcard");
            if (cel) {
              var pEl = cel.querySelector(".f-pct"), bEl = cel.querySelector(".f-basis"), aEl = cel.querySelector(".f-amt");
              /* v2.8: variant cards (Amount-only, Assumption) have no Use-% row —
                 nothing to sync there; the guard keeps their Amount edits live. */
              if (pEl && bEl && aEl) {
                var st2 = readState(panel);
                var base2 = bEl && bEl.value === "of ARV" ? N(st2.arv) : N(st2.purchase_price);
                if (t.classList.contains("f-amt")) {
                  if (base2 > 0 && aEl.value !== "") pEl.value = Math.round(N(aEl.value) / base2 * 10000) / 100;
                } else if (base2 > 0 && pEl.value !== "") {
                  aEl.value = Math.round(base2 * N(pEl.value) / 100);
                }
              }
            }
          }
        }
        clearTimeout(timer); timer = setTimeout(function () { recalc(panel); }, 90);
      });
      panel.addEventListener("change", function (e) {
        /* v1.6: the in-card loan-type dropdown re-applies that type's defaults */
        if (e.target && e.target.classList && e.target.classList.contains("f-loantype")) applyLoanDefaults(e.target);
        recalc(panel);
      });
      panel.addEventListener("click", function (e) {
        var t = e.target;
        if (t.classList && t.classList.contains("r-del")) {
          var row = t.closest(".rrow") || t.closest(".fcard"); if (row) row.remove(); recalc(panel); return;
        }
        var pt = t.closest ? t.closest(".f-pt") : null;
        if (pt) {
          var cardEl = pt.closest(".fcard");
          cardEl.dataset.ptype = pt.dataset.pt;
          cardEl.querySelectorAll(".f-pt").forEach(function (b) { b.classList.toggle("on", b === pt); });
          recalc(panel); return;
        }
        var fu = t.closest ? t.closest(".fu") : null;
        if (fu) {
          /* Months|Years segmented unit (Loan Term + Amortization Term) */
          var segW = fu.closest(".funit");
          var hid = segW ? segW.querySelector("input[type='hidden']") : null;
          if (hid) hid.value = fu.dataset.u;
          segW.querySelectorAll(".fu").forEach(function (b) { b.classList.toggle("on", b === fu); });
          recalc(panel); return;
        }
        var fit = t.closest ? t.closest(".f-it") : null;
        if (fit) {
          var cardEl2 = fit.closest(".fcard");
          cardEl2.dataset.itype = fit.dataset.it;
          cardEl2.querySelectorAll(".f-it").forEach(function (b) { b.classList.toggle("on", b === fit); });
          recalc(panel); return;
        }
        var vs = t.closest ? t.closest(".r-vacstep") : null;
        if (vs) {
          var inp = vs.parentNode.querySelector(".r-vac");
          inp.value = clamp(N(inp.value) + N(vs.dataset.vacstep), 0, 100);
          recalc(panel); return;
        }
        var ab = t.closest ? t.closest(".r-amen") : null;
        if (ab) {
          var ai = ab.parentNode.querySelector(".r-amenmo");
          ai.style.display = ai.style.display === "none" ? "" : "none";
          if (ai.style.display !== "none") ai.focus();
          return;
        }
      });
    });
    document.querySelectorAll(".uw-form").forEach(function (f) {
      f.addEventListener("submit", function () {
        serialize(f);
        var panel = f.closest(".uw-panel");
        try { localStorage.removeItem(lsKey(panel.dataset.strategy)); } catch (e) {}
      });
    });
  });
})();
