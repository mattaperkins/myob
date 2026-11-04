#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install a MYOB Uploader launcher in Applications and pin it to the Dock."""

from pathlib import Path
import plistlib
import secrets
import shlex
import shutil
import subprocess
import sys


BUNDLE_ID = "io.github.mattaperkins.myob-uploader"


def install_app():
    project = Path(__file__).resolve().parent
    app = Path.home() / "Applications" / "MYOB Uploader.app"
    contents = app / "Contents"
    info = contents / "Info.plist"
    if app.exists():
        if not info.is_file() or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != BUNDLE_ID:
            raise SystemExit(f"An unrelated app already exists at {app}.")

    executable = contents / "MacOS" / "MYOB Uploader"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources = contents / "Resources"
    resources.mkdir(exist_ok=True)
    shutil.copyfile(project / "assets" / "Uploader.icns", resources / "Uploader.icns")
    command = shlex.join([sys.executable, str(project / "myob_uploader.py")])
    executable.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    executable.chmod(0o755)
    info.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "MYOB Uploader",
        "CFBundleDisplayName": "MYOB Uploader",
        "CFBundleExecutable": executable.name,
        "CFBundleIconFile": "Uploader.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
    }))
    return app


def pin_to_dock(app):
    domain = "com.apple.dock"
    prefs = subprocess.check_output(["/usr/bin/defaults", "export", domain, "-"])
    url = app.as_uri() + "/"
    entries = plistlib.loads(prefs).get("persistent-apps", [])
    for entry in entries:
        tile = entry.get("tile-data", {})
        saved_url = tile.get("file-data", {}).get("_CFURLString", "")
        if tile.get("bundle-identifier") == BUNDLE_ID or saved_url.rstrip("/") == url.rstrip("/"):
            return
    tile = (f'{{ GUID = {secrets.randbits(32)}; '
            '"tile-type" = "file-tile"; "tile-data" = { '
            f'"bundle-identifier" = "{BUNDLE_ID}"; "file-label" = "MYOB Uploader"; "file-type" = 41; '
            f'"file-data" = {{ "_CFURLString" = "{url}"; "_CFURLStringType" = 15; }}; }}; }}')
    subprocess.run(["/usr/bin/defaults", "write", domain, "persistent-apps", "-array-add", tile],
                   check=True)
    subprocess.run(["/usr/bin/killall", "Dock"], check=True)


def main():
    if sys.platform != "darwin":
        raise SystemExit("The Dock installer requires macOS.")
    app = install_app()
    pin_to_dock(app)
    print(f"Installed and pinned to the Dock: {app}")


if __name__ == "__main__":
    main()
