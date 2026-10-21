"use strict";
(function () {
  var csrfMeta = document.querySelector('meta[name="csrf"]');
  var CSRF = csrfMeta ? csrfMeta.content : "";
  var pd = document.getElementById("page-data");
  var PAGE = pd ? JSON.parse(pd.textContent) : {};
  var viewer = document.body.classList.contains("viewer");

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  // ---- Add project dialog
  var addForm = document.querySelector("[data-addform]");
  if (addForm) {
    var addBtn = addForm.querySelector("[data-add]");
    var addErr = addForm.querySelector(".err");
    addBtn.addEventListener("click", function () {
      addErr.hidden = true;
      addBtn.disabled = true;
      post("/api/projects", { path: addForm.path.value.trim(), name: addForm.name.value.trim() })
        .then(function () { location.reload(); })
        .catch(function (e) { addErr.textContent = e.message; addErr.hidden = false; addBtn.disabled = false; });
    });
  }

  function mins(s) { return typeof s === "number" ? Math.max(1, Math.round(s / 60)) + " min" : "?"; }

  // ---- Run… dialog
  document.querySelectorAll("[data-open]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var dlg = document.getElementById(btn.getAttribute("data-open"));
      if (dlg && dlg.showModal) dlg.showModal();
    });
  });
  document.querySelectorAll("dialog.modal").forEach(function (dlg) {
    var project = dlg.getAttribute("data-project");
    if (!project) {
      dlg.querySelector("[data-close]").addEventListener("click", function () { dlg.close(); });
      return;
    }
    var start = dlg.querySelector("[data-start]");
    var est = dlg.querySelector(".estline");
    var err = dlg.querySelector(".err");
    var seq = 0;
    function suites() {
      return Array.prototype.map.call(dlg.querySelectorAll('input[name="suite"]:checked'), function (i) { return i.value; });
    }
    function mode() { var m = dlg.querySelector('input[name="mode"]:checked'); return m ? m.value : "scripted"; }
    function refresh() {
      var s = suites();
      err.hidden = true;
      start.disabled = !s.length;
      if (!s.length) { est.textContent = "Pick at least one suite."; return; }
      var my = ++seq;
      est.textContent = "Estimating…";
      fetch("/api/plan?project=" + encodeURIComponent(project) + "&suite=" + encodeURIComponent(s.join(",")), { credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); })
        .then(function (p) {
          if (my !== seq) return;
          var bits = ["Estimate: about " + mins(p.estimate_s)];
          if (p.flows != null) bits.push(p.flows + " flows");
          if (p.sims != null) bits.push(p.sims + (p.sims === 1 ? " simulator" : " simulators"));
          var nb = p.needs_build != null ? p.needs_build : p.build;
          if (nb != null) bits.push(nb ? "1 build" : "build cached");
          est.textContent = bits.join(" · ");
        })
        .catch(function (e) { if (my === seq) est.textContent = "No estimate: " + e.message; });
    }
    dlg.addEventListener("change", refresh);
    dlg.querySelector("[data-close]").addEventListener("click", function () { dlg.close(); });
    start.addEventListener("click", function () {
      var s = suites();
      if (!s.length) return;
      if (!window.confirm("Start " + project + " run with " + s.join(", ") + "?\nIt builds the app and boots test simulators on the Mac.")) return;
      start.disabled = true;
      post("/api/runs", { project: project, suites: s, mode: mode(), confirm: true })
        .then(function (j) { location.href = j.url; })
        .catch(function (e) { err.textContent = e.message; err.hidden = false; start.disabled = false; });
    });
  });

  // ---- Stop run (delegated: the button lives inside the SSE-refreshed fragment)
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest("[data-stop]");
    if (!b) return;
    var id = b.getAttribute("data-stop");
    if (!window.confirm("Stop run " + id + "? Its simulators are shut down and deleted.")) return;
    b.disabled = true;
    post("/api/runs/" + encodeURIComponent(id) + "/stop", { confirm: true })
      .then(function () { b.textContent = "Stopping…"; })
      .catch(function (e) { alert(e.message); b.disabled = false; });
  });

  // ---- Triage
  document.querySelectorAll("[data-triage]").forEach(function (b) {
    b.addEventListener("click", function () {
      var mark = b.getAttribute("data-triage");
      var box = b.closest(".fc");
      post("/api/triage", { run: b.getAttribute("data-run"), flow: b.getAttribute("data-flow"), mark: mark })
        .then(function (j) { box.querySelector(".mark").textContent = "Marked " + j.mark + " by " + j.by + ", just now"; })
        .catch(function (e) { box.querySelector(".mark").textContent = "Error: " + e.message; });
    });
  });

  // ---- Copy for Claude
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var ta = document.getElementById(b.getAttribute("data-copy"));
      var text = ta.value;
      function fallback() {
        // No clipboard API (plain http behind a tunnel): show the text selected for ⌘C.
        ta.hidden = false;
        ta.focus();
        ta.select();
        var ok = false;
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        b.textContent = ok ? "Copied" : "Press ⌘C";
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { b.textContent = "Copied"; }, fallback);
      } else {
        fallback();
      }
      setTimeout(function () { b.textContent = "Copy for Claude"; }, 2500);
    });
  });

  // ---- Live run: SSE for events + state, fragment refetch for the tiles and flow list
  if (PAGE.page === "live" && window.EventSource) {
    var run = PAGE.run;
    var live = document.getElementById("live");
    var log = document.getElementById("log");
    var lastFetch = 0, pending = null, tick = 0;
    function refetch() {
      var wait = 1000 - (Date.now() - lastFetch);
      if (wait > 0) { if (!pending) pending = setTimeout(function () { pending = null; refetch(); }, wait); return; }
      lastFetch = Date.now();
      fetch("/runs/" + encodeURIComponent(run) + "/fragment", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) { if (html != null) { live.innerHTML = html; paintSims(); } });
    }
    function paintSims() {
      live.querySelectorAll("img[data-live]").forEach(function (img) {
        var url = img.getAttribute("data-live") + "?t=" + tick;
        var pre = new Image();
        // Swap only after the new frame loads, so tiles never flash empty.
        pre.onload = function () { img.src = url; if (img.nextSibling) img.nextSibling.textContent = ""; };
        pre.src = url;
      });
    }
    setInterval(function () { tick++; paintSims(); }, 1000);
    paintSims();
    function hhmmss(ts) { var d = new Date(ts); return isNaN(d) ? "" : d.toTimeString().slice(0, 8); }
    function line(e) {
      var who = e.sim != null ? "sim-" + e.sim + " " : "";
      var txt;
      switch (e.type) {
        case "step": case "log": txt = e.text || e.message || ""; break;
        case "flow_start": txt = "▶ " + (e.flow || ""); break;
        case "flow_end": txt = "■ " + (e.flow || "") + " " + (e.status || ""); break;
        case "limit_hit": txt = "limit hit: " + (e.limit || e.text || JSON.stringify(e)); break;
        default: txt = e.type + (e.text ? " " + e.text : e.phase ? " " + e.phase : "");
      }
      return hhmmss(e.ts) + " " + who + txt;
    }
    var es = new EventSource("/api/runs/" + encodeURIComponent(run) + "/events");
    es.addEventListener("ev", function (m) {
      var e; try { e = JSON.parse(m.data); } catch (x) { return; }
      var stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.appendChild(document.createTextNode(line(e) + "\n"));
      while (log.childNodes.length > 1500) log.removeChild(log.firstChild);
      if (stick) log.scrollTop = log.scrollHeight;
    });
    es.addEventListener("state", refetch);
    es.addEventListener("end", function () { es.close(); refetch(); });
  }

  // ---- Projects/machine: light auto-refresh, never while a dialog is open
  if (PAGE.page === "projects" || PAGE.page === "machine") {
    setInterval(function () {
      if (!document.querySelector("dialog[open]") && !document.hidden) location.reload();
    }, viewer ? 10000 : 5000);
  }
})();
