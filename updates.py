# SPDX-License-Identifier: GPL-3.0-or-later
"""Check and install tagged MYOB tools releases from the official repository."""

import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from version import VERSION


PROJECT = Path(__file__).resolve().parent
REPOSITORY = "https://github.com/mattaperkins/myob.git"
LATEST_RELEASE = "https://api.github.com/repos/mattaperkins/myob/releases/latest"


def version_parts(tag):
    if re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        return tuple(map(int, tag[1:].split(".")))
    return None


def check_for_update():
    request = urllib.request.Request(LATEST_RELEASE, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # GitHub has no published release yet.
        raise
    tag = release.get("tag_name", "")
    available = version_parts(tag)
    if available and available > version_parts(f"v{VERSION}"):
        return tag
    return None


def git(*args):
    result = subprocess.run(["git", "-C", str(PROJECT), *args], capture_output=True,
                            text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git could not complete the update.")
    return result.stdout.strip()


def install_update(tag):
    if version_parts(tag) is None or version_parts(tag) <= version_parts(f"v{VERSION}"):
        raise ValueError("This is not a newer release.")
    if Path(git("rev-parse", "--show-toplevel")).resolve() != PROJECT:
        raise RuntimeError("Install a Git clone of the MYOB tools to apply updates from the app.")
    if git("branch", "--show-current") != "main" or git("status", "--porcelain"):
        raise RuntimeError("Switch to main and save or commit local changes before updating.")
    git("fetch", "--no-tags", REPOSITORY, f"refs/tags/{tag}")
    commit = git("rev-parse", "FETCH_HEAD^{commit}")
    git("merge", "--ff-only", "--no-overwrite-ignore", commit)
