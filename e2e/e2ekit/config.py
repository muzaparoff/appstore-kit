"""app.yml loading and limit clamping."""

import os
import re

import yaml

# (default, cap) in seconds / counts. Caps protect the one shared Mac: an
# app.yml can ask for less, never for more.
LIMITS = {
    "wait": (10, 30),
    "flow": (240, 600),
    "suite": (1500, 2700),
    "run": (5400, 7200),
    "sims": (2, 2),
    "retries": (1, 2),
}
MACHINE_SIMS = 3
QUARANTINE_AFTER = 3
RAM_FREE_GB = 3.0
DISK_FREE_GB = 12.0

ENGINES = ("maestro", "xcuitest")
# simctl privacy services an app.yml may grant; anything else is a typo.
PERMISSIONS = (
    "all", "calendar", "contacts-limited", "contacts", "location",
    "location-always", "photos-add", "photos", "media-library", "microphone",
    "motion", "reminders", "siri", "camera", "notifications",
)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*(-[a-z0-9_]+)*$")


class ConfigError(Exception):
    pass


def parse_duration(value, what):
    """'90s' / '4m' / '1h' / bare seconds -> int seconds."""
    if isinstance(value, bool):
        raise ConfigError("%s: not a duration: %r" % (what, value))
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", str(value))
    if not m:
        raise ConfigError("%s: not a duration: %r" % (what, value))
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)])


def clamp_limits(raw):
    """Return (limits, hits). hits lists every value the caps cut down."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("limits: must be a mapping")
    unknown = set(raw) - set(LIMITS)
    if unknown:
        raise ConfigError("limits: unknown key(s) %s" % ", ".join(sorted(unknown)))
    limits, hits = {}, []
    for key, (default, cap) in LIMITS.items():
        if key not in raw:
            limits[key] = default
            continue
        if key in ("sims", "retries"):
            try:
                asked = int(raw[key])
            except (TypeError, ValueError):
                raise ConfigError("limits.%s: not a number: %r" % (key, raw[key]))
        else:
            asked = parse_duration(raw[key], "limits." + key)
        floor = 1 if key in ("sims", "flow", "suite", "run", "wait") else 0
        value = max(floor, min(asked, cap))
        if value != asked:
            hits.append({"limit": key, "asked": asked, "used": value, "cap": cap})
        limits[key] = value
    return limits, hits


def _str_list(value, what):
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise ConfigError("%s: must be a list of strings" % what)
    return [str(v) for v in value]


def validate_app(data, repo):
    if not isinstance(data, dict):
        raise ConfigError("app.yml: must be a mapping")
    for key in ("project", "scheme", "bundle_id", "device", "suites"):
        if not data.get(key):
            raise ConfigError("app.yml: missing %s" % key)
    project = str(data["project"])
    if not NAME_RE.match(project):
        raise ConfigError("app.yml: project must be lowercase letters, digits, - or _")
    if bool(data.get("xcodeproj")) == bool(data.get("workspace")):
        raise ConfigError("app.yml: set exactly one of xcodeproj / workspace")
    device = str(data["device"])
    if not device.startswith("iPhone"):
        raise ConfigError("app.yml: device must be an iPhone, got %r" % device)

    seams = data.get("seams") or {}
    if not isinstance(seams, dict):
        raise ConfigError("app.yml: seams must be a mapping")
    seams = {str(k): _str_list(v, "seams." + str(k)) for k, v in seams.items()}

    perms = _str_list(data.get("permissions") or [], "permissions")
    bad = [p for p in perms if p not in PERMISSIONS]
    if bad:
        raise ConfigError("app.yml: unknown permission(s) %s" % ", ".join(bad))

    limits, hits = clamp_limits(data.get("limits"))

    if not isinstance(data["suites"], dict):
        raise ConfigError("app.yml: suites must be a mapping")
    suites = {}
    for name, s in data["suites"].items():
        name = str(name)
        if not NAME_RE.match(name):
            raise ConfigError("suite %r: bad name" % name)
        if not isinstance(s, dict) or s.get("engine") not in ENGINES:
            raise ConfigError("suite %s: engine must be one of %s" % (name, ", ".join(ENGINES)))
        suite = {"engine": s["engine"]}
        if s["engine"] == "maestro":
            if not s.get("flows"):
                raise ConfigError("suite %s: maestro needs flows: <dir>" % name)
            suite["flows"] = str(s["flows"])
        else:
            suite["tests"] = _str_list(s.get("tests") or [], "suite %s tests" % name)
            if not suite["tests"]:
                raise ConfigError("suite %s: xcuitest needs tests: [Target/Class]" % name)
        budget = parse_duration(s.get("budget", limits["suite"]), "suite %s budget" % name)
        if budget > limits["suite"]:
            hits.append({"limit": "suite:" + name, "asked": budget,
                         "used": limits["suite"], "cap": limits["suite"]})
            budget = limits["suite"]
        suite["budget"] = max(1, budget)
        suites[name] = suite

    return {
        "project": project,
        "repo": os.path.abspath(repo),
        "xcodeproj": data.get("xcodeproj"),
        "workspace": data.get("workspace"),
        "scheme": str(data["scheme"]),
        "bundle_id": str(data["bundle_id"]),
        "device": device,
        "runtime": str(data.get("runtime") or "latest"),
        "seams": seams,
        "permissions": perms,
        "suites": suites,
        "limits": limits,
        "limit_hits": hits,
    }


def load_app(repo):
    path = os.path.join(repo, "e2e", "app.yml")
    if not os.path.isfile(path):
        raise ConfigError("no e2e/app.yml in %s" % repo)
    with open(path) as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError("app.yml: %s" % e)
    return validate_app(data, repo)


def pick_suites(app, requested):
    """Fixed choices only: every requested suite must exist in app.yml."""
    names = [s.strip() for s in (requested or "").split(",") if s.strip()]
    if not names:
        raise ConfigError("--suite is required (one of: %s)" % ", ".join(app["suites"]))
    unknown = [n for n in names if n not in app["suites"]]
    if unknown:
        raise ConfigError("unknown suite(s) %s for %s (have: %s)" % (
            ", ".join(unknown), app["project"], ", ".join(app["suites"])))
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def list_flows(app, suite_name):
    """Flow descriptors for one suite, in a stable order."""
    suite = app["suites"][suite_name]
    flows = []
    if suite["engine"] == "maestro":
        root = os.path.join(app["repo"], suite["flows"])
        if not os.path.isdir(root):
            raise ConfigError("suite %s: no flows dir %s" % (suite_name, root))
        for fn in sorted(os.listdir(root)):
            stem, ext = os.path.splitext(fn)
            # config.yaml is Maestro's workspace config, not a flow.
            if ext not in (".yaml", ".yml") or stem == "config":
                continue
            flows.append({"id": "%s/%s" % (suite_name, stem), "suite": suite_name,
                          "engine": "maestro", "path": os.path.join(root, fn)})
    else:
        for t in suite["tests"]:
            flows.append({"id": "%s/%s" % (suite_name, t.split("/")[-1]), "suite": suite_name,
                          "engine": "xcuitest", "test": t})
    if not flows:
        raise ConfigError("suite %s: no flows" % suite_name)
    return flows
