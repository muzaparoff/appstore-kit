"""Dry-run backend: no xcodebuild, no simctl, no Maestro.

Outcomes follow flow names so the console can be built against every state:
`*fail*` fails, `*flaky*` fails once then passes, `*slow*` takes 3 s.
"""

import os
import struct
import threading
import time
import zlib

COLORS = [(52, 168, 83), (66, 133, 244), (251, 188, 5)]


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def png(width, height, rgb):
    """Solid-colour PNG with a darker header band, stdlib only."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    band = bytes(c // 2 for c in rgb)
    rows = []
    for y in range(height):
        px = band if y < height // 8 else bytes(rgb)
        rows.append(b"\x00" + px * width)
    raw = zlib.compress(b"".join(rows), 9)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


class FakeBackend(object):
    dry_run = True

    def __init__(self):
        self.step_s = env_float("E2E_FAKE_STEP_S", 0.1)
        self.build_s = env_float("E2E_FAKE_BUILD_S", 1.0)
        self._stop = threading.Event()
        self._n = 0

    def machine(self):
        return {"ram_free_gb": env_float("E2E_FAKE_RAM_GB", 8.0),
                "disk_free_gb": env_float("E2E_FAKE_DISK_GB", 40.0)}

    def _sleep(self, s):
        # Interruptible so a stop never waits out a fake 3 s flow.
        self._stop.wait(s)

    def build(self, app, run_dir, timeout, on_line=None):
        with open(os.path.join(run_dir, "build.log"), "w") as f:
            f.write("dry-run: no xcodebuild\n")
        self._sleep(min(self.build_s, timeout))
        return {"app": "/dry-run/%s.app" % app["scheme"], "xctestrun": "/dry-run/x.xctestrun"}

    def prepare_golden(self, app):
        return "FAKE-GOLDEN"

    def sim_up(self, app, artifacts, golden, name):
        self._n += 1
        return "FAKE-%s" % name

    def sim_down(self, udid):
        pass

    def screenshot(self, udid, path):
        slot = int(udid.rsplit("-", 1)[-1]) if udid[-1:].isdigit() else 1
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(png(60, 130, COLORS[(slot - 1) % len(COLORS)]))
        os.replace(tmp, path)
        return True

    def kill_all(self):
        self._stop.set()

    def run_flow(self, app, artifacts, flow, udid, outdir, timeout, attempt, on_step, limits):
        name = flow["id"].rsplit("/", 1)[-1]
        steps = ["launchApp %s" % app["bundle_id"], "tap id=%s.start" % flow["suite"],
                 "assertVisible id=%s.done" % flow["suite"]]
        dur = 3.0 if "slow" in name else self.step_s * len(steps)
        log = []
        t0 = time.time()
        for s in steps:
            on_step(s)
            log.append(s)
            self._sleep(dur / len(steps))
            if self._stop.is_set():
                break
            if time.time() - t0 > timeout:
                self._write(outdir, log, False)
                return {"status": "timeout", "message": "flow exceeded %ds" % timeout, "crash": None}
        ok = not ("fail" in name or ("flaky" in name and attempt == 1))
        if self._stop.is_set():
            ok = False
        self._write(outdir, log, ok)
        if not ok:
            self.screenshot(udid, os.path.join(outdir, "screenshot.png"))
            return {"status": "failed", "message": "dry-run: assertVisible id=%s.done not found" % flow["suite"],
                    "crash": None}
        return {"status": "passed", "message": None, "crash": None}

    def _write(self, outdir, log, ok):
        with open(os.path.join(outdir, "log.txt"), "w") as f:
            f.write("\n".join(log) + "\n" + ("PASSED" if ok else "FAILED") + "\n")
        with open(os.path.join(outdir, "junit.xml"), "w") as f:
            f.write('<?xml version="1.0"?>\n<testsuites><testsuite tests="1" failures="%d">'
                    '<testcase name="dry-run"/></testsuite></testsuites>\n' % (0 if ok else 1))
