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


def age_s(iso):
    """Seconds since an ISO timestamp, or None when it is not one."""
    try:
        return time.time() - datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago(iso):
    d = age_s(iso)
    if d is None:
        return h(iso)
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

def layout(ctx, title, body, active="", page_data=None, viewer_pill="view only"):
    projects = data.project_names()
    live = len([r for r in data.active_runs() if not r["stale"]])
    side = ['<div class="lg"><i></i>E2E Console</div>']
    for key, label, extra in (("projects", "Projects", ""),
                              ("runs", "Runs", pill("%d live" % live) if live else ""),
                              ("bugs", "Bugs", ""), ("triage", "Triage", ""), ("machine", "Machine", "")):
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
<header class="mtop"><b>E2E Console</b>%(viewpill)s<nav><a href="/projects">Projects</a><a href="/runs">Runs</a><a href="/bugs">Bugs</a><a href="/triage">Triage</a><a href="/machine">Machine</a></nav></header>
<div class="app"><nav class="side">%(side)s</nav>
<main class="main">%(body)s</main></div>%(pdata)s
</body></html>""" % {
        "csrf": h(ctx["csrf"]), "title": h(title), "side": "".join(side), "body": body,
        "bodycls": "viewer" if viewer else "operator",
        "viewpill": pill(viewer_pill) if viewer else "",
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


def bug_prefill(project, run, f):
    """What "File as bug" puts in the add form for one failed flow; the operator edits it before filing."""
    flow, msg = str(f.get("flow") or ""), str(f.get("message") or "")
    seen = ", ".join(k for k in ("screenshot", "video") if f.get(k)) or "report"
    return {"project": project, "run": run["run"], "flow": flow,
            "title": (msg or "%s %s" % (flow, f.get("kind") or "failed"))[:200],
            "do": ". ".join(str(x) for x in f.get("repro") or [])[:2000], "see": msg[:2000],
            "proof": "Failed flow %s in run %s (%s)" % (flow, run["run"], seen)}


def triage_row(ctx, name, run, f, marks, filed, can_file):
    flow = f.get("flow")
    right = pill(marks[flow].get("mark"), "good") if flow in marks else pill("open", "warn")
    bug = filed.get((run["run"], flow))
    if bug:
        right += ' <a class="pill live" href="/bugs/%s/%s">%s</a>' % (u(name), u(bug), h(bug))
    elif can_file:
        right += ' <button class="b sec sm" data-filebug="%s">File as bug</button>' % h(json.dumps(bug_prefill(name, run, f)))
    return '<div class="fl"><span class="fail">✕</span><span>%s <span class="muted">%s</span></span><span class="tr">%s</span></div>' % (
        h(flow), h(f.get("kind")), right)


def triage_page(ctx):
    from console import cli
    registered = cli.projects_or_empty()[0]
    blocks = []
    for name in data.project_names():
        done = [r for r in data.list_runs(name, limit=10) if r["has_report"] and r.get("failures")]
        filed = {}
        if done and name in registered:
            try:
                filed = {(c.get("run"), c.get("flow")): c.get("id") for c in cli.bug_list(name).get("bugs") or []
                         if c.get("run") and c.get("flow")}
            except (cli.CLIError, AttributeError):
                pass  # no board for this project: the rows still render
        for r in done[:3]:
            marks = data.triage_marks(name, r["run"])
            items = "".join(triage_row(ctx, name, r, f, marks, filed, not ctx["viewer"] and name in registered)
                            for f in r["failures"])
            blocks.append('<h4><a href="/runs/%s/report">%s</a> %s</h4><div class="flows">%s</div>' % (
                u(r["run"]), h(r["run"]), integrity_pill(r), items))
    body = '<div class="mh"><h3>Triage</h3></div><p class="muted small">Failures of the last three finished runs per project. Marks go to an append-only triage.ndjson; reports never change. A failure worth fixing becomes a card on the Bugs board.</p>%s' % (
        "".join(blocks) or '<p class="muted">Nothing to triage.</p>')
    if not ctx["viewer"]:
        body += bug_add_dialog()
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


# ---------------------------------------------------------------- bugs
# Cards come from `e2e bug list|show --json` (CONTRACT.md "Bug board"); nothing here
# decides what a card may do next. The engine refuses a wrong move and says why.

BUG_STATES = (("found", "Needs you"), ("open", "Open"), ("poc", "POC"), ("inprogress", "In progress"),
              ("testing", "Testing"), ("done", "Done"), ("rejected", "Rejected"))
BUG_WORKING = ("poc", "inprogress", "testing")
BUG_PRIORITIES = ("P1", "P2", "P3")
BUG_FIELDS = (("do", "Do"), ("see", "See"), ("want", "Want"), ("proof", "Proof"))
# An agent on a card notes it as it goes: the note is its heartbeat. Older than
# this on a card somebody is working on, and the agent has probably stopped.
BUG_STALE_S = 15 * 60
BUG_PASSES = 3  # runs in a row the engine wants before Done
BUG_REASON_MAX = 500
BUG_PILL = "view + decide"  # a phone is a viewer that may approve, reject and reopen


def cut(v, n):
    v = "" if v is None else str(v)
    return v if len(v) <= n else v[:n - 1].rstrip() + "…"


def when(iso):
    return '<span title="%s">%s</span>' % (h(iso), ago(iso))


def bug_prio(c):
    p = c.get("priority")
    if p in BUG_PRIORITIES:
        return '<span class="pr %s">%s</span>' % (p.lower(), p)
    if c.get("state") != "found":
        return ""
    s = c.get("suggest")
    return '<span class="pr q" title="the finder\'s suggestion">%s?</span>' % (s if s in BUG_PRIORITIES else "P")


def bug_stale(c):
    note = c.get("note") if isinstance(c.get("note"), dict) else {}
    age = age_s(note.get("ts")) if note else None
    return c.get("state") in BUG_WORKING and age is not None and age > BUG_STALE_S


def bug_note(c, full=False):
    """agent · "last note" · its age, marked stale when the heartbeat stopped."""
    note = c.get("note") if isinstance(c.get("note"), dict) else None
    bits = [h(c.get("agent") or "nobody yet")]
    if note:
        text = note.get("text") if full else cut(note.get("text"), 160)
        by = note.get("by")
        bits.append("“%s”%s" % (h(text), " (%s)" % h(by) if by and by != c.get("agent") else ""))
        bits.append(when(note.get("ts")) + (" " + pill("stale", "bad") if bug_stale(c) else ""))
    elif c.get("agent"):
        bits.append("no note yet")
    return " · ".join(bits)


def bug_checks(checks):
    items = []
    for k in checks if isinstance(checks, list) else []:
        if isinstance(k, dict):
            ok = bool(k.get("ok"))
            items.append('<li><b class="%s">%s %s %s</b> <span class="muted">%s</span></li>' % (
                "pass" if ok else "fail", "✓" if ok else "✕", h(k.get("name")), "ok" if ok else "not ok", h(k.get("detail"))))
    return '<ul class="checks">%s</ul>' % "".join(items) if items else ""


def bug_testing(c):
    """Passes out of three, and what the last `bug verify` said."""
    passes = c.get("passes") if isinstance(c.get("passes"), int) else 0
    out = '<div class="m"><b>Pass %d of %d</b></div>' % (passes, BUG_PASSES)
    v = c.get("last_verify") if isinstance(c.get("last_verify"), dict) else None
    if v:
        out += '<div class="m">Last verify %s: %s</div>%s' % (
            when(v.get("ts")), "all checks ok" if v.get("ok") else "not done yet", bug_checks(v.get("checks")))
    return out


def bug_actions(c):
    """The owner's three decisions; the same buttons on the Mac and on the phone.
    The finder's suggested priority is the filled button."""
    st = c.get("state")
    reject = '<button class="b dan" data-rejectopen>Not a bug…</button>'
    if st == "found":
        sug = c.get("suggest")
        btns = '<span class="lbl">Bug, priority</span>' + "".join(
            '<button class="b%s" data-approve="%s">%s</button>' % ("" if p == sug else " sec", p, p)
            for p in BUG_PRIORITIES) + reject
    elif st in ("done", "rejected"):
        return '<div class="btns"><button class="b sec" data-reopen>Reopen</button></div>'
    else:
        btns = reject
    return """<div class="btns">%s</div>
<div class="rej" data-rejectbox hidden><input name="reason" maxlength="%d" value="not a bug" aria-label="Reason" placeholder="Why? not a bug, same as B-4…" autocomplete="off"><button class="b dan" data-reject>Reject</button><button class="b sec" data-rejectcancel>Cancel</button></div>""" % (
        btns, BUG_REASON_MAX)


def bug_who(c):
    bits = [h(c.get("kind") or "bug")]
    if c.get("by"):
        bits.append("by %s" % h(c["by"]))
    if c.get("run"):
        bits.append("from run %s" % h(c["run"]))
    if c.get("dry_run"):
        bits.append("practice")
    return " · ".join(bits)


def bug_card(project, c):
    """One card on the board. A found card is spelled out, because the owner decides on it right here."""
    bid, st = str(c.get("id") or ""), c.get("state")
    head = '<div class="id"><span><a href="/bugs/%s/%s">%s</a> · %s</span>%s</div><div class="t">%s</div>' % (
        u(project), u(bid), h(bid), bug_who(c), bug_prio(c), h(c.get("title")))
    if st == "found":
        rows = "".join("<i>%s</i><span>%s</span>" % (label, h(c.get(k)) or "–") for k, label in BUG_FIELDS)
        return '<div class="bc need" data-bug="%s">%s<div class="bkv">%s</div>%s</div>' % (h(bid), head, rows, bug_actions(c))
    body = ""
    if st in BUG_WORKING or st == "open":
        body += '<div class="m">%s</div>' % bug_note(c)
    if c.get("branch"):
        body += '<div class="m">branch <code>%s</code></div>' % h(c["branch"])
    if st == "testing":
        body += bug_testing(c)
    elif st == "done":
        v = c.get("last_verify") if isinstance(c.get("last_verify"), dict) else {}
        body += '<div class="m ok">✓ verified by the engine %s</div>' % (when(v.get("ts")) if v.get("ts") else "")
    elif st == "rejected":
        body += '<div class="m">Rejected: %s</div>' % h(cut(c.get("reason"), 160))
    if st in ("done", "rejected"):
        body += bug_actions(c)
    return '<div class="bc%s" data-bug="%s">%s%s</div>' % (" stale" if bug_stale(c) else "", h(bid), head, body)


def bug_board_fragment(ctx, project, board, err=None):
    """The part of the board that polling refreshes."""
    if err or not isinstance(board, dict):
        return '<p class="err">e2e bug list: %s</p>' % h(err or "unexpected answer")
    cards = [c for c in board.get("bugs") or [] if isinstance(c, dict)]
    by_state = {s: [c for c in cards if c.get("state") == s] for s, _ in BUG_STATES}
    closed = len(by_state["done"]) + len(by_state["rejected"])
    segs = "".join(
        '<a href="#s-%s" data-s="%s"%s>%s %d</a>' % (s, s, ' class="on"' if s == "found" else "", label, len(by_state[s]))
        if by_state[s] else '<span data-s="%s">%s 0</span>' % (s, label) for s, label in BUG_STATES)
    out = ['<div class="segs">%s</div><p class="muted small">%d open of %d</p>' % (segs, len(cards) - closed, len(cards))]
    if board.get("freeze"):
        out.append('<div class="freeze"><b>Design freeze: on</b><span>A card is still outside Done or Rejected: '
                   'no new feature commits and no push. It lifts by itself when the last card closes.</span></div>')
    # One <section> a step: the page moves and hides them whole (the Columns dialog).
    for s, label in BUG_STATES:
        rows = "".join(bug_card(project, c) for c in by_state[s])
        if s == "found":
            sec = '<h4 id="s-found" class="need">Needs you %s</h4>' % pill(len(by_state[s]))
            sec += '<div class="bcards">%s</div>' % rows if rows else '<p class="muted">Nothing waits for you.</p>'
            if rows:
                sec += '<p class="muted small">New bugs wait here until you say it is a bug and give a priority. No agent starts before that.</p>'
        elif rows:
            sec = '<h4 id="s-%s">%s %s</h4><div class="bcards">%s</div>' % (s, label, pill(len(by_state[s])), rows)
        else:
            continue
        out.append('<section class="bsec" data-s="%s">%s</section>' % (s, sec))
    return "".join(out)


BUG_ERR = '<p class="err" id="bug-err" role="alert" hidden></p>'


def bug_text_fields(names, values=None, need_title=True):
    out = []
    for name, label, cap in names:
        v = h((values or {}).get(name))
        if name == "title":
            out.append('<label class="field"><span class="lbl">%s</span><input name="title" maxlength="%d" value="%s"%s autocomplete="off"></label>' % (
                label, cap, v, " required" if need_title else ""))
        else:
            # The newline after the tag is dropped by the parser; without it a value starting with one would lose it.
            out.append('<label class="field"><span class="lbl">%s</span><textarea name="%s" maxlength="%d" rows="2">\n%s</textarea></label>' % (
                label, name, cap, v))
    return "".join(out)


def chips(name, options, checked):
    return '<div class="chk">%s</div>' % "".join(
        '<label class="chip"><input type="radio" name="%s" value="%s"%s><span>%s</span></label>' % (
            name, h(v), " checked" if v == checked else "", h(label)) for v, label in options)


ADD_FIELDS = (("title", "Title", 200), ("do", "Do: what you do", 2000), ("see", "See: what happens", 2000),
              ("want", "Want: what should happen", 2000), ("proof", "Proof: where the evidence is", 2000))
EDIT_FIELDS = ADD_FIELDS + (("done_when", "Done when", 2000),)


def bug_add_dialog(project=""):
    """Operator only. On Triage the project, flow and run come from the row ("File as bug")."""
    return """<dialog id="bug-add" class="modal">
<form method="dialog" class="runform" data-bugadd data-for="%s">
<div class="mh"><h3>Add a bug</h3></div>
<p class="muted small" data-from hidden></p>
%s
<div><div class="lbl">Kind</div>%s</div>
<div><div class="lbl">Your guess at the priority</div>%s</div>
<p class="muted small">The card lands in “Needs you”. Nobody works on it until it is approved with a priority.</p>
<p class="err" hidden></p>
<div class="actions"><button type="button" class="b sec" data-close>Cancel</button><button type="button" class="b" data-bugfile>Add bug</button></div>
</form></dialog>""" % (h(project), bug_text_fields(ADD_FIELDS),
                       chips("kind", (("bug", "Bug"), ("lag", "Lag")), "bug"),
                       chips("suggest", (("", "None"),) + tuple((p, p) for p in BUG_PRIORITIES), ""))


def bug_cols_dialog():
    """Which steps this device shows, and in what order. The choice stays in the
    browser (console.js); the server and the board never hear of it, so a phone
    may use it too. "Needs you" is not on the list: it cannot be hidden or moved."""
    rows = "".join(
        '<li data-s="%s"><label><input type="checkbox" checked><span>%s</span></label>'
        '<button type="button" class="b sec" data-up aria-label="Move %s up">↑</button>'
        '<button type="button" class="b sec" data-down aria-label="Move %s down">↓</button></li>' % (s, label, label, label)
        for s, label in BUG_STATES if s != "found")
    return """<dialog id="bug-cols" class="modal">
<form method="dialog" class="runform">
<div class="mh"><h3>Columns</h3></div>
<p class="muted small">What this device shows, and in what order. Untick a step to hide its cards here; its count stays on top. “Needs you” always comes first.</p>
<ul class="cols">%s</ul>
<div class="actions"><button type="button" class="b sec" data-colreset>Reset</button><button type="button" class="b" data-close>Done</button></div>
</form></dialog>""" % rows


def bug_board_page(ctx, project, board, err=None):
    btns = '<button class="b sec" data-open="bug-cols">Columns</button>'
    if not ctx["viewer"]:
        btns += '<button class="b sec" data-open="bug-add">+ Add bug</button>'
    body = ('<div class="mh"><h3>%sBugs · %s</h3><div class="btns">%s</div></div>%s<p class="muted small" id="cols-note" hidden></p>'
            '<div id="bugs" class="bugs" data-project="%s" data-fragment="/bugs/%s/fragment">%s</div>%s') % (
        icon(project), h(project), btns, BUG_ERR, h(project), u(project), bug_board_fragment(ctx, project, board, err),
        bug_cols_dialog())
    if not ctx["viewer"]:
        body += bug_add_dialog(project)
    return layout(ctx, "Bugs · " + project, body, "bugs", {"page": "bugs"}, viewer_pill=BUG_PILL)


def bugs_index_page(ctx, boards):
    """Pick a project. `boards` is {name: `bug list` answer, or {"error": text}}."""
    cards = []
    for name, b in boards.items():
        states = [c.get("state") for c in b.get("bugs") or [] if isinstance(c, dict)]
        found = states.count("found")
        if b.get("error"):
            line = '<span class="err">e2e bug list: %s</span>' % h(b["error"])
        elif not states:
            line = "<span>No cards.</span>"
        else:
            line = "<span>%s</span><span>%d open</span><span>%d working</span><span>%d done</span>" % (
                '<b class="flaky">%d need you</b>' % found if found else "0 need you",
                states.count("open"), len([x for x in states if x in BUG_WORKING]), states.count("done"))
        cards.append('<div class="pc"><div class="t">%s%s %s</div><div class="kv">%s</div><div class="btns"><a class="b sec" href="/bugs/%s">Board</a></div></div>' % (
            icon(name), h(name), pill("design freeze", "warn") if b.get("freeze") else "", line, u(name)))
    body = '<div class="mh"><h3>Bugs</h3></div><div class="proj">%s</div>' % (
        "".join(cards) or '<p class="muted">No projects yet.</p>')
    return layout(ctx, "Bugs", body, "bugs", {"page": "bugs-index"}, viewer_pill=BUG_PILL)


def bug_value(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(bug_value(x) for x in v)
    return "" if v is None else str(v)


def bug_history(c):
    """Every line of the card, newest first, as the engine wrote it."""
    rows = []
    for e in reversed(c.get("history") if isinstance(c.get("history"), list) else []):
        if not isinstance(e, dict):
            continue
        extra = "".join(
            bug_checks(v) if k == "checks" else '<div class="m"><i>%s</i> %s</div>' % (h(k.replace("_", " ")), h(cut(bug_value(v), 600)))
            for k, v in e.items() if k not in ("ts", "bug", "type", "by", "via"))
        rows.append('<div class="bc"><div class="id"><span><b>%s</b> · %s%s</span>%s</div>%s</div>' % (
            h(e.get("type")), h(e.get("by")), " · via %s" % h(e["via"]) if e.get("via") else "", when(e.get("ts")), extra))
    return "".join(rows) or '<p class="muted">No history.</p>'


def bug_run_link(run):
    return '<a href="/runs/%s/report">%s</a>' % (u(run), h(run)) if data.split_run_id(str(run)) else h(run)


def bug_card_fragment(ctx, project, c):
    """The card page's refreshing part: the card as it stands, then its history."""
    bid, st = str(c.get("id") or ""), c.get("state")
    label = dict(BUG_STATES, found="Found · needs you").get(st, st)
    state = [h(label), bug_note(c, full=True)]
    if c.get("branch"):
        state.append("branch <code>%s</code>" % h(c["branch"]))
    rows = [("State", " · ".join(state))]
    rows += [(lab, h(c.get(k)) or "–") for k, lab in BUG_FIELDS]
    if c.get("flow"):
        rows.append(("Flow", "<code>%s</code>" % h(c["flow"])))
    if c.get("run"):
        rows.append(("Found in", bug_run_link(c["run"])))
    if c.get("done_when"):
        rows.append(("Done when", h(c["done_when"])))
    if c.get("limit") is not None:
        rows.append(("Limit", "%s %s" % (h(c["limit"]), h(c.get("limit_unit") or ""))))
    measures = [m for m in c.get("measures") or [] if isinstance(m, dict)]
    if measures:
        rows.append(("Measured", "<br>".join("%s %s · %s%s · %s" % (
            h(m.get("value")), h(m.get("unit") or ""), h(m.get("by")),
            " · run %s" % bug_run_link(m["run"]) if m.get("run") else "", when(m.get("ts"))) for m in measures[-5:])))
    r = c.get("review") if isinstance(c.get("review"), dict) else None
    if r:
        rows.append(("Review", '<b class="%s">%s</b> by %s %s%s' % (
            "pass" if r.get("ok") else "fail", "ok" if r.get("ok") else "not ok", h(r.get("by")), when(r.get("ts")),
            ": %s" % h(r["text"]) if r.get("text") else "")))
    if st == "rejected":
        rows.append(("Rejected", h(c.get("reason")) or "–"))
    if c.get("rev") != c.get("acked") and c.get("agent"):
        rows.append(("Changed", "rev %s; %s has read rev %s" % (h(c.get("rev")), h(c["agent"]), h(c.get("acked")))))
    kv = "".join("<i>%s</i><span>%s</span>" % (k, v) for k, v in rows)
    verify = ""
    if st == "testing":
        verify = bug_testing(c)
    elif isinstance(c.get("last_verify"), dict):
        v = c["last_verify"]
        verify = '<div class="m">Last verify %s: %s</div>%s' % (
            when(v.get("ts")), "all checks ok" if v.get("ok") else "not done", bug_checks(v.get("checks")))
    return """<div class="detail%s" data-bug="%s"><h4>%s · %s %s</h4><p class="muted small">%s · filed %s</p>
<div class="bkv">%s</div>%s%s</div>
<h4>History</h4><div class="bcards hist">%s</div>""" % (
        " stale" if bug_stale(c) else "", h(bid), h(bid), h(c.get("title")), bug_prio(c), bug_who(c), when(c.get("created")),
        kv, verify, bug_actions(c), bug_history(c))


def bug_edit_dialog(c):
    prio = ""
    if c.get("priority") in BUG_PRIORITIES:  # a found card gets its priority from the approve buttons
        prio = '<div><div class="lbl">Priority</div>%s</div>' % chips("priority", tuple((p, p) for p in BUG_PRIORITIES), c["priority"])
    return """<dialog id="bug-edit" class="modal">
<form method="dialog" class="runform" data-bugedit="%s">
<div class="mh"><h3>Edit %s</h3></div>
%s%s
<p class="muted small">Only what you change is saved. A card in progress or in testing goes back to POC and keeps its history; its agent has to read the change before it can finish.</p>
<p class="err" hidden></p>
<div class="actions"><button type="button" class="b sec" data-close>Cancel</button><button type="button" class="b" data-bugsave>Save</button></div>
</form></dialog>""" % (h(c.get("id")), h(c.get("id")), bug_text_fields(EDIT_FIELDS, c), prio)


def bug_card_page(ctx, project, c):
    bid = str(c.get("id") or "")
    can_edit = not ctx["viewer"] and c.get("state") not in ("done", "rejected")
    body = '<div class="mh"><h3>%s<a href="/bugs/%s">Bugs · %s</a></h3>%s</div>%s<div id="bugs" class="bugs" data-project="%s" data-fragment="/bugs/%s/%s/fragment">%s</div>%s' % (
        icon(project), u(project), h(project),
        '<button class="b sec" data-open="bug-edit">Edit…</button>' if can_edit else "", BUG_ERR,
        h(project), u(project), u(bid), bug_card_fragment(ctx, project, c), bug_edit_dialog(c) if can_edit else "")
    return layout(ctx, "%s · %s" % (bid, project), body, "bugs", {"page": "bug"}, viewer_pill=BUG_PILL)
