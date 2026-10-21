"""The only way the console changes anything: run bin/e2e with an argv list.

Never a shell, never a string command: every value that reaches argv has
already been checked against `e2e projects --json`.
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

DEFAULT_BIN = str(Path(__file__).resolve().parents[2] / "bin" / "e2e")
ALLOWED_MODES = ("scripted",)


def e2e_bin():
    return os.environ.get("E2E_BIN", DEFAULT_BIN)


class CLIError(Exception):
    pass


def call(args, timeout=60):
    """Run `e2e <args> --json` and return the parsed JSON (or raw text)."""
    argv = [e2e_bin()] + list(args) + ["--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, shell=False)
    except FileNotFoundError:
        raise CLIError("bin/e2e not found at %s" % argv[0])
    except subprocess.TimeoutExpired:
        raise CLIError("e2e %s timed out" % args[0])
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        msg = out or (p.stderr or "").strip() or "exit %d" % p.returncode
        try:
            j = json.loads(out)
            msg = j.get("error") or j.get("message") or msg
        except (ValueError, AttributeError):
            pass
        raise CLIError(str(msg)[:500])
    try:
        return json.loads(out)
    except ValueError:
        # --detach may print a bare run-id even with --json; take the last line.
        return out.splitlines()[-1] if out else ""


_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _norm_suites(raw):
    if isinstance(raw, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in raw.items()}
    out = {}
    for s in raw or []:
        if isinstance(s, dict) and s.get("name"):
            out[str(s["name"])] = s
        elif isinstance(s, str):
            out[s] = {}
    return out


def projects():
    """{name: {...project, "suites": {suite: info}}}; tolerant of list or dict shapes."""
    def load():
        data = call(["projects"], timeout=30)
        if isinstance(data, dict):
            data = data.get("projects", data)
        if isinstance(data, dict):
            data = [dict(v, name=k) for k, v in data.items() if isinstance(v, dict)]
        out = {}
        for p in data or []:
            if isinstance(p, dict) and p.get("name"):
                p = dict(p)
                p["suites"] = _norm_suites(p.get("suites"))
                out[str(p["name"])] = p
        return out
    return cached("projects", 10, load)


def projects_or_empty():
    try:
        return projects(), None
    except CLIError as e:
        return {}, str(e)


def plan(project, suites):
    return call(["plan", project, "--suite", ",".join(suites)], timeout=60)


def start_argv(project, suites, mode):
    argv = ["run", project, "--suite", ",".join(suites), "--mode", mode,
            "--by", "console"]
    if os.environ.get("E2E_CONSOLE_DRY_RUN") == "1":
        argv.append("--dry-run")
    argv.append("--detach")
    return argv


def start(project, suites, mode):
    res = call(start_argv(project, suites, mode), timeout=60)
    clear_cache()
    if isinstance(res, dict):
        return str(res.get("run") or res.get("run_id") or res.get("id") or "")
    return str(res).strip()


def stop(run_id):
    res = call(["stop", run_id], timeout=30)
    clear_cache()
    return res


def status():
    return cached("status", 3, lambda: call(["status"], timeout=20))
