"""xcodebuild build-for-testing into the project's kept DerivedData."""

import glob
import os
import plistlib

from . import proc, store


class BuildError(Exception):
    pass


def derived_data(project):
    return os.path.join(store.home(), "build", project)


def products_dir(project):
    return os.path.join(derived_data(project), "Build", "Products")


def has_cache(project):
    return bool(glob.glob(os.path.join(products_dir(project), "*.xctestrun")))


def command(app):
    cmd = ["xcodebuild", "build-for-testing"]
    if app.get("workspace"):
        cmd += ["-workspace", os.path.join(app["repo"], app["workspace"])]
    else:
        cmd += ["-project", os.path.join(app["repo"], app["xcodeproj"])]
    cmd += ["-scheme", app["scheme"], "-configuration", "Debug",
            "-destination", "generic/platform=iOS Simulator",
            "-derivedDataPath", derived_data(app["project"]),
            # Ad-hoc signing, as in bin/e2e-run.sh: Keychain works in the
            # simulator without a provisioning profile, and nothing is signed
            # with the owner's identity.
            "CODE_SIGN_STYLE=Manual", "CODE_SIGN_IDENTITY=-", "DEVELOPMENT_TEAM=",
            "PROVISIONING_PROFILE_SPECIFIER="]
    return cmd


def find_app(project, bundle_id):
    for path in sorted(glob.glob(os.path.join(products_dir(project), "Debug-iphonesimulator", "*.app"))):
        try:
            with open(os.path.join(path, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if info.get("CFBundleIdentifier") == bundle_id:
            return path
    return None


def find_xctestrun(project):
    runs = glob.glob(os.path.join(products_dir(project), "*.xctestrun"))
    return max(runs, key=os.path.getmtime) if runs else None


def build(app, log_path, timeout, on_line=None):
    r = proc.run(command(app), timeout=timeout, log_path=log_path, cwd=app["repo"],
                 on_line=on_line, keep_out=False)
    if r.timed_out:
        raise BuildError("build timed out after %ds" % timeout)
    if r.code != 0:
        tail = ""
        try:
            with open(log_path, errors="replace") as f:
                tail = "".join(f.readlines()[-40:])
        except OSError:
            pass
        raise BuildError("xcodebuild failed (%s)\n%s" % (r.code, tail))
    app_path = find_app(app["project"], app["bundle_id"])
    if not app_path:
        raise BuildError("no .app with bundle id %s in %s" % (app["bundle_id"], products_dir(app["project"])))
    return {"app": app_path, "xctestrun": find_xctestrun(app["project"])}
