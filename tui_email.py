#!/usr/bin/env python3
import platform

if platform.system() == "Windows":
    try:
        import curses
    except ImportError:
        try:
            import windows_curses as curses
        except ImportError:
            curses = None
else:
    try:
        import curses
    except ImportError:
        curses = None

import json
import sqlite3
import imaplib
import smtplib
import ssl
import email
import html
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import os
from html.parser import HTMLParser
from email.message import EmailMessage
from email.utils import parseaddr, getaddresses
from pathlib import Path
from datetime import datetime

if os.name == "nt":
    BASE_DIR = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / "tui_email"
else:
    BASE_DIR = Path.home() / ".tui_email"
DB_PATH = BASE_DIR / "messages.db"
CONFIG_PATH = BASE_DIR / "config.json"
FOLDERS_DEFAULT = ["Inbox", "Sent", "Drafts", "Archive", "Flagged", "Spam", "Trash"]
FETCH_LIMIT = 30

PIPER_VOICE_DIR = BASE_DIR / "piper_voices"
PIPER_DEFAULT_VOICE = "en_US-lessac-medium"
PIPER_VOICES_JSON_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
PIPER_DEFAULT_MODEL_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
PIPER_DEFAULT_CONFIG_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"

class Message:
    def __init__(
        self,
        id,
        folder,
        subject,
        from_addr,
        to_addr,
        cc_addr,
        bcc_addr,
        date,
        body,
        read=False,
        flagged=False,
        remote_uid=None,
        message_id=None,
    ):
        self.id = id
        self.folder = folder
        self.subject = subject
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.cc_addr = cc_addr
        self.bcc_addr = bcc_addr
        self.date = date
        self.body = body
        self.read = read
        self.flagged = flagged
        self.remote_uid = remote_uid
        self.message_id = message_id

    def snippet(self, length=70):
        return (self.body.replace("\n", " ")[:length-3] + "...") if len(self.body) > length else self.body


class Conversation:
    def __init__(self, key, subject, messages):
        self.key = key
        self.subject = subject
        self.messages = messages

    @property
    def latest(self):
        return self.messages[0] if self.messages else None

    @property
    def unread_count(self):
        return sum(1 for m in self.messages if not m.read)

    @property
    def display_from(self):
        latest = self.latest
        if not latest:
            return ""
        return latest.from_addr or latest.to_addr or "(unknown)"


def normalize_subject(subject):
    text = (subject or "(No Subject)").strip()
    # Collapse common mail prefixes so Re:/Fwd: messages stay in one thread.
    return re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:\s*)+", "", text, flags=re.IGNORECASE).strip().lower() or "(no subject)"


def _download_file(url, path):
    try:
        PIPER_VOICE_DIR.mkdir(mode=0o700, exist_ok=True)
        from urllib.request import urlopen

        with urlopen(url) as response:
            with open(path, "wb") as out_file:
                out_file.write(response.read())
        return True
    except Exception:
        return False


def _load_piper_voices():
    PIPER_VOICE_DIR.mkdir(mode=0o700, exist_ok=True)
    cache_path = PIPER_VOICE_DIR / "voices.json"
    voices = []
    try:
        if cache_path.exists():
            voices = json.loads(cache_path.read_text(encoding="utf-8"))
        if not voices:
            from urllib.request import urlopen

            with urlopen(PIPER_VOICES_JSON_URL) as response:
                voices_map = json.loads(response.read().decode("utf-8"))
            if isinstance(voices_map, dict):
                voices = sorted(voices_map.keys())
            elif isinstance(voices_map, list):
                voices = sorted(voices_map)
            if voices:
                cache_path.write_text(json.dumps(voices), encoding="utf-8")
    except Exception:
        pass
    return voices


def _voice_urls_for(voice):
    # voice string format: en_US-lessac-medium
    parts = voice.split("-")
    if len(parts) < 3:
        return None, None
    locale = parts[0]
    voice_name = parts[1]
    quality = parts[2]
    lang = locale.split("_")[0].lower()

    model_url = (
        f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang}/{locale}/{voice_name}/{quality}/{voice}.onnx"
    )
    config_url = (
        f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{lang}/{locale}/{voice_name}/{quality}/{voice}.onnx.json"
    )
    return model_url, config_url


def _ensure_piper_voice(model_path=None, config_path=None):
    PIPER_VOICE_DIR.mkdir(mode=0o700, exist_ok=True)

    if model_path:
        model_path = Path(model_path)
    else:
        model_path = PIPER_VOICE_DIR / f"{PIPER_DEFAULT_VOICE}.onnx"

    if config_path:
        config_path = Path(config_path)
    else:
        config_path = PIPER_VOICE_DIR / f"{PIPER_DEFAULT_VOICE}.onnx.json"

    if not model_path.exists() or not config_path.exists():
        voice = load_config().get("piper", {}).get("voice", PIPER_DEFAULT_VOICE)
        inferred_model_url, inferred_config_url = _voice_urls_for(voice)

        if not model_path.exists():
            if inferred_model_url:
                if not _download_file(inferred_model_url, model_path):
                    # fallback to default model URL once
                    _download_file(PIPER_DEFAULT_MODEL_URL, model_path)

        if not config_path.exists():
            if inferred_config_url:
                if not _download_file(inferred_config_url, config_path):
                    _download_file(PIPER_DEFAULT_CONFIG_URL, config_path)

    if not model_path.exists() or not config_path.exists():
        return None, None

    return str(model_path), str(config_path)


def build_conversations(messages):
    groups = {}
    ordered_keys = []
    for msg in messages:
        key = normalize_subject(msg.subject)
        if key not in groups:
            groups[key] = []
            ordered_keys.append(key)
        groups[key].append(msg)

    conversations = []
    for key in ordered_keys:
        convo_messages = groups[key]
        subject = convo_messages[0].subject or "(No Subject)"
        conversations.append(Conversation(key, subject, convo_messages))
    return conversations


def ensure_data_dir():
    BASE_DIR.mkdir(mode=0o700, exist_ok=True)


def _jobs_dir():
    ensure_data_dir()
    path = BASE_DIR / "jobs"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _job_results_dir():
    ensure_data_dir()
    path = BASE_DIR / "job_results"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _write_job_result(job_stem, message, touched_folders=None):
    result = {
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "message": message,
        "touched_folders": touched_folders or [],
    }
    result_path = _job_results_dir() / f"{job_stem}.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")


def load_config():
    ensure_data_dir()
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(config):
    ensure_data_dir()
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def init_db():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder TEXT NOT NULL,
            subject TEXT,
            from_addr TEXT,
            to_addr TEXT,
            cc_addr TEXT,
            bcc_addr TEXT,
            date TEXT,
            body TEXT,
            is_read INTEGER DEFAULT 0,
            is_flagged INTEGER DEFAULT 0,
            remote_uid TEXT,
            message_id TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            name TEXT PRIMARY KEY
        )
    """)
    conn.commit()

    # Handle existing databases created before remote UID/message-id support.
    c.execute("PRAGMA table_info(messages)")
    columns = {row[1] for row in c.fetchall()}
    if "remote_uid" not in columns:
        c.execute("ALTER TABLE messages ADD COLUMN remote_uid TEXT")
    if "message_id" not in columns:
        c.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    if "cc_addr" not in columns:
        c.execute("ALTER TABLE messages ADD COLUMN cc_addr TEXT")
    if "bcc_addr" not in columns:
        c.execute("ALTER TABLE messages ADD COLUMN bcc_addr TEXT")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_folder_uid ON messages(folder, remote_uid)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_folder_mid ON messages(folder, message_id)")
    conn.close()


def load_folders():
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM folders ORDER BY lower(name)")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


def save_folders(folders):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM folders")
    c.executemany("INSERT OR REPLACE INTO folders (name) VALUES (?)", [(f,) for f in folders])
    conn.commit()
    conn.close()


def sort_folders(folders):
    desired = ["inbox", "sent", "drafts", "archive", "flagged", "spam", "trash"]
    normalized = {f.lower(): f for f in folders}
    ordered = []
    for d in desired:
        if d in normalized:
            ordered.append(normalized[d])
    remaining = [f for f in folders if f.lower() not in set(k.lower() for k in ordered)]
    remaining_sorted = sorted(remaining, key=lambda s: s.lower())
    return ordered + remaining_sorted


def save_message(msg):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if msg.id is None:
        c.execute(
            "INSERT INTO messages (folder, subject, from_addr, to_addr, cc_addr, bcc_addr, date, body, is_read, is_flagged, remote_uid, message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                msg.folder,
                msg.subject,
                msg.from_addr,
                msg.to_addr,
                msg.cc_addr,
                msg.bcc_addr,
                msg.date,
                msg.body,
                int(msg.read),
                int(msg.flagged),
                msg.remote_uid,
                msg.message_id,
            ),
        )
        msg.id = c.lastrowid
    else:
        c.execute(
            "UPDATE messages SET folder=?, subject=?, from_addr=?, to_addr=?, cc_addr=?, bcc_addr=?, date=?, body=?, is_read=?, is_flagged=?, remote_uid=?, message_id=? WHERE id=?",
            (
                msg.folder,
                msg.subject,
                msg.from_addr,
                msg.to_addr,
                msg.cc_addr,
                msg.bcc_addr,
                msg.date,
                msg.body,
                int(msg.read),
                int(msg.flagged),
                msg.remote_uid,
                msg.message_id,
                msg.id,
            ),
        )
    conn.commit()
    conn.close()


def clear_folder(folder):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE folder = ?", (folder,))
    conn.commit()
    conn.close()


def delete_message_by_id(message_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()


def load_messages(folder=None):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if folder:
        c.execute(
            "SELECT id, folder, subject, from_addr, to_addr, cc_addr, bcc_addr, date, body, is_read, is_flagged, remote_uid, message_id FROM messages WHERE lower(folder)=lower(?) ORDER BY id DESC",
            (folder,),
        )
    else:
        c.execute(
            "SELECT id, folder, subject, from_addr, to_addr, cc_addr, bcc_addr, date, body, is_read, is_flagged, remote_uid, message_id FROM messages ORDER BY id DESC"
        )
    rows = c.fetchall()
    conn.close()
    msgs = []
    for r in rows:
        msgs.append(
            Message(
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                r[6],
                r[7],
                r[8],
                bool(r[9]),
                bool(r[10]),
                remote_uid=r[11],
                message_id=r[12],
            )
        )
    return msgs


def message_sync_key(msg):
    if msg.remote_uid:
        return ("uid", msg.remote_uid)
    if msg.message_id:
        return ("message-id", msg.message_id.strip().lower())
    return None


def apply_folder_diff(folder, remote_msgs):
    local_msgs = load_messages(folder)

    local_map = {}
    for m in local_msgs:
        key = message_sync_key(m)
        if key and key not in local_map:
            local_map[key] = m

    remote_map = {}
    for m in remote_msgs:
        key = message_sync_key(m)
        if key and key not in remote_map:
            remote_map[key] = m

    deleted = 0
    updated = 0
    inserted = 0

    for key, local_msg in local_map.items():
        if key not in remote_map:
            delete_message_by_id(local_msg.id)
            deleted += 1
            continue

        remote_msg = remote_map[key]
        if (
            local_msg.read != remote_msg.read
            or local_msg.flagged != remote_msg.flagged
            or local_msg.subject != remote_msg.subject
            or local_msg.from_addr != remote_msg.from_addr
            or local_msg.to_addr != remote_msg.to_addr
            or local_msg.date != remote_msg.date
            or local_msg.body != remote_msg.body
        ):
            local_msg.subject = remote_msg.subject
            local_msg.from_addr = remote_msg.from_addr
            local_msg.to_addr = remote_msg.to_addr
            local_msg.date = remote_msg.date
            local_msg.body = remote_msg.body
            local_msg.read = remote_msg.read
            local_msg.flagged = remote_msg.flagged
            local_msg.remote_uid = remote_msg.remote_uid
            local_msg.message_id = remote_msg.message_id
            save_message(local_msg)
            updated += 1

    for key, remote_msg in remote_map.items():
        if key not in local_map:
            save_message(remote_msg)
            inserted += 1

    return {
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "remote_total": len(remote_map),
    }


def _payload_to_text(payload, charset):
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode(charset, errors="replace")
    if isinstance(payload, str):
        return payload
    return str(payload)


class _TerminalHTMLRenderer(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.href_stack = []
        self.ignored_depth = 0
        self.in_pre = False

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth > 0:
            return

        if tag == "pre":
            self.in_pre = True

        if tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag in {"p", "div", "section", "article", "header", "footer", "tr"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")

        if tag == "a":
            href = ""
            for key, value in attrs:
                if (key or "").lower() == "href" and value:
                    href = value.strip()
                    break
            self.href_stack.append(href)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in {"script", "style", "noscript"}:
            if self.ignored_depth > 0:
                self.ignored_depth -= 1
            return
        if self.ignored_depth > 0:
            return

        if tag == "a":
            href = self.href_stack.pop() if self.href_stack else ""
            if href:
                self.parts.append(f" ({href})")

        if tag == "pre":
            self.in_pre = False

        if tag in {"p", "div", "section", "article", "header", "footer", "tr", "li"}:
            self.parts.append("\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.ignored_depth > 0 or data is None:
            return
        text = html.unescape(data)
        if not self.in_pre:
            text = re.sub(r"\s+", " ", text)
        if text:
            self.parts.append(text)

    def text(self):
        raw = "".join(self.parts)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in raw.split("\n")]

        normalized = []
        previous_blank = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if not previous_blank:
                    normalized.append("")
                previous_blank = True
            else:
                normalized.append(stripped)
                previous_blank = False
        return "\n".join(normalized).strip()


def html_to_terminal_text(html_text):
    if not html_text:
        return ""
    parser = _TerminalHTMLRenderer()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        # Fallback to basic tag removal if malformed HTML breaks parsing.
        fallback = re.sub(r"<[^>]+>", " ", html_text)
        fallback = html.unescape(fallback)
        fallback = re.sub(r"\s+", " ", fallback)
        return fallback.strip()
    return parser.text()


def fetch_imap_messages(cfg, folder, fetch_limit=FETCH_LIMIT):
    if not cfg or not cfg.get("imap_host") or not cfg.get("imap_user") or not cfg.get("imap_pass"):
        return None
    try:
        imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993)) if cfg.get("imap_ssl", True) else imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
        imap.login(cfg["imap_user"], cfg["imap_pass"])
        typ, _ = imap.select(folder)
        if typ != "OK":
            typ, _ = imap.select(f'"{folder}"')
        if typ != "OK":
            imap.logout()
            return None
        # Fetch and store true IMAP UIDs so later UID commands (delete/flags)
        # target the correct server-side message.
        typ, data = imap.uid("SEARCH", None, "ALL")
        if typ != "OK":
            imap.logout()
            return None
        uids = data[0].split() if data and data[0] else []
        # Keep sync quick by optionally fetching only the newest N messages.
        # Use fetch_limit <= 0 to fetch the complete folder.
        if fetch_limit > 0:
            uids = uids[-fetch_limit:]
        messages = []
        for uid in uids:
            typ, msgdata = imap.uid("FETCH", uid, "(RFC822 FLAGS)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            raw = None
            flags = []
            if isinstance(msgdata[0], tuple) and msgdata[0][1] is not None:
                raw = msgdata[0][1]
                header = msgdata[0][0].decode("utf-8", errors="ignore")
                if "FLAGS" in header:
                    try:
                        flags_part = header.split("FLAGS (")[1].split(")")[0]
                        flags = flags_part.split()
                    except Exception:
                        flags = []
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = msg.get("Subject", "(No Subject)")
            from_addr = parseaddr(msg.get("From", ""))[1]
            to_addr = ", ".join([a[1] for a in getaddresses(msg.get_all("To", []))])
            cc_addr = ", ".join([a[1] for a in getaddresses(msg.get_all("Cc", []))])
            date = msg.get("Date", "")
            message_id = (msg.get("Message-ID", "") or "").strip()
            body = ""
            if msg.is_multipart():
                plain_parts = []
                html_parts = []
                for part in msg.walk():
                    content_disposition = part.get("Content-Disposition")
                    if content_disposition:
                        continue
                    content_type = part.get_content_type()
                    charset = part.get_content_charset() or "utf-8"
                    payload_text = _payload_to_text(part.get_payload(decode=True), charset)
                    if content_type == "text/plain":
                        plain_parts.append(payload_text)
                    elif content_type == "text/html":
                        html_parts.append(payload_text)
                if plain_parts:
                    body = "\n".join(p for p in plain_parts if p)
                elif html_parts:
                    body = html_to_terminal_text("\n".join(p for p in html_parts if p))
            else:
                charset = msg.get_content_charset() or "utf-8"
                payload_text = _payload_to_text(msg.get_payload(decode=True), charset)
                if msg.get_content_type() == "text/html":
                    body = html_to_terminal_text(payload_text)
                else:
                    body = payload_text
            read = "\\Seen" in flags
            flagged = "\\Flagged" in flags
            uid_str = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid)
            m = Message(
                None,
                folder,
                subject,
                from_addr,
                to_addr,
                cc_addr,
                "",
                date,
                body,
                read=read,
                flagged=flagged,
                remote_uid=uid_str,
                message_id=message_id,
            )
            messages.append(m)
        imap.logout()
        return messages
    except Exception:
        return None


def remote_delete_message(cfg, folder, remote_uid):
    if not remote_uid:
        return False
    if not cfg or not cfg.get("imap_host") or not cfg.get("imap_user") or not cfg.get("imap_pass"):
        return False

    try:
        imap = (
            imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993))
            if cfg.get("imap_ssl", True)
            else imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
        )
        imap.login(cfg["imap_user"], cfg["imap_pass"])

        typ, _ = imap.select(folder)
        if typ != "OK":
            typ, _ = imap.select(f'"{folder}"')
        if typ != "OK":
            imap.logout()
            return False

        uid = str(remote_uid)

        # Prefer moving to Trash when available; fallback to hard delete.
        copy_typ, _ = imap.uid("COPY", uid, "Trash")
        if copy_typ != "OK":
            copy_typ, _ = imap.uid("COPY", uid, '"Trash"')

        store_typ, _ = imap.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
        if store_typ != "OK":
            imap.logout()
            return False

        expunge_typ, _ = imap.expunge()
        imap.logout()
        return expunge_typ == "OK"
    except Exception:
        return False


def _run_delete_job(job_file):
    job_path = Path(job_file)
    try:
        payload = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        return

    source_folder = payload.get("source_folder")
    message_ids = [m for m in payload.get("message_ids", []) if isinstance(m, int)]
    if not source_folder or not message_ids:
        return

    cfg = load_config()
    id_set = set(message_ids)
    all_messages = load_messages()
    target_msgs = [m for m in all_messages if m.id in id_set]
    if not target_msgs:
        return

    attempted = 0
    cmd_ok = 0
    unverifiable = 0
    cmd_ok_by_msg_id = {}
    keys_by_msg_id = {}

    for msg in target_msgs:
        key = message_sync_key(msg)
        keys_by_msg_id[msg.id] = key
        if key is None:
            cmd_ok_by_msg_id[msg.id] = False
            unverifiable += 1
            continue

        attempted += 1
        ok = remote_delete_message(cfg, msg.folder, msg.remote_uid)
        cmd_ok_by_msg_id[msg.id] = ok
        if ok:
            cmd_ok += 1

    result_message = "Delete worker finished"

    remote_after = fetch_imap_messages(cfg, source_folder, fetch_limit=0)
    if remote_after is None:
        # If verification fetch fails, keep command-level result as best effort.
        for msg in target_msgs:
            key = keys_by_msg_id.get(msg.id)
            if key is None or cmd_ok_by_msg_id.get(msg.id, False):
                msg.folder = "Trash"
            else:
                msg.folder = source_folder
            save_message(msg)
        result_message = (
            f"Delete done: cmd-ok {cmd_ok}/{attempted}, "
            f"local-only {unverifiable}, verify refresh failed"
        )
        _write_job_result(job_path.stem, result_message, touched_folders=[source_folder, "Trash"])
        return

    remote_keys = {message_sync_key(m) for m in remote_after if message_sync_key(m) is not None}
    verified_deleted = 0
    still_on_server = 0
    for msg in target_msgs:
        key = keys_by_msg_id.get(msg.id)
        if key is None:
            msg.folder = "Trash"
        elif key in remote_keys:
            still_on_server += 1
            msg.folder = source_folder
        else:
            verified_deleted += 1
            msg.folder = "Trash"
        save_message(msg)

    result_message = (
        f"Delete done: verified {verified_deleted}/{attempted}, "
        f"still {still_on_server}, local-only {unverifiable}"
    )
    _write_job_result(job_path.stem, result_message, touched_folders=[source_folder, "Trash"])


def _run_fetch_job(job_file):
    job_path = Path(job_file)
    try:
        payload = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        return

    cfg = load_config()
    fetch_limit = int(payload.get("fetch_limit", FETCH_LIMIT))
    mode = str(payload.get("mode", "current"))

    if mode == "current":
        folder = str(payload.get("folder", "")).strip()
        if not folder:
            return
        msgs = fetch_imap_messages(cfg, folder, fetch_limit=fetch_limit)
        if msgs is None:
            _write_job_result(job_path.stem, f"Fetch failed for '{folder}'", touched_folders=[])
            return
        result = apply_folder_diff(folder, msgs)
        _write_job_result(
            job_path.stem,
            f"{folder}: +{result['inserted']} ~{result['updated']} -{result['deleted']} (remote {result['remote_total']})",
            touched_folders=[folder],
        )
        return

    if mode == "all":
        folders = [str(f) for f in payload.get("folders", []) if str(f).strip()]
        if not folders:
            return

        inserted = 0
        updated = 0
        deleted = 0
        failed = 0
        touched = []
        for folder in folders:
            msgs = fetch_imap_messages(cfg, folder, fetch_limit=0)
            if msgs is None:
                failed += 1
                continue
            result = apply_folder_diff(folder, msgs)
            touched.append(folder)
            inserted += result["inserted"]
            updated += result["updated"]
            deleted += result["deleted"]

        _write_job_result(
            job_path.stem,
            f"All folders: +{inserted} ~{updated} -{deleted} ({len(folders)-failed}/{len(folders)} folders)",
            touched_folders=touched,
        )


def start_background_fetch_job(mode, folder=None, folders=None, fetch_limit=FETCH_LIMIT):
    jobs_dir = _jobs_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    job_file = jobs_dir / f"fetch-{stamp}.json"

    payload = {"mode": mode, "fetch_limit": int(fetch_limit)}
    if folder is not None:
        payload["folder"] = folder
    if folders is not None:
        payload["folders"] = list(folders)

    try:
        job_file.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        return False, f"Could not write fetch job: {exc}"

    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--fetch-job", str(job_file)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--fetch-job", str(job_file)]

        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True, "queued"
    except Exception as exc:
        try:
            job_file.unlink()
        except Exception:
            pass
        return False, f"Could not start fetch worker: {exc}"


def start_background_delete_job(source_folder, message_ids):
    ids = [m for m in message_ids if isinstance(m, int)]
    if not ids:
        return False, "No message IDs to delete"

    jobs_dir = _jobs_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    job_file = jobs_dir / f"delete-{stamp}.json"
    payload = {"source_folder": source_folder, "message_ids": ids}

    try:
        job_file.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        return False, f"Could not write delete job: {exc}"

    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--delete-job", str(job_file)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--delete-job", str(job_file)]

        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return True, "queued"
    except Exception as exc:
        try:
            job_file.unlink()
        except Exception:
            pass
        return False, f"Could not start delete worker: {exc}"


def _guess_smtp_host(imap_host):
    if not imap_host:
        return ""
    if imap_host.lower().startswith("imap."):
        return "smtp." + imap_host[5:]
    return imap_host


def send_draft_message(cfg, draft_msg):
    if not draft_msg:
        return False, "No draft selected"

    smtp_host = cfg.get("smtp_host") or _guess_smtp_host(cfg.get("imap_host", ""))
    smtp_port = int(cfg.get("smtp_port", 587))
    smtp_ssl = bool(cfg.get("smtp_ssl", False))
    smtp_starttls = bool(cfg.get("smtp_starttls", not smtp_ssl))
    smtp_user = cfg.get("smtp_user") or cfg.get("imap_user", "")
    smtp_pass = cfg.get("smtp_pass") or cfg.get("imap_pass", "")

    if not smtp_host:
        return False, "SMTP host missing in config"

    recipients = [
        addr
        for _, addr in getaddresses([
            draft_msg.to_addr or "",
            draft_msg.cc_addr or "",
            draft_msg.bcc_addr or "",
        ])
        if addr
    ]
    if not recipients:
        return False, "Draft has no valid recipient"

    from_addr = draft_msg.from_addr or smtp_user
    if not from_addr:
        return False, "From address missing"

    email_msg = EmailMessage()
    email_msg["From"] = from_addr
    email_msg["To"] = draft_msg.to_addr or ""
    if draft_msg.cc_addr:
        email_msg["Cc"] = draft_msg.cc_addr
    email_msg["Subject"] = draft_msg.subject or "(No Subject)"
    email_msg.set_content(draft_msg.body or "")

    try:
        if smtp_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(email_msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.ehlo()
                if smtp_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(email_msg)
        return True, "Sent"
    except Exception as exc:
        return False, f"Send failed: {exc}"


def setup_configuration(stdscr):
    cfg = load_config()
    fields = [
        ("imap_host", "IMAP Host", cfg.get("imap_host", "")),
        ("imap_port", "IMAP Port", str(cfg.get("imap_port", 993))),
        ("imap_ssl", "IMAP SSL (yes/no)", "yes" if cfg.get("imap_ssl", True) else "no"),
        ("imap_user", "IMAP User", cfg.get("imap_user", "")),
        ("imap_pass", "IMAP Pass", cfg.get("imap_pass", "")),
    ]

    curses.echo()
    for i, (key, label, val) in enumerate(fields):
        stdscr.clear()
        stdscr.addstr(2, 2, "Setup: enter IMAP settings", curses.A_BOLD)
        stdscr.addstr(4, 2, f"{label} [{val}]: ")
        stdscr.refresh()
        input_val = stdscr.getstr(4, len(label) + 6, 256).decode("utf-8").strip()
        if input_val:
            if key == "imap_port":
                cfg[key] = int(input_val)
            elif key == "imap_ssl":
                cfg[key] = input_val.lower() in ("yes", "y", "true", "1")
            else:
                cfg[key] = input_val
    curses.noecho()
    save_config(cfg)
    return cfg


class TUIEmail:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.folder_index = 0
        self.message_index = 0
        self.status = "Ready"
        self.status_attr = curses.A_REVERSE
        self.header_attr = curses.A_REVERSE | curses.A_BOLD
        self.detail_scroll = 0
        self.detail_scroll_max = 0
        self.detail_body_rect = None
        self.messages = []
        self.conversations = []
        self._current_tts_proc = None
        self._current_tts_tmp_path = None
        self.quit_requested = False

        init_db()
        source_folders = load_folders()
        if source_folders:
            existing = {f.lower() for f in source_folders}
            source_folders += [f for f in FOLDERS_DEFAULT if f.lower() not in existing]
        else:
            source_folders = list(FOLDERS_DEFAULT)
        self.folders = sort_folders(source_folders)
        save_folders(self.folders)

        self.config = load_config()
        if not (self.config.get("imap_host") and self.config.get("imap_user") and self.config.get("imap_pass")):
            self.config = setup_configuration(self.stdscr)
        try:
            self.fetch_limit = max(1, int(self.config.get("fetch_limit", FETCH_LIMIT)))
        except Exception:
            self.fetch_limit = FETCH_LIMIT

        self.last_fetch = {}
        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)
        self.pending_delete_jobs = 0
        self.pending_fetch_jobs = 0
        self.quit_requested = False
        self._refresh_background_feedback()

    def request_quit(self):
        self.quit_requested = True

    def _refresh_background_feedback(self):
        try:
            jobs_dir = _jobs_dir()
            self.pending_delete_jobs = sum(1 for _ in jobs_dir.glob("delete-*.json"))
            self.pending_fetch_jobs = sum(1 for _ in jobs_dir.glob("fetch-*.json"))
        except Exception:
            self.pending_delete_jobs = 0
            self.pending_fetch_jobs = 0

        try:
            results_dir = _job_results_dir()
            result_files = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            latest_message = None
            touched_folders = set()
            for result_file in result_files:
                try:
                    payload = json.loads(result_file.read_text(encoding="utf-8"))
                    msg = str(payload.get("message", "")).strip()
                    if msg:
                        latest_message = msg
                    for folder in payload.get("touched_folders", []):
                        if isinstance(folder, str) and folder.strip():
                            touched_folders.add(folder)
                except Exception:
                    pass
                try:
                    result_file.unlink()
                except Exception:
                    pass

            if touched_folders:
                now = datetime.now()
                for folder in touched_folders:
                    self.last_fetch[folder] = now
                self.messages = load_messages(self.current_folder())
                self.conversations = build_conversations(self.messages)
                self.message_index = min(self.message_index, max(0, len(self.conversations) - 1))

            if latest_message:
                self.status = latest_message
        except Exception:
            pass

    def _insert_char(self, text, cursor, ch):
        return text[:cursor] + ch + text[cursor:], cursor + 1

    def _draw_ascii_modal_border(self, win):
        win.border(
            ord("|"),
            ord("|"),
            ord("-"),
            ord("-"),
            ord("+"),
            ord("+"),
            ord("+"),
            ord("+"),
        )

    def _handle_single_line_key(self, key, text, cursor):
        if key in (curses.KEY_LEFT,):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_RIGHT,):
            cursor = min(len(text), cursor + 1)
        elif key in (curses.KEY_HOME,):
            cursor = 0
        elif key in (curses.KEY_END,):
            cursor = len(text)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if cursor > 0:
                text = text[: cursor - 1] + text[cursor:]
                cursor -= 1
        elif key == curses.KEY_DC:
            if cursor < len(text):
                text = text[:cursor] + text[cursor + 1 :]
        elif 32 <= key <= 126:
            text, cursor = self._insert_char(text, cursor, chr(key))
        return text, cursor

    def _wrap_lines(self, lines, width):
        if width <= 0:
            return lines or [""]

        wrapped = []
        for line in lines:
            if line == "":
                wrapped.append("")
                continue

            start = 0
            while start < len(line):
                wrapped.append(line[start : start + width])
                start += width

        return wrapped or [""]

    def _cursor_to_index(self, lines, row, col):
        index = 0
        for i in range(min(row, len(lines))):
            index += len(lines[i]) + 1
        return index + col

    def _index_to_cursor(self, lines, index):
        if not lines:
            return 0, 0

        remaining = max(0, index)
        for i, line in enumerate(lines):
            line_len = len(line)
            if remaining <= line_len:
                return i, remaining
            remaining -= line_len + 1

        return len(lines) - 1, len(lines[-1])

    def _handle_body_key(self, key, lines, row, col, wrap_width=None):
        current = lines[row]

        if key == curses.KEY_LEFT:
            if col > 0:
                col -= 1
            elif row > 0:
                row -= 1
                col = len(lines[row])
        elif key == curses.KEY_RIGHT:
            if col < len(current):
                col += 1
            elif row < len(lines) - 1:
                row += 1
                col = 0
        elif key == curses.KEY_UP:
            if row > 0:
                row -= 1
                col = min(col, len(lines[row]))
        elif key == curses.KEY_DOWN:
            if row < len(lines) - 1:
                row += 1
                col = min(col, len(lines[row]))
        elif key == curses.KEY_HOME:
            col = 0
        elif key == curses.KEY_END:
            col = len(current)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if col > 0:
                lines[row] = current[: col - 1] + current[col:]
                col -= 1
            elif row > 0:
                prev_len = len(lines[row - 1])
                lines[row - 1] += current
                del lines[row]
                row -= 1
                col = prev_len
        elif key == curses.KEY_DC:
            if col < len(current):
                lines[row] = current[:col] + current[col + 1 :]
            elif row < len(lines) - 1:
                lines[row] += lines[row + 1]
                del lines[row + 1]
        elif key in (10, 13, curses.KEY_ENTER):
            left = current[:col]
            right = current[col:]
            lines[row] = left
            lines.insert(row + 1, right)
            row += 1
            col = 0
        elif 32 <= key <= 126:
            lines[row] = current[:col] + chr(key) + current[col:]
            col += 1

        if not lines:
            lines.append("")
            row = 0
            col = 0

        if wrap_width:
            cursor_index = self._cursor_to_index(lines, row, col)
            lines = self._wrap_lines(lines, wrap_width)
            row, col = self._index_to_cursor(lines, cursor_index)

        row = max(0, min(row, len(lines) - 1))
        col = max(0, min(col, len(lines[row])))
        return lines, row, col

    def _prefixed_subject(self, subject, prefix):
        subject = (subject or "").strip()
        if subject.lower().startswith(prefix.lower() + ":"):
            return subject
        return f"{prefix}: {subject}" if subject else f"{prefix}: (No Subject)"

    def _quote_body(self, text):
        lines = (text or "").splitlines() or [""]
        return "\n".join(f"> {line}" for line in lines)

    def _build_reply_seed(self, msg):
        return {
            "title": "Reply",
            "to": msg.from_addr or "",
            "cc": "",
            "bcc": "",
            "subject": self._prefixed_subject(msg.subject, "Re"),
            "body": f"\n\nOn {msg.date}, {msg.from_addr} wrote:\n{self._quote_body(msg.body)}",
        }

    def _build_reply_all_seed(self, msg):
        my_addr = (self.config.get("imap_user", "") or "").strip().lower()
        seen = set()
        to_addrs = []
        cc_addrs = []

        def add_unique(target, addr):
            clean = (addr or "").strip().lower()
            if not clean or clean == my_addr or clean in seen:
                return
            seen.add(clean)
            target.append(clean)

        add_unique(to_addrs, msg.from_addr)

        original_recipients = getaddresses([msg.to_addr or "", msg.cc_addr or ""])
        for _, addr in original_recipients:
            add_unique(cc_addrs, addr)

        return {
            "title": "Reply All",
            "to": ", ".join(to_addrs),
            "cc": ", ".join(cc_addrs),
            "bcc": "",
            "subject": self._prefixed_subject(msg.subject, "Re"),
            "body": f"\n\nOn {msg.date}, {msg.from_addr} wrote:\n{self._quote_body(msg.body)}",
        }

    def _build_forward_seed(self, msg):
        forwarded = (
            "\n\n---------- Forwarded message ----------\n"
            f"From: {msg.from_addr}\n"
            f"Date: {msg.date}\n"
            f"Subject: {msg.subject}\n"
            f"To: {msg.to_addr}\n\n"
            f"{msg.body or ''}"
        )
        return {
            "title": "Forward",
            "to": "",
            "cc": "",
            "bcc": "",
            "subject": self._prefixed_subject(msg.subject, "Fwd"),
            "body": forwarded,
        }

    def _confirm_reset_modal(self):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(10, h - 2)
        modal_w = min(80, w - 4)

    def _piper_voice_selection_modal(self, current_voice, options):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(14, h - 4)
        modal_w = min(60, w - 8)
        if modal_h < 8 or modal_w < 30:
            return current_voice

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        selected_idx = 0
        if current_voice in options:
            selected_idx = options.index(current_voice)

        while True:
            win.erase()
            self._draw_ascii_modal_border(win)
            win.addstr(0, 2, " Select Piper Voice ", curses.A_BOLD)
            win.addstr(1, 2, "Enter to select, Esc/q to cancel")

            list_h = modal_h - 4
            top = max(0, min(selected_idx - list_h + 1, selected_idx))
            for i in range(list_h):
                idx = top + i
                if idx >= len(options):
                    break
                attr = curses.A_REVERSE if idx == selected_idx else curses.A_NORMAL
                voice = options[idx]
                win.addstr(2 + i, 2, voice[: modal_w - 4].ljust(modal_w - 4), attr)

            win.refresh()
            key = win.getch()
            if key in (27, ord("q"), ord("Q")):
                return current_voice
            if key in (10, 13, curses.KEY_ENTER):
                return options[selected_idx]
            if key in (curses.KEY_UP, ord("k")):
                selected_idx = max(0, selected_idx - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected_idx = min(len(options) - 1, selected_idx + 1)
            elif key in (curses.KEY_PPAGE,):
                selected_idx = max(0, selected_idx - list_h)
            elif key in (curses.KEY_NPAGE,):
                selected_idx = min(len(options) - 1, selected_idx + list_h)

    def _confirm_reset_modal(self):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(10, h - 2)
        modal_w = min(80, w - 4)
        if modal_h < 8 or modal_w < 50:
            return False

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        while True:
            win.erase()
            self._draw_ascii_modal_border(win)
            win.addstr(0, 2, " Confirm Reset ", curses.A_BOLD)
            win.addstr(2, 2, "Delete local messages DB and config?")
            win.addstr(3, 2, "This cannot be undone.")
            win.addstr(modal_h - 2, 2, "Press y to confirm, n to cancel")
            win.refresh()

            key = win.getch()
            if key in (ord("y"), ord("Y")):
                return True
            if key in (ord("n"), ord("N"), 27, ord("q"), ord("Q")):
                return False

    def _reset_local_data_and_reconfigure(self):
        try:
            if DB_PATH.exists():
                DB_PATH.unlink()
            if CONFIG_PATH.exists():
                CONFIG_PATH.unlink()
            init_db()
            self.folders = sort_folders(list(FOLDERS_DEFAULT))
            save_folders(self.folders)
            self.folder_index = 0
            self.message_index = 0
            self.detail_scroll = 0
            self.last_fetch = {}
            self.config = setup_configuration(self.stdscr)
            try:
                self.fetch_limit = max(1, int(self.config.get("fetch_limit", FETCH_LIMIT)))
            except Exception:
                self.fetch_limit = FETCH_LIMIT
            self.messages = load_messages(self.current_folder())
            self.conversations = build_conversations(self.messages)
            self.status = "Local DB/config reset and reconfigured"
        except Exception as exc:
            self.status = f"Reset failed: {exc}"

    def settings_modal(self):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(26, h - 2)
        modal_w = min(96, w - 4)
        if modal_h < 14 or modal_w < 60:
            self.status = "Terminal too small for settings modal"
            return

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        piper_voice_options = _load_piper_voices()

        fields = [
            {"key": "imap_host", "label": "IMAP Host", "type": "text", "secret": False},
            {"key": "imap_port", "label": "IMAP Port", "type": "int", "secret": False},
            {"key": "imap_ssl", "label": "IMAP SSL", "type": "bool", "secret": False},
            {"key": "imap_user", "label": "IMAP User", "type": "text", "secret": False},
            {"key": "imap_pass", "label": "IMAP Pass", "type": "text", "secret": True},
            {"key": "smtp_host", "label": "SMTP Host", "type": "text", "secret": False},
            {"key": "smtp_port", "label": "SMTP Port", "type": "int", "secret": False},
            {"key": "smtp_ssl", "label": "SMTP SSL", "type": "bool", "secret": False},
            {"key": "smtp_starttls", "label": "SMTP STARTTLS", "type": "bool", "secret": False},
            {"key": "smtp_user", "label": "SMTP User", "type": "text", "secret": False},
            {"key": "smtp_pass", "label": "SMTP Pass", "type": "text", "secret": True},
            {"key": "piper_voice", "label": "Piper Voice", "type": "text", "secret": False},
            {"key": "piper_model_path", "label": "Piper Model Path", "type": "text", "secret": False},
            {"key": "piper_config_path", "label": "Piper Config Path", "type": "text", "secret": False},
            {"key": "fetch_limit", "label": "Fetch Limit", "type": "int", "secret": False},
            {"key": "__reset__", "label": "Reset DB + Config", "type": "action", "secret": False},
        ]

        values = {}
        for field in fields:
            key = field["key"]
            if key == "__reset__":
                continue
            default = ""
            if key == "imap_port":
                default = "993"
            elif key == "smtp_port":
                default = "587"
            elif key == "fetch_limit":
                default = str(FETCH_LIMIT)

            if field["type"] == "bool":
                values[key] = bool(self.config.get(key, key in ("imap_ssl", "smtp_starttls")))
            else:
                if key == "piper_voice":
                    values[key] = str(
                        self.config.get("piper", {}).get("voice", self.config.get("piper_voice", PIPER_DEFAULT_VOICE))
                    )
                else:
                    values[key] = str(self.config.get(key, default) if self.config.get(key, default) is not None else "")

        active = 0
        cursor = len(values.get(fields[active]["key"], "")) if fields[active]["type"] in ("text", "int") else 0
        scroll = 0
        top = 2
        bottom_reserved = 3
        visible_rows = modal_h - top - bottom_reserved

        while True:
            max_scroll = max(0, len(fields) - visible_rows)
            scroll = max(0, min(scroll, max_scroll))
            if active < scroll:
                scroll = active
            if active >= scroll + visible_rows:
                scroll = active - visible_rows + 1

            win.erase()
            self._draw_ascii_modal_border(win)
            win.addstr(0, 2, " Settings ", curses.A_BOLD)

            for i in range(visible_rows):
                idx = scroll + i
                if idx >= len(fields):
                    break
                field = fields[idx]
                y = top + i
                key = field["key"]
                attr = curses.A_REVERSE if idx == active else curses.A_NORMAL
                label = f"{field['label']}:"
                win.addstr(y, 2, label[:24].ljust(24), attr)

                if field["type"] == "action":
                    action_text = "[ Enter ] Delete DB + Config"
                    win.addstr(y, 27, action_text[: modal_w - 30].ljust(modal_w - 30), attr)
                elif field["type"] == "bool":
                    bool_text = "yes" if values[key] else "no"
                    win.addstr(y, 27, bool_text.ljust(modal_w - 30), attr)
                else:
                    raw = values[key]
                    display = ("*" * len(raw)) if field.get("secret") and raw else raw
                    win.addstr(y, 27, display[: modal_w - 30].ljust(modal_w - 30), attr)

            voices_hint = ""
            if piper_voice_options:
                sample = piper_voice_options[:6]
                voices_hint = "Piper voices: " + ", ".join(sample)
                if len(piper_voice_options) > 6:
                    voices_hint += ", ..."
            win.addstr(modal_h - 3, 2, voices_hint[: modal_w - 4])
            win.addstr(modal_h - 2, 2, "Tab/Shift+Tab or Up/Down: navigate  F2:save  F5:reset  Esc/q:close")

            active_field = fields[active]
            if active_field["type"] in ("text", "int"):
                key = active_field["key"]
                row = active - scroll
                if 0 <= row < visible_rows:
                    y = top + row
                    x = 27 + min(cursor, max(0, modal_w - 31))
                    win.move(y, x)

            win.refresh()
            key = win.getch()

            if key in (ord("q"), ord("Q")):
                self.request_quit()
                return
            if key in (27, curses.KEY_F10):
                self.status = "Settings cancelled"
                return

            if active_field["type"] in ("text", "int") and active_field["key"] == "piper_voice" and key in (10, 13, curses.KEY_ENTER):
                selected = self._piper_voice_selection_modal(values["piper_voice"], piper_voice_options)
                if selected:
                    values["piper_voice"] = selected
                continue

            if key == curses.KEY_F5:
                if self._confirm_reset_modal():
                    self._reset_local_data_and_reconfigure()
                    return
                continue

            if key == curses.KEY_F2:
                try:
                    for field in fields:
                        if field["key"] == "__reset__":
                            continue
                        fkey = field["key"]
                        if field["type"] == "bool":
                            self.config[fkey] = bool(values[fkey])
                        elif field["type"] == "int":
                            v = int((values[fkey] or "").strip())
                            if fkey in ("imap_port", "smtp_port") and not (1 <= v <= 65535):
                                raise ValueError(f"{fkey} must be 1-65535")
                            if fkey == "fetch_limit" and v <= 0:
                                raise ValueError("fetch_limit must be > 0")
                            self.config[fkey] = v
                        elif fkey in ("piper_voice", "piper_model_path", "piper_config_path"):
                            piper_cfg = self.config.get("piper", {}) if isinstance(self.config.get("piper"), dict) else {}
                            if fkey == "piper_voice":
                                piper_cfg["voice"] = values[fkey]
                            elif fkey == "piper_model_path":
                                piper_cfg["model_path"] = values[fkey]
                            elif fkey == "piper_config_path":
                                piper_cfg["config_path"] = values[fkey]
                            self.config["piper"] = piper_cfg
                        else:
                            self.config[fkey] = values[fkey]

                    save_config(self.config)
                    try:
                        self.fetch_limit = max(1, int(self.config.get("fetch_limit", FETCH_LIMIT)))
                    except Exception:
                        self.fetch_limit = FETCH_LIMIT
                    self.status = "Settings saved"
                    return
                except Exception as exc:
                    self.status = f"Settings error: {exc}"
                    continue

            if key in (9, curses.KEY_DOWN):
                active = (active + 1) % len(fields)
                active_field = fields[active]
                cursor = len(values.get(active_field["key"], "")) if active_field["type"] in ("text", "int") else 0
                continue
            if key in (curses.KEY_BTAB, curses.KEY_UP):
                active = (active - 1) % len(fields)
                active_field = fields[active]
                cursor = len(values.get(active_field["key"], "")) if active_field["type"] in ("text", "int") else 0
                continue

            active_field = fields[active]
            if active_field["type"] == "action":
                if key in (10, 13, curses.KEY_ENTER):
                    if self._confirm_reset_modal():
                        self._reset_local_data_and_reconfigure()
                        return
                continue
            if active_field["type"] == "bool":
                if key in (10, 13, curses.KEY_ENTER, ord(" "), curses.KEY_LEFT, curses.KEY_RIGHT):
                    values[active_field["key"]] = not values[active_field["key"]]
                continue
            if active_field["type"] in ("text", "int"):
                val_key = active_field["key"]
                current_text = values[val_key]
                updated_text, updated_cursor = self._handle_single_line_key(key, current_text, cursor)
                if active_field["type"] == "int":
                    if updated_text and not updated_text.isdigit():
                        continue
                values[val_key] = updated_text
                cursor = updated_cursor

    def confirm_send_modal(self, draft_msg):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(12, h - 2)
        modal_w = min(90, w - 4)
        if modal_h < 8 or modal_w < 50:
            return True

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        while True:
            win.erase()
            self._draw_ascii_modal_border(win)
            win.addstr(0, 2, " Confirm Send ", curses.A_BOLD)
            win.addstr(2, 2, f"To: {draft_msg.to_addr}"[: modal_w - 4])
            win.addstr(3, 2, f"Cc: {draft_msg.cc_addr}"[: modal_w - 4])
            win.addstr(4, 2, f"Bcc: {draft_msg.bcc_addr}"[: modal_w - 4])
            win.addstr(5, 2, f"Subject: {draft_msg.subject}"[: modal_w - 4])
            win.addstr(modal_h - 2, 2, "Send this draft? y:yes  n:no")
            win.refresh()

            key = win.getch()
            if key in (ord("y"), ord("Y"), 10, 13, curses.KEY_ENTER):
                return True
            if key in (ord("q"), ord("Q")):
                self.request_quit()
                return False
            if key in (ord("n"), ord("N"), 27, curses.KEY_EXIT):
                return False

    def compose_modal(
        self,
        title="Compose",
        initial_to="",
        initial_cc="",
        initial_bcc="",
        initial_subject="",
        initial_body="",
    ):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(24, h - 2)
        modal_w = min(88, w - 4)
        if modal_h < 16 or modal_w < 50:
            self.status = "Terminal too small for compose modal"
            return

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        to_text = initial_to or ""
        cc_text = initial_cc or ""
        bcc_text = initial_bcc or ""
        subject_text = initial_subject or ""
        body_lines = (initial_body or "").splitlines() or [""]
        active_field = 0  # 0=to, 1=cc, 2=bcc, 3=subject, 4=body
        to_cursor = len(to_text)
        cc_cursor = len(cc_text)
        bcc_cursor = len(bcc_text)
        subject_cursor = len(subject_text)
        body_row = 0
        body_col = 0
        body_scroll = 0

        to_y = 2
        cc_y = 4
        bcc_y = 6
        subject_y = 8
        body_top = 11
        body_h = modal_h - body_top - 3
        body_w = modal_w - 4
        field_x = 11
        field_w = modal_w - field_x - 2

        body_lines = self._wrap_lines(body_lines, body_w)

        prev_cursor = curses.curs_set(1)
        try:
            while True:
                win.erase()
                self._draw_ascii_modal_border(win)
                win.addstr(0, 2, f" {title} ", curses.A_BOLD)
                win.addstr(modal_h - 2, 2, "Tab/Shift+Tab: field  F2: save  F10/Esc/q: cancel")

                win.addstr(to_y, 2, "To:")
                win.addstr(cc_y, 2, "Cc:")
                win.addstr(bcc_y, 2, "Bcc:")
                win.addstr(subject_y, 2, "Subject:")
                win.addstr(body_top - 1, 2, "Body:")

                to_attr = curses.A_REVERSE if active_field == 0 else curses.A_NORMAL
                cc_attr = curses.A_REVERSE if active_field == 1 else curses.A_NORMAL
                bcc_attr = curses.A_REVERSE if active_field == 2 else curses.A_NORMAL
                subject_attr = curses.A_REVERSE if active_field == 3 else curses.A_NORMAL
                body_attr = curses.A_REVERSE if active_field == 4 else curses.A_NORMAL

                win.addstr(to_y, field_x, to_text[:field_w].ljust(field_w), to_attr)
                win.addstr(cc_y, field_x, cc_text[:field_w].ljust(field_w), cc_attr)
                win.addstr(bcc_y, field_x, bcc_text[:field_w].ljust(field_w), bcc_attr)
                win.addstr(subject_y, field_x, subject_text[:field_w].ljust(field_w), subject_attr)

                for i in range(body_h):
                    idx = body_scroll + i
                    line = body_lines[idx] if idx < len(body_lines) else ""
                    win.addstr(body_top + i, 2, line[: modal_w - 4].ljust(modal_w - 4), body_attr)

                if active_field == 0:
                    win.move(to_y, field_x + min(to_cursor, field_w - 1))
                elif active_field == 1:
                    win.move(cc_y, field_x + min(cc_cursor, field_w - 1))
                elif active_field == 2:
                    win.move(bcc_y, field_x + min(bcc_cursor, field_w - 1))
                elif active_field == 3:
                    win.move(subject_y, field_x + min(subject_cursor, field_w - 1))
                else:
                    if body_row < body_scroll:
                        body_scroll = body_row
                    if body_row >= body_scroll + body_h:
                        body_scroll = body_row - body_h + 1
                    cursor_y = body_top + (body_row - body_scroll)
                    cursor_x = 2 + min(body_col, modal_w - 5)
                    win.move(cursor_y, cursor_x)

                win.refresh()
                key = win.getch()

                if key in (ord("q"), ord("Q")):
                    self.request_quit()
                    return
                if key in (27, curses.KEY_EXIT, curses.KEY_F10):
                    self.status = "Compose cancelled"
                    return
                if key in (19, curses.KEY_F2):  # Ctrl+S or F2
                    draft = Message(
                        None,
                        "Drafts",
                        subject_text.strip() or "(No Subject)",
                        self.config.get("imap_user", ""),
                        to_text.strip(),
                        cc_text.strip(),
                        bcc_text.strip(),
                        datetime.now().isoformat(timespec="seconds"),
                        "\n".join(body_lines).rstrip(),
                        read=True,
                        flagged=False,
                    )
                    save_message(draft)
                    self.status = "Draft saved"
                    self.messages = load_messages(self.current_folder())
                    self.conversations = build_conversations(self.messages)
                    self.message_index = min(self.message_index, max(0, len(self.conversations) - 1))
                    return
                if key == 9:  # Tab
                    active_field = (active_field + 1) % 5
                    continue
                if key == curses.KEY_BTAB:
                    active_field = (active_field - 1) % 5
                    continue

                if active_field == 0:
                    if key in (10, 13, curses.KEY_ENTER):
                        active_field = 1
                    else:
                        to_text, to_cursor = self._handle_single_line_key(key, to_text, to_cursor)
                elif active_field == 1:
                    if key in (10, 13, curses.KEY_ENTER):
                        active_field = 2
                    else:
                        cc_text, cc_cursor = self._handle_single_line_key(key, cc_text, cc_cursor)
                elif active_field == 2:
                    if key in (10, 13, curses.KEY_ENTER):
                        active_field = 3
                    else:
                        bcc_text, bcc_cursor = self._handle_single_line_key(key, bcc_text, bcc_cursor)
                elif active_field == 3:
                    if key in (10, 13, curses.KEY_ENTER):
                        active_field = 4
                    else:
                        subject_text, subject_cursor = self._handle_single_line_key(
                            key, subject_text, subject_cursor
                        )
                else:
                    body_lines, body_row, body_col = self._handle_body_key(
                        key, body_lines, body_row, body_col, wrap_width=body_w
                    )
        finally:
            curses.curs_set(prev_cursor)

    def current_folder(self):
        return self.folders[self.folder_index] if self.folders else "Inbox"

    def fetch_current_folder(self):
        folder = self.current_folder()
        ok, detail = start_background_fetch_job(
            mode="current",
            folder=folder,
            fetch_limit=self.fetch_limit,
        )
        if not ok:
            self.status = f"Fetch queue failed: {detail}"
            return
        self.status = f"Fetching {folder} in background..."

    def fetch_all_folders(self):
        ok, detail = start_background_fetch_job(
            mode="all",
            folders=self.folders,
            fetch_limit=0,
        )
        if not ok:
            self.status = f"Fetch-all queue failed: {detail}"
            return
        self.status = "Fetching all folders in background..."

    def delete_selected_conversation_verified(self):
        if not self.conversations:
            self.status = "No conversation selected"
            return

        selected_convo = self.conversations[self.message_index]
        source_folder = self.current_folder()
        target_msgs = list(selected_convo.messages)
        message_ids = [msg.id for msg in target_msgs if msg.id is not None]

        ok, detail = start_background_delete_job(source_folder, message_ids)
        if not ok:
            self.status = f"Delete queue failed: {detail}"
            return

        # Optimistically move to Trash now; background worker reconciles with server state.
        for msg in target_msgs:
            msg.folder = "Trash"
            save_message(msg)

        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)
        self.message_index = min(self.message_index, max(0, len(self.conversations) - 1))
        self.detail_scroll = 0
        self.status = f"Delete queued in background ({len(message_ids)} messages)"

    def _wrap_shortcuts(self, items, max_width):
        if max_width <= 0:
            return [""]
        lines = []
        current = ""
        for item in items:
            candidate = item if not current else f"{current}  {item}"
            if len(candidate) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = item
        if current:
            lines.append(current)
        return lines or [""]

    def _wrap_body_for_width(self, body, width):
        lines = (body or "").splitlines()
        wrapped_body_lines = []
        wrap_width = max(1, width)
        url_pattern = re.compile(r"https?://\S+")

        def _tokenize_preserving_urls(text):
            tokens = []
            pos = 0
            for m in url_pattern.finditer(text):
                if m.start() > pos:
                    tokens.extend(re.findall(r"\S+", text[pos : m.start()]))
                tokens.append(m.group(0))
                pos = m.end()
            if pos < len(text):
                tokens.extend(re.findall(r"\S+", text[pos:]))
            return tokens

        def _wrap_tokens(tokens):
            if not tokens:
                return [""]
            out = []
            current = ""
            for token in tokens:
                candidate = token if not current else f"{current} {token}"
                if len(candidate) <= wrap_width or not current:
                    current = candidate
                else:
                    out.append(current)
                    current = token
            if current:
                out.append(current)
            return out

        for raw_line in lines:
            if raw_line == "":
                wrapped_body_lines.append("")
                continue
            wrapped = _wrap_tokens(_tokenize_preserving_urls(raw_line))
            wrapped_body_lines.extend(wrapped or [""])
        return wrapped_body_lines or [""]

    def _stop_tts(self):
        proc = getattr(self, "_current_tts_proc", None)
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            except Exception:
                pass
        self._current_tts_proc = None

        tmp_path = getattr(self, "_current_tts_tmp_path", None)
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
            self._current_tts_tmp_path = None

    def _speak_text_offline(self, text):
        if not text:
            return False, "No text to speak"
        text = (text or "").strip()
        if not text:
            return False, "No text after trimming"

        # Stop any currently running TTS to avoid duplicates
        self._stop_tts()

        # Prefer Piper TTS when available
        if shutil.which("piper"):
            model_path = None
            config_path = None
            piper_cfg = self.config.get("piper") if isinstance(self.config.get("piper"), dict) else {}
            if piper_cfg:
                model_path = piper_cfg.get("model_path")
                config_path = piper_cfg.get("config_path")

            model_path, config_path = _ensure_piper_voice(model_path=model_path, config_path=config_path)
            if model_path and config_path:
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp_path = tmp.name

                    cmd = ["piper", "-m", model_path, "-c", config_path, "-i", "/tmp/piper-tts-input.txt", "-f", tmp_path]
                    # write text to input file (to support spaces/newlines safely)
                    with open("/tmp/piper-tts-input.txt", "w", encoding="utf-8") as f:
                        f.write(text)

                    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    player_cmd = None
                    if shutil.which("aplay"):
                        player_cmd = ["aplay", tmp_path]
                    elif shutil.which("ffplay"):
                        player_cmd = ["ffplay", "-nodisp", "-autoexit", tmp_path]
                    elif shutil.which("play"):
                        player_cmd = ["play", tmp_path]

                    if player_cmd is None and os.name == "nt":
                        # Use PowerShell play command on Windows if available.
                        player_cmd = [
                            "powershell",
                            "-Command",
                            f"Add-Type -AssemblyName presentationCore; $player = New-Object System.Windows.Media.MediaPlayer; $player.Open([Uri]('{Path(tmp_path).as_uri()}')); $player.Play(); Start-Sleep -Seconds 10"
                        ]

                    if player_cmd is None:
                        try:
                            Path(tmp_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        return False, "Piper TTS generated audio, but no playback utility found"

                    proc = subprocess.Popen(player_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._current_tts_proc = proc
                    self._current_tts_tmp_path = tmp_path
                    return True, "Reading with Piper TTS"
                except Exception:
                    try:
                        if tmp_path:
                            Path(tmp_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                    # If piper fails at runtime, fall back.
                    pass

        # Windows built-in TTS (PowerShell) before external drivers
        if os.name == "nt":
            try:
                # Play using PowerShell speech synthesizer
                cmd = [
                    "powershell",
                    "-Command",
                    "Add-Type -AssemblyName System.Speech; $s=new-object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak(\"" + text.replace('"', '\\"') + "\");",
                ]
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_tts_proc = proc
                self._current_tts_tmp_path = None
                return True, "Reading with Windows TTS"
            except Exception:
                pass

        # Fallback drivers
        if shutil.which("espeak"):
            # Use espeak directly; it is typically offline and available via package manager.
            try:
                # Run asynchronously to keep UI responsive.
                proc = subprocess.Popen(["espeak", "-v", "en", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_tts_proc = proc
                self._current_tts_tmp_path = None
                return True, "Reading with espeak"
            except Exception as exc:
                return False, f"espeak failed: {exc}"

        if shutil.which("pico2wave") and shutil.which("aplay"):
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = tmp.name
                subprocess.run(["pico2wave", "-w", tmp_path, text], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = subprocess.Popen(["aplay", tmp_path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_tts_proc = proc
                self._current_tts_tmp_path = tmp_path
                return True, "Reading with pico2wave/aplay"
            except Exception as exc:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
                return False, f"pico2wave/aplay failed: {exc}"

        if shutil.which("say"):
            try:
                proc = subprocess.Popen(["say", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_tts_proc = proc
                self._current_tts_tmp_path = None
                return True, "Reading with say"
            except Exception as exc:
                return False, f"say failed: {exc}"

        return False, "No offline TTS engine found; install espeak or pico2wave"

    def _read_message_aloud(self, msg):
        if not msg:
            return False, "No message selected"
        snippet = []
        snippet.append(f"Subject: {msg.subject or '(No Subject)'}")
        snippet.append(f"From: {msg.from_addr or '(Unknown)'}")
        body = (msg.body or "").replace("\n", " ")
        if body:
            snippet.append(body[:400])  # limit spoken length
        text = ". ".join(snippet)
        return self._speak_text_offline(text)


    def view_message_modal(self, msg):
        h, w = self.stdscr.getmaxyx()
        modal_h = min(h - 2, 30)
        modal_w = min(w - 4, 110)
        if modal_h < 12 or modal_w < 50:
            self.status = "Terminal too small for message modal"
            return

        start_y = (h - modal_h) // 2
        start_x = (w - modal_w) // 2
        win = curses.newwin(modal_h, modal_w, start_y, start_x)
        win.keypad(True)

        body_width = modal_w - 4
        wrapped_body = self._wrap_body_for_width(msg.body or "", body_width)
        body_top = 6
        body_h = modal_h - body_top - 2
        scroll = 0

        wheel_up_mask = (
            getattr(curses, "BUTTON4_PRESSED", 0)
            | getattr(curses, "BUTTON4_RELEASED", 0)
            | getattr(curses, "BUTTON4_CLICKED", 0)
        )
        wheel_down_mask = (
            getattr(curses, "BUTTON5_PRESSED", 0)
            | getattr(curses, "BUTTON5_RELEASED", 0)
            | getattr(curses, "BUTTON5_CLICKED", 0)
        )

        while True:
            max_scroll = max(0, len(wrapped_body) - body_h)
            scroll = max(0, min(scroll, max_scroll))

            win.erase()
            self._draw_ascii_modal_border(win)
            win.addstr(0, 2, " Email View ", curses.A_BOLD)
            win.addstr(1, 2, f"Subject: {msg.subject}"[: modal_w - 4])
            win.addstr(2, 2, f"From: {msg.from_addr}"[: modal_w - 4])
            win.addstr(3, 2, f"To: {msg.to_addr}"[: modal_w - 4])
            win.addstr(4, 2, f"Date: {msg.date}"[: modal_w - 4])

            for idx, line in enumerate(wrapped_body[scroll : scroll + body_h]):
                win.addstr(body_top + idx, 2, line[:body_width].ljust(body_width))

            hint = "r:reply  a:reply-all  f:forward  t:listen  Up/Down/PgUp/PgDn/wheel scroll  Space/Esc/q close"
            win.addstr(modal_h - 1, 2, hint[: modal_w - 4])
            win.refresh()

            key = win.getch()
            if key in (ord("q"), ord("Q")):
                self.request_quit()
                return
            if key in (27, ord(" ")):
                return
            if key in (ord("r"), ord("R")):
                seed = self._build_reply_seed(msg)
                self.compose_modal(
                    title=seed["title"],
                    initial_to=seed["to"],
                    initial_cc=seed["cc"],
                    initial_bcc=seed["bcc"],
                    initial_subject=seed["subject"],
                    initial_body=seed["body"],
                )
                return
            if key in (ord("a"), ord("A")):
                seed = self._build_reply_all_seed(msg)
                self.compose_modal(
                    title=seed["title"],
                    initial_to=seed["to"],
                    initial_cc=seed["cc"],
                    initial_bcc=seed["bcc"],
                    initial_subject=seed["subject"],
                    initial_body=seed["body"],
                )
                return
            if key in (ord("f"), ord("F")):
                seed = self._build_forward_seed(msg)
                self.compose_modal(
                    title=seed["title"],
                    initial_to=seed["to"],
                    initial_cc=seed["cc"],
                    initial_bcc=seed["bcc"],
                    initial_subject=seed["subject"],
                    initial_body=seed["body"],
                )
                return
            if key in (ord("t"), ord("T")):
                ok, msg_text = self._read_message_aloud(msg)
                self.status = msg_text if ok else f"TTS failed: {msg_text}"
                continue
            if key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll = min(max_scroll, scroll + 1)
            elif key == curses.KEY_PPAGE:
                scroll = max(0, scroll - body_h)
            elif key == curses.KEY_NPAGE:
                scroll = min(max_scroll, scroll + body_h)
            elif key == curses.KEY_HOME:
                scroll = 0
            elif key == curses.KEY_END:
                scroll = max_scroll
            elif key == curses.KEY_MOUSE:
                try:
                    _, _, _, _, bstate = curses.getmouse()
                    if bstate & wheel_up_mask:
                        scroll = max(0, scroll - 3)
                    elif bstate & wheel_down_mask:
                        scroll = min(max_scroll, scroll + 3)
                except curses.error:
                    pass

    def _draw(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if h < 24 or w < 80:
            self.stdscr.addstr(0, 0, "Resize terminal to 80x24 or larger")
            self.stdscr.refresh()
            return

        yoff = 1
        username = self.config.get("imap_user", "unknown")
        year = datetime.now().year
        header_text = f"{username} | TUI Email | (C) Lucas Burlingham {year}"
        header_display = header_text[: max(0, w - 1)]
        header_x = max(0, (w - len(header_display)) // 2)
        self.stdscr.addstr(0, 0, " " * (w - 1), self.header_attr)
        self.stdscr.addstr(0, header_x, header_display, self.header_attr)

        shortcut_items = [
            "q:Quit",
            "o:Settings",
            "Space:View",
            "c:Compose",
            "R:Reply",
            "W:Forward",
            "s:SendDraft",
            "t:Listen",
            "f:Fetch",
            "F:FetchAll",
            "←/→ Folder",
            "↑/↓ Conv",
            "PgUp/PgDn:Body",
            "[ / ]:Body",
            "d:ToTrash",
            "r:ToggleRead",
        ]
        shortcut_lines = self._wrap_shortcuts(shortcut_items, w - 1)
        footer_rows = 1 + len(shortcut_lines)  # status + wrapped shortcut lines
        footer_top = h - footer_rows
        content_bottom = footer_top - 1

        folder_w = 20
        list_w = 40
        detail_w = w - folder_w - list_w - 4

        self.stdscr.addstr(yoff + 0, 0, "Folders", curses.A_BOLD | curses.A_UNDERLINE)
        for i, folder in enumerate(self.folders):
            attr = curses.A_REVERSE if i == self.folder_index else curses.A_NORMAL
            ts = self.last_fetch.get(folder)
            ts_str = ts.strftime("%H:%M") if ts else "--:--"
            label = f"{folder} [{ts_str}]"
            self.stdscr.addstr(yoff + 1 + i, 0, label[:folder_w-1].ljust(folder_w-1), attr)

        self.stdscr.addstr(yoff + 0, folder_w + 1, "Messages", curses.A_BOLD | curses.A_UNDERLINE)
        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)
        if self.message_index >= len(self.conversations):
            self.message_index = max(0, len(self.conversations) - 1)
        max_message_rows = max(0, content_bottom - (yoff + 1) + 1)
        for i, convo in enumerate(self.conversations[:max_message_rows]):
            attrs = curses.A_REVERSE if i == self.message_index else curses.A_NORMAL
            unread = convo.unread_count
            prefix = "*" if unread > 0 else " "
            from_part = convo.display_from[:10]
            subject_part = convo.subject[:18]
            count_part = f"({len(convo.messages)})"
            line = f"{prefix} {from_part:10} {subject_part:18} {count_part}"
            self.stdscr.addstr(yoff + 1 + i, folder_w + 1, line[:list_w-1], attrs)

        detail_x = folder_w + list_w + 2
        self.detail_body_rect = None
        self.detail_scroll_max = 0
        self.stdscr.addstr(yoff + 0, detail_x, "Detail", curses.A_BOLD | curses.A_UNDERLINE)
        if self.conversations:
            selected_convo = self.conversations[self.message_index]
            selected = selected_convo.latest
            self.stdscr.addstr(yoff + 1, detail_x, f"Subject: {selected_convo.subject}"[:detail_w])
            self.stdscr.addstr(yoff + 2, detail_x, f"Messages: {len(selected_convo.messages)}  Unread: {selected_convo.unread_count}"[:detail_w])
            self.stdscr.addstr(yoff + 3, detail_x, f"From: {selected.from_addr}"[:detail_w])
            self.stdscr.addstr(yoff + 4, detail_x, f"To: {selected.to_addr}"[:detail_w])
            self.stdscr.addstr(yoff + 5, detail_x, f"Cc: {selected.cc_addr}"[:detail_w])
            self.stdscr.addstr(yoff + 6, detail_x, f"Date: {selected.date}"[:detail_w])

            thread_rows = max(0, min(4, h - 17))
            self.stdscr.addstr(yoff + 8, detail_x, "Thread:", curses.A_BOLD)
            for idx, thread_msg in enumerate(selected_convo.messages[:thread_rows]):
                marker = "*" if not thread_msg.read else " "
                thread_line = f"{marker} {thread_msg.date[:12]:12} {thread_msg.from_addr[:12]:12}"
                self.stdscr.addstr(yoff + 9 + idx, detail_x, thread_line[:detail_w])

            body_start = yoff + 10 + thread_rows
            wrapped_body_lines = self._wrap_body_for_width(selected.body, detail_w)
            max_body_rows = max(0, content_bottom - body_start + 1)
            self.detail_scroll_max = max(0, len(wrapped_body_lines) - max_body_rows)
            self.detail_scroll = max(0, min(self.detail_scroll, self.detail_scroll_max))
            self.detail_body_rect = (detail_x, body_start, detail_x + detail_w - 1, content_bottom)
            visible_lines = wrapped_body_lines[self.detail_scroll : self.detail_scroll + max_body_rows]
            for idx, line in enumerate(visible_lines):
                self.stdscr.addstr(body_start + idx, detail_x, line[:detail_w])

        status_text = f"Status: {self.status}"
        if self.pending_delete_jobs > 0:
            status_text += f" | bg-delete pending: {self.pending_delete_jobs}"
        if self.pending_fetch_jobs > 0:
            status_text += f" | bg-fetch pending: {self.pending_fetch_jobs}"
        self.stdscr.addstr(footer_top, 0, " " * (w - 1), self.status_attr)
        self.stdscr.addstr(footer_top, 1, status_text[: max(0, w - 3)], self.status_attr)
        for i, line in enumerate(shortcut_lines):
            self.stdscr.addstr(footer_top + 1 + i, 0, " " * (w - 1))
            self.stdscr.addstr(footer_top + 1 + i, 0, line[: w - 1])
        self.stdscr.refresh()

    def run(self):
        curses.curs_set(0)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            self.status_attr = curses.color_pair(1) | curses.A_BOLD
            self.header_attr = curses.color_pair(2) | curses.A_BOLD
        while True:
            if self.quit_requested:
                break
            self._refresh_background_feedback()
            self._draw()
            key = self.stdscr.getch()
            if key in (ord("q"), ord("Q")):
                self._stop_tts()
                self.request_quit()
                break
            elif key in (curses.KEY_LEFT, ord("h")):
                self._stop_tts()
                self.folder_index = max(0, self.folder_index - 1)
                self.message_index = 0
                self.detail_scroll = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                self._stop_tts()
                self.folder_index = min(len(self.folders) - 1, self.folder_index + 1)
                self.message_index = 0
                self.detail_scroll = 0
            elif key in (curses.KEY_UP, ord("k")):
                self._stop_tts()
                self.message_index = max(0, self.message_index - 1)
                self.detail_scroll = 0
            elif key in (curses.KEY_DOWN, ord("j")):
                self._stop_tts()
                self.message_index = min(len(self.conversations) - 1, self.message_index + 1)
                self.detail_scroll = 0
            elif key in (curses.KEY_PPAGE, ord("[")) and self.conversations:
                self.detail_scroll = max(0, self.detail_scroll - 5)
            elif key in (curses.KEY_NPAGE, ord("]")) and self.conversations:
                self.detail_scroll = min(self.detail_scroll_max, self.detail_scroll + 5)
            elif key == curses.KEY_MOUSE and self.conversations and self.detail_body_rect:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    continue

                x1, y1, x2, y2 = self.detail_body_rect
                if not (x1 <= mx <= x2 and y1 <= my <= y2):
                    continue

                wheel_up_mask = (
                    getattr(curses, "BUTTON4_PRESSED", 0)
                    | getattr(curses, "BUTTON4_RELEASED", 0)
                    | getattr(curses, "BUTTON4_CLICKED", 0)
                )
                wheel_down_mask = (
                    getattr(curses, "BUTTON5_PRESSED", 0)
                    | getattr(curses, "BUTTON5_RELEASED", 0)
                    | getattr(curses, "BUTTON5_CLICKED", 0)
                )

                if bstate & wheel_up_mask:
                    self.detail_scroll = max(0, self.detail_scroll - 3)
                elif bstate & wheel_down_mask:
                    self.detail_scroll = min(self.detail_scroll_max, self.detail_scroll + 3)
            elif key == ord(" ") and self.conversations:
                selected = self.conversations[self.message_index].latest
                self.view_message_modal(selected)
            elif key == ord("o"):
                self.settings_modal()
            elif key == ord("f"):
                self.fetch_current_folder()
            elif key == ord("F"):
                self.fetch_all_folders()
            elif key in (ord("t"), ord("T")) and self.conversations:
                selected = self.conversations[self.message_index].latest
                ok, msg_text = self._read_message_aloud(selected)
                self.status = msg_text if ok else f"TTS failed: {msg_text}"
            elif key == ord("d") and self.conversations:
                self.delete_selected_conversation_verified()
            elif key == ord("r") and self.conversations:
                selected_convo = self.conversations[self.message_index]
                mark_read = any(not m.read for m in selected_convo.messages)
                for msg in selected_convo.messages:
                    msg.read = mark_read
                    save_message(msg)
                self.status = "Conversation marked read" if mark_read else "Conversation marked unread"
            elif key == ord("s"):
                if self.current_folder().lower() != "drafts":
                    self.status = "Open Drafts to send"
                    continue
                if not self.conversations:
                    self.status = "No draft selected"
                    continue

                draft = self.conversations[self.message_index].latest
                if not self.confirm_send_modal(draft):
                    self.status = "Send cancelled"
                    continue
                ok, message = send_draft_message(self.config, draft)
                if not ok:
                    self.status = message
                    continue

                draft.folder = "Sent"
                draft.read = True
                draft.date = datetime.now().isoformat(timespec="seconds")
                save_message(draft)

                self.messages = load_messages(self.current_folder())
                self.conversations = build_conversations(self.messages)
                self.message_index = min(self.message_index, max(0, len(self.conversations) - 1))
                self.status = f"Draft sent to {draft.to_addr or '(unknown)'}"
            elif key == ord("R") and self.conversations:
                selected = self.conversations[self.message_index].latest
                seed = self._build_reply_seed(selected)
                self.compose_modal(
                    title=seed["title"],
                    initial_to=seed["to"],
                    initial_cc=seed["cc"],
                    initial_bcc=seed["bcc"],
                    initial_subject=seed["subject"],
                    initial_body=seed["body"],
                )
            elif key == ord("W") and self.conversations:
                selected = self.conversations[self.message_index].latest
                seed = self._build_forward_seed(selected)
                self.compose_modal(
                    title=seed["title"],
                    initial_to=seed["to"],
                    initial_cc=seed["cc"],
                    initial_bcc=seed["bcc"],
                    initial_subject=seed["subject"],
                    initial_body=seed["body"],
                )
            elif key == ord("c"):
                self.compose_modal()


def headless_speak_text_offline(text, config):
    if not text:
        return False, "No text to speak"
    text = (text or "").strip()
    if not text:
        return False, "No text after trimming"

    # try piper first
    if shutil.which("piper"):
        voices = _load_piper_voices()
        voice = config.get("piper", {}).get("voice", PIPER_DEFAULT_VOICE)
        model_path, config_path = _ensure_piper_voice(None, None)
        if model_path and config_path:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp_path = tmp.name
                with open("/tmp/piper-tts-input.txt", "w", encoding="utf-8") as f:
                    f.write(text)
                subprocess.run([
                    "piper",
                    "-m",
                    model_path,
                    "-c",
                    config_path,
                    "-i",
                    "/tmp/piper-tts-input.txt",
                    "-f",
                    tmp_path,
                ], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.name == "nt":
                    player_cmd = ["powershell", "-Command", f"Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak(\"{text}\");"]
                elif shutil.which("aplay"):
                    player_cmd = ["aplay", tmp_path]
                elif shutil.which("ffplay"):
                    player_cmd = ["ffplay", "-nodisp", "-autoexit", tmp_path]
                elif shutil.which("play"):
                    player_cmd = ["play", tmp_path]
                else:
                    player_cmd = None

                if player_cmd:
                    subprocess.Popen(player_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True, "Reading with Piper TTS"
            except Exception as exc:
                if tmp_path:
                    Path(tmp_path).unlink(missing_ok=True)
                return False, f"Piper tts failed: {exc}"

    # fallback to platform voice
    if os.name == "nt":
        try:
            cmd = [
                "powershell",
                "-Command",
                f"Add-Type -AssemblyName System.Speech; $s=new-object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak(\"{text.replace('"','\\"')}\");",
            ]
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "Reading with Windows TTS"
        except Exception as exc:
            return False, f"Windows TTS failed: {exc}"

    if shutil.which("espeak"):
        try:
            subprocess.Popen(["espeak", "-v", "en", text], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "Reading with espeak"
        except Exception as exc:
            return False, f"espeak failed: {exc}"

    return False, "No TTS available"


def headless_mode():
    init_db()
    cfg = load_config()

    folders = load_folders() or FOLDERS_DEFAULT
    # Prefer Inbox by default for headless mode
    if "Inbox" in folders:
        folder_idx = folders.index("Inbox")
    else:
        folder_idx = 0

    def speak(text):
        if not text:
            return
        try:
            headless_speak_text_offline(text, cfg)
        except Exception:
            pass

    speak("TUI Email Headless Mode started")
    speak("Type help for commands")

    while True:
        current_folder = folders[folder_idx] if folders else "Inbox"
        messages = load_messages(current_folder)
        conversations = build_conversations(messages)
        status = f"Folder: {current_folder}, {len(conversations)} threads"
        print(f"\n{status}")
        speak(status)
        print("cmd> ", end="", flush=True)
        cmd = sys.stdin.readline().strip()
        if not cmd:
            continue
        parts = cmd.split()
        action = parts[0].lower()
        if action in ("q", "quit", "exit"):
            speak("Exiting headless")
            print("Exiting headless.")
            break
        if action == "help":
            help_text = "commands: help, folders, set <idx>, list, view <idx>, tts <idx>, readall, fetch, refresh"
            print(help_text)
            speak(help_text)
            continue
        if action == "folders":
            lines = []
            for i, f in enumerate(folders):
                marker = "*" if i == folder_idx else " "
                line = f"{marker} [{i}] {f}"
                lines.append(line)
                print(line)
            speak("Available folders: " + ", ".join(folders[:8]))
            continue
        if action == "set" and len(parts) > 1:
            try:
                idx = int(parts[1])
                if 0 <= idx < len(folders):
                    folder_idx = idx
            except Exception:
                pass
            continue
        if action in ("list", "ls"):
            count = len(conversations)
            speak(f"There are {count} conversations")
            for i, convo in enumerate(conversations[:8]):
                label = f"{i}: {convo.subject} ({len(convo.messages)} messages)"
                print(label)
                speak(label)
            if count > 8:
                note = f"And {count-8} more..."
                print(note)
                speak(note)
            continue
        if action == "view" and len(parts) > 1:
            try:
                idx = int(parts[1])
                if 0 <= idx < len(conversations):
                    msg = conversations[idx].latest
                    header = f"Viewing message: {msg.subject} from {msg.from_addr}"
                    body = msg.body
                    print(f"--- {header} ---")
                    print(body)
                    speak(header)
                    speak(body[:400] + ("..." if len(body) > 400 else ""))
            except Exception:
                pass
            continue
        if action == "tts" and len(parts) > 1:
            try:
                idx = int(parts[1])
                if 0 <= idx < len(conversations):
                    msg = conversations[idx].latest
                    text = f"Subject: {msg.subject}. From: {msg.from_addr}. {msg.body}"
                    ok, s = headless_speak_text_offline(text, cfg)
                    response = s if ok else f"TTS failed: {s}"
                    print(response)
                    speak(response)
            except Exception as exc:
                err = f"TTS error: {exc}"
                print(err)
                speak(err)
            continue
        if action == "readall":
            for i, convo in enumerate(conversations):
                prompt = f"Message {i}: {convo.subject}. Read this message? (y/n)"
                print(prompt)
                speak(prompt)

                while True:
                    answer = sys.stdin.readline().strip().lower()
                    if not answer:
                        continue
                    if answer in ("y", "yes"):
                        msg = convo.latest
                        text = f"Reading message {i}: {msg.subject} from {msg.from_addr}. {msg.body}"
                        print(text[:1000])
                        speak(text)
                        break
                    elif answer in ("n", "no"):
                        skip_text = "Skipped."
                        print(skip_text)
                        speak(skip_text)
                        break
                    else:
                        invalid = "Please answer y or n."
                        print(invalid)
                        speak(invalid)
            continue
        if action in ("fetch", "refresh"):
            ok, detail = start_background_fetch_job(mode="current", folder=current_folder, fetch_limit=FETCH_LIMIT)
            print(f"Fetch queued: {ok} {detail}")
            continue
        print("Unknown command. Type help.")

    return

def main(stdscr):
    init_db()
    app = TUIEmail(stdscr)
    app.run()


def cli():
    args = sys.argv[1:]
    if "--headless" in args:
        return headless_mode()

    if curses is None:
        print("Curses is not available. On Windows install windows-curses or run on UNIX-like system, or use --headless.")
        return

    if len(sys.argv) >= 3 and sys.argv[1] == "--delete-job":
        try:
            _run_delete_job(sys.argv[2])
        finally:
            try:
                Path(sys.argv[2]).unlink()
            except Exception:
                pass
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--fetch-job":
        try:
            _run_fetch_job(sys.argv[2])
        finally:
            try:
                Path(sys.argv[2]).unlink()
            except Exception:
                pass
        return
    curses.wrapper(main)


if __name__ == "__main__":
    cli()
