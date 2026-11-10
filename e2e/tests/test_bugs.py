"""The bug board through the CLI: who may move a card where, and what Done takes."""

import json
import os
import subprocess

from tests.helpers import HomeCase

CARD = ("--title", "Map freezes when a day opens", "--do", "Open a day",
        "--see", "Map freezes about 1 s", "--want", "No freeze")


class BugCase(HomeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(flows={"smoke": ["01-launch"], "trips": ["01-list", "07-day-map-open"]})
        self.flow_file = os.path.join(self.repo, "e2e", "flows", "trips", "07-day-map-open.yaml")
        self.git("init", "-q", "-b", "main")
        self.commit("first")
        self.assertEqual(self.cli("add", self.repo).returncode, 0)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com"]
                       + list(args), check=True, capture_output=True)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def bug(self, *args):
        return self.cli_json("bug", *args)

    def ok(self, *args):
        code, out = self.bug(*args)
        self.assertEqual(code, 0, out)
        return out

    def refused(self, text, *args):
        code, out = self.bug(*args)
        self.assertEqual(code, 1, out)
        self.assertIn(text, out["error"])

    def run_flow(self, flow="trips/07-day-map-open"):
        code, out = self.cli_json("run", "demo", "--flow", flow, "--dry-run", "--by", "claude")
        return out


class BoardRules(BugCase):
    def test_found_waits_for_the_owner(self):
        card = self.ok("add", "demo", *CARD, "--by", "reviewer", "--suggest", "P1", "--dry-run")
        self.assertEqual((card["id"], card["state"], card["priority"], card["suggest"]),
                         ("B-1", "found", None, "P1"))
        self.refused("owner approves it first", "move", "demo", "B-1", "--to", "poc", "--by", "reproducer")
        self.refused("only the owner", "approve", "demo", "B-1", "--priority", "P1", "--by", "lead")
        card = self.ok("approve", "demo", "B-1", "--priority", "P2", "--by", "owner", "--via", "chat")
        self.assertEqual((card["state"], card["priority"]), ("open", "P2"))
        self.assertEqual(self.ok("add", "demo", *CARD, "--by", "lead")["id"], "B-2")
        self.refused("--reason", "reject", "demo", "B-2", "--reason", " ", "--by", "owner")
        self.assertEqual(self.ok("reject", "demo", "B-2", "--reason", "same as B-1", "--by", "owner")["state"],
                         "rejected")
        self.refused("no bug B-9", "claim", "demo", "B-9", "--by", "lead")
        self.refused("unknown project", "list", "nope")

    def test_work_needs_a_test_and_done_when(self):
        self.ok("add", "demo", *CARD, "--by", "lead", "--kind", "lag")
        self.ok("approve", "demo", "B-1", "--priority", "P1", "--by", "owner")
        card = self.ok("move", "demo", "B-1", "--to", "poc", "--by", "reproducer")
        self.assertEqual((card["state"], card["agent"]), ("poc", "reproducer"))
        self.refused("needs --flow and --done-when and --limit",
                     "move", "demo", "B-1", "--to", "inprogress", "--by", "reproducer")
        self.refused("suite/flow", "edit", "demo", "B-1", "--flow", "07-day-map-open", "--by", "reproducer")
        self.refused("only the owner sets the priority",
                     "edit", "demo", "B-1", "--priority", "P3", "--by", "reproducer")
        self.ok("edit", "demo", "B-1", "--flow", "trips/07-day-map-open", "--limit", "300",
                "--done-when", "trips/07 passes 3 runs in a row; open time under 300 ms", "--by", "reproducer")
        self.refused("cannot go from poc to testing", "move", "demo", "B-1", "--to", "testing", "--by", "fixer-1")
        self.ok("claim", "demo", "B-1", "--by", "fixer-1")
        self.ok("move", "demo", "B-1", "--to", "inprogress", "--by", "fixer-1")
        self.refused("needs --branch", "move", "demo", "B-1", "--to", "testing", "--by", "fixer-1")
        card = self.ok("move", "demo", "B-1", "--to", "testing", "--branch", "fix/b-1", "--by", "fixer-1")
        self.assertEqual((card["state"], card["branch"], card["passes"]), ("testing", "fix/b-1", 0))

    def test_owner_edit_sends_the_card_back_and_must_be_read(self):
        self.ok("add", "demo", *CARD, "--by", "lead", "--flow", "trips/07-day-map-open")
        self.ok("approve", "demo", "B-1", "--priority", "P1", "--by", "owner")
        self.ok("move", "demo", "B-1", "--to", "poc", "--by", "fixer-1")
        self.ok("edit", "demo", "B-1", "--done-when", "trips/07 passes", "--by", "fixer-1")
        card = self.ok("move", "demo", "B-1", "--to", "inprogress", "--by", "fixer-1")
        self.assertEqual((card["rev"], card["acked"]), (2, 2))  # its own edit counts as read
        card = self.ok("edit", "demo", "B-1", "--want", "No freeze, and the pins stay", "--by", "owner",
                       "--via", "console")
        self.assertEqual((card["state"], card["rev"], card["acked"]), ("poc", 3, 2))
        self.refused("read it and run: e2e bug ack", "move", "demo", "B-1", "--to", "inprogress", "--by", "fixer-1")
        self.assertEqual(self.ok("ack", "demo", "B-1", "--by", "fixer-1")["acked"], 3)
        self.assertEqual(self.ok("move", "demo", "B-1", "--to", "inprogress", "--by", "fixer-1")["state"],
                         "inprogress")
        self.ok("note", "demo", "B-1", "--text", "moving the map load off the main thread", "--by", "fixer-1")
        code, shown = self.bug("show", "demo", "B-1")
        self.assertEqual(shown["note"]["text"], "moving the map load off the main thread")
        self.assertEqual([e["type"] for e in shown["history"]],
                         ["add", "approve", "move", "edit", "move", "edit", "ack", "move", "note"])
        self.assertEqual(shown["history"][5]["via"], "console")

    def test_list_counts_and_freeze(self):
        self.ok("add", "demo", *CARD, "--by", "lead", "--dry-run")
        code, board = self.bug("list", "demo")
        self.assertEqual((board["freeze"], board["counts"]["found"]), (False, 1))  # practice cards never freeze
        self.ok("add", "demo", *CARD, "--by", "lead")
        self.ok("approve", "demo", "B-2", "--priority", "P1", "--by", "owner")
        code, board = self.bug("list", "demo")
        self.assertEqual((board["freeze"], [b["id"] for b in board["bugs"]]), (True, ["B-1", "B-2"]))
        code, board = self.bug("list", "demo", "--state", "open")
        self.assertEqual([b["id"] for b in board["bugs"]], ["B-2"])
        self.ok("reject", "demo", "B-2", "--reason", "not a bug", "--by", "owner")
        self.assertFalse(self.bug("list", "demo")[1]["freeze"])
        self.assertIn("design freeze: off", self.cli("bug", "list", "demo").stdout)


class SingleFlowRuns(BugCase):
    def test_flow_runs_only_that_flow(self):
        out = self.run_flow()
        self.assertEqual((out["status"], out["counts"]["total"]), ("passed", 1))
        req = self.cli_json("report", out["run"])[1]
        self.assertEqual([f["id"] for f in req["flows"]], ["trips/07-day-map-open"])
        code, err = self.cli_json("run", "demo", "--flow", "trips/99-nope", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("unknown flow(s) trips/99-nope", err["error"])
        code, err = self.cli_json("run", "demo", "--dry-run")
        self.assertIn("one of them", err["error"])

    def test_card_from_a_practice_run_is_a_practice_card(self):
        run = self.run_flow()["run"]
        card = self.ok("add", "demo", *CARD, "--run", run, "--by", "console")
        self.assertTrue(card["dry_run"])
        self.assertFalse(self.ok("list", "demo")["freeze"])

    def test_from_takes_only_a_checkout_of_the_same_repository(self):
        tree = os.path.join(self.tmp, "tree")
        self.git("worktree", "add", "-q", "-b", "fix/b-1", tree)
        code, out = self.cli_json("run", "demo", "--flow", "smoke/01-launch", "--from", tree, "--dry-run")
        self.assertEqual((code, out["run"]), (0, "demo-1"))
        with open(self.run_path("demo-1", "request.json")) as f:
            req = json.load(f)
        self.assertEqual((req["branch"], req["from"], req["flows"]),
                         ("fix/b-1", os.path.realpath(tree), ["smoke/01-launch"]))
        other = self.make_repo("other")
        code, err = self.cli_json("run", "demo", "--suite", "smoke", "--from", other, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("is not a checkout of demo", err["error"])


class Done(BugCase):
    """Only `bug verify` writes Done, and only with the evidence in the runs."""

    def start(self, kind="bug"):
        with open(self.flow_file, "a") as f:
            f.write("# dry-run: fail\n")  # the reproducer's failing test
        self.commit("test that catches the bug")
        self.assertEqual(self.run_flow()["status"], "failed")
        self.ok("add", "demo", *CARD, "--by", "lead", "--dry-run", "--kind", kind,
                "--flow", "trips/07-day-map-open", "--run", "demo-1")
        self.ok("approve", "demo", "B-1", "--priority", "P1", "--by", "owner")
        self.ok("move", "demo", "B-1", "--to", "poc", "--by", "fixer-1")
        self.ok("edit", "demo", "B-1", "--done-when", "trips/07 passes 3 runs in a row", "--by", "fixer-1",
                *(("--limit", "300") if kind == "lag" else ()))

    def fix(self):
        self.ok("move", "demo", "B-1", "--to", "inprogress", "--by", "fixer-1")
        self.git("checkout", "-q", "-b", "fix/b-1")
        with open(self.flow_file) as f:
            text = f.read()
        with open(self.flow_file, "w") as f:
            f.write(text.replace("# dry-run: fail\n", ""))
        self.commit("the fix")
        self.ok("move", "demo", "B-1", "--to", "testing", "--branch", "fix/b-1", "--by", "fixer-1")

    def verify(self):
        code, card = self.bug("verify", "demo", "B-1")
        return code, card, {c["name"]: c["ok"] for c in card["last_verify"]["checks"]}

    def test_no_move_and_no_agent_reaches_done(self):
        self.start()
        r = self.cli("bug", "move", "demo", "B-1", "--to", "done", "--by", "fixer-1", check=False)
        self.assertEqual(r.returncode, 2)  # not even a choice
        code, card, checks = self.verify()
        self.assertEqual((code, card["state"], checks), (1, "poc", {"state": False}))

    def test_three_passes_suite_and_review_make_done(self):
        self.start()
        self.fix()
        code, card, checks = self.verify()
        self.assertEqual((code, card["state"]), (1, "testing"))
        self.assertEqual(checks, {"red": True, "green": False, "suite": False, "review": False, "rev": True})
        for n in (1, 2, 3):
            self.assertEqual(self.run_flow()["status"], "passed")
            self.assertEqual(self.bug("show", "demo", "B-1")[1]["passes"], n)
        self.refused("somebody other than its own agent", "review", "demo", "B-1", "--by", "fixer-1")
        self.ok("review", "demo", "B-1", "--no", "--text", "dark mode pin is cut", "--by", "reviewer")
        code, card, checks = self.verify()
        self.assertEqual((checks["green"], checks["suite"], checks["review"]), (True, False, False))
        self.ok("review", "demo", "B-1", "--text", "light and dark match", "--by", "reviewer")
        self.cli_json("run", "demo", "--suite", "trips", "--dry-run")
        code, card, checks = self.verify()
        self.assertEqual((code, card["state"], set(checks.values())), (0, "done", {True}))
        self.refused("cannot be moved", "move", "demo", "B-1", "--to", "open", "--by", "fixer-1")
        card = self.ok("reopen", "demo", "B-1", "--by", "owner")
        self.assertEqual((card["state"], card["agent"], card["branch"]), ("open", None, None))

    def test_runs_on_another_branch_or_before_testing_do_not_count(self):
        self.start()
        self.fix()
        self.git("checkout", "-q", "main")  # the fix is not here: still red
        self.assertEqual(self.run_flow()["status"], "failed")
        self.git("checkout", "-q", "fix/b-1")
        self.run_flow()
        self.ok("move", "demo", "B-1", "--to", "inprogress", "--by", "reviewer")  # sent back
        self.ok("move", "demo", "B-1", "--to", "testing", "--by", "fixer-1")
        self.assertEqual(self.bug("show", "demo", "B-1")[1]["passes"], 0)  # the count starts again

    def test_lag_needs_a_number_under_the_limit(self):
        self.start("lag")
        self.ok("measure", "demo", "B-1", "--value", "980", "--run", "demo-1", "--by", "fixer-1")
        self.fix()
        code, card, checks = self.verify()
        self.assertFalse(checks["limit"])
        self.ok("measure", "demo", "B-1", "--value", "410", "--by", "fixer-1")
        self.assertFalse(self.verify()[2]["limit"])
        self.ok("measure", "demo", "B-1", "--value", "240", "--by", "fixer-1")
        self.assertTrue(self.verify()[2]["limit"])
