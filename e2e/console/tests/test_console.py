"""Console HTTP tests against dev fixtures. Run from e2e/:
    /usr/bin/python3 -m unittest discover -s console/tests -t .
"""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
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
    elif cmd == "add":
        out = {"name": argv[argv.index("--name") + 1], "path": argv[2], "suites": ["smoke"]}
    elif cmd == "triage":
        out = {"run": argv[2], "flow": argv[3], "mark": argv[argv.index("--mark") + 1],
               "by": argv[argv.index("--by") + 1], "note": ""}
    elif cmd == "bug":
        out, code = dev_fixtures.fake_bug(os.environ["E2E_HOME"], argv[2:-1])
        return subprocess.CompletedProcess(argv, code, json.dumps(out), "")
    else:
        out = {}
    return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


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
        dev_fixtures.make_bugs(self.home)  # the bug tests move cards
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

    def bug_writes(self):
        """argv (after the binary) of every `e2e bug …` call that is not a read."""
        return [c.args[0][1:] for c in self.run_mock.call_args_list
                if c.args[0][0] == "/nonexistent/e2e" and c.args[0][1] == "bug" and c.args[0][2] not in ("list", "show")]

    def bug(self, path, body=None, **kw):
        code, resp, _ = self.req("/api/bugs/" + path, "POST", {} if body is None else body, **kw)
        return code, json.loads(resp)

    def start_body(self, **kw):
        b = {"project": "camipack", "suites": ["trips", "budget"], "mode": "scripted", "confirm": True}
        b.update(kw)
        return b

    # -- auth and roles

    def test_auth_required(self):
        for path in ("/", "/runs"):
            code, body, hdr = self.req(path, auth=None)
            self.assertEqual(code, 401, path)
            self.assertIsNone(hdr["WWW-Authenticate"], "form, not the Basic popup")
            self.assertIn(b'action="/login"', body)
        code, body, _ = self.req("/api/runs/camipack-3", auth=None)
        self.assertEqual((code, json.loads(body)), (401, {"error": "sign in first"}))
        self.assertEqual(self.req("/static/console.css", auth=None)[0], 200)
        self.assertEqual(self.req("/static/console.js", auth=None)[0], 401)
        self.assertEqual(self.req("/", auth=("op", "wrong"))[0], 401)
        self.assertEqual(self.req("/", auth=("nope", "s3cret-test"))[0], 401)
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), auth=None)[0], 401)

    def login(self, user, pw, nxt="/runs"):
        body = urllib.parse.urlencode({"username": user, "password": pw, "next": nxt}).encode()
        r = urllib.request.Request(self.base + "/login", data=body, method="POST",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(r, timeout=10) as resp:
                resp.headers["X-Body"] = resp.read().decode()
                return resp.status, resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def test_login_sets_session_cookie(self):
        code, hdr = self.login("op", "s3cret-test", "/runs")
        self.assertEqual(code, 200)
        cookie = hdr["Set-Cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Lax", "Max-Age=7776000"):
            self.assertIn(flag, cookie)
        jar = cookie.split(";")[0]
        self.assertEqual(self.req("/runs", auth=None, headers={"Cookie": jar})[0], 200)
        # Split across headers, as Cloudflare forwards an HTTP/2 browser's cookies.
        split = http.client.HTTPConnection(self.base[7:], timeout=10)
        split.putrequest("GET", "/runs")
        split.putheader("Cookie", "__cf_bm=x")
        split.putheader("Cookie", jar)
        split.endheaders()
        self.assertEqual(split.getresponse().status, 200)
        split.close()
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), auth=None, headers={"Cookie": jar})[0], 201)
        name, _, val = jar.partition("=")
        exp, _, sig = val.partition(".")
        forged = "%s=%d.%s" % (name, int(exp) + 1, sig)
        self.assertEqual(self.req("/runs", auth=None, headers={"Cookie": forged})[0], 401)

    def test_session_renews_on_use(self):
        old = int(server.time.time()) + server.SESSION_S - 2 * server.RENEW_S
        jar = "%s=%d.%s" % (server.COOKIE, old, self.cfg.session_sig(old))
        code, _, hdr = self.req("/runs", auth=None, headers={"Cookie": jar})
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=7776000", hdr["Set-Cookie"])
        fresh = hdr["Set-Cookie"].split(";")[0]
        self.assertIsNone(self.req("/runs", auth=None, headers={"Cookie": fresh})[2]["Set-Cookie"])

    def test_csrf_survives_restart(self):
        self.assertEqual(server.Config("op", "s3cret-test").csrf(), self.cfg.csrf())
        self.assertNotEqual(server.Config("op", "other").csrf(), self.cfg.csrf())

    def test_login_rejects_bad_password_and_foreign_next(self):
        self.assertEqual(self.login("op", "wrong")[0], 401)
        for nxt in ("//evil.example/", "https://evil.example/", "/\\evil.example"):
            self.assertIn('content="0;url=/"', self.login("op", "s3cret-test", nxt)[1]["X-Body"], nxt)

    def test_logout_clears_cookie(self):
        r = urllib.request.Request(self.base + "/logout", data=b"", method="POST")
        try:
            urllib.request.build_opener(NoRedirect).open(r, timeout=10)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 303)
            self.assertIn("Max-Age=0", e.headers["Set-Cookie"])

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

    def test_add_project(self):
        repo = self.tmp / "repo"
        (repo / "e2e").mkdir(parents=True)
        with mock.patch.object(os.path, "expanduser", return_value=str(self.tmp)):
            code, resp, _ = self.req("/api/projects", "POST", {"path": str(repo), "name": "newapp"})
            self.assertEqual(code, 201, resp)
            argv = [c.args[0] for c in self.run_mock.call_args_list if c.args[0][0] == "/nonexistent/e2e"][-1]
            self.assertEqual(argv[1:], ["add", os.path.realpath(str(repo)), "--name", "newapp", "--json"])
            self.assertEqual(self.req("/api/projects", "POST", {"path": "/etc", "name": "x"})[0], 400)
            self.assertEqual(self.req("/api/projects", "POST", {"path": str(repo), "name": "Bad Name"})[0], 400)
            self.assertEqual(self.req("/api/projects", "POST", {"path": str(repo), "name": "ok"}, ua=IPHONE)[0], 403)

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

    # -- bug board

    def test_bug_pages_render(self):
        code, _, hdr = self.req("/bugs", headers={"Accept": "text/html"})
        self.assertEqual(code, 200, "only camipack has cards: /bugs goes straight to its board")
        opener = urllib.request.build_opener(NoRedirect)
        r = urllib.request.Request(self.base + "/bugs", headers={
            "User-Agent": DESKTOP, "Authorization": "Basic " + base64.b64encode(b"op:s3cret-test").decode()})
        with self.assertRaises(urllib.error.HTTPError) as e:
            opener.open(r, timeout=10)
        self.assertEqual((e.exception.code, e.exception.headers["Location"]), (303, "/bugs/camipack"))

        code, body, hdr = self.req("/bugs/camipack")
        board = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
        heads = [board.index('id="s-%s"' % s) for s in ("found", "open", "poc", "inprogress", "testing", "done", "rejected")]
        self.assertEqual(heads, sorted(heads), "Needs you first, then the contract's order")
        for text in ("Needs you", "Design freeze: on", "Days list stutters on fast scroll", "Open a 14-day trip. Scroll Days fast.",
                     'class="pr q"', "P1?", 'data-approve="P1"', 'data-approve="P2"', 'data-approve="P3"', "data-rejectbox",
                     "fix/b3-tab-bar-keyboard", "Pass 2 of 3", "green not ok", "review ok", "camipack-7 passed, camipack-8 passed",
                     "fixer-1", "moving the row state out of the list", "nobody yet", "Rejected: same as B-2", "data-reopen",
                     'data-open="bug-add"', '<dialog id="bug-add"'):
            self.assertIn(text, board)
        self.assertEqual(board.count('class="pill bad">stale'), 1, "only B-5's note is older than 15 minutes")
        self.assertIn('class="bc stale" data-bug="B-5"', board)
        for bad in ("confirm(", "prompt(", "<script>", "onclick"):
            self.assertNotIn(bad, board)

        frag = self.req("/bugs/camipack/fragment")[1].decode()
        self.assertTrue(frag.startswith('<div class="segs">'), frag[:80])
        self.assertNotIn("<html", frag)

        code, body, _ = self.req("/bugs/camipack/B-3")
        card = body.decode()
        self.assertEqual(code, 200)
        for text in ("Tab bar jumps when the keyboard opens", "History", "via phone", "light and dark look right",
                     "Pass 2 of 3", "suite not ok", 'data-open="bug-edit"', 'data-bugedit="B-3"', "data-rejectopen"):
            self.assertIn(text, card)
        self.assertEqual(self.req("/bugs/camipack/B-3/fragment")[0], 200)
        self.assertNotIn("data-open=", self.req("/bugs/camipack/B-2")[1].decode(), "a done card is reopened, not edited")
        code, body, _ = self.req("/bugs/babylogly")
        self.assertEqual(code, 200)
        self.assertIn("Nothing waits for you.", body.decode())
        self.assertNotIn("Design freeze", body.decode())

        for path in ("/bugs/nope", "/bugs/camipack/B-77", "/bugs/camipack/7", "/bugs/camipack/B-0", "/bugs/..%2Fcamipack",
                     "/bugs/camipack/B-3/history", "/bugs/camipack/B-3/fragment/x"):
            self.assertEqual(self.req(path)[0], 404, path)
        self.assertEqual(self.bug_writes(), [], "pages only read")
        reads = {tuple(c.args[0][1:]) for c in self.run_mock.call_args_list if c.args[0][1] == "bug"}
        self.assertIn(("bug", "list", "camipack", "--json"), reads)
        self.assertIn(("bug", "show", "camipack", "B-3", "--json"), reads)

    def test_bug_text_is_escaped(self):
        code, card = self.bug("camipack", {"title": '<img src=x onerror=alert(1)> "q"', "see": "</textarea><b>x"})
        self.assertEqual(code, 201, card)
        for path in ("/bugs/camipack", "/bugs/camipack/" + card["id"]):
            body = self.req(path)[1].decode()
            self.assertNotIn("<img src=x", body, path)
            self.assertNotIn("</textarea><b>x", body, path)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", body, path)

    def test_bug_phone_decides_and_nothing_else(self):
        board = self.req("/bugs/camipack", ua=IPHONE)[1].decode()
        for marker in ("data-open=", "<dialog", "data-bugadd", "+ Add bug"):
            self.assertNotIn(marker, board)
        for marker in ("view + decide", 'data-approve="P1"', "data-rejectopen", "data-reopen", 'name="csrf"'):
            self.assertIn(marker, board)
        card = self.req("/bugs/camipack/B-7", ua=IPHONE)[1].decode()
        self.assertNotIn("data-open=", card)
        self.assertNotIn("<dialog", card)
        self.assertNotIn("data-filebug", self.req("/triage", ua=IPHONE)[1].decode())

        code, card = self.bug("camipack/B-8/approve", {"priority": "P1"}, ua=IPHONE)
        self.assertEqual((code, card["state"], card["priority"]), (200, "open", "P1"))
        code, card = self.bug("camipack/B-9/reject", {"reason": "  same as B-4 "}, ua=IPHONE)
        self.assertEqual((code, card["state"], card["reason"]), (200, "rejected", "same as B-4"))
        code, card = self.bug("camipack/B-2/reopen", ua=IPHONE)
        self.assertEqual((code, card["state"]), (200, "open"))
        self.assertEqual(self.bug_writes(), [
            ["bug", "approve", "camipack", "B-8", "--priority", "P1", "--by", "owner", "--via", "phone", "--json"],
            ["bug", "reject", "camipack", "B-9", "--reason=same as B-4", "--by", "owner", "--via", "phone", "--json"],
            ["bug", "reopen", "camipack", "B-2", "--by", "owner", "--via", "phone", "--json"]])
        self.run_mock.reset_mock()

        # everything else a phone sends is still refused, bug or not
        self.assertEqual(self.bug("camipack", {"title": "from a phone"}, ua=IPHONE), (403, {"error": "view only on phones"}))
        self.assertEqual(self.bug("camipack/B-7/edit", {"title": "from a phone"}, ua=IPHONE)[0], 403)
        self.assertEqual(self.bug("camipack/B-7/edit", {"priority": "P1"}, ua=IPHONE)[0], 403)
        self.assertEqual(self.bug("camipack/B-7/approve/edit", {"priority": "P1"}, ua=IPHONE)[0], 404)
        self.assertEqual(self.bug("camipack/B-7/move", {"to": "done"}, ua=IPHONE)[0], 404)
        self.assertEqual(self.req("/api/runs", "POST", self.start_body(), ua=IPHONE)[0], 403)
        self.assertEqual(self.req("/api/runs/camipack-4/stop", "POST", {"confirm": True}, ua=IPHONE)[0], 403)
        self.assertEqual(self.req("/api/triage", "POST", {"run": "camipack-3", "flow": "budget/03-huge-amount-fail", "mark": "fix"},
                                  ua=IPHONE)[0], 403)
        # the three decisions still need the sign-in and the token
        self.assertEqual(self.bug("camipack/B-7/reject", {"reason": "x"}, ua=IPHONE, csrf=False)[0], 403)
        self.assertEqual(self.bug("camipack/B-7/reject", {"reason": "x"}, ua=IPHONE, auth=None)[0], 401)
        self.assertEqual(self.bug_writes(), [])
        self.assertEqual(self.acting_calls(), [])

    def test_bug_operator_writes_exact_argv(self):
        code, card = self.bug("camipack/B-8/approve", {"priority": "P2"})
        self.assertEqual((code, card["state"]), (200, "open"))
        self.assertEqual(self.bug("camipack/B-7/reject", {"reason": "not a bug"})[0], 200)
        self.assertEqual(self.bug("camipack/B-1/reopen")[0], 200)
        code, card = self.bug("camipack/B-4/edit", {"title": " Packing list stutters ", "done_when": "", "priority": "P2"})
        self.assertEqual((code, card["state"], card["title"], card["done_when"], card["priority"]),
                         (200, "poc", "Packing list stutters", "", "P2"))
        code, card = self.bug("camipack", {"title": "-rf looks like an option", "do": "--json", "see": "", "want": "A\nB",
                                           "kind": "lag", "suggest": "P3"})
        self.assertEqual((code, card["id"], card["state"], card["title"], card["do"], card["by"]),
                         (201, "B-10", "found", "-rf looks like an option", "--json", "console"))
        code, card = self.bug("camipack", {"title": "Budget crash", "flow": "budget/03-huge-amount-fail", "run": "camipack-3",
                                           "suggest": ""})
        self.assertEqual((code, card["run"], card["flow"]), (201, "camipack-3", "budget/03-huge-amount-fail"))
        self.assertEqual(self.bug_writes(), [
            ["bug", "approve", "camipack", "B-8", "--priority", "P2", "--by", "owner", "--via", "console", "--json"],
            ["bug", "reject", "camipack", "B-7", "--reason=not a bug", "--by", "owner", "--via", "console", "--json"],
            ["bug", "reopen", "camipack", "B-1", "--by", "owner", "--via", "console", "--json"],
            ["bug", "edit", "camipack", "B-4", "--title=Packing list stutters", "--done-when=", "--priority", "P2",
             "--by", "owner", "--via", "console", "--json"],
            ["bug", "add", "camipack", "--title=-rf looks like an option", "--do=--json", "--want=A\nB",
             "--kind", "lag", "--suggest", "P3", "--by", "console", "--json"],
            ["bug", "add", "camipack", "--title=Budget crash", "--kind", "bug", "--flow", "budget/03-huge-amount-fail",
             "--run", "camipack-3", "--by", "console", "--json"]])
        for c in self.run_mock.call_args_list:
            self.assertIs(c.kwargs.get("shell"), False)
        self.assertIn("B-10", self.req("/bugs/camipack")[1].decode(), "the board is fresh after a write")

    def test_bug_engine_refusal_is_409(self):
        code, resp = self.bug("camipack/B-7/approve", {"priority": "P1"})
        self.assertEqual((code, resp), (409, {"error": "B-7 is open; only a found bug can be approved"}))
        self.assertEqual(self.bug("camipack/B-7/reopen")[0], 409)
        self.assertEqual(self.bug("camipack/B-2/reject", {"reason": "x"})[0], 409)
        self.assertEqual(self.bug("camipack/B-2/edit", {"title": "x"})[0], 409)
        self.assertEqual(self.bug("camipack/B-77/approve", {"priority": "P1"}), (409, {"error": "no bug B-77"}))

    def test_bug_csrf_required(self):
        for path, body in (("camipack", {"title": "x"}), ("camipack/B-8/approve", {"priority": "P1"}),
                           ("camipack/B-8/reject", {"reason": "x"}), ("camipack/B-2/reopen", {}),
                           ("camipack/B-7/edit", {"title": "x"})):
            for ua in (DESKTOP, IPHONE):
                self.assertEqual(self.bug(path, body, ua=ua, csrf=False)[0], 403, path)
            self.assertEqual(self.bug(path, body, csrf=False, headers={"X-CSRF-Token": "0" * 64})[0], 403, path)
            self.assertEqual(self.bug(path, body, auth=None)[0], 401, path)
        self.assertEqual(self.bug_writes(), [])

    def test_bug_invalid_input_refused(self):
        long = "x" * 2001
        bad = [
            ("nope/B-8/approve", {"priority": "P1"}), ("nope", {"title": "x"}), ("..%2Fcamipack/B-8/approve", {"priority": "P1"}),
            ("camipack/8/approve", {"priority": "P1"}), ("camipack/B-0/approve", {"priority": "P1"}),
            ("camipack/b-8/approve", {"priority": "P1"}), ("camipack/B-8;ls/approve", {"priority": "P1"}),
            ("camipack/--by/approve", {"priority": "P1"}), ("camipack/B-8%20B-9/approve", {"priority": "P1"}),
            ("camipack/B-8/approve", {}), ("camipack/B-8/approve", {"priority": "P4"}), ("camipack/B-8/approve", {"priority": "p1"}),
            ("camipack/B-8/approve", {"priority": ["P1"]}), ("camipack/B-8/approve", {"priority": "P1 --by lead"}),
            ("camipack/B-8/reject", {}), ("camipack/B-8/reject", {"reason": "  "}), ("camipack/B-8/reject", {"reason": "x" * 501}),
            ("camipack/B-8/reject", {"reason": 7}),
            ("camipack/B-7/edit", {}), ("camipack/B-7/edit", {"title": ""}), ("camipack/B-7/edit", {"title": "x" * 201}),
            ("camipack/B-7/edit", {"see": long}), ("camipack/B-7/edit", {"do": ["x"]}), ("camipack/B-7/edit", {"priority": "P9"}),
            ("camipack/B-7/edit", {"flow": "trips/01-create-trip"}),
            ("camipack", {}), ("camipack", {"title": "  "}), ("camipack", {"title": "x" * 201}), ("camipack", {"title": "x", "do": long}),
            ("camipack", {"title": "x", "kind": "feature"}), ("camipack", {"title": "x", "suggest": "P0"}),
            ("camipack", {"title": "x", "flow": "../../etc/passwd"}), ("camipack", {"title": "x", "flow": "--by"}),
            ("camipack", {"title": "x", "run": "camipack-77"}), ("camipack", {"title": "x", "run": "babylogly-1"}),
            ("camipack", {"title": "x", "run": "../camipack-3"}), ("camipack", {"title": "x", "run": 3}),
            ("camipack", {"title": "x", "run": "camipack-3", "flow": "trips/nope"}),
        ]
        for path, body in bad:
            code, resp = self.bug(path, body)
            self.assertEqual(code, 400, (path, body, resp))
        self.assertEqual(self.bug("camipack/B-8/delete")[0], 404)
        self.assertEqual(self.bug("camipack/B-8")[0], 404)
        self.assertEqual(self.req("/api/bugs/camipack/B-8/approve", "POST", csrf=True)[0], 400, "no JSON body")
        self.assertEqual(self.bug_writes(), [])

    def test_triage_files_a_bug(self):
        page = self.req("/triage")[1].decode()
        self.assertIn("data-filebug=", page)
        self.assertIn('<dialog id="bug-add"', page)
        self.assertIn('href="/bugs/camipack/B-9">B-9</a>', page, "the flow B-9 came from links to its card")
        fill = page.split('data-filebug="')[1].split('"')[0].replace("&quot;", '"').replace("&amp;", "&")
        fill = json.loads(fill.replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">"))
        self.assertEqual(sorted(fill), ["do", "flow", "project", "proof", "run", "see", "title"])
        self.assertTrue(fill["title"])
        project, body = fill.pop("project"), fill
        code, card = self.bug(project, body)
        self.assertEqual(code, 201, card)
        self.assertEqual((card["run"], card["flow"], card["state"]), (fill["run"], fill["flow"], "found"))
        argv = self.bug_writes()[-1]
        self.assertEqual(argv[:3] + argv[-9:], ["bug", "add", project, "--kind", "bug", "--flow", fill["flow"], "--run", fill["run"],
                                                 "--by", "console", "--json"])
        after = self.req("/triage")[1].decode()
        self.assertIn('href="/bugs/%s/%s">%s</a>' % (project, card["id"], card["id"]), after)
        self.assertEqual(after.count("data-filebug="), page.count("data-filebug=") - 1)


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
