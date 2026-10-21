"""Whole runs through the CLI against the fake backend (--dry-run)."""

import json
import os
import stat
import threading
import time

from e2ekit import locks, store
from tests.helpers import HomeCase


class DryRunTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.assertEqual(self.cli("add", self.repo).returncode, 0)

    def test_projects_and_plan(self):
        code, items = self.cli_json("projects")
        self.assertEqual(code, 0)
        self.assertEqual([s["name"] for s in items[0]["suites"]], ["smoke", "trips", "ui"])
        code, plan = self.cli_json("plan", "demo", "--suite", "smoke,trips")
        self.assertEqual((code, plan["flows"], plan["sims"], plan["needs_build"]), (0, 4, 2, True))
        code, err = self.cli_json("plan", "demo", "--suite", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown suite", err["error"])

    def test_full_run_report(self):
        code, out = self.cli_json("run", "demo", "--suite", "smoke,trips,ui", "--dry-run")
        self.assertEqual((code, out["run"], out["status"]), (1, "demo-1", "failed"))
        with open(self.run_path("demo-1", "report.json")) as f:
            rep = json.load(f)
        by_id = {f["id"]: f for f in rep["flows"]}
        self.assertEqual(by_id["trips/02-fail-edit"]["status"], "failed")
        self.assertEqual(by_id["trips/02-fail-edit"]["attempts"], 2)
        self.assertEqual(by_id["trips/03-flaky-share"]["status"], "flaky")
        self.assertEqual(by_id["smoke/01-launch"]["status"], "passed")
        self.assertEqual(by_id["ui/PaywallTests"]["status"], "passed")
        self.assertEqual(rep["counts"], {"total": 5, "passed": 3, "failed": 1, "flaky": 1,
                                         "skipped": 0, "running": 0, "pending": 0})
        fail = rep["failures"][0]
        self.assertEqual((fail["flow"], fail["kind"]), ("trips/02-fail-edit", "failed"))
        self.assertTrue(fail["repro"] and fail["log_excerpt"])
        self.assertTrue(os.path.exists(self.run_path("demo-1", fail["screenshot"])))
        self.assertEqual(rep["limits"], {"flow_s": 240, "suite_s": 1500, "run_s": 5400,
                                         "sims": 2, "retries": 1})
        self.assertIn("disk_free_gb", rep["machine"])
        # failed attempt's evidence kept beside the retry
        self.assertTrue(os.path.exists(self.run_path(
            "demo-1", "flows/trips/03-flaky-share/attempt-1/log.txt")))
        # finalized: read-only, no live/, history hash matches, quarantine counted
        self.assertFalse(os.path.exists(self.run_path("demo-1", "live")))
        self.assertEqual(stat.S_IMODE(os.stat(self.run_path("demo-1")).st_mode), 0o555)
        code, ver = self.cli_json("verify", "demo")
        self.assertEqual((code, ver[0]["status"]), (0, "ok"))
        self.assertEqual(store.load_quarantine("demo")["trips/03-flaky-share"]["flaky_runs"], 1)
        with open(self.run_path("demo-1", "request.json")) as f:
            self.assertEqual(json.load(f)["dry_run"], True)
        types = [e["type"] for e in self.events("demo-1")]
        for t in ("queued", "build_start", "build_end", "sim_up", "flow_start", "step",
                  "flow_end", "sim_down", "done"):
            self.assertIn(t, types)
        self.assertEqual(types[-1], "done")
        self.assertEqual(locks.slot_usage(), {"build_busy": False, "sims_busy": [], "sims_cap": 3})

    def test_live_gone_after_finalize(self):
        # Slow flows keep the run up long enough for several live frames.
        self.make_repo(flows={"smoke": ["01-slow-a", "02-slow-b"], "trips": ["x"]})
        for _ in range(2):
            code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run", "--detach")
            self.wait_terminal(out["run"])
            time.sleep(1.5)  # a late frame would show up within a tick
            self.assertFalse(os.path.exists(self.run_path(out["run"], "live")))

    def test_triage(self):
        self.cli_json("run", "demo", "--suite", "trips", "--dry-run")
        code, entry = self.cli_json("triage", "demo-1", "trips/02-fail-edit", "--mark", "not-a-bug",
                                    "--by", "owner", "--note", "copy changed")
        self.assertEqual((code, entry["mark"], entry["by"]), (0, "not-a-bug", "owner"))
        lines = store.read_ndjson(os.path.join(self.home, "demo", "triage.ndjson"))
        self.assertEqual([set(l) for l in lines], [{"ts", "run", "flow", "mark", "by", "note"}])
        code, err = self.cli_json("triage", "demo-1", "trips/nope", "--mark", "fix", "--by", "x")
        self.assertEqual(code, 1)
        r = self.cli("triage", "demo-1", "trips/01-create", "--mark", "wontfix", "--by", "x", check=False)
        self.assertEqual(r.returncode, 2)

    def test_all_pass_exit_zero_and_numbering(self):
        code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run")
        self.assertEqual((code, out["status"]), (0, "passed"))
        code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run")
        self.assertEqual(out["run"], "demo-2")

    def test_quarantined_flow_does_not_fail_run(self):
        store.save_quarantine("demo", {"trips/02-fail-edit": {"flaky_runs": 3, "since": "x"}})
        code, out = self.cli_json("run", "demo", "--suite", "trips", "--dry-run")
        self.assertEqual(out["status"], "passed")

    def test_suite_timer_skips_rest(self):
        repo = self.make_repo("timed", flows={"smoke": ["01-slow-a", "02-slow-b", "03-ok"],
                                              "trips": ["01-x"]},
                              extra="limits: {sims: 1, suite: 2s}")
        self.cli("add", repo)
        code, out = self.cli_json("run", "timed", "--suite", "smoke", "--dry-run")
        self.assertEqual(out["status"], "failed")
        with open(self.run_path(out["run"], "report.json")) as f:
            rep = json.load(f)
        statuses = [f["status"] for f in rep["flows"]]
        self.assertEqual(statuses[0], "timeout")
        self.assertIn("skipped", statuses)
        self.assertTrue(any(e["type"] == "limit_hit" and e.get("limit") == "suite"
                            for e in self.events(out["run"])))

    def test_low_ram_waits_then_errors(self):
        repo = self.make_repo("lowram", extra="limits: {run: 2s}")
        self.cli("add", repo)
        code, out = self.cli_json("run", "lowram", "--suite", "smoke", "--dry-run",
                                  env={"E2E_FAKE_RAM_GB": "1.5"})
        self.assertEqual((code, out["status"]), (1, "error"))
        with open(self.run_path(out["run"], "report.json")) as f:
            rep = json.load(f)
        self.assertEqual(rep["counts"]["skipped"], 1)
        self.assertEqual(rep["sims"], [])

    def test_low_disk_refuses_run(self):
        code, err = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run",
                                  env={"E2E_FAKE_DISK_GB": "5"})
        self.assertEqual(code, 1)
        self.assertIn("disk", err["error"])
        self.assertEqual(store.run_ids("demo"), [])

    def test_project_lock_refuses_second_run(self):
        self.make_repo(flows={"smoke": ["01-slow-a", "02-slow-b"], "trips": ["x"]})
        code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run", "--detach")
        self.assertEqual(code, 0)
        rid = out["run"]
        code, err = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("already has a run", err["error"])
        self.wait_terminal(rid)
        self.assertEqual(sorted(store.run_ids("demo")), [rid])

    def test_stop_detached_run(self):
        self.make_repo(flows={"smoke": ["01-slow-a", "02-slow-b", "03-slow-c", "04-slow-d"],
                              "trips": ["x"]})
        code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run", "--detach",
                                  "--by", "console")
        rid = out["run"]
        with open(self.run_path(rid, "state.json")) as f:
            pid = json.load(f)["pid"]
        self.assertEqual(pid, out["pid"])
        self.assertEqual(os.getpgid(pid), pid)  # own process group
        deadline = time.time() + 10
        while time.time() < deadline:
            with open(self.run_path(rid, "state.json")) as f:
                if json.load(f)["status"] == "running":
                    break
            time.sleep(0.1)
        time.sleep(1.2)  # let the live screenshot ticker fire
        self.assertTrue(os.path.exists(self.run_path(rid, "live", "sim-1.png")))
        code, res = self.cli_json("stop", rid, "--wait", "15")
        self.assertEqual((code, res[0]["signalled"], res[0]["status"]), (0, True, "stopped"))
        st = self.wait_terminal(rid)
        self.assertEqual(st["status"], "stopped")
        self.assertEqual(st["sims"], [])
        self.assertFalse(os.path.exists(self.run_path(rid, "live")))
        self.assertTrue(os.path.exists(self.run_path(rid, "report.json")))
        self.assertIn("stop_requested", [e["type"] for e in self.events(rid)])
        eng = os.path.join(self.home, "engine")
        for name in os.listdir(eng):
            if name.endswith(".lock"):
                self.assertFalse(locks.is_busy(os.path.join(eng, name)), name)
        code, res = self.cli_json("stop", rid)
        self.assertFalse(res[0]["signalled"])
        # the project is free again
        code, out = self.cli_json("run", "demo", "--suite", "smoke", "--dry-run", "--detach")
        self.assertEqual(code, 0)
        self.wait_terminal(out["run"])

    def test_parallel_projects_share_build_slot(self):
        other = self.make_repo("other")
        self.cli("add", other)
        env = {"E2E_FAKE_BUILD_S": "1.0"}
        results = {}

        def go(project):
            results[project] = self.cli_json("run", project, "--suite", "smoke", "--dry-run", env=env)

        threads = [threading.Thread(target=go, args=(p,)) for p in ("demo", "other")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        spans = []
        for project, (code, out) in results.items():
            self.assertEqual(out["status"], "passed", project)
            ev = {e["type"]: e["ts"] for e in self.events(out["run"])}
            spans.append((ev["build_start"], ev["build_end"]))
        spans.sort()
        # ISO timestamps from one clock compare as strings
        self.assertLessEqual(spans[0][1], spans[1][0], spans)
        _, status = self.cli_json("status")
        self.assertEqual(status["runs"], [])
