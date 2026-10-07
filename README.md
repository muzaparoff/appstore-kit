# appstore-kit

Shared iOS release machinery for all of muzaparoff's App Store apps
(camipack, BabyLogly, shuttlx, HomePilot). One implementation of
CI/versioning/signing/TestFlight/App Store, consumed by each app repo as
reusable workflows — apps keep only thin callers, their metadata, and
their screenshots.

Everything here was extracted from camipack's pipeline, the newest and most
battle-tested of the four; the comments in the workflows carry the incident
history that shaped them (keychain search-list destruction, altool's
exit-0-on-fatal-error, tag pushes that trigger nothing, and friends).

## Reusable workflows (`.github/workflows/`)

| Workflow | What it does | Key inputs |
|---|---|---|
| `ci.yml` | Build + test (zero-warning gate), then auto-semver tag from conventional commits and dispatch the release | `project`, `scheme`, `release_workflow`, `site_notes_workflow` |
| `release.yml` | Archive, sign (non-destructive temp keychain), export, validate+upload to TestFlight, dispatch attach | `project`, `scheme`, `app_name`, `export_plist`, `attach_workflow`, `dry_run` |
| `testflight-status.yml` | Full TestFlight state: builds, processing, icon, groups, testers, visibility | `bundle_id` |
| `testflight-attach.yml` | Wait for VALID, answer export compliance, attach to every beta group | `bundle_id`, `build_number` |
| `testflight-resend-invite.yml` | Issue a fresh tester invitation | `bundle_id`, `email` |
| `site-release-notes.yml` | Extract feat/fix notes for a tag and push `docs/releases.json` to the app's public site repo | `tag`, `site_repo` |

## Scripts (`scripts/`)

- `stage_listing.py` — stages the full App Store listing via the ASC API:
  version attributes, copy for every locale directory, name/subtitle/privacy URL, categories,
  build selection, 6.9" screenshots. Idempotent.
- `submit_for_review.py` — files the review submission.
- Config via env: `ASC_APP_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`,
  `ASC_KEY_PATH` (+ `META_DIR`, `SHOTS_DIR` for staging). Copy layout:
  fastlane `metadata/` + `screenshots/en-US/` dirs in the app repo
  (`SHOTS_ROOT` = `screenshots/`: one set per locale folder).

## Consuming (caller repo)

```yaml
# .github/workflows/ci.yml in the app repo
name: 🧪 CI
on:
  push: { branches: [main, 'feature/**'], paths-ignore: ['**/*.md', 'docs/**'] }
  pull_request: { branches: [main] }
  workflow_dispatch:
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}
jobs:
  ci:
    uses: muzaparoff/appstore-kit/.github/workflows/ci.yml@v1
    with:
      project: Camipack.xcodeproj
      scheme: Camipack
      site_notes_workflow: site-release-notes.yml
    secrets: inherit
```

### Required secrets in each app repo (standard names)

`APPLE_TEAM_ID`, `IOS_CERTIFICATE_BASE64`, `KEY` (p12 password),
`KEYCHAIN_PASSWORD`, `IOS_PROVISIONING_PROFILE_BASE64`,
`WIDGET_PROVISIONING_PROFILE_BASE64` (optional),
`APP_STORE_CONNECT_API_KEY_ID`, `ISSUER_ID`,
`APP_STORE_CONNECT_API_KEY_BASE64`, `SITE_DEPLOY_KEY` (optional).

### Requirements

- Self-hosted macOS runner registered in the app repo (labels
  `self-hosted, macos`), Xcode 26+.
- Repo setting: this kit's Actions access is set to "repositories owned by
  muzaparoff", so private-to-private reuse works.
- Version the kit by tag; apps pin `@v1`. Breaking changes bump the tag.

## E2E (Maestro UI tests)

Available from `v1.3`. Opt in by adding `e2e_flows` to your `ci.yml` caller.
Apps that don't set it build, test, and tag exactly as before — no change needed.

```yaml
jobs:
  ci:
    uses: muzaparoff/appstore-kit/.github/workflows/ci.yml@v1.3
    with:
      project: MyApp.xcodeproj
      scheme: MyApp
      # --- E2E opt-in (all optional except e2e_flows) ---
      e2e_flows: e2e/                   # folder of Maestro YAML flows
      e2e_app_name: MyApp               # product name of the .app bundle
      e2e_setup: scripts/e2e-setup.sh  # prints KEY=VALUE env lines to stdout
      # e2e_simulator: "iPhone 17 Pro" # default
      # e2e_env: |                      # static KEY=VALUE pairs for the flows
      #   SOME_VAR=value
      # e2e_pass_env: "VAR1 VAR2"      # secret names forwarded as maestro -e
    secrets: inherit
```

The e2e job runs **on pushes to `main` only**, after `test` and before `tag`.
A failed e2e blocks tagging and therefore TestFlight.

### How it works

`bin/e2e-run.sh` is the single source of truth — it bootstraps Java 17 and
Maestro 2.11.0, runs your `e2e_setup` script (which may deploy a staging
backend and print env vars), builds the app in Debug for the simulator, boots
a fresh named simulator, runs `maestro test`, and shuts the simulator down on
exit. The same script runs locally via `/app-verify`.

### Optional E2E secrets

All four secrets are optional. Add whichever your setup script needs:

| Secret | Used for |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Deploying a staging Cloudflare Worker; optional — self-hosted runners can use the runner user's `wrangler login` instead |
| `E2E_TEST_SECRET` | Authenticating staging-only test endpoints |
| `E2E_USER_A_PASSWORD` | Fixed UI-driver test account password |
| `E2E_USER_B_PASSWORD` | Fixed API-driven test account password |

#### Cloudflare / wrangler in setup scripts

`CLOUDFLARE_API_TOKEN` is optional because the self-hosted runner runs as the
macOS user who has a persistent `wrangler login` session.

The kit handles both cases automatically in the "Run flows" step:

- **Token set:** the env var is present; wrangler picks it up automatically.
- **Token not set:** the kit unsets the env var (an empty string would make
  wrangler attempt token auth and fail) and, when a setup script is
  configured, runs `npx --yes wrangler whoami` before the script starts. If
  the OAuth session is missing the step fails with:
  `wrangler is not logged in on the runner — run npx wrangler login on the Mac mini, or set CLOUDFLARE_API_TOKEN`

Setup scripts do not need their own guard.

### Requirements

- Self-hosted macOS runner (Xcode 26+, Homebrew). Java 17 and Maestro are
  installed automatically on first run.
