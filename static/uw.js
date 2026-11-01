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
  function rowPayment(f) {
    /* ReiSearch funding-card payment model. Returns {pay (P&I or fixed), esc (taxes+insurance)}. */
    if (!f || f.type === "Cash" || f.type === "Equity") return { pay: 0, esc: 0 };
    var amt = N(f.amount); if (amt <= 0) return { pay: 0, esc: 0 };
    var rate = N(f.rate), months = termMonths(f), pt = f.payment_type || "amortized", pay = 0;
    if (pt === "fixed") pay = N(f.fixed_payment);
    else if (pt === "interest_only") pay = amt * rate / 100 / 12;
    else if (pt === "principal_only") pay = months ? amt / months : 0;
    else pay = loanPayment(amt, rate, months / 12);
    return { pay: pay, esc: N(f.taxes_mo) + N(f.insurance_mo) };
  }
  function amortY1Principal(amt, rate, yrs) {
    amt = N(amt); rate = N(rate); yrs = N(yrs);
    if (amt <= 0 || yrs <= 0) return 0;
    var r = rate / 100 / 12, pmt = loanPayment(amt, rate, yrs), bal = amt, prin = 0;
    for (var i = 0; i < 12; i++) { var inter = bal * r, p = Math.min(pmt - inter, bal); prin += p; bal -= p; if (bal <= 0) break; }
    return prin;
  }
  function scoreLabel(t) { return t >= 75 ? "Strong" : (t >= 55 ? "Workable" : (t >= 40 ? "Thin" : "Weak")); }
  function scoreRental(m, penalties) {
    function cs(x, t) { return t ? clamp((x / t) * 100, 0, 100) : 0; }
    var parts = {
      "Cash flow / room (25%)": Math.round(0.25 * cs(m.cf_per_room, 300) * 10) / 10,
      "DSCR (20%)": Math.round(0.20 * cs(m.dscr, 1.5) * 10) / 10,
      "Cash-on-cash (20%)": Math.round(0.20 * cs(m.coc, 0.25) * 10) / 10,
      "Cap rate (15%)": Math.round(0.15 * cs(m.cap_rate, 0.10) * 10) / 10,
      "Break-even occupancy (12%)": Math.round(0.12 * clamp((1.3 - m.breakeven) / 0.6 * 100, 0, 100) * 10) / 10,
      "Downside cash flow (8%)": Math.round(0.08 * (m.downside_cf > 0 ? 100 : (m.downside_cf === 0 ? 50 : 10)) * 10) / 10
    };
    var total = 0; Object.keys(parts).forEach(function (k) { total += parts[k]; });
    penalties.forEach(function (p) { total -= p.points; });
    total = clamp(Math.round(total), 1, 99);
    return { score: total, label: scoreLabel(total), parts: parts, penalties: penalties };
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
    (s.funding || []).forEach(function (f) {
      var amt = N(f.amount); funded += amt;
      pointsFees += amt * N(f.points) / 100 + N(f.fees);
      if (f.type === "Cash" || f.type === "Equity") cashEquity += amt;
      else {
        debtAmt += amt;
        var rp = rowPayment(f); debtRows += rp.pay + rp.esc;
        if (f.payment_type === "principal_only") paydownY1 += Math.min(amt, rp.pay * 12);
        else paydownY1 += amortY1Principal(amt, f.rate, termMonths(f) / 12);
      }
    });
    var entryFee = 0, mdPayment = 0;
    if (strat !== "Fix & Flip") {
      entryFee = N(s.md_entry_fee); mdPayment = N(s.md_monthly_payment);
      paydownY1 += N(s.md_piti_principal) * 12;
    }
    var debtService = debtRows + mdPayment;
    var closing = N(s.closing_costs);
    var financingCosts = closing + pointsFees;
    var uses = totalProject + holdingTotal;
    var gap = uses - funded;
    var cashIn = cashEquity + Math.max(gap, 0) + financingCosts + entryFee;
    var fundingStatus = funded <= 0 ? "Not Funded" : (gap <= 0 ? "Fully Funded" : "Partially Funded");
    var out = { strategy: strat, arv: arv, purchase_price: price, rehab: rehab, rehab_calc: rehabCalc,
      rehab_mode: rehabMode, appliances: appliances, furnishing: furnishing, conversion: conversion,
      total_project: totalProject, holding_cost: holdingTotal, uses: uses, funded: funded, gap: gap,
      cash_in: cashIn, financing_costs: financingCosts, funding_status: fundingStatus,
      debt_service_mo: debtService, entry_fee: entryFee };

    if (strat === "Fix & Flip") {
      var sale = N(s.sale_price) || arv;
      var commPct = N(s.commission_pct) || 0; if (s.commission_pct === "" || s.commission_pct == null) commPct = 6;
      var closePct = N(s.closing_pct) || 0; if (s.closing_pct === "" || s.closing_pct == null) closePct = 2;
      var commAmt = sale * commPct / 100, closeAmt = sale * closePct / 100;
      var net = sale - commAmt - closeAmt;
      var profit = net - totalProject - holdingTotal;
      var allIn = totalProject + holdingTotal + commAmt + closeAmt;
      var roi = cashIn ? profit / cashIn * 100 : null;
      var margin = sale ? profit / sale * 100 : null;
      var ann = (cashIn && hMonths) ? profit / cashIn * 100 * 12 / hMonths : null;
      function flip(sale2, rehab2, hold2) {
        var n2 = sale2 * (1 - commPct / 100 - closePct / 100);
        var proj = price + rehab2 + appliances + furnishing;
        return { sale: sale2, all_in: proj + hold2 + sale2 * (commPct + closePct) / 100, net: n2 - proj - hold2 };
      }
      var scen = { "Downside (-10%)": flip(sale * 0.9, rehab, holdingTotal),
        "Base Case (0%)": flip(sale, rehab, holdingTotal),
        "Upside (+10%)": flip(sale * 1.1, rehab, holdingTotal),
        "Overrun (rehab +20%, +3 mo)": flip(sale, rehab * 1.2, holdingMo * (hMonths + 3)) };
      function cs2(x, t) { return t ? clamp((x / t) * 100, 0, 100) : 0; }
      var parts = { "Profit margin (50%)": Math.round(0.5 * cs2((margin || 0) / 100, 0.30) * 10) / 10,
                    "ROI on cash (50%)": Math.round(0.5 * cs2((roi || 0) / 100, 0.50) * 10) / 10 };
      var fscore = clamp(Math.round(parts["Profit margin (50%)"] + parts["ROI on cash (50%)"]), 0, 99);
      out.sale_price = sale; out.commission_amt = commAmt; out.closing_amt = closeAmt;
      out.net_proceeds = net; out.all_in = allIn; out.profit = profit; out.roi = roi;
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
    if (debtService === 0 && taxesMo === 0) {
      taxesMo = (price || arv) ? Math.round((price || arv) * 0.011 / 12) : 0;
      penalties.push({ points: 6, reason: "Taxes were $0 — sanity default applied (1.1% of price/yr, county-style estimate)" });
      flags.push("Taxes estimated — verify with county");
    }
    if (debtService === 0 && insMo === 0) {
      insMo = 180;
      penalties.push({ points: 6, reason: "Insurance was $0 — sanity default applied ($180/mo estimate)" });
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
    if (util === 0 && maint === 0) penalties.push({ points: 10, reason: "No utilities/maintenance entered — expenses look unrealistically low" });
    var noi = egi - opex, cf = noi - debtService;
    var value = arv || price;
    var metrics = { cf_per_room: roomsN ? cf / roomsN : 0,
      dscr: debtService ? noi / debtService : (noi > 0 ? 1.5 : 0),
      coc: cashIn ? cf * 12 / cashIn : 0,
      cap_rate: value ? noi * 12 / value : 0,
      breakeven: (gross + other) ? (opex + debtService) / (gross + other) : 1.3,
      downside_cf: egi * 0.9 - opex - debtService };
    function scenR(mult, noVac) {
      var e2 = noVac ? gross + other : (gross - vacLoss) * mult + other;
      return { revenue: e2, expenses: opex + debtService, net: e2 - opex - debtService };
    }
    var scen2 = { "Downside (-10%)": scenR(0.9), "Base Case (0%)": scenR(1.0), "Upside (+10%)": scenR(1.1) };
    if (strat === "Co-Living") scen2["Fully Booked (0% vacancy)"] = scenR(1.0, true);
    var sc = scoreRental(metrics, penalties);
    var refiGross = arv ? Math.round(0.75 * arv) : 0;
    var refiNet = Math.max(0, refiGross - debtAmt);
    out.gross_mo = gross; out.vacancy_loss_mo = vacLoss; out.other_income_mo = other;
    out.cleaning_income_mo = cleaningIncome; out.fees_mo = feesCat; out.booking_fee_mo = bookingFee;
    out.service_fee_mo = serviceFee; out.mgmt_fee_mo = mgmtFee; out.platform_fee_mo = platformFee;
    out.egi_mo = egi; out.opex_mo = opex; out.opex_cats = opexCats; out.taxes_mo = taxesMo; out.insurance_mo = insMo;
    out.noi_mo = noi; out.cashflow_mo = cf; out.cf_per_room = metrics.cf_per_room;
    out.cash_per_room = roomsN ? cashIn / roomsN : null;
    out.dscr = Math.round(metrics.dscr * 100) / 100;
    out.coc_pct = metrics.coc * 100; out.cap_rate_pct = metrics.cap_rate * 100;
    out.breakeven_pct = metrics.breakeven * 100;
    out.payback_yrs = metrics.coc > 0 ? Math.round(10 / metrics.coc) / 10 : null;
    out.scenarios = scen2; out.rooms = roomsN;
    out.refi_proceeds = refiGross; out.remaining_in_deal = Math.max(0, cashIn - refiNet);
    out.paydown_y1 = paydownY1; out.cashflow_y1 = cf * 12;
    out.total_return_y1 = cf * 12 + paydownY1;
    out.total_roi_y1 = cashIn ? (cf * 12 + paydownY1) / cashIn * 100 : null;
    out.score = sc.score; out.label = sc.label; out.score_parts = sc.parts; out.penalties = sc.penalties; out.flags = flags;
    out.insight = insightFromParts(sc.parts);
    out.amenity_mo = amenityRows + N(s.amenities_mo);
    out.expenses_total = opex + debtService;
    return out;
  }

  /* ---------------- DOM: rows ---------------- */
  var FUNDING_TYPES = ["Conventional", "DSCR", "Hard Money", "Private Money", "Bridge", "HELOC", "Seller Financing", "Subject-To", "Cash", "Equity"];
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
  function fundingCard(f) {
    var d = el("div", "fcard"); d.dataset.kind = "funding";
    d.dataset.type = f.type || "Conventional"; d.dataset.preset = f.preset || f.type || "Conventional";
    d.dataset.ptype = f.payment_type || "amortized";
    var head = el("div", "fcard-h");
    head.appendChild(el("span", "f-title", ""));
    var posLab = el("label", "f-poslab", "Loan Position ");
    posLab.appendChild(selOf("f-pos", ["1st Position", "2nd Position", "3rd Position"], f.position || "1st Position"));
    head.appendChild(posLab);
    var calc = el("a", "btn sm sec", "Open Calculator"); calc.href = "#amortcard"; head.appendChild(calc);
    head.appendChild(delBtn());
    d.appendChild(head);
    var grid = el("div", "fgrid");
    function field(lab, node) { var w = el("div", "ffield"); w.appendChild(el("label", "", lab)); w.appendChild(node); return w; }
    grid.appendChild(field("Existing Loan Type", selOf("f-loantype", LOAN_TYPES, f.loan_type || f.type || "Conventional")));
    grid.appendChild(field("Loan Balance $", numInput("f-amt", f.amount)));
    grid.appendChild(field("Interest Rate %", numInput("f-rate", f.rate)));
    var termW = el("div", "fterm");
    termW.appendChild(numInput("f-termval", f.term_value));
    termW.appendChild(selOf("f-termunit", ["Months", "Years"], f.term_unit || "Months"));
    grid.appendChild(field("Remaining Term", termW));
    grid.appendChild(field("Points %", numInput("f-points", f.points)));
    grid.appendChild(field("Fees $", numInput("f-fees", f.fees)));
    grid.appendChild(field("Taxes $/mo (escrow)", numInput("f-taxes", f.taxes_mo)));
    grid.appendChild(field("Insurance $/mo (escrow)", numInput("f-ins", f.insurance_mo)));
    d.appendChild(grid);
    var seg = el("div", "fseg");
    seg.appendChild(el("span", "fseg-lab", "Payment Type"));
    PTYPES.forEach(function (pt) {
      var b = el("button", "f-pt" + (d.dataset.ptype === pt[0] ? " on" : ""), pt[1]);
      b.type = "button"; b.dataset.pt = pt[0]; seg.appendChild(b);
    });
    var fx = numInput("f-fixed", f.fixed_payment); fx.placeholder = "Fixed $/mo";
    fx.style.display = d.dataset.ptype === "fixed" ? "" : "none";
    seg.appendChild(fx);
    d.appendChild(seg);
    var pay = el("div", "fpay", "Monthly payment ");
    pay.appendChild(el("b", "f-pay", "$0"));
    d.appendChild(pay);
    return d;
  }
  function readCard(d) {
    var tv = d.querySelector(".f-termval").value;
    return { type: d.dataset.type || "Conventional", preset: d.dataset.preset || "Conventional",
      loan_type: d.querySelector(".f-loantype").value, position: d.querySelector(".f-pos").value,
      amount: N(d.querySelector(".f-amt").value), rate: N(d.querySelector(".f-rate").value),
      term_value: tv === "" ? "" : N(tv), term_unit: d.querySelector(".f-termunit").value,
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
    if (key === "dscr") return (v === null || v === undefined) ? "—" : (Math.round(v * 100) / 100).toFixed(2);
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
      funding_status: r.funding_status || "",
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
      md_monthly_payment_label: N(s.md_monthly_payment) ? (M(N(s.md_monthly_payment)) + "/mo") : "",
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
        elm.className = "fbadge " + (r.funding_status === "Fully Funded" ? "full" : (r.funding_status === "Partially Funded" ? "part" : "none"));
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
      elm.textContent = k === "rev" ? M(sc ? sc.revenue : r.egi_mo) : (k === "exp" ? M(r.opex_mo) : M(r.debt_service_mo));
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
      var v = elm.dataset.cfseg === "exp" ? r.opex_mo : (elm.dataset.cfseg === "debt" ? r.debt_service_mo : Math.max(ncfBase || 0, 0));
      elm.style.width = clamp(v / tot * 100, 0, 100) + "%";
    });
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
      var pay = d.querySelector(".f-pay"); if (pay) pay.textContent = M(rp.pay + rp.esc);
      var t = d.querySelector(".f-title");
      if (t) t.textContent = f.loan_type === f.preset ? (f.loan_type || "") : ((f.loan_type || "") + " · " + (f.preset || ""));
    });
    /* sidebar amortization mini-view: first real loan card, else the property loan */
    var am = panel.querySelector("[data-amort='side']");
    if (am) {
      var loan = null;
      (s.funding || []).forEach(function (f) { if (!loan && f.type !== "Cash" && f.type !== "Equity" && N(f.amount) > 0) loan = f; });
      var amt = 0, rate = 0, yrs = 30;
      if (loan) { amt = N(loan.amount); rate = N(loan.rate); yrs = termMonths(loan) / 12 || 30; }
      else if (UW.propLoan && N(UW.propLoan.balance) > 0) { amt = N(UW.propLoan.balance); rate = N(UW.propLoan.rate); yrs = 30; }
      if (amt > 0) {
        var sch = amortSchedule(amt, rate, yrs);
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
      var h = '<table><thead><tr><th>Scenario</th><th>' + (isFlip ? "Sale Price" : "Revenue") + '</th><th>' + (isFlip ? "All-in" : "Expenses") + '</th><th>Net</th></tr></thead><tbody>';
      Object.keys(r.scenarios).forEach(function (k) {
        var v = r.scenarios[k];
        h += "<tr><td>" + k + "</td><td>" + M(isFlip ? v.sale : v.revenue) + "</td><td>" + M(isFlip ? v.all_in : v.expenses) + "</td><td><b>" + M(v.net) + "</b></td></tr>";
      });
      scn.innerHTML = h + "</tbody></table>";
    }
    var det = panel.querySelector("[data-details]");
    if (det) {
      var rows = [];
      function H(t) { rows.push(["H", t]); }
      function R(a, b) { rows.push(["R", a, b]); }
      H("Deal Score");
      R("Deal Score", r.score + " (" + (r.label || "") + ")");
      Object.keys(r.score_parts || {}).forEach(function (k) { R("· " + k, r.score_parts[k]); });
      (r.penalties || []).forEach(function (p) { R("· Penalty −" + p.points, p.reason); });
      if (r.insight) R("Insight", r.insight);
      if (strat === "Fix & Flip") {
        H("Sale & Exit");
        R("Sale Price", M(r.sale_price)); R("Commission", M(r.commission_amt));
        R("Seller Closing Costs", M(r.closing_amt)); R("Net Sale Proceeds", M(r.net_proceeds));
        H("Costs");
        R("Purchase Price", M(r.purchase_price)); R("Rehab", M(r.rehab));
        R("Holding Costs", M(r.holding_cost)); R("All-in Cost", M(r.all_in));
        H("Cash & Capital");
        R("Total Uses", M(r.uses)); R("Funding Added", M(r.funded)); R("Cash Needed", M(r.cash_in));
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
        R("Total Uses", M(r.uses)); R("Funding Added", M(r.funded)); R("Cash Needed", M(r.cash_in));
        H("Return Metrics");
        R("Net Cash Flow", M(r.cashflow_mo) + "/mo"); R("Cash-on-Cash", PC(r.coc_pct));
        R("DSCR", (r.dscr === null || r.dscr === undefined) ? "—" : (Math.round(r.dscr * 100) / 100).toFixed(2));
        R("Cap Rate", PC(r.cap_rate_pct));
        H("Break-Even");
        R("Break-Even Occupancy", PC(r.breakeven_pct));
      }
      det.innerHTML = rows.map(function (x) {
        return x[0] === "H" ? '<h4 class="det-h">' + x[1] + "</h4>"
          : '<div class="srow"><span>' + x[1] + "</span><b>" + x[2] + "</b></div>";
      }).join("") + ((r.flags && r.flags.length) ? '<p class="small muted">Flags: ' + r.flags.join("; ") + "</p>" : "");
    }
    var ga = panel.querySelector("[data-gapalert]");
    if (ga) {
      if (r.gap > 0) { ga.style.display = "block"; ga.textContent = "⚠ Funding gap: Total Uses exceed funding added by " + M(r.gap) + " — the gap is counted in Cash Needed."; }
      else ga.style.display = "none";
    }
  }

  /* ---------------- recalc + autosave ---------------- */
  var suppressSave = false;
  function recalc(panel) {
    var s = readState(panel);
    var r = compute(panel.dataset.strategy, s);
    writeOutputs(panel, s, r);
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
      if (e && e.type !== "hidden") e.value = v;
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
    return { type: type, preset: type, loan_type: type, position: "1st Position", amount: 0, rate: 0,
      term_value: "", term_unit: "Months", points: 0, fees: 0, payment_type: "amortized", fixed_payment: "", taxes_mo: 0, insurance_mo: 0 };
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
  window.uwPreset = function (btn, name) {
    var panel = btn.closest(".uw-panel");
    var s = readState(panel);
    var r = compute(panel.dataset.strategy, s);
    var price = N(s.purchase_price), uses = r.uses;
    function card(type, amt, rate, tv, unit, ptype, extra) {
      var c = blankCard(type); c.amount = Math.round(amt); c.rate = rate;
      c.term_value = tv; c.term_unit = unit; c.payment_type = ptype;
      if (extra) Object.keys(extra).forEach(function (k) { c[k] = extra[k]; });
      return c;
    }
    var F = null;
    if (name === "Cash") F = [card("Cash", uses, 0, "", "Months", "amortized", { loan_type: "Cash" })];
    else if (name === "Conventional") F = [card("Conventional", 0.8 * price, 7, 30, "Years", "amortized")];
    else if (name === "DSCR") F = [card("DSCR", 0.75 * price, 7.5, 30, "Years", "amortized")];
    else if (name === "Hard Money") F = [card("Hard Money", 0.9 * (price + r.rehab), 11, 12, "Months", "interest_only")];
    else if (name === "Private Money") F = [card("Private Money", price, 10, 12, "Months", "interest_only")];
    else if (name === "Seller Financing") F = [card("Seller Financing", 0.9 * price, 6, 30, "Years", "amortized")];
    else if (name === "Custom Stack") {
      var loan = Math.round(0.7 * price);
      F = [card("Conventional", loan, 7, 30, "Years", "amortized"),
           card("Cash", Math.max(0, Math.round(uses - loan)), 0, "", "Months", "amortized", { loan_type: "Cash" })];
    } else if (name === "Subject-To") {
      if (panel.dataset.strategy === "Fix & Flip") {
        /* ReiSearch Flip behavior: the existing loan becomes a funding card (PITI = fixed payment) */
        var pl = UW.propLoan || {};
        F = [card("Subject-To", N(pl.balance), N(pl.rate), "", "Months", "fixed",
                  { loan_type: pl.type || "FHA", fixed_payment: N(pl.piti) })];
      } else {
        var det = panel.querySelector('details.acc.sub');
        if (det) { det.open = true; det.scrollIntoView({ behavior: "smooth", block: "center" }); }
        return false;
      }
    }
    if (F) { setRows(panel, "funding", F); recalc(panel); }
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
      panel.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { recalc(panel); }, 90); });
      panel.addEventListener("change", function () { recalc(panel); });
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
          cardEl.querySelector(".f-fixed").style.display = pt.dataset.pt === "fixed" ? "" : "none";
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
