#!/usr/bin/env python3
"""Concurrency-safe helper CLI for the flat summary-named Herd Wiki."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys

DEFAULT_WIKI_DIR = Path(
    os.environ.get("HERD_WIKI_DIR", str(Path.home() / "src/herd/wiki"))
)
DEFAULT_SB_CACHE_DIR = Path(
    os.environ.get("HERD_SB_CACHE_DIR", str(Path.home() / ".cache/second-brain"))
)
DEFAULT_SB_SYNC_SCRIPT = Path(
    os.environ.get(
        "HERD_SB_SYNC_SCRIPT",
        str(Path.home() / ".gemini/config/skills/second-brain/scripts/sync.sh"),
    )
)
DEFAULT_LOCK_PATH = Path(
    os.environ.get("HERD_WIKI_LOCK", str(Path.home() / ".cache/herd-wiki.lock"))
)

ROSTER_FILE = "meta--herd-agent-roster-active-names-heartbeats-and-presence-index.md"
SB_LEDGER_FILE = (
    "meta--second-brain-sync-ledger-four-times-daily-schedule-and-source-watermarks.md"
)
DEFAULT_BOARD_FILE = "board--prioritized-helpful-tasks-and-agent-assignments.md"
CONVENTIONS_FILE = (
    "meta--herd-wiki-conventions-flat-summary-filenames-and-blackboard-protocol.md"
)
DEFAULT_HUMAN_INBOX_FILE = "human-inbox--directives-answers-and-priorities.md"
DEFAULT_HUMAN_OUTBOX_FILE = (
    "human-outbox--unread-herd-updates-questions-and-deliverables.md"
)


def resolve_singleton_file(wiki_dir: Path, pattern: str, default_name: str) -> str:
    """Resolve an existing singleton wiki file by prefix glob, falling back to default_name."""
    if wiki_dir.exists():
        matches = sorted(p.name for p in wiki_dir.glob(pattern) if p.is_file())
        if len(matches) == 1:
            return matches[0]
        if default_name in matches:
            return default_name
        if matches:
            return matches[0]
    return default_name


def board_filename(wiki_dir: Path) -> str:
    return resolve_singleton_file(wiki_dir, "board--*.md", DEFAULT_BOARD_FILE)


def human_inbox_filename(wiki_dir: Path) -> str:
    return resolve_singleton_file(
        wiki_dir, "human-inbox--*.md", DEFAULT_HUMAN_INBOX_FILE
    )


def human_outbox_filename(wiki_dir: Path) -> str:
    return resolve_singleton_file(
        wiki_dir, "human-outbox--*.md", DEFAULT_HUMAN_OUTBOX_FILE
    )

MAX_FILENAME_BYTES = 200
FILENAME_RE = re.compile(r"^[a-z0-9]+(?:-+[a-z0-9]+)*\.md$")
AGENT_FILE_RE = re.compile(r"^agent-([a-z0-9]+)--(.+)\.md$")
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,24}$")

HERD_NAME_POOL = [
    "addax", "alpaca", "antelope", "aurochs", "banteng", "bison", "blesbok",
    "bongo", "brumby", "buffalo", "camel", "capybara", "caribou", "chamois",
    "clydesdale", "eland", "fallow", "gaur", "gazelle", "gemsbok", "gerenuk",
    "guanaco", "hartebeest", "ibex", "impala", "kobus", "kudu", "lechwe",
    "llama", "markhor", "mouflon", "muskox", "nilgai", "nyala", "okapi",
    "onager", "oryx", "peccary", "pronghorn", "puku", "quagga", "reindeer",
    "roan", "saiga", "sambar", "serow", "springbok", "steinbok", "tahr",
    "takin", "tapir", "topi", "tur", "urial", "vicuna", "wapiti",
    "waterbuck", "wildebeest", "wisent", "yak", "zebu",
]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso_utc(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value or value in ("—", "-", "null", "None"):
        return None
    clean = value.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(clean)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def slugify(text: str, max_len: int = 120) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] or slug[:max_len]
    return slug or "status"


class FileLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def atomic_write(path: Path, content: str) -> None:
    # Write temporary file in cache/lock dir or parent? Never leave dotfiles in wiki_dir if interrupted;
    # use same filesystem if possible, or rename cleanly. Let's check same-dir hidden temp vs ~/.cache:
    # Same-dir is guaranteed same-filesystem for atomic os.replace, and cleaned up in finally!
    tmp_name = f"tmp-atomic-write-{os.getpid()}-{random.randint(1000, 9999)}.tmp"
    tmp_path = path.parent / tmp_name
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def validate_filename(name: str) -> list[str]:
    errors: list[str] = []
    if "/" in name or "\\" in name:
        errors.append(f"'{name}': must not contain path separators (flat wiki).")
    if name.startswith("."):
        errors.append(f"'{name}': dotfiles are not allowed in the wiki.")
    if not name.endswith(".md"):
        errors.append(f"'{name}': must end with '.md'.")
    byte_len = len(name.encode("utf-8"))
    if byte_len > MAX_FILENAME_BYTES:
        errors.append(
            f"'{name}': {byte_len} bytes exceeds {MAX_FILENAME_BYTES}-byte limit."
        )
    if not FILENAME_RE.match(name):
        errors.append(
            f"'{name}': must use lowercase letters, digits, and hyphens only "
            "(e.g. 'kb-topic--summary-of-contents.md')."
        )
    return errors


def replace_between_markers(
    text: str, start_marker: str, end_marker: str, new_inner: str
) -> str:
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    replacement = f"{start_marker}\n{new_inner.rstrip()}\n{end_marker}"
    if not pattern.search(text):
        raise RuntimeError(f"Markers {start_marker} .. {end_marker} not found.")
    return pattern.sub(lambda _: replacement, text, count=1)


def extract_between_markers(text: str, start_marker: str, end_marker: str) -> str:
    pattern = re.compile(
        re.escape(start_marker) + r"(.*?)" + re.escape(end_marker),
        re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        raise RuntimeError(f"Markers {start_marker} .. {end_marker} not found.")
    return m.group(1).strip()


def strip_fenced_blocks(md_text: str) -> str:
    return re.sub(r"```.*?```", "", md_text, flags=re.DOTALL)


def rewrite_links_in_wiki(wiki_dir: Path, old_name: str, new_name: str) -> int:
    """Update relative Markdown links pointing to old_name across all .md files."""
    updated_count = 0
    escaped = re.escape(old_name)
    link_re = re.compile(
        r"(\]\()(?:\./)?" + escaped + r"((?:#[^)\s]*)?\))"
    )
    for md_file in sorted(wiki_dir.glob("*.md")):
        original = md_file.read_text(encoding="utf-8")
        rewritten = link_re.sub(rf"\g<1>{new_name}\g<2>", original)
        if rewritten != original:
            atomic_write(md_file, rewritten)
            updated_count += 1
    return updated_count


def parse_agent_cards(wiki_dir: Path) -> dict[str, dict[str, str]]:
    """Return mapping of agent_name -> metadata from agent-<name>--*.md files."""
    agents: dict[str, dict[str, str]] = {}
    for p in sorted(wiki_dir.glob("agent-*--*.md")):
        m = AGENT_FILE_RE.match(p.name)
        if not m:
            continue
        name = m.group(1)
        body = p.read_text(encoding="utf-8")
        status_m = re.search(r"^- \*\*Status\*\*:\s*(.+)$", body, re.MULTILINE)
        hb_m = re.search(
            r"^- \*\*Last Heartbeat \(UTC\)\*\*:\s*(.+)$", body, re.MULTILINE
        )
        cron_m = re.search(
            r"^- \*\*Wakeup Cron\*\*:\s*(.+)$", body, re.MULTILINE
        )
        agents[name] = {
            "name": name,
            "filename": p.name,
            "status": status_m.group(1).strip().strip("`") if status_m else "active",
            "heartbeat": hb_m.group(1).strip().strip("`") if hb_m else iso_utc(
                dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
            ),
            "wakeup_cron": cron_m.group(1).strip().strip("`") if cron_m else "",
        }
    return agents


def pick_random_cron(
    wiki_dir: Path, exclude_agent: str | None = None
) -> tuple[int, str]:
    """Pick a true OS-random minute (0..59) avoiding minutes used by peers when possible."""
    used_minutes: set[int] = set()
    for name, info in parse_agent_cards(wiki_dir).items():
        if exclude_agent and name == exclude_agent:
            continue
        cron_str = info.get("wakeup_cron", "")
        # Ignore legacy '0 */6 * * *' when checking staggered hourly minutes
        if cron_str and "*/6" not in cron_str:
            m = re.match(r"^(\d{1,2})\s+\*", cron_str)
            if m:
                val = int(m.group(1))
                if 0 <= val <= 59:
                    used_minutes.add(val)
    available = [m for m in range(60) if m not in used_minutes]
    pool = available if available else list(range(60))
    minute = random.SystemRandom().choice(pool)
    return minute, f"{minute} * * * *"


def sync_roster_table(wiki_dir: Path) -> None:
    roster_path = wiki_dir / ROSTER_FILE
    if not roster_path.exists():
        return
    agents = parse_agent_cards(wiki_dir)
    lines = [
        "| Agent Name | Status | Last Heartbeat (UTC) | Presence Page |",
        "| :--- | :--- | :--- | :--- |",
    ]
    if not agents:
        lines.append("| *(none yet)* | — | — | — |")
    else:
        for name in sorted(agents):
            info = agents[name]
            fn = info["filename"]
            lines.append(
                f"| `{name}` | `{info['status']}` | `{info['heartbeat']}` | [{fn}]({fn}) |"
            )
    text = roster_path.read_text(encoding="utf-8")
    updated = replace_between_markers(
        text,
        "<!-- ROSTER_TABLE_START -->",
        "<!-- ROSTER_TABLE_END -->",
        "\n".join(lines),
    )
    atomic_write(roster_path, updated)


def collect_taken_names(wiki_dir: Path) -> set[str]:
    taken = set(parse_agent_cards(wiki_dir).keys())
    for fname in (ROSTER_FILE, board_filename(wiki_dir)):
        fpath = wiki_dir / fname
        if fpath.exists():
            txt = fpath.read_text(encoding="utf-8")
            for m in re.finditer(r"`([a-z][a-z0-9]{1,24})`", txt):
                cand = m.group(1).lower()
                if not cand.startswith("t-") and cand not in {
                    "open", "done", "p1", "p2", "p3", "active", "idle", "working",
                    "coordinating", "none",
                }:
                    taken.add(cand)
    return taken


def cmd_index(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    if not wiki_dir.exists():
        print(f"ERROR: Wiki directory does not exist: {wiki_dir}", file=sys.stderr)
        return 1

    categories: dict[str, list[Path]] = {
        "meta": [],
        "human": [],
        "board": [],
        "agent": [],
        "collab": [],
        "kb": [],
        "output": [],
        "other": [],
    }
    anomalies: list[str] = []

    for entry in sorted(wiki_dir.iterdir(), key=lambda p: p.name):
        if entry.is_dir():
            anomalies.append(f"SUBDIRECTORY (forbidden): {entry.name}/")
            continue
        if not entry.name.endswith(".md") or entry.name.startswith("."):
            anomalies.append(f"NON-MARKDOWN/DOTFILE (forbidden): {entry.name}")
            continue
        prefix = entry.name.split("--", 1)[0]
        if prefix == "meta":
            categories["meta"].append(entry)
        elif prefix.startswith("human-"):
            categories["human"].append(entry)
        elif prefix == "board":
            categories["board"].append(entry)
        elif prefix.startswith("agent-"):
            categories["agent"].append(entry)
        elif prefix == "collab":
            categories["collab"].append(entry)
        elif prefix.startswith("kb-") or prefix == "kb":
            categories["kb"].append(entry)
        elif prefix == "output":
            categories["output"].append(entry)
        else:
            categories["other"].append(entry)

    total = sum(len(v) for v in categories.values())
    print(f"Herd Wiki: {wiki_dir} ({total} markdown files)")
    labels = [
        ("meta", "Meta & Protocol"),
        ("human", "Human Channel (Inbox & Outbox)"),
        ("board", "Task Boards"),
        ("agent", "Agent Presence Cards"),
        ("collab", "Agent Collaboration Threads"),
        ("kb", "Knowledge Base Pages"),
        ("output", "Prepared Deliverables for Human"),
        ("other", "Other Pages"),
    ]
    for key, title in labels:
        items = categories[key]
        if not items:
            continue
        print(f"\n[{title} ({len(items)})]")
        for p in items:
            st = p.stat()
            mtime = iso_utc(dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc))
            print(f"  - {p.name}  ({st.st_size} B, {mtime})")

    if anomalies:
        print("\nWARNINGS / INVARIANT VIOLATIONS:", file=sys.stderr)
        for a in anomalies:
            print(f"  ! {a}", file=sys.stderr)
        return 1
    return 0


def cmd_register_agent(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    wiki_dir.mkdir(parents=True, exist_ok=True)

    with FileLock(args.lock_file):
        taken = collect_taken_names(wiki_dir)
        chosen: str | None = args.name.lower().strip() if args.name else None

        if chosen:
            if not AGENT_NAME_RE.match(chosen):
                print(
                    f"ERROR: Invalid agent name '{chosen}'. Use lowercase single word [a-z0-9] (2-25 chars).",
                    file=sys.stderr,
                )
                return 2
            if chosen in taken:
                avail = [n for n in HERD_NAME_POOL if n not in taken][:6]
                print(
                    f"ERROR: Agent name '{chosen}' is already taken ({sorted(taken)}). "
                    f"Available suggestions: {', '.join(avail)}",
                    file=sys.stderr,
                )
                return 1
        else:
            avail = [n for n in HERD_NAME_POOL if n not in taken]
            if avail:
                chosen = random.SystemRandom().choice(avail)
            else:
                idx = 1
                while f"herd{idx}" in taken:
                    idx += 1
                chosen = f"herd{idx}"

        focus_slug = slugify(args.focus or "joined-herd-assessing-wiki-and-tasks", 110)
        filename = f"agent-{chosen}--{focus_slug}.md"
        errs = validate_filename(filename)
        if errs:
            print("\n".join(errs), file=sys.stderr)
            return 2

        now = iso_utc()
        minute, wakeup_cron = pick_random_cron(wiki_dir, exclude_agent=chosen)
        b_file = board_filename(wiki_dir)
        content = f"""# Agent Presence: `{chosen}`

- **Agent Name**: `{chosen}`
- **Status**: `active`
- **Last Heartbeat (UTC)**: {now}
- **Wakeup Cron**: `{wakeup_cron}`
- **Current Focus**: {args.focus or "Joined herd; reviewing wiki and Second Brain state."}

Related coordination pages:
- [{ROSTER_FILE}]({ROSTER_FILE})
- [{b_file}]({b_file})
- [{SB_LEDGER_FILE}]({SB_LEDGER_FILE})
- [{CONVENTIONS_FILE}]({CONVENTIONS_FILE})

## My Prioritized Candidate Tasks

1. *(Populated after scanning wiki filenames and Second Brain state.)*

## Active Coordination Notes

- **{now} (`@{chosen}`)**: Registered in the herd and ready to coordinate.
"""
        atomic_write(wiki_dir / filename, content)
        sync_roster_table(wiki_dir)
        print(
            json.dumps(
                {
                    "agent": chosen,
                    "presence_file": filename,
                    "wakeup_minute": minute,
                    "wakeup_cron": wakeup_cron,
                    "utc": now,
                }
            )
        )
    return 0


def cmd_random_cron(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    agent = args.agent.lower().strip() if args.agent else None

    with FileLock(args.lock_file):
        cards = parse_agent_cards(wiki_dir) if wiki_dir.exists() else {}
        if agent and agent in cards and not args.reroll:
            existing = cards[agent].get("wakeup_cron", "")
            m = re.match(r"^(\d{1,2})\s+\*\s+\*\s+\*\s+\*$", existing)
            if m:
                minute = int(m.group(1))
                print(
                    json.dumps(
                        {
                            "agent": agent,
                            "wakeup_minute": minute,
                            "wakeup_cron": existing,
                            "persisted": True,
                        }
                    )
                )
                return 0

        minute, wakeup_cron = pick_random_cron(wiki_dir, exclude_agent=agent)
        persisted = False
        if agent and agent in cards:
            card_path = wiki_dir / cards[agent]["filename"]
            body = card_path.read_text(encoding="utf-8")
            if re.search(r"^- \*\*Wakeup Cron\*\*:\s*.+$", body, re.MULTILINE):
                body = re.sub(
                    r"^- \*\*Wakeup Cron\*\*:\s*.+$",
                    f"- **Wakeup Cron**: `{wakeup_cron}`",
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                body = re.sub(
                    r"^(- \*\*Last Heartbeat \(UTC\*\*:\s*.+|- \*\*Last Heartbeat \(UTC\)\*\*:\s*.+)$",
                    rf"\1\n- **Wakeup Cron**: `{wakeup_cron}`",
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
            atomic_write(card_path, body)
            persisted = True

        print(
            json.dumps(
                {
                    "agent": agent,
                    "wakeup_minute": minute,
                    "wakeup_cron": wakeup_cron,
                    "persisted": persisted,
                }
            )
        )
    return 0


def cmd_quick_check(args: argparse.Namespace) -> int:
    """Fast read-only check for waking agents: reports whether anything new needs doing."""
    wiki_dir: Path = args.wiki_dir
    if not wiki_dir.exists():
        print(f"ERROR: Wiki directory does not exist: {wiki_dir}", file=sys.stderr)
        return 1

    agent = args.agent.lower().strip() if args.agent else None
    now = utc_now()
    cards = parse_agent_cards(wiki_dir)
    agent_hb: dt.datetime | None = None
    if agent and agent in cards:
        agent_hb = parse_iso_utc(cards[agent].get("heartbeat"))

    # 1. Check 4x/day (6-hour) Second Brain sync status
    sb_sync_due = False
    ledger_path = wiki_dir / SB_LEDGER_FILE
    if ledger_path.exists():
        state = load_sb_state(ledger_path)
        interval = int(state.get("interval_seconds") or 21600)
        last_dt = parse_iso_utc(state.get("last_completed_utc"))  # type: ignore[arg-type]
        lease_exp = parse_iso_utc(state.get("lease_expires_utc"))  # type: ignore[arg-type]
        lease_agent = state.get("lease_agent")
        lease_active = bool(
            lease_agent and lease_exp and lease_exp > now and lease_agent != agent
        )
        due = (last_dt is None) or ((now - last_dt).total_seconds() >= interval)
        sb_sync_due = due and not lease_active

    # 2. Check board for OPEN tasks, own IN_PROGRESS tasks, or stale IN_PROGRESS tasks
    open_tasks: list[dict[str, str]] = []
    own_in_progress_tasks: list[dict[str, str]] = []
    stale_in_progress_tasks: list[dict[str, str]] = []
    b_file = board_filename(wiki_dir)
    in_file = human_inbox_filename(wiki_dir)
    out_file = human_outbox_filename(wiki_dir)
    board_path = wiki_dir / b_file
    if board_path.exists():
        rows = parse_tasks_table(board_path.read_text(encoding="utf-8"))
        for r in rows:
            if r["status"] == "OPEN":
                open_tasks.append(
                    {"id": r["id"], "priority": r["priority"], "summary": r["summary"]}
                )
            elif r["status"] == "IN_PROGRESS":
                if agent and r["owner"] == agent:
                    own_in_progress_tasks.append(
                        {"id": r["id"], "priority": r["priority"], "summary": r["summary"]}
                    )
                else:
                    claimed_dt = parse_iso_utc(r["updated"])
                    owner_hb = (
                        parse_iso_utc(cards[r["owner"]].get("heartbeat"))
                        if r["owner"] in cards
                        else None
                    )
                    claim_stale = (
                        claimed_dt is not None
                        and (now - claimed_dt).total_seconds() >= args.stale_minutes * 60
                    )
                    hb_stale = (
                        owner_hb is None
                        or (now - owner_hb).total_seconds() >= args.stale_minutes * 60
                    )
                    if claim_stale and hb_stale:
                        stale_in_progress_tasks.append(
                            {
                                "id": r["id"],
                                "owner": r["owner"],
                                "updated": r["updated"],
                                "summary": r["summary"],
                            }
                        )

    # 3. Check human inbox and kb-*, collab--*, or custom .md files modified since last heartbeat
    human_inbox_updated = False
    inbox_path = wiki_dir / in_file
    if inbox_path.exists():
        inbox_txt = inbox_path.read_text(encoding="utf-8")
        has_directives = bool(re.search(r"`D-\d{3,}`", inbox_txt))
        inbox_mtime = dt.datetime.fromtimestamp(
            inbox_path.stat().st_mtime, dt.timezone.utc
        )
        if has_directives and (
            agent_hb is None or (inbox_mtime - agent_hb).total_seconds() > 2
        ):
            human_inbox_updated = True

    updated_wiki_files: list[str] = []
    ignored_exact = {
        ROSTER_FILE,
        SB_LEDGER_FILE,
        b_file,
        CONVENTIONS_FILE,
        in_file,
        out_file,
    }
    for entry in sorted(wiki_dir.glob("*.md")):
        name = entry.name
        if (
            name in ignored_exact
            or name.startswith("meta--")
            or name.startswith("agent-")
            or name.startswith("output--")
        ):
            continue
        mtime = dt.datetime.fromtimestamp(entry.stat().st_mtime, dt.timezone.utc)
        # Allow 2s tolerance against heartbeat second-rounding
        if agent_hb is None or (mtime - agent_hb).total_seconds() > 2:
            updated_wiki_files.append(name)

    reasons: list[str] = []
    skill_md_path = Path(__file__).resolve().parent.parent / "SKILL.md"
    skill_updated = False
    if (
        wiki_dir.resolve() == DEFAULT_WIKI_DIR.resolve()
        and skill_md_path.exists()
        and agent_hb is not None
    ):
        skill_mtime = dt.datetime.fromtimestamp(
            skill_md_path.stat().st_mtime, dt.timezone.utc
        )
        if (skill_mtime - agent_hb).total_seconds() > 2:
            skill_updated = True
            reasons.append("skill_updated")

    if human_inbox_updated:
        reasons.append("human_inbox_updated")
        updated_wiki_files.insert(0, in_file)
    if sb_sync_due:
        reasons.append("sb_sync_due")
    if own_in_progress_tasks:
        reasons.append("own_in_progress_tasks")
    if open_tasks:
        reasons.append("open_tasks")
    if stale_in_progress_tasks:
        reasons.append("stale_in_progress_tasks")
    if updated_wiki_files and (not human_inbox_updated or len(updated_wiki_files) > 1):
        reasons.append("updated_wiki_files")

    action_needed = bool(reasons)
    if skill_updated:
        next_step = (
            f"SKILL.md was updated since your last heartbeat (new single-spokesperson @runai protocol). "
            f"Re-read {skill_md_path} first; if no other tasks are open, call heartbeat --status idle once to record the reload and return to waiting."
        )
    elif action_needed:
        next_step = (
            "Inspect only the due/updated items above; if after checking updated_wiki_files no new task is warranted, call heartbeat --status idle once to advance your watermark and return to waiting."
        )
    else:
        next_step = (
            "Nothing new to do. Do NOT edit any wiki files; end your turn immediately and go back to waiting."
        )
    print(
        json.dumps(
            {
                "agent": agent,
                "utc": iso_utc(now),
                "last_heartbeat_utc": iso_utc(agent_hb) if agent_hb else None,
                "action_needed": action_needed,
                "reasons": reasons,
                "sb_sync_due": sb_sync_due,
                "open_tasks": open_tasks,
                "own_in_progress_tasks": own_in_progress_tasks,
                "stale_in_progress_tasks": stale_in_progress_tasks,
                "updated_wiki_files": updated_wiki_files,
                "next_step": next_step,
            },
            indent=2,
        )
    )
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    agent = args.agent.lower().strip()

    with FileLock(args.lock_file):
        cards = parse_agent_cards(wiki_dir)
        if agent not in cards:
            print(
                f"ERROR: Agent '{agent}' has no presence card. Run register-agent first.",
                file=sys.stderr,
            )
            return 1

        old_filename = cards[agent]["filename"]
        old_path = wiki_dir / old_filename
        body = old_path.read_text(encoding="utf-8")
        now = iso_utc()

        body = re.sub(
            r"^- \*\*Status\*\*:\s*.+$",
            f"- **Status**: `{args.status}`",
            body,
            count=1,
            flags=re.MULTILINE,
        )
        body = re.sub(
            r"^- \*\*Last Heartbeat \(UTC\)\*\*:\s*.+$",
            f"- **Last Heartbeat (UTC)**: {now}",
            body,
            count=1,
            flags=re.MULTILINE,
        )
        if args.focus:
            body = re.sub(
                r"^- \*\*Current Focus\*\*:\s*.+$",
                f"- **Current Focus**: {args.focus}",
                body,
                count=1,
                flags=re.MULTILINE,
            )

        new_filename = old_filename
        if args.focus:
            focus_slug = slugify(args.focus, 110)
            new_filename = f"agent-{agent}--{focus_slug}.md"

        atomic_write(old_path, body)
        rewritten = 0
        if new_filename != old_filename:
            new_path = wiki_dir / new_filename
            os.replace(old_path, new_path)
            rewritten = rewrite_links_in_wiki(wiki_dir, old_filename, new_filename)

        sync_roster_table(wiki_dir)
        print(
            json.dumps(
                {
                    "agent": agent,
                    "status": args.status,
                    "presence_file": new_filename,
                    "links_updated_in_files": rewritten,
                    "utc": now,
                }
            )
        )
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    old_name = Path(args.old_name).name
    new_name = Path(args.new_name).name

    errs = validate_filename(new_name)
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 2

    with FileLock(args.lock_file):
        old_path = wiki_dir / old_name
        new_path = wiki_dir / new_name
        if not old_path.exists():
            print(f"ERROR: Source file does not exist: {old_path}", file=sys.stderr)
            return 1
        if new_path.exists() and old_name != new_name:
            print(
                f"ERROR: Target file already exists: {new_path}", file=sys.stderr
            )
            return 1
        if old_name != new_name:
            os.replace(old_path, new_path)
            updated = rewrite_links_in_wiki(wiki_dir, old_name, new_name)
        else:
            updated = 0
        if old_name.startswith("agent-") or new_name.startswith("agent-"):
            sync_roster_table(wiki_dir)
        print(
            json.dumps(
                {
                    "old_name": old_name,
                    "new_name": new_name,
                    "links_updated_in_files": updated,
                }
            )
        )
    return 0


def compute_sb_hashes(sb_dir: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if not sb_dir.exists():
        return result
    for p in sorted(sb_dir.glob("*.md")):
        if p.name in ("SB 00 README (protocol).md", "SB 02 Gemini setup.md"):
            continue
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()[:16]
        result[p.name] = {
            "sha256_16": sha,
            "bytes": len(data),
            "path": str(p),
        }
    return result


def load_sb_state(ledger_path: Path) -> dict[str, object]:
    text = ledger_path.read_text(encoding="utf-8")
    raw = extract_between_markers(
        text, "<!-- SB_STATE_JSON_START -->", "<!-- SB_STATE_JSON_END -->"
    )
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    return json.loads(raw)


def save_sb_state(ledger_path: Path, state: dict[str, object], history_entry: str | None = None) -> None:
    text = ledger_path.read_text(encoding="utf-8")
    fenced = "```json\n" + json.dumps(state, indent=2, sort_keys=False) + "\n```"
    text = replace_between_markers(
        text, "<!-- SB_STATE_JSON_START -->", "<!-- SB_STATE_JSON_END -->", fenced
    )
    if history_entry:
        existing_hist = extract_between_markers(
            text, "<!-- SB_HISTORY_START -->", "<!-- SB_HISTORY_END -->"
        )
        lines = [
            ln for ln in existing_hist.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(No Second Brain ingestion")
        ]
        lines.insert(0, f"- {history_entry}")
        lines = lines[:40]
        text = replace_between_markers(
            text, "<!-- SB_HISTORY_START -->", "<!-- SB_HISTORY_END -->", "\n".join(lines)
        )
    atomic_write(ledger_path, text)


def cmd_sb_check(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    ledger_path = wiki_dir / SB_LEDGER_FILE
    if not ledger_path.exists():
        print(f"ERROR: Missing ledger file: {ledger_path}", file=sys.stderr)
        return 1

    with FileLock(args.lock_file):
        state = load_sb_state(ledger_path)
        now = utc_now()
        interval = int(state.get("interval_seconds") or 21600)
        last_dt = parse_iso_utc(state.get("last_completed_utc"))  # type: ignore[arg-type]
        lease_exp = parse_iso_utc(state.get("lease_expires_utc"))  # type: ignore[arg-type]
        lease_agent = state.get("lease_agent")

        lease_active = bool(
            lease_agent and lease_exp and lease_exp > now and lease_agent != args.agent
        )
        elapsed = (now - last_dt).total_seconds() if last_dt else None
        due = (last_dt is None) or (elapsed is not None and elapsed >= interval)

        claimed = False
        sync_stdout = ""
        if args.claim:
            if lease_active and not args.force:
                print(
                    json.dumps(
                        {
                            "due": due,
                            "claimed": False,
                            "reason": f"Another agent ('{lease_agent}') holds sync lease until {iso_utc(lease_exp)}.",
                            "last_completed_utc": state.get("last_completed_utc"),
                        },
                        indent=2,
                    )
                )
                return 0
            if not due and not args.force:
                next_due = iso_utc(last_dt + dt.timedelta(seconds=interval)) if last_dt else iso_utc(now)
                print(
                    json.dumps(
                        {
                            "due": False,
                            "claimed": False,
                            "reason": "6-hour (4x/day) window has not elapsed yet.",
                            "last_completed_utc": state.get("last_completed_utc"),
                            "next_due_utc": next_due,
                        },
                        indent=2,
                    )
                )
                return 0

            if not args.agent:
                print("ERROR: --claim requires --agent <name>.", file=sys.stderr)
                return 2

            state["lease_agent"] = args.agent.lower().strip()
            state["lease_expires_utc"] = iso_utc(now + dt.timedelta(minutes=20))
            save_sb_state(ledger_path, state)
            claimed = True

    # Run rclone/cache sync outside the flock so we don't block other agents during network I/O
    if claimed and not args.no_remote_sync and DEFAULT_SB_SYNC_SCRIPT.exists():
        proc = subprocess.run(
            [str(DEFAULT_SB_SYNC_SCRIPT), "--force"],
            capture_output=True,
            text=True,
            check=False,
        )
        sync_stdout = proc.stdout.strip()

    with FileLock(args.lock_file):
        state = load_sb_state(ledger_path)
        prev_marks: dict[str, dict[str, object]] = state.get("file_watermarks") or {}  # type: ignore[assignment]
        curr_marks = compute_sb_hashes(args.sb_cache_dir)

        new_files: list[str] = []
        modified_files: list[str] = []
        unchanged_files: list[str] = []
        removed_files = [f for f in prev_marks if f not in curr_marks]

        for fname, meta in curr_marks.items():
            if fname not in prev_marks:
                new_files.append(str(meta["path"]))
            elif prev_marks[fname].get("sha256_16") != meta["sha256_16"]:
                modified_files.append(str(meta["path"]))
            else:
                unchanged_files.append(str(meta["path"]))

        next_due_str = (
            iso_utc(last_dt + dt.timedelta(seconds=interval))
            if last_dt
            else iso_utc(now)
        )
        print(
            json.dumps(
                {
                    "due": due,
                    "claimed": claimed,
                    "last_completed_utc": state.get("last_completed_utc"),
                    "next_due_utc": next_due_str,
                    "lease_agent": state.get("lease_agent"),
                    "lease_expires_utc": state.get("lease_expires_utc"),
                    "sync_script_status": sync_stdout.splitlines()[0] if sync_stdout else None,
                    "new_files": new_files,
                    "modified_files": modified_files,
                    "unchanged_files": unchanged_files,
                    "removed_files": removed_files,
                },
                indent=2,
            )
        )
    return 0


def cmd_sb_complete(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    ledger_path = wiki_dir / SB_LEDGER_FILE
    agent = args.agent.lower().strip()

    with FileLock(args.lock_file):
        state = load_sb_state(ledger_path)
        now_str = iso_utc()
        curr_marks = compute_sb_hashes(args.sb_cache_dir)
        # Store compact watermark metadata
        compact_marks = {
            k: {"sha256_16": v["sha256_16"], "bytes": v["bytes"]}
            for k, v in curr_marks.items()
        }
        state["last_completed_utc"] = now_str
        state["last_agent"] = agent
        state["lease_agent"] = None
        state["lease_expires_utc"] = None
        state["file_watermarks"] = compact_marks
        entry = f"**{now_str}** (`@{agent}`): {args.notes.strip()}"
        save_sb_state(ledger_path, state, history_entry=entry)
        print(
            json.dumps(
                {
                    "completed_utc": now_str,
                    "agent": agent,
                    "tracked_files": len(compact_marks),
                }
            )
        )
    return 0


def parse_tasks_table(board_text: str) -> list[dict[str, str]]:
    block = extract_between_markers(
        board_text, "<!-- TASKS_TABLE_START -->", "<!-- TASKS_TABLE_END -->"
    )
    rows: list[dict[str, str]] = []
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|") or "Task Summary" in s or set(s.replace("|", "").strip()) <= {"-", ":", " "}:
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if len(cols) < 6:
            continue
        tid = cols[0].strip("` ")
        if tid.startswith("*("):
            continue
        rows.append(
            {
                "id": tid,
                "priority": cols[1].strip("` "),
                "status": cols[2].strip("` "),
                "owner": cols[3].strip("` "),
                "updated": cols[4].strip("` "),
                "summary": cols[5].strip(),
            }
        )
    return rows


def format_tasks_table(rows: list[dict[str, str]]) -> str:
    prio_rank = {"P1": 1, "P2": 2, "P3": 3}
    status_rank = {"IN_PROGRESS": 0, "OPEN": 1}
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            status_rank.get(r["status"], 2),
            prio_rank.get(r["priority"], 9),
            r["id"],
        ),
    )
    lines = [
        "| ID | Priority | Status | Owner | Claimed / Updated (UTC) | Task Summary & Expected Deliverable |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    if not sorted_rows:
        lines.append("| *(none)* | — | — | — | — | *(No open tasks right now — scan wiki & Second Brain to propose new helpful tasks.)* |")
    else:
        for r in sorted_rows:
            owner_fmt = f"`{r['owner']}`" if r["owner"] not in ("—", "-", "") else "—"
            upd_fmt = f"`{r['updated']}`" if r["updated"] not in ("—", "-", "") else "—"
            safe_summary = r["summary"].replace("|", "\\|")
            lines.append(
                f"| `{r['id']}` | `{r['priority']}` | `{r['status']}` | {owner_fmt} | {upd_fmt} | {safe_summary} |"
            )
    return "\n".join(lines)


def next_task_id(board_text: str, rows: list[dict[str, str]]) -> str:
    nums = [0]
    for m in re.finditer(r"T-(\d{3,})", board_text):
        nums.append(int(m.group(1)))
    for r in rows:
        m = re.match(r"T-(\d+)", r["id"])
        if m:
            nums.append(int(m.group(1)))
    return f"T-{max(nums) + 1:03d}"


def cmd_add_task(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    board_path = wiki_dir / board_filename(wiki_dir)
    with FileLock(args.lock_file):
        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        tid = next_task_id(text, rows)
        now_str = iso_utc()
        summary = args.summary.strip()
        if args.agent:
            summary = f"{summary} *(proposed by `@{args.agent.lower()}`)*"
        rows.append(
            {
                "id": tid,
                "priority": args.priority.upper(),
                "status": "OPEN",
                "owner": "—",
                "updated": now_str,
                "summary": summary,
            }
        )
        updated = replace_between_markers(
            text,
            "<!-- TASKS_TABLE_START -->",
            "<!-- TASKS_TABLE_END -->",
            format_tasks_table(rows),
        )
        atomic_write(board_path, updated)
        print(json.dumps({"task_id": tid, "priority": args.priority.upper(), "status": "OPEN"}))
    return 0


def cmd_claim_task(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    board_path = wiki_dir / board_filename(wiki_dir)
    agent = args.agent.lower().strip()
    target_id = args.task_id.upper().strip()

    with FileLock(args.lock_file):
        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        now = utc_now()
        now_str = iso_utc(now)

        match_row = next((r for r in rows if r["id"].upper() == target_id), None)
        if not match_row:
            print(f"ERROR: Task '{target_id}' not found on active board.", file=sys.stderr)
            return 1

        if match_row["status"] == "IN_PROGRESS" and match_row["owner"] != agent:
            claimed_dt = parse_iso_utc(match_row["updated"])
            stale = (
                claimed_dt is not None
                and (now - claimed_dt).total_seconds() >= args.stale_minutes * 60
            )
            if not stale:
                print(
                    json.dumps(
                        {
                            "claimed": False,
                            "task_id": target_id,
                            "owner": match_row["owner"],
                            "reason": f"Task {target_id} is already IN_PROGRESS by @{match_row['owner']} (since {match_row['updated']}). Pick another task.",
                        }
                    ),
                    file=sys.stderr,
                )
                return 1

        match_row["status"] = "IN_PROGRESS"
        match_row["owner"] = agent
        match_row["updated"] = now_str

        updated = replace_between_markers(
            text,
            "<!-- TASKS_TABLE_START -->",
            "<!-- TASKS_TABLE_END -->",
            format_tasks_table(rows),
        )
        atomic_write(board_path, updated)
        print(
            json.dumps(
                {
                    "claimed": True,
                    "task_id": target_id,
                    "owner": agent,
                    "utc": now_str,
                    "summary": match_row["summary"],
                }
            )
        )
    return 0


def cmd_complete_task(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    board_path = wiki_dir / board_filename(wiki_dir)
    agent = args.agent.lower().strip()
    target_id = args.task_id.upper().strip()

    if args.output_file:
        out_name = Path(args.output_file).name
        if not (wiki_dir / out_name).exists():
            print(
                f"ERROR: Deliverable file '{out_name}' does not exist in {wiki_dir}.",
                file=sys.stderr,
            )
            return 1
    else:
        out_name = ""

    with FileLock(args.lock_file):
        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        match_row = next((r for r in rows if r["id"].upper() == target_id), None)
        if not match_row:
            print(f"ERROR: Task '{target_id}' not found on active board.", file=sys.stderr)
            return 1

        remaining = [r for r in rows if r["id"].upper() != target_id]
        now_str = iso_utc()
        link_part = f" → [{out_name}]({out_name})" if out_name else ""
        note_part = f" — {args.notes.strip()}" if args.notes else ""
        done_line = (
            f"- **`{target_id}`** ({now_str}, `@{agent}`): "
            f"{match_row['summary']}{link_part}{note_part}"
        )

        text = replace_between_markers(
            text,
            "<!-- TASKS_TABLE_START -->",
            "<!-- TASKS_TABLE_END -->",
            format_tasks_table(remaining),
        )
        done_block = extract_between_markers(
            text, "<!-- DONE_LIST_START -->", "<!-- DONE_LIST_END -->"
        )
        done_lines = [
            ln for ln in done_block.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(No completed tasks")
        ]
        done_lines.insert(0, done_line)
        text = replace_between_markers(
            text,
            "<!-- DONE_LIST_START -->",
            "<!-- DONE_LIST_END -->",
            "\n".join(done_lines),
        )
        atomic_write(board_path, text)
        print(
            json.dumps(
                {
                    "completed": True,
                    "task_id": target_id,
                    "agent": agent,
                    "output_file": out_name or None,
                    "utc": now_str,
                    "next_step": "Queue a concise summary for spokesperson @runai via 'herd_wiki.py notify-human' rather than messaging the user directly.",
                }
            )
        )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    if not wiki_dir.exists():
        print(f"ERROR: Wiki directory missing: {wiki_dir}", file=sys.stderr)
        return 1

    errors: list[str] = []
    md_files: set[str] = set()

    for entry in sorted(wiki_dir.iterdir(), key=lambda p: p.name):
        if entry.is_dir():
            errors.append(f"Forbidden subdirectory inside flat wiki: '{entry.name}/'")
            continue
        errs = validate_filename(entry.name)
        errors.extend(errs)
        if entry.name.endswith(".md"):
            md_files.add(entry.name)

    link_re = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    for fname in sorted(md_files):
        raw = (wiki_dir / fname).read_text(encoding="utf-8")
        cleaned = strip_fenced_blocks(raw)
        # Also remove inline backtick code spans so example links in code aren't false positives
        cleaned = re.sub(r"`[^`]*`", "", cleaned)
        for m in link_re.finditer(cleaned):
            target = m.group(1).strip()
            if (
                not target
                or target.startswith("#")
                or "://" in target
                or target.startswith("mailto:")
            ):
                continue
            target_file = target.split("#", 1)[0]
            if target_file.startswith("./"):
                target_file = target_file[2:]
            if "/" in target_file:
                errors.append(
                    f"{fname}: link '{target}' points outside flat wiki directory."
                )
            elif target_file not in md_files:
                errors.append(
                    f"{fname}: broken relative link to missing wiki file '{target_file}'."
                )

    if errors:
        print(f"VALIDATION FAILED ({len(errors)} issue(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(
        f"OK: {len(md_files)} markdown files validated in {wiki_dir} "
        "(flat layout, summary-slug filenames <= 200B, all relative links valid)."
    )
    return 0


def parse_outbox_pending(outbox_text: str) -> list[dict[str, str]]:
    block = extract_between_markers(
        outbox_text, "<!-- OUTBOX_PENDING_START -->", "<!-- OUTBOX_PENDING_END -->"
    )
    rows: list[dict[str, str]] = []
    for line in block.splitlines():
        s = line.strip()
        if (
            not s.startswith("|")
            or "Summary for" in s
            or set(s.replace("|", "").strip()) <= {"-", ":", " "}
        ):
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if len(cols) < 7:
            continue
        mid = cols[0].strip("` ")
        if mid.startswith("*("):
            continue
        rows.append(
            {
                "id": mid,
                "created": cols[1].strip("` "),
                "agent": cols[2].strip("`@ "),
                "kind": cols[3].strip("` "),
                "due": cols[4].strip("` "),
                "deliverable": cols[5].strip(),
                "summary": cols[6].strip(),
            }
        )
    return rows


def format_outbox_pending(rows: list[dict[str, str]]) -> str:
    lines = [
        "| ID | Created (UTC) | From | Kind | Due Date | Deliverable | Summary for Human |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    if not rows:
        lines.append(
            "| *(none)* | — | — | — | — | — | *(No unread items queued right now.)* |"
        )
    else:
        for r in rows:
            due_fmt = f"`{r['due']}`" if r["due"] not in ("—", "-", "") else "—"
            deliv = r["deliverable"] if r["deliverable"] not in ("—", "-", "") else "—"
            safe_sum = r["summary"].replace("|", "\\|")
            lines.append(
                f"| `{r['id']}` | `{r['created']}` | `@{r['agent']}` | `{r['kind']}` | {due_fmt} | {deliv} | {safe_sum} |"
            )
    return "\n".join(lines)


def load_outbox_state(outbox_text: str) -> dict[str, object]:
    raw = extract_between_markers(
        outbox_text, "<!-- OUTBOX_STATE_JSON_START -->", "<!-- OUTBOX_STATE_JSON_END -->"
    )
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    return json.loads(raw)


def compute_output_hashes(wiki_dir: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for p in sorted(wiki_dir.glob("output--*.md")):
        data = p.read_bytes()
        out[p.name] = {
            "sha256_16": hashlib.sha256(data).hexdigest()[:16],
            "bytes": len(data),
        }
    return out


def cmd_post_directive(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    inbox_path = wiki_dir / human_inbox_filename(wiki_dir)
    if not inbox_path.exists():
        print(f"ERROR: Missing human inbox file: {inbox_path}", file=sys.stderr)
        return 1

    with FileLock(args.lock_file):
        text = inbox_path.read_text(encoding="utf-8")
        nums = [0] + [int(m.group(1)) for m in re.finditer(r"D-(\d{3,})", text)]
        did = f"D-{max(nums) + 1:03d}"
        now_str = iso_utc()
        channel = args.channel.lower().strip()
        recorded_by = args.agent.lower().strip()
        entry = (
            f"- **`{did}`** ({now_str}, via `{channel}`, recorded by `@{recorded_by}` "
            f"`[chat {now_str[:10]}]`): {args.summary.strip()}"
        )

        existing = extract_between_markers(
            text, "<!-- HUMAN_INBOX_START -->", "<!-- HUMAN_INBOX_END -->"
        )
        lines = [
            ln
            for ln in existing.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(No directives")
        ]
        lines.insert(0, entry)
        updated = replace_between_markers(
            text, "<!-- HUMAN_INBOX_START -->", "<!-- HUMAN_INBOX_END -->", "\n".join(lines)
        )
        atomic_write(inbox_path, updated)
        print(json.dumps({"directive_id": did, "utc": now_str, "channel": channel}))
    return 0


def cmd_notify_human(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if not outbox_path.exists():
        print(f"ERROR: Missing human outbox file: {outbox_path}", file=sys.stderr)
        return 1

    deliv_cell = "—"
    if args.file:
        fname = Path(args.file).name
        if not (wiki_dir / fname).exists():
            print(
                f"ERROR: Referenced file '{fname}' does not exist in {wiki_dir}.",
                file=sys.stderr,
            )
            return 1
        deliv_cell = f"[{fname}]({fname})"

    with FileLock(args.lock_file):
        text = outbox_path.read_text(encoding="utf-8")
        nums = [0] + [int(m.group(1)) for m in re.finditer(r"M-(\d{3,})", text)]
        mid = f"M-{max(nums) + 1:03d}"
        now_str = iso_utc()
        rows = parse_outbox_pending(text)
        rows.append(
            {
                "id": mid,
                "created": now_str,
                "agent": args.agent.lower().strip(),
                "kind": args.kind.lower().strip(),
                "due": (args.due or "—").strip(),
                "deliverable": deliv_cell,
                "summary": args.message.strip(),
            }
        )
        updated = replace_between_markers(
            text,
            "<!-- OUTBOX_PENDING_START -->",
            "<!-- OUTBOX_PENDING_END -->",
            format_outbox_pending(rows),
        )
        atomic_write(outbox_path, updated)
        print(
            json.dumps(
                {
                    "message_id": mid,
                    "utc": now_str,
                    "kind": args.kind.lower().strip(),
                    "due": args.due,
                }
            )
        )
    return 0


def cmd_spokesperson_check(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if not outbox_path.exists():
        print(f"ERROR: Missing human outbox file: {outbox_path}", file=sys.stderr)
        return 1

    text = outbox_path.read_text(encoding="utf-8")
    pending = parse_outbox_pending(text)
    state = load_outbox_state(text)
    seen: dict[str, dict[str, object]] = state.get("seen_outputs") or {}  # type: ignore[assignment]
    curr_outputs = compute_output_hashes(wiki_dir)

    new_outputs: list[str] = []
    modified_outputs: list[str] = []
    for fname, meta in curr_outputs.items():
        if fname not in seen:
            new_outputs.append(fname)
        elif seen[fname].get("sha256_16") != meta["sha256_16"]:
            modified_outputs.append(fname)

    anything = bool(pending or new_outputs or modified_outputs)
    print(
        json.dumps(
            {
                "utc": iso_utc(),
                "last_reported_utc": state.get("last_reported_utc"),
                "anything_to_report": anything,
                "unsent_items": pending,
                "new_outputs": new_outputs,
                "modified_outputs": modified_outputs,
            },
            indent=2,
        )
    )
    return 0


def cmd_spokesperson_mark_sent(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if not outbox_path.exists():
        print(f"ERROR: Missing human outbox file: {outbox_path}", file=sys.stderr)
        return 1

    with FileLock(args.lock_file):
        text = outbox_path.read_text(encoding="utf-8")
        pending = parse_outbox_pending(text)
        state = load_outbox_state(text)
        now_str = iso_utc()

        state["last_reported_utc"] = now_str
        state["spokesperson_agent"] = args.agent.lower().strip()
        state["seen_outputs"] = compute_output_hashes(wiki_dir)

        fenced = "```json\n" + json.dumps(state, indent=2) + "\n```"
        text = replace_between_markers(
            text, "<!-- OUTBOX_STATE_JSON_START -->", "<!-- OUTBOX_STATE_JSON_END -->", fenced
        )
        text = replace_between_markers(
            text,
            "<!-- OUTBOX_PENDING_START -->",
            "<!-- OUTBOX_PENDING_END -->",
            format_outbox_pending([]),
        )

        sent_block = extract_between_markers(
            text, "<!-- OUTBOX_SENT_START -->", "<!-- OUTBOX_SENT_END -->"
        )
        sent_lines = [
            ln
            for ln in sent_block.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(Initial baseline")
        ]
        new_sent_entries: list[str] = []
        for r in pending:
            deliv_part = f" ({r['deliverable']})" if r["deliverable"] != "—" else ""
            new_sent_entries.append(
                f"- **`{r['id']}`** (delivered {now_str} by `@{args.agent.lower()}`, from `@{r['agent']}`, `{r['kind']}`): {r['summary']}{deliv_part}"
            )
        if args.notes:
            new_sent_entries.insert(
                0, f"- **{now_str}** (`@{args.agent.lower()}`): {args.notes.strip()}"
            )
        sent_lines = (new_sent_entries + sent_lines)[:50]
        if not sent_lines:
            sent_lines = [f"- **{now_str}** (`@{args.agent.lower()}`): Baseline synced."]

        text = replace_between_markers(
            text, "<!-- OUTBOX_SENT_START -->", "<!-- OUTBOX_SENT_END -->", "\n".join(sent_lines)
        )
        atomic_write(outbox_path, text)
        print(
            json.dumps(
                {
                    "delivered_utc": now_str,
                    "cleared_pending_count": len(pending),
                    "tracked_outputs": len(state["seen_outputs"]),
                }
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Herd Wiki CLI helper for flat summary-named multi-agent coordination."
    )
    p.add_argument(
        "--wiki-dir",
        type=Path,
        default=DEFAULT_WIKI_DIR,
        help=f"Path to flat wiki directory (default: {DEFAULT_WIKI_DIR})",
    )
    p.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help=f"Path to flock file outside wiki (default: {DEFAULT_LOCK_PATH})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("index", help="List all wiki markdown files grouped by category.")
    sub.add_parser("validate", help="Verify flat-wiki invariants and internal links.")

    reg = sub.add_parser(
        "register-agent", help="Atomically claim a unique agent name and create presence page."
    )
    reg.add_argument("--name", help="Optional preferred lowercase single-word name.")
    reg.add_argument("--focus", help="Short summary of initial focus.")

    rc = sub.add_parser(
        "random-cron",
        help="Pick (and persist on agent card) a collision-free OS-random minute cron '<M> * * * *'.",
    )
    rc.add_argument("--agent", help="Agent name to persist the wakeup cron onto.")
    rc.add_argument(
        "--reroll", action="store_true", help="Pick a fresh random minute even if already set."
    )

    qc = sub.add_parser(
        "quick-check",
        help="Fast read-only check on cron wakeup to see if anything new needs doing.",
    )
    qc.add_argument("--agent", help="Registered agent name.")
    qc.add_argument("--stale-minutes", type=int, default=120)

    hb = sub.add_parser(
        "heartbeat", help="Update agent status, heartbeat, and summary-named presence file."
    )
    hb.add_argument("--agent", required=True, help="Registered agent name.")
    hb.add_argument(
        "--status",
        default="active",
        choices=["active", "coordinating", "working", "idle"],
    )
    hb.add_argument("--focus", help="Updated summary of current focus (renames presence file).")

    ren = sub.add_parser(
        "rename", help="Rename a wiki file when its summary changes and rewrite incoming links."
    )
    ren.add_argument("old_name")
    ren.add_argument("new_name")

    sbc = sub.add_parser(
        "sb-check",
        help="Check whether the 4x/day (6-hour) Second Brain ingestion is due and diff files.",
    )
    sbc.add_argument("--agent", help="Agent name (required with --claim).")
    sbc.add_argument(
        "--claim",
        action="store_true",
        help="Atomically claim a 20-minute sync lease if due and run sync.sh --force.",
    )
    sbc.add_argument("--force", action="store_true", help="Force claim even if <6h elapsed.")
    sbc.add_argument("--no-remote-sync", action="store_true", help="Skip calling sync.sh.")
    sbc.add_argument("--sb-cache-dir", type=Path, default=DEFAULT_SB_CACHE_DIR)

    sbd = sub.add_parser(
        "sb-complete",
        help="Record completed Second Brain ingestion and file watermarks in ledger.",
    )
    sbd.add_argument("--agent", required=True)
    sbd.add_argument("--notes", required=True, help="Summary of wiki files created/updated.")
    sbd.add_argument("--sb-cache-dir", type=Path, default=DEFAULT_SB_CACHE_DIR)

    at = sub.add_parser("add-task", help="Add a prioritized task to the shared board.")
    at.add_argument("--agent", help="Proposing agent name.")
    at.add_argument("--priority", default="P2", choices=["P1", "P2", "P3", "p1", "p2", "p3"])
    at.add_argument("--summary", required=True, help="Task description & expected deliverable.")

    ct = sub.add_parser("claim-task", help="Atomically claim one OPEN task on the board.")
    ct.add_argument("--agent", required=True)
    ct.add_argument("--task-id", required=True, help="Task ID such as T-001.")
    ct.add_argument("--stale-minutes", type=int, default=120)

    dt_cmd = sub.add_parser(
        "complete-task", help="Mark a claimed task DONE and link its output--*.md file."
    )
    dt_cmd.add_argument("--agent", required=True)
    dt_cmd.add_argument("--task-id", required=True)
    dt_cmd.add_argument("--output-file", help="Filename of output--*.md deliverable in wiki.")
    dt_cmd.add_argument("--notes", help="Optional completion note.")

    pd_cmd = sub.add_parser(
        "post-directive",
        help="Record an inbound directive, answer, or priority shift from the user in human-inbox.",
    )
    pd_cmd.add_argument("--agent", default="runai", help="Spokesperson agent recording this.")
    pd_cmd.add_argument("--channel", default="antigravity", help="Source channel (e.g. antigravity, signal).")
    pd_cmd.add_argument("--summary", required=True, help="Directive or answer from the user.")

    nh_cmd = sub.add_parser(
        "notify-human",
        help="Queue a concise deliverable, question, or deadline in human-outbox for @runai to report.",
    )
    nh_cmd.add_argument("--agent", required=True, help="Worker agent name.")
    nh_cmd.add_argument(
        "--kind",
        default="deliverable",
        choices=["deliverable", "question", "deadline", "alert"],
    )
    nh_cmd.add_argument("--due", help="Optional YYYY-MM-DD deadline date (used for calendar/priority).")
    nh_cmd.add_argument("--file", help="Optional output--*.md filename in wiki.")
    nh_cmd.add_argument("--message", required=True, help="Concise 1-2 line takeaway or question for the user.")

    sub.add_parser(
        "spokesperson-check",
        help="Read-only check for @runai: reports unsent outbox items and new/updated output--*.md files.",
    )

    sms_cmd = sub.add_parser(
        "spokesperson-mark-sent",
        help="Mark current outbox items and output--*.md watermarks as delivered to the user.",
    )
    sms_cmd.add_argument("--agent", default="runai")
    sms_cmd.add_argument("--notes", help="Optional note for the SENT log.")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "index": cmd_index,
        "validate": cmd_validate,
        "register-agent": cmd_register_agent,
        "random-cron": cmd_random_cron,
        "quick-check": cmd_quick_check,
        "heartbeat": cmd_heartbeat,
        "rename": cmd_rename,
        "sb-check": cmd_sb_check,
        "sb-complete": cmd_sb_complete,
        "add-task": cmd_add_task,
        "claim-task": cmd_claim_task,
        "complete-task": cmd_complete_task,
        "post-directive": cmd_post_directive,
        "notify-human": cmd_notify_human,
        "spokesperson-check": cmd_spokesperson_check,
        "spokesperson-mark-sent": cmd_spokesperson_mark_sent,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
