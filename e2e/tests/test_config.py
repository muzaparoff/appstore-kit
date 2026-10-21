import unittest

import yaml

from e2ekit import config
from tests.helpers import APP_YML, HomeCase


class LimitTests(unittest.TestCase):
    def test_defaults(self):
        limits, hits = config.clamp_limits(None)
        self.assertEqual(limits, {"wait": 10, "flow": 240, "suite": 1500, "run": 5400,
                                  "sims": 2, "retries": 1})
        self.assertEqual(hits, [])

    def test_caps_clamp_and_report(self):
        limits, hits = config.clamp_limits(
            {"sims": 4, "flow": "20m", "suite": "1h", "run": "3h", "retries": 5, "wait": "45s"})
        self.assertEqual(limits, {"wait": 30, "flow": 600, "suite": 2700, "run": 7200,
                                  "sims": 2, "retries": 2})
        self.assertEqual({h["limit"] for h in hits},
                         {"wait", "flow", "suite", "run", "sims", "retries"})

    def test_lower_values_kept(self):
        limits, hits = config.clamp_limits({"sims": 1, "flow": "90s", "retries": 0})
        self.assertEqual((limits["sims"], limits["flow"], limits["retries"]), (1, 90, 0))
        self.assertEqual(hits, [])

    def test_floor(self):
        limits, hits = config.clamp_limits({"sims": 0})
        self.assertEqual(limits["sims"], 1)
        self.assertEqual(len(hits), 1)

    def test_unknown_limit_refused(self):
        with self.assertRaises(config.ConfigError):
            config.clamp_limits({"simz": 2})

    def test_durations(self):
        self.assertEqual(config.parse_duration("4m", "x"), 240)
        self.assertEqual(config.parse_duration("1h", "x"), 3600)
        self.assertEqual(config.parse_duration(30, "x"), 30)
        with self.assertRaises(config.ConfigError):
            config.parse_duration("soon", "x")


class AppYmlTests(HomeCase):
    def load(self, extra="", **override):
        data = yaml.safe_load(APP_YML.format(project="demo", extra=extra))
        data.update(override)
        return config.validate_app(data, self.make_repo())

    def test_valid(self):
        app = self.load("limits: {sims: 3, suite: 10m}")
        self.assertEqual(app["limits"]["sims"], 2)
        # trips budget 12m exceeds the app's own 10m suite limit
        self.assertEqual(app["suites"]["trips"]["budget"], 600)
        self.assertIn("suite:trips", [h["limit"] for h in app["limit_hits"]])
        self.assertEqual(app["seams"]["seed"], ["-seedTrip", "{name}"])

    def test_unknown_suite_refused(self):
        app = self.load()
        with self.assertRaises(config.ConfigError) as cm:
            config.pick_suites(app, "smoke,nope")
        self.assertIn("nope", str(cm.exception))
        self.assertEqual(config.pick_suites(app, "trips,smoke,trips"), ["trips", "smoke"])
        with self.assertRaises(config.ConfigError):
            config.pick_suites(app, "")

    def test_ipad_refused(self):
        with self.assertRaises(config.ConfigError):
            self.load(device="iPad Pro 13-inch")

    def test_project_and_workspace_exclusive(self):
        with self.assertRaises(config.ConfigError):
            self.load(workspace="Demo.xcworkspace")

    def test_bad_engine_and_permission(self):
        with self.assertRaises(config.ConfigError):
            self.load(suites={"x": {"engine": "appium", "flows": "e2e"}})
        with self.assertRaises(config.ConfigError):
            self.load(permissions=["photoz"])

    def test_list_flows_skips_maestro_config(self):
        repo = self.make_repo(flows={"smoke": ["01-a", "config", "02-b"], "trips": ["x"]})
        app = config.load_app(repo)
        self.assertEqual([f["id"] for f in config.list_flows(app, "smoke")], ["smoke/01-a", "smoke/02-b"])
        self.assertEqual([f["id"] for f in config.list_flows(app, "ui")], ["ui/PaywallTests"])


if __name__ == "__main__":
    unittest.main()


class XCUITestGroupTests(unittest.TestCase):
    def test_string_and_group_entries_become_flows(self):
        tests = config._xcuitest_tests(
            ["T/C/testOne", {"name": "core-a", "only": ["T/C/testA", "T/C/testB"]}], "core")
        self.assertEqual(tests, [{"name": "testOne", "only": ["T/C/testOne"]},
                                 {"name": "core-a", "only": ["T/C/testA", "T/C/testB"]}])
        with self.assertRaises(config.ConfigError):
            config._xcuitest_tests([{"name": "Core A", "only": ["x"]}], "core")
        with self.assertRaises(config.ConfigError):
            config._xcuitest_tests([{"only": ["x"]}], "core")
