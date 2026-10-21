"""Console HTTP tests against dev fixtures. Run from e2e/:
    /usr/bin/python3 -m unittest discover -s console/tests -t .
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from console import cli, data, dev_fixtures, server  # noqa: E402

DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/605.1.15 Safari/605.1.15"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


REAL_RUN = subprocess.run


def fake_cli(argv, **kw):
    """Stands in for subprocess.run, but only for bin/e2e calls (vm_stat etc. run for real)."""
    if argv[0] != "/nonexistent/e2e":
        return REAL_RUN(argv, **kw)
    cmd = argv[1]
    if cmd == "projects":
        out = [dict(v, name=k) for k, v in dev_fixtures.PROJECTS.items()]
    elif cmd == "run":
        out = {"run": "camipack-99", "pid": 4242, "detached": True}
    elif cmd == "stop":
        out = [{"run": argv[2], "signalled": True}]
    elif cmd == "plan":
        out = {"estimate_s": 300, "sims": 2, "needs_build": True}
    elif cmd == "triage":
        out = {"run": argv[2], "flow": argv[3], "mark": argv[argv.index("--mark") + 1],
               "by": argv[argv.index("--by") + 1], "note": ""}
    else:
        out = {}
    return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")


class ConsoleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="e2e-console-test-"))
        cls.home = dev_fixtures.make(cls.tmp / "home")
        cls._env = dict(os.environ)
        os.environ["E2E_HOME"] = str(cls.home)
        os.environ["E2E_BIN"] = "/nonexistent/e2e"
        os.environ.pop("E2E_CONSOLE_DRY_RUN", None)
        cls.patch = mock.patch.object(cli.subprocess, "run", side_effect=fake_cli)
        cls.run_mock = cls.patch.start()
        cls.cfg = server.Config("op", "s3cret-test")
        cls.srv = server.make_server("127.0.0.1", 0, cls.cfg)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.patch.stop()
        os.environ.clear()
        os.environ.update(cls._env)
        dev_fixtures.unlock(cls.tmp)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        cli.clear_cache()
        self.run_mock.reset_mock()

    def req(self, path, method="GET", body=None, auth=("op", "s3cret-test"), ua=DESKTOP, csrf=True, headers=None):
        h = {"User-Agent": ua}
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode()
        if csrf and method == "POST":
            h["X-CSRF-Token"] = self.cfg.csrf()
        h.update(headers or {})
        raw = json.dumps(body).encode() if body is not None else None
        if raw is not None:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=raw, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def acting_calls(self):
        return [c.args[0] for c in self.run_mock.call_args_list
                if c.args[0][0] == "/nonexistent/e2e" and c.args[0][1] in ("run", "stop")]

    def start_body(self, **kw):
        b = {"project": "camipack", "suites": ["trips", "budget"], "mode": "scripted", "confirm": True}
        b.update(kw)
        return b

    # -- auth and roles

    def test_auth_required(self):
        for path in ("/", "/runs", "/api/runs/camipack-3", "/static/console.css"):
            code, _, hdr = self.req(path, auth=None)
            self.assertEqual(code, 401, path)
            self.assertIn("Basic", hdr["WWW-Authenticate"])
        self.assertEqual(self.req("/", auth=("op", "wrong"))[0], 401)
        self.assertEqual(self.req("/", auth=("nope", "s3cret-test"))[0], 401)
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), auth=None)[0], 401)

    def test_pages_render(self):
        for path in ("/", "/projects", "/projects/camipack", "/runs", "/triage", "/machine",
                     "/runs/camipack-4", "/runs/camipack-4/fragment", "/runs/camipack-3/report",
                     "/runs/camipack-3/junit"):
            code, body, hdr = self.req(path)
            self.assertEqual(code, 200, path)
            self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
            self.assertEqual(hdr["X-Frame-Options"], "DENY")
            self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
        body = self.req("/runs/camipack-4")[1].decode()
        self.assertIn('data-stop="camipack-4"', body)

    def test_mobile_is_viewer(self):
        for path in ("/", "/runs/camipack-4", "/runs/camipack-3/report"):
            body = self.req(path, ua=IPHONE)[1].decode()
            for marker in ("data-open=", "data-stop=", "data-triage=", "<dialog"):
                self.assertNotIn(marker, body, path)
            self.assertIn("view only", body)
        code, _, _ = self.req("/api/runs", "POST", self.start_body(), ua=IPHONE)
        self.assertEqual(code, 403)
        code, _, _ = self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "budget/03-huge-amount-fail", "mark": "fix"}, ua=IPHONE)
        self.assertEqual(code, 403)
        code, _, _ = self.req("/api/runs/camipack-4/stop", "POST", {"confirm": True}, ua=IPHONE)
        self.assertEqual(code, 403)
        self.assertEqual(self.acting_calls(), [])

    def test_csrf_required(self):
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), csrf=False)[0], 403)
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), csrf=False,
                                  headers={"X-CSRF-Token": "0" * 64})[0], 403)
        self.assertEqual(self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "x", "mark": "fix"}, csrf=False)[0], 403)
        self.assertEqual(self.acting_calls(), [])

    # -- start / stop

    def test_invalid_input_400(self):
        for body in (self.start_body(suites=["trips", "rm -rf /"]), self.start_body(project="nope"),
                     self.start_body(suites=[]), self.start_body(suites="trips"), self.start_body(mode="hunt"),
                     self.start_body(project="../camipack")):
            code, resp, _ = self.req("/api/runs", "POST", body)
            self.assertEqual(code, 400, (body, resp))
        code, _, _ = self.req("/api/runs", "POST", self.start_body(confirm=None))
        self.assertEqual(code, 400)
        runs = [c for c in self.run_mock.call_args_list if c.args[0][1] == "run"]
        self.assertEqual(runs, [])

    def test_start_calls_cli_exact_argv(self):
        code, resp, _ = self.req("/api/runs", "POST", self.start_body())
        self.assertEqual(code, 201, resp)
        self.assertEqual(json.loads(resp)["run"], "camipack-99")
        runs = [c for c in self.run_mock.call_args_list if c.args[0][1] == "run"]
        self.assertEqual(len(runs), 1)
        argv = runs[0].args[0]
        self.assertEqual(argv, ["/nonexistent/e2e", "run", "camipack", "--suite", "trips,budget",
                                "--mode", "scripted", "--by", "console", "--detach", "--json"])
        self.assertFalse(runs[0].kwargs.get("shell"))

    def test_stop_only_console_runs(self):
        code, _, _ = self.req("/api/runs/babylogly-1/stop", "POST", {"confirm": True})
        self.assertEqual(code, 403)  # requested_by cli
        code, _, _ = self.req("/api/runs/camipack-4/stop", "POST", {"confirm": True})
        self.assertEqual(code, 202)
        stops = [c.args[0] for c in self.run_mock.call_args_list if c.args[0][1] == "stop"]
        self.assertEqual(stops, [["/nonexistent/e2e", "stop", "camipack-4", "--json"]])
        self.assertEqual(self.req("/api/runs/camipack-3/stop", "POST", {"confirm": True})[0], 403)
        self.assertEqual(self.req("/api/runs/camipack-77/stop", "POST", {"confirm": True})[0], 404)

    # -- files and integrity

    def test_path_traversal_404(self):
        ok = self.req("/runs/camipack-3/files/flows/budget/03-huge-amount-fail/screenshot.png")
        self.assertEqual(ok[0], 200)
        self.assertEqual(ok[2]["Content-Type"], "image/png")
        for p in ("/runs/camipack-3/files/..%2f..%2fhistory.ndjson",
                  "/runs/camipack-3/files/%2e%2e/%2e%2e/triage.ndjson",
                  "/runs/camipack-3/files/..%2f..%2f..%2fprojects.json",
                  "/runs/camipack-3/files/%2fetc%2fpasswd",
                  "/runs/..%2f..%2fetc/files/passwd.txt",
                  "/runs/camipack-3/files/request.json%00.png",
                  "/runs/camipack-3/files/events.ndjson",
                  "/runs/camipack-3/files/flows"):
            self.assertEqual(self.req(p)[0], 404, p)
        # A symlink inside the run folder pointing outside must not be followed out.
        d = self.home / "camipack/runs/camipack-4"
        link = d / "leak.json"
        os.symlink(str(self.home / "projects.json"), str(link))
        try:
            self.assertEqual(self.req("/runs/camipack-4/files/leak.json")[0], 404)
        finally:
            link.unlink()

    def test_tampered_report_flagged(self):
        self.assertEqual(data.load_run("camipack-2")["integrity"], "tampered")
        self.assertEqual(data.load_run("camipack-3")["integrity"], "ok")
        body = self.req("/runs/camipack-2/report")[1].decode()
        self.assertIn("tampered", body)
        self.assertNotIn("pill bad tampered", self.req("/runs/camipack-3/report")[1].decode())

    def test_triage_goes_through_cli(self):
        code, resp, _ = self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "budget/03-huge-amount-fail",
                                                         "mark": "not-a-bug", "note": "expected"})
        self.assertEqual(code, 201, resp)
        argv = [c.args[0] for c in self.run_mock.call_args_list if c.args[0][0] == "/nonexistent/e2e"][-1]
        self.assertEqual(argv[1:], ["triage", "camipack-3", "budget/03-huge-amount-fail", "--mark", "not-a-bug",
                                    "--by", "console:op", "--note", "expected", "--json"])
        self.assertEqual(self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "budget/03-huge-amount-fail", "mark": "delete"})[0], 400)
        self.assertEqual(self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "nope", "mark": "fix"})[0], 400)
        self.assertEqual(self.req("/api/triage", "POST", {"run": "camipack-4", "flow": "trips/07-rename", "mark": "fix"})[0], 404)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="e2e-console-env-"))
        self.path = self.tmp / "console.env"
        self._env = os.environ.get("E2E_CONSOLE_ENV")
        os.environ["E2E_CONSOLE_ENV"] = str(self.path)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("E2E_CONSOLE_ENV", None)
        else:
            os.environ["E2E_CONSOLE_ENV"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            server.load_config()
        self.assertIn("chmod 600", str(cm.exception.code))

    def test_mode_must_be_600(self):
        self.path.write_text("E2E_CONSOLE_USER=a\nE2E_CONSOLE_PASS=b\n")
        self.path.chmod(0o644)
        with self.assertRaises(SystemExit):
            server.load_config()
        self.path.chmod(0o600)
        cfg = server.load_config()
        self.assertEqual((cfg.user, cfg.password), (b"a", b"b"))


if __name__ == "__main__":
    unittest.main()
