"""Read-only view of $E2E_HOME (layout in e2e/CONTRACT.md)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

TERMINAL = {"passed", "failed", "stopped", "error"}
NAME_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
RUN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,63})-(\d{1,9})$")
MACHINE_SIMS = 3
ARTIFACT_TYPES = {
    ".png": "image/png", ".mp4": "video/mp4", ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml", ".json": "application/json",
}


def home():
    return Path(os.environ.get("E2E_HOME") or os.path.expanduser("~/e2e-runs"))


def read_json(path, default=None):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return default


def read_ndjson(path, limit=None):
    out = []
    try:
        with open(path, "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return out
    if limit:
        lines = lines[-limit:]
    for ln in lines:
        try:
            v = json.loads(ln)
            if isinstance(v, dict):
                out.append(v)
        except ValueError:
            pass  # a half-written last line is normal while the engine appends
    return out


def split_run_id(run_id):
    m = RUN_RE.match(run_id or "")
    if not m or ".." in run_id:
        return None
    return m.group(1), int(m.group(2))


def run_dir(run_id):
    """Run folder for a run-id, or None. Never leaves $E2E_HOME."""
    parts = split_run_id(run_id)
    if not parts:
        return None
    root = home().resolve()
    d = (root / parts[0] / "runs" / run_id)
    try:
        real = d.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if root not in real.parents or not real.is_dir():
        return None
    return real


def project_names():
    root = home()
    names = set()
    for p in read_json(root / "projects.json", []) or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            names.add(p["name"])
    try:
        for d in root.iterdir():
            if d.is_dir() and (d / "runs").is_dir():
                names.add(d.name)
    except OSError:
        pass
    return sorted(n for n in names if NAME_RE.match(n))


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_run(run_id):
    d = run_dir(run_id)
    if not d:
        return None
    req = read_json(d / "request.json", {}) or {}
    state = read_json(d / "state.json", {}) or {}
    report = read_json(d / "report.json")
    run = dict(report or state)
    run.setdefault("run", run_id)
    run.setdefault("project", req.get("project") or split_run_id(run_id)[0])
    run.setdefault("status", "queued")
    run["request"] = req
    run["has_report"] = report is not None
    run["dir"] = d
    run["num"] = split_run_id(run_id)[1]
    run["stale"] = (run["status"] not in TERMINAL and not report
                    and isinstance(state.get("pid"), int) and not pid_alive(state.get("pid")))
    if report is not None:
        run["integrity"] = integrity(run["project"], run_id, d)
    return run


def list_runs(project=None, limit=200):
    root = home()
    out = []
    for name in ([project] if project else project_names()):
        rd = root / name / "runs"
        try:
            ids = [p.name for p in rd.iterdir() if split_run_id(p.name)]
        except OSError:
            continue
        for rid in ids:
            if split_run_id(rid)[0] == name:
                out.append(rid)
    out.sort(key=lambda r: (split_run_id(r)[1], r), reverse=True)
    if not project:
        out.sort(key=lambda r: _mtime(root / split_run_id(r)[0] / "runs" / r), reverse=True)
    runs = []
    for rid in out[:limit]:
        r = load_run(rid)
        if r:
            runs.append(r)
    return runs


def _mtime(p):
    for name in ("state.json", "request.json"):
        try:
            return (p / name).stat().st_mtime
        except OSError:
            pass
    return 0


def active_runs():
    return [r for r in list_runs(limit=50) if r["status"] not in TERMINAL and not r["has_report"]]


def history(project):
    return read_ndjson(home() / project / "history.ndjson")


def integrity(project, run_id, d):
    """'ok', 'tampered', or 'unrecorded' for a finished run's report.json."""
    rows = [h for h in history(project) if h.get("run") == run_id and h.get("sha256")]
    if not rows:
        return "unrecorded"
    try:
        with open(d / "report.json", "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "tampered"
    return "ok" if digest == str(rows[-1]["sha256"]).lower() else "tampered"


def triage(project):
    return read_ndjson(home() / project / "triage.ndjson")


def triage_marks(project, run_id):
    marks = {}
    for t in triage(project):
        if t.get("run") == run_id and t.get("flow"):
            marks[t["flow"]] = t
    return marks


def artifact(run_id, rel):
    """Resolve an artifact path inside a run folder, or None."""
    d = run_dir(run_id)
    if not d or not rel or "\x00" in rel or rel.startswith("/") or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ARTIFACT_TYPES:
        return None
    try:
        real = (d / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if d not in real.parents or not real.is_file():
        return None
    return real, ARTIFACT_TYPES[ext]


def machine():
    """Gauges computed from the run folders and the OS (read-only)."""
    active = active_runs()
    sims_used = sum(len(r.get("sims") or []) for r in active if not r["stale"])
    builder = next((r for r in active if r["status"] == "building" and not r["stale"]), None)
    try:
        du = shutil.disk_usage(str(home() if home().exists() else Path.home()))
        disk_free, disk_total = du.free / 1e9, du.total / 1e9
    except OSError:
        disk_free = disk_total = None
    ram_free, ram_total = _ram()
    locks = []
    eng = home() / "engine"
    try:
        locks = sorted(p.name for p in eng.glob("*.lock"))
    except OSError:
        pass
    return {
        "sims_used": sims_used, "sims_cap": MACHINE_SIMS,
        "ram_free_gb": ram_free, "ram_total_gb": ram_total,
        "disk_free_gb": disk_free, "disk_total_gb": disk_total,
        "build_holder": builder["project"] if builder else None,
        "build_run": builder["run"] if builder else None,
        "active": active, "locks": locks,
        "golden": read_json(eng / "golden.json", {}) or {},
    }


_ram_cache = [0.0, (None, None)]


def _ram():
    if time.time() - _ram_cache[0] < 5:
        return _ram_cache[1]
    free = total = None
    try:
        out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = int(re.search(r"page size of (\d+)", out).group(1))
        pages = {k.strip(): int(v.strip(" .")) for k, v in
                 (ln.split(":", 1) for ln in out.splitlines()[1:] if ":" in ln)
                 if v.strip(" .").isdigit()}
        # free + inactive + speculative is what macOS can hand a new sim without paging.
        free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative")) * page / 1e9
        mem = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        total = int(mem) / 1e9 if mem.isdigit() else None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    _ram_cache[0], _ram_cache[1] = time.time(), (free, total)
    return free, total
