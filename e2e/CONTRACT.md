# e2e: engine ↔ console contract

The e2e module has two halves that meet only on disk and through the CLI:

- **engine** (`e2e/e2ekit/`, CLI `bin/e2e`): builds apps, drives simulators,
  runs Maestro / XCUITest, writes run folders.
- **console** (`e2e/console/`): a web UI on 127.0.0.1:4020 that *reads* run
  folders and *starts/stops* runs only by invoking the CLI.

Neither imports the other. Anything the console shows comes from files below;
anything it changes goes through `bin/e2e`.

Python: macOS system `/usr/bin/python3` (3.9). Stdlib only, except PyYAML,
which `bin/e2e` installs once into a private venv
(`~/.local/share/e2e/venv`, `pyyaml==6.0.2`) and re-execs into. The console
uses the same venv. No other dependencies.

## Runs root

`$E2E_HOME` (default `~/e2e-runs`):

```
~/e2e-runs/
  projects.json              registry: [{"name","path","added"}]   (admin: e2e add)
  engine/
    build.lock               fcntl lock: the one build slot
    sim-1.lock … sim-N.lock  fcntl locks: the simulator pool (N = machine cap 3)
    project-<name>.lock      fcntl lock: one active run per project
    golden.json              {"<device>|<runtime>": "<udid>"} golden sims
  build/<project>/           DerivedData for that project (kept between runs)
  <project>/
    history.ndjson           append-only: one line per finished run
                             {"run","finished","status","counts","sha256"}
    triage.ndjson            append-only: {"ts","run","flow","mark","by","note"}
                             mark ∈ fix | not-a-bug | flaky
    quarantine.json          {"<flow>": {"flaky_runs": n, "since": ts}}
    bugs.ndjson              append-only: one line per bug-card event ("Bug board")
    runs/<run-id>/           run-id = "<project>-<n>", n increments
      request.json           written by `e2e run` before anything else
      state.json             rewritten atomically (tmp + rename) as it goes
      events.ndjson          append-only event stream
      live/sim-<k>.png       latest screenshot per sim, ~1 fps, deleted at end
      flows/<flow-id>/       per-flow artifacts: junit.xml, maestro/, xcresult,
                             video.mp4, log.txt, screenshot.png
      report.json            written ONCE at the end; then the whole run folder
                             is chmod -R a-w (dirs 0555, files 0444)
```

### request.json
```json
{"run":"camipack-14","project":"camipack","suites":["trips","budget"],
 "mode":"scripted","requested_by":"console|cli|claude","requested_at":"ISO",
 "commit":"79907d9","branch":"main","dry_run":false,
 "flows":null,"from":null}
```
`flows` is the list of flow ids of a `--flow` run (null = whole suites);
`from` is the folder of a `--from` run (null = the registered folder).
`commit` and `branch` are read from the folder the run was built from.

### state.json
```json
{"run":"camipack-14","project":"camipack","status":"queued|building|running|finishing|passed|failed|stopped|error",
 "phase":"waiting for build slot","started":"ISO","updated":"ISO","finished":null,
 "estimate_s":1140,"elapsed_s":702,
 "sims":[{"slot":1,"udid":"…","name":"e2e-camipack-14-1","flow":"budget/04-split-edit","since":"ISO"}],
 "counts":{"total":19,"passed":11,"failed":1,"flaky":1,"skipped":0,"running":2,"pending":4},
 "flows":[{"id":"trips/01-create-trip","suite":"trips","engine":"maestro",
           "status":"pending|running|passed|failed|flaky|skipped|timeout|crashed",
           "attempts":1,"duration_s":38,"error":null,"quarantined":false}],
 "pid":12345}
```

### events.ndjson (one JSON per line)
`{"ts","type","run",…}` with type ∈ `queued, build_start, build_end, sim_up,
sim_down, flow_start, flow_end, step, log, limit_hit, stop_requested, done`.
`step` carries `{"sim":k,"text":"tap id=budget.split.amount"}` for the live log.

### report.json
state.json's final shape plus:
```json
{"failures":[{"flow":"budget/03-huge-amount","kind":"failed|timeout|crashed",
  "message":"…","screenshot":"flows/…/screenshot.png","video":"flows/…/video.mp4",
  "log_excerpt":"last 40 lines","crash":"…or null","repro":["step 1","step 2"]}],
 "limits":{"flow_s":240,"suite_s":1500,"run_s":5400,"sims":2,"retries":1},
 "machine":{"ram_free_gb":5.1,"disk_free_gb":31.0}}
```
`history.ndjson` stores `sha256(report.json)`; `e2e verify` and the console
flag a report whose hash no longer matches as **tampered**.

## app.yml (in each app repo: `e2e/app.yml`)

```yaml
project: camipack
xcodeproj: Camipack.xcodeproj      # or workspace:
scheme: Camipack                   # Debug, build-for-testing
bundle_id: com.muzaparoff.camipack
device: iPhone 17e                 # iPhone only
runtime: latest                    # or "iOS 27.0"
seams:                             # shared seam names -> this app's launch args
  reset: [-e2e.reset]
  pro:   [-proUnlocked]
  seed:  [-seedTrip, "{name}"]
  open:  [-openURL, "{url}"]
permissions: [photos, location, notifications]   # simctl privacy grant
suites:
  smoke:   {engine: maestro,  flows: e2e/flows/smoke,  budget: 5m}
  trips:   {engine: maestro,  flows: e2e/flows/trips,  budget: 12m}
  paywall: {engine: xcuitest, tests: [CamipackUITests/PaywallTests], budget: 8m}
limits: {sims: 2, flow: 4m, suite: 25m, run: 90m, retries: 1}
```
Maestro flows get seams as env: `-e SEAM_RESET="-e2e.reset"` etc., and
`APP_ID=<bundle_id>`.

## Limits

Defaults, and hard caps the app.yml can't exceed (engine clamps, logs
`limit_hit` when clamping):

| limit | default | cap |
|---|---|---|
| element wait (flows' own) | 10 s | 30 s |
| flow | 4 min | 10 min |
| suite | 25 min | 45 min |
| run | 90 min | 120 min |
| sims per project | 2 | 2 |
| sims machine-wide | 3 | 3 |
| builds at once | 1 | 1 |
| retries | 1 | 2 |
| quarantine after flaky runs | 3 | — |
| RAM free to boot a new sim | 3 GB | — |
| disk free to start a run | 12 GB | — |

## CLI (`bin/e2e`)

All commands accept `--json` for machine output. Exit 0 = ok.

| command | role | does |
|---|---|---|
| `e2e projects` | any | list projects with suites + last run |
| `e2e add <repo-path>` | admin | register a repo that has `e2e/app.yml`. One project name per repository: a second name for the same repo, or for a worktree of it, is refused |
| `e2e plan <project> --suite a,b` | any | estimate (s), sims, needs build?; runs nothing |
| `e2e run <project> --suite a,b [--mode scripted] [--by console\|cli\|claude] [--dry-run] [--detach]` | operator | create run, queue, build, test, report. `--detach` prints run-id and runs in background (new session, own process group) |
| `e2e run <project> --flow <suite>/<flow>[,…] [--from <folder>] …` | operator | `--flow` runs only the named flows (ids as in state.json; `--suite` not needed). `--from` builds from another checkout of the same repository (the registered folder or a worktree of it; anything else is refused): same project, same history |
| `e2e stop <run-id>` / `e2e stop --all` | operator | SIGTERM the run's process group; run cleans up and ends `stopped` |
| `e2e status [<run-id>]` | any | machine gauges + active runs |
| `e2e report <run-id>` | any | print report summary |
| `e2e verify [<project>]` | any | recheck report hashes |
| `e2e triage <run-id> <flow> --mark fix\|not-a-bug\|flaky --by <who> [--note …]` | operator | append one line to `triage.ndjson` |
| `e2e watch <run-id> [--quiet]` | any | follow a run: prints its events as they are written and ends when the run ends. With `--json` each line is the raw event, then one last line `{"type":"report","run","status","counts","failures":[{"flow","kind","message"}]}`. `--quiet` prints only `flow_end` that did not pass, `limit_hit`, and the last line |
| `e2e brief [<project>] [--line]` | any | one screen: active runs, last result per suite, bug cards that need the owner, design freeze. `--line` = one line for a status line |
| `e2e bug …` | see "Bug board" | the only writer of `bugs.ndjson` |
| `e2e clean` | admin | delete orphan `e2e-*` sims, stale live/ dirs, stale locks |
| `e2e prune <project> --keep N` | admin | the only command that deletes history |

`e2e status --json`:
```json
{"machine":{"ram_free_gb":5.6,"ram_total_gb":17.2,"disk_free_gb":41.7,
            "build_busy":false,"sims_busy":[1,2],"sims_cap":3},
 "runs":[<state.json of each unfinished run, plus "alive": true|false>]}
```

`--dry-run` uses the fake backend: no xcodebuild, no simctl, no Maestro;
flows pass/fail/flake deterministically from their names (`*fail*` fails,
`*flaky*` passes on retry, `*slow*` takes 3 s; a Maestro flow file with the
line `# dry-run: fail` fails until the line is removed), live screenshots are
generated placeholder PNGs. The console is developed and tested against it.

## Bug board

A bug is a card with a life of its own, not a row in one run. `bugs.ndjson`
is append-only; a card is the fold of its lines, so history is free and
nothing is ever rewritten. Only `e2e bug` writes the file (under
`engine/bugs-<project>.lock`), and everybody else, the console included,
reads cards through `e2e bug list | show --json`, so the fold and the rules
exist once.

Every line: `{"ts","bug":"B-12","type","by",…}`; `via` (optional) says how
the owner's word arrived: `chat | console | phone`. `by` is a short name:
`owner`, `lead`, `reproducer`, `fixer-1`, `reviewer`, `engine`, `console`.

| type | written by | fields | effect |
|---|---|---|---|
| `add` | `bug add` | `title, kind (bug\|lag), do, see, want, proof, flow?, run?, suggest? (P1\|P2\|P3), dry_run?` | new card, state `found` |
| `approve` | `bug approve` (owner) | `priority (P1\|P2\|P3)` | `found → open` |
| `reject` | `bug reject` (owner) | `reason` (e.g. "not a bug", "same as B-4") | any state but `done → rejected` |
| `reopen` | `bug reopen` (owner) | — | `done \| rejected → open`; clears agent, branch, passes |
| `claim` | `bug claim` | — | `agent = by`, and the claimer has read the card (`acked = rev`) |
| `move` | `bug move` | `to (open\|poc\|inprogress\|testing), branch?` | see moves below |
| `note` | `bug note` | `text` | card's last note; the agent heartbeat |
| `edit` | `bug edit` | any of `title, do, see, want, proof, priority, flow, done_when, limit, limit_unit` | changes fields, `rev += 1`. By the card's own agent: counts as read. By anybody else: a card in `inprogress \| testing` goes back to `poc`. `priority` only by the owner |
| `ack` | `bug ack` | `rev` | the agent has read revision `rev` |
| `review` | `bug review` | `ok, text` | a second pair of eyes on a card in `testing` (screens light and dark, the diff); never by the card's own agent |
| `measure` | `bug measure` | `value, unit, run?` | a lag number (typed by an agent for now; the engine reads it from the test result once speed tests exist) |
| `verify` | `bug verify` only | `ok, checks:[{"name","ok","detail"}], runs:[…]` | `ok` and state `testing` → `done` |

States: `found, open, poc, inprogress, testing, done, rejected`.
Owner-only types need `--by owner`. No command writes `done` except
`bug verify`.

Moves (`bug move --to`), all refused on `found`, `done`, `rejected`:

- `open → poc` (sets `agent = by` when unclaimed)
- a move to `inprogress | testing` by the card's agent is refused while
  `acked < rev`: somebody changed the card, so read it and `bug ack` first
- `poc → inprogress`: needs `flow` and `done_when`; a `lag` card also needs `limit`
- `inprogress → testing`: needs `branch` (given here or earlier)
- `testing → inprogress`, and `poc | inprogress | testing → open` (gives the card back; clears agent)

### Card (`e2e bug list <project> --json`, `e2e bug show <project> <id> --json`)
```json
{"project":"camipack","freeze":true,
 "counts":{"found":2,"open":1,"poc":1,"inprogress":1,"testing":1,"done":2,"rejected":1},
 "bugs":[{"id":"B-12","state":"poc","title":"Map freezes about 1 s when a day opens",
   "kind":"lag","priority":"P1","suggest":"P1","dry_run":false,
   "do":"Open a day","see":"Map freezes about 1 s","want":"No freeze","proof":"video in run camipack-100",
   "flow":"days/07-day-map-open","run":"camipack-100",
   "done_when":"days/07 passes 3 runs in a row; open time under 300 ms","limit":300,"limit_unit":"ms",
   "agent":"reproducer","branch":null,"rev":2,"acked":1,"by":"lead","reason":null,
   "review":null,"testing_since":null,
   "note":{"ts":"ISO","by":"reproducer","text":"writing the failing test"},
   "measures":[{"ts":"ISO","by":"reproducer","value":980,"unit":"ms","run":"camipack-101"}],
   "passes":0,"last_verify":{"ts":"ISO","ok":false,"checks":[{"name":"red","ok":true,"detail":"…"}]},
   "created":"ISO","updated":"ISO"}]}
```
`show` returns one card plus `"history": [<its lines>]`. `list` sorts by
state (found first, as listed above), then priority, then id. `freeze` is
true while any card that is not `dry_run` is outside `done` and `rejected`.
`passes` = how many of the last three runs of `flow` on the card's branch,
asked for since the card entered `testing`, passed first try (0–3).
`review` is `{"ts","by","ok","text"}`, `reason` is why a card was rejected,
`by` is who filed it.

### `e2e bug verify <project> <id>`: the only way to Done

Reads finished runs (report.json) of the project. A `dry_run` card counts
only `--dry-run` runs, a real card only real runs. All checks must hold:

1. `red`: some finished run has `flow` as `failed | timeout | crashed`
   (the bug was caught by a test before the fix).
2. `green`: the last three finished runs that contain `flow`, were built
   from the card's `branch`, and started after the card last entered
   `testing`, all have it `passed` with `attempts == 1`.
3. `suite`: one finished run since `testing`, on the card's `branch`, ran the
   flow's whole suite (`request.flows` null) with no flow of that suite
   `failed | timeout | crashed`.
4. `limit` (lag cards): the last `measure` since `testing` is ≤ `limit`, and
   an earlier `measure` exists.
5. `review`: the last `review` since `testing` is ok.
6. `rev`: the card's agent has acked the current `rev`.

"Since testing" means the run was asked for (`requested_at`) after the card
last entered `testing`; going back to `inprogress` and in again starts the
count over. Not checked yet: new build warnings, and the owner's eye on a
visible change (the owner can `reopen`).

It appends one `verify` line either way and exits 0 when Done, 1 otherwise.

### Commands
```
e2e bug add <project> --title … --do … --see … --want … [--proof …] [--kind bug|lag]
            [--flow suite/flow] [--run run-id] [--suggest P1|P2|P3] [--dry-run] --by <who>
e2e bug approve <project> <id> --priority P1|P2|P3 --by owner [--via chat|console|phone]
e2e bug reject  <project> <id> --reason … --by owner [--via …]
e2e bug reopen  <project> <id> --by owner [--via …]
e2e bug claim   <project> <id> --by <agent>
e2e bug move    <project> <id> --to open|poc|inprogress|testing [--branch name] --by <agent>
e2e bug note    <project> <id> --text … --by <who>
e2e bug edit    <project> <id> [--title …] [--do …] [--see …] [--want …] [--proof …]
            [--priority P1|P2|P3] [--flow …] [--done-when …] [--limit N] [--limit-unit ms] --by <who>
e2e bug ack     <project> <id> --by <agent>
e2e bug measure <project> <id> --value N [--unit ms] [--run run-id] --by <who>
e2e bug review  <project> <id> [--no] [--text …] --by <another agent>
e2e bug verify  <project> <id>
e2e bug list    <project> [--state found,open,…]
e2e bug show    <project> <id>
```
Text fields are capped (title 200, others 2000 characters). Each writing
command prints the card after the change (`--json`: the card object).

## Run lifecycle (engine)

1. Validate project + suites against app.yml (fixed choices only). Disk ≥ 12 GB.
2. Take `project-<name>.lock` (non-blocking → refuse "already running").
3. **Retention**: delete heavy files of this project's previous runs
   (`flows/*/video.mp4` except failed flows of the last run, raw logs, `live/`,
   xcresult bundles). Keep report.json, JUnit, failure screenshot + log excerpt.
   Runs are read-only; retention flips write bits only on the files it deletes.
4. Wait for `build.lock` (status `queued`, phase "waiting for build slot"),
   `xcodebuild build-for-testing` into `build/<project>`, release lock.
5. Acquire sim slots (≤ project cap, ≥ 1; wait while none free). Before each
   new sim: RAM free ≥ 3 GB. Sims = `simctl clone <golden> e2e-<run>-<k>`;
   golden created once per device+runtime, never booted by tests.
   Install app, grant permissions.
6. Scheduler hands flows to free sims; per-flow timeout; retry once; passes on
   retry → `flaky` (+ quarantine counter). Suite and run timers stop the rest
   as `skipped` with `limit_hit`.
7. Live screenshots: `simctl io <udid> screenshot` ~1 fps into `live/`.
8. Always (finally + SIGTERM/SIGINT handlers): shutdown + delete this run's
   sims, release slots, remove `live/`, write report.json, append history,
   chmod read-only.

Engine never touches simulators whose name doesn't start with `e2e-`, never
commits, pushes, uploads or signs.

## Console roles

- **viewer**: every GET. Mobile user agents are always viewer.
- **operator**: POST `/api/runs` (start), POST `/api/runs/<id>/stop`
  (only runs with `requested_by == console`), POST `/api/triage`
  (→ `e2e triage`).
  Needs a signed-in session (see below), a non-mobile UA,
  a CSRF token, and the start form's confirm field.
  Bug board: GET `/bugs`, `/bugs/<project>`, `/bugs/<project>/<id>`;
  POST `/api/bugs/<project>` (add, e.g. "File as bug" on a Triage row),
  POST `/api/bugs/<project>/<id>/approve | reject | reopen | edit`
  (→ `e2e bug … --by owner --via console`).
- **phone**: a signed-in mobile UA is a viewer plus exactly three bug
  decisions: POST `/api/bugs/<project>/<id>/approve | reject | reopen`
  (→ `--by owner --via phone`), with the CSRF token. Nothing else: no runs,
  no stops, no edits, no adds.
- **admin**: none in the console. CLI on the Mac only.

Every request except `/login` and `/static/console.css` needs a login
(`E2E_CONSOLE_USER` / `E2E_CONSOLE_PASS` from `~/.config/e2e/console.env`,
mode 600, owner-written). Browsers sign in on the `/login` form and get an
`e2e_session` cookie (HMAC-signed expiry, 90 days, HttpOnly, Secure,
SameSite=Lax, renewed when used after a day; keyed on the login, so changing the password signs everyone
out). Scripts may send Basic auth instead. `POST /logout` clears the cookie.
Inputs are validated against `e2e projects --json`; nothing from a request is
ever passed to a shell.
