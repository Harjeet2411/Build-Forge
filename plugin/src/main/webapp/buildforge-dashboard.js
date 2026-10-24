/* Build Forge dashboard (/buildforge/admin): every failed build with filters, sorting, live updates, report downloads
 * (CSV, XLSX, PDF) and the solutions deck. Mounted from data attributes by buildforge.js, no inline script (works with
 * Jenkins' Content Security Policy). The page only talks to Jenkins; the plugin relays to the service with permissions.
 */
(function () {
  "use strict";
  var BF = window.BuildForge;
  var esc = BF.esc, badge = BF.badge, getJson = BF.getJson, ago = BF.ago, dur = BF.dur, md = BF.md;
  var CATEGORY = BF.CATEGORY, categoryBadge = BF.categoryBadge, crumbHeaders = BF.crumbHeaders;
  var reduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  var ICON = {
    search: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'><circle cx='7' cy='7' r='4.5'/><path d='M10.5 10.5L14 14'/></svg>",
    download: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'><path d='M8 2v8m0 0L5 7m3 3l3-3M3 13h10'/></svg>",
    bell: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M8 2.2a3.8 3.8 0 0 0-3.8 3.8c0 3.4-1.5 4.4-1.5 4.4h10.6s-1.5-1-1.5-4.4A3.8 3.8 0 0 0 8 2.2zM6.6 12.6a1.5 1.5 0 0 0 2.8 0'/></svg>",
    mail: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><rect x='2' y='3.5' width='12' height='9' rx='1.8'/><path d='M2.6 4.6L8 8.6l5.4-4'/></svg>",
    check: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'><path d='M3 8.4l3.2 3.1L13 4.6'/></svg>",
    plus: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'><path d='M8 3v10M3 8h10'/></svg>",
    upload: "<svg viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'><path d='M8 11V3m0 0L5 6m3-3l3 3M3 13h10'/></svg>",
    arrow: "<svg class='bf-arr' viewBox='0 0 10 10' width='10' height='10' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'><path d='M5 8V2m0 0L2.5 4.5M5 2l2.5 2.5'/></svg>"
  };
  var CAT_COLOR = {
    application: "#e3a008", infrastructure: "#e5484d", configuration: "#6e56cf", dependency: "#3e63dd", test_failure: "#f76b15",
    transient: "#12a594", unknown: "#8b8d98", excluded: "#b6b9c2", not_analysed: "#5b5f6c", pending: "#c8901e"
  };
  var PERIODS = [["7d", "Last 7 days"], ["30d", "Last 30 days"], ["90d", "Last 90 days"], ["12m", "Last 12 months"],
    ["thismonth", "This month"], ["lastmonth", "Last month"], ["month", "A specific month…"], ["custom", "Custom range…"], ["all", "All time"]];
  var OUTCOMES = [["", "Any outcome"], ["fixed-next", "Fixed on the next build"], ["fixed-later", "Fixed later"], ["open", "Still failing"], ["rebuilt", "Rebuilt via Build Forge"]];
  var ANALYSIS = [["", "Any analysis"], ["done", "Analysed"], ["skipped", "Excluded from analysis"], ["error", "Not analysed (error)"], ["progress", "In progress"]];
  var REBUILD = [["", "Rebuild: any"], ["yes", "Rebuild suggested"], ["no", "No rebuild"]];

  // ---------------------------------------------------------------- small helpers
  function html(s) { var t = document.createElement("template"); t.innerHTML = s.trim(); return t.content.firstChild; }
  function debounce(fn, ms) { var t; return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); }; }
  function fmtNum(n, dec) { return Number(n).toLocaleString(undefined, {minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0}); }
  function pct(a, b) { return b ? Math.round(100 * a / b) + "%" : "–"; }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function ymd(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function ym(d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1); }
  function whenText(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }
  function selectHtml(options, value, cls) {
    return "<select class='bf-select " + (cls || "") + "'>" + options.map(function (o) {
      return "<option value='" + esc(o[0]) + "'" + (o[0] === value ? " selected" : "") + ">" + esc(o[1]) + "</option>";
    }).join("") + "</select>";
  }
  function tween(node, to, dec, suffix) {
    var from = parseFloat(node.dataset.v);
    if (isNaN(from)) from = 0;
    node.dataset.v = to;
    var fmt = function (x) { return fmtNum(x, dec) + (suffix || ""); };
    cancelAnimationFrame(node._raf);
    if (reduced || from === to) { node.textContent = fmt(to); return; }
    var t0 = performance.now(), D = 650;
    (function step(t) {
      var p = Math.min(1, (t - t0) / D), e = 1 - Math.pow(1 - p, 3);
      node.textContent = fmt(from + (to - from) * e);
      if (p < 1) node._raf = requestAnimationFrame(step);
    })(t0);
  }

  // ---------------------------------------------------------------- periods
  function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()); }
  function periodRange(p) {
    var now = new Date(), y = now.getFullYear(), m = now.getMonth(), s = function (d) { return Math.floor(d.getTime() / 1000); };
    switch (p.kind) {
      case "7d": return {from: Math.floor(Date.now() / 1000) - 7 * 86400};
      case "30d": return {from: Math.floor(Date.now() / 1000) - 30 * 86400};
      case "90d": return {from: Math.floor(Date.now() / 1000) - 90 * 86400};
      case "12m": return {from: s(new Date(y, m - 12, now.getDate()))};
      case "thismonth": return {from: s(new Date(y, m, 1)), to: s(new Date(y, m + 1, 1))};
      case "lastmonth": return {from: s(new Date(y, m - 1, 1)), to: s(new Date(y, m, 1))};
      case "month":
        var mm = /^(\d{4})-(\d{2})$/.exec(p.month || ym(now)) || [0, y, pad(m + 1)];
        return {from: s(new Date(+mm[1], +mm[2] - 1, 1)), to: s(new Date(+mm[1], +mm[2], 1))};
      case "custom":
        var out = {}, a = /^(\d{4})-(\d{2})-(\d{2})$/.exec(p.from || ""), b = /^(\d{4})-(\d{2})-(\d{2})$/.exec(p.to || "");
        if (a) out.from = s(new Date(+a[1], +a[2] - 1, +a[3]));
        if (b) out.to = s(new Date(+b[1], +b[2] - 1, +b[3] + 1));
        return out;
      default: return {};
    }
  }
  function periodLabel(p) {
    var known = PERIODS.filter(function (x) { return x[0] === p.kind; })[0];
    if (p.kind === "month") return monthName(p.month);
    if (p.kind === "custom") return (p.from || "…") + " to " + (p.to || "today");
    return known ? known[1] : "";
  }
  function monthName(v) {
    var m = /^(\d{4})-(\d{2})$/.exec(v || "");
    return m ? new Date(+m[1], +m[2] - 1, 1).toLocaleString(undefined, {month: "long", year: "numeric"}) : "";
  }
  function shiftMonth(v, by) {
    var m = /^(\d{4})-(\d{2})$/.exec(v || ym(new Date())), d = new Date(+m[1], +m[2] - 1 + by, 1);
    return ym(d);
  }

  /** The period picker: a select that reveals a month stepper or two date fields. `p` is mutated in place. */
  function periodControl(p, onChange) {
    var wrap = html("<span class='bf-period'>" + selectHtml(PERIODS, p.kind, "bf-p-kind") + "<span class='bf-p-extra'></span></span>");
    var sel = wrap.querySelector("select"), extra = wrap.querySelector(".bf-p-extra");
    function draw() {
      sel.value = p.kind;
      if (p.kind === "month") {
        p.month = p.month || ym(new Date());
        extra.innerHTML = "<span class='bf-month-step'><button type='button' class='bf-btn bf-btn-sm' data-by='-1' aria-label='Previous month'>‹</button>" +
          "<input type='month' class='bf-input' value='" + esc(p.month) + "'>" +
          "<button type='button' class='bf-btn bf-btn-sm' data-by='1' aria-label='Next month'>›</button></span>";
        extra.querySelector("input").onchange = function () { if (this.value) { p.month = this.value; onChange(); } };
        Array.prototype.forEach.call(extra.querySelectorAll("button"), function (b) {
          b.onclick = function () { p.month = shiftMonth(p.month, +b.dataset.by); draw(); onChange(); };
        });
      } else if (p.kind === "custom") {
        p.from = p.from || ymd(new Date(Date.now() - 7 * 86400000));
        p.to = p.to || ymd(new Date());
        extra.innerHTML = "<input type='date' class='bf-input bf-p-from' value='" + esc(p.from) + "' aria-label='From'> <span class='bf-muted'>to</span> " +
          "<input type='date' class='bf-input bf-p-to' value='" + esc(p.to) + "' aria-label='To'>";
        extra.querySelector(".bf-p-from").onchange = function () { p.from = this.value; onChange(); };
        extra.querySelector(".bf-p-to").onchange = function () { p.to = this.value; onChange(); };
      } else {
        extra.innerHTML = "";
      }
    }
    sel.onchange = function () { p.kind = this.value; draw(); onChange(); };
    draw();
    return {el: wrap, redraw: draw};
  }

  // ---------------------------------------------------------------- multi-select popover
  function multiSelect(o) {
    var selected = (o.values || []).slice(), items = [], open = false, q = "";
    var wrap = html("<span class='bf-ms'><button type='button' class='bf-btn bf-ms-btn' aria-haspopup='listbox'><span></span></button></span>");
    var btn = wrap.querySelector("button"), label = btn.querySelector("span"), pop = null;

    function updateButton() {
      var n = selected.length;
      label.textContent = n === 0 ? o.all : n === 1 ? selected[0] : n + " " + o.plural;
      btn.dataset.on = n ? "1" : "";
    }
    function change() { updateButton(); o.onChange(selected.slice()); }
    function renderList() {
      if (!pop) return;
      var list = pop.querySelector(".bf-pop-list"), needle = q.toLowerCase();
      var known = {};
      items.forEach(function (i) { known[i.value] = 1; });
      var rows = selected.filter(function (v) { return !known[v]; }).map(function (v) { return {value: v, label: v, count: null}; }).concat(items);
      rows = rows.filter(function (r) { return !needle || r.label.toLowerCase().indexOf(needle) >= 0; });
      var out = rows.slice(0, 300).map(function (r) {
        return "<label class='bf-opt'><input type='checkbox' value='" + esc(r.value) + "'" + (selected.indexOf(r.value) >= 0 ? " checked" : "") + ">" +
          "<span title='" + esc(r.label) + "'>" + esc(r.label) + "</span>" + (r.count != null ? "<em>" + r.count + "</em>" : "") + "</label>";
      }).join("");
      if (o.allowPattern && q && (q.indexOf("*") >= 0 || !known[q]) && selected.indexOf(q) < 0) {
        out = "<label class='bf-opt' data-add='1'><input type='checkbox'><span>" + (q.indexOf("*") >= 0 ? "Add pattern " : "Add ") + "<code>" + esc(q) + "</code></span></label>" + out;
      }
      list.innerHTML = out || "<div class='bf-empty'>Nothing matches.</div>";
      Array.prototype.forEach.call(list.querySelectorAll("label"), function (lab) {
        var cb = lab.querySelector("input");
        cb.onchange = function () {
          var v = lab.dataset.add ? q : cb.value, i = selected.indexOf(v);
          if (cb.checked && i < 0) selected.push(v);
          if (!cb.checked && i >= 0) selected.splice(i, 1);
          if (lab.dataset.add) { q = ""; pop.querySelector("input[type=search]").value = ""; }
          change();
          renderList();
        };
      });
      pop.querySelector(".bf-pop-count").textContent = selected.length ? selected.length + " selected" : "None selected = all";
    }
    function show() {
      if (open) return;
      open = true;
      pop = html("<div class='bf-pop'>" + (o.searchable ? "<input type='search' class='bf-input' placeholder='" + esc(o.placeholder || "Search…") + "'>" : "") +
        "<div class='bf-pop-list'></div><div class='bf-pop-foot'><span class='bf-pop-count'></span><span>" +
        "<button type='button' class='bf-btn bf-btn-ghost bf-btn-sm bf-clear'>Clear</button></span></div></div>");
      wrap.appendChild(pop);
      var input = pop.querySelector("input[type=search]");
      if (input) {
        input.oninput = function () { q = this.value.trim(); renderList(); };
        input.onkeydown = function (ev) {
          if (ev.key === "Enter" && q && o.allowPattern) { ev.preventDefault(); if (selected.indexOf(q) < 0) selected.push(q); q = ""; this.value = ""; change(); renderList(); }
        };
        setTimeout(function () { input.focus(); }, 0);
      }
      pop.querySelector(".bf-clear").onclick = function () { selected = []; change(); renderList(); };
      if (o.loadItems) o.loadItems(function (list) { items = list; renderList(); });
      renderList();
    }
    function hide() { if (!open) return; open = false; if (pop) pop.remove(); pop = null; q = ""; }
    btn.onclick = function () { open ? hide() : show(); };
    document.addEventListener("mousedown", function (ev) { if (open && !wrap.contains(ev.target)) hide(); });
    document.addEventListener("keydown", function (ev) { if (open && ev.key === "Escape") { hide(); btn.focus(); } });
    updateButton();
    return {
      el: wrap, get: function () { return selected.slice(); },
      set: function (v) { selected = (v || []).slice(); updateButton(); renderList(); },
      setItems: function (list) { items = list; renderList(); }
    };
  }

  // ---------------------------------------------------------------- mount
  function mountAdmin(root) {
    var d = root.dataset, base = d.bfBase;
    var PERSIST = "buildforge.dash2";
    var state = {
      view: "failures", period: {kind: "30d"}, q: "", jobs: [], categories: [], outcome: "", analysis: "", rebuild: "",
      sort: "when", dir: "desc", limit: 25, offset: 0, live: true
    };
    var deck = {q: "", status: "", category: "", sort: "seen", dir: "desc", limit: 25, offset: 0, counts: null, mode: "fallback", total: 0};
    try {
      var saved = JSON.parse(localStorage.getItem(PERSIST) || "{}");
      if (saved.period && saved.period.kind) state.period = saved.period;
      ["limit", "sort", "dir", "live"].forEach(function (k) { if (saved[k] != null) state[k] = saved[k]; });
    } catch (e) { /* private window */ }
    function persist() {
      try { localStorage.setItem(PERSIST, JSON.stringify({period: state.period, limit: state.limit, sort: state.sort, dir: state.dir, live: state.live})); } catch (e) { /* ignore */ }
    }

    root.classList.add("bf-root", "bf-admin-root");
    document.body.classList.add("bf-wide");  // use the whole window, not Jenkins' centred column
    root.innerHTML =
      "<div class='bf-top'><img class='bf-logo' alt='' src='" + esc(d.bfLogo) + "'>" +
      "<div><div class='bf-title'>Build Forge</div><div class='bf-tag'>Every failed build, its fix and what happened next</div><div class='bf-credit'>Plugin developed by Harjeet L</div></div>" +
      "<div class='bf-top-right'><div class='bf-tabs' role='tablist'>" +
      "<button type='button' class='bf-tab' role='tab' data-view='failures' aria-selected='true'>Failures<span class='bf-count' data-c='failures'></span></button>" +
      "<button type='button' class='bf-tab' role='tab' data-view='deck' aria-selected='false'>Solutions deck<span class='bf-count' data-c='deck'></span></button></div>" +
      "<button type='button' class='bf-live' data-state='on' title='Click to pause or resume live updates'><span class='bf-dot'></span><span class='bf-live-t'>Live</span></button>" +
      "<button type='button' class='bf-bell' aria-label='Alerts' title='Alerts: pipelines that keep failing'>" + ICON.bell + "<span class='bf-bell-n' hidden>0</span></button>" +
      "<button type='button' class='bf-btn bf-btn-primary bf-export'>" + ICON.download + "Export</button></div></div>" +
      (d.bfActive === "true" ? "" : "<p class='bf-err'>Build Forge is turned off or has no service URL (Manage Jenkins › System › Build Forge).</p>") +
      "<div class='bf-banner'></div>" +
      "<section data-pane='failures'>" +
      "<div class='bf-toolbar bf-bar-main'><div class='bf-search'>" + ICON.search + "<input type='search' class='bf-input bf-q' placeholder='Search pipeline, stage, entry id, error text…' aria-label='Search'><kbd>/</kbd></div></div>" +
      "<div class='bf-chips'></div>" +
      "<div class='bf-kpis'></div>" +
      "<div class='bf-grid bf-grid-2'><div class='bf-box'><div class='bf-box-head'><h3 class='bf-chart-title'>Failures over time</h3><span class='bf-spacer bf-legend'><span><i class='bf-leg-f'></i>failed</span><span><i class='bf-leg-g'></i>fixed on the next build</span></span></div><div class='bf-chart'></div></div>" +
      "<div class='bf-box'><div class='bf-box-head'><h3>Where failures come from</h3></div><div class='bf-cats'></div></div></div>" +
      "<div class='bf-grid'><div class='bf-box'><div class='bf-box-head'><h3>Most frequent errors</h3></div><div class='bf-sigs'></div></div>" +
      "<div class='bf-box'><div class='bf-box-head'><h3>Pipelines that fail most</h3></div><div class='bf-jobs'></div></div></div>" +
      "<div class='bf-box'><div class='bf-box-head'><h3>Failed builds</h3><span class='bf-muted bf-total'></span></div><div class='bf-table-wrap'></div><div class='bf-pager'></div></div>" +
      "</section>" +
      "<section data-pane='deck' hidden>" +
      "<div class='bf-deck-stats'></div><div class='bf-deck-note'></div>" +
      "<div class='bf-toolbar bf-deck-bar'><div class='bf-search'>" + ICON.search + "<input type='search' class='bf-input bf-dq' placeholder='Search cards: error, solution, signature…' aria-label='Search the deck'></div></div>" +
      "<div class='bf-box'><div class='bf-table-wrap bf-deck-table'></div><div class='bf-pager bf-deck-pager'></div></div>" +
      "<div class='bf-box'><div class='bf-box-head'><h3>Try an error</h3><span class='bf-muted'>Paste an error to see which card would answer it</span></div>" +
      "<div class='bf-try'><textarea class='bf-input bf-try-text' rows='4' placeholder='fatal: unable to access https://git.example.com/…: Could not resolve host'></textarea>" +
      "<div><button type='button' class='bf-btn bf-try-go'>Find the best card</button></div><div class='bf-try-out' hidden></div></div></div>" +
      "</section>" +
      "<div class='bf-overlay bf-alerts-wrap' data-alerts hidden></div><div class='bf-overlay' data-drawer hidden></div><div class='bf-overlay bf-modal-wrap' data-modal hidden></div>" +
      "<div class='bf-toasts'></div><div class='bf-tip'></div>";
    var $ = function (s) { return root.querySelector(s); };
    var $$ = function (s) { return Array.prototype.slice.call(root.querySelectorAll(s)); };

    // ---------------------------------------------------------- toolbar controls
    var pControl = periodControl(state.period, function () { state.offset = 0; persist(); loadPipelines(true); refresh(); });
    var jobsMs = multiSelect({
      all: "All pipelines", plural: "pipelines", values: state.jobs, searchable: true, allowPattern: true,
      placeholder: "Search pipelines, or type a pattern like payments/*", loadItems: function (done) { loadPipelines(false, done); },
      onChange: function (v) { state.jobs = v; state.offset = 0; refresh(); }
    });
    var catItems = Object.keys(CATEGORY).map(function (k) { return {value: k, label: CATEGORY[k][0], count: null}; });
    var catMs = multiSelect({
      all: "All categories", plural: "categories", values: state.categories, searchable: false,
      loadItems: function (done) { done(catItems); }, onChange: function (v) { state.categories = v; state.offset = 0; refresh(); }
    });
    var tb = $(".bf-bar-main");
    tb.appendChild(pControl.el);
    tb.appendChild(jobsMs.el);
    tb.appendChild(catMs.el);
    [["outcome", OUTCOMES], ["analysis", ANALYSIS], ["rebuild", REBUILD]].forEach(function (x) {
      var s = html(selectHtml(x[1], state[x[0]], "bf-f-" + x[0]));
      s.onchange = function () { state[x[0]] = this.value; state.offset = 0; refresh(); };
      tb.appendChild(s);
    });
    var qIn = $(".bf-q");
    qIn.oninput = debounce(function () { state.q = qIn.value.trim(); state.offset = 0; refresh(); }, 280);

    function clearFilters() {
      state.q = ""; state.jobs = []; state.categories = []; state.outcome = ""; state.analysis = ""; state.rebuild = ""; state.offset = 0;
      qIn.value = "";
      jobsMs.set([]); catMs.set([]);
      $$(".bf-f-outcome,.bf-f-analysis,.bf-f-rebuild").forEach(function (s) { s.value = ""; });
      refresh();
    }
    function drawChips() {
      var chips = [];
      function add(k, v, drop) { chips.push([k, v, drop]); }
      if (state.q) add("Search", state.q, function () { state.q = ""; qIn.value = ""; });
      if (state.jobs.length) add("Pipelines", state.jobs.length > 2 ? state.jobs.length + " selected" : state.jobs.join(", "), function () { state.jobs = []; jobsMs.set([]); });
      if (state.categories.length) add("Category", state.categories.map(function (c) { return (CATEGORY[c] || [c])[0]; }).join(", "), function () { state.categories = []; catMs.set([]); });
      [["outcome", OUTCOMES, "Outcome"], ["analysis", ANALYSIS, "Analysis"], ["rebuild", REBUILD, "Rebuild"]].forEach(function (x) {
        if (state[x[0]]) add(x[2], x[1].filter(function (o) { return o[0] === state[x[0]]; })[0][1], function () { state[x[0]] = ""; $(".bf-f-" + x[0]).value = ""; });
      });
      var box = $(".bf-chips");
      box.innerHTML = chips.map(function (c, i) {
        return "<span class='bf-chip'><b>" + esc(c[0]) + "</b><span title='" + esc(c[1]) + "'>" + esc(c[1]) + "</span><button type='button' data-i='" + i + "' aria-label='Remove filter'>×</button></span>";
      }).join("") + (chips.length > 1 ? "<button type='button' class='bf-btn bf-btn-ghost bf-btn-sm bf-clear-all'>Clear all</button>" : "");
      $$(".bf-chip button").forEach(function (b) { b.onclick = function () { chips[+b.dataset.i][2](); state.offset = 0; refresh(); }; });
      var ca = $(".bf-clear-all");
      if (ca) ca.onclick = clearFilters;
      return chips.length;
    }

    // ---------------------------------------------------------- requests
    function params(extra, o) {
      o = o || {};
      var r = periodRange(o.period || state.period), p = [];
      function add(k, v) { if (v !== undefined && v !== null && v !== "") p.push(k + "=" + encodeURIComponent(v)); }
      add("from", r.from); add("to", r.to);
      add("tz", -new Date().getTimezoneOffset());
      if (!o.noFilters) {
        add("q", state.q); add("category", state.categories.join(","));
        add("outcome", state.outcome); add("analysis", state.analysis); add("rebuild", state.rebuild);
      }
      if (!o.noJobs) add("jobs", (o.jobs || state.jobs).join("|"));
      Object.keys(extra || {}).forEach(function (k) { add(k, extra[k]); });
      return p.join("&");
    }
    var pipelinesCache = null;
    function loadPipelines(force, done) {
      if (pipelinesCache && !force) { if (done) done(pipelinesCache); return; }
      getJson(base + "pipelines?" + params({}, {noJobs: true})).then(function (res) {
        pipelinesCache = (res.j.items || []).map(function (i) { return {value: i.job, label: i.job, count: i.failed}; });
        jobsMs.setItems(pipelinesCache);
        if (done) done(pipelinesCache);
      });
    }

    // ---------------------------------------------------------- KPIs
    var KPIS = [
      {k: "failed", l: "Failed builds", v: function (s) { return s.failed_builds; }, sub: function (s) { return fmtNum(s.failed_pipelines) + " pipelines"; }, accent: "var(--bf-bad)"},
      {k: "rate", l: "Failure rate", v: function (s) { return s.builds_total ? 100 * s.failed_builds / s.builds_total : null; }, dec: 1, suffix: "%",
        sub: function (s) { return fmtNum(s.failed_builds) + " of " + fmtNum(s.builds_total) + " builds"; }},
      {k: "fixed", l: "Fixed on the next build", v: function (s) { return s.fixed_next_build; },
        sub: function (s) { return pct(s.fixed_next_build, s.failed_builds) + " of failures · " + fmtNum(s.pipelines_fixed_next_build) + " pipelines"; }, accent: "var(--bf-good)"},
      {k: "open", l: "Still failing", v: function (s) { return s.still_failing; }, sub: function () { return "no green build since"; },
        accentIf: function (s) { return s.still_failing ? "var(--bf-bad)" : ""; }},
      {k: "analysed", l: "Analysed", v: function (s) { return s.analysed; }, accent: "var(--bf-gold)",
        sub: function (s) { return fmtNum(s.excluded) + " excluded · " + fmtNum(s.analysis_errors) + " not analysed" + (s.from_deck ? " · " + fmtNum(s.from_deck) + " from the deck" : ""); }},
      {k: "rebuilds", l: "Rebuilds via Build Forge", v: function (s) { return s.rebuilds_requested; },
        sub: function (s) { return fmtNum(s.rebuilds_succeeded) + " went green · " + fmtNum(s.rebuilds_failed) + " failed again"; }},
      {k: "recurring", l: "Recurring errors", v: function (s) { return s.recurring; }, sub: function () { return "same error seen more than once"; }},
      {k: "ttf", l: "Median time to fix", text: function (s) { return s.median_time_to_fix_seconds == null ? "–" : dur(s.median_time_to_fix_seconds); }, sub: function (s) { return "failure to green · avg analysis " + dur(s.avg_analysis_seconds); }}
    ];
    function drawKpis(s) {
      var box = $(".bf-kpis");
      if (!box.children.length) {
        box.innerHTML = KPIS.map(function (k) {
          return "<div class='bf-kpi' data-k='" + k.k + "'><div class='bf-kpi-l'>" + esc(k.l) + "</div><div class='bf-kpi-v'>–</div><div class='bf-kpi-s'></div></div>";
        }).join("");
      }
      KPIS.forEach(function (k) {
        var tile = box.querySelector("[data-k='" + k.k + "']"), v = tile.querySelector(".bf-kpi-v");
        var before = tile.dataset.raw;
        if (k.text) { v.textContent = k.text(s); tile.dataset.raw = v.textContent; }
        else {
          var val = k.v(s);
          if (val == null) { v.textContent = "–"; v.dataset.v = ""; } else tween(v, val, k.dec, k.suffix);
          tile.dataset.raw = String(val);
        }
        tile.querySelector(".bf-kpi-s").textContent = k.sub(s);
        var accent = k.accentIf ? k.accentIf(s) : k.accent;
        tile.style.setProperty("--bf-accent", accent || "transparent");
        if (before !== undefined && before !== tile.dataset.raw && !reduced) { tile.classList.remove("bf-flash"); void tile.offsetWidth; tile.classList.add("bf-flash"); }
      });
    }

    // ---------------------------------------------------------- charts
    var prevBars = {};
    var tip = $(".bf-tip");
    function showTip(ev, text) {
      tip.textContent = text;
      tip.classList.add("bf-show");
      var w = tip.offsetWidth;
      tip.style.left = Math.min(window.innerWidth - w - 8, ev.clientX + 12) + "px";
      tip.style.top = (ev.clientY - 40) + "px";
    }
    function drawBars(s) {
      var daily = s.daily || [], box = $(".bf-chart"), month = s.bucket === "month";
      $(".bf-chart-title").textContent = month ? "Failures per month" : "Failures per day";
      if (!daily.length) { box.innerHTML = "<div class='bf-empty'>No failures in this period.</div>"; prevBars = {}; return; }
      daily = daily.slice(month ? -36 : -62);
      var max = Math.max.apply(null, [1].concat(daily.map(function (x) { return x.failed; })));
      var bars = html("<div class='bf-bars'></div>"), next = {};
      daily.forEach(function (x) {
        var h = Math.max(3, 100 * x.failed / max), g = x.failed ? 100 * (x.fixed_next || 0) / x.failed : 0;
        next[x.day] = [h, g];
        var prev = prevBars[x.day] || [0, 0];
        var bar = html("<div class='bf-bar' role='img' aria-label='" + esc(x.day + ": " + x.failed + " failed") + "'><div class='bf-bar-f' style='height:" + (reduced ? h : prev[0]) + "%'><div class='bf-bar-g' style='height:" + (reduced ? g : prev[1]) + "%'></div></div></div>");
        var text = x.day + ": " + x.failed + " failed" + (x.fixed_next ? ", " + x.fixed_next + " fixed on the next build" : "");
        bar.onmouseenter = bar.onmousemove = function (ev) { showTip(ev, text); };
        bar.onmouseleave = function () { tip.classList.remove("bf-show"); };
        bar.onclick = function () {
          tip.classList.remove("bf-show");
          if (month) { state.period.kind = "month"; state.period.month = x.day; }
          else { state.period.kind = "custom"; state.period.from = x.day; state.period.to = x.day; }
          pControl.redraw(); persist(); state.offset = 0; loadPipelines(true); refresh();
        };
        bars.appendChild(bar);
      });
      prevBars = next;
      box.innerHTML = "";
      box.appendChild(bars);
      var ax = [daily[0].day, daily[Math.floor(daily.length / 2)].day, daily[daily.length - 1].day];
      box.appendChild(html("<div class='bf-axis'><span>" + esc(ax[0]) + "</span><span>" + (daily.length > 2 ? esc(ax[1]) : "") + "</span><span>" + (daily.length > 1 ? esc(ax[2]) : "") + "</span></div>"));
      if (!reduced) {
        void bars.offsetWidth;
        requestAnimationFrame(function () {
          Array.prototype.forEach.call(bars.children, function (b, i) {
            var t = next[daily[i].day];
            b.firstChild.style.height = t[0] + "%";
            b.firstChild.firstChild.style.height = t[1] + "%";
          });
        });
      }
    }
    var prevDonut = {};
    function drawDonut(s) {
      var cats = s.by_category || {}, keys = Object.keys(cats), total = keys.reduce(function (a, k) { return a + cats[k]; }, 0), box = $(".bf-cats");
      if (!total) { box.innerHTML = "<div class='bf-empty'>Nothing yet.</div>"; return; }
      var R = 52, C = 2 * Math.PI * R, off = 0, circles = "", targets = [];
      keys.forEach(function (k) {
        var len = C * cats[k] / total;
        targets.push([len, off, k]);
        circles += "<circle r='" + R + "' cx='66' cy='66' stroke='" + (CAT_COLOR[k] || "#8b8d98") + "' data-k='" + esc(k) + "' stroke-dasharray='" + (reduced ? len : 0) + " " + C + "' stroke-dashoffset='" + (-off) + "'/>";
        off += len;
      });
      box.innerHTML = "<div class='bf-donut-wrap'><div class='bf-donut-c'><svg class='bf-donut' viewBox='0 0 132 132'><circle r='" + R + "' cx='66' cy='66' stroke='rgba(128,128,128,.12)'/>" + circles + "</svg>" +
        "<div><b>" + fmtNum(total) + "</b><small>failures</small></div></div><div class='bf-leg'>" + keys.map(function (k) {
          return "<button type='button' class='bf-leg-row' data-k='" + esc(k) + "' aria-pressed='" + (state.categories.indexOf(k) >= 0) + "'><i style='background:" + (CAT_COLOR[k] || "#8b8d98") + "'></i><span>" + esc((CATEGORY[k] || [k])[0]) + "</span><b>" + cats[k] + "</b></button>";
        }).join("") + "</div></div>";
      $$(".bf-leg-row").forEach(function (b) {
        b.onclick = function () {
          var i = state.categories.indexOf(b.dataset.k);
          if (i >= 0) state.categories.splice(i, 1); else state.categories.push(b.dataset.k);
          catMs.set(state.categories); state.offset = 0; refresh();
        };
      });
      if (!reduced) {
        var circs = box.querySelectorAll(".bf-donut circle[data-k]");
        requestAnimationFrame(function () { requestAnimationFrame(function () {
          Array.prototype.forEach.call(circs, function (c, i) { c.setAttribute("stroke-dasharray", Math.max(0, targets[i][0] - 1.5) + " " + C); });
        }); });
      }
    }
    function drawTables(s) {
      $(".bf-sigs").innerHTML = (s.top_signatures || []).length ?
        "<table class='bf-t'><tr><th>Signature</th><th>Times</th><th>Pipelines</th><th>Fixed</th><th>Last seen</th></tr>" + s.top_signatures.map(function (x) {
          return "<tr><td><button type='button' class='bf-link-btn bf-q-set' data-q='" + esc(x.signature) + "'><span class='bf-badge bf-sig'>" + esc(x.signature) + "</span></button></td><td class='bf-num'>" + x.count + "</td><td class='bf-num'>" + x.pipelines + "</td><td class='bf-num'>" + x.fixed + "</td><td class='bf-muted'>" + esc(ago(x.last_seen)) + "</td></tr>";
        }).join("") + "</table>" : "<div class='bf-empty'>Nothing yet.</div>";
      $(".bf-jobs").innerHTML = (s.top_pipelines || []).length ?
        "<table class='bf-t'><tr><th>Pipeline</th><th>Failed</th><th>Fixed next build</th><th>Still failing</th></tr>" + s.top_pipelines.map(function (x) {
          return "<tr><td><button type='button' class='bf-link-btn bf-job-set' data-job='" + esc(x.job) + "'><strong>" + esc(x.job) + "</strong></button></td><td class='bf-num'>" + x.failed + "</td><td class='bf-num'>" + x.fixed_next + "</td><td class='bf-num'>" + x.open + "</td></tr>";
        }).join("") + "</table>" : "<div class='bf-empty'>Nothing yet.</div>";
      $$(".bf-q-set").forEach(function (b) { b.onclick = function () { qIn.value = state.q = b.dataset.q; state.offset = 0; refresh(); }; });
      $$(".bf-job-set").forEach(function (b) { b.onclick = function () { state.jobs = [b.dataset.job]; jobsMs.set(state.jobs); state.offset = 0; refresh(); }; });
    }

    // ---------------------------------------------------------- list
    var prevRows = null;
    function rowSig(i) { return [i.status, i.category, i.next_result, i.fixed_build, i.next_build].join("|"); }
    function analysisCell(i) {
      var a = i.analysis || {};
      if (i.status === "done") return categoryBadge(a.category) + (a.source === "deck" ? " " + badge("deck", "sig", "Answered from the solutions deck " + (a.deck_id || "")) : "");
      if (i.status === "error") return badge("Not analysed", "bad", i.error);
      if (i.status === "skipped") return badge("Excluded", "muted", i.error || "Excluded from analysis");
      return "<span class='bf-badge bf-info'><i class='bf-pulse'></i>Analysing</span>";
    }
    var COLS = [["id", "Entry"], ["job", "Pipeline"], ["stage", "Stage"], ["category", "Analysis"], [null, "Error observed"], ["outcome", "Outcome"], ["when", "Failed"]];
    function drawList(res) {
      var items = res.items || [], total = res.total || 0;
      $(".bf-total").textContent = fmtNum(total) + (total === 1 ? " failure" : " failures");
      var first = prevRows === null, next = {};
      var head = "<tr>" + COLS.map(function (c) {
        if (!c[0]) return "<th>" + c[1] + "</th>";
        var on = state.sort === c[0];
        return "<th data-sort='" + c[0] + "'" + (on ? " aria-sort='" + (state.dir === "asc" ? "ascending" : "descending") + "'" : "") + ">" + c[1] + ICON.arrow + "</th>";
      }).join("") + "</tr>";
      var rows = items.map(function (i, n) {
        var a = i.analysis || {}, sig = rowSig(i);
        next[i.id] = sig;
        var cls = first ? "bf-in" : (prevRows[i.id] === undefined ? "bf-new" : prevRows[i.id] !== sig ? "bf-changed" : "");
        var text = (a.error_observed || i.error || "").replace(/[`*]/g, "");
        return "<tr data-id='" + esc(i.id) + "' class='" + cls + "'" + (first ? " style='animation-delay:" + Math.min(n, 12) * 22 + "ms'" : "") + ">" +
          "<td><a href='#" + esc(i.id) + "' class='bf-id'>" + esc(i.id) + "</a></td>" +
          "<td><a class='bf-job' href='" + esc(i.build_url || "#") + "'>" + esc(i.job) + "<small>#" + i.build + "</small></a></td>" +
          "<td>" + esc(i.stage || "–") + "</td><td>" + analysisCell(i) + "</td>" +
          "<td class='bf-clip' title='" + esc(text) + "'>" + esc(text) + "</td>" +
          "<td>" + BF.outcome(i) + "</td>" +
          "<td class='bf-muted' title='" + esc(whenText(i.failed_at || i.created)) + "'>" + esc(ago(i.failed_at || i.created)) + "</td></tr>";
      }).join("");
      var filtered = drawChips() > 0;
      $(".bf-table-wrap").innerHTML = items.length ? "<table class='bf-t bf-list'>" + head + rows + "</table>" :
        "<div class='bf-empty'>" + (filtered ? "No failed builds match these filters. <button type='button' class='bf-btn bf-btn-sm bf-clear-all2'>Clear filters</button>" : "No failed builds in this period.") + "</div>";
      prevRows = next;
      var ca = $(".bf-clear-all2");
      if (ca) ca.onclick = clearFilters;
      $$(".bf-list th[data-sort]").forEach(function (th) {
        th.onclick = function () {
          var k = th.dataset.sort;
          if (state.sort === k) state.dir = state.dir === "asc" ? "desc" : "asc"; else { state.sort = k; state.dir = k === "when" ? "desc" : "asc"; }
          state.offset = 0; persist(); refresh(true);
        };
      });
      $$(".bf-list tr[data-id]").forEach(function (tr) {
        tr.onclick = function (ev) { if (ev.target.closest("a") && !ev.target.classList.contains("bf-id")) return; ev.preventDefault(); location.hash = tr.dataset.id; };
      });
      var from = total ? state.offset + 1 : 0, to = Math.min(total, state.offset + items.length);
      $(".bf-pager").innerHTML = "<span class='bf-muted'>" + fmtNum(from) + "–" + fmtNum(to) + " of " + fmtNum(total) + "</span>" +
        "<label class='bf-muted'>Rows " + selectHtml([["10", "10"], ["25", "25"], ["50", "50"], ["100", "100"]], String(state.limit), "bf-limit") + "</label>" +
        "<button type='button' class='bf-btn bf-btn-sm bf-prev'" + (state.offset ? "" : " disabled") + ">‹ Newer</button>" +
        "<button type='button' class='bf-btn bf-btn-sm bf-next'" + (to < total ? "" : " disabled") + ">Older ›</button>";
      $(".bf-prev").onclick = function () { state.offset = Math.max(0, state.offset - state.limit); refresh(true); };
      $(".bf-next").onclick = function () { state.offset += state.limit; refresh(true); };
      $(".bf-limit").onchange = function () { state.limit = +this.value; state.offset = 0; persist(); refresh(true); };
    }

    // ---------------------------------------------------------- refresh cycle
    var seq = 0, lastOk = null;
    function refresh(listOnly) {
      var my = ++seq, banner = $(".bf-banner");
      if (state.view !== "failures") return;
      var s = listOnly === true ? null : getJson(base + "stats?" + params({}));
      var l = getJson(base + "incidents?" + params({sort: state.sort, dir: state.dir, limit: state.limit, offset: state.offset}));
      drawChips();
      Promise.all([s, l]).then(function (r) {
        if (my !== seq) return;
        var st = r[0], li = r[1];
        if ((st && !st.ok) || !li.ok) { banner.innerHTML = "<p class='bf-err'>" + esc(((st && !st.ok ? st : li).j || {}).error || "Build Forge service is not answering.") + "</p>"; return; }
        banner.innerHTML = "";
        lastOk = Date.now();
        if (st) {
          drawKpis(st.j); drawBars(st.j); drawDonut(st.j); drawTables(st.j);
          var c = $("[data-c='failures']"); c.textContent = fmtNum(st.j.failed_builds);
        }
        drawList(li.j);
        if (state.offset && !(li.j.items || []).length && li.j.total) { state.offset = 0; refresh(true); }
      }).catch(function (e) { if (my === seq) banner.innerHTML = "<p class='bf-err'>" + esc(e) + "</p>"; });
    }

    // ---------------------------------------------------------- live updates
    var liveBtn = $(".bf-live"), liveT = $(".bf-live-t"), lastRev = null, lastNow = 0, pulseTimer = null, liveState = "on";
    function setLive(st) {
      liveState = st;
      liveBtn.dataset.state = st;
      paintLive();
    }
    function paintLive() {
      var t = liveState === "paused" ? "Paused" : liveState === "offline" ? "Offline, retrying" : "Live";
      if (liveState === "on" && lastOk) t += " · " + (Date.now() - lastOk < 8000 ? "just updated" : "updated " + ago(lastOk / 1000));
      liveT.textContent = t;
    }
    liveBtn.onclick = function () {
      state.live = !state.live; persist();
      setLive(state.live ? "on" : "paused");
      if (state.live) pulse();
    };
    setInterval(paintLive, 5000);
    function toast(title, sub, kind, href) {
      var box = $(".bf-toasts");
      while (box.children.length >= 3) box.removeChild(box.firstChild);
      var t = html("<div class='bf-toast " + (kind ? "bf-t-" + kind : "") + "'><i></i><div><strong>" + esc(title) + "</strong><small>" + esc(sub || "") + "</small></div></div>");
      t.onclick = function () { if (href) location.hash = href; t.remove(); };
      box.appendChild(t);
      setTimeout(function () { t.classList.add("bf-out"); setTimeout(function () { t.remove(); }, 260); }, 6500);
    }
    function announce(changes, since) {
      var shown = 0;
      (changes || []).slice().reverse().forEach(function (c) {
        if (shown >= 3) return;
        if (c.created > since) { toast("New failure · " + c.job + " #" + c.build, c.status === "skipped" ? "Excluded from analysis" : "Kiro is reading it…", "bad", c.id); shown++; }
        else if (c.status === "done") { toast("Analysis ready · " + c.job + " #" + c.build, (CATEGORY[c.category] || [c.category || ""])[0], "good", c.id); shown++; }
      });
      if ((changes || []).length > shown + 2) toast((changes.length - shown) + " more changes", "The list has been refreshed");
    }
    function pulse() {
      clearTimeout(pulseTimer);
      if (!document.body.contains(root)) return;
      if (!state.live || document.hidden) { pulseTimer = setTimeout(pulse, 4000); return; }
      getJson(base + "pulse" + (lastNow ? "?since=" + lastNow : "")).then(function (res) {
        if (!res.ok) { setLive("offline"); return; }
        setLive("on");
        var j = res.j;
        if (lastRev !== null && j.rev !== lastRev) {
          announce(j.changes, lastNow);
          loadAlerts(true);
          if (state.view === "failures") { pipelinesCache = null; refresh(); } else loadDeck(true);
          lastOk = Date.now();
        }
        lastRev = j.rev; lastNow = j.now;
      }).catch(function () { setLive("offline"); }).then(function () { pulseTimer = setTimeout(pulse, 4000); });
    }
    document.addEventListener("visibilitychange", function () { if (!document.hidden && state.live) { clearTimeout(pulseTimer); pulse(); } });

    // ---------------------------------------------------------- overlays
    var drawer = $("[data-drawer]"), modal = $("[data-modal]");
    function closeDrawer() { drawer.hidden = true; drawer.innerHTML = ""; if (/^#(BF|SD)-/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search + (state.view === "deck" ? "#deck" : "")); }
    function closeModal() { modal.hidden = true; modal.innerHTML = ""; }
    drawer.onclick = function (ev) { if (ev.target === drawer) closeDrawer(); };
    modal.onclick = function (ev) { if (ev.target === modal) closeModal(); };
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { if (!modal.hidden) closeModal(); else if (!drawer.hidden) closeDrawer(); else if (!alertsEl.hidden) closeAlerts(); }
      if (ev.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "") && modal.hidden && drawer.hidden) {
        ev.preventDefault(); (state.view === "deck" ? $(".bf-dq") : qIn).focus();
      }
    });
    function post(path, body) {
      var h = crumbHeaders(root); h["Content-Type"] = "application/json";
      return fetch(base + path, {method: "POST", headers: h, credentials: "same-origin", body: JSON.stringify(body || {})})
        .then(function (r) { return r.text().then(function (t) { var j; try { j = JSON.parse(t); } catch (e) { j = {error: "HTTP " + r.status}; } return {ok: r.ok, j: j}; }); });
    }
    function download(url, button, label) {
      var old = button.innerHTML;
      button.disabled = true;
      button.innerHTML = "<span class='bf-spin'></span> Preparing…";
      return fetch(url, {credentials: "same-origin"}).then(function (r) {
        if (!r.ok) return r.text().then(function (t) { var m; try { m = JSON.parse(t).error; } catch (e) { m = "HTTP " + r.status; } throw new Error(m); });
        var cd = r.headers.get("Content-Disposition") || "", m = /filename="?([^";]+)"?/.exec(cd);
        return r.blob().then(function (b) { return {b: b, name: m ? m[1] : "buildforge-report"}; });
      }).then(function (x) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(x.b); a.download = x.name; document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
        toast("Downloaded", x.name, "good");
      }).catch(function (e) { toast("Download failed", String(e.message || e), "bad"); })
        .then(function () { button.disabled = false; button.innerHTML = old; });
    }

    // ---------------------------------------------------------- incident drawer
    function showIncident(id) {
      drawer.hidden = false;
      drawer.innerHTML = "<div class='bf-drawer'><div class='bf-skeleton'></div><div class='bf-skeleton' style='width:70%'></div><div class='bf-skeleton' style='width:85%'></div></div>";
      getJson(base + "incident?id=" + encodeURIComponent(id)).then(function (res) {
        var i = res.j, box = drawer.querySelector(".bf-drawer");
        if (!res.ok) { box.innerHTML = "<div class='bf-drawer-bar'><button type='button' class='bf-btn bf-close'>Close</button></div><p class='bf-err'>" + esc(i.error) + "</p>"; box.querySelector(".bf-close").onclick = closeDrawer; return; }
        var a = i.analysis || {};
        box.innerHTML = "<div class='bf-drawer-bar'><a class='bf-btn' href='" + esc(i.build_url || "#") + "'>Open build</a>" +
          "<button type='button' class='bf-btn bf-re'>" + (i.status === "skipped" ? "Analyse anyway" : "Analyse again") + "</button>" +
          "<button type='button' class='bf-btn bf-dl'>Download log</button><button type='button' class='bf-btn bf-btn-ghost bf-close'>Close</button></div>" +
          (a.source === "deck" ? "<div class='bf-deckline'>Answered from the solutions deck: <a href='#" + esc(a.deck_id) + "'>" + esc(a.deck_id) + "</a> · " + esc(a.deck_note || "") + "</div>" : "") +
          "<div class='bf-muted bf-file'>Log file on the Jenkins server: <code>" + esc(i.log_file) + "</code></div>" +
          "<div class='bf-doc'>" + (i.log_text ? md(i.log_text) : "<p class='bf-muted'>No log file yet (analysis " + esc(i.status) + ").</p>") + "</div>" +
          ((i.related || []).length ? "<h3>Same error elsewhere</h3><ul>" + i.related.map(function (r) {
            return "<li><a href='#" + esc(r.id) + "'>" + esc(r.id) + "</a> " + esc(r.job) + " #" + r.build + " · " + esc(ago(r.created)) + (r.next_result ? " · next build " + esc(r.next_result) : "") + "</li>";
          }).join("") + "</ul>" : "");
        box.querySelector(".bf-close").onclick = closeDrawer;
        box.querySelector(".bf-dl").onclick = function () {
          var x = document.createElement("a");
          x.href = URL.createObjectURL(new Blob([i.log_text || ""], {type: "text/markdown"}));
          x.download = i.id + ".md"; x.click();
        };
        box.querySelector(".bf-re").onclick = function (ev) {
          ev.target.disabled = true;
          post("reanalyze?id=" + encodeURIComponent(i.id), {}).then(function (r) {
            ev.target.textContent = r.j.error ? r.j.error : "Queued";
            if (!r.j.error) toast("Analysis queued", i.id);
            refresh();
          });
        };
      });
    }

    // ---------------------------------------------------------- export dialog
    function openExport() {
      var ex = {scope: "full", format: "xlsx", period: JSON.parse(JSON.stringify(state.period)), jobs: state.jobs.slice(), other: true};
      var hasOther = !!(state.q || state.categories.length || state.outcome || state.analysis || state.rebuild);
      modal.hidden = false;
      modal.innerHTML = "<div class='bf-modal bf-root' role='dialog' aria-label='Download a report'><h2>Download a report</h2>" +
        "<p class='bf-sub'>Choose what to include, the period and the pipelines. The file matches what the dashboard shows for the same filters.</p>" +
        "<div class='bf-field'><span>What</span><div class='bf-choice bf-c-scope'>" +
        [["full", "Full report", "Summary numbers plus every failure"], ["list", "Failures list", "One row per failed build"], ["summary", "Dashboard summary", "Cards, per day, categories, top errors and pipelines"]].map(function (x) {
          return "<label><input type='radio' name='bf-scope' value='" + x[0] + "'" + (ex.scope === x[0] ? " checked" : "") + "><b>" + x[1] + "</b><small>" + x[2] + "</small></label>";
        }).join("") + "</div></div>" +
        "<div class='bf-field'><span>Format</span><div class='bf-choice bf-c-format'>" +
        [["xlsx", "Excel (.xlsx)", "Summary and failures on separate sheets"], ["csv", "CSV", "Opens anywhere, one flat table"], ["pdf", "PDF", "A readable, printable report"]].map(function (x) {
          return "<label><input type='radio' name='bf-format' value='" + x[0] + "'" + (ex.format === x[0] ? " checked" : "") + "><b>" + x[1] + "</b><small>" + x[2] + "</small></label>";
        }).join("") + "</div></div>" +
        "<div class='bf-field'><span>Period</span><div class='bf-ex-period'></div></div>" +
        "<div class='bf-field'><span>Pipelines</span><div class='bf-ex-jobs'></div></div>" +
        (hasOther ? "<label class='bf-opt'><input type='checkbox' class='bf-ex-other' checked><span>Also apply the other dashboard filters (search, category, outcome…)</span></label>" : "") +
        "<div class='bf-sum bf-ex-sum'></div>" +
        "<div class='bf-modal-foot'><span class='bf-note'></span><button type='button' class='bf-btn bf-cancel'>Cancel</button><button type='button' class='bf-btn bf-btn-primary bf-go'>" + ICON.download + "Download</button></div></div>";
      var box = modal.querySelector(".bf-modal");
      var pc = periodControl(ex.period, function () { updateSummary(); });
      box.querySelector(".bf-ex-period").appendChild(pc.el);
      var jm = multiSelect({
        all: "All pipelines", plural: "pipelines", values: ex.jobs, searchable: true, allowPattern: true,
        placeholder: "Search pipelines, or type a pattern like payments/*",
        loadItems: function (done) { loadPipelines(false, done); }, onChange: function (v) { ex.jobs = v; updateSummary(); }
      });
      box.querySelector(".bf-ex-jobs").appendChild(jm.el);
      Array.prototype.forEach.call(box.querySelectorAll("input[name=bf-scope]"), function (r) { r.onchange = function () { ex.scope = r.value; updateSummary(); }; });
      Array.prototype.forEach.call(box.querySelectorAll("input[name=bf-format]"), function (r) { r.onchange = function () { ex.format = r.value; }; });
      var oc = box.querySelector(".bf-ex-other");
      if (oc) oc.onchange = function () { ex.other = oc.checked; updateSummary(); };
      var sumSeq = 0;
      function exParams(extra) { return params(extra, {period: ex.period, jobs: ex.jobs, noFilters: !ex.other}); }
      var updateSummary = debounce(function () {
        var my = ++sumSeq, sum = box.querySelector(".bf-ex-sum");
        sum.innerHTML = "<div><b>Period</b>" + esc(periodLabel(ex.period)) + "</div><div><b>Pipelines</b>" + esc(ex.jobs.length ? ex.jobs.join(", ") : "All") + "</div><div><b>Includes</b><span class='bf-ex-n'>counting…</span></div>";
        getJson(base + "stats?" + exParams({})).then(function (res) {
          if (my !== sumSeq || !res.ok) return;
          var n = box.querySelector(".bf-ex-n");
          if (n) n.textContent = fmtNum(res.j.failed_builds) + " failed builds in " + fmtNum(res.j.failed_pipelines) + " pipelines";
          box.querySelector(".bf-go").disabled = !res.j.failed_builds && ex.scope === "list";
          box.querySelector(".bf-note").textContent = !res.j.failed_builds ? "Nothing matches this period and these pipelines." : (ex.format === "pdf" && res.j.failed_builds > 3000 ? "The PDF lists the first 3,000 failures; use Excel or CSV for all." : "");
        });
      }, 200);
      updateSummary();
      box.querySelector(".bf-cancel").onclick = closeModal;
      box.querySelector(".bf-go").onclick = function () {
        var go = this;
        download(base + "export?" + exParams({format: ex.format, scope: ex.scope, sort: state.sort, dir: state.dir}), go).then(function () { closeModal(); });
      };
    }
    $(".bf-export").onclick = function () {
      if (state.view === "deck") { openDeckExport(this); return; }
      openExport();
    };

    // ---------------------------------------------------------- solutions deck
    var deckStatuses = [["", "All cards"], ["usable", "Ready to use"], ["approved", "Approved"], ["draft", "Draft"], ["retired", "Retired"]];
    var deckSorts = [["seen", "Most seen"], ["last", "Recently seen"], ["pipelines", "Most pipelines"], ["fixed", "Best fix rate"], ["used", "Most used"], ["title", "Title A–Z"], ["updated", "Recently edited"]];
    var deckBar = $(".bf-deck-bar");
    var dStatus = html(selectHtml(deckStatuses, deck.status, "bf-d-status")), dCat = html(selectHtml([["", "All categories"]].concat(Object.keys(CATEGORY).slice(0, 7).map(function (k) { return [k, CATEGORY[k][0]]; })), "", "bf-d-cat")),
      dSort = html(selectHtml(deckSorts, deck.sort, "bf-d-sort"));
    var dNew = html("<button type='button' class='bf-btn'>" + ICON.plus + "New card</button>"), dImp = html("<button type='button' class='bf-btn'>" + ICON.upload + "Import</button>");
    var dFile = html("<input type='file' accept='application/json,.json' hidden>");
    [dStatus, dCat, dSort, dNew, dImp, dFile].forEach(function (n) { deckBar.appendChild(n); });
    dStatus.onchange = function () { deck.status = this.value; deck.offset = 0; loadDeck(); };
    dCat.onchange = function () { deck.category = this.value; deck.offset = 0; loadDeck(); };
    dSort.onchange = function () { deck.sort = this.value; deck.dir = this.value === "title" ? "asc" : "desc"; deck.offset = 0; loadDeck(); };
    $(".bf-dq").oninput = debounce(function () { deck.q = this.value.trim(); deck.offset = 0; loadDeck(); }, 250);
    dNew.onclick = function () { showCard(null); };
    dImp.onclick = function () { dFile.click(); };
    dFile.onchange = function () {
      var f = dFile.files[0];
      if (!f) return;
      var rd = new FileReader();
      rd.onload = function () {
        var data; try { data = JSON.parse(rd.result); } catch (e) { toast("Import failed", "That file is not JSON", "bad"); return; }
        post("solutionsImport", data).then(function (r) {
          if (r.ok) { toast("Deck imported", r.j.added + " added, " + r.j.skipped + " skipped (already there or incomplete)", "good"); loadDeck(); }
          else toast("Import failed", r.j.error || "", "bad");
        });
      };
      rd.readAsText(f);
      dFile.value = "";
    };
    function openDeckExport(anchor) {
      var old = root.querySelector(".bf-pop-export");
      if (old) { old.remove(); return; }
      var pop = html("<div class='bf-pop bf-pop-export' style='position:absolute;right:0;left:auto;width:230px'><div class='bf-pop-list'>" +
        [["pdf", "PDF deck", "Readable, one card per section"], ["xlsx", "Excel", "One row per card"], ["csv", "CSV", "Flat table"], ["json", "JSON", "Portable, re-importable"]].map(function (f) {
          return "<button type='button' class='bf-opt' style='width:100%;border:0;background:none;color:inherit;text-align:left' data-f='" + f[0] + "'><span><b>" + f[1] + "</b><br><small class='bf-muted'>" + f[2] + "</small></span></button>";
        }).join("") + "</div></div>");
      anchor.parentNode.style.position = "relative";
      anchor.parentNode.appendChild(pop);
      Array.prototype.forEach.call(pop.querySelectorAll("button"), function (b) {
        b.onclick = function () { pop.remove(); download(base + "solutionsExport?format=" + b.dataset.f + "&status=" + encodeURIComponent(deck.status === "usable" ? "" : deck.status) + "&tz=" + (-new Date().getTimezoneOffset()), anchor); };
      });
      setTimeout(function () { document.addEventListener("mousedown", function h(ev) { if (!pop.contains(ev.target)) { pop.remove(); document.removeEventListener("mousedown", h); } }); }, 0);
    }
    var deckSeq = 0;
    function loadDeck(silent) {
      var my = ++deckSeq;
      getJson(base + "solutions?" + ["q=" + encodeURIComponent(deck.q), "status=" + deck.status, "category=" + deck.category, "sort=" + deck.sort, "dir=" + deck.dir, "limit=" + deck.limit, "offset=" + deck.offset].join("&")).then(function (res) {
        if (my !== deckSeq) return;
        if (!res.ok) { $(".bf-banner").innerHTML = "<p class='bf-err'>" + esc(res.j.error) + "</p>"; return; }
        $(".bf-banner").innerHTML = "";
        deck.counts = res.j.counts; deck.mode = res.j.mode; deck.total = res.j.total;
        $("[data-c='deck']").textContent = fmtNum(res.j.counts.all);
        drawDeck(res.j, silent);
      });
    }
    var prevCards = null;
    function drawDeck(res, silent) {
      var c = res.counts, items = res.items || [];
      $(".bf-deck-stats").innerHTML = [["Cards", c.all], ["Ready to use", c.usable], ["Approved", c.approved], ["Draft", c.draft], ["Retired", c.retired]].map(function (x) {
        return "<div class='bf-pill-stat'><b>" + fmtNum(x[1]) + "</b>" + x[0] + "</div>";
      }).join("");
      var modeText = {
        fallback: "<strong>Fallback mode.</strong> Kiro answers every failure. If Kiro is unavailable, a matching card in the deck answers instead.",
        first: "<strong>Deck first.</strong> A close match with an approved card is answered from the deck before Kiro is asked; everything else goes to Kiro.",
        off: "<strong>Deck answers are off</strong> (BF_DECK_MODE=off). The deck keeps learning but is never used to answer."
      };
      $(".bf-deck-note").innerHTML = (modeText[res.mode] || modeText.fallback) + " Every analysis Kiro writes is filed here by error signature; approve a card once you trust its answer. A draft is used once its error has been seen " + "at least twice.";
      var first = prevCards === null, next = {};
      var rows = items.map(function (x, n) {
        var sig = x.status + "|" + x.times_seen + "|" + x.updated;
        next[x.id] = sig;
        var cls = first && !silent ? "bf-in" : (prevCards && prevCards[x.id] === undefined ? "bf-new" : prevCards && prevCards[x.id] !== sig ? "bf-changed" : "");
        return "<tr data-id='" + esc(x.id) + "' class='" + cls + "'><td><div><strong>" + esc(x.title) + "</strong></div><div class='bf-muted' style='font-size:.78rem'><a class='bf-id' href='#" + esc(x.id) + "'>" + esc(x.id) + "</a>" + (x.signature ? " <span class='bf-badge bf-sig'>" + esc(x.signature) + "</span>" : "") + "</div></td>" +
          "<td>" + deckStatusBadge(x) + "</td><td>" + categoryBadge(x.category) + "</td><td class='bf-num'>" + x.times_seen + "</td><td class='bf-num'>" + x.pipelines + "</td>" +
          "<td class='bf-num'>" + pct(x.fixed_next, x.times_seen) + "</td><td class='bf-num'>" + x.used + "</td><td class='bf-muted'>" + esc(ago(x.last_seen)) + "</td></tr>";
      }).join("");
      prevCards = next;
      $(".bf-deck-table").innerHTML = items.length ? "<table class='bf-t bf-list'><tr><th>Card</th><th>Status</th><th>Category</th><th>Seen</th><th>Pipelines</th><th>Fixed next build</th><th>Answers given</th><th>Last seen</th></tr>" + rows + "</table>" :
        "<div class='bf-empty'>" + (c.all ? "No cards match." : "No cards yet. They appear as soon as Kiro analyses a failure, or add one by hand.") + "</div>";
      $$(".bf-deck-table tr[data-id]").forEach(function (tr) { tr.onclick = function (ev) { ev.preventDefault(); location.hash = tr.dataset.id; }; });
      var from = res.total ? deck.offset + 1 : 0, to = Math.min(res.total, deck.offset + items.length);
      $(".bf-deck-pager").innerHTML = "<span class='bf-muted'>" + from + "–" + to + " of " + res.total + "</span><button type='button' class='bf-btn bf-btn-sm bf-dprev'" + (deck.offset ? "" : " disabled") + ">‹ Prev</button><button type='button' class='bf-btn bf-btn-sm bf-dnext'" + (to < res.total ? "" : " disabled") + ">Next ›</button>";
      $(".bf-dprev").onclick = function () { deck.offset = Math.max(0, deck.offset - deck.limit); loadDeck(); };
      $(".bf-dnext").onclick = function () { deck.offset += deck.limit; loadDeck(); };
    }
    function deckStatusBadge(x) {
      if (x.status === "approved") return badge("Approved", "good");
      if (x.status === "retired") return badge("Retired", "muted");
      return badge(x.usable ? "Draft · in use" : "Draft", x.usable ? "warn" : "muted", x.usable ? "Seen often enough to answer when Kiro is unavailable" : "Not seen often enough yet");
    }
    $(".bf-try-go").onclick = function () {
      var text = $(".bf-try-text").value.trim(), out = $(".bf-try-out");
      if (!text) return;
      out.hidden = false; out.textContent = "Looking…";
      getJson(base + "solutionMatch?all=1&min=0.25&text=" + encodeURIComponent(text)).then(function (res) {
        var m = res.j.match;
        if (!res.ok) { out.textContent = res.j.error || "Failed"; return; }
        if (!m) { out.innerHTML = "No card is close enough. This error would go to Kiro, and the answer would start a new card."; return; }
        var score = Math.round(m.score * 100);
        out.innerHTML = "<strong>" + esc(m.title) + "</strong> · <a href='#" + esc(m.id) + "'>" + esc(m.id) + "</a> · " + esc(m.how) + " · " + score + "% match" +
          "<div class='bf-meter'><span style='width:" + score + "%'></span></div>" +
          "<div class='bf-muted' style='margin-top:6px'>" + (m.usable ? "A failure like this would " + (m.status === "approved" ? "be answered from this card" : "get this card's answer if Kiro is unavailable") + "." : "This card is not trusted enough to answer yet (draft, seen " + m.times_seen + "×).") + "</div>";
      });
    };

    var CATS = ["application", "infrastructure", "configuration", "dependency", "test_failure", "transient", "unknown"];
    function showCard(id) {
      drawer.hidden = false;
      drawer.innerHTML = "<div class='bf-drawer'><div class='bf-skeleton'></div><div class='bf-skeleton' style='width:70%'></div></div>";
      if (!id) { drawCard({title: "", status: "draft", category: "unknown", error_observed: "", proposed_solution: "", rebuild_can_fix: false, rebuild_reason: "", notes: "", incidents: [], isNew: true}); return; }
      getJson(base + "solution?id=" + encodeURIComponent(id)).then(function (res) {
        if (!res.ok) { drawer.querySelector(".bf-drawer").innerHTML = "<div class='bf-drawer-bar'><button type='button' class='bf-btn bf-close'>Close</button></div><p class='bf-err'>" + esc(res.j.error) + "</p>"; drawer.querySelector(".bf-close").onclick = closeDrawer; return; }
        drawCard(res.j);
      });
    }
    function drawCard(c) {
      var box = drawer.querySelector(".bf-drawer");
      box.innerHTML = "<div class='bf-drawer-bar'><button type='button' class='bf-btn bf-btn-primary bf-save'>" + (c.isNew ? "Create card" : "Save changes") + "</button>" +
        (c.isNew ? "" : (c.status !== "approved" ? "<button type='button' class='bf-btn bf-approve'>Approve</button>" : "<button type='button' class='bf-btn bf-draft'>Back to draft</button>") +
          (c.status === "retired" ? "" : "<button type='button' class='bf-btn bf-retire'>Retire</button>")) +
        "<button type='button' class='bf-btn bf-btn-ghost bf-close'>Close</button></div>" +
        "<h2 style='margin:0 0 4px'>" + (c.isNew ? "New card" : esc(c.title)) + "</h2>" +
        (c.isNew ? "<p class='bf-muted'>Add a known problem and its fix. Paste the error lines it should match so the deck can recognise it later.</p>" :
          "<div class='bf-status-bar'><a class='bf-id' href='#" + esc(c.id) + "'>" + esc(c.id) + "</a>" + deckStatusBadge(c) + categoryBadge(c.category) + (c.signature ? "<span class='bf-badge bf-sig'>" + esc(c.signature) + "</span>" : "") +
          "<span class='bf-muted'>seen " + c.times_seen + "× in " + c.pipelines + " pipeline(s) · fixed on the next build " + pct(c.fixed_next, c.times_seen) + " · answered " + c.used + "×" +
          (c.edited_by ? " · last edited by " + esc(c.edited_by) : "") + (c.source === "manual" ? " · wording kept (Kiro will not overwrite it)" : "") + "</span></div>") +
        "<label class='bf-field'><span>Title</span><input class='bf-input bf-c-title' value='" + esc(c.title) + "'></label>" +
        "<div class='bf-form-row'><label class='bf-field'><span>Category</span>" + selectHtml(CATS.map(function (k) { return [k, CATEGORY[k][0]]; }), c.category || "unknown", "bf-c-cat") + "</label>" +
        "<label class='bf-field'><span>Signature (optional)</span><input class='bf-input bf-c-sig' value='" + esc(c.signature || "") + "' placeholder='npm-e401-codeartifact'></label></div>" +
        (c.isNew ? "<label class='bf-field'><span>Error lines to match</span><textarea class='bf-input bf-c-pattern' rows='4' placeholder='Paste the failing lines from a build log'></textarea></label>" : "") +
        "<label class='bf-field'><span>Error observed</span><textarea class='bf-input bf-c-err' rows='3'>" + esc(c.error_observed || "") + "</textarea></label>" +
        "<label class='bf-field'><span>Proposed solution (Markdown: numbered steps, code in backticks)</span><textarea class='bf-input bf-c-sol' rows='9'>" + esc(c.proposed_solution || "") + "</textarea></label>" +
        "<div class='bf-preview'><div class='bf-sec' style='margin-top:0'>Preview</div><div class='bf-text bf-c-prev'></div></div>" +
        "<div class='bf-form-row'><label class='bf-opt' style='align-self:end;margin-bottom:12px'><input type='checkbox' class='bf-c-rb'" + (c.rebuild_can_fix ? " checked" : "") + "><span>A rebuild with the same parameters can fix this</span></label>" +
        "<label class='bf-field'><span>Why (rebuild)</span><input class='bf-input bf-c-rbr' value='" + esc(c.rebuild_reason || "") + "'></label></div>" +
        "<label class='bf-field'><span>Notes for the team</span><textarea class='bf-input bf-c-notes' rows='2'>" + esc(c.notes || "") + "</textarea></label>" +
        (c.pattern ? "<div class='bf-sec'>Looks like</div><pre class='bf-pre'>" + esc(c.pattern) + "</pre>" : "") +
        ((c.incidents || []).length ? "<div class='bf-sec'>Recent failures with this error</div><ul>" + c.incidents.map(function (r) {
          return "<li><a href='#" + esc(r.id) + "'>" + esc(r.id) + "</a> " + esc(r.job) + " #" + r.build + " · " + esc(ago(r.ts)) + (r.next_result ? " · next build " + esc(r.next_result) : "") + (/^Solutions deck/.test(r.analysed_by || "") ? " · answered from the deck" : "") + "</li>";
        }).join("") + "</ul>" : "");
      var f = function (s) { return box.querySelector(s); };
      var prev = debounce(function () { f(".bf-c-prev").innerHTML = md(f(".bf-c-sol").value) || "<span class='bf-muted'>Nothing yet.</span>"; }, 120);
      f(".bf-c-sol").oninput = prev; prev();
      f(".bf-close").onclick = closeDrawer;
      function fields() {
        return {title: f(".bf-c-title").value, category: f(".bf-c-cat").value, signature: f(".bf-c-sig").value, error_observed: f(".bf-c-err").value, proposed_solution: f(".bf-c-sol").value,
          rebuild_can_fix: f(".bf-c-rb").checked, rebuild_reason: f(".bf-c-rbr").value, notes: f(".bf-c-notes").value};
      }
      function save(extra, done) {
        var body = Object.assign(fields(), extra || {});
        var req = c.isNew ? post("solutionCreate", Object.assign(body, {error_text: (f(".bf-c-pattern") || {}).value || ""})) : post("solutionUpdate", Object.assign(body, {id: c.id}));
        req.then(function (r) {
          if (!r.ok) { toast("Not saved", r.j.error || "", "bad"); return; }
          toast(c.isNew ? "Card created" : "Saved", r.j.title, "good");
          loadDeck(true);
          if (c.isNew) location.hash = r.j.id; else drawCard(Object.assign({}, c, r.j, {incidents: c.incidents}));
          if (done) done();
        });
      }
      f(".bf-save").onclick = function () { save(); };
      var ap = f(".bf-approve"), dr = f(".bf-draft"), rt = f(".bf-retire");
      if (ap) ap.onclick = function () { save({status: "approved"}); };
      if (dr) dr.onclick = function () { save({status: "draft"}); };
      if (rt) rt.onclick = function () { save({status: "retired"}); };
    }


    // ---------------------------------------------------------- alerts (the bell)
    var SEV = {critical: ["Critical", "bf-sev-critical"], high: ["High", "bf-sev-high"], low: ["Low", "bf-sev-low"]};
    var ENV = {prod: "Production", uat: "UAT / staging", other: "Other"};
    var al = {items: [], counts: {open: 0, unread: 0, dismissed: 0}, rules: {prod: 3, uat: 5, other: 5}, status: "open", sev: "", loaded: false, known: null, shown: []};
    var bell = $(".bf-bell"), bellN = $(".bf-bell-n"), alertsEl = $("[data-alerts]");
    function paintBell() {
      var c = al.counts, n = c.unread || 0, top = c.unread_critical ? "critical" : c.unread_high ? "high" : c.unread_low ? "low" : "";
      bellN.hidden = !n;
      bellN.textContent = n > 99 ? "99+" : String(n);
      bellN.className = "bf-bell-n" + (top ? " " + SEV[top][1] : "");
      bell.classList.toggle("bf-has", !!c.open);
      bell.setAttribute("aria-label", "Alerts" + (n ? ", " + n + " unread" : ""));
    }
    function ringBell() {
      if (reduced) return;
      bell.classList.remove("bf-ring"); void bell.offsetWidth; bell.classList.add("bf-ring");
    }
    function loadAlerts(announceNew) {
      return getJson(base + "alerts?status=" + al.status).then(function (res) {
        if (!res.ok) return;
        var j = res.j;
        if (al.status === "open") {
          var ids = {};
          j.items.forEach(function (a) { ids[a.id] = a; });
          if (announceNew && al.known) {
            j.items.filter(function (a) { return !al.known[a.id] && !a.read; }).slice(0, 2).forEach(function (a) {
              toast((SEV[a.severity] || [a.severity])[0] + " alert · " + a.job, a.streak + " failed builds in a row", a.severity === "low" ? "" : "bad", "alerts");
              ringBell();
            });
          }
          al.known = ids;
        }
        al.items = j.items; al.counts = j.counts; al.rules = j.rules || al.rules; al.loaded = true;
        paintBell();
        if (!alertsEl.hidden) drawAlerts();
      });
    }
    function mailtoFor(a) {
      var c = a.contacts || {}, to = [];
      (c.committers || []).forEach(function (p) { if (p.email && to.indexOf(p.email) < 0) to.push(p.email); });
      var subject = "[Build Forge] " + a.job + " has failed " + a.streak + " builds in a row";
      var body = "Hi,\n\nThe pipeline " + a.job + " has failed " + a.streak + " builds in a row (#" + a.first_build + " to #" + a.last_build + ") and has not been green since." +
        (a.latest && a.latest.build_url ? "\nLatest build: " + a.latest.build_url : "") +
        (a.latest && a.latest.error ? "\nError: " + a.latest.error.replace(/\s+/g, " ") : "") + "\n\nCould you take a look?\n";
      return "mailto:" + to.map(encodeURIComponent).join(",") + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
    }
    function whoLine(t) {
      if (!t) return "";
      var name = t.name || t.id || "";
      if (t.kind === "user") return "<b>" + esc(name) + "</b>" + (t.id && t.id !== name ? " <span class='bf-muted'>(" + esc(t.id) + ")</span>" : "");
      return "<span>" + esc(name || t.kind) + "</span>";
    }
    function alertCard(a, i) {
      var c = a.contacts || {}, sev = SEV[a.severity] || [a.severity, ""], people = c.committers || [], l = a.latest || {};
      var started = c.started_latest, first = c.started_first;
      var html_ = "<article class='bf-alert " + sev[1] + (a.read ? "" : " bf-unread") + "' data-id='" + esc(a.id) + "' style='animation-delay:" + Math.min(i, 8) * 40 + "ms'>" +
        "<div class='bf-alert-head'><span class='bf-sev " + sev[1] + "'>" + esc(sev[0]) + "</span><span class='bf-env'>" + esc(ENV[a.env] || a.env) + "</span>" +
        (a.read ? "" : "<span class='bf-new'>New</span>") + "<span class='bf-spacer bf-muted bf-alert-ago'>" + esc(ago(a.last_failed_at)) + "</span></div>" +
        "<h3 class='bf-alert-job' title='" + esc(a.job) + "'>" + esc(a.job) + "</h3>" +
        "<div class='bf-alert-sub'><b>" + a.streak + "</b> failed builds in a row <span class='bf-muted'>· #" + a.first_build + " to #" + a.last_build +
        (a.first_failed_at ? " · since " + esc(whenText(a.first_failed_at)) : "") + "</span></div>" +
        (l.signature || l.error ? "<div class='bf-alert-err'>" + (l.signature ? "<code>" + esc(l.signature) + "</code> " : "") + esc((l.error || "").replace(/`/g, "").slice(0, 160)) + "</div>" : "") +
        "<div class='bf-contacts'>" +
        (started ? "<div class='bf-contact-row'><span class='bf-ck'>Started by</span><span>" + whoLine(started) + " <span class='bf-muted'>#" + started.build + "</span>" +
          (first && first.build !== started.build && (first.name || first.id) !== (started.name || started.id) ? " <span class='bf-muted'>· first failure by</span> " + whoLine(first) : "") + "</span></div>" : "") +
        (people.length ? "<div class='bf-contact-row'><span class='bf-ck'>Committed by</span><span class='bf-people'>" + people.map(function (p) {
          var tip = (p.commits || []).map(function (x) { return (x.id ? x.id + " " : "") + x.message; }).join("\n");
          return "<span class='bf-person" + (p.first_failure ? " bf-p-first" : "") + "' title='" + esc(tip) + "'><b>" + esc(p.name || p.id) + "</b>" +
            (p.email ? " <a href='mailto:" + esc(p.email) + "'>" + esc(p.email) + "</a>" : (p.id && p.id !== p.name ? " <span class='bf-muted'>(" + esc(p.id) + ")</span>" : "")) +
            (p.first_failure ? " <em>first failing build</em>" : "") + "</span>";
        }).join("") + "</span></div>" : "") +
        (!started && !people.length ? "<div class='bf-contact-row bf-muted'>No commit or user information was recorded for these builds.</div>" : "") +
        "</div>" +
        "<div class='bf-alert-actions'>" +
        (people.some(function (p) { return p.email; }) ? "<a class='bf-btn bf-btn-sm bf-btn-primary' href='" + esc(mailtoFor(a)) + "'>" + ICON.mail + "Email</a>" : "") +
        (l.id ? "<button type='button' class='bf-btn bf-btn-sm' data-act='open'>Open entry</button>" : "") +
        (l.build_url ? "<a class='bf-btn bf-btn-sm' href='" + esc(l.build_url) + "'>Open build</a>" : "") +
        "<span class='bf-spacer'></span>" +
        (a.status === "dismissed" ? "<button type='button' class='bf-btn bf-btn-sm bf-btn-ghost' data-act='restore'>Restore</button>" :
          "<button type='button' class='bf-btn bf-btn-sm bf-btn-ghost' data-act='" + (a.read ? "unread" : "read") + "'>" + (a.read ? "Mark unread" : ICON.check + "Mark read") + "</button>" +
          "<button type='button' class='bf-btn bf-btn-sm bf-btn-ghost' data-act='dismiss'>Dismiss</button>") +
        "</div></article>";
      return html_;
    }
    function drawAlerts() {
      var c = al.counts, rules = al.rules, open = al.status === "open";
      var items = al.items.filter(function (a) { return !al.sev || a.severity === al.sev; });
      var sevChips = [["", "All", c.open]].concat(["critical", "high", "low"].map(function (k) { return [k, SEV[k][0], c[k] || 0]; }));
      alertsEl.innerHTML = "<aside class='bf-drawer bf-alerts' role='dialog' aria-label='Alerts'>" +
        "<div class='bf-alerts-top'><div><h2>Alerts</h2><p class='bf-muted'>Pipelines that keep failing. A green build removes the alert.</p></div>" +
        "<div class='bf-alerts-btns'>" + (open && c.unread ? "<button type='button' class='bf-btn bf-btn-sm bf-readall'>" + ICON.check + "Mark all read</button>" : "") +
        "<button type='button' class='bf-btn bf-btn-sm bf-btn-ghost bf-close'>Close</button></div></div>" +
        "<div class='bf-alert-tabs'><div class='bf-tabs'>" +
        "<button type='button' class='bf-tab' data-st='open' aria-selected='" + open + "'>Open<span class='bf-count'>" + (c.open || 0) + "</span></button>" +
        "<button type='button' class='bf-tab' data-st='dismissed' aria-selected='" + !open + "'>Dismissed<span class='bf-count'>" + (c.dismissed || 0) + "</span></button></div>" +
        (open ? "<div class='bf-sevchips'>" + sevChips.map(function (x) {
          return "<button type='button' class='bf-sevchip " + (x[0] ? SEV[x[0]][1] : "") + (al.sev === x[0] ? " bf-on" : "") + "' data-sev='" + x[0] + "'>" + esc(x[1]) + " <b>" + x[2] + "</b></button>";
        }).join("") + "</div>" : "") + "</div>" +
        "<p class='bf-rules'><span class='bf-sev bf-sev-critical'>Critical</span> production, more than " + rules.prod + " failed builds in a row " +
        "<span class='bf-sev bf-sev-high'>High</span> UAT or staging, more than " + rules.uat + " " +
        "<span class='bf-sev bf-sev-low'>Low</span> any other pipeline, more than " + rules.other + "</p>" +
        "<div class='bf-alert-list'>" + (items.length ? items.map(alertCard).join("") :
          "<div class='bf-empty'>" + ICON.check + "<b>" + (open ? (al.sev ? "No " + SEV[al.sev][0].toLowerCase() + " alerts" : "No pipeline is failing repeatedly") : "Nothing dismissed") + "</b><span class='bf-muted'>" +
          (open ? "You will be notified here, live, when one starts to." : "Dismissed alerts that are still failing show up here.") + "</span></div>") + "</div></aside>";
      var box = alertsEl.querySelector(".bf-alerts");
      box.querySelector(".bf-close").onclick = closeAlerts;
      var ra = box.querySelector(".bf-readall");
      if (ra) ra.onclick = function () { post("alertsRead", {all: true}).then(function () { loadAlerts(); }); };
      Array.prototype.forEach.call(box.querySelectorAll(".bf-alert-tabs .bf-tab"), function (t) {
        t.onclick = function () { al.status = t.dataset.st; al.sev = ""; loadAlerts(); };
      });
      Array.prototype.forEach.call(box.querySelectorAll(".bf-sevchip"), function (b) {
        b.onclick = function () { al.sev = b.dataset.sev; drawAlerts(); };
      });
      Array.prototype.forEach.call(box.querySelectorAll(".bf-alert"), function (card) {
        var a = al.items.filter(function (x) { return x.id === card.dataset.id; })[0];
        Array.prototype.forEach.call(card.querySelectorAll("[data-act]"), function (b) {
          b.onclick = function () {
            var act = b.dataset.act;
            if (act === "open") {
              post("alertsRead", {ids: [a.id]}).then(function () { loadAlerts(); });
              location.hash = a.latest.id;
              return;
            }
            card.classList.add("bf-leaving");
            var req = act === "read" ? post("alertsRead", {ids: [a.id]}) : act === "unread" ? post("alertsRead", {ids: [a.id], unread: true}) :
              act === "dismiss" ? post("alertDismiss", {id: a.id}) : post("alertRestore", {id: a.id});
            req.then(function (r) {
              if (!r.ok) { card.classList.remove("bf-leaving"); toast("Could not update the alert", r.j.error || "", "bad"); return; }
              if (act === "dismiss") toast("Alert dismissed", a.job + " (it stays in Dismissed until a green build)");
              loadAlerts();
            });
          };
        });
      });
    }
    function openAlerts() {
      alertsEl.hidden = false;
      al.status = "open"; al.sev = "";
      drawAlerts();
      loadAlerts();
      if (location.hash !== "#alerts") history.replaceState(null, "", location.pathname + location.search + "#alerts");
    }
    function closeAlerts() {
      alertsEl.hidden = true; alertsEl.innerHTML = "";
      if (location.hash === "#alerts") history.replaceState(null, "", location.pathname + location.search);
    }
    alertsEl.onclick = function (ev) { if (ev.target === alertsEl) closeAlerts(); };
    bell.onclick = function () { if (alertsEl.hidden) openAlerts(); else closeAlerts(); };
    loadAlerts();

    // ---------------------------------------------------------- views and routing
    function setView(v) {
      state.view = v;
      $$(".bf-tab").forEach(function (t) { t.setAttribute("aria-selected", String(t.dataset.view === v)); });
      $$("section[data-pane]").forEach(function (s) { s.hidden = s.dataset.pane !== v; });
      $(".bf-export").innerHTML = ICON.download + "Export";
      if (v === "deck") { prevCards = null; loadDeck(); } else { refresh(); }
    }
    $$(".bf-tab").forEach(function (t) {
      t.onclick = function () { location.hash = t.dataset.view === "deck" ? "deck" : "failures"; };
    });
    function route() {
      var h = location.hash.slice(1);
      if (h === "alerts") { if (alertsEl.hidden) openAlerts(); return; }
      if (/^SD-/.test(h)) { if (state.view !== "deck") setView("deck"); showCard(h); return; }
      if (/^BF-/.test(h)) { if (state.view !== "failures") setView("failures"); showIncident(h); return; }
      if (!drawer.hidden) closeDrawer();
      if (h === "deck" && state.view !== "deck") setView("deck");
      else if (h !== "deck" && state.view !== "failures") setView("failures");
    }
    window.addEventListener("hashchange", route);

    setLive(state.live ? "on" : "paused");
    drawChips();
    refresh();
    loadDeck(true);  // fills the count on the tab
    loadPipelines(false);
    pulse();
    if (location.hash.length > 1) route();
  }

  BF.mountAdmin = mountAdmin;
  BF.auto();
})();
