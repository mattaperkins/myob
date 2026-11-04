#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Supplier documents -> selected MYOB Uploads address, via supported email ingestion.

Python 3.10+, standard library only. Sends via SMTP2GO's HTTPS API.
Run without arguments for the GUI; use 'send --help' for command-line uploads.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
import urllib.request
import urllib.error
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid

DATA = Path(__file__).resolve().parent / "data"
API_URL = "https://api.smtp2go.com/v3/email/send"
ACTIVITY_URL = "https://api.smtp2go.com/v3/activity/search"
DELIVERY_EVENTS = ("delivered", "hard-bounced", "rejected", "soft-bounced", "processed")
MAX_BYTES = 10_000_000
TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff"}


def address(value):
    value = value.strip()
    if (len(value) > 254 or value.count("@") != 1 or
            any(c.isspace() or c in '<>,;:\\"' for c in value)):
        raise ValueError("Enter one plain email address, without a display name.")
    local, domain = value.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("Enter a valid email address.")
    value.encode("ascii")
    return value


def read_document(path):
    suffix = path.suffix.lower()
    if suffix not in TYPES:
        raise ValueError(f"Unsupported document format: {path.name}")
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError(f"{path.name}: file must be nonempty and at most 10 MB.")
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError(f"{path.name}: file exceeds 10 MB.")
    valid = ((suffix == ".pdf" and content.startswith(b"%PDF-")) or
             (suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n")) or
             (suffix in (".jpg", ".jpeg") and content.startswith(b"\xff\xd8\xff")) or
             (suffix in (".tif", ".tiff") and content[:4] in
              (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")))
    if not valid:
        raise ValueError(f"{path.name}: content does not match its file extension.")
    return content, hashlib.sha256(content).hexdigest()


def make_message(sender, recipient, path, content):
    msg = EmailMessage(policy=SMTP)
    msg["From"], msg["To"] = address(sender), address(recipient)
    name = path.name.replace("\r", " ").replace("\n", " ")
    msg["Subject"] = f"Supplier document: {name}"
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content("Supplier document for review in MYOB Uploads / In Tray.\n"
                    "This email does not indicate whether the bill is paid.\n")
    main, sub = TYPES[path.suffix.lower()].split("/")
    msg.add_attachment(content, maintype=main, subtype=sub, filename=name)
    return msg


class Store:
    def __init__(self, root=DATA):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "settings.json"
        self.config = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists()
                       else {"companies": [], "smtp2go": {"sender": ""}})
        # Preserve any previous installation's companies and From address.
        self.config.setdefault("smtp2go", {"sender": self.config.get("smtp", {}).get("sender", "")})
        self.db = sqlite3.connect(self.root / "history.sqlite3", timeout=5)
        self.db.execute("""CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY, destination TEXT NOT NULL, digest TEXT NOT NULL,
            filename TEXT NOT NULL, status TEXT NOT NULL, message_id TEXT NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(destination, digest))""")
        self.db.commit()
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(uploads)")}
        if "provider_id" not in columns:
            self.db.execute("ALTER TABLE uploads ADD COLUMN provider_id TEXT")
            self.db.commit()
        if "delivery_status" not in columns:
            self.db.execute("ALTER TABLE uploads ADD COLUMN delivery_status TEXT")
            self.db.commit()
        if os.name == "posix":
            (self.root / "history.sqlite3").chmod(0o600)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(self.config, stream, indent=2)
        if os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(self.path)

    def status(self, destination, digest):
        row = self.db.execute("SELECT status FROM uploads WHERE destination=? AND digest=?",
                              (destination.lower(), digest)).fetchone()
        return row[0] if row else None

    def reserve(self, destination, digest, filename, message_id):
        # Commit before sending; UNIQUE prevents concurrent duplicate sends locally.
        with self.db:
            self.db.execute("INSERT INTO uploads(destination,digest,filename,status,message_id) "
                            "VALUES(?,?,?,'uncertain',?)",
                            (destination.lower(), digest, filename, message_id))

    def accepted(self, destination, digest, provider_id=None):
        with self.db:
            self.db.execute("UPDATE uploads SET status='api_accepted',provider_id=? "
                            "WHERE destination=? AND digest=?", (provider_id, destination.lower(), digest))


def build_batch(store, company, paths):
    if not paths:
        raise ValueError("Choose at least one document.")
    destination = address(company["email"])
    sender = address(store.config["smtp2go"].get("sender", ""))
    batch, skipped, seen = [], [], set()
    # Validate all selected documents before sending any.
    for path in paths:
        content, digest = read_document(path)
        status = store.status(destination, digest)
        if status == "uncertain":
            raise ValueError(f"{path.name}: earlier delivery is uncertain. Check MYOB Uploads "
                             "and mail delivery before retrying; see README.md.")
        if status or digest in seen:
            skipped.append(path.name)
            continue
        seen.add(digest)
        batch.append((path, digest, make_message(sender, destination, path, content)))
    return batch, skipped


def load_api_key(root=DATA):
    value = os.environ.get("SMTP2GO_API_KEY", "").strip()
    if not value:
        path = Path(root) / "smtp2go.key"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
    return value


def save_api_key(root, key):
    if not key.startswith("api-") or len(key) <= 4 or any(c.isspace() for c in key):
        raise ValueError("Enter a valid SMTP2GO API key beginning with api-.")
    target = Path(root) / "smtp2go.key"
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                         delete=False) as stream:
            pending = Path(stream.name)
            stream.write(key + "\n")
        pending.replace(target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def api_payload(msg):
    return {
        "sender": address(str(msg["From"])),
        "to": [address(str(msg["To"]))],
        "subject": str(msg["Subject"]),
        "text_body": msg.get_body(preferencelist=("plain",)).get_content(),
        # Wait for processing counts; still not a delivery/ingestion guarantee.
        "fastaccept": False,
        "custom_headers": [{"header": "Message-ID", "value": str(msg["Message-ID"])}],
        "attachments": [{"filename": part.get_filename(), "mimetype": part.get_content_type(),
                         "fileblob": base64.b64encode(part.get_payload(decode=True)).decode("ascii")}
                        for part in msg.iter_attachments()],
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the API key or document to another URL.
        return None


def send_api(msg, api_key):
    if not api_key or not api_key.startswith("api-") or any(c.isspace() for c in api_key):
        raise ValueError("Enter a valid SMTP2GO API key.")
    req = urllib.request.Request(API_URL, method="POST",
        data=json.dumps(api_payload(msg)).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-Smtp2go-Api-Key": api_key})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=45) as response:
            result = json.loads(response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"SMTP2GO HTTP {exc.code}. Check API permissions, sender verification "
                           "and SMTP2GO activity. No automatic retry.") from None
    except Exception:
        # Never put raw responses/request headers (which may contain credentials) in logs.
        raise RuntimeError("SMTP2GO request did not complete with a valid response. "
                           "Check SMTP2GO activity before retrying.") from None
    data = result.get("data", {}) if isinstance(result, dict) else {}
    if (not isinstance(data, dict) or data.get("succeeded") != 1 or data.get("failed") != 0
            or data.get("failures") or data.get("error") or not data.get("email_id")):
        raise RuntimeError("SMTP2GO did not confirm acceptance of this email. "
                           "Check API permissions, sender verification and SMTP2GO activity.")
    return str(data["email_id"])


def delivery_event(api_key, provider_id, recipient, created):
    start = datetime.fromisoformat(created).replace(tzinfo=timezone.utc) - timedelta(minutes=5)
    payload = {"search_email_id": provider_id, "search_recipient": recipient,
               "start_date": start.isoformat(), "event_types": list(DELIVERY_EVENTS),
               "limit": 100}
    req = urllib.request.Request(ACTIVITY_URL, method="POST",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "X-Smtp2go-Api-Key": api_key})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=8) as response:
            data = json.loads(response.read(1_000_000))["data"]
        if not isinstance(data["events"], list) or data.get("error"):
            raise ValueError("Invalid response")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"SMTP2GO activity lookup returned HTTP {exc.code}. "
                           "Check that the API key can access Activity Search.") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("SMTP2GO activity lookup did not return a valid response.") from None

    events = [event for event in data["events"]
              if isinstance(event, dict) and event.get("email_id") == provider_id
              and str(event.get("recipient", "")).casefold() == recipient.casefold()
              and event.get("event") in DELIVERY_EVENTS]
    terminal = [event for event in events if event["event"] in DELIVERY_EVENTS[:3]]
    latest = max(terminal or events, key=lambda event: event.get("date", ""), default=None)
    return latest["event"] if latest else "pending"


def check_delivery(store, upload_id, api_key, log, *, poll=False):
    recipient, name, provider_id, created, saved = store.db.execute(
        "SELECT destination,filename,provider_id,created,delivery_status FROM uploads WHERE id=?",
        (upload_id,)).fetchone()
    status = saved or "pending"
    for delay in ((0, 3, 7, 15) if poll else (0,)):
        if delay:
            time.sleep(delay)
        try:
            observed = delivery_event(api_key, provider_id, recipient, created)
        except RuntimeError as exc:
            log(f"{name} (history ID {upload_id}): {exc}")
            break
        if observed != "pending" and (status not in DELIVERY_EVENTS[:3]
                                      or observed in DELIVERY_EVENTS[:3]):
            status = observed
        if status in DELIVERY_EVENTS[:3]:
            break
    with store.db:
        store.db.execute("UPDATE uploads SET delivery_status=? WHERE id=?", (status, upload_id))
    if status == "delivered":
        log(f"Delivered to MYOB's mail server: {name} (history ID {upload_id})")
    elif status in ("hard-bounced", "rejected"):
        log(f"Delivery failed ({status}): {name} (history ID {upload_id})")
    else:
        log(f"Delivery pending ({status}): {name} (history ID {upload_id})")
    return status


def write_archive_pdf(path, msg, stream):
    content = next(msg.iter_attachments()).get_payload(decode=True)
    if path.suffix.lower() == ".pdf":
        stream.write(content)
        return

    try:
        from PIL import Image, ImageOps, ImageSequence
    except ImportError:
        raise ValueError("Install Pillow to archive image invoices: python3 -m pip install Pillow.") from None

    with Image.open(BytesIO(content)) as source:
        pages = []
        for frame in ImageSequence.Iterator(source):
            image = ImageOps.exif_transpose(frame).convert("RGBA")
            page = Image.new("RGB", image.size, "white")
            page.paste(image, mask=image.getchannel("A"))
            pages.append(page)
        pages[0].save(stream, format="PDF", save_all=True, append_images=pages[1:])


def send_batch(store, company, batch, api_key, log):
    if not api_key or not api_key.startswith("api-") or any(c.isspace() for c in api_key):
        raise ValueError("Enter a valid SMTP2GO API key.")
    for path, digest, msg in batch:
        folder = Path.home() / "Documents" / "myob" / date.today().strftime("%m-%Y")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / f"{path.stem[:40]}-{uuid4().hex}.pdf"
        staged = None
        try:
            # Prepare the PDF before sending so conversion errors cannot leave an unarchived send.
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".pending-", suffix=".pdf",
                                             delete=False) as stream:
                staged = Path(stream.name)
                write_archive_pdf(path, msg, stream)

            store.reserve(company["email"], digest, path.name, str(msg["Message-ID"]))
            try:
                provider_id = send_api(msg, api_key)
            except Exception as exc:
                raise RuntimeError(f"Stopped at {path.name}; acceptance or delivery is unconfirmed. "
                                   f"{exc} Check MYOB and SMTP2GO activity before retrying.") from None

            # Keep the prepared PDF if publishing it fails after acceptance.
            pending, staged = staged, None
            try:
                store.accepted(company["email"], digest, provider_id)
            finally:
                try:
                    pending.replace(target)
                except OSError as exc:
                    raise RuntimeError(f"SMTP2GO accepted {path.name}, but the archive could not be "
                                       f"finalised. The PDF is at {pending}. {exc}") from exc
            log(f"SMTP2GO accepted: {path.name} (email ID: {provider_id})")
            log(f"Archived PDF: {target}")
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        upload_id = store.db.execute("SELECT id FROM uploads WHERE destination=? AND digest=?",
                                     (company["email"].lower(), digest)).fetchone()[0]
        check_delivery(store, upload_id, api_key, log, poll=True)


def launch(data_dir=DATA, initial_files=()):
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox, simpledialog
    except ImportError as exc:
        raise RuntimeError("Install Python with Tcl/Tk support to use this file picker.") from exc
    try:
        from tkinterdnd2 import COPY, DND_FILES, REFUSE_DROP, TkinterDnD
    except ImportError:
        TkinterDnD = None

    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    store = Store(data_dir)
    if sys.platform == "darwin":
        from install_print_workflow import install

        try:
            install()
        except (OSError, subprocess.CalledProcessError) as exc:
            messagebox.showwarning("Print workflow setup",
                                   f"Could not install Print → PDF → Upload to MYOB.\n\n{exc}",
                                   parent=root)
    root.title("MYOB Uploader")
    root.geometry("780x640")
    root.minsize(590, 500)
    style = ttk.Style(root)
    style.configure("Title.TLabel", font=("TkDefaultFont", 24, "bold"))
    style.configure("Section.TLabel", font=("TkDefaultFont", 12, "bold"))
    style.configure("Action.TButton", padding=(12, 6))

    outer = ttk.Frame(root, padding=(26, 22))
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer)
    header.pack(fill="x")
    ttk.Label(header, text="MYOB Uploader", style="Title.TLabel").pack(side="left")
    settings_button = ttk.Button(header, text="Settings…", command=lambda: open_settings())
    settings_button.pack(side="right")
    ttk.Label(outer, text="Send supplier invoices and receipts to MYOB Uploads.").pack(
        anchor="w", pady=(4, 18))

    routing = ttk.Frame(outer)
    routing.pack(fill="x", pady=(0, 16))
    routing.columnconfigure(0, weight=1)
    routing.columnconfigure(1, weight=1)
    ttk.Label(routing, text="MYOB company", style="Section.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(routing, text="Verified From address", style="Section.TLabel").grid(row=0, column=1, sticky="w")
    company_box = ttk.Combobox(routing, state="readonly", width=24)
    sender_box = ttk.Combobox(routing, state="readonly", width=24)
    company_box.grid(row=1, column=0, sticky="ew", padx=(0, 14), pady=(6, 0))
    sender_box.grid(row=1, column=1, sticky="ew", pady=(6, 0))

    def refresh(company_email=None):
        companies = store.config["companies"]
        company_box["values"] = [company["name"] for company in companies]
        company_box.set("")
        if companies:
            index = next((i for i, company in enumerate(companies)
                          if company["email"] == company_email), 0)
            company_box.current(index)
        mail = store.config["smtp2go"]
        senders = mail.get("senders", [mail["sender"]] if mail["sender"] else [])
        sender_box["values"] = senders
        sender_box.set(mail["sender"] if mail["sender"] in senders else "")

    def open_settings():
        config = deepcopy(store.config)
        companies = config["companies"]
        index = company_box.current()
        company_email = companies[index]["email"] if index >= 0 else None
        mail = config["smtp2go"]
        senders = mail.setdefault("senders", [mail["sender"]] if mail["sender"] else [])
        mail["sender"] = sender_box.get()

        dialog = tk.Toplevel(root)
        dialog.title("Uploader Settings")
        dialog.transient(root)
        dialog.minsize(540, 330)
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill="both", expand=True)
        tabs = ttk.Notebook(panel)
        tabs.pack(fill="both", expand=True)
        company_tab = ttk.Frame(tabs, padding=12)
        sender_tab = ttk.Frame(tabs, padding=12)
        tabs.add(company_tab, text="Companies")
        tabs.add(sender_tab, text="Verified senders")
        api_tab = ttk.Frame(tabs, padding=12)
        tabs.add(api_tab, text="SMTP2GO")
        if os.environ.get("SMTP2GO_API_KEY", "").strip():
            key_status = "The environment variable SMTP2GO_API_KEY takes precedence."
        elif (store.root / "smtp2go.key").is_file():
            key_status = "An API key is saved on this Mac."
        else:
            key_status = "No local API key is saved."
        ttk.Label(api_tab, text=key_status, wraplength=460).pack(anchor="w")
        ttk.Label(api_tab, text="New API key").pack(anchor="w", pady=(14, 4))
        key_entry = ttk.Entry(api_tab, show="•", width=44)
        key_entry.pack(fill="x")
        ttk.Label(api_tab, text="Leave this field blank to keep the current key.",
                  wraplength=460).pack(anchor="w", pady=(6, 0))

        def manage(tab, label, rows, columns, display, edit):
            ttk.Label(tab, text=label, wraplength=480).pack(anchor="w", pady=(0, 8))
            tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse", height=6)
            for column in columns:
                tree.heading(column, text=column)
                tree.column(column, width=190 if column == "Company" else 290)
            tree.pack(fill="both", expand=True)

            def populate(selected=None):
                tree.delete(*tree.get_children())
                for i, row in enumerate(rows):
                    tree.insert("", "end", iid=str(i), values=display(row))
                if selected is not None and rows:
                    item = str(min(selected, len(rows) - 1))
                    tree.selection_set(item)
                    tree.see(item)

            def change(action):
                selection = tree.selection()
                selected = int(selection[0]) if selection else None
                if action != "Add" and selected is None:
                    return
                try:
                    if action == "Remove":
                        if messagebox.askyesno("Remove entry", f"Remove {display(rows[selected])[0]}?",
                                               parent=dialog):
                            rows.pop(selected)
                    else:
                        index = selected if action == "Edit" else None
                        value = edit(index)
                        if value is None:
                            return
                        if index is None:
                            selected = len(rows)
                            rows.append(value)
                        else:
                            rows[index] = value
                    populate(selected)
                except ValueError as exc:
                    messagebox.showerror("Settings", str(exc), parent=dialog)

            controls = ttk.Frame(tab)
            controls.pack(fill="x", pady=(8, 0))
            for action in ("Add", "Edit", "Remove"):
                ttk.Button(controls, text=action, command=lambda action=action: change(action)).pack(
                    side="left", padx=(0, 6))
            populate(0)

        def edit_company(index):
            existing = companies[index] if index is not None else {"name": "", "email": ""}
            name = simpledialog.askstring("Company", "Company name", initialvalue=existing["name"],
                                          parent=dialog)
            if name is None:
                return None
            name = name.strip()
            if not name:
                raise ValueError("Enter a company name.")
            if any(i != index and company["name"].casefold() == name.casefold()
                   for i, company in enumerate(companies)):
                raise ValueError("This company name is already saved.")
            email = simpledialog.askstring("Company", "MYOB Uploads email address",
                                           initialvalue=existing["email"], parent=dialog)
            if email is None:
                return None
            email = address(email)
            if any(i != index and company["email"].casefold() == email.casefold()
                   for i, company in enumerate(companies)):
                raise ValueError("This uploads address is already saved.")
            return {"name": name, "email": email}

        def edit_sender(index):
            email = simpledialog.askstring("Verified sender", "Email address verified in SMTP2GO",
                                           initialvalue=senders[index] if index is not None else "",
                                           parent=dialog)
            if email is None:
                return None
            email = address(email)
            if any(i != index and sender.casefold() == email.casefold()
                   for i, sender in enumerate(senders)):
                raise ValueError("This sender address is already saved.")
            return email

        manage(company_tab, "Manage the company names and their MYOB Uploads addresses.",
               companies, ("Company", "MYOB Uploads address"),
               lambda company: (company["name"], company["email"]), edit_company)
        manage(sender_tab, "Add addresses already verified in SMTP2GO.",
               senders, ("Verified email address",), lambda email: (email,), edit_sender)

        def save_settings():
            if mail["sender"] not in senders:
                mail["sender"] = senders[0] if senders else ""
            previous = store.config
            store.config = config
            try:
                key = key_entry.get().strip()
                if key:
                    save_api_key(store.root, key)
                if config != previous:
                    store.save()
            except (OSError, ValueError) as exc:
                store.config = previous
                messagebox.showerror("Save settings", str(exc), parent=dialog)
                return
            refresh(company_email)
            dialog.destroy()

        footer = ttk.Frame(panel)
        footer.pack(fill="x", pady=(14, 0))
        ttk.Button(footer, text="Save settings", command=save_settings).pack(side="right")
        ttk.Button(footer, text="Cancel", command=dialog.destroy).pack(side="right", padx=8)
        dialog.wait_visibility()
        dialog.grab_set()

    ttk.Separator(outer).pack(fill="x", pady=(0, 14))
    documents = ttk.Frame(outer)
    documents.pack(fill="x")
    ttk.Label(documents, text="Documents", style="Section.TLabel").pack(side="left")
    files = list(initial_files)
    file_label = ttk.Label(outer, text="No documents selected")
    file_label.pack(anchor="w", pady=(5, 8))

    def show_files():
        file_label.config(text=f"{len(files)} document(s) selected")
        log.delete("1.0", "end")
        for path in files:
            log.insert("end", f"{path.name}\n")

    def choose_files():
        chosen = filedialog.askopenfilenames(parent=root, title="Choose supplier documents", filetypes=[
            ("Supplier documents", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff"), ("All files", "*")])
        if chosen:
            files[:] = [Path(p) for p in chosen]
            show_files()

    def drop_files(event):
        if busy:
            return REFUSE_DROP
        try:
            dropped = [Path(name) for name in root.tk.splitlist(event.data)]
            for path in dropped:
                if not path.is_file() or path.suffix.lower() not in TYPES:
                    raise ValueError(f"Choose PDF, PNG, JPEG or TIFF files. Cannot add: {path.name}")
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Add documents", str(exc), parent=root)
            return REFUSE_DROP
        for path in dropped:
            if path not in files:
                files.append(path)
        show_files()
        return COPY

    choose_button = ttk.Button(documents, text="Choose documents…", command=choose_files)
    choose_button.pack(side="right")
    log_frame = ttk.Frame(outer)
    log_frame.pack(fill="both", expand=True)
    log = tk.Text(log_frame, height=8, wrap="word", padx=14, pady=12,
                  font="TkDefaultFont", undo=False)
    scrollbar = ttk.Scrollbar(log_frame, command=log.yview)
    log.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)
    log.bind("<Key>", lambda event: "break" if not event.state & 0xC else None)
    busy = False
    if TkinterDnD:
        file_label.config(text="Drop invoices in the box below, or choose documents")
        log.drop_target_register(DND_FILES)
        log.dnd_bind("<<Drop>>", drop_files)

    def run():
        nonlocal busy
        try:
            index = company_box.current()
            if index < 0:
                raise ValueError("Add and select a company first.")
            company = dict(store.config["companies"][index])
            if not sender_box.get():
                raise ValueError("Add a verified sender in Settings first.")
            settings = dict(store.config["smtp2go"], sender=address(sender_box.get()))
            store.config["smtp2go"] = settings
            store.save()
            batch, skipped = build_batch(store, company, files)
            log.delete("1.0", "end")
            for name in skipped:
                log.insert("end", f"Skipped duplicate: {name}\n")
            if not batch:
                log.insert("end", "No new documents to send.\n")
                return
            if not messagebox.askyesno("Confirm company", f"Send {len(batch)} document(s) to:\n\n"
                        f"{company['name']}\n{company['email']}\n\n"
                        "Confirm this is the correct company's unique MYOB uploads address.", parent=root):
                return
            api_key = load_api_key(store.root)
            if not api_key:
                api_key = simpledialog.askstring("SMTP2GO authentication", "SMTP2GO API key (not saved):",
                                                  show="*", parent=root)
                if api_key is None:
                    return
                api_key = api_key.strip()
            events = queue.Queue()
            busy = True
            for button in buttons:
                button.config(state="disabled")
            company_box.config(state="disabled")
            sender_box.config(state="disabled")
            upload_status.set("Sending and checking delivery…")

            def worker():
                worker_store = None
                try:
                    worker_store = Store(store.root)
                    # Use the reviewed settings even if another instance edits its config.
                    worker_store.config["smtp2go"] = settings
                    send_batch(worker_store, company, batch, api_key, lambda line: events.put(("log", line)))
                    events.put(("done", "Delivery checks finished. See each document’s status above.\n"
                                          "API acceptance does not confirm MYOB has imported the documents."))
                except Exception as exc:
                    events.put(("error", str(exc)))
                finally:
                    if worker_store:
                        worker_store.db.close()

            def poll():
                nonlocal busy
                while True:
                    try:
                        kind, value = events.get_nowait()
                    except queue.Empty:
                        root.after(100, poll)
                        return
                    log.insert("end", value + "\n")
                    log.see("end")
                    if kind in ("done", "error"):
                        busy = False
                        for button in buttons:
                            button.config(state="normal")
                        company_box.config(state="readonly")
                        sender_box.config(state="readonly")
                        upload_status.set("Send stopped" if kind == "error" else "Delivery checks finished")
                        if kind == "error":
                            messagebox.showerror("Send stopped", value, parent=root)
                        return
            threading.Thread(target=worker, daemon=True).start()
            root.after(100, poll)
        except Exception as exc:
            messagebox.showerror("Upload", str(exc), parent=root)

    actions = ttk.Frame(outer)
    actions.pack(fill="x", pady=(14, 0))
    upload_status = tk.StringVar(value="PDF copies are saved to your monthly archive.")
    ttk.Label(actions, textvariable=upload_status).pack(side="left")
    send_button = ttk.Button(actions, text="Upload to MYOB…", command=run, style="Action.TButton")
    send_button.pack(side="right")
    buttons = [settings_button, choose_button, send_button]

    def close():
        if busy:
            messagebox.showinfo("Sending", "Please wait for the send to finish before closing.", parent=root)
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    refresh()
    if files:
        show_files()
    root.mainloop()
    store.db.close()


def select_company(companies, selector):
    if not companies:
        raise ValueError("Add a company in Settings or use the add-company command first.")
    if selector is None:
        if len(companies) == 1:
            return companies[0]
        raise ValueError("Specify --company with a number, exact name or uploads address. "
                         "Run 'companies' to list destinations.")
    if selector.isdecimal() and 1 <= int(selector) <= len(companies):
        return companies[int(selector) - 1]
    matches = [c for c in companies if selector.casefold() in
               (c["name"].casefold(), c["email"].casefold())]
    if len(matches) != 1:
        raise ValueError("Company selection is missing or ambiguous. Run 'companies' to list destinations.")
    return matches[0]


def document_paths(inputs, recursive=False):
    paths = []
    for value in inputs:
        path = Path(value).expanduser()
        if path.is_dir():
            candidates = path.rglob("*") if recursive else path.iterdir()
            found = sorted(p for p in candidates if p.is_file() and p.suffix.lower() in TYPES)
            if not found:
                raise ValueError(f"No supported documents in folder: {path}")
            paths.extend(found)
        elif path.is_file():
            paths.append(path)
        else:
            raise ValueError(f"Document or folder does not exist: {path}")
    return paths


def cli_send(store, args):
    company = select_company(store.config["companies"], args.company)
    if args.sender:
        store.config["smtp2go"]["sender"] = address(args.sender)
    paths = document_paths(args.documents, args.recursive)
    batch, skipped = build_batch(store, company, paths)
    print(f"Company: {company['name']}")
    print(f"To: {company['email']}")
    print(f"From: {store.config['smtp2go']['sender']}")
    for name in skipped:
        print(f"Skipped duplicate: {name}")
    print(f"New documents: {len(batch)}; skipped duplicates: {len(skipped)}")
    for path, _, _ in batch:
        print(f"Document: {path}")
    if args.preview_dir:
        folder = Path(args.preview_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        dest = hashlib.sha256(company["email"].lower().encode()).hexdigest()[:12]
        for _, digest, msg in batch:
            (folder / f"{dest}-{digest}.eml").write_bytes(msg.as_bytes())
        print(f"Preview emails saved to: {folder}")
    if args.dry_run:
        print("Dry run: nothing sent; no API key required.")
        return
    if not batch:
        print("No new documents to send.")
        return
    api_key = load_api_key(store.root)
    if not api_key:
        if args.no_input or not sys.stdin.isatty():
            raise ValueError("Set SMTP2GO_API_KEY or put the key in the data folder's smtp2go.key file.")
        api_key = getpass.getpass("SMTP2GO API key (not saved): ").strip()
    send_batch(store, company, batch, api_key, lambda line: print(line, flush=True))
    print("Delivery checks finished. Check MYOB Uploads to confirm document import.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA,
                        help="Settings, private API key and history folder (default: data beside script)")
    parser.add_argument("--printed-pdf", type=Path, nargs="+",
                        help="Open the desktop uploader with PDFs from the macOS Print menu")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("companies", help="List saved companies and uploads addresses")
    add = sub.add_parser("add-company", help="Save another company's Uploads destination")
    add.add_argument("--name", required=True)
    add.add_argument("--email", required=True, help="Company-specific MYOB uploads email address")
    send = sub.add_parser("send", help="Send supplier documents using the SMTP2GO API")
    send.add_argument("documents", nargs="+", help="Document paths or folders; shell-expanded wildcards also work")
    send.add_argument("--company", help="Company number, exact name or uploads email; optional if only one is saved")
    send.add_argument("--sender", help="Override the verified From address for this run")
    send.add_argument("--recursive", action="store_true", help="Include documents in subfolders")
    send.add_argument("--dry-run", action="store_true", help="Validate/list documents without sending or changing history")
    send.add_argument("--preview-dir", help="Also write .eml previews here; use --dry-run to avoid sending")
    send.add_argument("--no-input", action="store_true", help="Fail instead of prompting if the API key is missing")
    sub.add_parser("history", help="Show local mail acceptance and uncertain sends")
    delivery = sub.add_parser("delivery", help="Refresh SMTP2GO delivery status for a history ID")
    delivery.add_argument("id", type=int)
    resolve = sub.add_parser("resolve", help="Resolve uncertainty after checking MYOB and email delivery")
    resolve.add_argument("id", type=int)
    resolve.add_argument("--outcome", required=True, choices=("accepted", "not-sent"))
    args = parser.parse_args(argv)
    if args.command is None:
        if args.printed_pdf:
            with tempfile.TemporaryDirectory(prefix="myob-print-") as folder:
                copies = []
                for i, source in enumerate(args.printed_pdf):
                    subfolder = Path(folder) / str(i)
                    subfolder.mkdir()
                    target = subfolder / f"{source.stem}.pdf"
                    shutil.copyfile(source, target)
                    copies.append(target)
                launch(args.data_dir, copies)
        else:
            launch(args.data_dir)
        return
    store = Store(args.data_dir)
    try:
        if args.command == "companies":
            for i, company in enumerate(store.config["companies"], 1):
                print(f"{i}. {company['name']} <{company['email']}>")
        elif args.command == "add-company":
            name, email = args.name.strip(), address(args.email)
            if not name:
                raise ValueError("Company name must not be empty.")
            if any(c["email"].casefold() == email.casefold() for c in store.config["companies"]):
                raise ValueError("This uploads address is already saved.")
            store.config["companies"].append({"name": name, "email": email})
            store.save()
            print(f"Saved company {len(store.config['companies'])}: {name} <{email}>")
        elif args.command == "send":
            cli_send(store, args)
        elif args.command == "history":
            for row in store.db.execute("SELECT id,created,destination,filename,status,COALESCE(provider_id,''),COALESCE(delivery_status,'') FROM uploads ORDER BY id"):
                print(" | ".join(map(str, row)))
        elif args.command == "delivery":
            row = store.db.execute("SELECT provider_id FROM uploads WHERE id=?", (args.id,)).fetchone()
            if not row or not row[0]:
                raise ValueError("This history ID has no SMTP2GO email ID.")
            api_key = load_api_key(store.root)
            if not api_key:
                raise ValueError("Set SMTP2GO_API_KEY or save the key in data/smtp2go.key.")
            check_delivery(store, args.id, api_key, print)
        else:
            row = store.db.execute("SELECT destination,filename,status FROM uploads WHERE id=?", (args.id,)).fetchone()
            if not row or row[2] not in ("uncertain", "smtp_accepted", "api_accepted"):
                raise ValueError("This ID is not a recorded send.")
            print(f"{row[1]} -> {row[0]}")
            if input("After checking MYOB and email delivery, type CONFIRM to resolve: ") != "CONFIRM":
                return
            with store.db:
                if args.outcome == "not-sent":
                    store.db.execute("DELETE FROM uploads WHERE id=?", (args.id,))
                else:
                    store.db.execute("UPDATE uploads SET status='api_accepted' WHERE id=?", (args.id,))
            print("Resolved.")
    finally:
        store.db.close()


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
