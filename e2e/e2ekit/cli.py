"""`bin/e2e` command line (CONTRACT.md "CLI")."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from . import bugs, build, config, lifecycle, locks, machine, store


class CliError(Exception):
    pass


def emit(args, data, text):
    if args.json:
        print(json.dumps(data, indent=1))
    elif text:
        print(text)


def load_registered(name):
    entry = store.find_project(name)
    if not entry:
        raise CliError("unknown project %r (register it with: e2e add <repo>)" % name)
    app = config.load_app(entry["path"])
    # The registered name wins over app.yml's: history lives under it, so a
    # repo can be renamed or moved without losing its runs.
    app["project"] = name
    return app


def make_backend(dry_run):
    if dry_run:
        from .backend_fake import FakeBackend
        return FakeBackend()
    from .engines import RealBackend
    return RealBackend()


def pid_is_engine(pid):
    """True when pid is alive and is one of ours, so a recycled pid never
    gets a SIGTERM meant for a finished run."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "e2ekit" in cmd


def project_names():
    names = {p["name"] for p in store.projects()}
    try:
        for d in os.listdir(store.home()):
            if d not in ("engine", "build") and os.path.isdir(os.path.join(store.home(), d, "runs")):
                names.add(d)
    except OSError:
        pass
    return sorted(names)


def active_runs():
    out = []
    for p in project_names():
        for rid in store.run_ids(p):
            st = store.read_json(os.path.join(store.run_dir(rid), "state.json"))
            if st and st.get("status") not in store.TERMINAL:
                st["alive"] = pid_is_engine(st.get("pid"))
                out.append(st)
    return out


# --- commands --------------------------------------------------------------

def cmd_projects(args):
    items = []
    for entry in store.projects():
        item = dict(entry, suites=[], limits=None, last_run=None, error=None)
        try:
            app = config.load_app(entry["path"])
            item["suites"] = [{"name": n, "engine": s["engine"], "budget_s": s["budget"]}
                              for n, s in app["suites"].items()]
            item["limits"] = app["limits"]
            item["device"] = app["device"]
        except config.ConfigError as e:
            item["error"] = str(e)
        hist = store.read_ndjson(os.path.join(store.project_dir(entry["name"]), "history.ndjson"))
        if hist:
            item["last_run"] = hist[-1]
        items.append(item)
    lines = []
    for i in items:
        last = i["last_run"]
        lines.append("%-12s %s  last: %s" % (
            i["name"], ",".join(s["name"] for s in i["suites"]) or i["error"],
            "%s %s" % (last["run"], last["status"]) if last else "-"))
    emit(args, items, "\n".join(lines) or "no projects (e2e add <repo>)")
    return 0


def repo_root(path):
    """Accept the repo, its e2e/ folder or the app.yml itself."""
    path = os.path.realpath(os.path.expanduser(path))
    if os.path.isfile(path) and os.path.basename(path) == "app.yml":
        path = os.path.dirname(path)
    if os.path.basename(path) == "e2e" and os.path.isfile(os.path.join(path, "app.yml")):
        path = os.path.dirname(path)
    return path


def repo_identity(path):
    """What makes two folders the same repository.

    Worktrees of one repository share a git dir, so a second checkout is the
    same project, not a new one. The folder inside the checkout is part of the
    identity so two apps kept in one repository stay two projects.
    """
    path = os.path.realpath(path)
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute",
                            "--git-common-dir", "--show-prefix"], capture_output=True, text=True)
    except OSError:
        return path
    lines = r.stdout.split("\n")
    if r.returncode != 0 or not lines[0]:
        return path  # not a git checkout, or the folder is gone
    return os.path.join(os.path.realpath(lines[0]), lines[1] if len(lines) > 1 else "")


def cmd_add(args):
    app = config.load_app(repo_root(args.repo))
    if args.name:
        if not config.NAME_RE.match(args.name):
            raise CliError("name must be lowercase letters, digits, - or _")
        app["project"] = args.name
    # One name per repository: a second name splits its run history in two
    # and the console then shows one app as several projects.
    ident = repo_identity(app["repo"])
    for p in store.projects():
        if p["name"] != app["project"] and repo_identity(p["path"]) == ident:
            raise CliError("this repository is already project %r; one project name per repository"
                           % p["name"])
    store.register_project(app["project"], app["repo"])
    emit(args, {"name": app["project"], "path": app["repo"], "suites": list(app["suites"])},
         "registered %s (%s) suites: %s" % (app["project"], app["repo"], ", ".join(app["suites"])))
    return 0


def cmd_plan(args):
    app = load_registered(args.project)
    suites = config.pick_suites(app, args.suite)
    est = lifecycle.estimate(app, suites)
    data = dict(est, project=app["project"], suites=suites,
                needs_build=not build.has_cache(app["project"]),
                limits=app["limits"])
    emit(args, data, "%s %s: ~%ds, %d flows on %d sim(s), %s" % (
        app["project"], ",".join(suites), est["estimate_s"], est["flows"], est["sims"],
        "full build" if data["needs_build"] else "incremental build"))
    return 0


def _detach(run, args):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        code = 1
        try:
            # Own session and process group: the console's request ends, the
            # run keeps going, and `e2e stop` can signal the whole group.
            os.setsid()
            log = os.open(os.path.join(run.dir, "engine.log"),
                          os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            run.state["pid"] = os.getpid()
            run.save()
            os.write(w, b"1")
            os.close(w)
            status = run.execute()
            code = 0 if status == "passed" else 1
        finally:
            os._exit(code)
    os.close(w)
    os.read(r, 1)  # state.json carries the child's pid before we answer
    os.close(r)
    emit(args, {"run": run.id, "pid": pid, "detached": True}, run.id)
    return 0


def load_from(name, folder):
    """The project's app, read from another checkout of the same repository.

    A fix is tested where it was made: in its own worktree, before it is
    merged. Any other folder is refused, so a run filed under a project is
    always a run of that project's code.
    """
    entry = store.find_project(name)
    if not entry:
        raise CliError("unknown project %r (register it with: e2e add <repo>)" % name)
    folder = repo_root(folder)
    if repo_identity(folder) != repo_identity(entry["path"]):
        raise CliError("--from %s is not a checkout of %s (%s)" % (folder, name, entry["path"]))
    app = config.load_app(folder)
    app["project"] = name
    return app


def cmd_run(args):
    if bool(args.suite) == bool(args.flow):
        raise CliError("give --suite a,b or --flow suite/flow, one of them")
    app = load_from(args.project, args.source) if args.source else load_registered(args.project)
    flows = [f.strip() for f in (args.flow or "").split(",") if f.strip()]
    if flows:
        bad = [f for f in flows if "/" not in f]
        if bad:
            raise CliError("--flow takes suite/flow ids, not %s" % ", ".join(bad))
        args.suite = ",".join(f.split("/", 1)[0] for f in flows)
    suites = config.pick_suites(app, args.suite)
    backend = make_backend(args.dry_run)
    run = lifecycle.create(app, suites, backend, args.by, args.mode, flows=flows,
                           source=app["repo"] if args.source else None)
    if args.detach:
        return _detach(run, args)
    if not args.json:
        print(run.id, file=sys.stderr)
    status = run.execute()
    emit(args, {"run": run.id, "status": status, "counts": run.state["counts"]},
         "%s %s %s" % (run.id, status, _counts_text(run.state["counts"])))
    return 0 if status == "passed" else 1


def _counts_text(c):
    return " ".join("%s=%s" % (k, c.get(k, 0)) for k in
                    ("total", "passed", "failed", "flaky", "skipped"))


def _signal_run(rid):
    st = store.read_json(os.path.join(store.run_dir(rid), "state.json"))
    if not st:
        raise CliError("no run %s" % rid)
    if st.get("status") in store.TERMINAL:
        return {"run": rid, "signalled": False, "status": st["status"]}
    pid = st.get("pid")
    if not pid_is_engine(pid):
        return {"run": rid, "signalled": False, "status": st.get("status"),
                "note": "engine not running; `e2e clean` closes it"}
    if os.getpgid(pid) == pid:
        os.killpg(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)  # foreground run sharing a shell's group
    return {"run": rid, "signalled": True, "status": st.get("status")}


def cmd_stop(args):
    if args.all:
        rids = [r["run"] for r in active_runs()]
    elif args.run:
        rids = [args.run]
    else:
        raise CliError("give a run id or --all")
    results = [_signal_run(r) for r in rids]
    deadline = time.time() + args.wait
    for res in results:
        while res["signalled"] and time.time() < deadline:
            st = store.read_json(os.path.join(store.run_dir(res["run"]), "state.json")) or {}
            if st.get("status") in store.TERMINAL and os.path.exists(
                    os.path.join(store.run_dir(res["run"]), "report.json")):
                res["status"] = st["status"]
                break
            time.sleep(0.2)
    emit(args, results, "\n".join("%s %s%s" % (r["run"], "stopping" if r["signalled"] else "not running",
                                               " (%s)" % r["status"]) for r in results) or "no active runs")
    return 0


def cmd_status(args):
    if args.run:
        st = store.read_json(os.path.join(store.run_dir(args.run), "state.json"))
        if not st:
            raise CliError("no run %s" % args.run)
        emit(args, st, "%s %s (%s) %ds %s" % (st["run"], st["status"], st["phase"],
                                               st.get("elapsed_s", 0), _counts_text(st["counts"])))
        return 0
    gauges = machine.gauges()
    gauges.update(locks.slot_usage())
    runs = active_runs()
    data = {"machine": gauges, "runs": runs}
    lines = ["RAM free %s GB, disk free %s GB, sims %d/%d, build %s" % (
        gauges["ram_free_gb"], gauges["disk_free_gb"], len(gauges["sims_busy"]), gauges["sims_cap"],
        "busy" if gauges["build_busy"] else "free")]
    for r in runs:
        lines.append("%s %s (%s)%s" % (r["run"], r["status"], r["phase"], "" if r["alive"] else " DEAD"))
    emit(args, data, "\n".join(lines))
    return 0


def cmd_report(args):
    rdir = store.run_dir(args.run)
    rep = store.read_json(os.path.join(rdir, "report.json"))
    final = rep is not None
    if rep is None:
        rep = store.read_json(os.path.join(rdir, "state.json"))
        if rep is None:
            raise CliError("no run %s" % args.run)
    data = dict(rep, final=final)
    lines = ["%s %s%s %s" % (rep["run"], rep["status"], "" if final else " (in progress)",
                             _counts_text(rep.get("counts", {})))]
    for f in rep.get("failures", []):
        lines.append("  %s %s: %s" % (f["kind"], f["flow"], f.get("message")))
    for f in rep.get("flows", []):
        if f["status"] == "flaky":
            lines.append("  flaky %s" % f["id"])
    emit(args, data, "\n".join(lines))
    return 0


def cmd_verify(args):
    names = [args.project] if args.project else project_names()
    results = []
    for p in names:
        for r in store.verify(p):
            results.append(dict(r, project=p))
    bad = [r for r in results if r["status"] == "tampered"]
    emit(args, results, "\n".join("%s %s" % (r["run"], r["status"]) for r in results)
         or "no finished runs")
    return 1 if bad else 0


TRIAGE_MARKS = ("fix", "not-a-bug", "flaky")


def cmd_triage(args):
    st = store.read_json(os.path.join(store.run_dir(args.run), "state.json"))
    if not st:
        raise CliError("no run %s" % args.run)
    if args.flow not in [f["id"] for f in st.get("flows", [])]:
        raise CliError("run %s has no flow %r" % (args.run, args.flow))
    if not args.by.strip():
        raise CliError("--by is required")
    entry = {"ts": store.now_iso(), "run": args.run, "flow": args.flow, "mark": args.mark,
             "by": args.by.strip()[:64], "note": (args.note or "")[:2000]}
    store.append_ndjson(os.path.join(store.project_dir(st["project"]), "triage.ndjson"), entry)
    emit(args, entry, "%s %s: %s" % (args.run, args.flow, args.mark))
    return 0


def _event_text(e):
    t = e.get("type")
    if t == "flow_end":
        return "%s %s%s" % (e.get("status"), e.get("flow"),
                            " (try %s)" % e["attempt"] if e.get("attempt", 1) > 1 else "")
    if t == "build_end":
        return "build done in %ss" % e.get("duration_s")
    if t == "limit_hit":
        return "limit hit: %s" % (e.get("text") or e.get("limit") or "")
    if t in ("queued", "build_start", "stop_requested"):
        return t.replace("_", " ")
    return None  # steps, logs and simulators are noise in a terminal


def cmd_watch(args):
    """Follow one run until it ends: the live feed for a person or for Claude
    (a background task or Monitor gets one line per event, then the result)."""
    rdir = store.run_dir(args.run)
    if not os.path.isdir(rdir):
        raise CliError("no run %s" % args.run)
    pos, gone = 0, 0
    while True:
        # State first, events second: a run that ends between the two reads
        # is caught on the next turn, with none of its last events lost.
        st = store.read_json(os.path.join(rdir, "state.json")) or {}
        ended = st.get("status") in store.TERMINAL
        try:
            with open(os.path.join(rdir, "events.ndjson"), "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            chunk = b""
        cut = chunk.rfind(b"\n") + 1  # a half-written last line waits for the next turn
        pos += cut
        for raw in chunk[:cut].splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            loud = e.get("type") == "limit_hit" or (e.get("type") == "flow_end" and e.get("status") != "passed")
            if args.quiet and not loud:
                continue
            line = json.dumps(e, separators=(",", ":")) if args.json else _event_text(e)
            if line:
                print(line, flush=True)
        if ended:
            break
        gone = 0 if pid_is_engine(st.get("pid")) else gone + 1
        if gone >= 4:
            raise CliError("%s is %s but its engine is gone (e2e clean closes it)" % (
                args.run, st.get("status", "unknown")))
        time.sleep(args.poll)
    rep = store.read_json(os.path.join(rdir, "report.json")) or st
    fails = [{"flow": f["flow"], "kind": f["kind"], "message": f.get("message")} for f in rep.get("failures", [])]
    if args.json:
        print(json.dumps({"type": "report", "run": args.run, "status": rep["status"],
                          "counts": rep.get("counts", {}), "failures": fails}, separators=(",", ":")), flush=True)
    else:
        print("\n".join(["%s %s %s" % (args.run, rep["status"], _counts_text(rep.get("counts", {})))]
                        + ["  %s %s: %s" % (f["kind"], f["flow"], f["message"]) for f in fails]), flush=True)
    return 0 if rep["status"] == "passed" else 1


BRIEF_RUNS = 30  # how far back "last result per suite" looks


def _last_per_suite(project):
    """{suite: {"run","status","finished","failed":[flow ids]}} from the newest
    whole-suite runs; single-flow runs say nothing about a suite."""
    out = {}
    for rid in list(reversed(store.run_ids(project)))[:BRIEF_RUNS]:
        d = store.run_dir(rid)
        req = store.read_json(os.path.join(d, "request.json")) or {}
        if req.get("flows") or req.get("dry_run"):
            continue
        fresh = [s for s in req.get("suites") or [] if s not in out]
        rep = store.read_json(os.path.join(d, "report.json")) if fresh else None
        if not rep:
            continue
        for s in fresh:
            flows = [f for f in rep.get("flows", []) if f.get("suite") == s]
            bad = [f["id"] for f in flows if f.get("status") in bugs.BROKEN]
            done = all(f.get("status") in bugs.RAN for f in flows)
            out[s] = {"run": rid, "finished": rep.get("finished"), "failed": bad,
                      "status": "failed" if bad else ("passed" if done else rep.get("status"))}
    return out


def cmd_brief(args):
    """What a session should know before it starts: what is running, what the
    owner has to decide, whether design is frozen."""
    names = [args.project] if args.project else [p["name"] for p in store.projects()]
    if args.project and not store.find_project(args.project):
        raise CliError("unknown project %r" % args.project)
    runs = [{"run": r["run"], "project": r["project"], "status": r["status"], "phase": r.get("phase"),
             "counts": r.get("counts", {}), "alive": r["alive"]}
            for r in active_runs() if r.get("project") in names]
    projects = []
    for n in names:
        board = bugs.board(n)
        item = {"name": n, "freeze": board["freeze"], "bugs": board["counts"],
                "needs_owner": [{"id": c["id"], "title": c["title"], "kind": c["kind"], "suggest": c["suggest"]}
                                for c in board["bugs"] if c["state"] == "found"]}
        if not args.line:
            item["suites"] = _last_per_suite(n)
        projects.append(item)
    data = {"runs": runs, "projects": projects}
    if args.line:
        bits = ["%s %s %d/%d" % (r["run"], r["status"], r["counts"].get("passed", 0) + r["counts"].get("flaky", 0)
                                 + r["counts"].get("failed", 0), r["counts"].get("total", 0)) for r in runs]
        waiting = sum(len(p["needs_owner"]) for p in projects)
        if waiting:
            bits.append("1 bug needs you" if waiting == 1 else "%d bugs need you" % waiting)
        frozen = [p["name"] for p in projects if p["freeze"]]
        if frozen:
            bits.append("design freeze: %s" % ", ".join(frozen))
        emit(args, data, "e2e: " + (" · ".join(bits) or "idle"))
        return 0
    lines = ["running: " + (", ".join("%s %s (%s)%s" % (r["run"], r["status"], r["phase"],
                                                         "" if r["alive"] else " DEAD") for r in runs) or "nothing")]
    for p in projects:
        lines.append("%s  design freeze: %s  bugs: %s" % (
            p["name"], "ON" if p["freeze"] else "off",
            " ".join("%s=%d" % kv for kv in p["bugs"].items() if kv[1]) or "none"))
        for c in p["needs_owner"]:
            lines.append("  needs the owner: %s %s%s" % (
                c["id"], c["title"], " (suggested %s)" % c["suggest"] if c["suggest"] else ""))
        for s, r in p["suites"].items():
            lines.append("  %-12s %-7s %s%s" % (s, r["status"], r["run"],
                                               "  " + ", ".join(r["failed"][:3]) if r["failed"] else ""))
    emit(args, data, "\n".join(lines))
    return 0


# --- bug board --------------------------------------------------------------

BUG_TEXT = (("title", 200), ("do", 2000), ("see", 2000), ("want", 2000), ("proof", 2000),
            ("done_when", 2000), ("limit_unit", 16))


def _bug_fields(args, names):
    """The event fields the command line gave, trimmed and checked for shape."""
    caps, out = dict(BUG_TEXT), {}
    for n in names:
        v = getattr(args, n, None)
        if v is None:
            continue
        if n in caps:
            v = v.strip()[:caps[n]]
        elif n == "flow":
            v = v.strip()
            if not bugs.FLOW_RE.match(v):
                raise CliError("--flow takes a suite/flow id, not %r" % v)
        elif n == "run":
            st = store.read_json(os.path.join(store.run_dir(v), "state.json"))
            if not st or st.get("project") != args.project:
                raise CliError("%s has no run %s" % (args.project, v))
        out[n] = v
    return out


def _card_text(c):
    lines = ["%s  %-10s %-3s %-3s %s" % (c["id"], c["state"], c["priority"] or "-", c["kind"], c["title"])]
    for label, key in (("do", "do"), ("see", "see"), ("want", "want"), ("proof", "proof"),
                       ("done when", "done_when"), ("rejected", "reason")):
        if c.get(key):
            lines.append("  %-10s %s" % (label + ":", c[key]))
    if c["limit"] is not None:
        lines.append("  %-10s %s %s" % ("limit:", c["limit"], c["limit_unit"] or ""))
    lines.append("  flow: %s  branch: %s  agent: %s  rev %d (read %d)%s" % (
        c["flow"] or "-", c["branch"] or "-", c["agent"] or "-", c["rev"], c["acked"],
        "  passes %d/%d" % (c["passes"], bugs.GREEN_RUNS) if c["state"] == "testing" else ""))
    if c["note"]:
        lines.append("  note (%s): %s" % (c["note"]["by"], c["note"]["text"]))
    for k in (c["last_verify"] or {}).get("checks", []):
        lines.append("  %s %-6s %s" % ("ok " if k["ok"] else "NO ", k["name"], k["detail"]))
    return "\n".join(lines)


def cmd_bug(args):
    project, act = args.project, args.act
    if not store.find_project(project):
        raise CliError("unknown project %r (register it with: e2e add <repo>)" % project)
    if act == "list":
        states = [s.strip() for s in (args.state or "").split(",") if s.strip()]
        bad = [s for s in states if s not in bugs.STATES]
        if bad:
            raise CliError("unknown state(s) %s (have: %s)" % (", ".join(bad), ", ".join(bugs.STATES)))
        data = bugs.board(project, states)
        rows = ["%-5s %-10s %-3s %-3s %-10s %s" % (
            c["id"], c["state"], c["priority"] or "-", c["kind"], c["agent"] or "-", c["title"])
            for c in data["bugs"]]
        emit(args, data, "\n".join(
            ["%s  design freeze: %s  %s" % (project, "ON" if data["freeze"] else "off", " ".join(
                "%s=%d" % kv for kv in data["counts"].items() if kv[1]))] + rows))
        return 0
    if act == "show":
        card = bugs.read(project).get(args.bug)
        if not card:
            raise CliError("no bug %s" % args.bug)
        emit(args, bugs.public(card, project, history=True), _card_text(bugs.public(card, project)))
        return 0
    if act == "verify":
        card, ok = bugs.verify(project, args.bug)
        emit(args, bugs.public(card, project), _card_text(bugs.public(card, project)))
        return 0 if ok else 1
    fields = {
        "add": ("title", "kind", "do", "see", "want", "proof", "flow", "run", "suggest"),
        "approve": ("priority",), "reject": ("reason",), "move": ("to", "branch"),
        "note": ("text",), "measure": ("value", "unit", "run"), "review": ("text",),
        "edit": bugs.EDITABLE,
    }.get(act, ())
    event = _bug_fields(args, fields)
    if act == "add":
        for k in ("do", "see", "want", "proof"):
            event.setdefault(k, "")
        # A practice run proves nothing about the app: a card filed from one is a practice
        # card too, so it never freezes the design and verify counts the same kind of run.
        src = event.get("run")
        req = store.read_json(os.path.join(store.run_dir(src), "request.json"), {}) if src else {}
        if args.dry_run or req.get("dry_run"):
            event["dry_run"] = True
    elif act == "reject":
        event["reason"] = (event.get("reason") or "").strip()[:2000]
    elif act == "note":
        event["text"] = event["text"].strip()[:2000]
    elif act == "review":
        event.update(ok=not args.no, text=(event.get("text") or "").strip()[:2000])
    card = bugs.write(project, act, args.by, getattr(args, "bug", None), via=getattr(args, "via", None), fields=event)
    emit(args, bugs.public(card, project), _card_text(bugs.public(card, project)))
    return 0


def _bug_parser(sub):
    b = sub.add_parser("bug", help="the bug board (CONTRACT.md \"Bug board\")").add_subparsers(dest="act")
    b.required = True

    def cmd(name, bug=True, by=True, owner=False):
        s = b.add_parser(name)
        s.add_argument("project")
        if bug:
            s.add_argument("bug", help="like B-12")
        if by:
            s.add_argument("--by", required=True, help="owner, lead, reproducer, fixer-1, reviewer…")
        if owner:
            s.add_argument("--via", choices=bugs.VIAS, help="how the owner's word arrived")
        s.set_defaults(fn=cmd_bug)
        return s

    def card_fields(s, required):
        s.add_argument("--title", required=required)
        s.add_argument("--do", help="what you do")
        s.add_argument("--see", help="what you see")
        s.add_argument("--want", help="what you want to see")
        s.add_argument("--proof", help="where the evidence is")
        s.add_argument("--flow", help="the test that catches it: suite/flow")

    s = cmd("add", bug=False)
    card_fields(s, True)
    s.add_argument("--kind", default="bug", choices=bugs.KINDS)
    s.add_argument("--run", help="the run it was found in")
    s.add_argument("--suggest", choices=bugs.PRIORITIES, help="the finder's guess; the owner decides")
    s.add_argument("--dry-run", action="store_true", help="a practice card: counts only dry runs, no freeze")
    cmd("approve", owner=True).add_argument("--priority", required=True, choices=bugs.PRIORITIES)
    cmd("reject", owner=True).add_argument("--reason", required=True)
    cmd("reopen", owner=True)
    cmd("claim")
    s = cmd("move")
    s.add_argument("--to", required=True, choices=sorted(bugs.MOVES))
    s.add_argument("--branch")
    cmd("note").add_argument("--text", required=True)
    s = cmd("edit", owner=True)
    card_fields(s, False)
    s.add_argument("--priority", choices=bugs.PRIORITIES)
    s.add_argument("--done-when", dest="done_when")
    s.add_argument("--limit", type=float)
    s.add_argument("--limit-unit", dest="limit_unit")
    cmd("ack")
    s = cmd("measure")
    s.add_argument("--value", type=float, required=True)
    s.add_argument("--unit", default="ms")
    s.add_argument("--run")
    s = cmd("review")
    s.add_argument("--no", action="store_true", help="the check failed")
    s.add_argument("--text")
    cmd("verify", by=False)
    cmd("list", bug=False, by=False).add_argument("--state", help="found,open,…")
    cmd("show", by=False)


def cmd_clean(args):
    from . import sims
    active = {r["run"] for r in active_runs() if r["alive"]}
    golden = sims.golden_udids()
    deleted = []
    for d in sims.devices():
        name = d["name"]
        if not name.startswith(sims.PREFIX) or d["udid"] in golden:
            continue
        owner = name[len(sims.PREFIX):].rpartition("-")[0]
        if owner in active:
            continue
        sims.delete(d["udid"])
        deleted.append(name)
    closed = []
    for p in project_names():
        for rid in store.run_ids(p):
            if rid in active:
                continue
            if lifecycle.finalize_dead(rid):
                closed.append(rid)
            elif store.remove_from_read_only(os.path.join(store.run_dir(rid), "live")):
                closed.append(rid + " (live/)")
    stale = locks.clean_stale()
    data = {"sims_deleted": deleted, "runs_closed": closed, "locks_removed": stale}
    emit(args, data, "deleted sims: %s\nclosed: %s\nlocks: %s" % (
        ", ".join(deleted) or "-", ", ".join(closed) or "-", ", ".join(stale) or "-"))
    return 0


def cmd_prune(args):
    if args.keep < 0:
        raise CliError("--keep must be >= 0")
    lk = locks.project_lock(args.project)
    if not lk.try_acquire("prune"):
        raise CliError("%s has a run in progress" % args.project)
    try:
        doomed = store.prune(args.project, args.keep)
    finally:
        lk.release()
    emit(args, {"deleted": doomed}, "deleted %d run(s)" % len(doomed))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="e2e", description="Manual iOS e2e runs on this Mac. "
                                "Every command takes --json for machine output.")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    sub.add_parser("projects").set_defaults(fn=cmd_projects)
    s = sub.add_parser("add")
    s.add_argument("repo", help="repo, its e2e/ folder, or e2e/app.yml")
    s.add_argument("--name", help="project name (default: app.yml project); history is kept per name")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("plan")
    s.add_argument("project")
    s.add_argument("--suite", required=True)
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("run")
    s.add_argument("project")
    s.add_argument("--suite")
    s.add_argument("--flow", help="only these flows: suite/flow[,suite/flow]")
    s.add_argument("--from", dest="source", metavar="FOLDER",
                   help="build from another checkout (worktree) of the project's repository")
    s.add_argument("--mode", default="scripted", choices=["scripted"])
    s.add_argument("--by", default="cli", choices=["console", "cli", "claude"])
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--detach", action="store_true")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("stop")
    s.add_argument("run", nargs="?")
    s.add_argument("--all", action="store_true")
    s.add_argument("--wait", type=float, default=0, help="seconds to wait for the run to end")
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("status")
    s.add_argument("run", nargs="?")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("report")
    s.add_argument("run")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("verify")
    s.add_argument("project", nargs="?")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("triage")
    s.add_argument("run")
    s.add_argument("flow")
    s.add_argument("--mark", required=True, choices=TRIAGE_MARKS)
    s.add_argument("--by", required=True)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_triage)
    s = sub.add_parser("watch")
    s.add_argument("run")
    s.add_argument("--quiet", action="store_true", help="only failures, limits and the result")
    s.add_argument("--poll", type=float, default=0.5, help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("brief")
    s.add_argument("project", nargs="?")
    s.add_argument("--line", action="store_true", help="one line, for a status line")
    s.set_defaults(fn=cmd_brief)
    _bug_parser(sub)
    sub.add_parser("clean").set_defaults(fn=cmd_clean)
    s = sub.add_parser("prune")
    s.add_argument("project")
    s.add_argument("--keep", type=int, required=True)
    s.set_defaults(fn=cmd_prune)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # --json is accepted anywhere on the line, before or after the command.
    as_json = "--json" in argv
    args = parser().parse_args([a for a in argv if a != "--json"])
    args.json = as_json
    try:
        return args.fn(args)
    except (CliError, config.ConfigError, lifecycle.RunError, ValueError) as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(e)}))
        else:
            print("e2e: %s" % e, file=sys.stderr)
        return 1
