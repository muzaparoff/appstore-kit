"""simctl wrapper. Only ever touches devices whose name starts with `e2e-`."""

import json
import os
import re

from . import proc, store

PREFIX = "e2e-"


class SimError(Exception):
    pass


def _simctl(*args, timeout=180):
    return proc.check(["xcrun", "simctl"] + list(args), timeout=timeout)


def devices():
    """[{udid, name, state, runtime}] for every simulator."""
    data = json.loads(_simctl("list", "-j", "devices"))
    out = []
    for runtime, items in data.get("devices", {}).items():
        for d in items:
            out.append({"udid": d["udid"], "name": d["name"], "state": d.get("state"),
                        "runtime": runtime})
    return out


def name_of(udid):
    for d in devices():
        if d["udid"] == udid:
            return d["name"]
    return None


def _guard(udid):
    # Re-read the name from simctl rather than trusting our own records: the
    # owner's hand-made simulators must be unreachable even if a udid is wrong.
    name = name_of(udid)
    if name is None:
        return None
    if not name.startswith(PREFIX):
        raise SimError("refusing to touch simulator %s (%s): not e2e-*" % (udid, name))
    return name


def resolve_runtime(spec):
    """'latest' or 'iOS 27.0' -> runtime identifier."""
    data = json.loads(_simctl("list", "-j", "runtimes"))
    ios = [r for r in data.get("runtimes", [])
           if r.get("isAvailable") and r.get("platform") == "iOS"]
    if not ios:
        raise SimError("no available iOS simulator runtime")
    if spec == "latest":
        ios.sort(key=lambda r: [int(x) for x in re.findall(r"\d+", r.get("version", "0"))])
        return ios[-1]["identifier"], ios[-1].get("version")
    want = spec.replace("iOS", "").strip()
    for r in ios:
        if r.get("version") == want or r.get("name") == spec:
            return r["identifier"], r.get("version")
    raise SimError("runtime %r not installed" % spec)


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()


def ensure_golden(device, runtime_spec):
    """Golden sim per device+runtime: created once, cloned per run, never
    booted, so every run starts from the same pristine state."""
    runtime_id, version = resolve_runtime(runtime_spec)
    key = "%s|%s" % (device, runtime_id)
    path = os.path.join(store.engine_dir(), "golden.json")
    golden = store.read_json(path, {}) or {}
    known = {d["udid"]: d for d in devices()}
    udid = golden.get(key)
    if udid in known and known[udid]["name"].startswith(PREFIX):
        return udid
    name = "%sgolden-%s-%s" % (PREFIX, _slug(device), _slug(version or runtime_id))
    udid = _simctl("create", name, device, runtime_id).strip().splitlines()[-1]
    golden[key] = udid
    store.atomic_write_json(path, golden)
    return udid


def golden_udids():
    return set((store.read_json(os.path.join(store.engine_dir(), "golden.json"), {}) or {}).values())


def clone(golden_udid, name):
    if not name.startswith(PREFIX):
        raise SimError("clone name must start with %s" % PREFIX)
    _guard(golden_udid)
    return _simctl("clone", golden_udid, name).strip().splitlines()[-1]


def boot(udid):
    _guard(udid)
    _simctl("boot", udid)
    _simctl("bootstatus", udid, "-b", timeout=300)


def install(udid, app_path):
    _guard(udid)
    _simctl("install", udid, app_path, timeout=300)


def grant(udid, bundle_id, permissions):
    _guard(udid)
    for p in permissions:
        _simctl("privacy", udid, "grant", p, bundle_id)


def screenshot(udid, path):
    tmp = path + ".tmp.png"
    r = proc.run(["xcrun", "simctl", "io", udid, "screenshot", "--type=png", tmp],
                 timeout=15, keep_out=False)
    if r.code == 0 and os.path.exists(tmp):
        os.replace(tmp, path)
        return True
    return False


def record_video(udid, path):
    return proc.start(["xcrun", "simctl", "io", udid, "recordVideo", "--codec=h264",
                       "--force", path])


def delete(udid):
    """Shutdown + delete one e2e-* sim. Missing sims are fine."""
    if _guard(udid) is None:
        return False
    proc.run(["xcrun", "simctl", "shutdown", udid], timeout=60)
    _simctl("delete", udid, timeout=120)
    return True
