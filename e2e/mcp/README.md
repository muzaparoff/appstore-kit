# e2e MCP server

`bin/e2e` as named tools for Claude Code, so each kind of action has its own
permission: reading can be always allowed while runs and the owner's decisions
stay on "ask".

`e2e/mcp/server.py` is one file, Python 3.9, standard library only. It speaks
MCP over stdio (newline-delimited JSON-RPC, tools only). Every tool checks its
input against a JSON Schema, builds an argv list and runs `bin/e2e … --json`;
nothing goes through a shell. A CLI refusal comes back as a tool error with
the CLI's message. `E2E_HOME` is honoured as usual.

## Tools

| group | tools |
|---|---|
| reading (change nothing) | `projects`, `status`, `report`, `plan`, `history_verify`, `brief`, `bug_list`, `bug_show`, `run_events` |
| agent writes (each signed with `by`; the engine signs `bug_verify`) | `bug_add`, `bug_claim`, `bug_move`, `bug_note`, `bug_edit`, `bug_ack`, `bug_measure`, `bug_review`, `bug_verify`, `triage_mark` |
| runs | `run_flow` (named flows, at most 3), `run_suite` (whole suites), `run_stop` |
| the owner's word | `bug_decide` (approve, reject, reopen; written as `--by owner --via chat`) |

Runs start detached with `--by claude` and return a run id; follow one with
`run_events` (pass the returned `next` back as `after`). The agent tools refuse
`by: owner`, and `bug_edit` has no `priority`: only `bug_decide` writes as the
owner.

## Add it to Claude Code

```sh
claude mcp add e2e -- /Users/you/GitHub/appstore-kit/bin/e2e-mcp
```

or in a project's `.mcp.json`:

```json
{"mcpServers": {"e2e": {"command": "/Users/you/GitHub/appstore-kit/bin/e2e-mcp"}}}
```

## Suggested permissions

In `.claude/settings.json`: allow the reading tools, ask for the rest.

```json
{"permissions": {
  "allow": ["mcp__e2e__projects", "mcp__e2e__status", "mcp__e2e__report", "mcp__e2e__plan",
            "mcp__e2e__history_verify", "mcp__e2e__brief", "mcp__e2e__bug_list",
            "mcp__e2e__bug_show", "mcp__e2e__run_events"],
  "ask": ["mcp__e2e__run_suite", "mcp__e2e__run_stop", "mcp__e2e__bug_decide"]
}}
```

Tools in neither list ask by default. The agent writes and `run_flow` are
meant to be usable without asking; move them to `allow` once that is wanted.
Keep `run_suite` and `bug_decide` on ask: each is the owner saying yes.

## Tests

```sh
cd e2e && ~/.local/share/e2e/venv/bin/python -m unittest discover -s mcp/tests -t .
```

They drive the server as a subprocess against a throwaway `E2E_HOME` and the
fake backend (`--dry-run`); nothing real is built or run.
