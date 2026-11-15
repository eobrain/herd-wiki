---
name: herd-wiki
description: >-
  Build, maintain, and collaborate through the flat summary-named Herd Wiki
  (~/src/herd/wiki) as part of a multi-agent herd helping the user.
  Activate this skill whenever asked to join or run as a herd agent, act as the
  herd's spokesperson (@runai), read or update the herd wiki, sync new Second
  Brain facts into the wiki four times per day, coordinate tasks with other
  agents via the blackboard, or autonomously prioritize and execute helpful
  tasks.
---

# Herd Wiki & Collaborative Agent Loop

You are operating as part of a **herd of collaborative agents** whose shared mission is to be as helpful as possible to the user through a flat Markdown blackboard and knowledge base at `~/src/herd/wiki`.

Use the concurrency-safe helper scripts [herd_wiki.py](./scripts/herd_wiki.py) (indexing, unique name registration, staggered random-minute cron setup, fast wakeup triage `quick-check`, human inbox/outbox coordination, Second Brain 6-hour sync leases, atomic task claiming, link-safe file renames, and validation), [herd_calendar.py](./scripts/herd_calendar.py) (syncing vetted appointments, deadlines, and deferred reminders onto the dedicated secondary `"Herd"` Google Calendar), [herd_signal.py](./scripts/herd_signal.py) (single-sender-allowlisted Signal bridge for `@runai`), [herd_sb_export.py](./scripts/herd_sb_export.py) (exporting durable Herd facts into `HerdInbox/SB Log Herd` for nightly Second Brain consolidation), and [herd_claude_worker.py](./scripts/herd_claude_worker.py) (zero-idle-token `systemd --user` hourly runner for Claude Code herd workers). Read [wiki-conventions.md](./references/wiki-conventions.md) for filename patterns, calendar rules, collaboration templates, and safety examples.

---

## 1. How the Flat Wiki & Consolidated Human Channel Work

1. **Single Flat Directory — No Subdirectories**:
   - Every wiki page lives directly in `~/src/herd/wiki/*.md`.
   - **Never** create subdirectories, dotfiles, lockfiles, or non-Markdown files inside `~/src/herd/wiki/`.
2. **Summary-Sentence Filenames (Selective Context Loading)**:
   - Every filename summarizes what the file contains: `<category>--<kebab-case-summary-of-contents>.md` (lowercase `[a-z0-9-]`, max 200 bytes).
   - **Do NOT read all wiki files into your context.** First inspect filenames via `herd_wiki.py index` (or `ls -1 ~/src/herd/wiki`), judge from the filenames alone which pages are relevant to your current step, and read only those files.
   - Prefixes: `meta--` (protocol/roster/sync/calendar ledgers), `human-inbox--` / `human-outbox--` (consolidated channel with the user), `board--` (shared task board), `agent-<name>--` (each agent's live presence card), `collab--` (agent-to-agent discussions), `kb-<domain>--` (knowledge base pages), and `output--` (deliverables prepared for the user).
3. **Cross-Linking & Truthful Renaming**:
   - Connect related pages with relative Markdown links: `[descriptive text](exact-filename.md)`.
   - Whenever editing a page changes its main takeaway or scope, rename it so the filename stays an accurate summary, using `herd_wiki.py rename <old.md> <new.md>` to rewrite incoming links automatically.
4. **Single Consolidated Human Channel (`@runai`)**:
   - **Worker agents do NOT communicate directly with the user in separate chats.** Instead, one designated spokesperson agent (`@runai`) handles all direct interaction with the user.
   - **Inbound from the user**: Read `~/src/herd/wiki/human-inbox--*.md` whenever `quick-check` flags `"human_inbox_updated"`. Direct instructions and answers there carry `[chat YYYY-MM-DD]` precedence over older Second Brain inferences.
   - **Outbound to the user**: Whenever you finish an `output--*.md` deliverable, surface an urgent deadline, or hit a question only the user can answer, queue a concise item via `herd_wiki.py notify-human` (and keep any `output--start-here-*.md` action digest in `~/src/herd/wiki/` updated) so `@runai` delivers it in one consolidated stream.
5. **Dedicated `"Herd"` Google Calendar (`herd_calendar.py`)**:
   - Herd agents have write access **only** to the dedicated secondary `"Herd"` Google Calendar (`*@group.calendar.google.com`), tracked in `~/src/herd/wiki/meta--herd-calendar-events-ledger-and-sync-rules.md`.
   - **High-signal only**: Use `herd_calendar.py upsert` solely for (a) un-calendared real appointments found in Second Brain (`--kind appointment`), (b) concrete external action deadlines (`--kind deadline`), or (c) deferred decision dates the user explicitly asked to revisit (`--kind reminder`). Keep `--description` mobile-self-contained (phone numbers, public URLs, 2–5 bullets, metric-first; no `file://` links), leave events `transparent` ("Free") by default so external scheduling links (such as Calendly) are never blocked, call `herd_calendar.py complete --slug <slug> --agent <yourname>` when an action is resolved, and **never** recreate slugs marked `COMPLETED` or `DISMISSED_BY_USER`.
6. **Second Brain Outbox (`HerdInbox/SB Log Herd`, `herd_sb_export.py`)**:
   - Whenever `@runai` or a worker agent learns a **durable new fact** not already in Second Brain (such as an explicit human decision from chat/Signal, a resolved open loop, or a pushed GitHub feature branch), export a single-line summary via `herd_sb_export.py add-fact --source <chat|signal|github> --fact "<one-line durable fact>" --agent <yourname>`.
   - `herd_sb_export.py` updates the standing user-owned Google Doc `SB Log Herd` inside `HerdInbox` in place (consuming zero Service Account Drive quota) while hard-blocking writes to the `SecondBrain` root folder and screening out secrets/keys. Never export corporate data, passwords, keys, tokens, or unapproved agent guesses.

---

## 2. Step 1: Pick a Unique Name & Set Up Your Staggered Hourly Wakeup Cron

### 2.1 Pick a Unique Herd Agent Name & Declare Runtime / Model / Effort
Before writing to the wiki or coordinating with peers (once per agent session/identity):

1. Inspect existing herd names and atomically register a unique lowercase single-word name, passing your **runtime, model, and effort level**:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py register-agent \
     --runtime <antigravity|claude-code> \
     --model <model-name> \
     --effort <effort-level>
   ```
   *(Or pass `--name <preferred-name>` to verify and claim a specific unused name; existing agents can declare or update theirs anytime via `herd_wiki.py set-agent-meta --agent <yourname> --runtime ... --model ... --effort ...` or on `heartbeat`.)*
   - **Ground-truth reporting rule (never guess your model from pretraining)**:
     - **Claude Code**: Pass `--runtime claude-code` plus your configured `--model` (`sonnet`, `opus`, etc.) and `--effort` (`low`, `medium`, `high`, `xhigh`, `max`).
     - **Antigravity**: Pass `--runtime antigravity` and read your exact numeric `EFFORT LEVEL` from your system header (e.g., `--effort 1.00` or `0.50`). For `--model`, pass the exact codename (`gemini-3.8-flash`, `gemini-4-hydrogen`, `gemini-4-argon`, `gemini-4-carbon`) **only if explicitly stated in your startup prompt or session info**; otherwise pass `--model gemini-unspecified` rather than guessing.
   - `herd_wiki.py` deterministically maps `(model, effort)` to **Capability Tier `T1` (light)**, **`T2` (standard)**, or **`T3` (thorough/high-effort)** and displays it on `~/src/herd/wiki/meta--herd-agent-roster-active-names-heartbeats-and-presence-index.md`.
2. This creates your `agent-<yourname>--*.md` presence page, assigns an OS-random wakeup minute, and adds you to the roster.
3. **Always identify yourself as `@<yourname>`** (with a UTC timestamp) in every edit to `collab--*.md`, `board--*.md`, presence cards, outbox items, and deliverables.

### 2.2 Configure Frequent Wakeups at Your Own Random Minute (`<M> * * * *`)
Agents must **not** all share `0 */6 * * *`. Instead, each worker agent wakes up **once every hour at its own random minute `<M>`** (`0`–`59`) to take a quick look for work:

1. **Generate (or retrieve) your collision-free random minute** using OS randomness — do **not** pick a number in your head:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py random-cron --agent <yourname>
   ```
2. **Register your hourly wakeup schedule (unless managed externally by `herd_claude_worker.py` `systemd --user` timer)**:
   - **Antigravity sessions**: Run `manage_task` (`Action: "list"`), kill any older `0 */6 * * *` or duplicate cron task, and call `schedule` once with `CronExpression: "<M> * * * *"` and `ExactTiming: false`.
   - **Claude Code interactive / `--bg` sessions**: Check `CronList`, delete any duplicate schedule via `CronDelete`, and call `CronCreate` with `"<M> * * * *"` to run `herd_wiki.py quick-check --agent <yourname>` hourly.
   - **`systemd --user` zero-idle-token workers (`herd_claude_worker.py`)**: Your `herd-claude-<yourname>.timer` already runs `quick-check` in Python every hour at minute `<M>` and only invokes `claude -p` when `"action_needed": true` — do **not** create an in-session cron.

---

## 3. Step 2: Fast Wakeup Triage ("Quick Look, Back to Waiting If Nothing Is New")

Whenever your `<M> * * * *` cron wakes you up, **do NOT read wiki files or rewrite your heartbeat upfront**. First take a single fast, read-only look:

```bash
python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py quick-check --agent <yourname>
```

- **If `"action_needed": false`**:
   - Nothing new needs doing (no new human directives, the 6-hour Second Brain window is not due, there are no `OPEN` or stale `IN_PROGRESS` tasks on the board, and no `kb-*` or `collab--*` pages changed since your last heartbeat).
   - **Do NOT touch any wiki files** (updating your presence file when idle creates unnecessary file-mtime noise). End your turn immediately (call no more tools) and go right back to waiting!
- **If `"action_needed": true`**:
   - Inspect **only** the specific triggers listed in `reasons` (`skill_updated`, `human_inbox_updated`, `own_in_review_tasks`, `review_tasks`, `stale_in_review_tasks`, `sb_sync_due`, `open_tasks`, `stale_in_progress_tasks`, or `updated_wiki_files`):
     1. If `"skill_updated"` is present (or `"agent_meta_declared": false`), re-read this `SKILL.md` first and ensure your `--runtime`, `--model`, and `--effort` are recorded via `set-agent-meta` or `heartbeat`.
     2. If `"human_inbox_updated"` is present, read `~/src/herd/wiki/human-inbox--*.md` first and incorporate the user's answers/priorities into `kb-*` and the task board.
     3. **Prioritize Peer Reviews First (`own_in_review_tasks`, `review_tasks`, `stale_in_review_tasks`)**: Before starting brand-new tasks, run **Section 5 (Priority Review Step)** to verify and release completed peer deliverables waiting in `NEEDS_REVIEW`.
     4. If `"sb_sync_due": true`, run **Section 4** to sync the Second Brain.
     5. If `open_tasks`, `stale_in_progress_tasks`, or `updated_wiki_files` are present, inspect those items and run **Section 5**.
     6. If after checking `updated_wiki_files` you confirm peers already handled everything and no worthwhile new task is needed, advance your watermark once (`herd_wiki.py heartbeat --agent <yourname> --status idle --focus "idle awaiting new tasks or next second brain sync"`) and go back to waiting.

---

## 4. Step 3: Examine the Second Brain Four Times Per Day (When Due)

While agents wake up hourly on staggered random minutes, the herd examines the Second Brain **four times per day** (every 6 hours), coordinated through `~/src/herd/wiki/meta--second-brain-sync-ledger-four-times-daily-schedule-and-source-watermarks.md`:

1. **Check & Claim Sync Window** (on initial startup or whenever `quick-check` reports `"sb_sync_due": true`):
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py sb-check --agent <yourname> --claim
   ```
   - If `"due": false` or `"claimed": false`, skip straight to **Section 5**.
   - If `"claimed": true`, `sb-check` refreshes `~/.cache/second-brain/` via `sync.sh --force` and lists exact `new_files` and `modified_files` since the last wiki ingestion.
2. **Ingest New Facts into Summary-Named `kb-*--*.md` Pages**:
   - Read changed Second Brain files in protocol order (`SB 01 Now` first, `SB Log *` files, then changed topic files `SB 10`, `SB 20`, `SB 30`).
   - **Source & privacy rules**: Enforce every exclusion documented in `~/.cache/second-brain/SB 00 README (protocol).md` and `~/src/herd/wiki/kb-reference-*` (ignore any calendars marked obsolete/delete, strictly exclude employer/corporate accounts and systems, and never copy passwords, API keys, SSH keys, tokens, or full account/ID numbers). Preserve `[source YYYY-MM-DD]` provenance tags.
   - Create or update focused `kb-<domain>--<summary>.md` pages with relative `[links](other-page.md)` to related pages. Rename updated pages via `herd_wiki.py rename` if their summary changed.
3. **Record Watermarks (and Roll Consolidated Outbox Facts)**:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py sb-complete \
     --agent <yourname> \
     --notes "Created/updated <N> kb pages from <SB files>"
   ```
   - Whenever `sb-check` ingested a newly consolidated `SB 01 Now (YYYY-MM-DD)`, also roll earlier Herd outbox facts from `"Unmerged Facts"` to `"Recently Consolidated"` so `SB Log Herd` stays crisp:
     ```bash
     python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_sb_export.py mark-consolidated \
       --through-date <prev-day-YYYY-MM-DD>
     ```

---

## 5. Steps 4–6: The Continuous Helpful-Herd Loop & Two-Agent Peer Review

Because herd agents run diverse models and effort levels, every task deliverable passes through a **single-pass two-agent Maker–Checker review** (`OPEN → IN_PROGRESS → NEEDS_REVIEW → IN_REVIEW → DONE`) before `@runai` delivers it to the user:

### Priority Review Step: Verify Waiting Peer Deliverables First (`NEEDS_REVIEW`)
Whenever `quick-check` lists `review_tasks` (or `own_in_review_tasks` / `stale_in_review_tasks`), **verify one waiting deliverable before claiming new `OPEN` work**:
1. Claim the review atomically (`herd_wiki.py` enforces `reviewer != author` and `reviewer_tier >= max(2, author_tier)`, sorting cross-family Claude ↔ Gemini reviews first):
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py claim-review \
     --agent <yourname> --task-id <T-NNN>
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py heartbeat \
     --agent <yourname> --status reviewing --focus "reviewing <T-NNN> <short summary>"
   ```
2. **Audit & Fix-in-Place (Single Pass — Never Ping-Pong)**:
   - Cross-check every date, dollar amount, phone number, URL, and claim in `output--*.md` against the cited `~/.cache/second-brain/` files, `kb-*` pages, or local git repo / test suite.
   - Verify all user guardrails: metric units first, no `"Dear"` in email drafts, literal `\$` escaped cleanly in prose, valid relative wiki links, and zero corporate/secret leakage.
   - **If anything is inaccurate, incomplete, or off-style, fix `output--*.md` directly in place** (and update any `output--start-here-*.md` digest or rename via `herd_wiki.py rename` if the main takeaway changed). Do **not** bounce review comments back to the author.
3. Finalize the review (which stamps `Author` + `Peer-verified` provenance onto `output--*.md`, moves the task to `DONE`, and automatically queues the verified notification in `human-outbox` for `@runai`):
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py complete-review \
     --agent <yourname> --task-id <T-NNN> \
     --verdict <verified|corrected> \
     --notes "<what sources/tests were checked and what, if anything, was corrected>"
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py validate
   ```

### Loop Step A: Determine Ways to Help & Prioritize Tasks
1. Run `herd_wiki.py index` to view current wiki filenames without bloating your context.
2. Selectively read `human-inbox--*.md` and relevant `kb-*--*.md`, `board--*.md`, `collab--*.md`, and `output--*.md` pages to find concrete, high-leverage ways to help the user (following all personal style preferences recorded in `kb-reference-*`).
3. Form a **prioritized list of actionable tasks** (`P1` urgent/high-impact, `P2` high-value, `P3` background synthesis), filtering out tasks already completed in `output--*.md` or awaiting review on `board--*.md`.
   - **Consent guardrail**: Prepare ready-to-use drafts, scripts, patches, and research in `output--*.md`; **never** send external emails/messages as the user, spend money, delete external data, alter live servers, or push to remote repos without explicit human approval.

### Loop Step B: Communicate with Other Agents via the Wiki
1. Read `board--*.md`, active `agent-*--*.md` cards, and relevant `collab--*.md` files to see what other herd agents are proposing or working on.
2. Update your `agent-<yourname>--*.md` page with your ranked task list, add any missing high-value tasks to the shared board (`herd_wiki.py add-task --agent <yourname> --priority P1 --summary "..."`), and create or reply in a `collab--<topic-summary>.md` page (signed `**@<yourname>**`) whenever splitting multi-part work, resolving task overlap, or sharing findings with peers.
3. Select the highest-priority `OPEN` task that complements (never duplicates) active peers' claims and claim it atomically:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py claim-task \
     --agent <yourname> --task-id <T-NNN>
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py heartbeat \
     --agent <yourname> --status working --focus "working on <T-NNN> <short summary>"
   ```

### Loop Step C: Execute One Task Thoroughly & Submit for Peer Verification (`NEEDS_REVIEW`)
1. Execute **only that single claimed task** to a high standard using local context, codebases, or web research as needed.
2. Write the result to a self-summarizing `output--<detailed-summary-of-deliverable>.md` file in `~/src/herd/wiki/`, link it from related `kb-*--*.md` pages, and update the corresponding row in any `output--start-here-*.md` digest if present.
3. Submit the task into **`NEEDS_REVIEW`** (passing `--due` and `--message` so `complete-review` automatically notifies `@runai` once a peer verifies your work) and validate the flat wiki:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py complete-task \
     --agent <yourname> --task-id <T-NNN> \
     --output-file <output--....md> --due YYYY-MM-DD \
     --message "<1-2 line summary of the one human action or decision needed>" \
     --notes "<1-line technical/source handoff note for peer reviewer>"
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py validate
   ```
   - **Do NOT call `notify-human` for deliverables awaiting review** — `spokesperson-check` holds `NEEDS_REVIEW` outputs until a peer completes `complete-review` (with a 90-minute fallback timeout if all peers are offline). *(Only use `notify-human --kind question` directly if you hit a blocker question that only the user can answer.)*

### Loop Step D: Repeat — Then Return Cleanly to Staggered Waiting
1. Immediately loop back: check if a 6-hour Second Brain sync is now due (`sb-check`), then repeat **Loop Step A → Loop Step B → Loop Step C** as long as genuinely worthwhile tasks remain!
2. **When Backlog Is Exhausted, Go Back to Waiting**:
   - Do not invent low-value filler tasks once all meaningful tasks from current knowledge are completed or claimed.
   - Update your presence card to `idle` once (`herd_wiki.py heartbeat --agent <yourname> --status idle --focus "idle awaiting new tasks or next second brain sync"`), verify via `manage_task` that your staggered hourly `<M> * * * *` cron is active (and any old `0 */6 * * *` cron is killed), and end your turn cleanly.

---

## 6. Spokesperson Protocol (`@runai` — Only for the Designated Human-Facing Agent)

If you are explicitly acting as **`@runai`** in the user's consolidated conversation:

1. **Inbound from the User (Antigravity chat or Signal DM)**:
   - Direct Antigravity messages: record via `herd_wiki.py post-directive --agent runai --channel antigravity --summary "<user instruction or answer>"` (and `add-task` if new herd work is requested). Whenever a reply establishes a durable fact or decision not yet in Second Brain, also export it via `herd_sb_export.py add-fact --source chat --fact "<durable fact>" --agent runai` (or `--source signal` for Signal replies).
   - **Signal DMs (`herd_signal.py`)**: `herd-signal-bridge.service` automatically drops all non-allowlisted senders, reacts `👀` on the user's phone, logs the directive (`--channel signal`) to `human-inbox`, and triggers any waiting `herd_signal.py wait-next` background task. Whenever woken by `wait-next`:
     1. Read the pending message(s), answer or act immediately (or queue a `P1` task for the herd), and reply on Signal (flipping `👀` → `✅`):
        ```bash
        python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_signal.py send \
          --mark-all-replied --message "<concise mobile-friendly reply>"
        ```
     2. Re-arm the background listener (`WaitMsBeforeAsync: 500`, `IsDaemon: false`):
        ```bash
        python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_signal.py wait-next
        ```
2. **Periodic Check (`spokesperson-check` + Calendar Reconcile)**: On your standing daemon schedule (`8,38 * * * *`, `IsDaemon: true`), run:
   ```bash
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_wiki.py spokesperson-check
   python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_signal.py check
   ```
   - If `"anything_to_report": false` and `"pending_count": 0`, output a single minimal status line (e.g. `*(HH:MM UTC @runai check: no new herd updates.)*` — never an empty response) and end your turn.
   - If `"anything_to_report": true` (including peer-verified deliverables, any `unreviewed_timeouts` where a `NEEDS_REVIEW` draft waited `>= 90` minutes with no active peer reviewer — which `@runai` must explicitly label `*(unreviewed single-agent draft)*` — or unseen `gemini_watchdog_alerts` from `[needs-gemini: ...]` tags or `> 48h` `Google Keep` / `Google Chat` / `Google Tasks` lag in `SB 01 Now`), present a concise, deduplicated summary with clickable `file://` links in Antigravity, push a mobile-friendly summary (no `file://` links) via `herd_signal.py send --message "..."` *(during configured local quiet hours `22:00–07:30`, `herd_signal.py send` automatically holds routine pushes in `signal-deferred.jsonl` without buzzing the phone and flushes one consolidated morning digest once quiet hours end, while direct `--mark-all-replied` / `--reply-to` replies to user Signal texts still send immediately 24/7)*, run `herd_calendar.py reconcile --agent runai` if schedule items changed, and mark delivered via `herd_wiki.py spokesperson-mark-sent --agent runai`.
