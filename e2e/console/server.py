"""E2E console HTTP server (stdlib only). Roles and auth: e2e/CONTRACT.md "Console roles"."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from console import cli, data, pages  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
MOBILE_RE = re.compile(r"Mobi|iPhone|iPad|iPod|Android|Silk|Kindle|BlackBerry|Opera Mini|IEMobile", re.I)
MARKS = ("fix", "not-a-bug", "flaky")
MAX_BODY = 16 * 1024
SSE_MAX_S = 3 * 3600
CSP = ("default-src 'self'; img-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

ENV_HELP = """\
e2e console: credentials file {path} {problem}.
Create it yourself (pick a long password; never commit it):

  mkdir -p ~/.config/e2e && touch ~/.config/e2e/console.env && chmod 600 ~/.config/e2e/console.env
  open -e ~/.config/e2e/console.env      # add two lines:
      E2E_CONSOLE_USER=sergey
      E2E_CONSOLE_PASS=<long random password>
"""


class Config:
    def __init__(self, user, password):
        self.user = user.encode("utf-8")
        self.password = password.encode("utf-8")
        # Per-process secret: restarting the console invalidates old CSRF tokens.
        self.secret = secrets.token_bytes(32)

    def csrf(self):
        return hmac.new(self.secret, b"csrf:" + self.user, hashlib.sha256).hexdigest()


def env_path():
    return Path(os.environ.get("E2E_CONSOLE_ENV") or os.path.expanduser("~/.config/e2e/console.env"))


def load_config():
    """Read creds; refuse (SystemExit with instructions) unless the file is 0600 and ours."""
    path = env_path()
    try:
        st = path.stat()
    except OSError:
        raise SystemExit(ENV_HELP.format(path=path, problem="is missing"))
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise SystemExit(ENV_HELP.format(path=path, problem="must be a file you own with mode 600 (chmod 600 %s)" % path))
    vals = {}
    for ln in path.read_text("utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        vals[k] = v
    user, pw = vals.get("E2E_CONSOLE_USER", ""), vals.get("E2E_CONSOLE_PASS", "")
    if not user or not pw:
        raise SystemExit(ENV_HELP.format(path=path, problem="needs both E2E_CONSOLE_USER and E2E_CONSOLE_PASS"))
    return Config(user, pw)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    server_version = "e2e-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    config = None  # set by make_server

    # ---------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):
        # Default logging prints only the request line; credentials never reach the log.
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.security_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def security_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj), "application/json", {"Cache-Control": "no-store"})

    def page(self, code, html):
        self.send(code, html, headers={"Cache-Control": "no-store"})

    def not_found(self):
        if self.path.startswith("/api/"):
            return self.send_json(404, {"error": "not found"})
        self.page(404, pages.error_page(self.ctx(), 404, "Not found."))

    # ---------------------------------------------------------- auth

    def authorized(self):
        cfg = self.config
        hdr = self.headers.get("Authorization", "")
        ok = False
        if hdr[:6].lower() == "basic ":
            try:
                raw = base64.b64decode(hdr[6:].strip(), validate=True)
                user, _, pw = raw.partition(b":")
                # Evaluate both compares so timing doesn't reveal which half failed.
                u_ok = hmac.compare_digest(user, cfg.user)
                p_ok = hmac.compare_digest(pw, cfg.password)
                ok = u_ok and p_ok
            except (ValueError, base64.binascii.Error):
                ok = False
        if not ok:
            body = b"Authentication required.\n"
            self.send_response(401)
            self.security_headers()
            self.send_header("WWW-Authenticate", 'Basic realm="e2e console", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return ok

    def is_mobile(self):
        return bool(MOBILE_RE.search(self.headers.get("User-Agent", "")))

    def ctx(self):
        return {"viewer": self.is_mobile(), "csrf": self.config.csrf(), "user": self.config.user.decode()}

    # ---------------------------------------------------------- GET

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.authorized():
            return
        parts = urlsplit(self.path)
        path = parts.path
        q = parse_qs(parts.query)
        try:
            self.route_get(path, q)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.crashed()

    def route_get(self, path, q):
        ctx = self.ctx()
        if path in ("/", "/projects"):
            return self.page(200, pages.projects_page(ctx))
        if path.startswith("/static/"):
            return self.static(path[len("/static/"):])
        if path == "/runs":
            return self.page(200, pages.runs_page(ctx))
        if path == "/triage":
            return self.page(200, pages.triage_page(ctx))
        if path == "/machine":
            return self.page(200, pages.machine_page(ctx))
        if path == "/api/plan":
            return self.api_plan(q)
        m = re.match(r"^/projects/([^/]+)$", path)
        if m:
            name = unquote(m.group(1))
            if not data.NAME_RE.match(name) or name not in set(data.project_names()) | set(cli.projects_or_empty()[0]):
                return self.not_found()
            return self.page(200, pages.project_page(ctx, name))
        m = re.match(r"^/(?:api/)?runs/([^/]+)(/.*)?$", path)
        if not m:
            return self.not_found()
        rid, rest = unquote(m.group(1)), m.group(2) or ""
        if rest.startswith("/files/"):
            return self.artifact(rid, unquote(rest[len("/files/"):]))
        run = data.load_run(rid)
        if not run:
            return self.not_found()
        api = path.startswith("/api/")
        if api and rest == "/events":
            return self.sse(run)
        if api and rest == "":
            return self.send_json(200, {k: v for k, v in run.items() if k != "dir"})
        if not api and rest == "":
            return self.page(200, pages.live_page(ctx, run))
        if not api and rest == "/fragment":
            return self.page(200, pages.live_fragment(ctx, run))
        if not api and rest == "/report":
            if not run["has_report"]:
                return self.page(200, pages.live_page(ctx, run))
            return self.page(200, pages.report_page(ctx, run))
        if not api and rest == "/junit":
            return self.page(200, pages.junit_page(ctx, run))
        return self.not_found()

    def static(self, name):
        types = {"console.css": "text/css; charset=utf-8", "console.js": "text/javascript; charset=utf-8"}
        if name not in types:
            return self.not_found()
        self.send(200, (STATIC / name).read_bytes(), types[name], {"Cache-Control": "no-cache"})

    def artifact(self, rid, rel):
        hit = data.artifact(rid, rel)
        if not hit:
            return self.not_found()
        path, ctype = hit
        size = path.stat().st_size
        live = rel.startswith("live/")
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache" if live else "private, max-age=300"}
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
        if m and size and (m.group(1) or m.group(2)):
            # Safari will not play <video> without byte ranges.
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.security_headers()
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        if ctype.startswith(("application/xml", "application/json", "text/plain")):
            # Artifacts are shown, never run: a sandbox CSP stops any HTML/SVG tricks.
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        self.send(code, body, ctype, headers)

    def api_plan(self, q):
        project = (q.get("project") or [""])[0]
        suites = [s for s in (q.get("suite") or [""])[0].split(",") if s]
        err = self.validate_choice(project, suites, "scripted")
        if err:
            return self.send_json(400, {"error": err})
        try:
            return self.send_json(200, cli.plan(project, suites))
        except cli.CLIError as e:
            return self.send_json(502, {"error": str(e)})

    def sse(self, run):
        """Tail events.ndjson and watch state.json; the event id is the byte offset."""
        d = run["dir"]
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        last_id = self.headers.get("Last-Event-ID", "")
        offset = int(last_id) if last_id.isdigit() else None
        evpath, stpath, rpath = d / "events.ndjson", d / "state.json", d / "report.json"
        if offset is None:
            # First connect: replay only the tail so a long run doesn't flood the page.
            try:
                size = evpath.stat().st_size
                offset = max(0, size - 64 * 1024)
                if offset:
                    with open(evpath, "rb") as f:
                        f.seek(offset)
                        offset += len(f.readline())
            except OSError:
                offset = 0
        last_state = None
        t0 = beat = time.time()
        w = self.wfile
        while time.time() - t0 < SSE_MAX_S:
            try:
                with open(evpath, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(256 * 1024)
                # Only whole lines: the engine may be mid-append.
                cut = chunk.rfind(b"\n") + 1
                for ln in chunk[:cut].splitlines():
                    offset += len(ln) + 1
                    if ln.strip():
                        w.write(b"id: %d\nevent: ev\ndata: %s\n\n" % (offset, ln.strip()))
                if cut != len(chunk) and cut == 0 and len(chunk) == 256 * 1024:
                    offset += len(chunk)  # absurdly long line: skip it rather than stall
            except OSError:
                pass
            try:
                st = stpath.stat()
                sig = (st.st_mtime_ns, st.st_size, rpath.exists())
            except OSError:
                sig = None
            if sig != last_state:
                last_state = sig
                s = data.read_json(stpath, {}) or {}
                w.write(b"event: state\ndata: %s\n\n" % json.dumps(
                    {"status": s.get("status"), "updated": s.get("updated"), "report": bool(sig and sig[2])}).encode())
                if sig and sig[2]:
                    w.write(b"event: end\ndata: {}\n\n")
                    w.flush()
                    return
            if time.time() - beat > 15:
                beat = time.time()
                w.write(b": keepalive\n\n")
            w.flush()
            time.sleep(0.5)

    # ---------------------------------------------------------- POST

    def do_POST(self):
        if not self.authorized():
            return
        try:
            self.route_post(urlsplit(self.path).path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.crashed()

    def crashed(self):
        # Traceback to the log only; the client gets no paths or internals.
        traceback.print_exc()
        try:
            self.send(500, "Internal error.\n", "text/plain; charset=utf-8")
        except OSError:
            pass

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > MAX_BODY:
            return None
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def operator_check(self, body, need_confirm):
        """None if this request may act as operator, else (code, message)."""
        if self.is_mobile():
            return 403, "view only on phones"
        token = self.headers.get("X-CSRF-Token", "") or str((body or {}).get("csrf", ""))
        if not hmac.compare_digest(token.encode(), self.config.csrf().encode()):
            return 403, "missing or bad CSRF token"
        if body is None:
            return 400, "bad JSON body"
        if need_confirm and body.get("confirm") is not True:
            return 400, "confirm required"
        return None

    def validate_choice(self, project, suites, mode):
        if not isinstance(project, str) or not data.NAME_RE.match(project):
            return "unknown project"
        try:
            projs = cli.projects()
        except cli.CLIError as e:
            return "cannot list projects: %s" % e
        p = projs.get(project)
        if not p:
            return "unknown project"
        if (not isinstance(suites, list) or not suites or len(suites) > 64
                or not all(isinstance(s, str) for s in suites) or len(set(suites)) != len(suites)):
            return "pick at least one suite"
        bad = [s for s in suites if s not in p["suites"]]
        if bad:
            return "unknown suite: %s" % ", ".join(sorted(bad))[:200]
        if mode not in cli.ALLOWED_MODES:
            return "unknown mode"
        return None

    def route_post(self, path):
        body = self.read_body()
        if path == "/api/runs":
            denied = self.operator_check(body, need_confirm=True)
            if denied:
                return self.send_json(denied[0], {"error": denied[1]})
            project, suites, mode = body.get("project"), body.get("suites"), body.get("mode", "scripted")
            err = self.validate_choice(project, suites, mode)
            if err:
                return self.send_json(400, {"error": err})
            try:
                rid = cli.start(project, suites, mode)
            except cli.CLIError as e:
                return self.send_json(409, {"error": str(e)})
            if not data.split_run_id(rid):
                return self.send_json(502, {"error": "e2e run returned no run-id"})
            return self.send_json(201, {"run": rid, "url": "/runs/%s" % rid})
        m = re.match(r"^/api/runs/([^/]+)/stop$", path)
        if m:
            denied = self.operator_check(body, need_confirm=True)
            if denied:
                return self.send_json(denied[0], {"error": denied[1]})
            run = data.load_run(unquote(m.group(1)))
            if not run:
                return self.send_json(404, {"error": "no such run"})
            if run["request"].get("requested_by") != "console":
                return self.send_json(403, {"error": "only runs started from the console can be stopped here"})
            if run["status"] in data.TERMINAL or run["has_report"]:
                return self.send_json(409, {"error": "run already finished"})
            try:
                cli.stop(run["run"])
            except cli.CLIError as e:
                return self.send_json(409, {"error": str(e)})
            return self.send_json(202, {"run": run["run"], "stopping": True})
        if path == "/api/triage":
            denied = self.operator_check(body, need_confirm=False)
            if denied:
                return self.send_json(denied[0], {"error": denied[1]})
            return self.triage(body)
        return self.send_json(404, {"error": "not found"})

    def triage(self, body):
        rid, flow, mark = body.get("run"), body.get("flow"), body.get("mark")
        note = body.get("note", "")
        if mark not in MARKS:
            return self.send_json(400, {"error": "mark must be one of %s" % ", ".join(MARKS)})
        if not isinstance(note, str) or len(note) > 500:
            return self.send_json(400, {"error": "note too long"})
        run = data.load_run(rid) if isinstance(rid, str) else None
        if not run or not run["has_report"]:
            return self.send_json(404, {"error": "no finished run %s" % rid})
        known = {f.get("flow") for f in run.get("failures") or []} | {f.get("id") for f in run.get("flows") or []}
        if not isinstance(flow, str) or flow not in known:
            return self.send_json(400, {"error": "unknown flow"})
        entry = {"ts": now_iso(), "run": run["run"], "flow": flow, "mark": mark,
                 "by": "console:%s" % self.config.user.decode(), "note": note}
        data.append_triage(run["project"], entry)
        return self.send_json(201, entry)


def make_server(host, port, config):
    handler = type("BoundHandler", (Handler,), {"config": config})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main():
    config = load_config()
    host = os.environ.get("E2E_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("E2E_CONSOLE_PORT", "4020"))
    srv = make_server(host, port, config)
    sys.stderr.write("e2e console on http://%s:%d (E2E_HOME=%s, bin=%s)\n" % (host, port, data.home(), cli.e2e_bin()))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
