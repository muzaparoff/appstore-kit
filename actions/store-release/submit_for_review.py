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

    req("PATCH", f"/reviewSubmissions/{sub['id']}", {"data": {
        "type": "reviewSubmissions", "id": sub["id"],
        "attributes": {"submitted": True}}})
    print(f"SUBMITTED {vstr} for App Review. Typical decision time: 24-48h.")


if __name__ == "__main__":
    main()
