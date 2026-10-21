"""Real backend wiring with every subprocess mocked: nothing touches
xcodebuild, simctl or Maestro."""

import json
import os
from unittest import mock

from e2ekit import build, config, engines, proc, sims
from tests.helpers import HomeCase

DEVICES = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [
    {"udid": "MINE", "name": "iPhone 18 Pro", "state": "Shutdown"},
    {"udid": "E2E-1", "name": "e2e-demo-1-1", "state": "Booted"},
]}}


def fake_result(out="", code=0, timed_out=False):
    return proc.Result(code, out, timed_out, False)


class SimsTests(HomeCase):
    def test_never_touches_non_e2e_sims(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[2:4] == ["list", "-j"]:
                return fake_result(json.dumps(DEVICES))
            return fake_result()

        with mock.patch.object(proc, "run", side_effect=run):
            with self.assertRaises(sims.SimError):
                sims.delete("MINE")
            with self.assertRaises(sims.SimError):
                sims.boot("MINE")
            with self.assertRaises(sims.SimError):
                sims.clone("E2E-1", "my-sim")
            self.assertTrue(sims.delete("E2E-1"))
            self.assertFalse(sims.delete("GONE"))
        mutating = [c for c in calls if c[2] in ("delete", "shutdown", "boot", "clone")]
        self.assertTrue(mutating)
        self.assertTrue(all(c[3] == "E2E-1" for c in mutating))

    def test_runtime_latest(self):
        rts = {"runtimes": [
            {"isAvailable": True, "platform": "iOS", "version": "26.2", "identifier": "ios26", "name": "iOS 26.2"},
            {"isAvailable": True, "platform": "iOS", "version": "27.0", "identifier": "ios27", "name": "iOS 27.0"},
            {"isAvailable": True, "platform": "watchOS", "version": "28.0", "identifier": "w28", "name": "watchOS 28.0"},
        ]}
        with mock.patch.object(proc, "run", return_value=fake_result(json.dumps(rts))):
            self.assertEqual(sims.resolve_runtime("latest"), ("ios27", "27.0"))
            self.assertEqual(sims.resolve_runtime("iOS 26.2"), ("ios26", "26.2"))
            with self.assertRaises(sims.SimError):
                sims.resolve_runtime("iOS 18.0")


class EngineTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.app = config.load_app(self.make_repo())
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def test_seam_env(self):
        env = engines.seam_env(self.app, self.app["limits"])
        self.assertEqual(env["SEAM_RESET"], "-e2e.reset")
        self.assertEqual(env["SEAM_SEED"], "-seedTrip {name}")
        self.assertEqual(env["APP_ID"], "com.example.demo")

    def test_build_command(self):
        cmd = build.command(self.app)
        self.assertEqual(cmd[:2], ["xcodebuild", "build-for-testing"])
        self.assertIn(os.path.join(self.home, "build", "demo"), cmd)
        self.assertIn("CODE_SIGN_IDENTITY=-", cmd)

    def _backend(self):
        b = engines.RealBackend()
        b._env = {}
        return b

    def _run(self, flow, result):
        b = self._backend()
        seen = {}

        def run(cmd, **kw):
            if cmd[:2] == ["xcrun", "xcresulttool"]:
                return fake_result(json.dumps({"testFailures": [
                    {"testName": "testBuy()", "failureText": "XCTAssertTrue failed"}]}))
            seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")
            if "-resultBundlePath" in cmd:
                os.makedirs(cmd[cmd.index("-resultBundlePath") + 1], exist_ok=True)
            if kw.get("on_line"):
                kw["on_line"]("Tap on id: trips.add... COMPLETED")
                kw["on_line"]("    t =     1.20s Tap \"Buy\" Button")
            return result

        with mock.patch.object(proc, "run", side_effect=run), \
                mock.patch.object(sims, "record_video", return_value=None), \
                mock.patch.object(proc, "stop"), \
                mock.patch.object(sims, "screenshot") as shot:
            steps = []
            res = b.run_flow(self.app, {"xctestrun": "/x.xctestrun"}, flow, "E2E-1", self.out, 90,
                             1, steps.append, self.app["limits"])
        return res, seen, steps, shot

    def test_maestro_pass(self):
        flow = config.list_flows(self.app, "trips")[0]
        res, seen, steps, shot = self._run(flow, fake_result("ok"))
        self.assertEqual(res["status"], "passed")
        cmd = seen["cmd"]
        self.assertEqual(cmd[1:4], ["--device", "E2E-1", "test"])
        self.assertIn("SEAM_RESET=-e2e.reset", cmd)
        self.assertIn("--format", cmd)
        self.assertEqual(cmd[-1], flow["path"])
        self.assertEqual(seen["timeout"], 90)
        self.assertIn("Tap on id: trips.add... COMPLETED", steps)
        shot.assert_not_called()

    def test_maestro_fail_timeout_crash(self):
        flow = config.list_flows(self.app, "trips")[0]
        res, _, _, shot = self._run(flow, fake_result("Assert visible id: x... FAILED", code=1))
        self.assertEqual(res["status"], "failed")
        self.assertIn("FAILED", res["message"])
        shot.assert_called_once()
        res, _, _, _ = self._run(flow, fake_result("", code=-15, timed_out=True))
        self.assertEqual(res["status"], "timeout")
        res, _, _, _ = self._run(flow, fake_result("App crashed: SIGABRT", code=1))
        self.assertEqual(res["status"], "crashed")
        self.assertIn("SIGABRT", res["crash"])

    def test_xcuitest(self):
        flow = config.list_flows(self.app, "ui")[0]
        res, seen, steps, _ = self._run(flow, fake_result("", code=65))
        self.assertIn("-only-testing:DemoUITests/PaywallTests", seen["cmd"])
        self.assertIn("test-without-building", seen["cmd"])
        self.assertEqual(res["status"], "failed")
        self.assertIn("XCTAssertTrue failed", res["message"])
        self.assertEqual(steps, ['Tap "Buy" Button'])
