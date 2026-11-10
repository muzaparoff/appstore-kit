#!/usr/bin/env python3
"""MCP tool server for the e2e engine: `bin/e2e` as named tools, over stdio.

One tool per kind of action, so each can carry its own permission: the
reading tools can be always-allowed while runs and the owner's decisions stay
on "ask". Every tool builds an argv list and runs `bin/e2e … --json`; nothing
goes through a shell, and nothing but JSON-RPC is written to stdout.

Standard library only (Python 3.9), so the protocol is written out by hand:
newline-delimited JSON-RPC 2.0 with initialize, ping, tools/list, tools/call.
"""

import json
import math
import os
import re
import subprocess
import sys

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
# E2E_MCP_CLI swaps the engine for a stub in tests; nothing else reads it.
CLI = os.environ.get("E2E_MCP_CLI") or os.path.join(KIT, "bin", "e2e")
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TERMINAL = ("passed", "failed", "stopped", "error")
EVENTS_MAX, EVENTS_BYTES = 200, 18000  # one run_events result stays under ~20 kB


class ToolError(Exception):
    """The tool ran and failed; shown to the model as an isError result."""


def log(text):
    print("e2e-mcp: %s" % text, file=sys.stderr, flush=True)


# --- the engine --------------------------------------------------------------

def e2e(argv):
    """Run `bin/e2e <argv> --json` and return its JSON."""
    try:
        r = subprocess.run([CLI] + argv + ["--json"], stdin=subprocess.DEVNULL, capture_output=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise ToolError("e2e %s gave no answer in 120 s" % argv[0])
    except (OSError, ValueError) as e:
        raise ToolError("cannot run %s: %s" % (CLI, e))
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise ToolError((r.stderr or r.stdout).strip()[-2000:] or "e2e exited %d with no output" % r.returncode)
    # Exit 1 alone is not an error: `bug verify` and `verify` answer "no" that
    # way and still print the card or the list.
    if r.returncode != 0 and isinstance(data, dict) and list(data) == ["error"]:
        raise ToolError(str(data["error"]))
    return data


FLAGS = {"suites": "--suite", "flows": "--flow", "states": "--state"}


def argv_of(cmd, pos, args):
    """cmd + positionals + every other argument as `--name=value`.

    The `=` form keeps a value that starts with a dash a value. Lists join
    with commas; `true` is a bare flag, `false` is left out.
    """
    out = list(cmd) + [args[p] for p in pos if p in args]
    for name, value in args.items():
        flag = FLAGS.get(name, "--" + name.replace("_", "-"))
        if name in pos or value is False:
            continue
        if value is True:
            out.append(flag)
        else:
            out.append("%s=%s" % (flag, ",".join(value) if isinstance(value, list) else value))
    return out


def start_run(args):
    return e2e(argv_of(["run"], ("project",), args) + ["--detach", "--by=claude"])


def bug_review(args):
    rest = dict(args)
    ok = rest.pop("ok")
    return e2e(argv_of(["bug", "review"], ("project", "id"), rest) + ([] if ok else ["--no"]))


def bug_decide(args):
    decision = args["decision"]
    need = {"approve": "priority", "reject": "reason"}.get(decision)
    extra = sorted(k for k in ("priority", "reason") if k in args and k != need)
    if need and need not in args:
        raise ToolError("decision %s needs %s" % (decision, need))
    if extra:
        raise ToolError("decision %s does not take %s" % (decision, ", ".join(extra)))
    rest = {k: v for k, v in args.items() if k != "decision"}
    return e2e(argv_of(["bug", decision], ("project", "id"), rest) + ["--by=owner", "--via=chat"])


def _event(line):
    try:
        ev = json.loads(line)
    except ValueError:
        return {"type": "unreadable", "text": line[:300]}
    if len(line) > 2000 and isinstance(ev, dict):
        ev = {k: v[:300] + "…" if isinstance(v, str) and len(v) > 300 else v for k, v in ev.items()}
        if len(json.dumps(ev)) > 4000:
            ev = {"ts": ev.get("ts"), "type": ev.get("type"), "trimmed": True}
    return ev


def run_events(args):
    """No command gives a slice of events.ndjson (`e2e watch` follows a run to
    its end), so the file is read here, read-only."""
    run, after = args["run"], args.get("after", 0)
    state = e2e(["status", run])  # also refuses a run that does not exist
    home = os.path.abspath(os.path.expanduser(os.environ.get("E2E_HOME") or "~/e2e-runs"))
    path = os.path.join(home, run.rpartition("-")[0], "runs", run, "events.ndjson")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            # Text after the last newline is a line still being written.
            lines = [l for l in f.read().split("\n")[:-1] if l.strip()]
    except OSError:
        lines = []
    events, size = [], 0
    for line in lines[after:after + EVENTS_MAX]:
        ev = _event(line)
        size += len(json.dumps(ev)) + 1
        if events and size > EVENTS_BYTES:
            break
        events.append(ev)
    nxt = min(after, len(lines)) + len(events)
    return {"run": run, "status": state.get("status"), "phase": state.get("phase"),
            "finished": state.get("status") in TERMINAL, "counts": state.get("counts"),
            "elapsed_s": state.get("elapsed_s"), "after": after, "next": nxt, "total": len(lines),
            "more": nxt < len(lines), "events": events}


# --- tools -------------------------------------------------------------------

def _s(description, **kw):
    return dict({"type": "string", "description": description}, **kw)


def _text(description, cap=2000):
    return _s(description, minLength=1, maxLength=cap)


def _list(items, description):
    return {"type": "array", "items": items, "minItems": 1, "maxItems": 20, "description": description}


NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"
PROJECT = _s("Project name, as listed by `projects`.", pattern=NAME)
RUN = _s("Run id like camipack-14.", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}-[0-9]{1,9}$")
BUG = _s("Card id like B-12.", pattern=r"^B-[1-9][0-9]{0,8}$")
FLOW = _s("Flow id as suite/flow, e.g. trips/01-create-trip.", pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
          maxLength=200)
SUITE = _s("Suite name from the project's app.yml.", pattern=NAME)
PRIORITY = _s("P1 is the most urgent.", enum=["P1", "P2", "P3"])
BY = dict(_s("Your short agent name: lead, reproducer, fixer-1, reviewer…", pattern=r"^[a-z][a-z0-9-]{0,31}$"),
          **{"not": {"enum": ["owner", "engine", "console"]}})
FROM = _text("Folder of another checkout (worktree) of the same repository to build from.", 1024)
DRY = {"type": "boolean", "description": "Use the fake backend: no Xcode, no simulators."}
CARD = {"project": PROJECT, "id": BUG}
CARD_TEXT = {"title": _text("One line.", 200), "do": _text("What you do."), "see": _text("What you see."),
             "want": _text("What you want to see instead."), "proof": _text("Where the evidence is."),
             "flow": dict(FLOW, description="The test that catches it, as suite/flow.")}

TOOLS = {}


def tool(name, description, props=None, required=(), cmd=None, pos=(), call=None, read=False):
    schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    TOOLS[name] = {"name": name, "description": description, "inputSchema": schema, "read": read,
                   "call": call or (lambda args: e2e(argv_of(cmd, pos, args)))}


# Reading: change nothing, safe to always allow.
tool("projects", "List the registered projects with their suites, limits and last run. Start here to learn "
     "project and suite names.", cmd=["projects"], read=True)
tool("status", "Without a run id: free RAM and disk, busy simulators and every unfinished run. With a run id: "
     "that run's current state (phase, counts, each flow's status).",
     {"run": RUN}, cmd=["status"], pos=("run",), read=True)
tool("report", "The result of a run: counts, each flow's status, and for every failure its message, log "
     "excerpt and artifact paths. `final` is false while the run is still going.",
     {"run": RUN}, ("run",), cmd=["report"], pos=("run",), read=True)
tool("plan", "Estimate a run of whole suites without running anything: seconds, flow count, simulators, and "
     "whether a full build is needed. Use it before asking the owner for a suite run.",
     {"project": PROJECT, "suites": _list(SUITE, "Suites to estimate.")}, ("project", "suites"),
     cmd=["plan"], pos=("project",), read=True)
tool("history_verify", "Recheck the stored hash of every finished report of a project; a report edited after "
     "the run shows as `tampered`.", {"project": PROJECT}, ("project",), cmd=["verify"], pos=("project",), read=True)
tool("brief", "One-screen summary: active runs, last result per suite, bug cards waiting for the owner, and "
     "whether the design freeze is on. Use it to catch up at the start of a session.",
     {"project": PROJECT}, cmd=["brief"], pos=("project",), read=True)
tool("bug_list", "The bug board of a project: counts per state, the design freeze, and every card (optionally "
     "only the given states).",
     {"project": PROJECT, "states": _list(_s("Card state.", enum=[
         "found", "open", "poc", "inprogress", "testing", "done", "rejected"]), "Only cards in these states.")},
     ("project",), cmd=["bug", "list"], pos=("project",), read=True)
tool("bug_show", "One bug card in full, with its whole history. Read it before working on a card and whenever "
     "its `rev` is ahead of `acked`.", CARD, ("project", "id"), cmd=["bug", "show"], pos=("project", "id"), read=True)
tool("run_events", "New events of a run since the `after` you have already seen, plus the run's status and "
     "counts. Poll it to follow a run: pass the returned `next` as `after` until `finished` is true and `more` "
     "is false.",
     {"run": RUN, "after": {"type": "integer", "minimum": 0, "description": "Events already seen (default 0)."}},
     ("run",), call=run_events, read=True)

# Agent writes: one line each in the project's bug or triage file, signed `by`
# (the engine signs a verify line itself).
tool("bug_add", "File a new bug card in state `found`; the owner then approves or rejects it. Returns the "
     "card with its new id.",
     dict({"project": PROJECT}, **CARD_TEXT, kind=_s("`lag` for a slowness with a number to beat.", enum=["bug", "lag"]),
          run=dict(RUN, description="The run it was found in."),
          suggest=dict(PRIORITY, description="Your guess at the priority; the owner decides."),
          dry_run={"type": "boolean", "description": "A practice card: counts only dry runs, no design freeze."},
          by=BY),
     ("project", "title", "do", "see", "want", "by"), cmd=["bug", "add"], pos=("project",))
tool("bug_claim", "Take a card as its agent, which also marks its current revision as read. Returns the card.",
     dict(CARD, by=BY), ("project", "id", "by"), cmd=["bug", "claim"], pos=("project", "id"))
tool("bug_move", "Move a card between open, poc, inprogress and testing (never to done: only `bug_verify` "
     "does that). Refused moves come back as an error saying what the card still needs.",
     dict(CARD, to=_s("Target state.", enum=["open", "poc", "inprogress", "testing"]),
          branch=_text("The fix branch; needed to enter testing.", 200), by=BY),
     ("project", "id", "to", "by"), cmd=["bug", "move"], pos=("project", "id"))
tool("bug_note", "Leave a short progress note on a card; it replaces the card's last note and serves as the "
     "agent's heartbeat.", dict(CARD, text=_text("The note."), by=BY), ("project", "id", "text", "by"),
     cmd=["bug", "note"], pos=("project", "id"))
tool("bug_edit", "Change a card's text, its test (`flow`), `done_when` or lag limit; bumps the card's `rev`. "
     "Priority cannot be set here: it is the owner's.",
     dict(CARD, **CARD_TEXT, done_when=_text("What must be true for the card to be done."),
          limit={"type": "number", "minimum": 0, "description": "Lag cards: the number to get under."},
          limit_unit=_text("Unit of the limit, e.g. ms.", 16), by=BY),
     ("project", "id", "by"), cmd=["bug", "edit"], pos=("project", "id"))
tool("bug_ack", "Record that you, the card's agent, have read its current revision after somebody edited it; "
     "needed before moving it on.", dict(CARD, by=BY), ("project", "id", "by"),
     cmd=["bug", "ack"], pos=("project", "id"))
tool("bug_measure", "Record a measured number on a lag card (for example open time in ms), before and after "
     "the fix.",
     dict(CARD, value={"type": "number", "minimum": 0, "description": "The measured value."},
          unit=_text("Unit, default ms.", 16), run=dict(RUN, description="The run it was measured in."), by=BY),
     ("project", "id", "value", "by"), cmd=["bug", "measure"], pos=("project", "id"))
tool("bug_review", "Record a second pair of eyes on a card in testing (screens light and dark, the diff). "
     "Never by the card's own agent.",
     dict(CARD, ok={"type": "boolean", "description": "true = the check passed, false = it failed."},
          text=_text("What was checked and found."), by=BY),
     ("project", "id", "ok", "by"), call=bug_review)
tool("bug_verify", "Run the engine's checks on a card in testing (red, green, suite, limit, review, rev) and "
     "record the result; the card becomes done only if all hold. Returns the card with `last_verify`.",
     CARD, ("project", "id"), cmd=["bug", "verify"], pos=("project", "id"))
tool("triage_mark", "Mark one flow of a finished run as fix, not-a-bug or flaky in the project's triage log.",
     {"run": RUN, "flow": FLOW, "mark": _s("The verdict.", enum=["fix", "not-a-bug", "flaky"]),
      "note": _text("Why."), "by": BY},
     ("run", "flow", "mark", "by"), cmd=["triage"], pos=("run", "flow"))

# Runs.
tool("run_flow", "Start a run of only the named flows (at most 3) in the background and return its run id; "
     "follow it with `run_events`. Agents may use this without asking, to reproduce a bug or check a fix.",
     {"project": PROJECT, "flows": dict(_list(FLOW, "The flows to run."), maxItems=3), "from": FROM, "dry_run": DRY},
     ("project", "flows"), call=start_run)
tool("run_suite", "Start a run of whole suites in the background and return its run id. Only after the owner "
     "said yes in this conversation, having been told the project, the suites, the time estimate (from `plan`) "
     "and the simulators it will use.",
     {"project": PROJECT, "suites": _list(SUITE, "The suites to run."), "from": FROM, "dry_run": DRY},
     ("project", "suites"), call=start_run)
tool("run_stop", "Stop a run: it cleans up its simulators and ends as `stopped`. Returns whether the run was "
     "signalled.", {"run": RUN}, ("run",), cmd=["stop"], pos=("run",))

# The owner's word.
tool("bug_decide", "Record the OWNER's decision on a card: approve (found to open, with a priority), reject "
     "(with a reason) or reopen. Call it only when the owner has just said so in this conversation, never on "
     "an agent's own judgment.",
     dict(CARD, decision=_s("The owner's decision.", enum=["approve", "reject", "reopen"]),
          priority=dict(PRIORITY, description="For approve: the priority the owner gave."),
          reason=_text("For reject: the owner's reason, e.g. \"not a bug\" or \"same as B-4\".")),
     ("project", "id", "decision"), call=bug_decide)


# --- input checking ----------------------------------------------------------

TYPES = {"string": str, "boolean": bool, "integer": int, "number": (int, float), "array": list, "object": dict}


def problem(schema, value, where="arguments"):
    """The first way `value` breaks `schema`, or None. Covers the keywords the
    tool schemas above use, no more."""
    t = schema.get("type")
    if t and (not isinstance(value, TYPES[t]) or (t != "boolean" and isinstance(value, bool))
              or (isinstance(value, float) and not math.isfinite(value))):
        return "%s must be %s %s" % (where, "an" if t[0] in "aio" else "a", t)
    if "enum" in schema and value not in schema["enum"]:
        return "%s must be one of: %s" % (where, ", ".join(map(str, schema["enum"])))
    if "not" in schema and problem(schema["not"], value, where) is None:
        return "%s may not be %r" % (where, value)
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            return "%s must be %d to %d characters" % (where, schema.get("minLength", 0), schema.get("maxLength", 0))
        # Python's `$` also matches before a final newline; JSON Schema's does not.
        if "pattern" in schema and (not re.search(schema["pattern"], value) or value.endswith("\n")):
            return "%s %r does not look right (%s)" % (where, value[:80], schema.get("description", schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < schema.get("minimum", value):
        return "%s must be at least %s" % (where, schema["minimum"])
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
            return "%s must have %d to %d items" % (where, schema.get("minItems", 0), schema.get("maxItems", 0))
        for i, item in enumerate(value):
            bad = problem(schema.get("items", {}), item, "%s[%d]" % (where, i))
            if bad:
                return bad
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            return "missing %s" % ", ".join(missing)
        unknown = [k for k in value if k not in props]
        if unknown and schema.get("additionalProperties") is False:
            return "unknown argument %s (have: %s)" % (", ".join(unknown), ", ".join(props) or "none")
        for k, v in value.items():
            bad = problem(props.get(k, {}), v, k)
            if bad:
                return bad
    return None


# --- JSON-RPC ----------------------------------------------------------------

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def call_tool(params):
    t = TOOLS.get(params.get("name")) if isinstance(params, dict) else None
    if not t:
        raise RpcError(-32602, "Unknown tool: %s" % (params.get("name") if isinstance(params, dict) else None))
    args = params.get("arguments")
    args = {} if args is None else args
    try:
        bad = problem(t["inputSchema"], args)
        if bad:
            raise ToolError("%s: %s" % (t["name"], bad))
        text, failed = json.dumps(t["call"](args), separators=(",", ":"), ensure_ascii=False), False
    except ToolError as e:
        text, failed = str(e), True
    log("%s %s" % (t["name"], "refused" if failed else "ok"))
    return {"content": [{"type": "text", "text": text}], "isError": failed}


def answer(method, params):
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "e2e", "version": "1.0.0"},
                "instructions": "The e2e test console of this Mac: runs, reports and the bug board. Reading "
                                "tools change nothing. `run_suite` and `bug_decide` need the owner's word in "
                                "this conversation first."}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [dict({k: t[k] for k in ("name", "description", "inputSchema")},
                               annotations={"readOnlyHint": t["read"]}) for t in TOOLS.values()]}
    if method == "tools/call":
        return call_tool(params)
    raise RpcError(-32601, "Method not found: %s" % method)


def handle(line):
    """One line in, one reply out; None when the line was a notification."""
    try:
        msg = json.loads(line)
    except ValueError:
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None  # a reply to something; this server asks nothing
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled, …
    try:
        return {"jsonrpc": "2.0", "id": msg["id"], "result": answer(msg["method"], msg.get("params"))}
    except RpcError as e:
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": e.code, "message": str(e)}}
    except Exception as e:  # a bug here must not take the session down
        log("%s: %r" % (msg["method"], e))
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "Internal error: %s" % e}}


def main():
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print can never corrupt the protocol stream
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        reply = handle(line)
        if reply is not None:
            out.write(json.dumps(reply).encode() + b"\n")
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
