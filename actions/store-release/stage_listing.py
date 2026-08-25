#!/usr/bin/env python3
"""Config via env: ASC_APP_ID, ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH, META_DIR, SHOTS_DIR."""
"""Stages the Camipack App Store listing via the App Store Connect API:
version metadata, localization copy for every locale directory under
META_DIR, name/subtitle/privacy URL, categories,
build selection, and the 6.9" screenshot set. Idempotent — safe to re-run.

Leaves untouched (no public API / one-time human steps): the App Privacy
questionnaire and the Age Rating questionnaire. Submission itself is a
separate explicit step.

Usage: venv/bin/python3 stage_listing.py <version> <build_number>
"""
import hashlib, json, os, pathlib, sys, time, urllib.request, urllib.error

import jwt

APP_ID = os.environ["ASC_APP_ID"]
KEY_PATH = os.environ["ASC_KEY_PATH"]
ISSUER = os.environ["ASC_ISSUER_ID"]
KEY_ID = os.environ["ASC_KEY_ID"]
META = pathlib.Path(os.environ.get("META_DIR", "fastlane/metadata"))
SHOTS = pathlib.Path(os.environ.get("SHOTS_DIR", "fastlane/screenshots/en-US"))
BASE = "https://api.appstoreconnect.apple.com/v1"


def token():
    return jwt.encode(
        {"iss": ISSUER, "exp": int(time.time()) + 1200, "aud": "appstoreconnect-v1"},
        pathlib.Path(KEY_PATH).read_text(), algorithm="ES256", headers={"kid": KEY_ID})


def req(method, path, body=None, raw_url=None):
    r = urllib.request.Request(
        raw_url or BASE + path, method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise SystemExit(f"{method} {path} -> HTTP {e.code}\n{detail}")


def put_chunk(url, headers, data):
    r = urllib.request.Request(url, method="PUT", data=data)
    for h in headers:
        r.add_header(h["name"], h["value"])
    urllib.request.urlopen(r).read()


def text(p):
    """Missing metadata file -> None: the field keeps its current ASC value,
    matching fastlane deliver's behavior."""
    f = META / p
    return f.read_text().strip() if f.exists() else None


PRIMARY = os.environ.get("PRIMARY_LOCALE", "en-US")

# App Store Connect rejects an unknown locale with a 409 halfway through the
# loop, having already written the ones before it. Checking names here turns a
# typo'd directory into a local warning instead of a half-updated listing.
ASC_LOCALES = {
    "ar-SA", "ca", "cs", "da", "de-DE", "el", "en-AU", "en-CA", "en-GB", "en-US",
    "es-ES", "es-MX", "fi", "fr-CA", "fr-FR", "he", "hi", "hr", "hu", "id",
    "it", "ja", "ko", "ms", "nl-NL", "no", "pl", "pt-BR", "pt-PT", "ro", "ru",
    "sk", "sv", "th", "tr", "uk", "vi", "zh-Hans", "zh-Hant",
}

# Apple's caps. Exceeding one is a 409 at write time, which is a bad place to
# find out — every locale is checked before anything is sent.
LIMITS = {
    "name.txt": 30, "subtitle.txt": 30, "keywords.txt": 100,
    "promotional_text.txt": 170, "description.txt": 4000, "release_notes.txt": 4000,
}


def discover_locales():
    """Locale directories under META, primary first.

    Only directories count, so copyright.txt and the category files at the root
    are excluded without naming them. A sibling directory such as
    fastlane/metadata-pending/ sits outside META entirely and is therefore
    excluded structurally — that is load-bearing, since it is how deliberately
    unpublished listings are parked.
    """
    found, skipped = [], []
    for entry in sorted(META.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in ("review_information", "trade_representative_contact_information"):
            continue
        (found if entry.name in ASC_LOCALES else skipped).append(entry.name)
    for name in skipped:
        print(f"  ! skipping '{name}': not an App Store Connect locale")
    if PRIMARY in found:
        found.remove(PRIMARY)
        found.insert(0, PRIMARY)
    return found


def validate(locales):
    """Every length checked before the first write.

    Without this an over-long en-AU subtitle fails after en-US and en-GB have
    already been sent, leaving the listing partly updated and the run red.
    """
    problems = []
    for locale in locales:
        for filename, cap in LIMITS.items():
            value = text(f"{locale}/{filename}")
            if value is not None and len(value) > cap:
                problems.append(f"{locale}/{filename}: {len(value)}/{cap}")
    if problems:
        raise SystemExit("metadata too long:\n  " + "\n  ".join(problems))


def main():
    version_string, build_number = sys.argv[1], sys.argv[2]

    # 1. The editable version: attributes + copy.
    versions = req("GET", f"/apps/{APP_ID}/appStoreVersions")["data"]
    editable = [v for v in versions
                if v["attributes"]["appStoreState"] in
                ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED")]
    if editable:
        vid = editable[0]["id"]
    else:
        # An app whose previous version shipped has no editable version yet —
        # create the next one.
        created = req("POST", "/appStoreVersions", {"data": {
            "type": "appStoreVersions",
            "attributes": {"platform": "IOS", "versionString": version_string},
            "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}})
        vid = created["data"]["id"]
        print(f"created new store version {version_string}")
    version_attrs = {"versionString": version_string, "releaseType": "AFTER_APPROVAL"}
    if text("copyright.txt"):
        version_attrs["copyright"] = text("copyright.txt")
    req("PATCH", f"/appStoreVersions/{vid}", {"data": {
        "type": "appStoreVersions", "id": vid, "attributes": version_attrs}})
    print(f"1/6 version {version_string} attributes set ({vid})")

    # 2. Version localizations: description, keywords, promo, URLs, notes —
    #    for every locale directory, not only the primary one. Each storefront
    #    localization carries its own keyword field, which is the whole reason
    #    en-GB / en-AU / en-CA exist alongside en-US.
    locales = discover_locales()
    validate(locales)
    locs = req("GET", f"/appStoreVersions/{vid}/appStoreVersionLocalizations")["data"]
    by_locale = {l["attributes"]["locale"]: l for l in locs}
    # Computed once, from the pre-loop fetch: if step 1 created the version,
    # `versions` is the stale list from before it existed, so a genuinely first
    # release still evaluates true here. Every locale must agree on this.
    first_release = len(versions) == 1
    written = {}

    for locale in locales:
        loc_attrs = {k: v for k, v in {
            "description": text(f"{locale}/description.txt"),
            "keywords": text(f"{locale}/keywords.txt"),
            "promotionalText": text(f"{locale}/promotional_text.txt"),
            "supportUrl": text(f"{locale}/support_url.txt"),
            "marketingUrl": text(f"{locale}/marketing_url.txt"),
            "whatsNew": text(f"{locale}/release_notes.txt"),
        }.items() if v is not None}
        # whatsNew is rejected by the API for a first-ever version.
        attrs = {k: v for k, v in loc_attrs.items()
                 if not (first_release and k == "whatsNew")}
        existing = by_locale.get(locale)
        if existing:
            req("PATCH", f"/appStoreVersionLocalizations/{existing['id']}", {"data": {
                "type": "appStoreVersionLocalizations", "id": existing["id"],
                "attributes": attrs}})
            written[locale] = existing["id"]
        elif attrs:
            created = req("POST", "/appStoreVersionLocalizations", {"data": {
                "type": "appStoreVersionLocalizations",
                "attributes": {"locale": locale, **attrs},
                "relationships": {"appStoreVersion":
                                  {"data": {"type": "appStoreVersions", "id": vid}}}}})
            written[locale] = created["data"]["id"]
        else:
            print(f"  - {locale}: no metadata files, nothing to create")
            continue
        print(f"  · {locale} version localization set")

    # Screenshots belong to one localization and SHOTS_DIR is the primary's, so
    # this must be the primary id — resolved after the loop, because the
    # localization may not have existed until this run created it.
    loc_id = written.get(PRIMARY) or (by_locale.get(PRIMARY) or {}).get("id")
    print(f"2/6 version localizations set ({len(written)}: {', '.join(written) or 'none'})")

    # 3. App-level info: name, subtitle, privacy policy URL.
    infos = req("GET", f"/apps/{APP_ID}/appInfos")["data"]
    info = next(i for i in infos if i["attributes"]["appStoreState"] != "READY_FOR_SALE")
    info_locs = req("GET", f"/appInfos/{info['id']}/appInfoLocalizations")["data"]
    info_by_locale = {l["attributes"]["locale"]: l for l in info_locs}
    named = []
    for locale in locales:
        info_attrs = {k: v for k, v in {
            "name": text(f"{locale}/name.txt"),
            "subtitle": text(f"{locale}/subtitle.txt"),
            "privacyPolicyUrl": text(f"{locale}/privacy_url.txt")}.items() if v is not None}
        if not info_attrs:
            continue
        existing = info_by_locale.get(locale)
        if existing:
            req("PATCH", f"/appInfoLocalizations/{existing['id']}", {"data": {
                "type": "appInfoLocalizations", "id": existing["id"],
                "attributes": info_attrs}})
        else:
            req("POST", "/appInfoLocalizations", {"data": {
                "type": "appInfoLocalizations",
                "attributes": {"locale": locale, **info_attrs},
                "relationships": {"appInfo":
                                  {"data": {"type": "appInfos", "id": info["id"]}}}}})
        named.append(locale)
    print(f"3/6 name, subtitle, privacy URL set ({len(named)}: {', '.join(named) or 'none'})")

    # 4. Categories (from metadata files; missing files keep current values).
    primary = text("primary_category.txt")
    secondary = text("secondary_category.txt")
    rels = {}
    if primary:
        rels["primaryCategory"] = {"data": {"type": "appCategories", "id": primary}}
    if secondary:
        rels["secondaryCategory"] = {"data": {"type": "appCategories", "id": secondary}}
    if rels:
        req("PATCH", f"/appInfos/{info['id']}", {"data": {
            "type": "appInfos", "id": info["id"], "relationships": rels}})
        print(f"4/6 categories set: {primary}/{secondary}")
    else:
        print("4/6 categories: keeping current values")

    # 5. Attach the build.
    builds = req("GET", f"/builds?filter[app]={APP_ID}&filter[version]={build_number}")["data"]
    if not builds:
        raise SystemExit(f"Build {build_number} not found/processed yet.")
    req("PATCH", f"/appStoreVersions/{vid}/relationships/build",
        {"data": {"type": "builds", "id": builds[0]["id"]}})
    print(f"5/6 build {build_number} attached")

    # 6. Screenshots: replace the 6.9" set — skipped when no dir is provided
    #    (keeps the previously uploaded set).
    if not os.environ.get("SHOTS_DIR") or not SHOTS.is_dir():
        print("6/6 screenshots skipped (no SHOTS_DIR)")
        print("\nStaging complete.")
        return
    # Without this the screenshots would be posted against a null id, or worse
    # against whichever localization happened to come back first — the primary
    # is the only one SHOTS_DIR describes.
    if not loc_id:
        raise SystemExit(f"no '{PRIMARY}' localization to attach screenshots to; "
                         f"expected metadata under {META / PRIMARY}")
    sets = req("GET", f"/appStoreVersionLocalizations/{loc_id}/appScreenshotSets")["data"]
    target = next((s for s in sets
                   if s["attributes"]["screenshotDisplayType"] == "APP_IPHONE_67"), None)
    if target is None:
        target = req("POST", "/appScreenshotSets", {"data": {
            "type": "appScreenshotSets",
            "attributes": {"screenshotDisplayType": "APP_IPHONE_67"},
            "relationships": {"appStoreVersionLocalization":
                              {"data": {"type": "appStoreVersionLocalizations", "id": loc_id}}}}})["data"]
    existing = req("GET", f"/appScreenshotSets/{target['id']}/appScreenshots")["data"]
    for shot in existing:
        req("DELETE", f"/appScreenshots/{shot['id']}")
    pngs = sorted(SHOTS.glob("*.png"))
    # fastlane-frameit layout: a *_framed.png replaces its raw sibling;
    # files without a framed counterpart (e.g. a hero composite) stay.
    stems = {p.stem for p in pngs}
    pngs = [p for p in pngs
            if p.stem.endswith("_framed") or f"{p.stem}_framed" not in stems]
    order = []
    for png in pngs:
        blob = png.read_bytes()
        shot = req("POST", "/appScreenshots", {"data": {
            "type": "appScreenshots",
            "attributes": {"fileName": png.name, "fileSize": len(blob)},
            "relationships": {"appScreenshotSet":
                              {"data": {"type": "appScreenshotSets", "id": target["id"]}}}}})["data"]
        for op in shot["attributes"]["uploadOperations"]:
            put_chunk(op["url"], op["requestHeaders"],
                      blob[op["offset"]:op["offset"] + op["length"]])
        req("PATCH", f"/appScreenshots/{shot['id']}", {"data": {
            "type": "appScreenshots", "id": shot["id"],
            "attributes": {"uploaded": True,
                           "sourceFileChecksum": hashlib.md5(blob).hexdigest()}}})
        order.append(shot["id"])
        print(f"   uploaded {png.name}")
    req("PATCH", f"/appScreenshotSets/{target['id']}/relationships/appScreenshots",
        {"data": [{"type": "appScreenshots", "id": i} for i in order]})
    print("6/6 screenshot set uploaded and ordered")
    print("\nStaging complete. Remaining human steps: App Privacy + Age Rating questionnaires.")


if __name__ == "__main__":
    main()
