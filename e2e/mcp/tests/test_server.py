"""The MCP server as Claude Code sees it: a subprocess spoken to over stdio.

Run from e2e/:
    ~/.local/share/e2e/venv/bin/python -m unittest discover -s mcp/tests -t .

Every run here is a --dry-run against a throwaway E2E_HOME.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

from tests.helpers import E2E_DIR, HomeCase

LAUNCHER = os.path.join(os.path.dirname(E2E_DIR), "bin", "e2e-mcp")
READING = {"projects", "status", "report", "plan", "history_verify", "brief", "bug_list", "bug_show", "run_events"}
WRITING = {"bug_add", "bug_claim", "bug_move", "bug_note", "bug_edit", "bug_ack", "bug_measure", "bug_review",
           "bug_verify", "triage_mark"}
OTHER = {"run_flow", "run_suite", "run_stop", "bug_decide"}
CARD = {"project": "demo", "title": "Map freezes when a day opens", "do": "Open a day",
        "see": "Map freezes about 1 s", "want": "No freeze"}


class Client:
    """A minimal MCP client; keeps every line the server ever wrote to stdout."""

    def __init__(self, env, stderr_path):
        self.err = open(stderr_path, "w")
        self.p = subprocess.Popen([LAUNCHER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.err, env=env, text=True)
        self.lines, self.q, self.n = [], queue.Queue(), 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.lines.append(line)
            self.q.put(line)
        self.q.put(None)

    def send(self, text):
        self.p.stdin.write(text + "\n")
        self.p.stdin.flush()

    def read(self):
        line = self.q.get(timeout=60)
        if line is None:
            raise AssertionError("server closed stdout")
        return json.loads(line)

    def notify(self, method):
        self.send(json.dumps({"jsonrpc": "2.0", "method": method}))

    def rpc(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(json.dumps(msg))
        reply = self.read()
        assert reply.get("id") == self.n, reply
        return reply

    def call(self, name, args=None):
        """(isError, the result: parsed JSON when ok, the message when not)."""
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}})["result"]
        (part,) = result["content"]
        assert part["type"] == "text", part
        return result["isError"], part["text"] if result["isError"] else json.loads(part["text"])

    def close(self):
        self.p.stdin.close()
        code = self.p.wait(timeout=30)
        self.p.stdout.close()
        self.err.close()
        return code


class ServerCase(HomeCase):
    cli_stub = None  # source of a stand-in for bin/e2e, for commands the engine lacks

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        env = dict(os.environ)
        if sys.prefix != sys.base_prefix:
            env["E2E_VENV"] = sys.prefix  # the launcher and bin/e2e use this interpreter
        if self.cli_stub:
            stub = os.path.join(self.tmp, "e2e-stub")
            with open(stub, "w") as f:
                f.write("#!%s\n%s" % (sys.executable, self.cli_stub))
            os.chmod(stub, 0o755)
            env["E2E_MCP_CLI"] = stub
        else:
            self.assertEqual(self.cli("add", self.repo).returncode, 0)
        self.c = Client(env, os.path.join(self.tmp, "server.err"))
        self.addCleanup(self.shutdown)
        init = self.c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "test", "version": "0"}})
        self.init = init["result"]
        self.c.notify("notifications/initialized")

    def shutdown(self):
        """Every test ends here: the server exits on EOF, and stdout held
        nothing but JSON-RPC replies, one per line."""
        self.assertEqual(self.c.close(), 0)
        for line in self.c.lines:
            msg = json.loads(line)
            self.assertEqual(msg["jsonrpc"], "2.0", line)
            self.assertTrue(("result" in msg) != ("error" in msg), line)
        self.assertEqual(len(self.c.lines), self.c.n + getattr(self, "extra_replies", 0))

    def ok(self, name, args=None):
        failed, out = self.c.call(name, args)
        self.assertFalse(failed, out)
        return out

    def refused(self, text, name, args=None):
        failed, out = self.c.call(name, args)
        self.assertTrue(failed, out)
        self.assertIn(text, out)

    def follow(self, run):
        """Poll run_events the way an agent would; returns (last result, all events)."""
        events, after, deadline = [], 0, time.time() + 30
        while time.time() < deadline:
            out = self.ok("run_events", {"run": run, "after": after})
            events += out["events"]
            after = out["next"]
            if out["finished"] and not out["more"]:
                return out, events
            time.sleep(0.1)
        raise AssertionError("run %s did not finish" % run)


class Protocol(ServerCase):
    def test_handshake_and_tool_list(self):
        self.assertEqual(self.init["protocolVersion"], "2025-06-18")
        self.assertEqual(self.init["capabilities"], {"tools": {}})
        self.assertEqual(self.init["serverInfo"]["name"], "e2e")
        self.assertEqual(self.c.rpc("ping")["result"], {})  # the notification got no reply of its own
        tools = {t["name"]: t for t in self.c.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), READING | WRITING | OTHER)
        for name, t in tools.items():
            self.assertGreater(len(t["description"]), 40, name)
            self.assertEqual(t["inputSchema"]["type"], "object", name)
            self.assertIs(t["inputSchema"]["additionalProperties"], False, name)
            self.assertEqual(t["annotations"]["readOnlyHint"], name in READING, name)
        for name in WRITING - {"bug_verify"}:  # the engine signs a verify
            self.assertIn("by", tools[name]["inputSchema"]["required"], name)
        self.assertNotIn("priority", tools["bug_edit"]["inputSchema"]["properties"])
        self.assertNotIn("by", tools["bug_decide"]["inputSchema"]["properties"])
        self.assertIn("only when the owner has just said so", tools["bug_decide"]["description"])
        self.assertIn("Only after the owner said yes", tools["run_suite"]["description"])

    def test_unknown_tool_method_and_garbage(self):
        reply = self.c.rpc("tools/call", {"name": "bug_delete", "arguments": {}})
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(self.c.rpc("resources/list")["error"]["code"], -32601)
        self.c.send("this is not json")
        self.assertEqual(self.c.read(), {"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "Parse error"}})
        self.extra_replies = 1
        self.assertEqual(self.c.rpc("ping")["result"], {})  # still serving

    def test_unsupported_protocol_version_gets_ours(self):
        result = self.c.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")


class Reading(ServerCase):
    def test_projects_plan_status(self):
        (project,) = self.ok("projects")
        self.assertEqual(project["name"], "demo")
        self.assertEqual({s["name"] for s in project["suites"]}, {"smoke", "trips", "ui"})
        plan = self.ok("plan", {"project": "demo", "suites": ["smoke", "trips"]})
        self.assertEqual((plan["suites"], plan["flows"]), (["smoke", "trips"], 4))
        status = self.ok("status")
        self.assertEqual(status["runs"], [])
        self.assertIn("ram_free_gb", status["machine"])
        self.assertEqual(self.ok("history_verify", {"project": "demo"}), [])
        self.assertEqual(self.ok("bug_list", {"project": "demo"})["bugs"], [])
        self.refused("unknown project", "plan", {"project": "nope", "suites": ["smoke"]})
        self.refused("no run demo-9", "status", {"run": "demo-9"})

    def test_run_events_pages_stay_small(self):
        # A long, chatty run, written by hand: 500 events of ~350 bytes.
        d = self.run_path("demo-7")
        os.makedirs(d)
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump({"run": "demo-7", "project": "demo", "status": "running", "phase": "testing",
                       "counts": {"total": 1}, "elapsed_s": 5, "flows": []}, f)
        with open(os.path.join(d, "events.ndjson"), "w") as f:
            for i in range(500):
                f.write(json.dumps({"ts": "t", "type": "step", "run": "demo-7", "sim": 1, "n": i,
                                    "text": "x" * 300}) + "\n")
            f.write(json.dumps({"type": "log", "text": "y" * 50000}) + "\n")
            f.write('{"type":"step","half written')
        seen, after, calls = [], 0, 0
        while True:
            reply = self.c.rpc("tools/call", {"name": "run_events", "arguments": {"run": "demo-7", "after": after}})
            text = reply["result"]["content"][0]["text"]
            self.assertLess(len(text.encode()), 20000)
            out = json.loads(text)
            self.assertLessEqual(len(out["events"]), 200)
            self.assertEqual((out["status"], out["finished"], out["total"]), ("running", False, 501))
            self.assertEqual(out["next"], after + len(out["events"]))
            seen += out["events"]
            after, calls = out["next"], calls + 1
            if not out["more"]:
                break
        self.assertGreater(calls, 3)
        self.assertEqual([e["n"] for e in seen[:500]], list(range(500)))
        self.assertLess(len(seen[500]["text"]), 400)  # the 50 kB line came back cut
        self.assertEqual(self.ok("run_events", {"run": "demo-7", "after": 9999})["events"], [])


class Board(ServerCase):
    def test_card_path(self):
        card = self.ok("bug_add", dict(CARD, by="lead", suggest="P1", dry_run=True, flow="trips/01-create"))
        self.assertEqual((card["id"], card["state"], card["suggest"], card["by"]), ("B-1", "found", "P1", "lead"))
        card = self.ok("bug_decide", {"project": "demo", "id": "B-1", "decision": "approve", "priority": "P2"})
        self.assertEqual((card["state"], card["priority"]), ("open", "P2"))
        card = self.ok("bug_move", {"project": "demo", "id": "B-1", "to": "poc", "by": "reproducer"})
        self.assertEqual((card["state"], card["agent"]), ("poc", "reproducer"))
        # A value that looks like an option stays a value.
        self.ok("bug_note", {"project": "demo", "id": "B-1", "text": "--by=owner", "by": "reproducer"})
        self.ok("bug_note", {"project": "demo", "id": "B-1", "text": "writing the failing test", "by": "reproducer"})
        self.ok("bug_edit", {"project": "demo", "id": "B-1", "done_when": "trips/01 passes 3 runs in a row",
                             "by": "reproducer"})
        card = self.ok("bug_show", {"project": "demo", "id": "B-1"})
        self.assertEqual((card["state"], card["note"]["text"], card["note"]["by"]),
                         ("poc", "writing the failing test", "reproducer"))
        self.assertEqual(card["done_when"], "trips/01 passes 3 runs in a row")
        self.assertEqual([h["type"] for h in card["history"]], ["add", "approve", "move", "note", "note", "edit"])
        self.assertEqual((card["history"][1]["by"], card["history"][1]["via"]), ("owner", "chat"))
        self.assertEqual((card["history"][3]["by"], card["history"][3]["text"]), ("reproducer", "--by=owner"))
        board = self.ok("bug_list", {"project": "demo", "states": ["poc", "testing"]})
        self.assertEqual([b["id"] for b in board["bugs"]], ["B-1"])
        self.assertEqual(self.ok("bug_list", {"project": "demo", "states": ["found"]})["bugs"], [])
        # reject and reopen are the owner's too
        self.ok("bug_add", dict(CARD, by="lead"))
        card = self.ok("bug_decide", {"project": "demo", "id": "B-2", "decision": "reject", "reason": "same as B-1"})
        self.assertEqual((card["state"], card["reason"]), ("rejected", "same as B-1"))
        self.assertEqual(self.ok("bug_decide", {"project": "demo", "id": "B-2", "decision": "reopen"})["state"], "open")

    def test_refused_move_is_a_tool_error(self):
        self.ok("bug_add", dict(CARD, by="lead"))
        self.refused("owner approves it first", "bug_move",
                     {"project": "demo", "id": "B-1", "to": "poc", "by": "reproducer"})
        self.refused("no bug B-9", "bug_claim", {"project": "demo", "id": "B-9", "by": "lead"})
        self.assertEqual(self.ok("bug_show", {"project": "demo", "id": "B-1"})["state"], "found")

    def test_bad_input_is_refused_before_the_cli(self):
        self.ok("bug_add", dict(CARD, by="lead"))
        card = {"project": "demo", "id": "B-1"}
        for text, name, args in (
            ("id", "bug_show", {"project": "demo", "id": "12"}),
            ("id", "bug_show", {"project": "demo", "id": "B-1\n"}),
            ("project", "bug_list", {"project": "../demo"}),
            ("missing id", "bug_show", {"project": "demo"}),
            ("must be one of", "bug_move", dict(card, to="done", by="lead")),
            ("unknown argument priority", "bug_edit", dict(card, priority="P1", by="lead")),
            ("may not be 'owner'", "bug_note", dict(card, text="approved", by="owner")),
            ("unknown argument by", "bug_decide", dict(card, decision="approve", priority="P1", by="lead")),
            ("needs priority", "bug_decide", dict(card, decision="approve")),
            ("does not take priority", "bug_decide", dict(card, decision="reopen", priority="P1")),
            ("must be one of", "bug_decide", dict(card, decision="done")),
            ("title must be 1 to 200", "bug_add", dict(CARD, title="x" * 201, by="lead")),
            ("must be a number", "bug_measure", dict(card, value="fast", by="lead")),
            ("must be a boolean", "bug_review", dict(card, ok="yes", by="reviewer")),
            ("flows[0]", "run_flow", {"project": "demo", "flows": ["smoke"], "dry_run": True}),
            ("1 to 3 items", "run_flow", {"project": "demo", "flows": ["smoke/01-launch"] * 4, "dry_run": True}),
            ("missing suites", "run_suite", {"project": "demo", "dry_run": True}),
            ("run", "run_stop", {"run": "demo"}),
            ("must be an object", "projects", ["x"]),
        ):
            self.refused(text, name, args)
        shown = self.ok("bug_show", card)
        self.assertEqual((shown["state"], len(shown["history"])), ("found", 1))
        self.assertFalse(os.path.exists(os.path.join(self.home, "demo", "runs")))


class Runs(ServerCase):
    def test_run_flow_then_events_report_triage(self):
        out = self.ok("run_flow", {"project": "demo", "flows": ["trips/02-fail-edit"], "dry_run": True})
        self.assertEqual((out["run"], out["detached"]), ("demo-1", True))
        last, events = self.follow("demo-1")
        self.assertEqual(last["status"], "failed")
        self.assertEqual(last["counts"]["total"], 1)
        types = [e["type"] for e in events]
        self.assertEqual((types[0], types[-1]), ("queued", "done"))
        self.assertEqual(events, self.events("demo-1"))
        self.wait_terminal("demo-1")
        with open(self.run_path("demo-1", "request.json")) as f:
            req = json.load(f)
        self.assertEqual((req["flows"], req["requested_by"], req["dry_run"]),
                         (["trips/02-fail-edit"], "claude", True))
        report = self.ok("report", {"run": "demo-1"})
        self.assertEqual((report["final"], report["failures"][0]["flow"]), (True, "trips/02-fail-edit"))
        self.assertEqual(self.ok("status", {"run": "demo-1"})["status"], "failed")
        self.assertEqual(self.ok("run_stop", {"run": "demo-1"})[0]["signalled"], False)
        mark = self.ok("triage_mark", {"run": "demo-1", "flow": "trips/02-fail-edit", "mark": "fix",
                                       "note": "real bug", "by": "lead"})
        self.assertEqual((mark["mark"], mark["by"]), ("fix", "lead"))
        self.refused("has no flow", "triage_mark", {"run": "demo-1", "flow": "trips/01-create", "mark": "flaky",
                                                    "by": "lead"})
        self.assertEqual(self.ok("history_verify", {"project": "demo"})[0]["status"], "ok")

    def test_run_suite_dry(self):
        out = self.ok("run_suite", {"project": "demo", "suites": ["smoke"], "from": self.repo, "dry_run": True})
        last, _ = self.follow(out["run"])
        self.assertEqual(last["status"], "passed")
        self.wait_terminal(out["run"])
        with open(self.run_path(out["run"], "request.json")) as f:
            req = json.load(f)
        self.assertEqual((req["flows"], req["suites"], req["requested_by"]), (None, ["smoke"], "claude"))
        self.refused("is not a checkout of demo", "run_flow", {"project": "demo", "flows": ["smoke/01-launch"],
                                                               "from": self.tmp, "dry_run": True})


class Argv(ServerCase):
    """What the server asks of `bin/e2e`, seen by a stand-in that echoes its
    argv. Covers `brief`, which the engine may not have yet, and the real
    (not dry) runs that a test must never start."""

    cli_stub = "import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n"

    def argv(self, name, args=None):
        return self.ok(name, args)["argv"]

    def test_argv(self):
        card = {"project": "demo", "id": "B-3"}
        self.assertEqual(self.argv("brief"), ["brief", "--json"])
        self.assertEqual(self.argv("brief", {"project": "demo"}), ["brief", "demo", "--json"])
        self.assertEqual(self.argv("bug_list", {"project": "demo", "states": ["found", "open"]}),
                         ["bug", "list", "demo", "--state=found,open", "--json"])
        self.assertEqual(self.argv("run_flow", {"project": "demo", "flows": ["a/1", "b/2"], "from": "/w t/x"}),
                         ["run", "demo", "--flow=a/1,b/2", "--from=/w t/x", "--detach", "--by=claude", "--json"])
        self.assertEqual(self.argv("run_suite", {"project": "demo", "suites": ["smoke", "trips"], "dry_run": False}),
                         ["run", "demo", "--suite=smoke,trips", "--detach", "--by=claude", "--json"])
        self.assertEqual(self.argv("run_stop", {"run": "demo-4"}), ["stop", "demo-4", "--json"])
        self.assertEqual(self.argv("bug_decide", dict(card, decision="reject", reason="; rm -rf $HOME `x`")),
                         ["bug", "reject", "demo", "B-3", "--reason=; rm -rf $HOME `x`", "--by=owner",
                          "--via=chat", "--json"])
        self.assertEqual(self.argv("bug_review", dict(card, ok=False, text="dark screen is cut", by="reviewer")),
                         ["bug", "review", "demo", "B-3", "--text=dark screen is cut", "--by=reviewer", "--no",
                          "--json"])
        self.assertEqual(self.argv("bug_review", dict(card, ok=True, by="reviewer")),
                         ["bug", "review", "demo", "B-3", "--by=reviewer", "--json"])
        self.assertEqual(self.argv("bug_verify", card), ["bug", "verify", "demo", "B-3", "--json"])
        self.assertEqual(self.argv("bug_edit", dict(card, done_when="3 green", limit=300, limit_unit="ms", by="x")),
                         ["bug", "edit", "demo", "B-3", "--done-when=3 green", "--limit=300", "--limit-unit=ms",
                          "--by=x", "--json"])
        self.assertEqual(self.argv("bug_measure", dict(card, value=280.5, run="demo-4", by="x")),
                         ["bug", "measure", "demo", "B-3", "--value=280.5", "--run=demo-4", "--by=x", "--json"])
        self.assertEqual(self.argv("triage_mark", {"run": "demo-4", "flow": "a/1", "mark": "not-a-bug", "by": "x"}),
                         ["triage", "demo-4", "a/1", "--mark=not-a-bug", "--by=x", "--json"])


class BrokenCli(ServerCase):
    cli_stub = "import sys\nprint('usage: e2e …', file=sys.stderr)\nsys.exit(2)\n"

    def test_cli_without_json_is_a_tool_error(self):
        self.refused("usage: e2e", "brief")
