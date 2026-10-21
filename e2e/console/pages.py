"""Server-rendered HTML. Everything from disk or the CLI is escaped with h()."""
import html
import json
import time
from datetime import datetime
from urllib.parse import quote

from console import data

ICON_COLORS = 6
STATUS_ICON = {
    "passed": ("pass", "✓"), "failed": ("fail", "✕"), "timeout": ("fail", "✕"),
    "crashed": ("fail", "✕"), "flaky": ("flaky", "↻"), "running": ("run", "●"),
    "pending": ("muted", "○"), "skipped": ("muted", "–"),
}
DEFAULT_LIMITS = {"flow_s": 240, "suite_s": 1500, "run_s": 5400, "sims": 2, "retries": 1}


def h(v):
    return html.escape("" if v is None else str(v), quote=True)


def u(v):
    return quote(str(v), safe="")


def upath(v):
    return quote(str(v), safe="/")


def json_script(id_, obj):
    # "<" escaped so a value can never close the <script> data block.
    return '<script type="application/json" id="%s">%s</script>' % (
        h(id_), json.dumps(obj).replace("<", "\\u003c"))


def dur(s):
    if not isinstance(s, (int, float)):
        return "–"
    s = int(s)
    return "%d:%02d" % (s // 60, s % 60) if s < 3600 else "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def mins(s):
    return "%d min" % max(1, round(s / 60)) if isinstance(s, (int, float)) else "–"


def gb(v):
    return "%.1f GB" % v if isinstance(v, (int, float)) else "–"


def ago(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return h(iso)
    d = time.time() - t
    if d < 90:
        return "just now"
    if d < 5400:
        return "%d min ago" % (d // 60)
    if d < 129600:
        return "%d h ago" % (d // 3600)
    return "%d days ago" % (d // 86400)


def bar(frac):
    pct = max(0.0, min(1.0, frac or 0.0)) * 100
    return '<svg class="bar" aria-hidden="true"><rect width="%.0f%%" height="100%%"/></svg>' % pct


def pill(text, cls=""):
    return '<span class="pill %s">%s</span>' % (h(cls), h(text))


def icon(name):
    return '<span class="ico c%d">%s</span>' % (sum(map(ord, name)) % ICON_COLORS, h(name[:1].upper()))


def counts_line(c):
    if not isinstance(c, dict):
        return ""
    bits = []
    if c.get("failed"):
        bits.append('<span class="fail">%d failed</span>' % c["failed"])
    if c.get("flaky"):
        bits.append('<span class="flaky">%d flaky</span>' % c["flaky"])
    if c.get("passed") is not None:
        bits.append('<span class="pass">%d passed</span>' % c["passed"])
    if c.get("skipped"):
        bits.append("%d skipped" % c["skipped"])
    return " · ".join(bits)


def status_pill(run):
    st = run.get("status", "?")
    if run.get("stale"):
        return pill("process gone", "bad")
    cls = {"passed": "good", "failed": "bad", "error": "bad", "stopped": "warn"}.get(st, "live")
    return pill(st, cls)


def integrity_pill(run):
    i = run.get("integrity")
    if i == "tampered":
        return '<span class="pill bad tampered" title="report.json no longer matches history.ndjson">tampered</span>'
    if i == "unrecorded":
        return pill("not in history", "warn")
    return ""


# ---------------------------------------------------------------- layout

def layout(ctx, title, body, active="", page_data=None):
    projects = data.project_names()
    live = len([r for r in data.active_runs() if not r["stale"]])
    side = ['<div class="lg"><i></i>E2E Console</div>']
    for key, label, extra in (("projects", "Projects", ""),
                              ("runs", "Runs", pill("%d live" % live) if live else ""),
                              ("triage", "Triage", ""), ("machine", "Machine", "")):
        side.append('<a href="/%s"%s>%s %s</a>' % (key, ' class="on"' if active == key else "", label, extra))
    side.append('<div class="h">Projects</div>')
    for p in projects:
        side.append('<a href="/projects/%s"%s>%s</a>' % (u(p), ' class="on"' if active == "p:" + p else "", h(p)))
    side.append('<form class="out" method="post" action="/logout"><span class="muted">%s</span>'
                '<button class="lnk">Sign out</button></form>' % h(ctx["user"]))
    viewer = ctx["viewer"]
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="csrf" content="%(csrf)s">
<title>%(title)s · E2E Console</title>
<link rel="stylesheet" href="/static/console.css">
<script src="/static/console.js" defer></script>
</head><body class="%(bodycls)s">
<header class="mtop"><b>E2E Console</b>%(viewpill)s<nav><a href="/projects">Projects</a><a href="/runs">Runs</a><a href="/triage">Triage</a><a href="/machine">Machine</a></nav></header>
<div class="app"><nav class="side">%(side)s</nav>
<main class="main">%(body)s</main></div>%(pdata)s
</body></html>""" % {
        "csrf": h(ctx["csrf"]), "title": h(title), "side": "".join(side), "body": body,
        "bodycls": "viewer" if viewer else "operator",
        "viewpill": pill("view only") if viewer else "",
        "pdata": json_script("page-data", page_data) if page_data is not None else "",
    }


def login_page(nxt, error=""):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Sign in · E2E Console</title>
<link rel="stylesheet" href="/static/console.css">
</head><body class="signin">
<form class="login" method="post" action="/login">
<div class="lg"><i></i>E2E Console</div>
<p class="err" role="alert">%s</p>
<input type="hidden" name="next" value="%s">
<input name="username" autocomplete="username" autocapitalize="none" spellcheck="false" placeholder="User" aria-label="User" required autofocus>
<input name="password" type="password" autocomplete="current-password" placeholder="Password" aria-label="Password" required>
<button class="b">Sign in</button>
</form></body></html>""" % (h(error), h(nxt))


def signed_in_page(nxt):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="refresh" content="0;url=%s">
<title>Signed in · E2E Console</title>
<link rel="stylesheet" href="/static/console.css">
</head><body class="signin">
<div class="login"><div class="lg"><i></i>E2E Console</div>
<p>Signed in. <a href="%s">Continue</a></p></div></body></html>""" % (h(nxt), h(nxt))


def error_page(ctx, code, msg):
    return layout(ctx, "Error", '<div class="mh"><h3>%d</h3></div><p>%s</p>' % (code, h(msg)))


# ---------------------------------------------------------------- projects

def gauges(m):
    ram_frac = (m["ram_free_gb"] / m["ram_total_gb"]) if m["ram_free_gb"] and m["ram_total_gb"] else 0
    disk_frac = (m["disk_free_gb"] / m["disk_total_gb"]) if m["disk_free_gb"] and m["disk_total_gb"] else 0
    holder = m["build_holder"]
    low_ram = isinstance(m["ram_free_gb"], float) and m["ram_free_gb"] < 3
    low_disk = isinstance(m["disk_free_gb"], float) and m["disk_free_gb"] < 12
    return """<div class="res">
<div class="gauge">Simulators <b>%d / %d</b>%s</div>
<div class="gauge%s">RAM free <b>%s</b>%s</div>
<div class="gauge%s">Disk free <b>%s</b>%s</div>
<div class="gauge">Build slot <b>%s</b>%s</div></div>""" % (
        m["sims_used"], m["sims_cap"], bar(m["sims_used"] / m["sims_cap"]),
        " low" if low_ram else "", gb(m["ram_free_gb"]), bar(ram_frac),
        " low" if low_disk else "", gb(m["disk_free_gb"]), bar(disk_frac),
        h(holder) if holder else "free", bar(1 if holder else 0))


def suite_summary(p):
    suites = p.get("suites") or {}
    engines = {}
    flows = 0
    for s in suites.values():
        engines[s.get("engine", "?")] = engines.get(s.get("engine", "?"), 0) + 1
        n = s.get("flows_count", s.get("count"))
        if isinstance(s.get("flows"), list):
            n = len(s["flows"])
        if isinstance(n, int):
            flows += n
    left = "%d suites" % len(suites) + (" · %d flows" % flows if flows else "")
    right = " · ".join("%s %d" % (k, v) for k, v in sorted(engines.items()))
    return "<span>%s</span><span>%s</span>" % (h(left), h(right))


def norm_limits(raw):
    """app.yml limits come as {flow, suite, run} seconds; report.json as {flow_s, ...}."""
    lim = dict(DEFAULT_LIMITS)
    for k, v in (raw or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            lim[k if k.endswith("_s") or k in ("sims", "retries") else k + "_s"] = v
    return lim


def run_dialog(ctx, name, p):
    chips = "".join('<label class="chip"><input type="checkbox" name="suite" value="%s"><span>%s</span></label>'
                    % (h(s), h(s)) for s in p.get("suites") or {})
    lim = norm_limits(p.get("limits"))
    limits = "Hard stops: %s per flow · %s per suite · %s per run · %s sims · %s retry." % (
        mins(lim.get("flow_s")), mins(lim.get("suite_s")), mins(lim.get("run_s")), lim.get("sims"), lim.get("retries"))
    ref = " · ".join(h(p[k]) for k in ("branch", "commit") if p.get(k))
    return """<dialog id="run-%(id)s" class="modal" data-project="%(name)s">
<form method="dialog" class="runform">
<div class="mh"><h3>Run %(name)s</h3>%(ref)s</div>
<div><div class="lbl">Suites</div><div class="chk">%(chips)s</div></div>
<div><div class="lbl">Mode</div><div class="chk"><label class="chip"><input type="radio" name="mode" value="scripted" checked><span>Scripted flows</span></label></div></div>
<div class="est"><b class="estline">Pick at least one suite.</b><span class="muted">%(limits)s It waits in the queue if another project holds the build slot.</span></div>
<p class="err" hidden></p>
<div class="actions"><button type="button" class="b sec" data-close>Cancel</button><button type="button" class="b" data-start disabled>Start run</button></div>
</form></dialog>""" % {"id": h(name), "name": h(name), "chips": chips or '<span class="muted">No suites in app.yml.</span>',
                       "limits": h(limits), "ref": pill(ref) if ref else ""}


def project_card(ctx, name, p, active):
    hist = data.history(name)
    last = hist[-1] if hist else None
    run = next((r for r in active if r["project"] == name), None)
    st = run["status"] if run else (last.get("status") if last else "no runs")
    head = '<div class="t">%s%s %s</div>' % (icon(name), h(name), pill(st, "live" if run else ""))
    lines = ['<div class="kv">%s</div>' % suite_summary(p)]
    if last:
        lines.append('<div class="kv"><span>Last: %s · %s</span></div>' % (counts_line(last.get("counts")) or h(last.get("status")), ago(last.get("finished"))))
    if run:
        est, el = run.get("estimate_s"), run.get("elapsed_s")
        frac = (el / est) if isinstance(est, (int, float)) and est and isinstance(el, (int, float)) else 0
        lines.append(bar(frac))
        lines.append('<div class="kv"><span><a href="/runs/%s">Run #%d</a> · %s · %s of ~%s</span></div>' % (
            u(run["run"]), run["num"], h(run.get("phase") or ",".join(run["request"].get("suites") or [])),
            mins(el), mins(est)))
    btns = []
    if not ctx["viewer"] and p.get("suites"):
        btns.append('<button class="b" data-open="run-%s">Run…</button>' % h(name))
    btns.append('<a class="b sec" href="/projects/%s#suites">Suites</a>' % u(name))
    btns.append('<a class="b sec" href="/projects/%s#history">History</a>' % u(name))
    return '<div class="pc">%s%s<div class="btns">%s</div></div>%s' % (
        head, "".join(lines), "".join(btns), "" if ctx["viewer"] else run_dialog(ctx, name, p))


def projects_page(ctx):
    projs, err = data_projects()
    m = data.machine()
    names = sorted(set(projs) | set(data.project_names()))
    cards = "".join(project_card(ctx, n, projs.get(n, {}), m["active"]) for n in names)
    add = "" if ctx["viewer"] else '<button class="b sec" data-open="add-project">+ Add project</button>'
    body = '<div class="mh"><h3>Projects</h3>%s</div>%s%s<div class="proj">%s</div>' % (
        add, gauges(m), '<p class="err">e2e projects: %s</p>' % h(err) if err else "",
        cards or '<p class="muted">No projects yet.</p>')
    body += '<p class="muted small">A project is a local folder with an <code>e2e/app.yml</code>. Its history is kept under its name: add the same name again with a new path and the history stays.</p>'
    if not ctx["viewer"]:
        body += ADD_DIALOG
    return layout(ctx, "Projects", body, "projects", {"page": "projects"})


ADD_DIALOG = """<dialog id="add-project" class="modal">
<form method="dialog" class="runform" data-addform>
<div class="mh"><h3>Add project</h3></div>
<label class="field"><span class="lbl">Local path</span><input name="path" required placeholder="/Users/sergei/GitHub/camipack" autocomplete="off" spellcheck="false"></label>
<label class="field"><span class="lbl">Name</span><input name="name" required pattern="[a-z0-9][a-z0-9_-]*" placeholder="camipack" autocomplete="off" spellcheck="false"></label>
<p class="muted small">The repo, its <code>e2e/</code> folder, or <code>e2e/app.yml</code>. Lowercase name; runs and history are filed under it.</p>
<p class="err" hidden></p>
<div class="actions"><button type="button" class="b sec" data-close>Cancel</button><button type="button" class="b" data-add>Add</button></div>
</form></dialog>"""


def data_projects():
    from console import cli
    return cli.projects_or_empty()


def project_page(ctx, name):
    projs, err = data_projects()
    p = projs.get(name, {})
    rows = []
    for s, info in (p.get("suites") or {}).items():
        where = info.get("flows") or info.get("tests") or ""
        rows.append("<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            h(s), h(info.get("engine", "")), h(where if isinstance(where, str) else ", ".join(map(str, where))),
            h(mins(info["budget_s"]) if isinstance(info.get("budget_s"), (int, float)) else info.get("budget", ""))))
    runs = {r["run"]: r for r in data.list_runs(name, limit=100)}
    hrows = []
    for hrow in reversed(data.history(name)[-100:]):
        rid = str(hrow.get("run", ""))
        r = runs.get(rid) or {}
        hrows.append("<tr><td><a href=\"/runs/%s/report\">%s</a></td><td>%s</td><td>%s</td><td>%s %s</td></tr>" % (
            u(rid), h(rid), h(hrow.get("status")), counts_line(hrow.get("counts")), ago(hrow.get("finished")), integrity_pill(r)))
    live = [r for r in runs.values() if not r["has_report"]]
    lrows = "".join('<tr><td><a href="/runs/%s">%s</a></td><td>%s</td><td>%s</td><td></td></tr>' % (
        u(r["run"]), h(r["run"]), status_pill(r), h(r.get("phase", ""))) for r in live)
    btn = ('<button class="b" data-open="run-%s">Run…</button>' % h(name)) if not ctx["viewer"] and p.get("suites") else ""
    if p.get("error"):
        err = "app.yml: %s" % p["error"]
    body = """<div class="mh"><h3>%s%s</h3>%s</div>%s
<h4 id="suites">Suites</h4><div class="tbl"><table><tr><th>Suite</th><th>Engine</th><th>Flows / tests</th><th>Budget</th></tr>%s</table></div>
<h4 id="history">History</h4><div class="tbl"><table><tr><th>Run</th><th>Status</th><th>Counts</th><th>Finished</th></tr>%s%s</table></div>%s""" % (
        icon(name), h(name), btn, '<p class="err">%s</p>' % h(err) if err else "",
        "".join(rows) or '<tr><td colspan="4" class="muted">No suites known.</td></tr>',
        lrows, "".join(hrows) or ('' if lrows else '<tr><td colspan="4" class="muted">No finished runs.</td></tr>'),
        "" if ctx["viewer"] or not p.get("suites") else run_dialog(ctx, name, p))
    return layout(ctx, name, body, "p:" + name, {"page": "project"})


# ---------------------------------------------------------------- runs

def runs_page(ctx):
    rows = []
    for r in data.list_runs(limit=200):
        link = "/runs/%s%s" % (u(r["run"]), "/report" if r["has_report"] else "")
        rows.append('<tr><td><a href="%s">%s</a></td><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            link, h(r["run"]), status_pill(r), integrity_pill(r),
            h(", ".join(r["request"].get("suites") or [])), counts_line(r.get("counts")),
            h(r["request"].get("requested_by", ""))))
    body = '<div class="mh"><h3>Runs</h3></div><div class="tbl"><table><tr><th>Run</th><th>Status</th><th>Suites</th><th>Counts</th><th>By</th></tr>%s</table></div>' % (
        "".join(rows) or '<tr><td colspan="5" class="muted">No runs yet.</td></tr>')
    return layout(ctx, "Runs", body, "runs", {"page": "runs"})


def live_fragment(ctx, run):
    """The part of the live page that SSE refreshes (header, sims, flows)."""
    rid = run["run"]
    est, el = run.get("estimate_s"), run.get("elapsed_s")
    can_stop = (not ctx["viewer"] and run["request"].get("requested_by") == "console"
                and run["status"] not in data.TERMINAL and not run["has_report"])
    btns = []
    if run["has_report"]:
        btns.append('<a class="b" href="/runs/%s/report">Report</a>' % u(rid))
    if can_stop:
        btns.append('<button class="b dan" data-stop="%s">Stop run</button>' % h(rid))
    head = '<div class="mh"><h3>%s · run #%d %s %s</h3><div class="btns">%s</div></div>' % (
        h(run["project"]), run["num"], pill("%s / ~%s" % (dur(el), mins(est))), status_pill(run), "".join(btns))
    sub = '<p class="muted small">%s · suites %s · by %s%s</p>' % (
        h(run.get("phase") or run["status"]), h(", ".join(run["request"].get("suites") or [])),
        h(run["request"].get("requested_by", "?")), " · dry run" if run["request"].get("dry_run") else "")
    tiles = []
    for s in run.get("sims") or []:
        k = s.get("slot")
        if not isinstance(k, int):
            continue
        tiles.append('<div class="sim"><div class="scr"><img data-live="/runs/%s/files/live/sim-%d.png" alt="sim-%d screen"><span>no frame yet</span></div><b>sim-%d</b><span class="muted">%s</span></div>' % (
            u(rid), k, k, k, h(s.get("flow") or "idle")))
    sims = '<div class="sims">%s</div>' % "".join(tiles) if tiles else ""
    c = run.get("counts") or {}
    counts = '<p class="small">%s%s</p>' % (counts_line(c), " · %d running · %d pending" % (c.get("running", 0), c.get("pending", 0)) if c else "")
    flows = []
    for f in run.get("flows") or []:
        cls, ic = STATUS_ICON.get(f.get("status"), ("muted", "?"))
        st = f.get("status")
        right = {"running": "running", "flaky": "passed on retry", "pending": "pending"}.get(st) or "%s · %s" % (st, dur(f.get("duration_s")) if f.get("duration_s") else "")
        q = pill("quarantined") if f.get("quarantined") else ""
        flows.append('<div class="fl"><span class="%s">%s</span><span>%s %s</span><span class="muted">%s</span></div>' % (
            cls, ic, h(f.get("id")), q, h(right.strip(" ·"))))
    return head + sub + sims + counts + ('<div class="flows">%s</div>' % "".join(flows) if flows else "")


def live_page(ctx, run):
    body = '<div id="live">%s</div><h4>Live log</h4><div class="logs" id="log"></div>' % live_fragment(ctx, run)
    return layout(ctx, run["run"], body, "runs", {"page": "live", "run": run["run"]})


def failure_text(run):
    """Plain text for 'Copy for Claude': everything needed to start fixing, with local paths."""
    req = run["request"]
    d = run["dir"]
    lines = ["E2E run %s (project %s) ended %s." % (run["run"], run["project"], run.get("status")),
             "Suites: %s. Commit %s on %s. Requested by %s." % (
                 ", ".join(req.get("suites") or []), req.get("commit", "?"), req.get("branch", "?"), req.get("requested_by", "?"))]
    c = run.get("counts") or {}
    if c:
        lines.append("Counts: " + ", ".join("%s %s" % (k, v) for k, v in c.items()))
    if run.get("integrity") == "tampered":
        lines.append("WARNING: report.json does not match its history hash (tampered).")
    lines.append("Run folder: %s" % d)
    for i, f in enumerate(run.get("failures") or [], 1):
        lines += ["", "%d. %s: %s" % (i, f.get("flow"), f.get("kind")), "Message: %s" % (f.get("message") or "")]
        if f.get("crash"):
            lines.append("Crash: %s" % f["crash"])
        if f.get("repro"):
            lines.append("Steps to reproduce:")
            lines += ["  %d) %s" % (j, s) for j, s in enumerate(f["repro"], 1)]
        for k in ("screenshot", "video"):
            if f.get(k):
                lines.append("%s: %s" % (k.title(), d / f[k]))
        if f.get("log_excerpt"):
            lines += ["Log excerpt:", str(f["log_excerpt"]).rstrip()]
    if not run.get("failures"):
        lines.append("No failures.")
    return "\n".join(lines) + "\n"


def report_page(ctx, run):
    rid = run["run"]
    marks = data.triage_marks(run["project"], rid)
    cards = []
    for f in run.get("failures") or []:
        flow = str(f.get("flow", ""))
        shot = f.get("screenshot")
        img = ('<a href="/runs/%s/files/%s"><img src="/runs/%s/files/%s" alt="screenshot of %s"></a>' % (
            u(rid), upath(shot), u(rid), upath(shot), h(flow))) if shot else "no screenshot"
        links = []
        if f.get("video"):
            links.append('<a href="/runs/%s/files/%s">Video</a>' % (u(rid), upath(f["video"])))
        if data.artifact(rid, "flows/%s/log.txt" % flow):
            links.append('<a href="/runs/%s/files/flows/%s/log.txt">App log</a>' % (u(rid), upath(flow)))
        if data.artifact(rid, "flows/%s/junit.xml" % flow):
            links.append('<a href="/runs/%s/files/flows/%s/junit.xml">JUnit</a>' % (u(rid), upath(flow)))
        extra = ""
        if f.get("crash"):
            extra += "<details><summary>Crash</summary><pre class=\"logs\">%s</pre></details>" % h(f["crash"])
        if f.get("repro"):
            extra += "<details><summary>Steps to reproduce</summary><ol>%s</ol></details>" % "".join("<li>%s</li>" % h(s) for s in f["repro"])
        if f.get("log_excerpt"):
            extra += "<details><summary>Log excerpt</summary><pre class=\"logs\">%s</pre></details>" % h(f["log_excerpt"])
        mark = marks.get(flow)
        marked = '<span class="mark">%s</span>' % (h("Marked %s by %s, %s" % (mark.get("mark"), mark.get("by"), ago(mark.get("ts")))) if mark else "")
        btns = "" if ctx["viewer"] else "".join(
            '<button class="b%s" data-triage="%s" data-run="%s" data-flow="%s">%s</button>' % (
                "" if m == "fix" else " sec", m, h(rid), h(flow), label)
            for m, label in (("fix", "Fix it"), ("not-a-bug", "Not a bug"), ("flaky", "Flaky")))
        cards.append('<div class="failcard"><div class="scr">%s</div><div class="fc"><b>%s %s</b><span class="muted">%s</span><span class="links">%s</span>%s<div class="btns">%s</div>%s</div></div>' % (
            img, h(flow), h(f.get("kind")), h(f.get("message")), " · ".join(links), extra, btns, marked))
    lim = norm_limits(run.get("limits")) if run.get("limits") else {}
    mach = run.get("machine") or {}
    meta = '<p class="muted small">%s · %s · limits: flow %s, suite %s, run %s, %s sims, %s retries · machine at end: RAM %s, disk %s</p>' % (
        counts_line(run.get("counts")), h(", ".join(run["request"].get("suites") or [])),
        mins(lim.get("flow_s")), mins(lim.get("suite_s")), mins(lim.get("run_s")), h(lim.get("sims", "–")), h(lim.get("retries", "–")),
        gb(mach.get("ram_free_gb")), gb(mach.get("disk_free_gb")))
    tamper = ('<div class="warn"><b>Tampered</b><span>report.json no longer matches the SHA-256 recorded in history.ndjson. Treat this report as untrusted.</span></div>'
              if run.get("integrity") == "tampered" else "")
    body = """<div class="mh"><h3>%s run #%d report %s %s</h3><div class="btns"><a class="b sec" href="/runs/%s/junit">JUnit</a><button class="b sec" data-copy="claude-text">Copy for Claude</button></div></div>
%s%s%s<textarea id="claude-text" class="copybox" readonly hidden>%s</textarea>
<h4>Flows</h4><div class="flows">%s</div>""" % (
        h(run["project"]), run["num"], status_pill(run), integrity_pill(run), u(rid), tamper, meta,
        "".join(cards) or '<p class="pass">No failures.</p>', h(failure_text(run)),
        "".join('<div class="fl"><span class="%s">%s</span><span>%s</span><span class="muted">%s</span></div>' % (
            STATUS_ICON.get(f.get("status"), ("muted", "?"))[0], STATUS_ICON.get(f.get("status"), ("muted", "?"))[1],
            h(f.get("id")), h(f.get("status"))) for f in run.get("flows") or []))
    return layout(ctx, rid + " report", body, "runs", {"page": "report", "run": rid})


def junit_page(ctx, run):
    rid = run["run"]
    files = sorted(str(p.relative_to(run["dir"])) for p in run["dir"].rglob("*.xml"))
    rows = "".join('<li><a href="/runs/%s/files/%s">%s</a></li>' % (u(rid), upath(f), h(f)) for f in files)
    body = '<div class="mh"><h3>JUnit · %s</h3></div><ul>%s</ul>' % (h(rid), rows or "<li class=\"muted\">No JUnit files.</li>")
    return layout(ctx, rid + " JUnit", body, "runs")


def triage_page(ctx):
    blocks = []
    for name in data.project_names():
        done = [r for r in data.list_runs(name, limit=10) if r["has_report"] and r.get("failures")]
        for r in done[:3]:
            marks = data.triage_marks(name, r["run"])
            items = "".join('<div class="fl"><span class="fail">✕</span><span>%s <span class="muted">%s</span></span><span>%s</span></div>' % (
                h(f.get("flow")), h(f.get("kind")),
                pill(marks[f.get("flow")].get("mark"), "good") if f.get("flow") in marks else pill("open", "warn"))
                for f in r["failures"])
            blocks.append('<h4><a href="/runs/%s/report">%s</a> %s</h4><div class="flows">%s</div>' % (
                u(r["run"]), h(r["run"]), integrity_pill(r), items))
    body = '<div class="mh"><h3>Triage</h3></div><p class="muted small">Failures of the last three finished runs per project. Marks go to an append-only triage.ndjson; reports never change.</p>%s' % (
        "".join(blocks) or '<p class="muted">Nothing to triage.</p>')
    return layout(ctx, "Triage", body, "triage", {"page": "triage"})


def machine_page(ctx):
    from console import cli
    m = data.machine()
    rows = "".join('<tr><td><a href="/runs/%s">%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        u(r["run"]), h(r["run"]), status_pill(r), h(r.get("phase", "")),
        h(", ".join("sim-%s %s" % (s.get("slot"), s.get("name", "")) for s in r.get("sims") or []))) for r in m["active"])
    golden = "".join("<tr><td>%s</td><td><code>%s</code></td></tr>" % (h(k), h(v)) for k, v in m["golden"].items())
    try:
        st = json.dumps(cli.status(), indent=2)[:8000]
    except cli.CLIError as e:
        st = "e2e status: %s" % e
    body = """<div class="mh"><h3>Machine</h3></div>%s
<p class="muted small">RAM total %s · disk total %s · new sim needs 3 GB RAM free · a run needs 12 GB disk free.</p>
<h4>Active runs</h4><div class="tbl"><table><tr><th>Run</th><th>Status</th><th>Phase</th><th>Sims</th></tr>%s</table></div>
<h4>Golden simulators</h4><div class="tbl"><table><tr><th>Device | runtime</th><th>UDID</th></tr>%s</table></div>
<h4>Engine locks</h4><p class="small">%s</p>
<details><summary>e2e status --json</summary><pre class="logs">%s</pre></details>""" % (
        gauges(m), gb(m["ram_total_gb"]), gb(m["disk_total_gb"]),
        rows or '<tr><td colspan="4" class="muted">Idle.</td></tr>',
        golden or '<tr><td colspan="2" class="muted">None yet.</td></tr>',
        h(", ".join(m["locks"]) or "none"), h(st))
    return layout(ctx, "Machine", body, "machine", {"page": "machine"})
