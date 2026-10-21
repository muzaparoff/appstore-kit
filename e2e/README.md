# e2e

Shared iOS end-to-end runs for the iPhone apps, on this Mac, started by hand.
Never in CI. `e2e/CONTRACT.md` is the binding spec; this is the short version.

- **engine** — `e2e/e2ekit/`, driven by `bin/e2e`: builds the app, clones
  simulators, runs Maestro flows and XCUITest classes, writes run folders.
- **console** — `e2e/console/`: web UI that reads run folders and starts or
  stops runs only through `bin/e2e`.

`bin/e2e` creates a private venv at `~/.local/share/e2e/venv` (system
`/usr/bin/python3` + `pyyaml==6.0.2`) on first use. Call it by absolute path
from anywhere. The older `bin/e2e-run.sh` (CI single-sim script) is separate
and unchanged.

## Commands

All take `--json`. Exit 0 = ok.

```sh
bin/e2e add ~/GitHub/camipack                 # register a repo with e2e/app.yml
bin/e2e projects                              # suites + last run
bin/e2e plan camipack --suite smoke,trips     # estimate; runs nothing
bin/e2e run camipack --suite smoke --dry-run  # fake backend, no Xcode/sims
bin/e2e run camipack --suite smoke --detach   # real run in background, prints run id
bin/e2e status [camipack-14]                  # machine gauges + active runs
bin/e2e stop camipack-14 [--wait 30]          # or: stop --all
bin/e2e report camipack-14
bin/e2e triage camipack-14 trips/02-edit --mark fix --by owner [--note "…"]
bin/e2e verify [camipack]                     # recheck report hashes (tampered?)
bin/e2e clean                                 # orphan e2e-* sims, dead runs, stale locks
bin/e2e prune camipack --keep 20              # the only thing that deletes history
```

`--by console|cli|claude` records who asked. `--dry-run` outcomes follow flow
names: `*fail*` fails, `*flaky*` passes on retry, `*slow*` takes 3 s.

## app.yml

Lives in each app repo at `e2e/app.yml`:

```yaml
project: camipack
xcodeproj: Camipack.xcodeproj      # or workspace:
scheme: Camipack
bundle_id: com.muzaparoff.camipack
device: iPhone 17e                 # iPhone only
runtime: latest                    # or "iOS 27.0"
seams:                             # passed to Maestro as SEAM_RESET etc.
  reset: [-e2e.reset]
  seed:  [-seedTrip, "{name}"]
permissions: [photos, location, notifications]
suites:
  smoke:   {engine: maestro,  flows: e2e/flows/smoke, budget: 5m}
  paywall: {engine: xcuitest, tests: [CamipackUITests/PaywallTests], budget: 8m}
limits: {sims: 2, flow: 4m, suite: 25m, run: 90m, retries: 1}
```

Each `*.yaml` in a Maestro suite's folder is one flow (`config.yaml` is
skipped). Each `tests:` entry is one XCUITest flow (`-only-testing`). Maestro
flows also get `APP_ID` and `E2E_WAIT_MS`.

## Where data lives

`$E2E_HOME`, default `~/e2e-runs`:

- `projects.json` — registry
- `engine/` — slot locks (`build.lock`, `sim-1..3.lock`, `project-*.lock`), `golden.json`
- `build/<project>/` — DerivedData, kept between runs
- `<project>/runs/<project>-<n>/` — `request.json`, `state.json`,
  `events.ndjson`, `flows/<suite>/<flow>/…`, `report.json`, `engine.log`
  (detached runs). Read-only once finished.
- `<project>/history.ndjson`, `quarantine.json`, `triage.ndjson`

Each new run trims earlier runs of the same project: videos (except failed
flows of the latest one), raw logs, Maestro output, xcresult bundles and
`live/` go; reports, JUnit and failure screenshots stay.

## Limits

| | default | cap |
|---|---|---|
| flow | 4 min | 10 min |
| suite | 25 min | 45 min |
| run | 90 min | 120 min |
| sims per project / machine | 2 / 3 | 2 / 3 |
| builds at once | 1 | 1 |
| retries | 1 | 2 |

Higher values in app.yml are clamped and logged as `limit_hit`. A run needs
12 GB free disk to start and 3 GB free RAM per new simulator. A flow that is
flaky in 3 runs is quarantined: it still runs, but no longer fails the run.

## Safety

The engine only creates, boots and deletes simulators named `e2e-*`, and
checks the name with simctl before every change. Golden simulators
(`e2e-golden-*`) are cloned, never booted. It never commits, pushes, uploads
or signs with a real identity. `stop`, Ctrl-C and SIGTERM all clean up: sims
deleted, slots released, report written.

## Tests

```sh
cd e2e && ~/.local/share/e2e/venv/bin/python -m unittest discover -s tests -t .
```

Everything runs on the fake backend or mocked subprocesses in a temp
`E2E_HOME`; no simulators or builds.
