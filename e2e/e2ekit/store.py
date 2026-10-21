"""Runs root: registry, run folders, atomic writes, history, retention."""

import datetime
import hashlib
import json
import os
import shutil
import stat
import tempfile

TERMINAL = ("passed", "failed", "stopped", "error")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("E2E_HOME") or "~/e2e-runs"))


def engine_dir():
    return ensure_dir(os.path.join(home(), "engine"))


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path


def project_dir(project):
    return os.path.join(home(), project)


def runs_dir(project):
    return os.path.join(project_dir(project), "runs")


def run_dir(run_id):
    return os.path.join(runs_dir(project_of(run_id)), run_id)


def project_of(run_id):
    project, _, n = run_id.rpartition("-")
    if not project or not n.isdigit():
        raise ValueError("bad run id %r" % run_id)
    return project


def atomic_write_json(path, data):
    # tmp + rename so readers (the console) never see half a file.
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1, sort_keys=False)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_ndjson(path, obj):
    # One write() per line on an O_APPEND fd keeps concurrent appenders from
    # interleaving inside a line.
    line = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def read_ndjson(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


# --- registry -------------------------------------------------------------

def projects():
    return read_json(os.path.join(home(), "projects.json"), []) or []


def find_project(name):
    for p in projects():
        if p["name"] == name:
            return p
    return None


def register_project(name, path):
    ensure_dir(home())
    items = [p for p in projects() if p["name"] != name]
    items.append({"name": name, "path": os.path.abspath(path), "added": now_iso()})
    items.sort(key=lambda p: p["name"])
    atomic_write_json(os.path.join(home(), "projects.json"), items)


# --- runs -----------------------------------------------------------------

def run_ids(project):
    """Run ids of a project, oldest first."""
    try:
        names = os.listdir(runs_dir(project))
    except OSError:
        return []
    ids = []
    for n in names:
        try:
            if project_of(n) == project:
                ids.append(n)
        except ValueError:
            pass
    return sorted(ids, key=lambda r: int(r.rpartition("-")[2]))


def next_run_id(project):
    # Caller holds the project lock, so nobody else is numbering this project.
    nums = [int(r.rpartition("-")[2]) for r in run_ids(project)]
    for h in read_ndjson(os.path.join(project_dir(project), "history.ndjson")):
        try:
            nums.append(int(str(h.get("run", "")).rpartition("-")[2]))
        except ValueError:
            pass
    return "%s-%d" % (project, max(nums or [0]) + 1)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_read_only(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                os.chmod(p, 0o444)
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                os.chmod(p, 0o555)
    os.chmod(path, 0o555)


def _rm_tree_writable(path):
    for root, dirs, _ in os.walk(path):
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
    if os.path.isdir(path) and not os.path.islink(path):
        os.chmod(path, 0o755)
        shutil.rmtree(path, ignore_errors=True)
    else:
        os.unlink(path)


def remove_from_read_only(path):
    """Delete one file or tree inside a read-only run folder.

    Only the parent dir gets its write bit back, and only for the unlink, so
    everything kept stays read-only.
    """
    if not os.path.lexists(path):
        return False
    parent = os.path.dirname(path)
    mode = stat.S_IMODE(os.stat(parent).st_mode)
    os.chmod(parent, mode | stat.S_IWUSR)
    try:
        _rm_tree_writable(path)
    finally:
        os.chmod(parent, mode)
    return True


def retention(project, keep_run=None):
    """Drop heavy files of previous runs; keep report, JUnit, failure shots.

    Videos of the latest previous run's failed flows survive so the owner can
    still watch what broke last time.
    """
    ids = [r for r in run_ids(project) if r != keep_run]
    removed = 0
    latest = ids[-1] if ids else None
    for rid in ids:
        rdir = run_dir(rid)
        report = read_json(os.path.join(rdir, "report.json"))
        if report is None:
            continue  # not finalized: maybe still running elsewhere, leave it
        failed = set()
        if rid == latest:
            failed = {f["flow"] for f in report.get("failures", [])}
        for name in ("live", "build.log", "engine.log"):
            removed += remove_from_read_only(os.path.join(rdir, name))
        flows_root = os.path.join(rdir, "flows")
        for root, dirs, files in os.walk(flows_root):
            rel = os.path.relpath(root, flows_root)
            for d in list(dirs):
                if d == "maestro" or d.endswith(".xcresult"):
                    removed += remove_from_read_only(os.path.join(root, d))
                    dirs.remove(d)
            for f in files:
                if f == "log.txt" or (f == "video.mp4" and rel not in failed):
                    removed += remove_from_read_only(os.path.join(root, f))
    return removed


def prune(project, keep):
    """Delete all but the newest `keep` runs and their history lines."""
    ids = run_ids(project)
    doomed = ids[:-keep] if keep > 0 else ids
    for rid in doomed:
        _rm_tree_writable(run_dir(rid))
    hist = os.path.join(project_dir(project), "history.ndjson")
    if os.path.exists(hist):
        kept = [h for h in read_ndjson(hist) if h.get("run") not in doomed]
        tmp = hist + ".tmp"
        with open(tmp, "w") as f:
            for h in kept:
                f.write(json.dumps(h, separators=(",", ":")) + "\n")
        os.replace(tmp, hist)
    return doomed


def verify(project):
    """[{run, status: ok|tampered|missing}] for every history line."""
    out = []
    for h in read_ndjson(os.path.join(project_dir(project), "history.ndjson")):
        rid = h.get("run")
        path = os.path.join(runs_dir(project), str(rid), "report.json")
        if not os.path.exists(path):
            status = "missing"
        elif sha256_file(path) == h.get("sha256"):
            status = "ok"
        else:
            status = "tampered"
        out.append({"run": rid, "status": status})
    return out


def load_quarantine(project):
    return read_json(os.path.join(project_dir(project), "quarantine.json"), {}) or {}


def save_quarantine(project, data):
    atomic_write_json(os.path.join(ensure_dir(project_dir(project)), "quarantine.json"), data)
