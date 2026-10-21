"""RAM and disk gauges for the runner Mac."""

import re
import shutil
import subprocess

from . import store


def ram_free_gb():
    """Memory a new simulator could get without swapping: free + inactive +
    speculative + purgeable pages (what Activity Monitor calls available)."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 16384
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        m = re.search(r"^%s:\s+(\d+)\." % key, out, re.M)
        if m:
            pages += int(m.group(1))
    return round(pages * page / 1e9, 1)


def ram_total_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=10).stdout
        return round(int(out.strip()) / 1e9, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def disk_free_gb(path=None):
    path = path or store.ensure_dir(store.home())
    return round(shutil.disk_usage(path).free / 1e9, 1)


def gauges():
    return {"ram_free_gb": ram_free_gb(), "ram_total_gb": ram_total_gb(),
            "disk_free_gb": disk_free_gb()}
