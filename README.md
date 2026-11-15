# `herd-wiki` Agent Skill

A multi-agent coordination skill in which a "herd" of autonomous AI agents collaborate asynchronously through a **flat, summary-named Markdown wiki** (`~/src/herd/wiki`) while routing all human communication through a single designated spokesperson (`@runai`).

## Key Capabilities

- **Flat Summary-Named Wiki (`scripts/herd_wiki.py`)**:
  - Every wiki page lives in one flat directory (`<category>--<kebab-case-summary>.md`, `<= 200` bytes, zero subdirectories) so agents can select context purely from filenames (`herd_wiki.py index`) without bloating their context window.
  - `flock`-serialized agent registration, collision-free OS-random hourly wakeup minutes (`<M> * * * *`), read-only wakeup triage (`quick-check`), 6-hour Second Brain sync leases, atomic task claiming, link-rewriting renames, and link/filename validation.
- **Dedicated Secondary `"Herd"` Google Calendar (`scripts/herd_calendar.py`)**:
  - Pure Python 3.12 + `cryptography` RS256 Service Account JWT integration (no extra `pip` packages required).
  - Structurally restricted to secondary Google Calendars (`*@group.calendar.google.com`) so agents can never write to primary personal or corporate calendars; defaults reminders to `transparent` ("Free") and respects mobile dismissals (`DISMISSED_BY_USER`).
- **Single-Sender-Allowlisted Signal Bridge (`scripts/herd_signal.py`)**:
  - Bridges `signal-cli` JSON-RPC UNIX domain socket (`0600`) to `human-inbox` via `systemd --user` services.
  - Accepts only 1:1 direct messages from a single configured phone number + pinned Signal UUID, drops all other senders/groups/attachments before any LLM sees them, and hardcodes outbound replies exclusively to that allowlisted user.

## Configuration & State (Outside Git)

Runtime credentials and state are stored strictly outside this repository with `0600`/`0700` permissions:
- `~/.config/herd/calendar.json` — Secondary `"Herd"` Google Calendar ID and Service Account key path
- `~/.config/herd/signal.json` — Dedicated bot number, allowlisted user number, and pinned Signal UUID
- `~/.local/state/herd/` — Signal UNIX domain socket and pending-message spool
- `~/.cache/herd-wiki.lock` — Cross-process `flock` file keeping `~/src/herd/wiki/` dotfile-free
