"""One run, start to read-only report (CONTRACT.md "Run lifecycle")."""

import math
import os
import shutil
import signal
import subprocess
import threading
import time
import traceback

from . import config, locks, store
from .scheduler import FAILED, Scheduler

DEFAULT_FLOW_S = {"maestro": 40, "xcuitest": 60}
DEFAULT_BUILD_S = 180


class RunError(Exception):
    pass


def git_info(repo):
    def git(*args):
        try:
            return subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                                  text=True, timeout=10).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return git("rev-parse", "--short", "HEAD"), git("rev-parse", "--abbrev-ref", "HEAD")


def last_report(project):
    for rid in reversed(store.run_ids(project)):
        rep = store.read_json(os.path.join(store.run_dir(rid), "report.json"))
        if rep:
            return rep
    return None


def estimate(app, suites):
    """Seconds for build + flows spread over the sims, from the last report's
    durations where it has them."""
    prev = last_report(app["project"]) or {}
    known = {f["id"]: f.get("duration_s") for f in prev.get("flows", []) if f.get("duration_s")}
    build_s = prev.get("build_s") or DEFAULT_BUILD_S
    total, flows = 0, 0
    for s in suites:
        for f in config.list_flows(app, s):
            total += known.get(f["id"]) or DEFAULT_FLOW_S[f["engine"]]
            flows += 1
    sims = app["limits"]["sims"]
    est = int(build_s + math.ceil(total / float(sims)) + 30 * sims)
    return {"estimate_s": min(est, app["limits"]["run"]), "flows": flows, "sims": sims,
            "build_s": int(build_s)}


class Run(object):
    """Owns state.json / events.ndjson for one run."""

    def __init__(self, app, suites, run_id, backend, project_lock, request):
        self.app = app
        self.project = app["project"]
        self.suites = suites
        self.id = run_id
        self.dir = store.run_dir(run_id)
        self.backend = backend
        self.limits = app["limits"]
        self.project_lock = project_lock
        self.request = request
        self.mu = threading.RLock()
        self.stopping = False
        self.artifacts = None
        self.sim_locks = []
        self.build_lock = None
        self.t0 = time.time()
        self.deadline = self.t0 + self.limits["run"]
        self.limit_skips = set()
        self.extra = {}  # per-flow crash/repro, kept out of state.json
        quarantine = store.load_quarantine(self.project)
        self.flows = []
        for s in suites:
            self.flows.extend(config.list_flows(app, s))
        self.state = {
            "run": run_id, "project": self.project, "status": "queued",
            "phase": "starting", "started": store.now_iso(), "updated": store.now_iso(),
            "finished": None, "estimate_s": estimate(app, suites)["estimate_s"], "elapsed_s": 0,
            "sims": [], "counts": {},
            "flows": [{"id": f["id"], "suite": f["suite"], "engine": f["engine"],
                       "status": "pending", "attempts": 0, "duration_s": None, "error": None,
                       "quarantined": quarantine.get(f["id"], {}).get("flaky_runs", 0)
                       >= config.QUARANTINE_AFTER}
                      for f in self.flows],
            "pid": os.getpid(),
        }
        self._count()

    # --- state ------------------------------------------------------------

    def _count(self):
        c = {"total": len(self.state["flows"]), "passed": 0, "failed": 0, "flaky": 0,
             "skipped": 0, "running": 0, "pending": 0}
        for f in self.state["flows"]:
            st = f["status"]
            c["failed" if st in FAILED else st] = c.get("failed" if st in FAILED else st, 0) + 1
        self.state["counts"] = c

    def save(self):
        with self.mu:
            self.state["updated"] = store.now_iso()
            self.state["elapsed_s"] = int(time.time() - self.t0)
            self._count()
            store.atomic_write_json(os.path.join(self.dir, "state.json"), self.state)

    def event(self, type_, **kw):
        ev = {"ts": store.now_iso(), "type": type_, "run": self.id}
        ev.update(kw)
        store.append_ndjson(os.path.join(self.dir, "events.ndjson"), ev)

    def phase(self, status, phase):
        with self.mu:
            self.state["status"] = status
            self.state["phase"] = phase
        self.save()

    def set_flow(self, flow_id, limit_skip=False, crash=None, repro=None, **fields):
        with self.mu:
            for f in self.state["flows"]:
                if f["id"] == flow_id:
                    f.update(fields)
            if limit_skip:
                self.limit_skips.add(flow_id)
            if crash is not None or repro is not None:
                self.extra[flow_id] = {"crash": crash, "repro": repro or []}
        self.save()

    def sim_flow(self, slot, flow_id):
        with self.mu:
            for s in self.state["sims"]:
                if s["slot"] == slot:
                    s["flow"] = flow_id
                    s["since"] = store.now_iso()
        self.save()

    def should_stop(self):
        return self.stopping

    def request_stop(self, signum=None, frame=None):
        # Signal handler: only flip the flag. Every wait loop polls it, and the
        # main thread does the killing in normal (non-handler) context.
        if not self.stopping:
            self.stopping = True
            try:
                self.event("stop_requested", signal=signum)
            except OSError:
                pass

    # --- lifecycle --------------------------------------------------------

    def _in_thread(self, fn, *args):
        """Run a blocking step off the main thread so a stop can interrupt it."""
        box = {}

        def target():
            try:
                box["value"] = fn(*args)
            except BaseException as e:
                box["error"] = e
        t = threading.Thread(target=target, daemon=True)
        t.start()
        while t.is_alive():
            if self.stopping:
                self.backend.kill_all()
            t.join(0.2)
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _ticker(self, done):
        # ~1 fps live screenshots, plus a heartbeat so elapsed_s moves.
        live = os.path.join(self.dir, "live")
        os.makedirs(live, exist_ok=True)
        while not done.wait(1.0):
            with self.mu:
                sims = list(self.state["sims"])
            for s in sims:
                try:
                    self.backend.screenshot(s["udid"], os.path.join(live, "sim-%d.png" % s["slot"]))
                except Exception:
                    pass
            try:
                self.save()
            except OSError:
                pass

    def execute(self):
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.save()
        self.event("queued", suites=self.suites, dry_run=self.backend.dry_run)
        for hit in self.app["limit_hits"]:
            self.event("limit_hit", text="app.yml %(limit)s=%(asked)s clamped to %(used)s" % hit, **hit)
        done = threading.Event()
        ticker = threading.Thread(target=self._ticker, args=(done,), daemon=True)
        error = None
        try:
            self._body(ticker)
        except Exception as e:
            error = e
            self.event("log", level="error", text="%s\n%s" % (e, traceback.format_exc()[-2000:]))
        finally:
            done.set()
            # A frame still in flight would land in live/ after we remove it.
            if ticker.ident is not None:
                ticker.join(20)
            self._finalize(error)
        return self.state["status"]

    def _body(self, ticker):
        self.phase("queued", "retention")
        removed = store.retention(self.project, keep_run=self.id)
        if removed:
            self.event("log", text="retention removed %d item(s) from earlier runs" % removed)

        self.phase("queued", "waiting for build slot")
        self.build_lock = locks.build_lock()
        if not self.build_lock.acquire(self.id, should_stop=self.should_stop):
            return
        try:
            self.phase("building", "build-for-testing")
            self.event("build_start")
            tb = time.time()
            self.artifacts = self._in_thread(
                self.backend.build, self.app, self.dir, max(1, int(self.deadline - time.time())))
            # Golden creation rides on the build slot so two runs never race
            # to create the same golden simulator.
            golden = self._in_thread(self.backend.prepare_golden, self.app)
            self.extra["_build_s"] = round(time.time() - tb, 1)
            self.event("build_end", duration_s=self.extra["_build_s"])
        finally:
            self.build_lock.release()
        if self.stopping:
            return

        self._sims_up(golden)
        if self.stopping or not self.state["sims"]:
            return
        ticker.start()
        self.phase("running", "running flows")
        Scheduler(self, self.flows, list(self.state["sims"])).execute()

    def _sims_up(self, golden):
        want = self.limits["sims"]
        self.phase("queued", "waiting for simulator slot")
        while not self.stopping:
            got = locks.acquire_sims(want, self.id)
            if got:
                break
            if time.time() > self.deadline:
                raise RunError("no simulator slot before the run time limit")
            time.sleep(0.5)
        else:
            return
        self.sim_locks = got
        self.phase("running", "starting simulators")
        for i, (slot, lk) in enumerate(got, 1):
            ram = self.backend.machine().get("ram_free_gb")
            while ram is not None and ram < config.RAM_FREE_GB:
                if self.state["sims"]:
                    break  # we have one; run with what we've got
                self.phase("queued", "waiting for %.0f GB free RAM" % config.RAM_FREE_GB)
                if self.stopping or time.time() > self.deadline:
                    return
                time.sleep(1)
                ram = self.backend.machine().get("ram_free_gb")
            if ram is not None and ram < config.RAM_FREE_GB:
                self.event("limit_hit", limit="ram", ram_free_gb=ram,
                           text="only %.1f GB free; not booting simulator %d" % (ram, slot))
                lk.release()
                continue
            name = "e2e-%s-%d" % (self.id, i)
            udid = self._in_thread(self.backend.sim_up, self.app, self.artifacts, golden, name)
            with self.mu:
                self.state["sims"].append({"slot": slot, "udid": udid, "name": name,
                                           "flow": None, "since": store.now_iso()})
            self.event("sim_up", sim=slot, udid=udid, name=name)
            self.save()
            if self.stopping:
                return
        self.sim_locks = [(s, l) for s, l in got if l.held()]

    def _final_status(self, error):
        if self.stopping:
            return "stopped"
        if error is not None:
            return "error"
        for f in self.state["flows"]:
            if f["quarantined"]:
                continue
            if f["status"] in FAILED or f["id"] in self.limit_skips:
                return "failed"
        return "passed"

    def _finalize(self, error):
        with self.mu:
            self.state["status"] = "finishing"
            self.state["phase"] = "cleaning up"
        self._safe(self.save)
        self._safe(self.backend.kill_all)
        for s in list(self.state["sims"]):
            self._safe(self.backend.sim_down, s["udid"])
            self._safe(self.event, "sim_down", sim=s["slot"], udid=s["udid"])
        with self.mu:
            self.state["sims"] = []
        for _, lk in self.sim_locks:
            lk.release()
        if self.build_lock:
            self.build_lock.release()
        shutil.rmtree(os.path.join(self.dir, "live"), ignore_errors=True)

        with self.mu:
            never_ran = 0
            for f in self.state["flows"]:
                if f["status"] in ("pending", "running"):
                    f["status"] = "skipped"
                    never_ran += 1
            # Flows left over without a stop or a timer (no sim ever came up,
            # say) mean the run broke, not that it passed.
            if never_ran and error is None and not self.stopping:
                error = RunError("%d flow(s) never ran" % never_ran)
            status = self._final_status(error)
            self.state["status"] = status
            self.state["phase"] = "error: %s" % error if error is not None else "done"
            self.state["finished"] = store.now_iso()
        self._safe(self.save)
        self._safe(self.event, "done", status=status, counts=self.state["counts"])
        self._safe(self._report)
        self._safe(store.make_read_only, self.dir)
        self._safe(self._history)
        self._safe(self._quarantine)
        self.project_lock.release()

    def _safe(self, fn, *args, **kw):
        # Cleanup keeps going past any one failure: sims and locks matter more
        # than a perfect report.
        try:
            fn(*args, **kw)
        except Exception as e:
            try:
                self.event("log", level="error", text="cleanup %s: %s" % (fn.__name__, e))
            except Exception:
                pass

    def _report(self):
        rep = dict(self.state)
        rep["build_s"] = self.extra.get("_build_s")
        rep["failures"] = []
        for f in self.state["flows"]:
            if f["status"] not in FAILED:
                continue
            fdir = os.path.join("flows", f["id"])
            extra = self.extra.get(f["id"], {})
            excerpt = ""
            try:
                with open(os.path.join(self.dir, fdir, "log.txt"), errors="replace") as fh:
                    excerpt = "".join(fh.readlines()[-40:])
            except OSError:
                pass
            rel = lambda name: (os.path.join(fdir, name)  # noqa: E731
                                if os.path.exists(os.path.join(self.dir, fdir, name)) else None)
            rep["failures"].append({
                "flow": f["id"], "kind": f["status"], "message": f["error"],
                "screenshot": rel("screenshot.png"), "video": rel("video.mp4"),
                "log_excerpt": excerpt, "crash": extra.get("crash"),
                "repro": extra.get("repro", []), "quarantined": f["quarantined"],
            })
        rep["limits"] = {"flow_s": self.limits["flow"], "suite_s": self.limits["suite"],
                         "run_s": self.limits["run"], "sims": self.limits["sims"],
                         "retries": self.limits["retries"]}
        m = {}
        try:
            m = self.backend.machine()
        except Exception:
            pass
        rep["machine"] = {"ram_free_gb": m.get("ram_free_gb"), "disk_free_gb": m.get("disk_free_gb")}
        rep["dry_run"] = self.backend.dry_run
        store.atomic_write_json(os.path.join(self.dir, "report.json"), rep)

    def _history(self):
        path = os.path.join(self.dir, "report.json")
        store.append_ndjson(os.path.join(store.project_dir(self.project), "history.ndjson"), {
            "run": self.id, "finished": self.state["finished"], "status": self.state["status"],
            "counts": self.state["counts"], "sha256": store.sha256_file(path)})

    def _quarantine(self):
        flaky = [f["id"] for f in self.state["flows"] if f["status"] == "flaky"]
        if not flaky:
            return
        q = store.load_quarantine(self.project)
        for fid in flaky:
            entry = q.setdefault(fid, {"flaky_runs": 0, "since": store.now_iso()})
            entry["flaky_runs"] += 1
        store.save_quarantine(self.project, q)


def create(app, suites, backend, requested_by, mode="scripted"):
    """Steps 1–2 plus request.json: validate, disk, project lock, run folder.

    Returns a Run ready to execute(); the caller may fork first (--detach),
    the project lock is inherited by the child.
    """
    if mode != "scripted":
        raise RunError("unknown mode %r (only: scripted)" % mode)
    disk = backend.machine().get("disk_free_gb")
    if disk is not None and disk < config.DISK_FREE_GB:
        raise RunError("only %.1f GB disk free; need %d GB to start a run" % (disk, config.DISK_FREE_GB))
    for s in suites:
        config.list_flows(app, s)  # fail before taking anything
    plock = locks.project_lock(app["project"])
    if not plock.try_acquire("run"):
        raise RunError("%s already has a run in progress" % app["project"])
    try:
        run_id = store.next_run_id(app["project"])
        rdir = store.ensure_dir(store.run_dir(run_id))
        commit, branch = git_info(app["repo"])
        request = {"run": run_id, "project": app["project"], "suites": suites, "mode": mode,
                   "requested_by": requested_by, "requested_at": store.now_iso(),
                   "commit": commit, "branch": branch, "dry_run": backend.dry_run}
        store.atomic_write_json(os.path.join(rdir, "request.json"), request)
        return Run(app, suites, run_id, backend, plock, request)
    except BaseException:
        plock.release()
        raise


def finalize_dead(run_id):
    """Close out a run whose engine died without cleaning up (SIGKILL,
    power loss): mark it error, drop live/, write report, make read-only."""
    rdir = store.run_dir(run_id)
    state = store.read_json(os.path.join(rdir, "state.json"))
    if not state or state.get("status") in store.TERMINAL or os.path.exists(os.path.join(rdir, "report.json")):
        return False
    shutil.rmtree(os.path.join(rdir, "live"), ignore_errors=True)
    for f in state.get("flows", []):
        if f["status"] in ("pending", "running"):
            f["status"] = "skipped"
    state.update({"status": "error", "phase": "engine died; closed by e2e clean",
                  "finished": store.now_iso(), "sims": []})
    store.atomic_write_json(os.path.join(rdir, "state.json"), state)
    rep = dict(state, failures=[], limits=None, machine=None)
    store.atomic_write_json(os.path.join(rdir, "report.json"), rep)
    store.append_ndjson(os.path.join(rdir, "events.ndjson"),
                        {"ts": store.now_iso(), "type": "done", "run": run_id, "status": "error"})
    store.make_read_only(rdir)
    store.append_ndjson(os.path.join(store.project_dir(state["project"]), "history.ndjson"), {
        "run": run_id, "finished": state["finished"], "status": "error",
        "counts": state.get("counts"), "sha256": store.sha256_file(os.path.join(rdir, "report.json"))})
    return True
