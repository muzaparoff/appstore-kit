#!/usr/bin/env python3
"""Config via env: ASC_APP_ID, ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH."""
"""Submits the staged Camipack App Store version for review.
Run ONLY after stage_listing.py and the two one-time questionnaires
(App Privacy, Age Rating) are done in App Store Connect.

Usage: venv/bin/python3 submit_for_review.py
"""
import json, os, pathlib, time, urllib.request, urllib.error

import jwt

APP_ID = os.environ["ASC_APP_ID"]
KEY_PATH = os.environ["ASC_KEY_PATH"]
ISSUER = os.environ["ASC_ISSUER_ID"]
KEY_ID = os.environ["ASC_KEY_ID"]
BASE = "https://api.appstoreconnect.apple.com/v1"
META_DIR = pathlib.Path(os.environ.get("META_DIR", "fastlane/metadata"))


def token():
    return jwt.encode(
        {"iss": ISSUER, "exp": int(time.time()) + 1200, "aud": "appstoreconnect-v1"},
        pathlib.Path(KEY_PATH).read_text(), algorithm="ES256", headers={"kid": KEY_ID})


def req(method, path, body=None):
    r = urllib.request.Request(
        BASE + path, method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}\n{e.read().decode()[:600]}")


def req_or_none(method, path):
    """Like req, but a missing related resource is None rather than an exit."""
    r = urllib.request.Request(BASE + path, method=method,
                               headers={"Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return (json.loads(data) if data else {}).get("data")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise SystemExit(f"{method} {path} -> HTTP {e.code}\n{e.read().decode()[:600]}")


def set_review_notes(vid):
    """App Review notes from META_DIR/review_information/notes.txt, if present.

    Apps without the file keep whatever notes App Store Connect already has.
    """
    path = META_DIR / "review_information" / "notes.txt"
    if not path.exists():
        return
    notes = path.read_text().strip()
    detail = req_or_none("GET", f"/appStoreVersions/{vid}/appStoreReviewDetail")
    if detail:
        req("PATCH", f"/appStoreReviewDetails/{detail['id']}", {"data": {
            "type": "appStoreReviewDetails", "id": detail["id"],
            "attributes": {"notes": notes}}})
    else:
        req("POST", "/appStoreReviewDetails", {"data": {
            "type": "appStoreReviewDetails", "attributes": {"notes": notes},
            "relationships": {"appStoreVersion": {"data": {
                "type": "appStoreVersions", "id": vid}}}}})
    print("review notes set")


def ready_products():
    """Subscriptions and in-app purchases waiting to go to review with a version.

    Apple requires an app's first subscription to be reviewed together with an
    app version; submitting the version alone leaves the product unsellable.
    Only products in READY_TO_SUBMIT are touched, so an app with none behaves
    exactly as before.
    """
    subs, stuck = [], []
    for group in req("GET", f"/apps/{APP_ID}/subscriptionGroups?limit=50").get("data", []):
        for item in req("GET", f"/subscriptionGroups/{group['id']}/subscriptions?limit=50").get("data", []):
            state = item["attributes"].get("state")
            print(f"   subscription {item['attributes'].get('productId')}: {state}")
            if state == "READY_TO_SUBMIT":
                subs.append(item)
            elif state not in ("APPROVED", "WAITING_FOR_REVIEW", "IN_REVIEW", "REMOVED_FROM_SALE"):
                stuck.append(f"{item['attributes'].get('productId')} ({state})")
    # A subscription that is neither sellable nor going to review with this
    # version would ship a paywall with nothing behind it: stop instead.
    if stuck:
        raise SystemExit("Not submitting: these subscriptions are not ready for review — "
                         + ", ".join(stuck) + ". Finish them in App Store Connect first.")
    # Apple answers this with HTTP 500 for some apps that have no in-app
    # purchases at all (seen on Camipack, which only sells subscriptions), so
    # a failure here means "none found" rather than stopping the submission.
    try:
        listed = req("GET", f"/apps/{APP_ID}/inAppPurchasesV2?limit=200").get("data", [])
    except SystemExit as failure:
        print(f"warning: could not list in-app purchases, assuming none\n{failure}")
        listed = []
    iaps = [i for i in listed if i["attributes"].get("state") == "READY_TO_SUBMIT"]
    return subs, iaps


def submit_products(subs, iaps):
    for sub in subs:
        req("POST", "/subscriptionSubmissions", {"data": {
            "type": "subscriptionSubmissions",
            "relationships": {"subscription": {"data": {"type": "subscriptions", "id": sub["id"]}}}}})
        print(f"submitted subscription {sub['attributes'].get('productId')}")
    for iap in iaps:
        req("POST", "/inAppPurchaseSubmissions", {"data": {
            "type": "inAppPurchaseSubmissions",
            "relationships": {"inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap["id"]}}}}})
        print(f"submitted in-app purchase {iap['attributes'].get('productId')}")


def report_products(subs, iaps):
    """What Apple now says about each product submitted with this version."""
    for sub in subs:
        state = req("GET", f"/subscriptions/{sub['id']}")["data"]["attributes"].get("state")
        print(f"   subscription {sub['attributes'].get('productId')}: {state}")
    for iap in iaps:
        state = req("GET", f"/inAppPurchasesV2/{iap['id']}")["data"]["attributes"].get("state")
        print(f"   in-app purchase {iap['attributes'].get('productId')}: {state}")


def screenshot_states(vid):
    """Every screenshot on this version, as (id, filename, delivery state).

    Walks localizations -> sets -> screenshots. A version with no screenshots
    at all yields nothing, which is a valid state: the caller may have run with
    an empty shots_dir.
    """
    out = []
    for loc in req("GET", f"/appStoreVersions/{vid}/appStoreVersionLocalizations").get("data", []):
        for s in req("GET", f"/appStoreVersionLocalizations/{loc['id']}/appScreenshotSets").get("data", []):
            for shot in req("GET", f"/appScreenshotSets/{s['id']}/appScreenshots").get("data", []):
                a = shot["attributes"]
                state = (a.get("assetDeliveryState") or {}).get("state")
                out.append((shot["id"], a.get("fileName", "?"), state))
    return out


def await_screenshots(vid, timeout=900, interval=15):
    """Block until Apple finishes ingesting every screenshot.

    Uploading a screenshot returns immediately, but Apple processes it
    asynchronously and refuses the submission with
    STATE_ERROR.SCREENSHOT_UPLOADS_IN_PROGRESS while any are still in flight.
    Staging and submitting run seconds apart, so that race lost every time.
    """
    deadline = time.time() + timeout
    while True:
        shots = screenshot_states(vid)
        failed = [(n, s) for _, n, s in shots if s == "FAILED"]
        if failed:
            raise SystemExit("Apple rejected these screenshots: " +
                             ", ".join(f"{n} ({s})" for n, s in failed))
        pending = [(n, s) for _, n, s in shots if s != "COMPLETE"]
        if not pending:
            print(f"screenshots ready ({len(shots)} complete)")
            return
        if time.time() >= deadline:
            raise SystemExit(
                f"screenshots still processing after {timeout}s: " +
                ", ".join(f"{n} ({s})" for n, s in pending[:5]))
        print(f"   waiting on {len(pending)}/{len(shots)} screenshot(s): "
              f"{pending[0][0]} is {pending[0][1]}")
        time.sleep(interval)


def main():
    versions = req("GET", f"/apps/{APP_ID}/appStoreVersions")["data"]
    # Guard: if a version is already with Apple, filing another submission is
    # at best a no-op and at worst cancels/queues confusingly. Skip cleanly.
    in_flight = [v for v in versions if v["attributes"]["appStoreState"] in
                 ("WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_APPLE_RELEASE",
                  "PENDING_DEVELOPER_RELEASE", "IN_REVIEW")]
    if in_flight:
        v = in_flight[0]["attributes"]
        print(f"SKIP: version {v['versionString']} is already {v['appStoreState']} — "
              "not submitting another. Re-run after Apple decides.")
        return
    editable = next(v for v in versions
                    if v["attributes"]["appStoreState"] in
                    ("PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED",
                     "METADATA_REJECTED"))
    vid, vstr = editable["id"], editable["attributes"]["versionString"]

    await_screenshots(vid)
    set_review_notes(vid)
    ready_subs, ready_iaps = ready_products()
    print(f"products to submit with {vstr}: " +
          (", ".join(p["attributes"].get("productId", "?")
                     for p in ready_subs + ready_iaps) or "none"))

    # Reuse an open submission if one exists, else create.
    subs = req("GET", f"/reviewSubmissions?filter[app]={APP_ID}&filter[state]=READY_FOR_REVIEW,WAITING_FOR_REVIEW,IN_REVIEW,UNRESOLVED_ISSUES")
    open_subs = subs.get("data", [])
    if open_subs:
        sub = open_subs[0]
        print(f"reusing open review submission {sub['id']} ({sub['attributes']['state']})")
    else:
        sub = req("POST", "/reviewSubmissions", {"data": {
            "type": "reviewSubmissions",
            "attributes": {"platform": "IOS"},
            "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}})["data"]
        print(f"created review submission {sub['id']}")

    items = req("GET", f"/reviewSubmissions/{sub['id']}/items").get("data", [])
    if not items:
        req("POST", "/reviewSubmissionItems", {"data": {
            "type": "reviewSubmissionItems",
            "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub["id"]}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": vid}}}}})
        print(f"added version {vstr} to the submission")

    # Products before the version is sent: if Apple refuses one, this exits
    # with the version still unsubmitted rather than shipping a paywall
    # with nothing behind it.
    submit_products(ready_subs, ready_iaps)

    req("PATCH", f"/reviewSubmissions/{sub['id']}", {"data": {
        "type": "reviewSubmissions", "id": sub["id"],
        "attributes": {"submitted": True}}})
    print(f"SUBMITTED {vstr} for App Review. Typical decision time: 24-48h.")
    if ready_subs or ready_iaps:
        report_products(ready_subs, ready_iaps)


if __name__ == "__main__":
    main()
