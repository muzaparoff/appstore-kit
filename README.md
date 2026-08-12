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
  version attributes, en-US copy, name/subtitle/privacy URL, categories,
  build selection, 6.9" screenshots. Idempotent.
- `submit_for_review.py` — files the review submission.
- Config via env: `ASC_APP_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`,
  `ASC_KEY_PATH` (+ `META_DIR`, `SHOTS_DIR` for staging). Copy layout:
  fastlane `metadata/` + `screenshots/en-US/` dirs in the app repo.

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
