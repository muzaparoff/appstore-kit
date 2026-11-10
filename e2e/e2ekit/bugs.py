"""Bug board: `bugs.ndjson` and the rules for moving a card (CONTRACT.md "Bug board").

The file is append-only and a card is the fold of its lines, so a card's
history is free and nothing is ever rewritten. Every rule about who may move
a card where lives here, in one place, so the CLI, the console and the MCP
tools cannot disagree: they all go through `write()`.
"""

import collections
import datetime
import os
import re

from . import locks, store

STATES = ("found", "open", "poc", "inprogress", "testing", "done", "rejected")
PRIORITIES = ("P1", "P2", "P3")
KINDS = ("bug", "lag")
VIAS = ("chat", "console", "phone")
OWNER = "owner"
OWNER_TYPES = ("approve", "reject", "reopen")
EDITABLE = ("title", "do", "see", "want", "proof", "priority", "flow", "done_when", "limit", "limit_unit")
BROKEN = ("failed", "timeout", "crashed")
RAN = ("passed", "flaky") + BROKEN
GREEN_RUNS = 3

# What `bug move --to` allows, by the state the card is in.
MOVES = {
    "open": ("poc",),
    "poc": ("inprogress", "open"),
    "inprogress": ("testing", "open"),
    "testing": ("inprogress", "open"),
}

FLOW_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ID_RE = re.compile(r"^B-[1-9][0-9]*$")


class BugError(ValueError):
    pass


def path(project):
    return os.path.join(store.project_dir(project), "bugs.ndjson")


def _lock(project):
    return locks.Lock(os.path.join(store.engine_dir(), "bugs-%s.lock" % project))


def _time(iso):
    return datetime.datetime.fromisoformat(iso)


def _since(iso, start):
    """True when `iso` is at or after `start`; nothing is "since" a card that
    never entered testing."""
    if not iso or not start:
        return False
    try:
        return _time(iso) >= _time(start)
    except ValueError:
        return False


# --- fold -------------------------------------------------------------------

def fold(lines):
    """bugs.ndjson lines → {id: card}, in the order the cards were added.

    Reading is forgiving: a line that makes no sense for its card (hand
    edits, an old version) is skipped rather than failing the whole board.
    """
    cards = collections.OrderedDict()
    for e in lines:
        bug, kind = e.get("bug"), e.get("type")
        if kind == "add":
            cards[bug] = {
                "id": bug, "state": "found", "title": e.get("title", ""), "kind": e.get("kind", "bug"),
                "priority": None, "suggest": e.get("suggest"), "dry_run": bool(e.get("dry_run")),
                "do": e.get("do", ""), "see": e.get("see", ""), "want": e.get("want", ""),
                "proof": e.get("proof", ""), "flow": e.get("flow"), "run": e.get("run"),
                "done_when": None, "limit": None, "limit_unit": None,
                "agent": None, "branch": None, "rev": 1, "acked": 0, "note": None,
                "measures": [], "review": None, "reason": None, "passes": 0, "last_verify": None,
                "testing_since": None, "by": e.get("by"), "created": e.get("ts"), "updated": e.get("ts"),
                "history": [],
            }
        c = cards.get(bug)
        if c is None:
            continue
        c["history"].append(e)
        c["updated"] = e.get("ts")
        by = e.get("by")
        if kind == "approve":
            c["state"], c["priority"] = "open", e.get("priority")
        elif kind == "reject":
            c["state"], c["reason"] = "rejected", e.get("reason")
        elif kind == "reopen":
            c.update(state="open", agent=None, branch=None, testing_since=None, review=None, reason=None)
        elif kind == "claim":
            c["agent"], c["acked"] = by, c["rev"]
        elif kind == "move":
            to = e.get("to")
            if to not in MOVES:
                continue
            if to == "open":
                c.update(agent=None, testing_since=None, review=None)
            elif to == "poc" and not c["agent"]:
                c["agent"], c["acked"] = by, c["rev"]
            elif to == "testing":
                c["testing_since"], c["review"] = e.get("ts"), None
            if e.get("branch"):
                c["branch"] = e["branch"]
            c["state"] = to
        elif kind == "note":
            c["note"] = {"ts": e.get("ts"), "by": by, "text": e.get("text", "")}
        elif kind == "edit":
            for k in EDITABLE:
                if k in e:
                    c[k] = e[k]
            c["rev"] += 1
            if by == c["agent"]:
                c["acked"] = c["rev"]  # the agent wrote it, so the agent has read it
            elif c["state"] in ("inprogress", "testing"):
                # The target moved under the work: back to POC so the test
                # and "done when" are checked against the new wording.
                c.update(state="poc", testing_since=None, review=None)
        elif kind == "ack":
            c["acked"] = min(c["rev"], e.get("rev") or 0)
        elif kind == "measure":
            c["measures"].append({k: e.get(k) for k in ("ts", "by", "value", "unit", "run")})
        elif kind == "review":
            c["review"] = {"ts": e.get("ts"), "by": by, "ok": bool(e.get("ok")), "text": e.get("text", "")}
        elif kind == "verify":
            c["last_verify"] = {"ts": e.get("ts"), "ok": bool(e.get("ok")), "checks": e.get("checks", [])}
            if e.get("ok") and c["state"] == "testing":
                c["state"] = "done"
    return cards


def read(project):
    return fold(store.read_ndjson(path(project)))


def freeze(cards):
    """Design freeze: on while any real card is not finished."""
    return any(not c["dry_run"] and c["state"] not in ("done", "rejected") for c in cards.values())


def public(card, project=None, history=False):
    """A card as the CLI prints it; `project` adds the live pass count."""
    out = {k: v for k, v in card.items() if k != "history"}
    if project and card["state"] == "testing":
        out["passes"] = _passes(project, card)
    if history:
        out["history"] = card["history"]
    return out


def board(project, states=None):
    cards = read(project)
    counts = collections.OrderedDict((s, 0) for s in STATES)
    for c in cards.values():
        counts[c["state"]] += 1
    rows = [public(c, project) for c in cards.values() if not states or c["state"] in states]
    rows.sort(key=lambda c: (STATES.index(c["state"]), c["priority"] or c["suggest"] or "P9",
                             int(c["id"][2:])))
    return {"project": project, "freeze": freeze(cards), "counts": counts, "bugs": rows}


# --- write ------------------------------------------------------------------

def _text(value, cap):
    return (value or "").strip()[:cap]


def _need(card, bug):
    if card is None:
        raise BugError("no bug %s" % bug)
    return card


def _check(event, cards):
    """Refuse an event the rules do not allow. Returns nothing; raises BugError."""
    kind, by, bug = event["type"], event["by"], event["bug"]
    if kind in OWNER_TYPES and by != OWNER:
        raise BugError("only the owner can %s a bug (--by owner)" % kind)
    if kind == "add":
        if not event["title"]:
            raise BugError("--title is required")
        return
    c = _need(cards.get(bug), bug)
    state = c["state"]
    if kind == "approve":
        if state != "found":
            raise BugError("%s is %s; only a found bug can be approved" % (bug, state))
    elif kind == "reject":
        if state in ("done", "rejected"):
            raise BugError("%s is %s; reopen it first" % (bug, state))
        if not event["reason"]:
            raise BugError("--reason is required")
    elif kind == "reopen":
        if state not in ("done", "rejected"):
            raise BugError("%s is %s; only a done or rejected bug can be reopened" % (bug, state))
    elif kind == "move":
        to = event["to"]
        if state not in MOVES:
            raise BugError("%s is %s and cannot be moved%s" % (
                bug, state, "; the owner approves it first" if state == "found" else ""))
        if to not in MOVES[state]:
            raise BugError("%s cannot go from %s to %s (allowed: %s)" % (bug, state, to, ", ".join(MOVES[state])))
        if to in ("inprogress", "testing") and c["agent"] == by and c["acked"] < c["rev"]:
            raise BugError("%s changed (rev %d); read it and run: e2e bug ack" % (bug, c["rev"]))
        if state == "poc" and to == "inprogress":
            missing = [n for n, v in (("--flow", c["flow"]), ("--done-when", c["done_when"]),
                                      ("--limit", c["limit"] if c["kind"] == "lag" else True))
                       if v in (None, "")]
            if missing:
                raise BugError("%s needs %s before work starts (e2e bug edit)" % (bug, " and ".join(missing)))
        if to == "testing" and not (event.get("branch") or c["branch"]):
            raise BugError("%s needs --branch to enter testing" % bug)
    elif kind == "claim":
        if state in ("found", "done", "rejected"):
            raise BugError("%s is %s and cannot be claimed" % (bug, state))
    elif kind == "edit":
        if state in ("done", "rejected"):
            raise BugError("%s is %s; reopen it first" % (bug, state))
        if not any(k in event for k in EDITABLE):
            raise BugError("nothing to change")
        if "priority" in event and by != OWNER:
            raise BugError("only the owner sets the priority")
        if "title" in event and not event["title"]:
            raise BugError("--title cannot be empty")
    elif kind == "review":
        if state != "testing":
            raise BugError("%s is %s; a review is for a bug in testing" % (bug, state))
        if by == c["agent"]:
            raise BugError("a bug is reviewed by somebody other than its own agent")
    elif kind == "measure":
        if c["kind"] != "lag":
            raise BugError("%s is not a lag bug" % bug)


def write(project, kind, by, bug=None, via=None, fields=None):
    """Append one event after checking it against the rules; returns the card.

    `kind` is the event type; `fields` are the event's own fields, already
    checked for shape by the caller (`cli.py`). The lock makes read-check-append one step, so two
    agents cannot both claim a card or number two bugs the same.
    """
    by = _text(by, 64)
    if not by:
        raise BugError("--by is required")
    if not store.find_project(project):
        raise BugError("unknown project %r" % project)
    if bug is not None and not ID_RE.match(bug):
        raise BugError("bad bug id %r (like B-12)" % bug)
    store.ensure_dir(store.project_dir(project))
    lk = _lock(project)
    lk.acquire(kind)
    try:
        lines = store.read_ndjson(path(project))
        cards = fold(lines)
        if kind == "add":
            bug = "B-%d" % (sum(1 for l in lines if l.get("type") == "add") + 1)
        event = {"ts": store.now_iso(), "bug": bug, "type": kind, "by": by}
        if via:
            event["via"] = via
        event.update(fields or {})
        if kind == "ack":
            event["rev"] = _need(cards.get(bug), bug)["rev"]
        _check(event, cards)
        store.append_ndjson(path(project), event)
        return fold(lines + [event])[bug]
    finally:
        lk.release()


# --- verify -----------------------------------------------------------------

def _runs(project, dry_run):
    """(request, report) of finished runs, newest first; dry runs and real
    runs never count for each other's cards."""
    for rid in reversed(store.run_ids(project)):
        d = store.run_dir(rid)
        req = store.read_json(os.path.join(d, "request.json")) or {}
        if bool(req.get("dry_run")) != bool(dry_run):
            continue
        rep = store.read_json(os.path.join(d, "report.json"))
        if rep and rep.get("status") in store.TERMINAL:
            yield req, rep


def _flow(report, flow):
    for f in report.get("flows", []):
        if f.get("id") == flow:
            return f
    return None


def _fix_runs(project, card):
    """Runs that test the fix: on the card's branch, asked for after the card
    last entered testing. Newest first."""
    for req, rep in _runs(project, card["dry_run"]):
        if not _since(req.get("requested_at"), card["testing_since"]):
            break  # older runs follow; none of them can count
        if card["branch"] and req.get("branch") == card["branch"]:
            yield req, rep


def _green(project, card):
    """Results of `flow` in the latest fix runs that ran it, newest first."""
    out = []
    for req, rep in _fix_runs(project, card):
        f = _flow(rep, card["flow"])
        if f and f.get("status") in RAN:
            out.append({"run": rep["run"], "status": f["status"], "attempts": f.get("attempts")})
            if len(out) == GREEN_RUNS:
                break
    return out


def _passed_first_try(result):
    return result["status"] == "passed" and result["attempts"] in (1, None)


def _passes(project, card):
    if not card["flow"]:
        return 0
    n = 0
    for r in _green(project, card):
        if not _passed_first_try(r):
            break
        n += 1
    return n


def checks(project, card):
    """The Done checks for one card: [{"name","ok","detail"}]."""
    flow, out = card["flow"], []

    def add(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    if card["state"] != "testing":
        add("state", False, "%s is %s; only a bug in testing can be verified" % (card["id"], card["state"]))
        return out

    red = None
    for req, rep in _runs(project, card["dry_run"]):
        f = _flow(rep, flow)
        if f and f.get("status") in BROKEN:
            red = rep["run"]
            break
    add("red", red, "%s failed in %s" % (flow, red) if red
        else "no run has %s failing: the bug was never caught by a test" % flow)

    green = _green(project, card)
    good = len(green) == GREEN_RUNS and all(_passed_first_try(r) for r in green)
    add("green", good, "%s on branch %s: %s (need %d passes in a row, first try)" % (
        flow, card["branch"], ", ".join("%s %s" % (r["run"], r["status"]) for r in green) or "no runs yet",
        GREEN_RUNS))

    suite = flow.split("/")[0]
    whole, why = None, "no whole-suite run of %s on branch %s since testing" % (suite, card["branch"])
    for req, rep in _fix_runs(project, card):
        if req.get("flows") or suite not in (req.get("suites") or []):
            continue
        bad = [f["id"] for f in rep.get("flows", [])
               if f.get("suite") == suite and f.get("status") not in ("passed", "flaky")]
        if bad:
            why = "%s: %s did not pass" % (rep["run"], ", ".join(bad[:3]))
        else:
            whole = rep["run"]
        break  # the latest whole-suite run is the one that counts
    add("suite", whole, "%s passed in %s" % (suite, whole) if whole else why)

    if card["kind"] == "lag":
        before = [m for m in card["measures"] if not _since(m["ts"], card["testing_since"])]
        after = [m for m in card["measures"] if _since(m["ts"], card["testing_since"])]
        if not before:
            add("limit", False, "no number from before the fix (e2e bug measure in POC)")
        elif not after:
            add("limit", False, "no number since testing (limit %s %s)" % (card["limit"], card["limit_unit"] or ""))
        else:
            m = after[-1]
            add("limit", card["limit"] is not None and m["value"] <= card["limit"], "%s → %s %s (limit %s)" % (
                before[-1]["value"], m["value"], m.get("unit") or "", card["limit"]))

    r = card["review"]
    add("review", r and r["ok"], "checked by %s" % r["by"] if r and r["ok"]
        else ("%s said no: %s" % (r["by"], r["text"]) if r else "no review by another agent since testing"))
    add("rev", card["acked"] >= card["rev"], "rev %d, agent read rev %d" % (card["rev"], card["acked"]))
    return out


def verify(project, bug):
    """Run the checks and write the result. The only way a card reaches Done."""
    card = _need(read(project).get(bug), bug)
    result = checks(project, card)
    ok = all(c["ok"] for c in result)
    return write(project, "verify", "engine", bug, fields={"ok": ok, "checks": result}), ok
