"""`bin/e2e` command line (CONTRACT.md "CLI")."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from . import build, config, lifecycle, locks, machine, store


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


def cmd_add(args):
    app = config.load_app(repo_root(args.repo))
    if args.name:
        if not config.NAME_RE.match(args.name):
            raise CliError("name must be lowercase letters, digits, - or _")
        app["project"] = args.name
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


def cmd_run(args):
    app = load_registered(args.project)
    suites = config.pick_suites(app, args.suite)
    backend = make_backend(args.dry_run)
    run = lifecycle.create(app, suites, backend, args.by, args.mode)
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
    s.add_argument("--suite", required=True)
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
