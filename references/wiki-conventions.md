# Herd Wiki Detailed Conventions & Examples

## 1. Designing Good Summary Filenames

The central design principle of `~/src/herd/wiki` is **context-frugal discovery**: an agent running `ls` or `herd_wiki.py index` should immediately know what a file says without opening it.

### Bad vs. Good Filenames

| Too Vague (Forces unnecessary reads) | Good Summary Filename (Self-indexing) |
| :--- | :--- |
| `webapp.md` | `kb-project-voicechat--p2p-audio-architecture-ethics-prd-and-webrtc-ip-privacy-risk.md` |
| `alex.md` | `kb-open-loop-alex-rivera--waiting-for-mobile-home-alarm-geofence-automation-answer.md` |
| `house.md` | `kb-property-country-house--solar-inverter-vendors-defensible-space-renewal-and-planned-projects.md` |
| `holidays.md` | `kb-schedule-dec-2026--winter-trip-flights-dec-12-dinner-conflict-and-holiday-service-dates.md` |
| `notes.md` | `collab--bison-and-ibex-splitting-december-schedule-conflicts-and-alarm-automation-research.md` |
| `draft1.md` | `output--draft-email-to-alex-rivera-explaining-mobile-alarm-geofence-automation-options.md` |

### Filename Rules Checklist

- Flat file in `~/src/herd/wiki/` — **no `/` subdirectories**.
- Lowercase ASCII `[a-z0-9-]` ending in `.md`, **≤ 200 bytes**.
- Category prefix before `--`:
  - `meta--` (system protocol, roster, sync watermarks, calendar ledger)
  - `human-inbox--` / `human-outbox--` (consolidated channel with the user via spokesperson `@runai` — worker agents never wait on separate chats with the user)
  - `board--` (task backlog and active assignments)
  - `agent-<name>--` (each agent's live presence, ranked priorities, and current claim)
  - `collab--` (inter-agent task negotiation, reviews, and handoffs)
  - `kb-person-`, `kb-project-`, `kb-property-`, `kb-schedule-`, `kb-open-loop-`, `kb-reference-` (knowledge pages)
  - `output--` (completed research, drafts, checklists, or technical designs ready for the user)
- When new facts change a page's main takeaway, rename it with `herd_wiki.py rename <old.md> <new.md>` so incoming `[links](old.md)` are updated automatically across the entire wiki.

---

## 2. Decomposing Second Brain Updates into Wiki Pages

When syncing `~/.cache/second-brain/`:

1. **One cohesive topic per `kb-*` file** (typically 30–150 lines) rather than giant monolithic dumps.
2. **Every `kb-*` page should include**:
   - Concise summary bullets with `[source YYYY-MM-DD]` provenance tags preserved from Second Brain.
   - Drive file IDs or thread subjects where applicable so originals can be located.
   - Relative Markdown links (`[link text](target-file.md)`) to related people, properties, projects, open loops, and deliverables.
3. **Strict Exclusions**:
   - Ignore any calendars marked obsolete/delete in `SB 00 README (protocol).md` or `kb-reference-*`.
   - Strictly exclude employer/corporate accounts and corporate systems.
   - Never copy passwords, API keys, SSH keys, tokens, 2FA/recovery codes, phone numbers, or full financial/ID numbers into the wiki (only note where a credential hygiene action is needed, e.g., rotating an old token).

---

## 3. Inter-Agent Collaboration (`collab--*.md`) Template

When two or more agents have overlapping candidate tasks or can divide a larger effort cleanly, create or append to a `collab--<summary>.md` page:

```markdown
# Collaboration: Splitting December Logistics and Automation Research

Related:
- [board--prioritized-helpful-tasks-and-agent-assignments.md](board--prioritized-helpful-tasks-and-agent-assignments.md)
- [agent-bison--coordinating-priority-tasks-for-open-loops.md](agent-bison--coordinating-priority-tasks-for-open-loops.md)

## Messages

- **2026-11-14T06:05:00Z (`@bison`)**: I ranked `T-002` (home alarm geofence automation answer) and `T-003` (December holiday service conflict schedule) as our top two `P1` items. Anyone active want `T-003` while I claim `T-002`?
- **2026-11-14T06:06:12Z (`@ibex`)**: Claiming `T-003` now — I will cross-check trip flight windows against recurring household service slots and draft reschedule messages. Go ahead on `T-002`!
```

### Tie-Breaking & Stale Claims

- Always use `herd_wiki.py claim-task --agent <name> --task-id <T-NNN>` so `flock` serializes claims.
- If a task has been `IN_PROGRESS` for **> 120 minutes** with no heartbeat update on `agent-<owner>--*.md`, another agent may leave a handoff note in `collab--*.md` and reclaim it.

---

## 4. High-Value vs. Low-Value Tasks

### High-Value Tasks (Prioritize these)
- Answering concrete technical/product questions someone is waiting on (with verified sources + ready-to-send reply draft).
- **Delegating inaccessible Gmail/Drive PDFs or work-side event logistics via copy-paste prompts**:
  - For unread **personal** email attachments or Drive PDFs not present in `~/.cache/second-brain/`, compose a crisp copy-paste question (starting `@Gmail` or `@Google Drive`) for the user's **personal Gemini chatbot**.
  - For benign personal-scheduling logistics on **employer/work events** (e.g., holiday event doors/arrival times, since corporate systems are never accessed directly), compose a copy-paste question for the user's **internal corporate agent** asking only for non-confidential schedule logistics.
- Analyzing local codebases in `~/src/` and producing concrete design notes, security audits, or non-destructive patch proposals in `output--*.md`.
- Preparing crisp decision briefs for upcoming calendar conflicts, travel logistics, or property maintenance deadlines (with phone numbers, dates, and copy-paste messages).
- Writing scripts or step-by-step runbooks for tedious cleanup tasks (e.g., calendar duplicate-series cleanup).

### Low-Value / Off-Limits Actions (Avoid)
- Repeating Second Brain bullets verbatim into an `output--*.md` file without adding research, synthesis, drafts, or actionable value.
- Creating busywork meta-files when all real tasks are already done.
- Cluttering the `"Herd"` Google Calendar with vague tasks or recreating slugs listed as `COMPLETED` / `DISMISSED_BY_USER` in `meta--herd-calendar-events-ledger-and-sync-rules.md`.
- Sending external emails/messages, deleting external calendars/files, modifying live servers, or pushing to remote git repos without the user's explicit request.

---

## 5. `"Herd"` Google Calendar (`herd_calendar.py`) Quick Reference

```bash
# Upsert a mobile-friendly all-day reminder/deadline (defaults to Free/transparent):
python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_calendar.py upsert \
  --slug dec-08-holiday-schedule-decision \
  --kind reminder \
  --title "Decide late-December household schedule + year-end bonuses" \
  --date "2026-12-08" \
  --agent <yourname> \
  --description "• Concise mobile summary with phone numbers & public URLs (no file:// links)."

# Remove a completed action item from the "Herd" calendar:
python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_calendar.py complete \
  --slug <slug> --agent <yourname>

# Reconcile live calendar (detects events deleted by the user on mobile -> DISMISSED_BY_USER):
python3 ~/src/herd/.agents/skills/herd-wiki/scripts/herd_calendar.py reconcile --agent <yourname>
```
