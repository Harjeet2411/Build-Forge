/* Build Forge: the panel on failed builds (analysis under the failed stage + Rebuild button) and the /buildforge/admin
 * page (stats, failed builds, incident logs). No chat: the panel only shows the Kiro agent's analysis.
 * Mounted from data attributes, no inline script (works with Jenkins' Content Security Policy).
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])_([^_]+)_(?=[\s).,]|$)/g, "$1<em>$2</em>")
      .replace(/(^|[\s(|])(https?:\/\/[^\s<|)]+)/g, "$1<a href='$2' target='_blank' rel='noopener noreferrer'>$2</a>");
  }

  // Small, safe Markdown: fences, headings, tables, ordered/bullet lists, paragraphs, inline code/bold/links.
  function md(text) {
    var parts = String(text || "").split(/```[a-zA-Z0-9_-]*\n?/);
    var out = "";
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) { out += "<pre class='bf-pre'>" + esc(parts[i].replace(/\n$/, "")) + "</pre>"; continue; }
      var lines = parts[i].split("\n"), list = null, para = [], table = [];
      var flush = function () {
        if (para.length) { out += "<p>" + para.join("<br>") + "</p>"; para = []; }
        if (table.length) {
          out += "<table class='bf-mdtable'>" + table.map(function (row) {
            return "<tr>" + row.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") + "</table>";
          table = [];
        }
      };
      var closeList = function () { if (list) { out += "</" + list + ">"; list = null; } };
      for (var j = 0; j < lines.length; j++) {
        var raw = lines[j];
        var h = /^(#{1,4})\s+(.*)$/.exec(raw);
        if (h) { flush(); closeList(); var n = Math.min(h[1].length + 2, 6); out += "<h" + n + " class='bf-h'>" + inline(h[2]) + "</h" + n + ">"; continue; }
        if (/^\s*\|.*\|\s*$/.test(raw)) {
          closeList();
          if (para.length) { out += "<p>" + para.join("<br>") + "</p>"; para = []; }
          if (/^\s*\|[\s:|-]+\|\s*$/.test(raw)) continue;  // separator row
          var cells = raw.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
          if (cells.join("") !== "") table.push(cells);
          continue;
        }
        var ol = /^\s*\d+[.)]\s+(.*)$/.exec(raw), ul = /^\s*[-*]\s+(.*)$/.exec(raw);
        if (ol || ul) {
          flush();
          var kind = ol ? "ol" : "ul";
          if (list !== kind) { closeList(); out += "<" + kind + ">"; list = kind; }
          out += "<li>" + inline((ol || ul)[1]) + "</li>";
          continue;
        }
        if (list && /^\s{2,}\S/.test(raw)) { out = out.replace(/<\/li>$/, "<br>" + inline(raw.trim()) + "</li>"); continue; }
        closeList();
        if (/^\s*$/.test(raw)) { flush(); } else { if (table.length) flush(); para.push(inline(raw)); }
      }
      flush();
      closeList();
    }
    return out;
  }

  function badge(text, kind, title) {
    return "<span class='bf-badge bf-" + kind + "'" + (title ? " title='" + esc(title) + "'" : "") + ">" + esc(text) + "</span>";
  }

  function crumbHeaders(el) {
    var h = {}, head = (document.head && document.head.dataset) || {};
    var name = el.dataset.crumbHeader || head.crumbHeader, value = el.dataset.crumbValue || head.crumbValue;
    if (name && value) h[name] = value;
    return h;
  }

  function getJson(url, headers) {
    return fetch(url, {headers: headers || {}, credentials: "same-origin"}).then(function (r) {
      return r.text().then(function (t) {
        var j;
        try { j = JSON.parse(t); } catch (e) { j = {error: "HTTP " + r.status}; }
        return {ok: r.ok, status: r.status, j: j};
      });
    });
  }

  function ago(ts) {
    if (!ts) return "";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }

  function dur(s) {
    if (s == null) return "-";
    s = Math.round(s);
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 172800) return (s / 3600).toFixed(1) + " h";
    return Math.round(s / 86400) + " days";
  }

  var CATEGORY = {
    application: ["Application", "warn"], infrastructure: ["Infrastructure", "bad"], configuration: ["Configuration", "warn"],
    dependency: ["Dependency", "warn"], test_failure: ["Test failure", "warn"], transient: ["Transient", "info"],
    unknown: ["Unknown", "muted"]
  };
  function categoryBadge(c) { var x = CATEGORY[c] || [c || "pending", "muted"]; return badge(x[0], x[1]); }

  function outcome(i) {
    if (i.next_result === "SUCCESS") return badge(i.next_is_rebuild ? "Fixed next build (rebuild)" : "Fixed next build", "good");
    if (i.fixed_build) return badge("Fixed later #" + i.fixed_build, "info");
    if (i.next_build) return badge("Still failing", "bad", "Next build #" + i.next_build + " was " + i.next_result);
    return badge("Waiting for next build", "muted");
  }

  // ---------------------------------------------------------------- build panel
  function mountPanel(root) {
    var d = root.dataset, headers = function () { return crumbHeaders(root); };
    var started = Date.now(), stageLink = null;
    root.classList.add("bf-root");
    root.innerHTML =
      "<div class='bf-head'>" + (d.bfInSummary === "true" ? "" : "<img class='bf-logo' alt='' src='" + esc(d.bfLogo) + "'>") +
      "<div><div class='bf-name'>Build Forge</div><div class='bf-credit'>Plugin developed by Harjeet L</div></div>" +
      "<span class='bf-right'></span></div><div class='bf-body'><p class='bf-muted'>Loading…</p></div>";
    var body = root.querySelector(".bf-body"), right = root.querySelector(".bf-right");

    function waiting(text) {
      body.innerHTML = "<div class='bf-wait'><span class='bf-spin'></span>" + esc(text) + "</div>";
    }

    function stageStrip(stages) {
      if (!stages || !stages.length) return "";
      return "<div class='bf-stages'>" + stages.map(function (s, n) {
        var icon = {passed: "✓", failed: "✕", skipped: "–"}[s.status] || "·";
        return (n ? "<span class='bf-arrow'></span>" : "") +
          "<span class='bf-stage bf-st-" + esc(s.status) + "'><i>" + icon + "</i>" + esc(s.name) + "</span>";
      }).join("") + "</div>";
    }

    function render(v) {
      var a = v.analysis || {};
      right.innerHTML = (v.id ? (d.bfAdmin ? "<a class='bf-id' href='" + esc(d.bfAdmin) + "#" + esc(v.id) + "' title='Open this entry in Build Forge admin'>" + esc(v.id) + "</a>"
        : "<span class='bf-id'>" + esc(v.id) + "</span>") : "");
      var html = stageStrip(v.stages);
      html += "<div class='bf-card'><span class='bf-caret'></span>";
      html += "<div class='bf-where'>" + (v.stage ? "Failed in stage <strong>" + esc(v.stage) + "</strong>" : "<strong>Build failed</strong>") +
        (v.node ? " <span class='bf-muted'>on " + esc(v.node) + "</span>" : "") +
        "<span class='bf-links'><a class='bf-stage-link' href='" + esc(d.bfRun) + "console'>Console</a></span></div>";
      if (v.status === "queued" || v.status === "analysing") {
        html += "<div class='bf-wait'><span class='bf-spin'></span>Kiro is reading the error of this stage…</div></div>";
        body.innerHTML = html;
        place();
        return;
      }
      if (v.status === "error") {
        html += "<p class='bf-muted'>Kiro could not analyse this failure: " + esc(v.error) + "</p>" +
          (d.bfAdmin ? "<p class='bf-muted'>Fix the runner, then use <em>Analyse again</em> on the admin page.</p>" : "") + "</div>";
        body.innerHTML = html;
        place();
        return;
      }
      html += "<div class='bf-sec'>Error observed</div><div class='bf-text'>" + md(a.error_observed) + "</div>";
      html += "<div class='bf-sec'>Proposed solution</div><div class='bf-text'>" + md(a.proposed_solution) + "</div>";
      var rec = v.recurrence || {};
      html += "<div class='bf-badges'>" + categoryBadge(a.category) +
        (a.severity ? badge(a.severity + " severity", a.severity === "high" ? "bad" : a.severity === "medium" ? "warn" : "muted") : "") +
        (a.confidence != null ? badge(Math.round(a.confidence * 100) + "% sure", "muted") : "") +
        (a.signature ? badge(a.signature, "sig", "Error signature") : "") +
        (rec.in_job ? badge("Seen " + rec.in_job + "× before here", "warn") : "") +
        (v.rebuild_of ? badge("Rebuild of #" + v.rebuild_of, "info") : "") + "</div>";
      var pending = (v.rebuilds || []).filter(function (r) { return !r.new_result; })[0];
      if (a.rebuild_can_fix && v.fixed_build) {
        html += "<p class='bf-norebuild'>Kiro said a rebuild could fix this; a later build (#" + v.fixed_build + ") has already passed.</p>";
      } else if (a.rebuild_can_fix && pending) {
        html += "<div class='bf-rebuild'><div><strong>Rebuild in progress.</strong> Requested by " + esc(pending.requested_by || "?") + " " +
          esc(ago(pending.requested_at)) + (pending.new_build ? ", running as #" + pending.new_build : ", waiting in the queue") + ".</div></div>";
      } else if (a.rebuild_can_fix) {
        html += "<div class='bf-rebuild'><div><strong>A rebuild with the same parameters should fix this.</strong> " +
          esc(a.rebuild_reason || "") + "</div>" +
          (d.bfCanRebuild === "true" ? "<button type='button' class='jenkins-button jenkins-button--primary bf-rb'>Rebuild</button>"
            : "<span class='bf-muted'>You need Build permission on this job to rebuild it.</span>") + "</div>";
      } else if (a.rebuild_reason) {
        html += "<p class='bf-norebuild'>A rebuild alone won't fix this: " + esc(a.rebuild_reason) + "</p>";
      }
      var rb = (v.rebuilds || []).map(function (r) {
        return "Rebuild by " + esc(r.requested_by || "?") + " " + esc(ago(r.requested_at)) +
          (r.new_build ? " → #" + r.new_build + (r.new_result ? " " + esc(r.new_result) : " (running)") : " (queued)");
      });
      if (v.fixed_build) rb.push("Fixed in #" + v.fixed_build);
      if (rb.length) html += "<p class='bf-muted bf-history'>" + rb.join(" · ") + "</p>";
      html += "<div class='bf-foot'>Analysed by " + esc(v.analysed_by || "Kiro") + (v.seconds ? " in " + dur(v.seconds) : "") + "</div></div>";
      body.innerHTML = html;
      var btn = body.querySelector(".bf-rb");
      if (btn) btn.onclick = function () { rebuild(btn); };
      place();
    }

    // Point the card's caret at the failed stage, and link the stage in the pipeline graph when Jenkins has one.
    function place() {
      var failed = body.querySelector(".bf-st-failed"), caret = body.querySelector(".bf-caret"), strip = body.querySelector(".bf-stages");
      if (failed && caret && strip) {
        caret.style.left = Math.max(12, failed.offsetLeft + failed.offsetWidth / 2 - 8) + "px";  // the strip is the offset parent
        caret.style.display = "block";
        caret.parentNode.classList.add("bf-pointed");
      }
      if (stageLink) addStageLink(stageLink);
    }

    function addStageLink(href) {
      var links = body.querySelector(".bf-links");
      if (links && !links.querySelector(".bf-graph")) {
        links.insertAdjacentHTML("afterbegin", "<a class='bf-graph' href='" + esc(href) + "'>Open stage</a>");
      }
    }

    function findStage(nodes, name) {
      for (var i = 0; nodes && i < nodes.length; i++) {
        if (nodes[i].name === name) return nodes[i];
        var c = findStage(nodes[i].children, name);
        if (c) return c;
      }
      return null;
    }

    function resolveStage(name) {
      if (!name) return;
      var run = d.bfRun;
      // Pipeline Graph View (its tree API names the URL to use), then Pipeline Stage View's REST API.
      var trees = ["stages", "pipeline-overview", "pipeline-graph"];
      (function next(k) {
        if (k < trees.length) {
          getJson(run + trees[k] + "/tree").then(function (r) {
            var st = r.ok && r.j && r.j.data ? findStage(r.j.data.stages, name) : null;
            if (st && st.id != null) { stageLink = run + trees[k] + "/?selected-node=" + encodeURIComponent(st.id); addStageLink(stageLink); }
            else next(k + 1);
          }).catch(function () { next(k + 1); });
          return;
        }
        getJson(run + "wfapi/describe").then(function (r) {
          var st = r.ok && r.j ? findStage(r.j.stages, name) : null;
          if (st && st.id != null) { stageLink = run + "execution/node/" + encodeURIComponent(st.id) + "/"; addStageLink(stageLink); }
        }).catch(function () {});
      })(0);
    }

    function rebuild(btn) {
      if (!confirm("Start this job again with the same parameters?")) return;
      btn.disabled = true;
      btn.textContent = "Queuing…";
      fetch(d.bfRebuild, {method: "POST", headers: headers(), credentials: "same-origin"})
        .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
        .then(function (res) {
          var note = document.createElement("p");
          note.className = res.ok ? "bf-ok" : "bf-err";
          note.textContent = res.j.message || res.j.error || "Done.";
          btn.parentNode.appendChild(note);
          btn.textContent = res.ok ? "Rebuild queued" : "Rebuild";
          btn.disabled = res.ok;
          if (res.ok) setTimeout(load, 8000);
        })
        .catch(function (e) { btn.disabled = false; btn.textContent = "Rebuild"; alert("Rebuild failed: " + e); });
    }

    var resolved = false;
    function load() {
      getJson(d.bfAnalysis).then(function (res) {
        if (res.status === 404) {
          if (Date.now() - started < 120000) { waiting("Waiting for Build Forge to receive this build…"); setTimeout(load, 4000); }
          else body.innerHTML = "<p class='bf-muted'>Build Forge has no record of this build (it failed before Build Forge was installed, or the job is filtered out).</p>";
          return;
        }
        if (!res.ok) { body.innerHTML = "<p class='bf-muted'>" + esc(res.j.error || "Build Forge is unavailable.") + "</p>"; return; }
        render(res.j);
        if (!resolved && res.j.stage) { resolved = true; resolveStage(res.j.stage); }
        var busy = res.j.status === "queued" || res.j.status === "analysing";
        var rebuilding = (res.j.rebuilds || []).some(function (r) { return !r.new_result; });
        if ((busy && Date.now() - started < 900000) || (rebuilding && Date.now() - started < 1800000)) setTimeout(load, busy ? 4000 : 15000);
      }).catch(function (e) { body.innerHTML = "<p class='bf-muted'>Build Forge is unreachable: " + esc(e) + "</p>"; });
    }
    window.addEventListener("resize", place);
    load();
  }

  // ---------------------------------------------------------------- admin page
  function mountAdmin(root) {
    var d = root.dataset, base = d.bfBase, state = {days: 30, q: "", category: "", outcome: "", offset: 0, limit: 25};
    try { var saved = JSON.parse(localStorage.getItem("buildforge.admin") || "{}"); if (saved.days != null) state.days = saved.days; } catch (e) { /* private window */ }
    root.classList.add("bf-root", "bf-admin-root");
    root.innerHTML =
      "<div class='bf-head bf-admin-head'><img class='bf-logo bf-logo-lg' alt='' src='" + esc(d.bfLogo) + "'>" +
      "<div><div class='bf-name bf-name-lg'>Build Forge</div><div class='bf-tag'>Mending broken pipelines · every failure, its fix and what happened next</div>" +
      "<div class='bf-credit'>Plugin developed by Harjeet L</div></div>" +
      "<span class='bf-right'><label class='bf-muted'>Period <select class='bf-days bf-select'>" +
      "<option value='7'>7 days</option><option value='30'>30 days</option><option value='90'>90 days</option><option value='365'>1 year</option><option value='0'>All time</option>" +
      "</select></label></span></div>" +
      (d.bfActive === "true" ? "" : "<p class='bf-err'>Build Forge is turned off or has no service URL (Manage Jenkins › System › Build Forge).</p>") +
      "<div class='bf-cards'></div><div class='bf-grid'><div class='bf-box bf-chart'></div><div class='bf-box bf-cats'></div></div>" +
      "<div class='bf-grid'><div class='bf-box bf-sigs'></div><div class='bf-box bf-jobs'></div></div>" +
      "<div class='bf-box'><div class='bf-box-head'><h3>Failed builds</h3><span class='bf-filters'>" +
      "<input class='bf-q bf-input' placeholder='Search entry, pipeline, stage, signature'>" +
      "<select class='bf-cat bf-select'><option value=''>All categories</option>" +
      Object.keys(CATEGORY).map(function (c) { return "<option value='" + c + "'>" + CATEGORY[c][0] + "</option>"; }).join("") + "</select>" +
      "<select class='bf-out bf-select'><option value=''>Any outcome</option><option value='fixed-next'>Fixed on the next build</option>" +
      "<option value='fixed-later'>Fixed later</option><option value='open'>Still failing</option><option value='rebuilt'>Rebuilt via Build Forge</option></select>" +
      "</span></div><div class='bf-table'></div><div class='bf-pager'></div></div>" +
      "<div class='bf-detail' hidden></div>";
    var $ = function (s) { return root.querySelector(s); };
    $(".bf-days").value = String(state.days);

    function card(label, value, sub, kind) {
      return "<div class='bf-stat" + (kind ? " bf-stat-" + kind : "") + "'><div class='bf-stat-v'>" + esc(value) + "</div>" +
        "<div class='bf-stat-l'>" + esc(label) + "</div>" + (sub ? "<div class='bf-stat-s'>" + sub + "</div>" : "") + "</div>";
    }
    function pct(a, b) { return b ? Math.round(100 * a / b) + "%" : "-"; }

    function loadStats() {
      getJson(base + "stats?days=" + state.days).then(function (res) {
        if (!res.ok) { $(".bf-cards").innerHTML = "<p class='bf-err'>" + esc(res.j.error) + "</p>"; return; }
        var s = res.j;
        $(".bf-cards").innerHTML =
          card("Failed builds", s.failed_builds, esc(s.failed_pipelines) + " pipelines") +
          card("Fixed on the next build", s.fixed_next_build, pct(s.fixed_next_build, s.failed_builds) + " of failures · " + esc(s.pipelines_fixed_next_build) + " pipelines", "good") +
          card("Fixed later", s.fixed_later, pct(s.fixed_later, s.failed_builds) + " of failures") +
          card("Still failing", s.still_failing, "no green build since", s.still_failing ? "bad" : "") +
          card("Rebuilds via Build Forge", s.rebuilds_requested, esc(s.rebuilds_succeeded) + " went green · " + esc(s.rebuilds_failed) + " failed again") +
          card("Rebuild suggested", s.rebuild_suggested, "by the Kiro agent") +
          card("Recurring errors", s.recurring, "same error seen more than once") +
          card("Analysis time", dur(s.avg_analysis_seconds), "average · " + esc(s.analysis_errors) + " not analysed") +
          card("Time to fix", dur(s.median_time_to_fix_seconds), "median, failure to green");
        var max = Math.max.apply(null, [1].concat((s.daily || []).map(function (x) { return x.failed; })));
        $(".bf-chart").innerHTML = "<h3>Failures per day <span class='bf-legend'><i class='bf-dot-f'></i>failed <i class='bf-dot-g'></i>fixed next build</span></h3>" +
          ((s.daily || []).length ? "<div class='bf-bars'>" + s.daily.slice(-60).map(function (x) {
            return "<div class='bf-bar' title='" + esc(x.day + ": " + x.failed + " failed, " + (x.fixed_next || 0) + " fixed on the next build") + "'>" +
              "<span class='bf-bar-f' style='height:" + (100 * x.failed / max) + "%'><span class='bf-bar-g' style='height:" + (x.failed ? 100 * (x.fixed_next || 0) / x.failed : 0) + "%'></span></span></div>";
          }).join("") + "</div>" : "<p class='bf-muted'>No failures in this period.</p>");
        var cats = s.by_category || {}, total = s.failed_builds || 1;
        $(".bf-cats").innerHTML = "<h3>By category</h3>" + (Object.keys(cats).length ? Object.keys(cats).map(function (c) {
          return "<div class='bf-hbar'><span>" + categoryBadge(c === "pending" ? null : c) + "</span><span class='bf-hbar-track'><span style='width:" + (100 * cats[c] / total) + "%'></span></span><b>" + cats[c] + "</b></div>";
        }).join("") : "<p class='bf-muted'>Nothing yet.</p>");
        $(".bf-sigs").innerHTML = "<h3>Most frequent errors</h3>" + ((s.top_signatures || []).length ?
          "<table class='bf-t'><tr><th>Signature</th><th>Times</th><th>Pipelines</th><th>Fixed</th><th>Last</th></tr>" + s.top_signatures.map(function (x) {
            return "<tr><td><a href='#" + esc(x.last_id) + "' class='bf-sig-q' data-q='" + esc(x.signature) + "'>" + esc(x.signature) + "</a></td><td>" + x.count + "</td><td>" + x.pipelines + "</td><td>" + x.fixed + "</td><td>" + esc(ago(x.last_seen)) + "</td></tr>";
          }).join("") + "</table>" : "<p class='bf-muted'>Nothing yet.</p>");
        $(".bf-jobs").innerHTML = "<h3>Pipelines that fail most</h3>" + ((s.top_pipelines || []).length ?
          "<table class='bf-t'><tr><th>Pipeline</th><th>Failed</th><th>Fixed next build</th><th>Still failing</th></tr>" + s.top_pipelines.map(function (x) {
            return "<tr><td><a href='#' class='bf-job-q' data-q='" + esc(x.job) + "'>" + esc(x.job) + "</a></td><td>" + x.failed + "</td><td>" + x.fixed_next + "</td><td>" + x.open + "</td></tr>";
          }).join("") + "</table>" : "<p class='bf-muted'>Nothing yet.</p>");
        Array.prototype.forEach.call(root.querySelectorAll(".bf-sig-q,.bf-job-q"), function (a) {
          a.onclick = function (ev) { ev.preventDefault(); state.q = a.dataset.q; $(".bf-q").value = state.q; state.offset = 0; loadList(); };
        });
      });
    }

    function loadList() {
      var url = base + "incidents?days=" + state.days + "&limit=" + state.limit + "&offset=" + state.offset +
        "&q=" + encodeURIComponent(state.q) + "&category=" + state.category + "&outcome=" + state.outcome;
      getJson(url).then(function (res) {
        if (!res.ok) { $(".bf-table").innerHTML = "<p class='bf-err'>" + esc(res.j.error) + "</p>"; return; }
        var items = res.j.items || [];
        $(".bf-table").innerHTML = items.length ? "<table class='bf-t bf-list'><tr><th>Entry</th><th>Pipeline</th><th>Stage</th><th>Category</th><th>Error observed</th><th>Rebuild?</th><th>Outcome</th><th>When</th></tr>" +
          items.map(function (i) {
            var a = i.analysis || {};
            return "<tr data-id='" + esc(i.id) + "'><td><a href='#" + esc(i.id) + "' class='bf-id'>" + esc(i.id) + "</a></td>" +
              "<td><a href='" + esc(i.build_url || "#") + "'>" + esc(i.job) + " #" + i.build + "</a></td><td>" + esc(i.stage || "-") + "</td>" +
              "<td>" + (i.status === "done" ? categoryBadge(a.category) : i.status === "error" ? badge("not analysed", "bad", i.error) : badge(i.status + "…", "info")) + "</td>" +
              "<td class='bf-clip'>" + esc((a.error_observed || i.error || "").replace(/[`*]/g, "")) + "</td>" +
              "<td>" + (i.status !== "done" ? "" : i.rebuild_can_fix ? badge("yes", "good") : badge("no", "muted")) + "</td>" +
              "<td>" + outcome(i) + "</td><td class='bf-muted' title='" + esc(new Date(i.created * 1000).toLocaleString()) + "'>" + esc(ago(i.created)) + "</td></tr>";
          }).join("") + "</table>" : "<p class='bf-muted'>No failed builds match.</p>";
        var total = res.j.total || 0, from = total ? state.offset + 1 : 0, to = Math.min(total, state.offset + items.length);
        $(".bf-pager").innerHTML = "<span class='bf-muted'>" + from + "–" + to + " of " + total + "</span>" +
          "<button type='button' class='jenkins-button bf-prev'" + (state.offset ? "" : " disabled") + ">Newer</button>" +
          "<button type='button' class='jenkins-button bf-next'" + (to < total ? "" : " disabled") + ">Older</button>";
        $(".bf-prev").onclick = function () { state.offset = Math.max(0, state.offset - state.limit); loadList(); };
        $(".bf-next").onclick = function () { state.offset += state.limit; loadList(); };
        Array.prototype.forEach.call(root.querySelectorAll(".bf-list tr[data-id]"), function (tr) {
          tr.onclick = function (ev) { if (ev.target.tagName === "A" && !ev.target.classList.contains("bf-id")) return; ev.preventDefault(); location.hash = tr.dataset.id; };
        });
      });
    }

    function showDetail(id) {
      var box = $(".bf-detail");
      if (!id) { box.hidden = true; return; }
      box.hidden = false;
      box.innerHTML = "<div class='bf-detail-inner'><p class='bf-muted'>Loading " + esc(id) + "…</p></div>";
      getJson(base + "incident?id=" + encodeURIComponent(id)).then(function (res) {
        var i = res.j, inner = box.querySelector(".bf-detail-inner");
        if (!res.ok) { inner.innerHTML = "<button type='button' class='bf-close jenkins-button'>Close</button><p class='bf-err'>" + esc(i.error) + "</p>"; bindClose(); return; }
        inner.innerHTML = "<div class='bf-detail-bar'><a class='jenkins-button' href='" + esc(i.build_url || "#") + "'>Open build</a>" +
          "<button type='button' class='jenkins-button bf-re'>Analyse again</button>" +
          "<button type='button' class='jenkins-button bf-dl'>Download log</button>" +
          "<button type='button' class='jenkins-button bf-close'>Close</button></div>" +
          "<div class='bf-muted bf-file'>Log file on the Jenkins server: <code>" + esc(i.log_file) + "</code></div>" +
          "<div class='bf-doc'>" + (i.log_text ? md(i.log_text) : "<p class='bf-muted'>No log file yet (analysis " + esc(i.status) + ").</p>") + "</div>" +
          ((i.related || []).length ? "<h3>Same error elsewhere</h3><ul>" + i.related.map(function (r) {
            return "<li><a href='#" + esc(r.id) + "'>" + esc(r.id) + "</a> " + esc(r.job) + " #" + r.build + " · " + esc(ago(r.created)) + (r.next_result ? " · next build " + esc(r.next_result) : "") + "</li>";
          }).join("") + "</ul>" : "");
        bindClose();
        inner.querySelector(".bf-dl").onclick = function () {
          var a = document.createElement("a");
          a.href = URL.createObjectURL(new Blob([i.log_text || ""], {type: "text/markdown"}));
          a.download = i.id + ".md";
          a.click();
        };
        inner.querySelector(".bf-re").onclick = function (ev) {
          ev.target.disabled = true;
          fetch(base + "reanalyze?id=" + encodeURIComponent(i.id), {method: "POST", headers: crumbHeaders(root), credentials: "same-origin"})
            .then(function (r) { return r.json(); })
            .then(function (j) { ev.target.textContent = j.error ? j.error : "Queued, refresh in a minute"; loadList(); });
        };
      });
      function bindClose() {
        box.querySelector(".bf-close").onclick = function () { history.replaceState(null, "", location.pathname + location.search); box.hidden = true; };
      }
    }

    $(".bf-days").onchange = function () {
      state.days = +this.value; state.offset = 0;
      try { localStorage.setItem("buildforge.admin", JSON.stringify({days: state.days})); } catch (e) { /* ignore */ }
      loadStats(); loadList();
    };
    var t;
    $(".bf-q").oninput = function () { var v = this.value; clearTimeout(t); t = setTimeout(function () { state.q = v.trim(); state.offset = 0; loadList(); }, 300); };
    $(".bf-cat").onchange = function () { state.category = this.value; state.offset = 0; loadList(); };
    $(".bf-out").onchange = function () { state.outcome = this.value; state.offset = 0; loadList(); };
    $(".bf-detail").onclick = function (ev) { if (ev.target === this) { history.replaceState(null, "", location.pathname + location.search); this.hidden = true; } };
    window.addEventListener("hashchange", function () { showDetail(location.hash.slice(1)); });
    loadStats();
    loadList();
    if (location.hash.length > 1) showDetail(location.hash.slice(1));
    setInterval(function () { if (!document.hidden) { loadStats(); loadList(); } }, 60000);
  }

  function auto() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-bf-analysis],[data-bf-base]"), function (el) {
      if (el.dataset.bfMounted) return;
      el.dataset.bfMounted = "1";
      if (el.dataset.bfBase) mountAdmin(el); else mountPanel(el);
    });
  }
  window.BuildForge = {md: md, mountPanel: mountPanel, mountAdmin: mountAdmin};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
