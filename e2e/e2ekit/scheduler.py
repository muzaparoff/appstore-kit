"""Hands flows to free simulators: per-flow timeout, retries, suite and run
timers. One worker thread per simulator; the run owns all shared state."""

import os
import shutil
import threading
import time

FAILED = ("failed", "timeout", "crashed")


class Scheduler(object):
    def __init__(self, run, flows, sims):
        self.run = run          # lifecycle.Run: state, events, backend, limits
        self.queue = list(flows)
        self.sims = sims        # [{"slot", "udid", ...}] already booted
        self.mu = threading.Lock()
        self.suite_started = {}

    def _next(self):
        """Pop the next runnable flow and its time budget, skipping flows
        whose suite or run timer has already run out."""
        run = self.run
        while True:
            with self.mu:
                if not self.queue or run.stopping:
                    return None, 0
                flow = self.queue.pop(0)
                now = time.time()
                started = self.suite_started.setdefault(flow["suite"], now)
            budget = run.app["suites"][flow["suite"]]["budget"]
            suite_left = started + budget - now
            run_left = run.deadline - now
            if suite_left <= 0 or run_left <= 0:
                which = "run" if run_left <= 0 else "suite"
                run.set_flow(flow["id"], status="skipped",
                             error="%s time limit reached" % which, limit_skip=True)
                run.event("limit_hit", limit=which, flow=flow["id"],
                          suite=flow["suite"], text="%s timer ran out; skipped" % which)
                continue
            return flow, int(max(1, min(run.limits["flow"], suite_left, run_left)))

    def _worker(self, sim):
        run = self.run
        while True:
            flow, timeout = self._next()
            if flow is None:
                return
            self._run_flow(sim, flow, timeout)

    def _run_flow(self, sim, flow, timeout):
        run = self.run
        outdir = os.path.join(run.dir, "flows", flow["id"])
        os.makedirs(outdir, exist_ok=True)
        run.sim_flow(sim["slot"], flow["id"])
        steps = []

        def on_step(text):
            steps.append(text)
            del steps[:-20]
            run.event("step", sim=sim["slot"], flow=flow["id"], text=text)

        status, res, t0 = "failed", {}, time.time()
        attempts = 1 + run.limits["retries"]
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                _shelve_attempt(outdir, attempt - 1)
                steps[:] = []
            run.set_flow(flow["id"], status="running", attempts=attempt)
            run.event("flow_start", flow=flow["id"], sim=sim["slot"], attempt=attempt)
            t_attempt = time.time()
            try:
                res = run.backend.run_flow(run.app, run.artifacts, flow, sim["udid"], outdir,
                                           timeout, attempt, on_step, run.limits)
            except Exception as e:  # a broken tool call fails the flow, not the run
                res = {"status": "failed", "message": "engine: %s" % e, "crash": None}
            status = res["status"]
            run.event("flow_end", flow=flow["id"], sim=sim["slot"], attempt=attempt,
                      status=status, duration_s=round(time.time() - t_attempt, 1))
            if status == "passed" or run.stopping:
                break
            # The timer, not the flow, decided a timeout when the budget was
            # cut short; retrying would only hit the same wall.
            if status == "timeout" and timeout < run.limits["flow"]:
                break
        if status == "passed" and attempt > 1:
            status = "flaky"
        if run.stopping and status != "passed" and status != "flaky":
            status = "skipped"
            res = dict(res, message="run stopped")
        run.set_flow(flow["id"], status=status, attempts=attempt,
                     duration_s=round(time.time() - t0, 1),
                     error=None if status in ("passed", "flaky") else res.get("message"),
                     crash=res.get("crash"), repro=list(steps))
        run.sim_flow(sim["slot"], None)

    def execute(self):
        threads = [threading.Thread(target=self._worker, args=(s,), daemon=True,
                                    name="sim-%d" % s["slot"]) for s in self.sims]
        for t in threads:
            t.start()
        # The main thread only polls, so SIGTERM is noticed within a beat and
        # it can kill the children the workers are blocked on.
        while any(t.is_alive() for t in threads):
            if self.run.stopping:
                self.run.backend.kill_all()
            for t in threads:
                t.join(0.2)
        with self.mu:
            left, self.queue = self.queue, []
        for flow in left:
            self.run.set_flow(flow["id"], status="skipped", error="run stopped")


def _shelve_attempt(outdir, n):
    """Keep the failed attempt's evidence next to the retry, for triage."""
    dest = os.path.join(outdir, "attempt-%d" % n)
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(outdir):
        if name.startswith("attempt-"):
            continue
        shutil.move(os.path.join(outdir, name), os.path.join(dest, name))
