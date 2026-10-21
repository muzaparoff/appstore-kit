"""Real backend: xcodebuild + simctl + Maestro / XCUITest.

The fake twin lives in backend_fake.py; both expose the same methods so the
scheduler never knows which one it drives.
"""

import json
import os
import re
import subprocess

from . import build, machine, proc, sims

MAESTRO_HOME = os.path.expanduser("~/.maestro/bin")
CRASH_RE = re.compile(r"crash", re.I)


def java_home():
    # Same lookup as bin/e2e-run.sh: Maestro needs Java 17+ from Homebrew.
    try:
        prefix = subprocess.run(["brew", "--prefix", "openjdk@17"], capture_output=True,
                                text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        prefix = ""
    jdk = os.path.join(prefix, "libexec/openjdk.jdk/Contents/Home") if prefix else ""
    if not jdk or not os.access(os.path.join(jdk, "bin", "java"), os.X_OK):
        raise RuntimeError("Java 17 missing: brew install openjdk@17")
    return jdk


def maestro_env():
    env = dict(os.environ)
    jdk = java_home()
    env["JAVA_HOME"] = jdk
    env["PATH"] = "%s:%s:%s" % (os.path.join(jdk, "bin"), MAESTRO_HOME, env.get("PATH", ""))
    env["MAESTRO_CLI_NO_ANALYTICS"] = "1"
    return env


def seam_env(app, limits):
    """Seams reach flows as env vars: SEAM_RESET="-e2e.reset" etc."""
    env = {"APP_ID": app["bundle_id"], "E2E_WAIT_MS": str(limits["wait"] * 1000)}
    for name, args in app["seams"].items():
        env["SEAM_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()] = " ".join(args)
    return env


def tail(path, n=40):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return ""


class RealBackend(object):
    dry_run = False

    def __init__(self):
        self._env = None

    def machine(self):
        return {"ram_free_gb": machine.ram_free_gb(), "disk_free_gb": machine.disk_free_gb()}

    def build(self, app, run_dir, timeout, on_line=None):
        return build.build(app, os.path.join(run_dir, "build.log"), timeout, on_line)

    def prepare_golden(self, app):
        return sims.ensure_golden(app["device"], app["runtime"])

    def sim_up(self, app, artifacts, golden, name):
        udid = sims.clone(golden, name)
        try:
            sims.boot(udid)
            sims.install(udid, artifacts["app"])
            sims.grant(udid, app["bundle_id"], app["permissions"])
        except Exception:
            sims.delete(udid)
            raise
        return udid

    def sim_down(self, udid):
        sims.delete(udid)

    def screenshot(self, udid, path):
        return sims.screenshot(udid, path)

    def kill_all(self):
        proc.kill_all()

    def run_flow(self, app, artifacts, flow, udid, outdir, timeout, attempt, on_step, limits):
        video = os.path.join(outdir, "video.mp4")
        rec = sims.record_video(udid, video)
        try:
            if flow["engine"] == "maestro":
                res = self._maestro(app, flow, udid, outdir, timeout, on_step, limits)
            else:
                res = self._xcuitest(artifacts, flow, udid, outdir, timeout, on_step)
        finally:
            proc.stop(rec)
        if res["status"] != "passed":
            sims.screenshot(udid, os.path.join(outdir, "screenshot.png"))
        elif os.path.exists(video):
            os.unlink(video)  # passing videos are never looked at; save the disk
        return res

    def _maestro(self, app, flow, udid, outdir, timeout, on_step, limits):
        if self._env is None:
            self._env = maestro_env()
        cmd = [os.path.join(MAESTRO_HOME, "maestro"), "--device", udid, "test", "--no-ansi"]
        for k, v in sorted(seam_env(app, limits).items()):
            cmd += ["-e", "%s=%s" % (k, v)]
        cmd += ["--format", "junit", "--output", os.path.join(outdir, "junit.xml"),
                "--test-output-dir", os.path.join(outdir, "maestro"), flow["path"]]
        log = os.path.join(outdir, "log.txt")

        def line(text):
            text = text.strip()
            if text:
                on_step(text[:200])

        r = proc.run(cmd, timeout=timeout, log_path=log, on_line=line, env=self._env,
                     cwd=app["repo"])
        lines = [l for l in r.out.splitlines() if l.strip()]
        crash = [l for l in lines if CRASH_RE.search(l)]
        if r.timed_out:
            return {"status": "timeout", "message": "flow exceeded %ds" % timeout, "crash": None}
        if r.code == 0:
            return {"status": "passed", "message": None, "crash": None}
        if crash:
            return {"status": "crashed", "message": crash[-1][:500], "crash": "\n".join(crash[-20:])}
        failing = [l for l in lines if "FAILED" in l or "Assertion" in l or "Error" in l]
        msg = (failing or lines or ["maestro exited %s" % r.code])[-1]
        return {"status": "failed", "message": msg.strip()[:500], "crash": None}

    def _xcuitest(self, artifacts, flow, udid, outdir, timeout, on_step):
        if not artifacts.get("xctestrun"):
            return {"status": "failed", "message": "no .xctestrun from build-for-testing", "crash": None}
        bundle = os.path.join(outdir, "result.xcresult")
        cmd = ["xcodebuild", "test-without-building", "-xctestrun", artifacts["xctestrun"],
               "-destination", "id=" + udid, "-only-testing:" + flow["test"],
               "-parallel-testing-enabled", "NO", "-resultBundlePath", bundle]

        def line(text):
            # XCTest activity lines ("t =  1.20s Tap ...") are the live steps.
            m = re.match(r"^\s*t =\s+[\d.]+s\s+(.*)$", text)
            if m:
                on_step(m.group(1)[:200])

        r = proc.run(cmd, timeout=timeout, log_path=os.path.join(outdir, "log.txt"), on_line=line)
        if r.timed_out:
            return {"status": "timeout", "message": "test exceeded %ds" % timeout, "crash": None}
        if r.code == 0:
            return {"status": "passed", "message": None, "crash": None}
        failures = self._xcresult_failures(bundle)
        msg = failures[0] if failures else "xcodebuild exited %s" % r.code
        crashes = [f for f in failures if CRASH_RE.search(f)]
        if crashes:
            return {"status": "crashed", "message": crashes[0][:500], "crash": "\n".join(crashes)}
        return {"status": "failed", "message": msg[:500], "crash": None}

    def _xcresult_failures(self, bundle):
        if not os.path.exists(bundle):
            return []
        r = proc.run(["xcrun", "xcresulttool", "get", "test-results", "summary",
                      "--path", bundle, "--compact"], timeout=60)
        try:
            data = json.loads(r.out)
        except ValueError:
            return []
        return ["%s: %s" % (f.get("testName", "?"), f.get("failureText", ""))
                for f in data.get("testFailures", [])]
