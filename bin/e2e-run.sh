#!/usr/bin/env bash
# Runs an app's Maestro flows on a fresh iOS simulator. Used by e2e.yml in CI and by /app-verify locally.
set -euo pipefail

: "${PROJECT:?PROJECT is required}" "${SCHEME:?SCHEME is required}" "${APP_NAME:?APP_NAME is required}"
FLOWS="${FLOWS:-e2e/}"
SIMULATOR="${SIMULATOR:-iPhone 17 Pro}"
MAESTRO_VERSION="${MAESTRO_VERSION:-2.11.0}"
OUT_DIR="${OUT_DIR:-build/e2e}"
PASS_ENV="${PASS_ENV:-}"
SETUP="${SETUP:-}"

log() { echo "▶ $*" >&2; }
mkdir -p "$OUT_DIR"

# Maestro needs Java 17+.
JDK="$(brew --prefix openjdk@17 2>/dev/null || true)/libexec/openjdk.jdk/Contents/Home"
if [ ! -x "$JDK/bin/java" ]; then
  log "installing openjdk@17"
  brew install openjdk@17 >&2
  JDK="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
fi
export JAVA_HOME="$JDK" PATH="$JDK/bin:$HOME/.maestro/bin:$PATH"

if [ "$(maestro --version 2>/dev/null | tail -1)" != "$MAESTRO_VERSION" ]; then
  log "installing Maestro $MAESTRO_VERSION"
  curl -Ls https://get.maestro.mobile.dev | MAESTRO_VERSION="$MAESTRO_VERSION" bash >&2
fi

# App-specific preparation (staging deploy, data reset, …). Its KEY=VALUE stdout lines become env.
if [ -n "$SETUP" ]; then
  log "setup: $SETUP"
  bash "$SETUP" > "$OUT_DIR/setup.env"
  while IFS= read -r kv; do
    if [[ "$kv" =~ ^[A-Z_][A-Z0-9_]*= ]]; then export "${kv?}"; fi
  done < "$OUT_DIR/setup.env"
fi

log "building $SCHEME (Debug, simulator)"
# Ad-hoc signing ("-") so Keychain works in the simulator without a provisioning profile.
if ! xcodebuild build -project "$PROJECT" -scheme "$SCHEME" -configuration Debug \
    -destination "generic/platform=iOS Simulator" -derivedDataPath "$OUT_DIR/dd" \
    CODE_SIGN_STYLE=Manual CODE_SIGN_IDENTITY=- DEVELOPMENT_TEAM= PROVISIONING_PROFILE_SPECIFIER= \
    > "$OUT_DIR/build.log" 2>&1; then
  tail -40 "$OUT_DIR/build.log" >&2
  exit 1
fi
APP="$OUT_DIR/dd/Build/Products/Debug-iphonesimulator/$APP_NAME.app"
[ -d "$APP" ] || { log "no app at $APP"; exit 1; }

UDID="$(xcrun simctl create "e2e-${GITHUB_RUN_ID:-local}-$$" "$SIMULATOR")"
cleanup() {
  xcrun simctl shutdown "$UDID" >/dev/null 2>&1 || true
  xcrun simctl delete "$UDID" >/dev/null 2>&1 || true
}
trap cleanup EXIT
xcrun simctl boot "$UDID"
xcrun simctl bootstatus "$UDID" -b >/dev/null
xcrun simctl install "$UDID" "$APP"

ARGS=()
for name in $PASS_ENV; do ARGS+=(-e "$name=${!name:-}"); done

log "maestro test $FLOWS on $SIMULATOR ($UDID)"
maestro --device "$UDID" test ${ARGS[@]+"${ARGS[@]}"} \
  --format junit --output "$OUT_DIR/report.xml" --test-output-dir "$OUT_DIR/artifacts" "$FLOWS"
