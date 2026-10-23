/* Build Forge: the panel on failed builds (analysis under the failed stage + Rebuild button) and the helpers the
 * dashboard (buildforge-dashboard.js) shares. No chat: the panel only shows the analysis.
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
    unknown: ["Unknown", "muted"], excluded: ["Not analysed (excluded)", "muted"], not_analysed: ["Not analysed", "bad"],
    pending: ["Analysing", "info"]
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
      if (v.status === "skipped") {
        html += "<p class='bf-muted'>This pipeline is excluded from analysis, so Kiro has not looked at this failure.</p>" +
          (d.bfAdmin ? "<p class='bf-muted'>Open it in the Build Forge dashboard and use <em>Analyse</em> to ask anyway.</p>" : "") + "</div>";
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
      if (a.source === "deck") {
        html += "<div class='bf-deckline'><strong>From the solutions deck.</strong> " + esc(a.deck_note || "") +
          (d.bfAdmin ? " <a href='" + esc(d.bfAdmin) + "#" + esc(a.deck_id) + "'>Open card " + esc(a.deck_id) + "</a>" : "") +
          (a.kiro_error ? "<br><span class='bf-muted'>Kiro was not available: " + esc(a.kiro_error) + "</span>" : "") + "</div>";
      }
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

  function auto() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-bf-analysis],[data-bf-base]"), function (el) {
      if (el.dataset.bfMounted) return;
      el.dataset.bfMounted = "1";
      if (el.dataset.bfBase) {
        if (window.BuildForge.mountAdmin) { window.BuildForge.mountAdmin(el); } else { delete el.dataset.bfMounted; }
      } else mountPanel(el);
    });
  }
  window.BuildForge = {md: md, esc: esc, badge: badge, getJson: getJson, crumbHeaders: crumbHeaders, ago: ago, dur: dur,
    CATEGORY: CATEGORY, categoryBadge: categoryBadge, outcome: outcome, mountPanel: mountPanel, mountAdmin: null, auto: auto};
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
