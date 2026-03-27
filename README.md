# TUI Email Client

A lightweight terminal-based email client with Outlook-like workflow and secure local storage.

## Features

- Folder view: `Inbox`, `Sent`, `Drafts`, `Archive`, `Flagged`, `Trash`
- Conversation view inside each folder (threads grouped by subject)
- Scrollable detail pane with wrapped message bodies
- Scrollable full-message modal viewer
- Safe terminal rendering for HTML-only emails
- Message operations:
  - `o`: Open Settings modal (reconfigure IMAP/SMTP/basic options)
  - `d`: Delete selected conversation with remote verification and immediate refresh
  - `r`: Toggle Read/Unread for selected conversation
  - `s`: Send selected draft (from Drafts)
  - `R`: Reply to selected message (opens prefilled compose)
  - `W`: Forward selected message (opens prefilled compose)
  - `f`: Refresh current folder from server
  - `F`: Refresh all folders from server
  - `c`: Compose new message (opens compose modal)
    - In compose modal: `To`, `Cc`, `Bcc`, `Subject`, `Body` fields
    - `Tab`/`Shift+Tab` switch fields, arrow keys move cursor, `F2` saves draft, `F10`/`Esc`/`q` cancel
  - `Space`: Open selected email in scrollable modal viewer
    - In message modal: `r` reply, `a` reply all, `f` forward
    - `Up`/`Down`, `PgUp`/`PgDn`, `Home`/`End`, and mouse wheel scroll
  - In Settings modal: `F2` saves config, `F5` resets (deletes local DB + config, then re-setup)
  - Sending with `s` shows a confirmation dialog (`y`/`n`) before sending
  - `q`: Quit
  - arrow keys / `hjkl` for navigation
  - `[` / `]` and `PgUp` / `PgDn` scroll the side detail pane
- SQLite persistence for messages in `~/.tui_email/messages.db`
- Config stored in `~/.tui_email/config.json`

## Initial setup

On first run, the client will prompt for:

- IMAP host/port/ssl/username/password

For sending drafts with `s`, SMTP defaults are inferred from IMAP settings. You can add optional overrides in `~/.tui_email/config.json`:

- `smtp_host`
- `smtp_port` (default `587`)
- `smtp_ssl` (default `false`)
- `smtp_starttls` (default `true` when `smtp_ssl` is `false`)
- `smtp_user` / `smtp_pass` (default to IMAP credentials)
- `fetch_limit` (default `30`)

This is a one-time setup. The config is written to `~/.tui_email/config.json`.

## How to run

```bash
cd ~/Projects/tuiemail
python3 tui_email.py
```

## Build a distributable binary

This project can be distributed as a standalone Linux console binary with PyInstaller.

Install build dependency:

```bash
cd ~/Projects/tuiemail
./.venv/bin/python -m pip install -r requirements-build.txt
```

Build with the included spec:

```bash
./.venv/bin/python -m PyInstaller --clean tuiemail.spec
```

Or use the helper script:

```bash
sh build-pyinstaller.sh
```

The generated binary is written to:

```bash
dist/tuiemail
```

Notes:

- Build on Linux for Linux distribution.
- The binary still uses `~/.tui_email/config.json` and `~/.tui_email/messages.db` at runtime.
- `curses` is a system capability on Linux, so build and test on a target-like environment.
- A GitHub Actions workflow is included at `.github/workflows/release.yml` to build and upload a Linux binary for version tags like `v1.0.0`.

## Storage and security

- `~/.tui_email/config.json` stores IMAP connection info (host/port/SSL/user/pass).
- `~/.tui_email/messages.db` is plain SQLite, used directly by the app.
- The implementation currently does not encrypt message contents or the database on disk.

## Data flow

- App loads messages from database on startup (`load_messages`).
- Folders are loaded from DB (`load_folders`) and the UI starts from cached local state.
- UI is shown immediately using cached data from DB.
- `f` refreshes the current folder from IMAP.
- `F` refreshes all folders from IMAP, then reconciles local data with server state.
- Read/unread/delete actions are persisted to DB immediately via `save_message`.
- Delete now attempts remote removal, auto-refreshes the source folder, and reports verified server outcome in the status line.
- Multipart messages prefer `text/plain`; HTML-only messages are converted to terminal-safe text.

## Dependencies

- Python 3.8+
- `curses` (std lib; on Linux built-in)

## Troubleshooting

- Ensure terminal is at least `80x24`.
- Ensure IMAP credentials are correct and reachable.
- If folder sync fails, check network and server settings.
- If delete reports messages still on server, verify that the remote mailbox supports the IMAP delete/expunge flow used by the account.
- If mouse wheel scrolling does not work, confirm your terminal forwards mouse events to curses applications.
