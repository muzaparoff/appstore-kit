import json
import os
import stat
from unittest import mock

from e2ekit import store
from tests.helpers import HomeCase


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class StoreTests(HomeCase):
    def test_atomic_write_keeps_old_file_on_failure(self):
        path = os.path.join(store.ensure_dir(self.home), "state.json")
        store.atomic_write_json(path, {"status": "running"})
        with mock.patch("json.dump", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                store.atomic_write_json(path, {"status": "passed"})
        self.assertEqual(store.read_json(path), {"status": "running"})
        self.assertEqual([n for n in os.listdir(self.home) if n.startswith(".tmp")], [])

    def test_ndjson_append(self):
        path = os.path.join(store.ensure_dir(self.home), "e.ndjson")
        for i in range(3):
            store.append_ndjson(path, {"i": i})
        self.assertEqual([e["i"] for e in store.read_ndjson(path)], [0, 1, 2])

    def test_run_ids_and_numbering(self):
        for rid in ("demo-1", "demo-2", "demo-10", "demo-x"):
            os.makedirs(os.path.join(self.home, "demo", "runs", rid))
        self.assertEqual(store.run_ids("demo"), ["demo-1", "demo-2", "demo-10"])
        self.assertEqual(store.next_run_id("demo"), "demo-11")
        self.assertEqual(store.project_of("my-app-3"), "my-app")

    def _finished_run(self, rid, failed=()):
        rdir = store.run_dir(rid)
        flows = ["trips/01-ok", "trips/02-fail"]
        for f in flows:
            base = os.path.join(rdir, "flows", f)
            write(os.path.join(base, "junit.xml"))
            write(os.path.join(base, "log.txt"))
            write(os.path.join(base, "video.mp4"))
            write(os.path.join(base, "maestro", "commands.json"))
            if f in failed:
                write(os.path.join(base, "screenshot.png"))
        write(os.path.join(rdir, "build.log"))
        write(os.path.join(rdir, "live", "sim-1.png"))
        store.atomic_write_json(os.path.join(rdir, "report.json"), {
            "run": rid, "failures": [{"flow": f} for f in failed]})
        store.make_read_only(rdir)
        store.append_ndjson(os.path.join(store.project_dir("demo"), "history.ndjson"), {
            "run": rid, "sha256": store.sha256_file(os.path.join(rdir, "report.json"))})
        return rdir

    def test_read_only_and_tamper_detection(self):
        rdir = self._finished_run("demo-1")
        report = os.path.join(rdir, "report.json")
        self.assertEqual(stat.S_IMODE(os.stat(report).st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(os.stat(rdir).st_mode), 0o555)
        self.assertEqual(store.verify("demo"), [{"run": "demo-1", "status": "ok"}])
        os.chmod(report, 0o644)
        with open(report, "w") as f:
            json.dump({"run": "demo-1", "status": "passed"}, f)
        self.assertEqual(store.verify("demo")[0]["status"], "tampered")

    def test_retention_keeps_reports(self):
        old = self._finished_run("demo-1", failed=["trips/02-fail"])
        last = self._finished_run("demo-2", failed=["trips/02-fail"])
        os.makedirs(store.run_dir("demo-3"))
        store.retention("demo", keep_run="demo-3")
        for rdir in (old, last):
            self.assertTrue(os.path.exists(os.path.join(rdir, "report.json")))
            self.assertFalse(os.path.exists(os.path.join(rdir, "live")))
            self.assertFalse(os.path.exists(os.path.join(rdir, "build.log")))
            for f in ("01-ok", "02-fail"):
                base = os.path.join(rdir, "flows", "trips", f)
                self.assertTrue(os.path.exists(os.path.join(base, "junit.xml")))
                self.assertFalse(os.path.exists(os.path.join(base, "log.txt")))
                self.assertFalse(os.path.exists(os.path.join(base, "maestro")))
                self.assertFalse(os.path.exists(os.path.join(base, "video.mp4")) and f == "01-ok")
            self.assertTrue(os.path.exists(os.path.join(rdir, "flows/trips/02-fail/screenshot.png")))
        # only the latest previous run keeps its failed flow's video
        self.assertFalse(os.path.exists(os.path.join(old, "flows/trips/02-fail/video.mp4")))
        self.assertTrue(os.path.exists(os.path.join(last, "flows/trips/02-fail/video.mp4")))
        # everything kept is still read-only, and hashes still verify
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(last, "flows/trips")).st_mode), 0o555)
        self.assertEqual({r["status"] for r in store.verify("demo")}, {"ok"})

    def test_prune(self):
        for i in (1, 2, 3):
            self._finished_run("demo-%d" % i)
        self.assertEqual(store.prune("demo", 1), ["demo-1", "demo-2"])
        self.assertEqual(store.run_ids("demo"), ["demo-3"])
        self.assertEqual([h["run"] for h in store.read_ndjson(
            os.path.join(store.project_dir("demo"), "history.ndjson"))], ["demo-3"])
