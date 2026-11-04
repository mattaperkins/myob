# MYOB tools

This directory hosts tools for working with MYOB. Add each tool and its usage here as the project grows.

## Getting started

Use Python 3.10 or later with Tkinter for the desktop app. On macOS, clone the repository and run the installer.

```sh
git clone https://github.com/mattaperkins/myob.git
cd myob
python3 install_macos_app.py
```

The installer creates a private Python environment in `.venv/`, installs the dependencies from `requirements.txt`, and adds the app to the Dock. Open **MYOB Uploader** from the Dock. For command-line use, run `.venv/bin/python myob_uploader.py --help`.

On first launch, open **Settings…** and add your company's unique MYOB Uploads address, an SMTP2GO verified sender, and your SMTP2GO API key. Local settings and upload history live in the ignored `data/` directory. Keep credentials and invoice files outside the repository.

## Install on the macOS Dock

Run `python3 install_macos_app.py` from the project directory. The installer creates **MYOB Uploader.app** in your personal Applications folder and pins it to the Dock. You can run the installer again after moving the project or changing Python environments. Keep the project directory and Python environment available because the app launches the uploader from there. The Dock launcher also installs dependencies when the requirements file changes after an update.

To uninstall, remove **MYOB Uploader** from the Dock and delete `~/Applications/MYOB Uploader.app`.

## Supplier document uploader

`myob_uploader.py` emails supplier invoices and other documents to a company's MYOB Uploads address through SMTP2GO.

- Run `python3 myob_uploader.py` to open the desktop interface (requires Tkinter).
- To enable drag and drop, install `tkinterdnd2` with `python3 -m pip install tkinterdnd2` using the same Python that runs the app. Drop invoice files from Finder into the document box to add them to the selection. Choose **Upload to MYOB…** when ready.
- Run `python3 myob_uploader.py send --help` for command-line options.
- Preview a batch with `python3 myob_uploader.py send invoice.pdf --sender sender@example.com --dry-run`.
- Open **Settings…** to add, edit, rename or remove companies and manage email addresses verified in SMTP2GO. Select the company and From address in the main window before uploading. Set the API key under **Settings… → SMTP2GO**, or set `SMTP2GO_API_KEY` for an environment override. Settings stores the key in the private `data/smtp2go.key` file. Leave the masked field blank to keep the current key.
- PDF uploads need only the Python standard library. For PNG, JPEG and TIFF uploads, install Pillow with `python3 -m pip install Pillow`.

### Invoice archive

The desktop and command-line uploader save one PDF per document accepted by SMTP2GO. The uploader creates `~/Documents/myob/MM-YYYY/` as needed, using the local upload month and year (for example, `11-2026`). Each filename includes the original name stem and a unique suffix to avoid overwrites. Existing PDFs are copied byte for byte. Images are converted for the archive, including all pages of a multipage TIFF. MYOB still receives the original attachment.

Dry runs, previews, skipped duplicates and unconfirmed sends do not produce a completed archive entry. The archive starts with new sends. SMTP2GO acceptance does not confirm MYOB imported the document. Use `python3 myob_uploader.py history` to see recorded send status. For an uncertain send, check MYOB and SMTP2GO activity before using `python3 myob_uploader.py resolve --help` to record the outcome.

### Delivery status

After sending and archiving, the uploader polls SMTP2GO Activity Search for each email ID. It reports delivery to the MYOB mail server, a hard bounce or rejection, or pending delivery. A soft bounce can still be retried by SMTP2GO. Mail delivery and document import are separate, so confirm import in MYOB Uploads.

Use `python3 myob_uploader.py history` to find the history ID and last recorded status, then run `python3 myob_uploader.py delivery ID` to check again without resending. The API key needs access to SMTP2GO Activity Search.

### Print to MYOB on macOS

The desktop uploader checks for **Upload to MYOB** on startup and installs it in the macOS Print dialog’s **PDF** menu if missing. You can also run `python3 install_print_workflow.py` manually. Use the same Python you use to run the uploader. From an app that supports printing, choose **Print → PDF → Upload to MYOB**. The uploader opens with a temporary copy of the printed PDF selected. Choose the company and confirm the upload as usual. Closing the uploader removes the temporary copy. Successful sends use the normal monthly archive.

If you move this project or change Python installations, delete `~/Library/PDF Services/Upload to MYOB.app` and reopen the uploader to recreate it.

## Versions and updates

All tools in this repository share the semantic version in `version.py`. The uploader displays it in the title area and with `--version`.

The desktop app checks for a published GitHub release when it starts. You can also check under **Settings… → Updates**. When a newer release is available, the app asks before installing it. Installation advances a clean Git checkout on `main` to the release tag and requires that the tag be a descendant of the current commit. Reopen the app after installation. Local credentials and upload history stay in `data/`. Source archives can check for updates and need to be replaced manually. Set `MYOB_SKIP_UPDATE_CHECK=1` to skip the startup check.

To publish a release, update `VERSION`, commit and push the changes, then create a GitHub release with the matching tag (for example, `v1.1.0`). A tag by itself does not trigger the app’s update prompt.

## License

GNU General Public License, version 3 or later. See [LICENSE](LICENSE).

The upload list shows each invoice’s status. Red marks invoices ready to send or stopped by an error. Orange marks uploading, delivery checks and pending delivery. Green means SMTP2GO confirmed that MYOB’s mail server accepted the email. Click **View log** for the detailed activity.
