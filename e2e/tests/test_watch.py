"""What Claude reads instead of polling: the live feed of a run and the session brief."""

import json
import os

from tests.helpers import HomeCase

CARD = ("--title", "Map freezes when a day opens", "--do", "Open a day",
        "--see", "Map freezes about 1 s", "--want", "No freeze")


class Watch(HomeCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("add", self.make_repo()).returncode, 0)

    def lines(self, *args):
        r = self.cli("watch", *args)
        return r.returncode, [json.loads(l) for l in r.stdout.splitlines()]

    def test_follows_a_live_run_to_its_result(self):
        rid = self.cli_json("run", "demo", "--suite", "trips", "--dry-run", "--detach")[1]["run"]
        code, lines = self.lines(rid, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(lines[:-1], self.events(rid))  # every event, once, in order
        last = lines[-1]
        self.assertEqual((last["type"], last["run"], last["status"]), ("report", rid, "failed"))
        self.assertEqual([f["flow"] for f in last["failures"]], ["trips/02-fail-edit"])
        self.assertEqual(set(last["failures"][0]), {"flow", "kind", "message"})

    def test_quiet_keeps_only_what_went_wrong(self):
        rid = self.cli_json("run", "demo", "--suite", "trips", "--dry-run")[1]["run"]
        code, lines = self.lines(rid, "--json", "--quiet")
        self.assertEqual(lines[-1]["type"], "report")
        self.assertTrue(lines[:-1])
        for e in lines[:-1]:
            self.assertEqual(e["type"], "flow_end")
            self.assertNotEqual(e["status"], "passed")
        text = self.cli("watch", rid, "--quiet").stdout
        self.assertIn("%s failed total=3" % rid, text)
        self.assertIn("trips/02-fail-edit", text)

    def test_passed_run_exits_zero_and_unknown_run_is_refused(self):
        rid = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run")[1]["run"]
        self.assertEqual(self.lines(rid, "--json", "--quiet"),
                         (0, [{"type": "report", "run": rid, "status": "passed",
                               "counts": self.cli_json("report", rid)[1]["counts"], "failures": []}]))
        code, out = self.cli_json("watch", "demo-99")
        self.assertEqual((code, out["error"]), (1, "no run demo-99"))


class Brief(HomeCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("add", self.make_repo()).returncode, 0)

    def line(self, *args):
        return self.cli("brief", "--line", *args).stdout.strip()

    def test_line_says_what_needs_the_owner(self):
        self.assertEqual(self.line(), "e2e: idle")
        self.cli("bug", "add", "demo", *CARD, "--by", "reviewer", "--dry-run")  # practice: no freeze
        self.assertEqual(self.line(), "e2e: 1 bug needs you")
        self.cli("bug", "add", "demo", *CARD, "--by", "reviewer", "--suggest", "P1")
        self.assertEqual(self.line("demo"), "e2e: 2 bugs need you · design freeze: demo")
        self.cli("bug", "approve", "demo", "B-1", "--priority", "P1", "--by", "owner")
        self.assertEqual(self.line(), "e2e: 1 bug needs you · design freeze: demo")
        code, out = self.cli_json("brief", "demo")
        p = out["projects"][0]
        self.assertEqual((p["freeze"], [c["id"] for c in p["needs_owner"]]), (True, ["B-2"]))
        self.assertEqual(self.cli_json("brief", "nope")[1]["error"], "unknown project 'nope'")

    def test_last_result_per_suite_skips_single_flows_and_dry_runs(self):
        whole = self.cli_json("run", "demo", "--suite", "trips,smoke", "--dry-run")[1]["run"]
        self.cli_json("run", "demo", "--flow", "trips/01-create", "--dry-run")
        self.assertEqual(self.cli_json("brief")[1]["projects"][0]["suites"], {})
        for rid in ("demo-1", "demo-2"):  # as if they had run on a simulator
            path = self.run_path(rid, "request.json")
            with open(path) as f:
                req = json.load(f)
            os.chmod(path, 0o644)  # a finished run is sealed
            with open(path, "w") as f:
                json.dump(dict(req, dry_run=False), f)
        suites = self.cli_json("brief")[1]["projects"][0]["suites"]
        self.assertEqual({s: (r["run"], r["status"], r["failed"]) for s, r in suites.items()},
                         {"trips": (whole, "failed", ["trips/02-fail-edit"]), "smoke": (whole, "passed", [])})
        self.assertIn("trips        failed  demo-1  trips/02-fail-edit", self.cli("brief").stdout)
