---
name: e2e-console
description: Run and read e2e tests and the bug board through the `e2e` command (testing.muzaparoff.com). Use when asked to run a flow or suite, check a run, look at failures, or work a bug card (found, approve, POC, fix, verify).
---

# e2e console

One command, `~/GitHub/appstore-kit/bin/e2e`, is the only door to runs and bug
cards. The website shows the same data; never write under `~/e2e-runs` by hand.
Add `--json` to any command for data instead of text. The full contract is
`~/GitHub/appstore-kit/e2e/CONTRACT.md`.

## Who may do what

- **A single flow of your own card: run it without asking.**
  `e2e run <project> --flow <suite>/<flow> --by claude --detach`
- **A whole suite: ask the owner first** with project, suites, time estimate
  (`e2e plan <project> --suite a,b`) and simulators. Run only after a yes.
- **Owner decisions** (`bug approve | reject | reopen`, and `--priority` in
  `bug edit`) take `--by owner`. Pass it only when the owner said so in this
  chat, in their own words, and add `--via chat`. A subagent never passes it.
- One project name per repository. Never `e2e add` a worktree; build from one
  with `e2e run <project> … --from <folder>`.
- Never stop someone else's run. `e2e stop <run-id>` only for a run you started.

## Reading

| Need | Command |
|---|---|
| what is running, what waits for the owner, design freeze | `e2e brief [<project>]` |
| follow a run to its result | `e2e watch <run-id> --quiet` as a background task: one line per failure, then the result. Do not poll `status` in a loop |
| why a run failed | `e2e report <run-id> --json` → `failures[]` (message, log_excerpt, screenshot, video, repro) |
| the board | `e2e bug list <project>` · one card with history: `e2e bug show <project> B-<n>` |

## A bug card, start to Done

1. Found: `e2e bug add <project> --title … --do … --see … --want … [--kind lag] [--flow suite/flow] [--suggest P1|P2|P3] --by <agent>`.
   Then tell the owner in chat, short: what you do, what you see, what should
   happen, suggested priority. Nothing moves until the owner approves.
2. POC: `bug claim`, `bug move --to poc`. Write a flow that fails for this
   bug and run it (red). Record it: `bug edit --flow suite/flow --done-when "…"`
   (lag cards also `--limit <n> --limit-unit ms` and a first `bug measure`).
3. In progress: `bug move --to inprogress`. Fix on a branch. `bug note --text …`
   is the heartbeat while you work.
4. Testing: `bug move --to testing --branch <branch>`. Run the card's flow
   until it has passed three times in a row, then ask the owner for the
   whole-suite run. Another agent adds `bug review`.
5. Done: `e2e bug verify <project> B-<n>`. It is the only way to Done and
   prints each check that is missing. No agent and no move can set Done.

If the owner edits your card it goes back to POC: read it, `bug ack`, then
carry on. While any real card is open the design freeze is on: no new feature
work and no push.
