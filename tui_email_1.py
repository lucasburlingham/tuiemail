#!/usr/bin/env python3
import curses
import json
import sqlite3
import imaplib
import email
import re
from email.utils import parseaddr, getaddresses
from pathlib import Path
from datetime import datetime

BASE_DIR = Path.home() / ".tui_email"
DB_PATH = BASE_DIR / "messages.db"
CONFIG_PATH = BASE_DIR / "config.json"
FOLDERS_DEFAULT = ["Inbox", "Sent", "Drafts", "Archive", "Flagged", "Spam", "Trash"]
FETCH_LIMIT = 30

class Message:
    def __init__(
        self,
        id,
        folder,
        subject,
        from_addr,
        to_addr,
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
            "INSERT INTO messages (folder, subject, from_addr, to_addr, date, body, is_read, is_flagged, remote_uid, message_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                msg.folder,
                msg.subject,
                msg.from_addr,
                msg.to_addr,
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
            "UPDATE messages SET folder=?, subject=?, from_addr=?, to_addr=?, date=?, body=?, is_read=?, is_flagged=?, remote_uid=?, message_id=? WHERE id=?",
            (
                msg.folder,
                msg.subject,
                msg.from_addr,
                msg.to_addr,
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
            "SELECT id, folder, subject, from_addr, to_addr, date, body, is_read, is_flagged, remote_uid, message_id FROM messages WHERE lower(folder)=lower(?) ORDER BY id DESC",
            (folder,),
        )
    else:
        c.execute(
            "SELECT id, folder, subject, from_addr, to_addr, date, body, is_read, is_flagged, remote_uid, message_id FROM messages ORDER BY id DESC"
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
                bool(r[7]),
                bool(r[8]),
                remote_uid=r[9],
                message_id=r[10],
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
        typ, data = imap.search(None, "ALL")
        if typ != "OK":
            imap.logout()
            return None
        ids = data[0].split() if data and data[0] else []
        # Keep sync quick by optionally fetching only the newest N messages.
        # Use fetch_limit <= 0 to fetch the complete folder.
        if fetch_limit > 0:
            ids = ids[-fetch_limit:]
        messages = []
        for uid in ids:
            typ, msgdata = imap.fetch(uid, "(RFC822 FLAGS)")
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
            date = msg.get("Date", "")
            message_id = (msg.get("Message-ID", "") or "").strip()
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                        charset = part.get_content_charset() or "utf-8"
                        body += _payload_to_text(part.get_payload(decode=True), charset)
            else:
                charset = msg.get_content_charset() or "utf-8"
                body = _payload_to_text(msg.get_payload(decode=True), charset)
            read = "\\Seen" in flags
            flagged = "\\Flagged" in flags
            uid_str = uid.decode("utf-8", errors="ignore") if isinstance(uid, bytes) else str(uid)
            m = Message(
                None,
                folder,
                subject,
                from_addr,
                to_addr,
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
        self.messages = []
        self.conversations = []

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

        self.last_fetch = {}
        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)

    def current_folder(self):
        return self.folders[self.folder_index] if self.folders else "Inbox"

    def fetch_current_folder(self):
        folder = self.current_folder()
        self.status = f"Fetching {folder}..."
        msgs = fetch_imap_messages(self.config, folder, fetch_limit=FETCH_LIMIT)
        if msgs is None:
            self.status = f"Fetch failed for '{folder}'"
            return
        result = apply_folder_diff(folder, msgs)
        self.messages = load_messages(folder)
        self.conversations = build_conversations(self.messages)
        self.message_index = 0
        self.last_fetch[folder] = datetime.now()
        self.status = (
            f"{folder}: +{result['inserted']} ~{result['updated']} -{result['deleted']} "
            f"(remote {result['remote_total']})"
        )

    def fetch_all_folders(self):
        self.status = "Fetching all folders..."
        inserted = 0
        updated = 0
        deleted = 0
        failed = 0
        for folder in self.folders:
            msgs = fetch_imap_messages(self.config, folder, fetch_limit=0)
            if msgs is None:
                failed += 1
                continue
            result = apply_folder_diff(folder, msgs)
            self.last_fetch[folder] = datetime.now()
            inserted += result["inserted"]
            updated += result["updated"]
            deleted += result["deleted"]
        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)
        self.message_index = 0
        self.status = (
            f"All folders: +{inserted} ~{updated} -{deleted} "
            f"({len(self.folders)-failed}/{len(self.folders)} folders)"
        )

    def _draw(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if h < 24 or w < 80:
            self.stdscr.addstr(0, 0, "Resize terminal to 80x24 or larger")
            self.stdscr.refresh()
            return

        folder_w = 20
        list_w = 40
        detail_w = w - folder_w - list_w - 4

        self.stdscr.addstr(0, 0, "Folders", curses.A_BOLD | curses.A_UNDERLINE)
        for i, folder in enumerate(self.folders):
            attr = curses.A_REVERSE if i == self.folder_index else curses.A_NORMAL
            ts = self.last_fetch.get(folder)
            ts_str = ts.strftime("%H:%M") if ts else "--:--"
            label = f"{folder} [{ts_str}]"
            self.stdscr.addstr(1 + i, 0, label[:folder_w-1].ljust(folder_w-1), attr)

        self.stdscr.addstr(0, folder_w + 1, "Messages", curses.A_BOLD | curses.A_UNDERLINE)
        self.messages = load_messages(self.current_folder())
        self.conversations = build_conversations(self.messages)
        if self.message_index >= len(self.conversations):
            self.message_index = max(0, len(self.conversations) - 1)
        for i, convo in enumerate(self.conversations[:h-6]):
            attrs = curses.A_REVERSE if i == self.message_index else curses.A_NORMAL
            unread = convo.unread_count
            prefix = "*" if unread > 0 else " "
            from_part = convo.display_from[:10]
            subject_part = convo.subject[:18]
            count_part = f"({len(convo.messages)})"
            line = f"{prefix} {from_part:10} {subject_part:18} {count_part}"
            self.stdscr.addstr(1 + i, folder_w + 1, line[:list_w-1], attrs)

        detail_x = folder_w + list_w + 2
        self.stdscr.addstr(0, detail_x, "Detail", curses.A_BOLD | curses.A_UNDERLINE)
        if self.conversations:
            selected_convo = self.conversations[self.message_index]
            selected = selected_convo.latest
            self.stdscr.addstr(1, detail_x, f"Subject: {selected_convo.subject}"[:detail_w])
            self.stdscr.addstr(2, detail_x, f"Messages: {len(selected_convo.messages)}  Unread: {selected_convo.unread_count}"[:detail_w])
            self.stdscr.addstr(3, detail_x, f"From: {selected.from_addr}"[:detail_w])
            self.stdscr.addstr(4, detail_x, f"To: {selected.to_addr}"[:detail_w])
            self.stdscr.addstr(5, detail_x, f"Date: {selected.date}"[:detail_w])

            thread_rows = max(0, min(5, h - 16))
            self.stdscr.addstr(7, detail_x, "Thread:", curses.A_BOLD)
            for idx, thread_msg in enumerate(selected_convo.messages[:thread_rows]):
                marker = "*" if not thread_msg.read else " "
                thread_line = f"{marker} {thread_msg.date[:12]:12} {thread_msg.from_addr[:12]:12}"
                self.stdscr.addstr(8 + idx, detail_x, thread_line[:detail_w])

            body_start = 9 + thread_rows
            body_lines = selected.body.splitlines()
            for idx, line in enumerate(body_lines[: max(0, h - body_start - 3)]):
                self.stdscr.addstr(body_start + idx, detail_x, line[:detail_w])

        self.stdscr.addstr(h-2, 0, "q:Quit f:Fetch F:FetchAll ←/→ Folder ↑/↓ Conv d:ToTrash r:ToggleRead    " + self.status)
        self.stdscr.refresh()

    def run(self):
        curses.curs_set(0)
        while True:
            self._draw()
            key = self.stdscr.getch()
            if key == ord("q"):
                break
            elif key in (curses.KEY_LEFT, ord("h")):
                self.folder_index = max(0, self.folder_index - 1)
                self.message_index = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.folder_index = min(len(self.folders) - 1, self.folder_index + 1)
                self.message_index = 0
            elif key in (curses.KEY_UP, ord("k")):
                self.message_index = max(0, self.message_index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.message_index = min(len(self.conversations) - 1, self.message_index + 1)
            elif key == ord("f"):
                self.fetch_current_folder()
            elif key == ord("F"):
                self.fetch_all_folders()
            elif key == ord("d") and self.conversations:
                selected_convo = self.conversations[self.message_index]
                remote_deleted_count = 0
                for msg in selected_convo.messages:
                    if remote_delete_message(self.config, msg.folder, msg.remote_uid):
                        remote_deleted_count += 1
                    msg.folder = "Trash"
                    save_message(msg)
                moved_count = len(selected_convo.messages)
                self.status = f"Conversation to Trash ({moved_count} msgs, {remote_deleted_count} remote)"
                self.messages = load_messages(self.current_folder())
                self.conversations = build_conversations(self.messages)
                self.message_index = min(self.message_index, max(0, len(self.conversations) - 1))
            elif key == ord("r") and self.conversations:
                selected_convo = self.conversations[self.message_index]
                mark_read = any(not m.read for m in selected_convo.messages)
                for msg in selected_convo.messages:
                    msg.read = mark_read
                    save_message(msg)
                self.status = "Conversation marked read" if mark_read else "Conversation marked unread"


def main(stdscr):
    init_db()
    app = TUIEmail(stdscr)
    app.run()


if __name__ == "__main__":
    curses.wrapper(main)
