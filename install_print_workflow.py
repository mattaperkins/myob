#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install Upload to MYOB in the current user's macOS Print > PDF menu."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def install():
    """Ensure the workflow exists. Return True if it was just installed."""
    if sys.platform != "darwin":
        raise SystemExit("This print workflow requires macOS.")

    uploader = Path(__file__).resolve().with_name("myob_uploader.py")
    folder = Path.home() / "Library" / "PDF Services"
    target = folder / "Upload to MYOB.app"
    if target.exists():
        return False
    folder.mkdir(parents=True, exist_ok=True)

    # The Print menu passes PDF files to the app via an AppleScript open event.
    script = f"""
on open pdfFiles
    set launchCommand to quoted form of {json.dumps(sys.executable, ensure_ascii=False)} & " " & quoted form of {json.dumps(str(uploader), ensure_ascii=False)} & " --printed-pdf"
    repeat with pdfFile in pdfFiles
        set launchCommand to launchCommand & " " & quoted form of (POSIX path of pdfFile)
    end repeat
    with timeout of 86400 seconds
        do shell script launchCommand
    end timeout
end open
"""
    with tempfile.TemporaryDirectory(dir=folder) as scratch:
        app = Path(scratch) / target.name
        subprocess.run(["/usr/bin/osacompile", "-o", str(app), "-e", script], check=True)
        try:
            app.rename(target)
        except OSError:
            if target.exists():
                return False
            raise
    return True


if __name__ == "__main__":
    if install():
        print("Installed Print > PDF > Upload to MYOB.")
    else:
        print("Print > PDF > Upload to MYOB is already installed.")
