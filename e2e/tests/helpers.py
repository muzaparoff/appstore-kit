import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

E2E_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_YML = """\
project: {project}
xcodeproj: Demo.xcodeproj
scheme: Demo
bundle_id: com.example.{project}
device: iPhone 17 Pro
runtime: latest
seams:
  reset: [-e2e.reset]
  seed: [-seedTrip, "{{name}}"]
permissions: [photos, location]
suites:
  smoke: {{engine: maestro, flows: e2e/flows/smoke, budget: 5m}}
  trips: {{engine: maestro, flows: e2e/flows/trips, budget: 12m}}
  ui:    {{engine: xcuitest, tests: [DemoUITests/PaywallTests], budget: 8m}}
{extra}
"""


class HomeCase(unittest.TestCase):
    """Fresh E2E_HOME and app repos per test; fast fake backend."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-test-")
        self.home = os.path.join(self.tmp, "home")
        self._env = dict(os.environ)
        os.environ["E2E_HOME"] = self.home
        os.environ["E2E_FAKE_STEP_S"] = "0.05"
        os.environ["E2E_FAKE_BUILD_S"] = "0.2"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        for root, dirs, _ in os.walk(self.tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_repo(self, project="demo", flows=None, extra=""):
        repo = os.path.join(self.tmp, "repo-" + project)
        flows = flows or {"smoke": ["01-launch"], "trips": ["01-create", "02-fail-edit", "03-flaky-share"]}
        for suite, names in flows.items():
            d = os.path.join(repo, "e2e", "flows", suite)
            os.makedirs(d, exist_ok=True)
            for n in names:
                with open(os.path.join(d, n + ".yaml"), "w") as f:
                    f.write("appId: x\n---\n- launchApp\n")
        os.makedirs(os.path.join(repo, "e2e"), exist_ok=True)
        with open(os.path.join(repo, "e2e", "app.yml"), "w") as f:
            f.write(APP_YML.format(project=project, extra=extra))
        return repo

    def cli(self, *args, check=True, env=None):
        e = dict(os.environ)
        e["PYTHONPATH"] = E2E_DIR
        e.update(env or {})
        r = subprocess.run([sys.executable, "-m", "e2ekit"] + list(args), capture_output=True,
                           text=True, env=e, timeout=120)
        if check and r.returncode not in (0, 1):
            raise AssertionError("cli %s: %s %s" % (args, r.returncode, r.stderr))
        return r

    def cli_json(self, *args, **kw):
        r = self.cli(*(list(args) + ["--json"]), **kw)
        return r.returncode, json.loads(r.stdout)

    def run_path(self, run_id, *parts):
        project = run_id.rpartition("-")[0]
        return os.path.join(self.home, project, "runs", run_id, *parts)

    def wait_terminal(self, run_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.run_path(run_id, "report.json")):
                with open(self.run_path(run_id, "state.json")) as f:
                    st = json.load(f)
                if st["status"] in ("passed", "failed", "stopped", "error"):
                    return st
            time.sleep(0.1)
        raise AssertionError("run %s did not finish" % run_id)

    def events(self, run_id):
        with open(self.run_path(run_id, "events.ndjson")) as f:
            return [json.loads(l) for l in f if l.strip()]
