#!/usr/bin/env python3
"""Config via env: ASC_APP_ID, ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH, META_DIR, SHOTS_DIR."""
"""Stages the Camipack App Store listing via the App Store Connect API:
version metadata, localization copy, name/subtitle/privacy URL, categories,
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

    # 2. Version localization (en-US): description, keywords, promo, URLs, notes.
    locs = req("GET", f"/appStoreVersions/{vid}/appStoreVersionLocalizations")["data"]
    en = next((l for l in locs if l["attributes"]["locale"] == "en-US"), None)
    loc_attrs = {k: v for k, v in {
        "description": text("en-US/description.txt"),
        "keywords": text("en-US/keywords.txt"),
        "promotionalText": text("en-US/promotional_text.txt"),
        "supportUrl": text("en-US/support_url.txt"),
        "marketingUrl": text("en-US/marketing_url.txt"),
        "whatsNew": text("en-US/release_notes.txt"),
    }.items() if v is not None}
    if en:
        # whatsNew is rejected by the API for a first-ever version.
        first_release = len(versions) == 1
        attrs = {k: v for k, v in loc_attrs.items() if not (first_release and k == "whatsNew")}
        req("PATCH", f"/appStoreVersionLocalizations/{en['id']}", {"data": {
            "type": "appStoreVersionLocalizations", "id": en["id"], "attributes": attrs}})
        loc_id = en["id"]
    else:
        created = req("POST", "/appStoreVersionLocalizations", {"data": {
            "type": "appStoreVersionLocalizations",
            "attributes": {"locale": "en-US", **loc_attrs},
            "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": vid}}}}})
        loc_id = created["data"]["id"]
    print("2/6 en-US version localization set")

    # 3. App-level info: name, subtitle, privacy policy URL.
    infos = req("GET", f"/apps/{APP_ID}/appInfos")["data"]
    info = next(i for i in infos if i["attributes"]["appStoreState"] != "READY_FOR_SALE")
    info_locs = req("GET", f"/appInfos/{info['id']}/appInfoLocalizations")["data"]
    ien = next((l for l in info_locs if l["attributes"]["locale"] == "en-US"), None)
    info_attrs = {k: v for k, v in {
        "name": text("en-US/name.txt"),
        "subtitle": text("en-US/subtitle.txt"),
        "privacyPolicyUrl": text("en-US/privacy_url.txt")}.items() if v is not None}
    if not info_attrs:
        print("3/6 app info: nothing to set")
        info_attrs = None
    if info_attrs is None:
        pass
    elif ien:
        req("PATCH", f"/appInfoLocalizations/{ien['id']}", {"data": {
            "type": "appInfoLocalizations", "id": ien["id"], "attributes": info_attrs}})
    else:
        req("POST", "/appInfoLocalizations", {"data": {
            "type": "appInfoLocalizations",
            "attributes": {"locale": "en-US", **info_attrs},
            "relationships": {"appInfo": {"data": {"type": "appInfos", "id": info["id"]}}}}})
    print("3/6 name, subtitle, privacy URL set")

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
    framed = [p for p in pngs if p.stem.endswith("_framed")]
    if framed:
        # fastlane-frameit layout: prefer the framed marketing versions and
        # ignore their raw siblings, matching deliver's behavior.
        pngs = framed
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
