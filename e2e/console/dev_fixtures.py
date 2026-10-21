#!/usr/bin/env python3
"""Fixture $E2E_HOME for developing and testing the console without the engine.

    python3 e2e/console/dev_fixtures.py make <dir>        # build a fixture runs root
    python3 e2e/console/dev_fixtures.py animate <dir> <run-id> [seconds]

`make` also writes <dir>/fake-bin/e2e, a stand-in CLI (projects, plan, status,
run --detach, stop) so the console runs its real subprocess path against it:
    E2E_HOME=<dir> E2E_BIN=<dir>/fake-bin/e2e python3 -m console
Everything follows e2e/CONTRACT.md; nothing here touches simulators or Xcode.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS = {
    "camipack": {"suites": {
        "smoke": {"engine": "maestro", "flows": "e2e/flows/smoke", "budget": "5m"},
        "trips": {"engine": "maestro", "flows": "e2e/flows/trips", "budget": "12m"},
        "budget": {"engine": "maestro", "flows": "e2e/flows/budget", "budget": "10m"},
        "paywall": {"engine": "xcuitest", "tests": ["CamipackUITests/PaywallTests"], "budget": "8m"}},
        "limits": {"flow_s": 240, "suite_s": 1500, "run_s": 5400, "sims": 2, "retries": 1},
        "branch": "main", "commit": "79907d9"},
    "babylogly": {"suites": {
        "smoke": {"engine": "xcuitest", "tests": ["BabyLoglyUITests/SmokeTests"], "budget": "5m"},
        "feeds": {"engine": "xcuitest", "tests": ["BabyLoglyUITests/FeedTests"], "budget": "8m"}},
        "branch": "main", "commit": "a1b2c3d"},
}
FLOWS = {
    "smoke": ["smoke/01-launch", "smoke/02-tabs"],
    "trips": ["trips/01-create-trip", "trips/02-open-from-link", "trips/05-delete-joined-flaky", "trips/07-rename"],
    "budget": ["budget/03-huge-amount-fail", "budget/04-split-edit", "budget/05-currency-slow"],
    "paywall": ["paywall/PaywallTests.testPurchase"],
    "feeds": ["feeds/FeedTests.testAddFeed", "feeds/FeedTests.testEditFeed"],
}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now():
    return datetime.now(timezone.utc)


def png(path, rgb, w=118, h=256):
    """Solid-colour PNG with a darker top band, so live tiles visibly change."""
    rows = b""
    for y in range(h):
        c = tuple(int(v * (0.6 if y < 30 else 1)) for v in rgb)
        rows += b"\x00" + bytes(c) * w
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)  # atomic, like the engine


def append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def flow_status(fid):
    if "fail" in fid:
        return "failed"
    if "flaky" in fid:
        return "flaky"
    return "passed"


def counts(flows):
    c = {"total": len(flows), "passed": 0, "failed": 0, "flaky": 0, "skipped": 0, "running": 0, "pending": 0}
    for f in flows:
        k = {"timeout": "failed", "crashed": "failed"}.get(f["status"], f["status"])
        c[k] = c.get(k, 0) + 1
    return c


def lock_down(d):
    for root, dirs, files in os.walk(d):
        for f in files:
            os.chmod(os.path.join(root, f), 0o444)
        for x in dirs:
            os.chmod(os.path.join(root, x), 0o555)
    os.chmod(d, 0o555)


def unlock(d):
    for root, dirs, files in os.walk(d):
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)


def finish(home, run_id, status=None, tamper=False):
    """Write report.json once, append history with its sha256, chmod read-only."""
    project = run_id.rsplit("-", 1)[0]
    d = home / project / "runs" / run_id
    state = json.loads((d / "state.json").read_text())
    for f in state["flows"]:
        if f["status"] in ("pending", "running"):
            f["status"] = "skipped" if status == "stopped" else flow_status(f["id"])
    failures = []
    for f in state["flows"]:
        fd = d / "flows" / f["id"]
        fd.mkdir(parents=True, exist_ok=True)
        ok = f["status"] in ("passed", "flaky")
        (fd / "junit.xml").write_text(
            '<?xml version="1.0"?><testsuite name="%s" tests="1" failures="%d"><testcase name="%s"/></testsuite>\n'
            % (f["suite"], 0 if ok else 1, f["id"]))
        if f["status"] == "failed":
            f["error"] = "Assertion failed: visible \"Sergey owes 60.25\" not found in 10 s"
            png(fd / "screenshot.png", (179, 38, 30))
            (fd / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            (fd / "log.txt").write_text("".join("12:00:%02d app log line %d\n" % (i, i) for i in range(40)))
            failures.append({"flow": f["id"], "kind": "crashed" if "huge" in f["id"] else "failed",
                             "message": "Typed 1e309 in the amount field and the app crashed in SplitBoard.swift:25 (Int(amount*100)).",
                             "screenshot": "flows/%s/screenshot.png" % f["id"], "video": "flows/%s/video.mp4" % f["id"],
                             "log_excerpt": "".join("12:00:%02d app log line %d\n" % (i, i) for i in range(30, 40)),
                             "crash": "Fatal error: Double value cannot be converted to Int because it is either infinite or NaN\nSplitBoard.swift:25",
                             "repro": ["Open the Rome trip", "Budget > Add expense", "Type 1e309 in Amount", "Tap Save"]})
    state["counts"] = counts(state["flows"])
    state["status"] = status or ("failed" if state["counts"]["failed"] else "passed")
    state["phase"] = "done"
    state["finished"] = state["updated"] = iso(now())
    state["sims"] = []
    state["counts"]["running"] = state["counts"]["pending"] = 0
    report = dict(state, failures=failures,
                  limits={"flow_s": 240, "suite_s": 1500, "run_s": 5400, "sims": 2, "retries": 1},
                  machine={"ram_free_gb": 5.1, "disk_free_gb": 31.0})
    shutil.rmtree(d / "live", ignore_errors=True)
    raw = json.dumps(report, indent=1).encode()
    (d / "report.json").write_bytes(raw)
    write_json(d / "state.json", state)
    append(d / "events.ndjson", {"ts": iso(now()), "type": "done", "run": run_id, "status": state["status"]})
    append(home / project / "history.ndjson", {"run": run_id, "finished": state["finished"], "status": state["status"],
                                                "counts": state["counts"], "sha256": hashlib.sha256(raw).hexdigest()})
    if tamper:
        (d / "report.json").write_bytes(raw.replace(b'"failed"', b'"passed"', 1) + b"\n")
    lock_down(d)


def new_run(home, project, suites, by, dry_run=True, status="queued", pid=None):
    pdir = home / project / "runs"
    pdir.mkdir(parents=True, exist_ok=True)
    n = 1 + max([int(p.name.rsplit("-", 1)[1]) for p in pdir.iterdir() if p.name.rsplit("-", 1)[-1].isdigit()] or [0])
    run_id = "%s-%d" % (project, n)
    d = pdir / run_id
    d.mkdir()
    t = iso(now())
    info = PROJECTS.get(project, {})
    write_json(d / "request.json", {"run": run_id, "project": project, "suites": suites, "mode": "scripted",
                                    "requested_by": by, "requested_at": t, "commit": info.get("commit", "0000000"),
                                    "branch": info.get("branch", "main"), "dry_run": dry_run})
    flows = [{"id": f, "suite": s, "engine": info["suites"][s]["engine"], "status": "pending", "attempts": 0,
              "duration_s": None, "error": None, "quarantined": False} for s in suites for f in FLOWS.get(s, [])]
    write_json(d / "state.json", {"run": run_id, "project": project, "status": status, "phase": "waiting for build slot",
                                  "started": t, "updated": t, "finished": None, "estimate_s": 40 * len(flows) + 60,
                                  "elapsed_s": 0, "sims": [], "counts": counts(flows), "flows": flows,
                                  "pid": pid or os.getpid()})
    append(d / "events.ndjson", {"ts": t, "type": "queued", "run": run_id})
    return run_id


def animate(home, run_id, seconds=30):
    """Walk a run through build -> flows -> report, writing state/events/live like the engine."""
    project = run_id.rsplit("-", 1)[0]
    d = home / project / "runs" / run_id
    sp = d / "state.json"
    ev = d / "events.ndjson"
    stopped = [False]

    def on_term(*_):
        stopped[0] = True
    import signal
    signal.signal(signal.SIGTERM, on_term)
    st = json.loads(sp.read_text())
    st["pid"] = os.getpid()
    t0 = time.time()

    def save(**kw):
        st.update(kw, updated=iso(now()), elapsed_s=int(time.time() - t0))
        st["counts"] = counts(st["flows"])
        write_json(sp, st)

    save(status="building", phase="build-for-testing")
    append(ev, {"ts": iso(now()), "type": "build_start", "run": run_id})
    time.sleep(min(3, seconds / 6))
    append(ev, {"ts": iso(now()), "type": "build_end", "run": run_id})
    sims = [{"slot": k, "udid": "FAKE-%d" % k, "name": "e2e-%s-%d" % (run_id, k), "flow": None, "since": iso(now())} for k in (1, 2)]
    for s in sims:
        append(ev, {"ts": iso(now()), "type": "sim_up", "run": run_id, "sim": s["slot"]})
    save(status="running", phase="running flows", sims=sims)
    per = max(1.0, seconds / max(1, len(st["flows"])) * 2)
    queue = list(st["flows"])
    colors = [(26, 107, 71), (122, 90, 248), (14, 116, 144), (194, 65, 12)]
    while queue and not stopped[0]:
        batch = [queue.pop(0) for _ in range(min(2, len(queue)))]
        for s, f in zip(sims, batch):
            f["status"], f["attempts"] = "running", 1
            s["flow"] = f["id"]
            append(ev, {"ts": iso(now()), "type": "flow_start", "run": run_id, "sim": s["slot"], "flow": f["id"]})
        save()
        steps = int(per)
        for i in range(steps):
            if stopped[0]:
                break
            for s, f in zip(sims, batch):
                png(d / "live" / ("sim-%d.png" % s["slot"]), colors[(i + s["slot"]) % len(colors)])
                append(ev, {"ts": iso(now()), "type": "step", "run": run_id, "sim": s["slot"],
                            "text": "tap id=%s.step%d" % (f["id"].split("/")[0], i)})
            save()
            time.sleep(1)
        for s, f in zip(sims, batch):
            if stopped[0]:
                break
            f["status"], f["duration_s"] = flow_status(f["id"]), steps
            append(ev, {"ts": iso(now()), "type": "flow_end", "run": run_id, "sim": s["slot"], "flow": f["id"], "status": f["status"]})
        save()
    if stopped[0]:
        append(ev, {"ts": iso(now()), "type": "stop_requested", "run": run_id})
    for s in sims:
        append(ev, {"ts": iso(now()), "type": "sim_down", "run": run_id, "sim": s["slot"]})
    save(status="finishing", phase="writing report")
    finish(home, run_id, "stopped" if stopped[0] else None)


FAKE_CLI = '''#!/usr/bin/env python3
# Stand-in for bin/e2e, generated by e2e/console/dev_fixtures.py. Dev only.
import json, os, signal, subprocess, sys
sys.path.insert(0, %(console_dir)r)
import dev_fixtures as fx
from pathlib import Path
home = Path(os.environ.get("E2E_HOME", %(home)r))
a = [x for x in sys.argv[1:] if x != "--json"]
def out(o): print(json.dumps(o)); sys.exit(0)
def die(m): print(json.dumps({"error": m})); sys.exit(2)
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
cmd = a[0] if a else ""
if cmd == "projects":
    out([dict(v, name=k, path="/fake/" + k) for k, v in fx.PROJECTS.items()])
if cmd == "plan":
    s = opt("--suite", "").split(",")
    n = sum(len(fx.FLOWS.get(x, [])) for x in s)
    out({"project": a[1], "suites": s, "estimate_s": 40 * n + 60, "sims": 2, "needs_build": True})
if cmd == "status":
    out({"machine": {"sims_cap": 3, "sims_busy": [], "build_busy": False}, "runs": []})
if cmd == "triage":
    rid, flow, mark = a[1], a[2], opt("--mark")
    if mark not in ("fix", "not-a-bug", "flaky"): die("bad mark")
    e = {"ts": fx.iso(fx.now()), "run": rid, "flow": flow, "mark": mark, "by": opt("--by"), "note": opt("--note", "")}
    fx.append(home / rid.rsplit("-", 1)[0] / "triage.ndjson", e)
    out(e)
if cmd == "run":
    p, s = a[1], opt("--suite", "").split(",")
    if p not in fx.PROJECTS or any(x not in fx.PROJECTS[p]["suites"] for x in s): die("bad project/suite")
    rid = fx.new_run(home, p, s, opt("--by", "cli"), "--dry-run" in a)
    proc = subprocess.Popen([sys.executable, fx.__file__, "animate", str(home), rid, "20"],
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out({"run": rid, "pid": proc.pid})
if cmd == "stop":
    st = json.loads((home / a[1].rsplit("-", 1)[0] / "runs" / a[1] / "state.json").read_text())
    try: os.killpg(st["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError): os.kill(st["pid"], signal.SIGTERM)
    out({"run": a[1], "stopping": True})
die("unknown command " + cmd)
'''


def make(home):
    home = Path(home)
    if home.exists():
        unlock(home)
        shutil.rmtree(home)
    (home / "engine").mkdir(parents=True)
    for name in ("build.lock", "sim-1.lock", "sim-2.lock", "sim-3.lock", "project-camipack.lock"):
        (home / "engine" / name).touch()
    write_json(home / "engine" / "golden.json", {"iPhone 17e|iOS 27.0": "11111111-2222-3333-4444-555555555555"})
    write_json(home / "projects.json", [{"name": k, "path": "/fake/" + k, "added": iso(now())} for k in PROJECTS])
    # Finished runs: one passed, one tampered, one failed (with triage on it).
    for suites, kwargs in ((["smoke"], {}), (["smoke", "trips"], {"tamper": True}), (["trips", "budget"], {})):
        rid = new_run(home, "camipack", suites, "cli")
        st = json.loads((home / "camipack/runs" / rid / "state.json").read_text())
        if kwargs.get("tamper"):
            # Give the tampered run a failure, so the edit (failed -> passed) means something.
            st["flows"][0]["id"] = "smoke/01-launch-fail"
        for f in st["flows"]:
            f["status"], f["attempts"], f["duration_s"] = flow_status(f["id"]), 1, 30
        write_json(home / "camipack/runs" / rid / "state.json", st)
        finish(home, rid, **kwargs)
    append(home / "camipack" / "triage.ndjson", {"ts": iso(now() - timedelta(minutes=5)), "run": "camipack-3",
                                                 "flow": "budget/03-huge-amount-fail", "mark": "fix", "by": "cli", "note": ""})
    # Live runs (pid 1 = always alive, so they don't show as "process gone"): camipack started from the console (stoppable), babylogly from the CLI (not).
    live = new_run(home, "camipack", ["trips", "budget"], "console", status="running", pid=1)
    st = json.loads((home / "camipack/runs" / live / "state.json").read_text())
    st.update(phase="running flows", elapsed_s=702, estimate_s=1140,
              sims=[{"slot": 1, "udid": "FAKE-1", "name": "e2e-%s-1" % live, "flow": "trips/07-rename", "since": iso(now())},
                    {"slot": 2, "udid": "FAKE-2", "name": "e2e-%s-2" % live, "flow": "budget/04-split-edit", "since": iso(now())}])
    for f in st["flows"]:
        f["status"] = {"trips/07-rename": "running", "budget/04-split-edit": "running",
                       "budget/05-currency-slow": "pending"}.get(f["id"], flow_status(f["id"]))
    st["counts"] = counts(st["flows"])
    write_json(home / "camipack/runs" / live / "state.json", st)
    png(home / "camipack/runs" / live / "live/sim-1.png", (26, 107, 71))
    png(home / "camipack/runs" / live / "live/sim-2.png", (122, 90, 248))
    for i in range(5):
        append(home / "camipack/runs" / live / "events.ndjson",
               {"ts": iso(now()), "type": "step", "run": live, "sim": 2, "text": "tap id=budget.split.amount #%d" % i})
    b = new_run(home, "babylogly", ["smoke"], "cli", status="building", pid=1)
    st = json.loads((home / "babylogly/runs" / b / "state.json").read_text())
    st.update(phase="build-for-testing", elapsed_s=120, estimate_s=360)
    write_json(home / "babylogly/runs" / b / "state.json", st)
    fake = home / "fake-bin" / "e2e"
    fake.parent.mkdir()
    fake.write_text(FAKE_CLI % {"console_dir": str(Path(__file__).resolve().parent), "home": str(home)})
    fake.chmod(0o755)
    return home


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "make":
        print(make(sys.argv[2]))
    elif len(sys.argv) >= 4 and sys.argv[1] == "animate":
        animate(Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 30)
    else:
        sys.exit(__doc__)
