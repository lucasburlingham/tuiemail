# TUI Email Client

A lightweight terminal-based email client with Outlook-like workflow and secure local storage.

## Features

- Folder view: `Inbox`, `Sent`, `Drafts`, `Archive`, `Flagged`, `Trash`
- Conversation view inside each folder (threads grouped by subject)
- Message operations:
  - `d`: Move selected conversation to Trash
  - `r`: Toggle Read/Unread for selected conversation
  - `s`: Send selected draft (from Drafts)
  - `u`: Refresh from server (Update)
  - `c`: Compose new message (opens compose modal)
    - In compose modal: `Tab`/`Shift+Tab` switch fields, `F2` saves draft, `F10`/`Esc`/`q` cancel
  - `q`: Quit
  - arrow keys / hjkl for navigation
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

This is a one-time setup. The config is written to `~/.tui_email/config.json`.

## How to run

```bash
cd ~/Projects/tuiemail
python3 tui_email.py
```

## Storage and security

- `~/.tui_email/config.json` stores IMAP connection info (host/port/SSL/user/pass).
- `~/.tui_email/messages.db` is plain SQLite, used directly by the app.
- The implementation currently does not encrypt message contents or the database on disk.

## Data flow

- App loads messages from database on startup (`load_messages`).
- Folders are loaded from DB (`load_folders`) or from IMAP (`fetch_imap_folders`) at startup.
- UI is shown immediately using cached data from DB.
- Background sync (`sync_all`) updates folders sequentially with Inbox first, writing to DB using `save_message`.
- `p` key triggers manual refresh via `sync_all`.
- Read/unread/delete actions are persisted to DB immediately via `save_message`.

## Dependencies

- Python 3.8+
- `curses` (std lib; on Linux built-in)

## Troubleshooting

- Ensure terminal is at least `80x24`.
- Ensure IMAP credentials are correct and reachable.
- If folder sync fails, check network and server settings.
