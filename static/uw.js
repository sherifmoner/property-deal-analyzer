/* Property Deal Analyzer v1.4 — reactive underwriting engine.
   Mirrors compute_underwriting() in app.py formula-for-formula. No Calculate button:
   every keystroke recomputes section totals, the sidebar outcome, and the Co-Living
   bottom summary in place. Saving a scenario posts the form (rows serialized to the
   hidden *_json inputs the server route already parses). */
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
    var funded = 0, cashEquity = 0, debtAmt = 0, debtRows = 0, paydownY1 = 0;
    (s.funding || []).forEach(function (f) {
      var amt = N(f.amount); funded += amt;
      if (f.type === "Cash" || f.type === "Equity") cashEquity += amt;
      else { debtAmt += amt; debtRows += loanPayment(amt, f.rate, f.term_years); paydownY1 += amortY1Principal(amt, f.rate, f.term_years); }
    });
    var entryFee = 0, mdPayment = 0;
    if (strat !== "Fix & Flip") {
      entryFee = N(s.md_entry_fee); mdPayment = N(s.md_monthly_payment);
      paydownY1 += N(s.md_piti_principal) * 12;
    }
    var debtService = debtRows + mdPayment;
    var closing = N(s.closing_costs);
    var uses = totalProject + holdingTotal;
    var gap = uses - funded;
    var cashIn = cashEquity + Math.max(gap, 0) + closing + entryFee;
    var out = { strategy: strat, arv: arv, purchase_price: price, rehab: rehab, rehab_calc: rehabCalc,
      rehab_mode: rehabMode, appliances: appliances, furnishing: furnishing, conversion: conversion,
      total_project: totalProject, holding_cost: holdingTotal, uses: uses, funded: funded, gap: gap,
      cash_in: cashIn, financing_costs: closing, debt_service_mo: debtService, entry_fee: entryFee };

    if (strat === "Fix & Flip") {
      var sale = N(s.sale_price) || arv;
      var commPct = N(s.commission_pct) || 0; if (s.commission_pct === "" || s.commission_pct == null) commPct = 6;
      var closePct = N(s.closing_pct) || 0; if (s.closing_pct === "" || s.closing_pct == null) closePct = 2;
      var commAmt = sale * commPct / 100, closeAmt = sale * closePct / 100;
      var net = sale - commAmt - closeAmt;
      var profit = net - totalProject - holdingTotal;
      var allIn = totalProject + holdingTotal;
      var roi = cashIn ? profit / cashIn * 100 : null;
      var margin = sale ? profit / sale * 100 : null;
      var ann = (cashIn && hMonths) ? profit / cashIn * 100 * 12 / hMonths : null;
      function flip(sale2, rehab2, hold2) {
        var n2 = sale2 * (1 - commPct / 100 - closePct / 100);
        var proj = price + rehab2 + appliances + furnishing;
        return { sale: sale2, all_in: proj + hold2, net: n2 - proj - hold2 };
      }
      var scen = { "Downside (-10%)": flip(sale * 0.9, rehab, holdingTotal),
        "Base Case (0%)": flip(sale, rehab, holdingTotal),
        "Upside (+10%)": flip(sale * 1.1, rehab, holdingTotal),
        "Overrun (rehab +20%, +3 mo)": flip(sale, rehab * 1.2, holdingMo * (hMonths + 3)) };
      function cs2(x, t) { return t ? clamp((x / t) * 100, 0, 100) : 0; }
      var parts = { "Profit margin (50%)": Math.round(0.5 * cs2((margin || 0) / 100, 0.30) * 10) / 10,
                    "ROI on cash (50%)": Math.round(0.5 * cs2((roi || 0) / 100, 0.50) * 10) / 10 };
      var fscore = clamp(Math.round(parts["Profit margin (50%)"] + parts["ROI on cash (50%)"]), 1, 99);
      out.sale_price = sale; out.commission_amt = commAmt; out.closing_amt = closeAmt;
      out.net_proceeds = net; out.all_in = allIn; out.profit = profit; out.roi = roi;
      out.annualized_roi = ann; out.margin_pct = margin; out.rule70_mao = arv * 0.70 - rehab;
      out.scenarios = scen; out.score = fscore; out.label = scoreLabel(fscore);
      out.score_parts = parts; out.penalties = [];
      return out;
    }

    /* rentals */
    var penalties = [], flags = [];
    var taxesMo = N(s.taxes_mo), insMo = N(s.insurance_mo);
    if (taxesMo === 0) {
      taxesMo = (price || arv) ? Math.round((price || arv) * 0.011 / 12) : 0;
      penalties.push({ points: 6, reason: "Taxes were $0 — sanity default applied (1.1% of price/yr, county-style estimate)" });
      flags.push("Taxes estimated — verify with county");
    }
    if (insMo === 0) {
      insMo = 180;
      penalties.push({ points: 6, reason: "Insurance was $0 — sanity default applied ($180/mo estimate)" });
      flags.push("Insurance estimated");
    }
    var gross = 0, vacLoss = 0, other = 0, roomsN = 1, cleaningIncome = 0;
    if (strat === "Co-Living") {
      var rows = s.rooms_rows || [];
      rows.forEach(function (r) { var mo = N(r.rent_wk) * 52 / 12; gross += mo; vacLoss += mo * (N(r.vacancy) || 0) / 100; });
      roomsN = rows.length || 4;
      var laundryMo = N(s.laundry_amt) * ((s.laundry_period || "wk") === "wk" ? 52 / 12 : 1);
      other = N(s.amenities_mo) + N(s.late_fees_mo) + laundryMo + N(s.misc_income_mo);
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
    var bookingFee = gross ? bookingDays * (gross / 30) / 12 : 0;
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
    out.expenses_total = opex + debtService;
    return out;
  }

  /* ---------------- DOM: rows ---------------- */
  var FUNDING_TYPES = ["Conventional", "DSCR", "Hard Money", "Private Money", "Bridge", "HELOC", "Seller Financing", "Subject-To", "Cash", "Equity"];
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function numInput(cls, val) { var i = el("input", cls); i.type = "number"; i.step = "any"; i.value = (val === undefined || val === null) ? "" : val; return i; }
  function delBtn() { var b = el("button", "btn sm danger r-del", "✕"); b.type = "button"; return b; }

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
  function fundingRow(r) {
    var d = el("div", "rrow f"); d.dataset.kind = "funding";
    var sel = el("select", "r-type");
    var types = FUNDING_TYPES.slice(); if (r.type && types.indexOf(r.type) < 0) types.push(r.type);
    types.forEach(function (t) { var o = el("option", "", t); o.value = t; sel.appendChild(o); });
    sel.value = r.type || "Conventional";
    d.appendChild(sel); d.appendChild(numInput("r-amt", r.amount)); d.appendChild(numInput("r-rate", r.rate));
    d.appendChild(numInput("r-term", r.term_years)); d.appendChild(el("span", "r-pay", "$0")); d.appendChild(delBtn()); return d;
  }
  function roomRow(r, idx) {
    var d = el("div", "rrow r"); d.dataset.kind = "room";
    d.appendChild(el("span", "r-roomno", "Room " + (idx + 1)));
    var sel = el("select", "r-bath");
    ["Private", "Shared"].forEach(function (t) { var o = el("option", "", t); o.value = t; sel.appendChild(o); });
    sel.value = r.bath || "Shared";
    d.appendChild(sel); d.appendChild(numInput("r-wk", r.rent_wk)); d.appendChild(el("span", "r-mo", "$0"));
    d.appendChild(numInput("r-vac", r.vacancy)); d.appendChild(el("span", "r-net", "$0")); d.appendChild(delBtn()); return d;
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
      else if (kind === "funding") box.appendChild(fundingRow(r));
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
    box.querySelectorAll(".rrow").forEach(function (d) {
      var k = d.dataset.kind;
      if (k === "rehab_room") out.push({ label: d.querySelector(".r-label").value, material: N(d.querySelector(".r-mat").value), labor: N(d.querySelector(".r-lab").value) });
      else if (k === "rehab_flat") { /* handled below */ }
      else if (k === "funding") out.push({ type: d.querySelector(".r-type").value, amount: N(d.querySelector(".r-amt").value), rate: N(d.querySelector(".r-rate").value), term_years: d.querySelector(".r-term").value === "" ? "" : N(d.querySelector(".r-term").value) });
      else if (k === "room") out.push({ bath: d.querySelector(".r-bath").value, rent_wk: N(d.querySelector(".r-wk").value), vacancy: N(d.querySelector(".r-vac").value) });
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
  function writeOutputs(panel, s, r) {
    var strat = panel.dataset.strategy;
    var special = {
      rehab_total: M(r.rehab), rehab_mode_label: r.rehab_mode === "est" ? "ESTIMATED TOTAL" : "CALCULATED TOTAL",
      rehab_interior_total: M((s.rehab_rooms || []).reduce(function (a, x) { return a + N(x.material) + N(x.labor); }, 0)),
      appliances_total: M(r.appliances), conversion_total: M(r.conversion), furnishing_total: M(r.furnishing),
      cat_fees: M(r.opex_cats ? r.opex_cats["Booking & Service Fees"] : 0),
      cat_utilities: M(r.opex_cats ? r.opex_cats["Utilities"] : 0),
      cat_cleaning: M(r.opex_cats ? r.opex_cats["Cleaning Service"] : 0),
      cat_maintenance: M(r.opex_cats ? r.opex_cats["Maintenance"] : 0),
      cat_management: M(r.opex_cats ? r.opex_cats["Management"] : 0),
      cat_taxes: M(r.opex_cats ? r.opex_cats["Taxes, Insurance & HOA"] : 0),
      rev_other_fees: M(N(s.late_fees_mo) + N(s.misc_income_mo)),
      rev_amenity: M(N(s.amenities_mo) + (r.laundry_mo || 0)),
      rev_other_plain: M(N(s.pet_fees_mo) + N(s.parking_mo) + N(s.misc_mo)),
      expenses_total: M(r.expenses_total || 0), sale_price_out: M(r.sale_price || 0),
      blended_rate: r.egi_mo ? (((r.booking_fee_mo + r.service_fee_mo) / r.egi_mo * 100).toFixed(1) + "%") : "—",
      payback: r.payback_yrs ? ("Payback " + r.payback_yrs + " yrs") : "",
      breakeven_line: "Break-even occupancy: " + PC(r.breakeven_pct) + " — that share of gross rent must be collected to cover expenses + loan payments.",
      holding_desc: (s.holding_period || 0) + " " + (s.holding_unit || "Months") + " × " + M(N(s.holding_mo)) + "/mo ≈ " + M(r.holding_cost) + " total holding costs",
      md_monthly_payment_label: N(s.md_monthly_payment) ? (M(N(s.md_monthly_payment)) + "/mo") : "",
      profit_full: strat === "Fix & Flip" ? (M(r.profit) + (r.margin_pct != null ? " (" + PC(r.margin_pct) + " of sale price)" : "")) : "",
      rule70_full: r.rule70_mao !== undefined ? (M(r.rule70_mao - N(s.purchase_price)) + " vs max offer " + M(r.rule70_mao)) : "",
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
    });
    panel.querySelectorAll("[data-ncfdisp]").forEach(function (elm) {
      elm.textContent = M(panel.dataset.ncfMode === "yr" ? r.cashflow_y1 : r.cashflow_mo);
    });
    panel.querySelectorAll("[data-ncfunit]").forEach(function (elm) {
      elm.textContent = panel.dataset.ncfMode === "yr" ? "/yr" : "/mo";
    });
    panel.querySelectorAll("[data-yr]").forEach(function (elm) {
      var inp = panel.querySelector('[name="' + elm.dataset.yr + '"]');
      elm.textContent = inp ? (M(N(inp.value) * 12) + "/yr") : "";
    });
    panel.querySelectorAll("[data-mirror]").forEach(function (elm) {
      var inp = panel.querySelector('[name="' + elm.dataset.mirror + '"]');
      if (inp) elm.textContent = inp.value || "0";
    });
    /* row totals */
    panel.querySelectorAll(".rrow").forEach(function (d) {
      var k = d.dataset.kind;
      if (k === "rehab_room" || k === "rehab_flat") {
        var tot = d.querySelector(".r-tot"); if (tot) tot.textContent = M(N(d.querySelector(".r-mat").value) + N(d.querySelector(".r-lab").value));
      } else if (k === "funding") {
        var pay = d.querySelector(".r-pay"); if (pay) pay.textContent = M(loanPayment(d.querySelector(".r-amt").value, d.querySelector(".r-rate").value, d.querySelector(".r-term").value));
      } else if (k === "room") {
        var wk = N(d.querySelector(".r-wk").value), mo = wk * 52 / 12, vac = N(d.querySelector(".r-vac").value);
        d.querySelector(".r-mo").textContent = M(mo);
        d.querySelector(".r-net").textContent = M(mo * (1 - vac / 100));
        var no = d.querySelector(".r-roomno"); if (no) no.textContent = "Room " + (Array.prototype.indexOf.call(d.parentNode.querySelectorAll(".rrow"), d) + 1);
      } else {
        var t2 = d.querySelector(".r-tot"); if (t2) t2.textContent = M(N(d.querySelector(".r-qty").value) * N(d.querySelector(".r-unit").value));
      }
    });
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

  function recalc(panel) {
    var s = readState(panel);
    var r = compute(panel.dataset.strategy, s);
    writeOutputs(panel, s, r);
    return r;
  }

  /* ---------------- state apply / serialize ---------------- */
  function applyState(panel, state) {
    if (!state) return;
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
  }
  function serialize(form) {
    var panel = form.closest(".uw-panel");
    var s = readState(panel);
    function set(name, obj) { var e = form.querySelector('[name="' + name + '"]'); if (e) e.value = JSON.stringify(obj); }
    var rehabLines = [];
    (s.rehab_rooms || []).forEach(function (r) { rehabLines.push({ label: r.label, material: r.material, labor: r.labor }); });
    Object.keys(s.rehab_flats || {}).forEach(function (k) { rehabLines.push({ label: (UW.flatLabels || {})[k] || k, flat: k, material: s.rehab_flats[k].material, labor: s.rehab_flats[k].labor }); });
    set("rehab_lines_json", rehabLines);
    function unitJson(rows) { return (rows || []).map(function (r) { var o = { label: r.label, qty: r.qty, material: r.unit, labor: 0 }; if (r.group) o.group = r.group; return o; }); }
    set("appliance_lines_json", unitJson(s.appliances));
    set("conversion_lines_json", unitJson(s.conversion));
    set("furnishing_lines_json", unitJson(s.furnishing));
    set("funding_stack_json", (s.funding || []).map(function (f) { return { type: f.type, amount: f.amount, rate: f.rate, term_years: f.term_years }; }));
    set("rooms_rows_json", s.rooms_rows || []);
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
  window.uwNcfMode = function (btn, mode) {
    var panel = btn.closest(".uw-panel");
    panel.dataset.ncfMode = mode;
    panel.querySelectorAll(".moyrtoggle button").forEach(function (b) { b.classList.toggle("on", b === btn); });
    recalc(panel);
    return false;
  };
  window.uwAddRow = function (btn, kind) {
    var panel = btn.closest(".uw-panel");
    var box = panel.querySelector('[data-rows="' + kind + '"]'); if (!box) return false;
    if (kind === "rehab_rooms") box.appendChild(rehabRoomRow({ label: "", material: 0, labor: 0 }));
    else if (kind === "funding") box.appendChild(fundingRow({ type: "Conventional", amount: 0, rate: 0, term_years: "" }));
    else if (kind === "rooms_rows") box.appendChild(roomRow({ bath: "Shared", rent_wk: 0, vacancy: 15 }, box.querySelectorAll(".rrow").length));
    else box.appendChild(unitRow(kind, { label: "", qty: 1, unit: 0, group: kind === "furnishing" ? "General" : undefined }));
    recalc(panel);
    return false;
  };
  window.uwLoadPicked = function (sel) {
    var rec = (UW.saved || {})[sel.value];
    if (!rec) return;
    window.uwSeg(rec.strategy);
    var panel = document.querySelector('.uw-panel[data-strategy="' + rec.strategy + '"]');
    if (panel) applyState(panel, rec.state);
  };
  window.uwCreateNew = function () {
    var panel = document.querySelector('.uw-panel:not([style*="none"])') || document.querySelector(".uw-panel");
    if (panel && UW.fresh) applyState(panel, UW.fresh[panel.dataset.strategy]);
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
    var F = null;
    if (name === "Cash") F = [{ type: "Cash", amount: Math.round(uses), rate: 0, term_years: "" }];
    else if (name === "Conventional") F = [{ type: "Conventional", amount: Math.round(0.8 * price), rate: 7, term_years: 30 }];
    else if (name === "DSCR") F = [{ type: "DSCR", amount: Math.round(0.75 * price), rate: 7.5, term_years: 30 }];
    else if (name === "Hard Money") F = [{ type: "Hard Money", amount: Math.round(0.9 * (price + r.rehab)), rate: 11, term_years: "" }];
    else if (name === "Private Money") F = [{ type: "Private Money", amount: Math.round(price), rate: 10, term_years: "" }];
    else if (name === "Seller Financing") F = [{ type: "Seller Financing", amount: Math.round(0.9 * price), rate: 6, term_years: 30 }];
    else if (name === "Custom Stack") {
      var loan = Math.round(0.7 * price);
      F = [{ type: "Conventional", amount: loan, rate: 7, term_years: 30 }, { type: "Cash", amount: Math.max(0, Math.round(uses - loan)), rate: 0, term_years: "" }];
    } else if (name === "Subject-To") {
      var det = panel.querySelector('details.acc.sub');
      if (det) { det.open = true; det.scrollIntoView({ behavior: "smooth", block: "center" }); }
      return false;
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
      var timer = null;
      panel.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(function () { recalc(panel); }, 90); });
      panel.addEventListener("change", function () { recalc(panel); });
      panel.addEventListener("click", function (e) {
        if (e.target.classList && e.target.classList.contains("r-del")) {
          var row = e.target.closest(".rrow"); if (row) row.remove(); recalc(panel);
        }
      });
    });
    document.querySelectorAll(".uw-form").forEach(function (f) {
      f.addEventListener("submit", function () { serialize(f); });
    });
  });
})();
