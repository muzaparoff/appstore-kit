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
 "commit":"79907d9","branch":"main","dry_run":false}
```

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
| `e2e add <repo-path>` | admin | register a repo that has `e2e/app.yml` |
| `e2e plan <project> --suite a,b` | any | estimate (s), sims, needs build?; runs nothing |
| `e2e run <project> --suite a,b [--mode scripted] [--by console\|cli\|claude] [--dry-run] [--detach]` | operator | create run, queue, build, test, report. `--detach` prints run-id and runs in background (new session, own process group) |
| `e2e stop <run-id>` / `e2e stop --all` | operator | SIGTERM the run's process group; run cleans up and ends `stopped` |
| `e2e status [<run-id>]` | any | machine gauges + active runs |
| `e2e report <run-id>` | any | print report summary |
| `e2e verify [<project>]` | any | recheck report hashes |
| `e2e triage <run-id> <flow> --mark fix\|not-a-bug\|flaky --by <who> [--note …]` | operator | append one line to `triage.ndjson` |
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
`*flaky*` passes on retry, `*slow*` takes 3 s), live screenshots are
generated placeholder PNGs. The console is developed and tested against it.

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
- **admin**: none in the console. CLI on the Mac only.

Every request except `/login` and `/static/console.css` needs a login
(`E2E_CONSOLE_USER` / `E2E_CONSOLE_PASS` from `~/.config/e2e/console.env`,
mode 600, owner-written). Browsers sign in on the `/login` form and get an
`e2e_session` cookie (HMAC-signed expiry, 90 days, HttpOnly, Secure,
SameSite=Lax, renewed when used after a day; keyed on the login, so changing the password signs everyone
out). Scripts may send Basic auth instead. `POST /logout` clears the cookie.
Inputs are validated against `e2e projects --json`; nothing from a request is
ever passed to a shell.
