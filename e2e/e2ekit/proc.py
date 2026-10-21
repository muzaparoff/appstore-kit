"""Child processes the engine can always kill.

Every tool runs in its own process group so a per-flow timeout takes down
Maestro's JVM and xcodebuild's helpers too, not just the parent we spawned.
"""

import os
import signal
import subprocess
import threading
import time

_live = set()
_mu = threading.Lock()


class Result(object):
    def __init__(self, code, out, timed_out, killed):
        self.code = code
        self.out = out
        self.timed_out = timed_out
        self.killed = killed


def _killpg(p, sig):
    try:
        os.killpg(p.pid, sig)
    except (OSError, ProcessLookupError):
        pass


def run(cmd, timeout=None, log_path=None, on_line=None, env=None, cwd=None, keep_out=True):
    """Run cmd, stream merged stdout/stderr to log_path and on_line."""
    log = open(log_path, "ab") if log_path else None
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                         start_new_session=True)
    with _mu:
        _live.add(p)
    out = []
    state = {"timed_out": False}

    def reader():
        for raw in iter(p.stdout.readline, b""):
            if log:
                log.write(raw)
                log.flush()
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if keep_out:
                out.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        state["timed_out"] = True
        terminate(p)
    finally:
        t.join(5)
        with _mu:
            _live.discard(p)
        if log:
            log.close()
    killed = p.returncode is not None and p.returncode < 0
    return Result(p.returncode, "\n".join(out), state["timed_out"], killed)


def check(cmd, timeout=120):
    """Run a short tool call; raise with its output on failure."""
    r = run(cmd, timeout=timeout)
    if r.code != 0:
        raise RuntimeError("%s failed (%s): %s" % (" ".join(cmd[:3]), r.code, r.out[-800:]))
    return r.out


def terminate(p, grace=5):
    _killpg(p, signal.SIGTERM)
    deadline = time.time() + grace
    while p.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if p.poll() is None:
        _killpg(p, signal.SIGKILL)
        p.wait()


def kill_all():
    with _mu:
        procs = list(_live)
    for p in procs:
        if p.poll() is None:
            terminate(p, grace=3)


def start(cmd, log_path=None):
    """Start a long-lived helper (video recording); caller stops it."""
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    p = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    with _mu:
        _live.add(p)
    return p


def stop(p, sig=signal.SIGINT, grace=5):
    if p.poll() is None:
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            p.wait(grace)
        except subprocess.TimeoutExpired:
            terminate(p)
    with _mu:
        _live.discard(p)
