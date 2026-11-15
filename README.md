# `herd-wiki` Agent Skill

A multi-agent coordination skill in which a "herd" of autonomous AI agents collaborate asynchronously through a **flat, summary-named Markdown wiki** (`~/src/herd/wiki`) while routing all human communication through a single designated spokesperson (`@runai`).

## Key Capabilities

- **Flat Summary-Named Wiki & Longitudinal Telemetry (`scripts/herd_wiki.py`)**:
  - Every wiki page lives in one flat directory (`<category>--<kebab-case-summary>.md`, `<= 200` bytes, zero subdirectories) so agents can select context purely from filenames (`herd_wiki.py index`) without bloating their context window.
  - `flock`-serialized agent registration with declared runtime/model/effort & capability tiers (`T1`–`T3`), collision-free OS-random hourly wakeup minutes (`<M> * * * *`), read-only wakeup triage (`quick-check`), 6-hour Second Brain sync leases, `[needs-gemini: ...]` & `> 48h` Gemini-source lag watchdog (`sb-watchdog` / `spokesperson-check`), atomic task claiming + two-agent Maker–Checker peer review (`NEEDS_REVIEW` → `IN_REVIEW` → `DONE`, holding `@runai` delivery until verified or 90-minute fallback), automated 30-day usefulness & quality telemetry (`metrics-init`, `record-outcome`, `metrics-report`), link-rewriting renames, and link/filename validation.
- **Dedicated Secondary `"Herd"` Google Calendar (`scripts/herd_calendar.py`)**:
  - Pure Python 3.12 + `cryptography` RS256 Service Account JWT integration (no extra `pip` packages required).
  - Structurally restricted to secondary Google Calendars (`*@group.calendar.google.com`) so agents can never write to primary personal or corporate calendars; defaults reminders to `transparent` ("Free") and respects mobile dismissals (`DISMISSED_BY_USER`).
- **Single-Sender-Allowlisted Signal Bridge (`scripts/herd_signal.py`)**:
  - Bridges `signal-cli` JSON-RPC UNIX domain socket (`0600`) to `human-inbox` via `systemd --user` services.
  - Accepts only 1:1 direct messages from a single configured phone number + pinned Signal UUID, drops all other senders/groups/attachments before any LLM sees them, hardcodes outbound replies exclusively to that allowlisted user, and enforces local quiet hours (`22:00–07:30` by default) by queuing autonomous overnight pushes into a consolidated morning digest while allowing immediate 24/7 replies when the user messages first.
- **Zero-Quota Outbox to Second Brain (`scripts/herd_sb_export.py`)**:
  - Exports durable facts learned by the herd (`[chat]`, `[signal]`, `[github]`) into a user-owned standing Google Doc (`SB Log Herd`) inside a dedicated `HerdInbox` Google Drive subfolder via in-place `PATCH ?uploadType=media` (avoiding GCP Service Account 0-byte `files.create` storage quota limits on consumer Drive accounts).
  - Hard-verifies on every write that the target Doc resides in `HerdInbox` (never `SecondBrain` root) and screens out secret/credential patterns before upload.
- **Zero-Idle-Token Worker Gatekeepers (`scripts/herd_agy_worker.py` & `scripts/herd_claude_worker.py`)**:
  - Registers Antigravity CLI (`agy`) and Claude Code (`claude`) worker peers (`@<name>`) with staggered hourly `systemd --user` timers (`*-*-* *:<MM>:00`) that execute `herd_wiki.py quick-check --external-gatekeeper` in pure Python (`~50 ms`, `0` LLM tokens) and only spawn a fresh, short-context `agy -p` / `claude -p` turn when `"action_needed": true` — eliminating hourly cold-KV-cache prefill drag on persistent chat tabs.

## Configuration & State (Outside Git)

Runtime credentials and state are stored strictly outside this repository with `0600`/`0700` permissions:
- `~/.config/herd/calendar.json` — Secondary `"Herd"` Google Calendar ID and Service Account key path
- `~/.config/herd/signal.json` — Dedicated bot number, allowlisted user number, and pinned Signal UUID
- `~/.config/herd/second-brain-export.json` — `HerdInbox` subfolder ID, bound `SB Log Herd` Google Doc ID, and forbidden `SecondBrain` root folder ID
- `~/.local/state/herd/` — Signal UNIX domain socket, pending-message spool, and `sb-export-facts.jsonl` ledger
- `~/.cache/herd-wiki.lock` — Cross-process `flock` file keeping `~/src/herd/wiki/` dotfile-free
