#!/usr/bin/env python3
import curses
import os
import json
import sqlite3
import imaplib
import smtplib
import email
from email.message import EmailMessage
import threading
import time
from pathlib import Path
from datetime import datetime

BASE_DIR = Path.home() / ".tui_email"
DB_PATH = BASE_DIR / "messages.db"
CONFIG_PATH = BASE_DIR / "config.json"
FOLDERS_DEFAULT = ["Inbox", "Sent", "Drafts", "Archive", "Flagged", "Trash"]

class Message:
    def __init__(self, id, folder, subject, from_addr, to_addr, date, body, read=False, flagged=False):
        self.id = id
        self.folder = folder
        self.subject = subject
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.date = date
        self.body = body
        self.read = read
        self.flagged = flagged

    def snippet(self, length=70):
        return (self.body.replace("\n"," ")[:length-3] + "...") if len(self.body) > length else self.body


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
            is_flagged INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            name TEXT PRIMARY KEY
        )
    """)
    conn.commit()
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
    desired = ["inbox", "sent", "drafts", "archive", "flagged", "trash"]
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
            "INSERT INTO messages (folder, subject, from_addr, to_addr, date, body, is_read, is_flagged) VALUES (?,?,?,?,?,?,?,?)",
            (msg.folder, msg.subject, msg.from_addr, msg.to_addr, msg.date, msg.body, int(msg.read), int(msg.flagged)),
        )
        msg.id = c.lastrowid
    else:
        c.execute(
            "UPDATE messages SET folder=?, subject=?, from_addr=?, to_addr=?, date=?, body=?, is_read=?, is_flagged=? WHERE id=?",
            (msg.folder, msg.subject, msg.from_addr, msg.to_addr, msg.date, msg.body, int(msg.read), int(msg.flagged), msg.id),
        )
    conn.commit()
    conn.close()


def clear_folder(folder):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE folder = ?", (folder,))
    conn.commit()
    conn.close()


def load_messages(folder=None):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if folder:
        c.execute("SELECT id, folder, subject, from_addr, to_addr, date, body, is_read, is_flagged FROM messages WHERE lower(folder)=lower(?) ORDER BY id DESC", (folder,))
    else:
        c.execute("SELECT id, folder, subject, from_addr, to_addr, date, body, is_read, is_flagged FROM messages ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    msgs = []
    for r in rows:
        m = Message(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]), bool(r[8]))
        msgs.append(m)
    return msgs


def fetch_imap_folders(cfg):
    try:
        imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993)) if cfg.get("imap_ssl", True) else imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
        imap.login(cfg["imap_user"], cfg["imap_pass"])
        typ, data = imap.list()
        imap.logout()
        if typ != "OK":
            return []
        folders = []
        for item in data:
            if not item:
                continue
            try:
                line = item.decode("utf-8")
                name = line.split('"')[-2] if '"' in line else line.strip().split()[-1]
                if name:
                    folders.append(name)
            except Exception:
                continue
        return folders or FOLDERS_DEFAULT
    except Exception:
        return FOLDERS_DEFAULT


def fetch_imap_messages(cfg, folder):
    try:
        imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg.get("imap_port", 993)) if cfg.get("imap_ssl", True) else imaplib.IMAP4(cfg["imap_host"], cfg.get("imap_port", 143))
        imap.login(cfg["imap_user"], cfg["imap_pass"])
        typ, _ = imap.select(folder)
        if typ != "OK":
            typ, _ = imap.select(f'"{folder}"')
        if typ != "OK":
            imap.logout()
            return []
        typ, data = imap.search(None, "ALL")
        if typ != "OK":
            imap.logout()
            return []
        ids = data[0].split() if data and data[0] else []
        messages = []
        for uid in ids:
            typ, msgdata = imap.fetch(uid, "(RFC822 FLAGS)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            # msgdata often comes as [(b'123 (RFC822 {..}', raw_bytes), b')'] or with flag header
            raw = None
            flags = []
            if isinstance(msgdata[0], tuple) and msgdata[0][1] is not None:
                raw = msgdata[0][1]
                header = msgdata[0][0].decode("utf-8", errors="ignore")
                if "FLAGS" in header:
                    # parse list of flags from response like: 123 (RFC822 {..} FLAGS (\Seen))
                    try:
                        flags_part = header.split("FLAGS (")[1].split(")")[0]
                        flags = flags_part.split()
                    except Exception:
                        flags = []
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            subject = msg.get("Subject", "(No Subject)")
            from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
            to_addr = ", ".join([a[1] for a in email.utils.getaddresses(msg.get_all("To", []))])
            date = msg.get("Date", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                        charset = part.get_content_charset() or "utf-8"
                        body += part.get_payload(decode=True).decode(charset, errors="replace")
            else:
                charset = msg.get_content_charset() or "utf-8"
                body = msg.get_payload(decode=True).decode(charset, errors="replace")
            read = "\\Seen" in flags
            flagged = "\\Flagged" in flags
            m = Message(None, folder, subject, from_addr, to_addr, date, body, read=read, flagged=flagged)
            messages.append(m)
        imap.logout()
        return messages
    except Exception:
        return []


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
        stdscr.addstr(2, 2, "Initial setup: enter email connection settings", curses.A_BOLD)
        stdscr.addstr(4, 2, f"{label} [{val}]: ")
        stdscr.refresh()
        input_val = stdscr.getstr(4, len(label) + 6, 256).decode("utf-8").strip()
        if input_val:
            if key in ("imap_port",):
                cfg[key] = int(input_val)
            elif key == "imap_ssl":
                cfg[key] = input_val.lower() in ("yes", "y", "true", "1")
            else:
                cfg[key] = input_val
    curses.noecho()

    save_config(cfg)
    return cfg


class TUIEmail:
    def __init__(self, stdscr, config):
        self.stdscr = stdscr
        self.config = config
        self.folder_index = 0
        self.message_index = 0
        self.status = "Ready"
        self.lock = threading.Lock()
        self.messages = []
        self.mode = "list"
        self.compose_data = None
        self.compose_field_idx = 0
        self.compose_cursor = 0

        init_db()
        source_folders = load_folders() or fetch_imap_folders(self.config)
        self.folders = sort_folders(source_folders)
        if not load_folders():
            save_folders(self.folders)

        self.messages = load_messages(self.current_folder())
        self.start_sync()

    def current_folder(self):
        return self.folders[self.folder_index] if self.folders else "Inbox"

    def sync_all(self):
        with self.lock:
            self.status = "Syncing folders..."

        folders = fetch_imap_folders(self.config)
        if folders:
            self.folders = sort_folders(folders)
            save_folders(self.folders)

        # Always sync inbox first so user sees the main folder refreshed quickly
        ordered_folders = [f for f in self.folders if f.lower() == "inbox"] + [f for f in self.folders if f.lower() != "inbox"]

        count = 0
        for folder in ordered_folders:
            clear_folder(folder)
            msgs = fetch_imap_messages(self.config, folder)
            for m in msgs:
                save_message(m)
            count += len(msgs)
            with self.lock:
                # update visible messages gradually as folders are synced
                if folder.lower() == self.current_folder().lower():
                    self.messages = load_messages(self.current_folder())
                    self.message_index = 0
                    self.status = f"Synced {count} messages; current folder '{folder}' done"

        with self.lock:
            self.messages = load_messages(self.current_folder())
            self.status = f"Synced {count} messages"
            self.message_index = 0

    def _start_compose(self):
        self.mode = "compose"
        self.compose_field_idx = 0
        self.compose_cursor = 0
        self.compose_data = {
            "from": self.config.get("imap_user", ""),
            "to": "",
            "cc": "",
            "subject": "",
            "body": "",
        }

    def _save_draft_and_exit(self):
        d = self.compose_data
        date = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = Message(None, "Drafts", d["subject"], d["from"], d["to"], date, d["body"], read=False, flagged=False)
        save_message(msg)
        self.mode = "list"
        self.status = "Draft saved"

    def _send_draft_message(self):
        if self.current_folder().lower() != "drafts":
            self.status = "Send works from Drafts only"
            return
        if not self.messages:
            self.status = "No message to send"
            return

        m = self.messages[self.message_index]
        if m.folder.lower() != "drafts":
            self.status = "Selected message is not a draft"
            return

        cfg = self.config
        try:
            smtp_host = cfg.get("smtp_host")
            smtp_port = cfg.get("smtp_port", 587)
            smtp_user = cfg.get("smtp_user")
            smtp_pass = cfg.get("smtp_pass")
            smtp_ssl = cfg.get("smtp_ssl", False)

            if smtp_ssl:
                server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10)
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
                server.ehlo()
                try:
                    server.starttls()
                except Exception:
                    pass

            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)

            msg_obj = EmailMessage()
            msg_obj["From"] = m.from_addr or smtp_user or ""
            msg_obj["To"] = m.to_addr
            if m.cc:
                msg_obj["Cc"] = m.cc
            msg_obj["Subject"] = m.subject
            msg_obj.set_content(m.body)

            recipients = [addr.strip() for addr in m.to_addr.split(",") if addr.strip()]
            if m.cc:
                recipients += [addr.strip() for addr in m.cc.split(",") if addr.strip()]

            server.send_message(msg_obj, to_addrs=recipients)
            server.quit()

            m.folder = "Sent"
            m.read = True
            save_message(m)
            self.status = f"Sent draft to {m.to_addr}"

            # refresh view
            self.messages = load_messages(self.current_folder())
            self.message_index = min(self.message_index, max(0, len(self.messages) - 1))
        except Exception as e:
            self.status = f"Send failed: {e}"

    def _draw_compose(self):
        h, w = self.stdscr.getmaxyx()
        win_w = min(80, w - 4)
        win_h = min(20, h - 4)
        x0 = (w - win_w) // 2
        y0 = (h - win_h) // 2
        self.stdscr.attron(curses.A_REVERSE)
        for i in range(win_h):
            self.stdscr.addstr(y0 + i, x0, " " * win_w)
        self.stdscr.attroff(curses.A_REVERSE)

        fields = ["from", "to", "cc", "subject", "body"]
        titles = {"from": "From", "to": "To", "cc": "Cc", "subject": "Subject", "body": "Body"}
        for idx, key in enumerate(fields):
            is_current = idx == self.compose_field_idx
            label = f"> {titles[key]}: " if is_current else f"  {titles[key]}: "
            value = self.compose_data.get(key, "")
            # body field multi-line handling
            if key == "body":
                if is_current:
                    self.stdscr.addstr(y0 + 2 + idx, x0 + 1, label, curses.A_BOLD)
                    body_lines = value.split("\n")
                    for i,line in enumerate(body_lines[:win_h-8]):
                        self.stdscr.addstr(y0 + 3 + idx + i, x0 + 3, line[:win_w-6])
                else:
                    self.stdscr.addstr(y0 + 2 + idx, x0 + 1, label + value.split("\n")[0][:win_w-10])
            else:
                attr = curses.A_REVERSE if is_current else curses.A_NORMAL
                disp = value[:win_w - len(label) - 3]
                self.stdscr.addstr(y0 + 2 + idx, x0 + 1, label + disp, attr)

        tip = "Esc=save draft/close | Tab=next field | Arrows navigate text"
        self.stdscr.addstr(y0 + win_h - 2, x0 + 2, tip[:win_w-4], curses.A_DIM)

    def _handle_compose_input(self, key):
        fields = ["from", "to", "cc", "subject", "body"]
        current = fields[self.compose_field_idx]
        value = self.compose_data[current]

        if key == 27:  # ESC
            self._save_draft_and_exit()
            return

        if key == 9:  # TAB
            self.compose_field_idx = (self.compose_field_idx + 1) % len(fields)
            self.compose_cursor = min(self.compose_cursor, len(self.compose_data[fields[self.compose_field_idx]]))
            return

        if key == curses.KEY_UP:
            self.compose_field_idx = max(0, self.compose_field_idx - 1)
            self.compose_cursor = min(self.compose_cursor, len(self.compose_data[fields[self.compose_field_idx]]))
            return
        if key == curses.KEY_DOWN:
            self.compose_field_idx = min(len(fields) - 1, self.compose_field_idx + 1)
            self.compose_cursor = min(self.compose_cursor, len(self.compose_data[fields[self.compose_field_idx]]))
            return

        if key == curses.KEY_LEFT:
            self.compose_cursor = max(0, self.compose_cursor - 1)
            return
        if key == curses.KEY_RIGHT:
            self.compose_cursor = min(len(value), self.compose_cursor + 1)
            return

        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.compose_cursor > 0:
                self.compose_data[current] = value[: self.compose_cursor - 1] + value[self.compose_cursor :]
                self.compose_cursor -= 1
            return

        if key in (10, 13):
            if current == "body":
                self.compose_data[current] = value[: self.compose_cursor] + "\n" + value[self.compose_cursor :]
                self.compose_cursor += 1
            else:
                self.compose_field_idx = min(len(fields) - 1, self.compose_field_idx + 1)
                self.compose_cursor = min(len(self.compose_data[fields[self.compose_field_idx]]), self.compose_cursor)
            return

        if 32 <= key <= 126:
            self.compose_data[current] = value[: self.compose_cursor] + chr(key) + value[self.compose_cursor :]
            self.compose_cursor += 1
            return

    def start_sync(self):
        threading.Thread(target=self.sync_all, daemon=True).start()

    def draw(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if h < 24 or w < 80:
            self.stdscr.addstr(0, 0, "Resize terminal to 80x24 or larger")
            self.stdscr.refresh()
            return

        if self.mode == "compose":
            self._draw_compose()
            self.stdscr.refresh()
            return

        folder_w = 20
        list_w = 40
        detail_w = w - folder_w - list_w - 4

        self.stdscr.addstr(0, 0, "Folders", curses.A_BOLD | curses.A_UNDERLINE)
        for i, folder in enumerate(self.folders):
            attr = curses.A_REVERSE if i == self.folder_index else curses.A_NORMAL
            self.stdscr.addstr(1 + i, 0, folder[:folder_w-1].ljust(folder_w-1), attr)

        self.stdscr.addstr(0, folder_w + 1, "Messages", curses.A_BOLD | curses.A_UNDERLINE)
        self.messages = load_messages(self.current_folder())
        for i, msg in enumerate(self.messages[:h-6]):
            attrs = curses.A_REVERSE if i == self.message_index else curses.A_NORMAL
            prefix = "*" if not msg.read else " "
            line = f"{prefix} {msg.from_addr[:12]:12} {msg.subject[:20]:20}"
            self.stdscr.addstr(1 + i, folder_w + 1, line[:list_w-1], attrs)

        detail_x = folder_w + list_w + 2
        self.stdscr.addstr(0, detail_x, "Detail", curses.A_BOLD | curses.A_UNDERLINE)
        if self.messages:
            selected = self.messages[self.message_index]
            self.stdscr.addstr(1, detail_x, f"Subject: {selected.subject}"[:detail_w])
            self.stdscr.addstr(2, detail_x, f"From: {selected.from_addr}"[:detail_w])
            self.stdscr.addstr(3, detail_x, f"To: {selected.to_addr}"[:detail_w])
            self.stdscr.addstr(4, detail_x, f"Date: {selected.date}"[:detail_w])
            body_lines = selected.body.splitlines()
            for idx, line in enumerate(body_lines[:h-10]):
                self.stdscr.addstr(6 + idx, detail_x, line[:detail_w])

        self.stdscr.addstr(h-2, 0, f"q:Quit u:Update ←/→ Folder ↑/↓ Msg d:Delete r:ToggleRead n:Compose s:SendDraft {self.status}")
        self.stdscr.refresh()

    def run(self):
        curses.curs_set(0)
        while True:
            self.draw()
            key = self.stdscr.getch()
            if self.mode == "compose":
                self._handle_compose_input(key)
                continue
            if key == ord("q"):
                break
            elif key == ord("u"):
                self.status = "Refreshing..."
                self.start_sync()
            elif key in (curses.KEY_LEFT, ord("h")):
                self.folder_index = max(0, self.folder_index - 1)
                self.message_index = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.folder_index = min(len(self.folders) - 1, self.folder_index + 1)
                self.message_index = 0
            elif key in (curses.KEY_UP, ord("k")):
                self.message_index = max(0, self.message_index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.message_index = min(len(self.messages) - 1, self.message_index + 1)
            elif key == ord("n"):
                self._start_compose()
            elif key == ord("s"):
                self._send_draft_message()
            elif key == ord("d") and self.messages:
                m = self.messages[self.message_index]
                m.folder = "Trash"
                save_message(m)
                self.messages = load_messages(self.current_folder())
                self.message_index = min(self.message_index, max(0, len(self.messages) - 1))
            elif key == ord("r") and self.messages:
                m = self.messages[self.message_index]
                m.read = not m.read
                save_message(m)
                self.status = "Marked read" if m.read else "Marked unread"


def main(stdscr):
    cfg = load_config()
    if not all([cfg.get("imap_host"), cfg.get("imap_user"), cfg.get("imap_pass")]):
        cfg = setup_configuration(stdscr)
    init_db()
    app = TUIEmail(stdscr, cfg)
    app.run()


if __name__ == "__main__":
    curses.wrapper(main)
