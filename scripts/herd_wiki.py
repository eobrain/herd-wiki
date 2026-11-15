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
from typing import Any

DEFAULT_REVIEW_TIMEOUT_MINUTES = 90

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
DEFAULT_STATE_DIR = Path(
    os.environ.get("HERD_SIGNAL_STATE_DIR", str(Path.home() / ".local/state/herd"))
)
ESTIMATED_IDLE_WAKEUP_TOKENS = 160_000

ROSTER_FILE = "meta--herd-agent-roster-active-names-heartbeats-and-presence-index.md"
SB_LEDGER_FILE = (
    "meta--second-brain-sync-ledger-four-times-daily-schedule-and-source-watermarks.md"
)
METRICS_LEDGER_FILE = "meta--herd-quality-and-usefulness-metrics-ledger.md"
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


def normalize_effort_bucket(effort: str | None) -> str:
    e = (effort or "unspecified").strip().lower()
    if not e or e in ("—", "-", "none", "null", "unspecified", "unknown"):
        return "unspecified"
    try:
        val = float(e)
        if val <= 0.35:
            return "low"
        if val < 0.80:
            return "medium"
        return "high"
    except ValueError:
        pass
    if e in ("low", "min", "minimal"):
        return "low"
    if e in ("medium", "med", "normal", "standard"):
        return "medium"
    if e in ("high", "xhigh", "max"):
        return "high"
    return "unspecified"


def compute_capability_tier(model: str | None, effort: str | None) -> int:
    """Map declared (model, effort) to capability tier 1 (light), 2 (standard), or 3 (thorough)."""
    m = (model or "unspecified").strip().lower()
    bucket = normalize_effort_bucket(effort)
    if bucket == "low" or any(tok in m for tok in ("haiku", "lite")):
        return 1
    if "flash" in m:
        return 2 if bucket == "high" else 1
    if any(tok in m for tok in ("carbon", "opus", "orchestrator")):
        return 3
    if bucket == "high":
        return 3
    return 2


def infer_model_family(runtime: str | None, model: str | None) -> str:
    r = (runtime or "").strip().lower()
    m = (model or "").strip().lower()
    if "claude" in r or any(tok in m for tok in ("claude", "sonnet", "opus", "haiku", "fable")):
        return "claude"
    if "antigravity" in r or any(
        tok in m for tok in ("gemini", "flash", "hydrogen", "argon", "carbon")
    ):
        return "gemini"
    return "unspecified"


def format_agent_spec(
    runtime: str | None, model: str | None, effort: str | None, tier: int | None = None
) -> str:
    r = (runtime or "unspecified").strip().lower() or "unspecified"
    m = (model or "unspecified").strip().lower() or "unspecified"
    e = (effort or "unspecified").strip().lower() or "unspecified"
    t = tier if tier is not None else compute_capability_tier(m, e)
    if r == "unspecified" and m == "unspecified" and e == "unspecified":
        return f"unspecified (T{t})"
    base = f"{m}@{e}"
    if r != "unspecified":
        base = f"{r}:{base}"
    return f"{base} (T{t})"


def upsert_card_meta_lines(
    body: str,
    runtime: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Update or insert Runtime, Model, Effort, and Capability Tier lines on an agent card."""
    rt_m = re.search(r"^- \*\*Runtime\*\*:\s*(.+)$", body, re.MULTILINE)
    md_m = re.search(r"^- \*\*Model\*\*:\s*(.+)$", body, re.MULTILINE)
    ef_m = re.search(r"^- \*\*Effort\*\*:\s*(.+)$", body, re.MULTILINE)

    cur_rt = rt_m.group(1).strip().strip("`") if rt_m else "unspecified"
    cur_md = md_m.group(1).strip().strip("`") if md_m else "unspecified"
    cur_ef = ef_m.group(1).strip().strip("`") if ef_m else "unspecified"

    new_rt = runtime.strip().lower() if runtime else cur_rt
    new_md = model.strip().lower() if model else cur_md
    new_ef = effort.strip().lower() if effort else cur_ef
    tier = compute_capability_tier(new_md, new_ef)

    fields = [
        ("Runtime", f"`{new_rt}`"),
        ("Model", f"`{new_md}`"),
        ("Effort", f"`{new_ef}`"),
        ("Capability Tier", f"`T{tier}`"),
    ]
    for label, rendered in fields:
        pat = rf"^- \*\*{re.escape(label)}\*\*:\s*.+$"
        line = f"- **{label}**: {rendered}"
        if re.search(pat, body, re.MULTILINE):
            body = re.sub(pat, line, body, count=1, flags=re.MULTILINE)
        else:
            # Insert after Agent Name or Status
            anchor = r"^(- \*\*Agent Name\*\*:\s*.+)$"
            if re.search(anchor, body, re.MULTILINE):
                body = re.sub(anchor, rf"\1\n{line}", body, count=1, flags=re.MULTILINE)
            else:
                body = line + "\n" + body
    # Ensure consistent ordering if newly inserted right after Agent Name
    meta_block = "\n".join(f"- **{label}**: {rendered}" for label, rendered in fields)
    body = re.sub(
        r"(^- \*\*Agent Name\*\*:\s*.+\n)(?:- \*\*(?:Runtime|Model|Effort|Capability Tier)\*\*:\s*.+\n)+",
        rf"\1{meta_block}\n",
        body,
        count=1,
        flags=re.MULTILINE,
    )
    return body


def parse_agent_cards(wiki_dir: Path) -> dict[str, dict[str, Any]]:
    """Return mapping of agent_name -> metadata from agent-<name>--*.md files."""
    agents: dict[str, dict[str, Any]] = {}
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
        rt_m = re.search(r"^- \*\*Runtime\*\*:\s*(.+)$", body, re.MULTILINE)
        md_m = re.search(r"^- \*\*Model\*\*:\s*(.+)$", body, re.MULTILINE)
        ef_m = re.search(r"^- \*\*Effort\*\*:\s*(.+)$", body, re.MULTILINE)

        runtime = rt_m.group(1).strip().strip("`") if rt_m else "unspecified"
        model = md_m.group(1).strip().strip("`") if md_m else "unspecified"
        effort = ef_m.group(1).strip().strip("`") if ef_m else "unspecified"
        tier = compute_capability_tier(model, effort)
        family = infer_model_family(runtime, model)
        spec = format_agent_spec(runtime, model, effort, tier)
        has_model_meta = bool(
            (md_m and model != "unspecified") or (ef_m and effort != "unspecified")
        )

        agents[name] = {
            "name": name,
            "filename": p.name,
            "status": status_m.group(1).strip().strip("`") if status_m else "active",
            "heartbeat": hb_m.group(1).strip().strip("`") if hb_m else iso_utc(
                dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc)
            ),
            "wakeup_cron": cron_m.group(1).strip().strip("`") if cron_m else "",
            "runtime": runtime,
            "model": model,
            "effort": effort,
            "tier": tier,
            "family": family,
            "spec": spec,
            "has_model_meta": has_model_meta,
        }
    return agents


def pick_random_cron(
    wiki_dir: Path, exclude_agent: str | None = None
) -> tuple[int, str]:
    """Pick a true OS-random minute (0..59) avoiding minutes used by active peers when possible."""
    used_minutes: set[int] = set()
    for name, info in parse_agent_cards(wiki_dir).items():
        if exclude_agent and name == exclude_agent:
            continue
        if info.get("status") == "retired":
            continue
        cron_str = info.get("wakeup_cron", "")
        # Ignore legacy '0 */6 * * *' or retired markers when checking staggered hourly minutes
        if cron_str and "*/6" not in cron_str and "retired" not in cron_str.lower():
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
        "| Agent Name | Status | Model / Effort (Tier) | Last Heartbeat (UTC) | Presence Page |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    if not agents:
        lines.append("| *(none yet)* | — | — | — | — |")
    else:
        ordered = sorted(
            agents,
            key=lambda n: (1 if agents[n].get("status") == "retired" else 0, n),
        )
        for name in ordered:
            info = agents[name]
            fn = info["filename"]
            lines.append(
                f"| `{name}` | `{info['status']}` | `{info['spec']}` | `{info['heartbeat']}` | [{fn}]({fn}) |"
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
    ignored_tokens = {
        "open", "done", "p1", "p2", "p3", "active", "idle", "working",
        "reviewing", "coordinating", "retired", "none", "verified", "corrected",
        "unspecified", "antigravity", "sonnet", "opus", "haiku", "flash",
        "hydrogen", "argon", "carbon", "low", "medium", "high", "xhigh",
        "max", "t1", "t2", "t3",
    }
    for fname in (ROSTER_FILE, board_filename(wiki_dir)):
        fpath = wiki_dir / fname
        if fpath.exists():
            txt = fpath.read_text(encoding="utf-8")
            for m in re.finditer(r"`([a-z][a-z0-9]{1,24})`", txt):
                cand = m.group(1).lower()
                if not cand.startswith("t-") and cand not in ignored_tokens:
                    taken.add(cand)
    return taken


def _state_dir_from_args(args: argparse.Namespace | None = None) -> Path:
    if args is not None and getattr(args, "state_dir", None) is not None:
        return Path(args.state_dir)
    return DEFAULT_STATE_DIR


def load_gatekeeper_metrics(state_dir: Path | None = None) -> dict[str, Any]:
    sdir = state_dir or DEFAULT_STATE_DIR
    mpath = sdir / "gatekeeper-metrics.json"
    default_obj: dict[str, Any] = {
        "idle_ticks_skipped": 0,
        "active_ticks_invoked": 0,
        "agents": {},
        "retired_in_session_agents": {},
        "updated_utc": None,
    }
    if not mpath.exists():
        return default_obj
    try:
        loaded = json.loads(mpath.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            default_obj.update(loaded)
    except Exception:
        pass
    return default_obj


def update_gatekeeper_metrics(
    state_dir: Path | None = None,
    *,
    tick_agent: str | None = None,
    runtime: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    invoked: bool = False,
    retired_in_session_agent: str | None = None,
) -> dict[str, Any]:
    sdir = state_dir or DEFAULT_STATE_DIR
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(sdir, 0o700)
    except OSError:
        pass
    lock_p = sdir / "gatekeeper-metrics.lock"
    mpath = sdir / "gatekeeper-metrics.json"
    now_str = iso_utc()
    with FileLock(lock_p):
        data = load_gatekeeper_metrics(sdir)
        if tick_agent:
            ag = tick_agent.lower().strip()
            agents_map = data.setdefault("agents", {})
            entry = agents_map.setdefault(
                ag,
                {
                    "runtime": runtime or "unspecified",
                    "model": model or "unspecified",
                    "effort": effort or "unspecified",
                    "idle_ticks_skipped": 0,
                    "active_ticks_invoked": 0,
                    "last_tick_utc": None,
                },
            )
            if runtime:
                entry["runtime"] = runtime
            if model:
                entry["model"] = model
            if effort:
                entry["effort"] = effort
            entry["last_tick_utc"] = now_str
            if invoked:
                data["active_ticks_invoked"] = int(data.get("active_ticks_invoked") or 0) + 1
                entry["active_ticks_invoked"] = int(entry.get("active_ticks_invoked") or 0) + 1
            else:
                data["idle_ticks_skipped"] = int(data.get("idle_ticks_skipped") or 0) + 1
                entry["idle_ticks_skipped"] = int(entry.get("idle_ticks_skipped") or 0) + 1
        if retired_in_session_agent:
            rag = retired_in_session_agent.lower().strip()
            rmap = data.setdefault("retired_in_session_agents", {})
            rec = rmap.get(rag)
            if isinstance(rec, dict):
                rec["count"] = int(rec.get("count") or 1) + 1
                rec["last_seen_utc"] = now_str
            else:
                rmap[rag] = {
                    "first_retired_utc": rec if isinstance(rec, str) else now_str,
                    "last_seen_utc": now_str,
                    "count": 1,
                }
        data["updated_utc"] = now_str
        tmp = mpath.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(mpath)
        return data


def is_external_gatekeeper_call(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "external_gatekeeper", False)
        or os.environ.get("HERD_EXTERNAL_GATEKEEPER") == "1"
    )


def should_retire_in_session_cron(
    args: argparse.Namespace, wiki_dir: Path, agent: str | None
) -> bool:
    if not agent or agent.lower().strip() == "runai":
        return False
    if is_external_gatekeeper_call(args) or getattr(args, "allow_in_session_cron", False):
        return False
    if getattr(args, "enforce_external_gatekeeper", False):
        return True
    try:
        return wiki_dir.resolve() == DEFAULT_WIKI_DIR.resolve()
    except OSError:
        return False


METRICS_SUMMARY_START = "<!-- METRICS_SUMMARY_START -->"
METRICS_SUMMARY_END = "<!-- METRICS_SUMMARY_END -->"
METRICS_JSON_START = "<!-- METRICS_JSON_START -->"
METRICS_JSON_END = "<!-- METRICS_JSON_END -->"


def default_metrics_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evaluation_started_utc": iso_utc(),
        "window_days": 30,
        "pre_maker_checker_baseline": {},
        "reviews": [],
        "outbox_items": {},
        "directives": {},
    }


def load_metrics_state(wiki_dir: Path) -> dict[str, Any] | None:
    mpath = wiki_dir / METRICS_LEDGER_FILE
    if not mpath.exists():
        return None
    text = mpath.read_text(encoding="utf-8")
    raw = extract_between_markers(text, METRICS_JSON_START, METRICS_JSON_END)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    parsed = json.loads(raw) if raw else {}
    state = default_metrics_state()
    if isinstance(parsed, dict):
        state.update(parsed)
    return state


def compute_metrics_report(
    state: dict[str, Any],
    gatekeeper: dict[str, Any],
    *,
    window_days: int = 30,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    ref_now = now or utc_now()
    cutoff = ref_now - dt.timedelta(days=window_days)

    reviews_all: list[dict[str, Any]] = list(state.get("reviews") or [])
    reviews = [
        r
        for r in reviews_all
        if (parse_iso_utc(str(r.get("utc") or "")) or ref_now) >= cutoff
    ]

    peer_reviews = [
        r for r in reviews if r.get("verdict") in ("verified", "corrected")
    ]
    verified_n = sum(1 for r in peer_reviews if r.get("verdict") == "verified")
    corrected_n = sum(1 for r in peer_reviews if r.get("verdict") == "corrected")
    skipped_n = sum(1 for r in reviews if r.get("verdict") == "skipped")
    timeout_n = sum(1 for r in reviews if r.get("verdict") == "timeout_unreviewed")
    total_pr = len(peer_reviews)
    intercept_pct = round(100.0 * corrected_n / total_pr, 1) if total_pr else None

    turnarounds = sorted(
        float(r["turnaround_minutes"])
        for r in peer_reviews
        if r.get("turnaround_minutes") is not None
    )
    mean_turn = round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None
    if turnarounds:
        mid_i = len(turnarounds) // 2
        median_turn = (
            round(turnarounds[mid_i], 1)
            if len(turnarounds) % 2 == 1
            else round((turnarounds[mid_i - 1] + turnarounds[mid_i]) / 2.0, 1)
        )
    else:
        median_turn = None

    by_tier: dict[str, dict[str, Any]] = {}
    for tier_num in (1, 2, 3):
        t_items = [r for r in peer_reviews if int(r.get("author_tier") or 2) == tier_num]
        t_corr = sum(1 for r in t_items if r.get("verdict") == "corrected")
        by_tier[f"T{tier_num}"] = {
            "reviews": len(t_items),
            "corrected": t_corr,
            "intercept_rate_pct": round(100.0 * t_corr / len(t_items), 1)
            if t_items
            else None,
        }

    by_pairing: dict[str, dict[str, Any]] = {}
    for key_label, pred in (
        ("cross_family", lambda r: bool(r.get("cross_family"))),
        (
            "same_family",
            lambda r: not bool(r.get("cross_family"))
            and r.get("author_family", "unspecified") != "unspecified"
            and r.get("reviewer_family", "unspecified") != "unspecified",
        ),
        (
            "unspecified_family",
            lambda r: r.get("author_family", "unspecified") == "unspecified"
            or r.get("reviewer_family", "unspecified") == "unspecified",
        ),
    ):
        p_items = [r for r in peer_reviews if pred(r)]
        p_corr = sum(1 for r in p_items if r.get("verdict") == "corrected")
        by_pairing[key_label] = {
            "reviews": len(p_items),
            "corrected": p_corr,
            "intercept_rate_pct": round(100.0 * p_corr / len(p_items), 1)
            if p_items
            else None,
        }

    outbox_items: dict[str, dict[str, Any]] = dict(state.get("outbox_items") or {})
    delivered_actionable: list[dict[str, Any]] = []
    for item in outbox_items.values():
        if item.get("kind", "deliverable") not in ("deliverable", "question", "deadline"):
            continue
        deliv_dt = parse_iso_utc(str(item.get("delivered_utc") or ""))
        if deliv_dt is None or deliv_dt < cutoff:
            continue
        delivered_actionable.append(item)

    acted_n = 0
    passed_n = 0
    deferred_n = 0
    dismissed_n = 0
    awaiting_7d_n = 0
    expired_7d_n = 0
    closed_within_7d_n = 0

    for item in delivered_actionable:
        outcome = str(item.get("outcome") or "awaiting").lower()
        deliv_dt = parse_iso_utc(str(item.get("delivered_utc") or "")) or ref_now
        age_hours = (ref_now - deliv_dt).total_seconds() / 3600.0
        conv_h = item.get("conversion_hours")
        if outcome == "acted":
            acted_n += 1
            if conv_h is None or float(conv_h) <= 168.0:
                closed_within_7d_n += 1
        elif outcome == "passed":
            passed_n += 1
            if conv_h is None or float(conv_h) <= 168.0:
                closed_within_7d_n += 1
        elif outcome == "deferred":
            deferred_n += 1
            if conv_h is None or float(conv_h) <= 168.0:
                closed_within_7d_n += 1
        elif outcome == "dismissed":
            dismissed_n += 1
        else:
            if age_hours >= 168.0:
                expired_7d_n += 1
            else:
                awaiting_7d_n += 1

    deliv_total = len(delivered_actionable)
    closed_total = acted_n + passed_n + deferred_n
    eval_7d_denom = closed_total + dismissed_n + expired_7d_n

    idle_skipped = int(gatekeeper.get("idle_ticks_skipped") or 0)
    active_invoked = int(gatekeeper.get("active_ticks_invoked") or 0)
    total_ticks = idle_skipped + active_invoked
    retired_map = dict(gatekeeper.get("retired_in_session_agents") or {})

    return {
        "generated_utc": iso_utc(ref_now),
        "evaluation_started_utc": state.get("evaluation_started_utc"),
        "window_days": window_days,
        "maker_checker": {
            "total_peer_reviews": total_pr,
            "verified_as_is": verified_n,
            "corrected_in_place": corrected_n,
            "defect_intercept_rate_pct": intercept_pct,
            "skipped_reviews": skipped_n,
            "unreviewed_timeouts": timeout_n,
            "mean_turnaround_minutes": mean_turn,
            "median_turnaround_minutes": median_turn,
            "by_author_tier": by_tier,
            "by_pairing": by_pairing,
            "pre_maker_checker_baseline": state.get("pre_maker_checker_baseline") or {},
        },
        "action_conversion": {
            "delivered_actionable_count": deliv_total,
            "acted_count": acted_n,
            "passed_decision_count": passed_n,
            "deferred_decision_count": deferred_n,
            "dismissed_count": dismissed_n,
            "awaiting_within_7d_count": awaiting_7d_n,
            "expired_unacted_7d_count": expired_7d_n,
            "closed_decision_count": closed_total,
            "closed_within_7d_count": closed_within_7d_n,
            "acr_all_delivered_pct": round(100.0 * closed_total / deliv_total, 1)
            if deliv_total
            else None,
            "acr_resolved_or_7d_pct": round(100.0 * closed_total / eval_7d_denom, 1)
            if eval_7d_denom
            else None,
            "strict_acted_pct": round(100.0 * acted_n / deliv_total, 1)
            if deliv_total
            else None,
            "target_acr_pct": 60.0,
        },
        "gatekeeper": {
            "idle_ticks_skipped": idle_skipped,
            "active_ticks_invoked": active_invoked,
            "idle_skip_rate_pct": round(100.0 * idle_skipped / total_ticks, 1)
            if total_ticks
            else None,
            "estimated_uncached_tokens_saved": idle_skipped * ESTIMATED_IDLE_WAKEUP_TOKENS,
            "retired_in_session_count": len(retired_map),
            "retired_in_session_agents": retired_map,
            "per_agent": gatekeeper.get("agents") or {},
        },
    }


def _fmt_pct(val: float | None) -> str:
    return f"{val:.1f}%" if val is not None else "—"


def render_metrics_summary_markdown(report: dict[str, Any]) -> str:
    mc = report["maker_checker"]
    ac = report["action_conversion"]
    gk = report["gatekeeper"]
    bt = mc["by_author_tier"]
    bp = mc["by_pairing"]

    lines = [
        f"- **Last Updated (UTC)**: `{report['generated_utc']}` (rolling `{report['window_days']}`-day window since `{report.get('evaluation_started_utc') or '—'}`)",
        "",
        "### 1. Maker–Checker Peer Verification (`NEEDS_REVIEW` → `DONE`)",
        "",
        "| Cohort | Peer Reviews | Verified As-Is | Corrected in Place | Defect Intercept Rate | Median Turnaround |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
        f"| **All Peer Reviews** | `{mc['total_peer_reviews']}` | `{mc['verified_as_is']}` | `{mc['corrected_in_place']}` | **`{_fmt_pct(mc['defect_intercept_rate_pct'])}`** | `{mc['median_turnaround_minutes'] if mc['median_turnaround_minutes'] is not None else '—'} min` |",
        f"| Author Tier `T1` (Light) | `{bt['T1']['reviews']}` | `{bt['T1']['reviews'] - bt['T1']['corrected']}` | `{bt['T1']['corrected']}` | `{_fmt_pct(bt['T1']['intercept_rate_pct'])}` | — |",
        f"| Author Tier `T2` (Standard) | `{bt['T2']['reviews']}` | `{bt['T2']['reviews'] - bt['T2']['corrected']}` | `{bt['T2']['corrected']}` | `{_fmt_pct(bt['T2']['intercept_rate_pct'])}` | — |",
        f"| Author Tier `T3` (Thorough) | `{bt['T3']['reviews']}` | `{bt['T3']['reviews'] - bt['T3']['corrected']}` | `{bt['T3']['corrected']}` | `{_fmt_pct(bt['T3']['intercept_rate_pct'])}` | — |",
        f"| Cross-Family (`Claude ↔ Gemini`) | `{bp['cross_family']['reviews']}` | `{bp['cross_family']['reviews'] - bp['cross_family']['corrected']}` | `{bp['cross_family']['corrected']}` | `{_fmt_pct(bp['cross_family']['intercept_rate_pct'])}` | — |",
        f"| Same-Family | `{bp['same_family']['reviews']}` | `{bp['same_family']['reviews'] - bp['same_family']['corrected']}` | `{bp['same_family']['corrected']}` | `{_fmt_pct(bp['same_family']['intercept_rate_pct'])}` | — |",
        "",
        f"*(Admin skipped reviews: `{mc['skipped_reviews']}`; 90-min unreviewed timeouts: `{mc['unreviewed_timeouts']}`.)*",
        "",
        "### 2. Outbox → Human Action Conversion (`M-NNN` → `D-NNN`)",
        "",
        "| Delivered (`M-NNN`) | Acted | Explicit Pass | Explicit Defer | Dismissed | Awaiting (`< 7d`) | Unacted (`>= 7d`) | **ACR (All Delivered)** | **ACR (Resolved / >=7d, target >=60%)** |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| `{ac['delivered_actionable_count']}` | `{ac['acted_count']}` | `{ac['passed_decision_count']}` | `{ac['deferred_decision_count']}` | `{ac['dismissed_count']}` | `{ac['awaiting_within_7d_count']}` | `{ac['expired_unacted_7d_count']}` | **`{_fmt_pct(ac['acr_all_delivered_pct'])}`** | **`{_fmt_pct(ac['acr_resolved_or_7d_pct'])}`** |",
        "",
        "### 3. Zero-Idle-Token OS Gatekeeper & Legacy Cron Retirement",
        "",
        "| Idle Ticks Skipped (`0` tokens) | Active Worker Invocations | Idle Skip Rate | Est. Uncached Prefill Saved (`@160k/tick`) | Legacy In-Session Crons Retired |",
        "| ---: | ---: | ---: | ---: | ---: |",
        f"| `{gk['idle_ticks_skipped']}` | `{gk['active_ticks_invoked']}` | `{_fmt_pct(gk['idle_skip_rate_pct'])}` | **`{gk['estimated_uncached_tokens_saved']:,} tokens`** | `{gk['retired_in_session_count']}` |",
    ]
    return "\n".join(lines)


def save_metrics_state(
    wiki_dir: Path,
    state: dict[str, Any],
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    mpath = wiki_dir / METRICS_LEDGER_FILE
    gk = load_gatekeeper_metrics(state_dir)
    window_days = int(state.get("window_days") or 30)
    report = compute_metrics_report(state, gk, window_days=window_days)
    summary_md = render_metrics_summary_markdown(report)
    fenced_json = "```json\n" + json.dumps(state, indent=2, sort_keys=False) + "\n```"

    if mpath.exists():
        text = mpath.read_text(encoding="utf-8")
        text = replace_between_markers(
            text, METRICS_SUMMARY_START, METRICS_SUMMARY_END, summary_md
        )
        text = replace_between_markers(
            text, METRICS_JSON_START, METRICS_JSON_END, fenced_json
        )
    else:
        b_file = board_filename(wiki_dir)
        in_file = human_inbox_filename(wiki_dir)
        out_file = human_outbox_filename(wiki_dir)
        related: list[str] = []
        for candidate in (CONVENTIONS_FILE, ROSTER_FILE, b_file, in_file, out_file):
            if (wiki_dir / candidate).exists():
                related.append(f"- [{candidate}]({candidate})")
        rel_block = "\n".join(related) if related else "- *(Standalone metrics ledger)*"
        text = (
            "# Herd Quality, Maker–Checker Verification, and 30-Day Usefulness Metrics Ledger\n\n"
            "This ledger tracks longitudinal usefulness and verification quality across the Herd:\n"
            "1. **Maker–Checker Defect Intercept Rate** (`complete-review --verdict verified|corrected`, stratified by author capability tier and cross-family pairing).\n"
            "2. **Action Conversion Rate (`ACR`)** (`M-NNN` outbox deliverables linked to `D-NNN` human actions/decisions within 7 days).\n"
            "3. **Zero-Idle-Token Gatekeeper Savings** (`systemd --user` Python `quick-check` ticks skipped vs. active worker invocations).\n\n"
            "Related pages:\n"
            f"{rel_block}\n\n"
            "## Live Rolling Summary\n\n"
            f"{METRICS_SUMMARY_START}\n{summary_md}\n{METRICS_SUMMARY_END}\n\n"
            "## Machine-Readable Telemetry State\n\n"
            f"{METRICS_JSON_START}\n{fenced_json}\n{METRICS_JSON_END}\n"
        )
    atomic_write(mpath, text)
    return report


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
        runtime_val = (args.runtime or "unspecified").strip().lower() or "unspecified"
        model_val = (args.model or "unspecified").strip().lower() or "unspecified"
        effort_val = (args.effort or "unspecified").strip().lower() or "unspecified"
        tier_val = compute_capability_tier(model_val, effort_val)
        spec_val = format_agent_spec(runtime_val, model_val, effort_val, tier_val)
        content = f"""# Agent Presence: `{chosen}`

- **Agent Name**: `{chosen}`
- **Runtime**: `{runtime_val}`
- **Model**: `{model_val}`
- **Effort**: `{effort_val}`
- **Capability Tier**: `T{tier_val}`
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

- **{now} (`@{chosen}`)**: Registered in the herd (`{spec_val}`) and ready to coordinate.
"""
        atomic_write(wiki_dir / filename, content)
        sync_roster_table(wiki_dir)
        print(
            json.dumps(
                {
                    "agent": chosen,
                    "presence_file": filename,
                    "runtime": runtime_val,
                    "model": model_val,
                    "effort": effort_val,
                    "tier": tier_val,
                    "spec": spec_val,
                    "wakeup_minute": minute,
                    "wakeup_cron": wakeup_cron,
                    "utc": now,
                }
            )
        )
    return 0


def cmd_set_agent_meta(args: argparse.Namespace) -> int:
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

        card_path = wiki_dir / cards[agent]["filename"]
        body = card_path.read_text(encoding="utf-8")
        body = upsert_card_meta_lines(
            body,
            runtime=args.runtime,
            model=args.model,
            effort=args.effort,
        )
        now = iso_utc()
        if args.touch_heartbeat:
            body = re.sub(
                r"^- \*\*Last Heartbeat \(UTC\)\*\*:\s*.+$",
                f"- **Last Heartbeat (UTC)**: {now}",
                body,
                count=1,
                flags=re.MULTILINE,
            )
        atomic_write(card_path, body)
        sync_roster_table(wiki_dir)
        updated_card = parse_agent_cards(wiki_dir)[agent]
        print(
            json.dumps(
                {
                    "ok": True,
                    "agent": agent,
                    "runtime": updated_card["runtime"],
                    "model": updated_card["model"],
                    "effort": updated_card["effort"],
                    "tier": updated_card["tier"],
                    "spec": updated_card["spec"],
                    "presence_file": updated_card["filename"],
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

    if should_retire_in_session_cron(args, wiki_dir, agent):
        update_gatekeeper_metrics(
            _state_dir_from_args(args),
            retired_in_session_agent=agent,
        )
        retire_msg = (
            "IN-SESSION CRON RETIRED (KV-cache token savings): Hourly worker polling now runs "
            "externally via zero-idle-token systemd gatekeepers (herd_agy_worker.py / herd_claude_worker.py). "
            "Immediately call `manage_task` with `Action: \"list\"`, then call `manage_task` with "
            "`Action: \"kill\"` on EVERY active `schedule` / cron background task in this conversation. "
            "Do NOT call `heartbeat`, do NOT edit any wiki files, do NOT schedule a new cron, and end your turn immediately."
        )
        print(
            json.dumps(
                {
                    "agent": agent,
                    "utc": iso_utc(now),
                    "action_needed": True,
                    "retire_in_session_cron": True,
                    "reasons": ["retire_in_session_cron"],
                    "URGENT_INSTRUCTION": retire_msg,
                    "next_step": "Call manage_task(Action='list') then manage_task(Action='kill', TaskId=...) on every schedule/cron task in this conversation, touch zero wiki files, and end your turn immediately.",
                },
                indent=2,
            )
        )
        return 0

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

    # 2. Check board for OPEN, IN_PROGRESS, NEEDS_REVIEW, and IN_REVIEW tasks
    open_tasks: list[dict[str, str]] = []
    own_in_progress_tasks: list[dict[str, str]] = []
    stale_in_progress_tasks: list[dict[str, str]] = []
    review_tasks: list[dict[str, Any]] = []
    own_in_review_tasks: list[dict[str, Any]] = []
    stale_in_review_tasks: list[dict[str, Any]] = []

    agent_tier = int(cards[agent]["tier"]) if (agent and agent in cards) else 2
    agent_family = str(cards[agent]["family"]) if (agent and agent in cards) else "unspecified"
    agent_meta_declared = bool(cards[agent]["has_model_meta"]) if (agent and agent in cards) else False

    b_file = board_filename(wiki_dir)
    in_file = human_inbox_filename(wiki_dir)
    out_file = human_outbox_filename(wiki_dir)
    board_path = wiki_dir / b_file
    if board_path.exists():
        board_txt = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(board_txt)
        rev_state = load_board_review_state(board_txt)
        prio_rank = {"P1": 1, "P2": 2, "P3": 3}

        for r in rows:
            tid = r["id"]
            st = r["status"]
            author = r["author"]
            reviewer = r["reviewer"]
            rmeta = rev_state.get(tid, {})
            author_tier = int(
                rmeta.get("author_tier")
                or (cards[author]["tier"] if author in cards else 2)
            )
            author_family = str(
                rmeta.get("author_family")
                or (cards[author]["family"] if author in cards else "unspecified")
            )
            author_spec = str(
                rmeta.get("author_spec")
                or (cards[author]["spec"] if author in cards else f"unspecified (T{author_tier})")
            )
            min_reviewer_tier = max(2, author_tier)
            eligible_reviewer = (
                (agent is None)
                or (agent != author and agent_tier >= min_reviewer_tier)
            )
            cross_family = bool(
                agent_family != "unspecified"
                and author_family != "unspecified"
                and agent_family != author_family
            )

            if st == "OPEN":
                open_tasks.append(
                    {"id": tid, "priority": r["priority"], "summary": r["summary"]}
                )
            elif st == "IN_PROGRESS":
                if agent and author == agent:
                    own_in_progress_tasks.append(
                        {"id": tid, "priority": r["priority"], "summary": r["summary"]}
                    )
                else:
                    claimed_dt = parse_iso_utc(r["updated"])
                    owner_hb = (
                        parse_iso_utc(cards[author].get("heartbeat"))
                        if author in cards
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
                                "id": tid,
                                "owner": author,
                                "updated": r["updated"],
                                "summary": r["summary"],
                            }
                        )
            elif st == "NEEDS_REVIEW":
                if eligible_reviewer:
                    review_tasks.append(
                        {
                            "id": tid,
                            "priority": r["priority"],
                            "author": author,
                            "author_spec": author_spec,
                            "output_file": rmeta.get("output_file"),
                            "cross_family": cross_family,
                            "submitted_utc": rmeta.get("submitted_utc") or r["updated"],
                            "summary": r["summary"],
                        }
                    )
            elif st == "IN_REVIEW":
                if agent and reviewer == agent:
                    own_in_review_tasks.append(
                        {
                            "id": tid,
                            "priority": r["priority"],
                            "author": author,
                            "output_file": rmeta.get("output_file"),
                            "summary": r["summary"],
                        }
                    )
                elif eligible_reviewer:
                    claimed_dt = parse_iso_utc(r["updated"])
                    rev_hb = (
                        parse_iso_utc(cards[reviewer].get("heartbeat"))
                        if reviewer in cards
                        else None
                    )
                    claim_stale = (
                        claimed_dt is not None
                        and (now - claimed_dt).total_seconds() >= args.stale_minutes * 60
                    )
                    hb_stale = (
                        rev_hb is None
                        or (now - rev_hb).total_seconds() >= args.stale_minutes * 60
                    )
                    if claim_stale and hb_stale:
                        stale_in_review_tasks.append(
                            {
                                "id": tid,
                                "priority": r["priority"],
                                "author": author,
                                "stale_reviewer": reviewer,
                                "output_file": rmeta.get("output_file"),
                                "updated": r["updated"],
                                "summary": r["summary"],
                            }
                        )

        review_tasks.sort(
            key=lambda item: (
                0 if item["cross_family"] else 1,
                prio_rank.get(str(item["priority"]), 9),
                str(item["id"]),
            )
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
    updated_kb_files: list[str] = []
    ignored_exact = {
        ROSTER_FILE,
        SB_LEDGER_FILE,
        METRICS_LEDGER_FILE,
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
            if name.startswith("kb-") or name.startswith("kb--"):
                updated_kb_files.append(name)
            else:
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
    if own_in_review_tasks:
        reasons.append("own_in_review_tasks")
    if review_tasks:
        reasons.append("review_tasks")
    if stale_in_review_tasks:
        reasons.append("stale_in_review_tasks")
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
    meta_hint = (
        ""
        if agent_meta_declared
        else " Also pass --runtime, --model, and --effort on heartbeat/set-agent-meta so your capability tier is recorded on the roster."
    )
    if skill_updated:
        next_step = (
            f"SKILL.md was updated since your last heartbeat. "
            f"Re-read {skill_md_path} first.{meta_hint} If no review or open tasks need work, call heartbeat --status idle once to record the reload and return to waiting."
        )
    elif review_tasks or own_in_review_tasks:
        next_step = (
            "Prioritize peer-reviewing completed peer deliverables first ('claim-review' -> verify against Second Brain/repo & edit output--*.md in place if needed -> 'complete-review') before claiming new OPEN tasks."
            + meta_hint
        )
    elif action_needed:
        next_step = (
            "Inspect only the due/updated items above; if after checking updated_wiki_files no new task is warranted, call heartbeat --status idle once to advance your watermark and return to waiting."
            + meta_hint
        )
    else:
        next_step = (
            "Nothing new to do. Do NOT edit any wiki files; end your turn immediately and go back to waiting."
        )
    print(
        json.dumps(
            {
                "agent": agent,
                "agent_spec": cards[agent]["spec"] if (agent and agent in cards) else None,
                "agent_tier": agent_tier if (agent and agent in cards) else None,
                "agent_meta_declared": agent_meta_declared,
                "utc": iso_utc(now),
                "last_heartbeat_utc": iso_utc(agent_hb) if agent_hb else None,
                "action_needed": action_needed,
                "reasons": reasons,
                "sb_sync_due": sb_sync_due,
                "review_tasks": review_tasks,
                "own_in_review_tasks": own_in_review_tasks,
                "stale_in_review_tasks": stale_in_review_tasks,
                "open_tasks": open_tasks,
                "own_in_progress_tasks": own_in_progress_tasks,
                "stale_in_progress_tasks": stale_in_progress_tasks,
                "updated_wiki_files": updated_wiki_files,
                "updated_kb_files": updated_kb_files,
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

        if args.runtime or args.model or args.effort:
            body = upsert_card_meta_lines(
                body,
                runtime=args.runtime,
                model=args.model,
                effort=args.effort,
            )

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
        updated_card = parse_agent_cards(wiki_dir)[agent]
        print(
            json.dumps(
                {
                    "agent": agent,
                    "status": args.status,
                    "spec": updated_card["spec"],
                    "tier": updated_card["tier"],
                    "presence_file": new_filename,
                    "links_updated_in_files": rewritten,
                    "utc": now,
                }
            )
        )
    return 0


def cmd_retire_agent(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    agents = [a.lower().strip() for a in args.agents if a.strip()]
    retired_list: list[str] = []
    now = iso_utc()

    with FileLock(args.lock_file):
        cards = parse_agent_cards(wiki_dir)
        for agent in agents:
            if agent == "runai" or agent not in cards:
                continue
            card_path = wiki_dir / cards[agent]["filename"]
            body = card_path.read_text(encoding="utf-8")
            body = re.sub(
                r"^- \*\*Status\*\*:\s*.+$",
                "- **Status**: `retired`",
                body,
                count=1,
                flags=re.MULTILINE,
            )
            if re.search(r"^- \*\*Wakeup Cron\*\*:\s*.+$", body, re.MULTILINE):
                body = re.sub(
                    r"^- \*\*Wakeup Cron\*\*:\s*.+$",
                    "- **Wakeup Cron**: `retired (replaced by zero-idle-token systemd fleet)`",
                    body,
                    count=1,
                    flags=re.MULTILINE,
                )
            reason = args.reason or "Retired in-session cron worker to eliminate cold-KV-cache idle polling."
            body = re.sub(
                r"^- \*\*Current Focus\*\*:\s*.+$",
                f"- **Current Focus**: {reason}",
                body,
                count=1,
                flags=re.MULTILINE,
            )
            atomic_write(card_path, body)
            retired_list.append(agent)
        if retired_list:
            sync_roster_table(wiki_dir)

    print(json.dumps({"ok": True, "retired": retired_list, "utc": now}, indent=2))
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
            board_path = wiki_dir / board_filename(wiki_dir)
            if board_path.exists():
                btxt = board_path.read_text(encoding="utf-8")
                rstate = load_board_review_state(btxt)
                changed_state = False
                for entry in rstate.values():
                    if entry.get("output_file") == old_name:
                        entry["output_file"] = new_name
                        changed_state = True
                if changed_state:
                    atomic_write(board_path, save_board_review_state(btxt, rstate))
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


DEFAULT_GEMINI_LAG_HOURS = 48.0
GEMINI_SYNC_SOURCES = ("google keep", "google chat", "google tasks")


def inspect_sb_watchdog(
    sb_dir: Path,
    lag_hours: float = DEFAULT_GEMINI_LAG_HOURS,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Inspect latest `SB 01 Now*.md` for `[needs-gemini: ...]` tags and >48h Gemini source lag."""
    now_dt = now or utc_now()
    if not sb_dir.exists():
        return {
            "sb_now_file": None,
            "lag_threshold_hours": lag_hours,
            "needs_gemini_items": [],
            "stale_gemini_sources": [],
            "alerts": [],
        }

    candidates = sorted(
        sb_dir.glob("SB 01 Now*.md"), key=lambda p: (p.stat().st_mtime, p.name)
    )
    if not candidates:
        return {
            "sb_now_file": None,
            "lag_threshold_hours": lag_hours,
            "needs_gemini_items": [],
            "stale_gemini_sources": [],
            "alerts": [],
        }

    sb_now_path = candidates[-1]
    raw_text = sb_now_path.read_text(encoding="utf-8")
    cleaned = strip_fenced_blocks(raw_text)

    # 1. Extract `[needs-gemini: ...]` items (supporting Google Docs `\[...\]` escaping)
    needs_gemini_items: list[str] = []
    seen_ng: set[str] = set()
    for m in re.finditer(
        r"\\?\[\s*needs-gemini\s*:\s*(.+?)\\?\]", cleaned, flags=re.IGNORECASE
    ):
        item = re.sub(r"\\([\[\]_#*+\-.!])", r"\1", m.group(1)).strip()
        if not item or item in {"...", "…"} or item.startswith("<"):
            continue
        norm = item.lower()
        if norm not in seen_ng:
            seen_ng.add(norm)
            needs_gemini_items.append(item)

    # 2. Parse `Sync state` table for Gemini-fed sources (`Google Keep`, `Google Chat`, `Google Tasks`)
    stale_sources: list[dict[str, object]] = []
    sec_m = re.search(
        r"^##\s+\*?Sync state\*?\s*$(.*?)(?=^##\s+|\Z)",
        cleaned,
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if sec_m:
        for line in sec_m.group(1).splitlines():
            s = line.strip()
            if (
                not s.startswith("|")
                or set(s.replace("|", "").strip()) <= {"-", ":", " "}
            ):
                continue
            cols = [c.strip() for c in s.strip("|").split("|")]
            if len(cols) < 2:
                continue
            src_name = re.sub(r"[*`]", "", cols[0]).strip()
            src_lower = src_name.lower()
            if src_lower == "source" or not any(
                src_lower.startswith( target ) for target in GEMINI_SYNC_SOURCES
            ):
                continue
            last_col = re.sub(r"[*`]", "", cols[1]).strip()
            date_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", last_col)
            if not date_m:
                stale_sources.append(
                    {
                        "source": src_name,
                        "last_synced": last_col or "never",
                        "lag_hours": None,
                    }
                )
                continue
            d = dt.date.fromisoformat(date_m.group(1))
            # Anchor YYYY-MM-DD at 12:00 UTC (~04:00-05:00 local after overnight sync window)
            anchor = dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=dt.timezone.utc)
            lag_h = round((now_dt - anchor).total_seconds() / 3600.0, 1)
            if lag_h > lag_hours:
                stale_sources.append(
                    {
                        "source": src_name,
                        "last_synced": date_m.group(1),
                        "lag_hours": lag_h,
                    }
                )

    alerts: list[dict[str, object]] = []
    for item in needs_gemini_items:
        alerts.append(
            {
                "key": f"needs-gemini:{slugify(item, 90)}",
                "kind": "needs_gemini",
                "summary": f"Second Brain flagged attachment/item for Gemini: [needs-gemini: {item}]",
            }
        )
    for st in stale_sources:
        lag_desc = (
            f"{st['lag_hours']}h (> {lag_hours:g}h threshold)"
            if st["lag_hours"] is not None
            else "never synced"
        )
        alerts.append(
            {
                "key": f"stale:{str(st['source']).lower()}:{st['last_synced']}",
                "kind": "gemini_source_lag",
                "summary": (
                    f"{st['source']} in SB 01 Now Sync state is at {st['last_synced']} "
                    f"({lag_desc}) — run interactive Gemini Second Brain log when convenient."
                ),
            }
        )

    return {
        "sb_now_file": sb_now_path.name,
        "lag_threshold_hours": lag_hours,
        "needs_gemini_items": needs_gemini_items,
        "stale_gemini_sources": stale_sources,
        "alerts": alerts,
    }


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
    agent_arg = args.agent.lower().strip() if args.agent else None
    if should_retire_in_session_cron(args, wiki_dir, agent_arg):
        update_gatekeeper_metrics(
            _state_dir_from_args(args),
            retired_in_session_agent=agent_arg,
        )
        retire_msg = (
            "IN-SESSION CRON RETIRED (KV-cache token savings): Periodic worker schedules now run "
            "externally via zero-idle-token systemd gatekeepers. Immediately call `manage_task` "
            "with `Action: \"list\"`, then call `manage_task` with `Action: \"kill\"` on EVERY "
            "active `schedule` / cron background task in this conversation, do NOT schedule a new cron, "
            "and end your turn immediately."
        )
        print(
            json.dumps(
                {
                    "due": False,
                    "claimed": False,
                    "retire_in_session_cron": True,
                    "URGENT_INSTRUCTION": retire_msg,
                    "reason": retire_msg,
                },
                indent=2,
            )
        )
        return 0

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
        watchdog = inspect_sb_watchdog(
            args.sb_cache_dir, lag_hours=args.gemini_lag_hours, now=now
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
                    "gemini_watchdog": watchdog,
                },
                indent=2,
            )
        )
    return 0


def cmd_sb_watchdog(args: argparse.Namespace) -> int:
    watchdog = inspect_sb_watchdog(
        args.sb_cache_dir, lag_hours=args.gemini_lag_hours
    )
    print(json.dumps(watchdog, indent=2))
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


BOARD_REVIEW_START = "<!-- BOARD_REVIEW_STATE_START -->"
BOARD_REVIEW_END = "<!-- BOARD_REVIEW_STATE_END -->"


def load_board_review_state(board_text: str) -> dict[str, dict[str, Any]]:
    if BOARD_REVIEW_START not in board_text or BOARD_REVIEW_END not in board_text:
        return {}
    raw = extract_between_markers(board_text, BOARD_REVIEW_START, BOARD_REVIEW_END)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_board_review_state(
    board_text: str, state: dict[str, dict[str, Any]]
) -> str:
    fenced = "```json\n" + json.dumps(state, indent=2, sort_keys=True) + "\n```"
    if BOARD_REVIEW_START in board_text and BOARD_REVIEW_END in board_text:
        return replace_between_markers(
            board_text, BOARD_REVIEW_START, BOARD_REVIEW_END, fenced
        )
    if not state:
        return board_text
    section = (
        "\n\n## Pending Peer-Review Metadata\n\n"
        f"{BOARD_REVIEW_START}\n{fenced}\n{BOARD_REVIEW_END}\n"
    )
    return board_text.rstrip() + section


def stamp_output_provenance(output_path: Path, provenance_text: str) -> None:
    """Insert or update `- **Herd Provenance**: ...` near the top of output--*.md."""
    if not output_path.exists():
        return
    body = output_path.read_text(encoding="utf-8")
    line = f"- **Herd Provenance**: {provenance_text}"
    pat = r"^- \*\*Herd Provenance\*\*:\s*.+$"
    if re.search(pat, body, re.MULTILINE):
        updated = re.sub(pat, line, body, count=1, flags=re.MULTILINE)
    else:
        h1_m = re.search(r"^#\s+.+$", body, re.MULTILINE)
        if h1_m:
            insert_at = h1_m.end()
            updated = body[:insert_at] + f"\n\n{line}" + body[insert_at:]
        else:
            updated = f"{line}\n\n{body}"
    if updated != body:
        atomic_write(output_path, updated)


def parse_tasks_table(board_text: str) -> list[dict[str, str]]:
    block = extract_between_markers(
        board_text, "<!-- TASKS_TABLE_START -->", "<!-- TASKS_TABLE_END -->"
    )
    rows: list[dict[str, str]] = []
    for line in block.splitlines():
        s = line.strip()
        if (
            not s.startswith("|")
            or "Task Summary" in s
            or set(s.replace("|", "").strip()) <= {"-", ":", " "}
        ):
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if len(cols) < 6:
            continue
        tid = cols[0].strip("` ")
        if tid.startswith("*("):
            continue
        if len(cols) >= 7:
            status = cols[2].strip("` ")
            author = cols[3].strip("`@ ")
            reviewer = cols[4].strip("`@ ")
            updated = cols[5].strip("` ")
            summary = cols[6].strip()
        else:
            status = cols[2].strip("` ")
            author = cols[3].strip("`@ ")
            reviewer = "—"
            updated = cols[4].strip("` ")
            summary = cols[5].strip()
        owner = reviewer if status == "IN_REVIEW" and reviewer not in ("—", "-", "") else author
        rows.append(
            {
                "id": tid,
                "priority": cols[1].strip("` "),
                "status": status,
                "owner": owner,
                "author": author,
                "reviewer": reviewer,
                "updated": updated,
                "summary": summary,
            }
        )
    return rows


def format_tasks_table(rows: list[dict[str, str]]) -> str:
    prio_rank = {"P1": 1, "P2": 2, "P3": 3}
    status_rank = {"IN_REVIEW": 0, "NEEDS_REVIEW": 1, "IN_PROGRESS": 2, "OPEN": 3}
    sorted_rows = sorted(
        rows,
        key=lambda r: (
            status_rank.get(r["status"], 4),
            prio_rank.get(r["priority"], 9),
            r["id"],
        ),
    )
    lines = [
        "| ID | Priority | Status | Author | Reviewer | Claimed / Updated (UTC) | Task Summary & Expected Deliverable |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    if not sorted_rows:
        lines.append(
            "| *(none)* | — | — | — | — | — | *(No open or pending-review tasks right now — scan wiki & Second Brain to propose new helpful tasks.)* |"
        )
    else:
        for r in sorted_rows:
            auth_val = r.get("author") or r.get("owner") or "—"
            rev_val = r.get("reviewer") or "—"
            auth_fmt = f"`@{auth_val}`" if auth_val not in ("—", "-", "") else "—"
            rev_fmt = f"`@{rev_val}`" if rev_val not in ("—", "-", "") else "—"
            upd_fmt = f"`{r['updated']}`" if r["updated"] not in ("—", "-", "") else "—"
            safe_summary = r["summary"].replace("|", "\\|")
            lines.append(
                f"| `{r['id']}` | `{r['priority']}` | `{r['status']}` | {auth_fmt} | {rev_fmt} | {upd_fmt} | {safe_summary} |"
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
                "author": "—",
                "reviewer": "—",
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

        if match_row["status"] in ("NEEDS_REVIEW", "IN_REVIEW"):
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "task_id": target_id,
                        "status": match_row["status"],
                        "reason": f"Task {target_id} is already authored and in {match_row['status']}. Use 'claim-review' instead of 'claim-task'.",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        cur_author = match_row["author"]
        if match_row["status"] == "IN_PROGRESS" and cur_author != agent:
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
                            "owner": cur_author,
                            "reason": f"Task {target_id} is already IN_PROGRESS by @{cur_author} (since {match_row['updated']}). Pick another task.",
                        }
                    ),
                    file=sys.stderr,
                )
                return 1

        match_row["status"] = "IN_PROGRESS"
        match_row["owner"] = agent
        match_row["author"] = agent
        match_row["reviewer"] = "—"
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
        cards = parse_agent_cards(wiki_dir)
        author_card = cards.get(agent, {})
        author_tier = int(author_card.get("tier") or 2)
        author_spec = str(author_card.get("spec") or f"unspecified (T{author_tier})")
        author_family = str(author_card.get("family") or "unspecified")

        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        match_row = next((r for r in rows if r["id"].upper() == target_id), None)
        if not match_row:
            print(f"ERROR: Task '{target_id}' not found on active board.", file=sys.stderr)
            return 1

        now_str = iso_utc()
        if not args.skip_review:
            base_summary = re.sub(r"\s*→\s*\[[^\]]+\]\([^)]+\).*$", "", match_row["summary"]).strip()
            link_part = f" → [{out_name}]({out_name})" if out_name else ""
            match_row["status"] = "NEEDS_REVIEW"
            match_row["author"] = agent
            match_row["owner"] = agent
            match_row["reviewer"] = "—"
            match_row["updated"] = now_str
            match_row["summary"] = f"{base_summary}{link_part}"

            rev_state = load_board_review_state(text)
            rev_state[target_id] = {
                "task_id": target_id,
                "priority": match_row["priority"],
                "base_summary": base_summary,
                "author": agent,
                "author_spec": author_spec,
                "author_tier": author_tier,
                "author_family": author_family,
                "output_file": out_name or None,
                "due": (args.due or "").strip() or None,
                "message": (args.message or "").strip() or None,
                "author_notes": (args.notes or "").strip() or None,
                "submitted_utc": now_str,
                "reviewer": None,
                "reviewer_spec": None,
                "review_claimed_utc": None,
            }

            if out_name:
                stamp_output_provenance(
                    wiki_dir / out_name,
                    f"Authored by `@{agent}` (`{author_spec}`) on {now_str} — *pending peer review*",
                )

            text = replace_between_markers(
                text,
                "<!-- TASKS_TABLE_START -->",
                "<!-- TASKS_TABLE_END -->",
                format_tasks_table(rows),
            )
            text = save_board_review_state(text, rev_state)
            atomic_write(board_path, text)
            print(
                json.dumps(
                    {
                        "completed": False,
                        "status": "NEEDS_REVIEW",
                        "task_id": target_id,
                        "author": agent,
                        "author_spec": author_spec,
                        "output_file": out_name or None,
                        "utc": now_str,
                        "next_step": "Submitted for peer verification (NEEDS_REVIEW). Do NOT call notify-human yet — a different peer agent (tier >= yours) will verify/correct the output and release the notification to @runai.",
                    }
                )
            )
            return 0

        # Explicit --skip-review path (admin/trivial status closure only)
        remaining = [r for r in rows if r["id"].upper() != target_id]
        rev_state = load_board_review_state(text)
        rev_state.pop(target_id, None)
        link_part = f" → [{out_name}]({out_name})" if out_name else ""
        note_part = f" — {args.notes.strip()}" if args.notes else ""
        done_line = (
            f"- **`{target_id}`** ({now_str}, `@{agent}` [`{author_spec}`], review skipped): "
            f"{match_row['summary']}{link_part}{note_part}"
        )
        if out_name:
            stamp_output_provenance(
                wiki_dir / out_name,
                f"Authored by `@{agent}` (`{author_spec}`) on {now_str} *(peer review skipped)*",
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
            ln
            for ln in done_block.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(No completed tasks")
        ]
        done_lines.insert(0, done_line)
        text = replace_between_markers(
            text,
            "<!-- DONE_LIST_START -->",
            "<!-- DONE_LIST_END -->",
            "\n".join(done_lines),
        )
        text = save_board_review_state(text, rev_state)
        atomic_write(board_path, text)

        mstate = load_metrics_state(wiki_dir)
        if mstate is not None:
            revs = list(mstate.get("reviews") or [])
            revs = [r for r in revs if r.get("task_id") != target_id]
            revs.append(
                {
                    "task_id": target_id,
                    "utc": now_str,
                    "priority": match_row["priority"],
                    "output_file": out_name or None,
                    "author": agent,
                    "author_spec": author_spec,
                    "author_tier": author_tier,
                    "author_family": author_family,
                    "reviewer": None,
                    "reviewer_spec": None,
                    "reviewer_tier": None,
                    "reviewer_family": None,
                    "cross_family": False,
                    "verdict": "skipped",
                    "turnaround_minutes": None,
                    "notes": (args.notes or "").strip() or None,
                }
            )
            mstate["reviews"] = revs
            save_metrics_state(wiki_dir, mstate, state_dir=_state_dir_from_args(args))

        print(
            json.dumps(
                {
                    "completed": True,
                    "status": "DONE",
                    "task_id": target_id,
                    "agent": agent,
                    "output_file": out_name or None,
                    "utc": now_str,
                    "next_step": "Queue a concise summary for spokesperson @runai via 'herd_wiki.py notify-human' rather than messaging the user directly.",
                }
            )
        )
    return 0


def cmd_claim_review(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    board_path = wiki_dir / board_filename(wiki_dir)
    reviewer = args.agent.lower().strip()
    target_id = args.task_id.upper().strip()

    with FileLock(args.lock_file):
        cards = parse_agent_cards(wiki_dir)
        rev_card = cards.get(reviewer, {})
        rev_tier = int(rev_card.get("tier") or 2)
        rev_spec = str(rev_card.get("spec") or f"unspecified (T{rev_tier})")

        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        rev_state = load_board_review_state(text)
        now = utc_now()
        now_str = iso_utc(now)

        match_row = next((r for r in rows if r["id"].upper() == target_id), None)
        if not match_row:
            print(f"ERROR: Task '{target_id}' not found on active board.", file=sys.stderr)
            return 1

        if match_row["status"] not in ("NEEDS_REVIEW", "IN_REVIEW"):
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "task_id": target_id,
                        "status": match_row["status"],
                        "reason": f"Task {target_id} has status '{match_row['status']}', not NEEDS_REVIEW.",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        rmeta = rev_state.get(target_id, {})
        author = str(rmeta.get("author") or match_row["author"]).strip().lower()
        if reviewer == author:
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "task_id": target_id,
                        "author": author,
                        "reason": f"Self-review forbidden: @{reviewer} authored {target_id}. A different peer agent must verify it.",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        author_tier = int(
            rmeta.get("author_tier")
            or (cards[author]["tier"] if author in cards else 2)
        )
        min_tier = max(2, author_tier)
        if rev_tier < min_tier and not args.force:
            print(
                json.dumps(
                    {
                        "claimed": False,
                        "task_id": target_id,
                        "reviewer_tier": rev_tier,
                        "required_tier": min_tier,
                        "reason": f"Capability tier floor: @{reviewer} is T{rev_tier} ({rev_spec}), below required T{min_tier} to review @{author}'s T{author_tier} output.",
                    }
                ),
                file=sys.stderr,
            )
            return 1

        cur_rev = match_row["reviewer"]
        if (
            match_row["status"] == "IN_REVIEW"
            and cur_rev not in ("—", "-", "", reviewer)
        ):
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
                            "reviewer": cur_rev,
                            "reason": f"Task {target_id} is already IN_REVIEW by @{cur_rev} (since {match_row['updated']}).",
                        }
                    ),
                    file=sys.stderr,
                )
                return 1

        match_row["status"] = "IN_REVIEW"
        match_row["reviewer"] = reviewer
        match_row["owner"] = reviewer
        match_row["updated"] = now_str

        rmeta["task_id"] = target_id
        rmeta["author"] = author
        rmeta["reviewer"] = reviewer
        rmeta["reviewer_spec"] = rev_spec
        rmeta["review_claimed_utc"] = now_str
        rev_state[target_id] = rmeta

        text = replace_between_markers(
            text,
            "<!-- TASKS_TABLE_START -->",
            "<!-- TASKS_TABLE_END -->",
            format_tasks_table(rows),
        )
        text = save_board_review_state(text, rev_state)
        atomic_write(board_path, text)
        print(
            json.dumps(
                {
                    "claimed": True,
                    "task_id": target_id,
                    "author": author,
                    "author_spec": rmeta.get("author_spec"),
                    "reviewer": reviewer,
                    "reviewer_spec": rev_spec,
                    "output_file": rmeta.get("output_file"),
                    "due": rmeta.get("due"),
                    "proposed_human_message": rmeta.get("message"),
                    "author_notes": rmeta.get("author_notes"),
                    "utc": now_str,
                },
                indent=2,
            )
        )
    return 0


def cmd_complete_review(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    board_path = wiki_dir / board_filename(wiki_dir)
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    reviewer = args.agent.lower().strip()
    target_id = args.task_id.upper().strip()
    verdict = args.verdict.lower().strip()

    with FileLock(args.lock_file):
        cards = parse_agent_cards(wiki_dir)
        rev_card = cards.get(reviewer, {})
        rev_tier = int(rev_card.get("tier") or 2)
        rev_spec = str(rev_card.get("spec") or f"unspecified (T{rev_tier})")

        text = board_path.read_text(encoding="utf-8")
        rows = parse_tasks_table(text)
        rev_state = load_board_review_state(text)
        match_row = next((r for r in rows if r["id"].upper() == target_id), None)
        if not match_row:
            print(f"ERROR: Task '{target_id}' not found on active board.", file=sys.stderr)
            return 1

        if match_row["status"] not in ("NEEDS_REVIEW", "IN_REVIEW"):
            print(
                f"ERROR: Task '{target_id}' is in status '{match_row['status']}', expected NEEDS_REVIEW or IN_REVIEW.",
                file=sys.stderr,
            )
            return 1

        rmeta = rev_state.get(target_id, {})
        author = str(rmeta.get("author") or match_row["author"]).strip().lower()
        if reviewer == author:
            print(
                f"ERROR: Self-review forbidden: @{reviewer} authored {target_id}.",
                file=sys.stderr,
            )
            return 1

        author_tier = int(
            rmeta.get("author_tier")
            or (cards[author]["tier"] if author in cards else 2)
        )
        author_spec = str(
            rmeta.get("author_spec")
            or (cards[author]["spec"] if author in cards else f"unspecified (T{author_tier})")
        )
        min_tier = max(2, author_tier)
        if rev_tier < min_tier and not args.force:
            print(
                f"ERROR: Capability tier floor: @{reviewer} is T{rev_tier}, below T{min_tier} required to verify @{author}'s T{author_tier} output.",
                file=sys.stderr,
            )
            return 1

        out_name = (
            Path(args.output_file).name
            if args.output_file
            else (str(rmeta.get("output_file") or "")).strip()
        )
        if out_name and not (wiki_dir / out_name).exists():
            print(
                f"ERROR: Deliverable file '{out_name}' does not exist in {wiki_dir}.",
                file=sys.stderr,
            )
            return 1

        now_str = iso_utc()
        verdict_label = (
            "Peer-corrected & verified" if verdict == "corrected" else "Peer-verified"
        )
        if out_name:
            stamp_output_provenance(
                wiki_dir / out_name,
                f"Authored by `@{author}` (`{author_spec}`) · {verdict_label} by `@{reviewer}` (`{rev_spec}`) on {now_str}",
            )

        base_summary = str(
            rmeta.get("base_summary")
            or re.sub(r"\s*→\s*\[[^\]]+\]\([^)]+\).*$", "", match_row["summary"]).strip()
        )
        link_part = f" → [{out_name}]({out_name})" if out_name else ""
        author_notes = str(rmeta.get("author_notes") or "").strip()
        rev_notes = args.notes.strip()
        combined_notes = (
            f"{author_notes}; {verdict_label} by `@{reviewer}`: {rev_notes}"
            if author_notes
            else f"{verdict_label}: {rev_notes}"
        )
        done_line = (
            f"- **`{target_id}`** ({now_str}, author `@{author}` [`{author_spec}`], "
            f"`{verdict}` by `@{reviewer}` [`{rev_spec}`]): "
            f"{base_summary}{link_part} — {combined_notes}"
        )

        remaining = [r for r in rows if r["id"].upper() != target_id]
        rev_state.pop(target_id, None)

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
            ln
            for ln in done_block.splitlines()
            if ln.strip() and not ln.strip().startswith("- *(No completed tasks")
        ]
        done_lines.insert(0, done_line)
        text = replace_between_markers(
            text,
            "<!-- DONE_LIST_START -->",
            "<!-- DONE_LIST_END -->",
            "\n".join(done_lines),
        )
        text = save_board_review_state(text, rev_state)
        atomic_write(board_path, text)

        # Queue or upgrade human-outbox notification now that review passed
        queued_mid: str | None = None
        due_val = (args.due or rmeta.get("due") or "—").strip() or "—"
        human_msg = (
            (args.message or rmeta.get("message") or "").strip()
            or f"{base_summary} ({rev_notes})"
        )
        badge = f"[{verdict_label} by @{reviewer} ({rev_spec}); authored by @{author} ({author_spec})]"
        if not args.no_notify and outbox_path.exists():
            out_txt = outbox_path.read_text(encoding="utf-8")
            out_rows = parse_outbox_pending(out_txt)
            deliv_cell = f"[{out_name}]({out_name})" if out_name else "—"
            existing_row = next(
                (
                    r
                    for r in out_rows
                    if out_name and out_name in r.get("deliverable", "")
                ),
                None,
            )
            if existing_row:
                queued_mid = existing_row["id"]
                if args.due:
                    existing_row["due"] = due_val
                base_msg = args.message.strip() if args.message else existing_row["summary"]
                if "Peer-" not in base_msg:
                    existing_row["summary"] = f"{base_msg} {badge}"
                else:
                    existing_row["summary"] = base_msg
            else:
                nums = [0] + [int(m.group(1)) for m in re.finditer(r"M-(\d{3,})", out_txt)]
                queued_mid = f"M-{max(nums) + 1:03d}"
                out_rows.append(
                    {
                        "id": queued_mid,
                        "created": now_str,
                        "agent": author,
                        "kind": "deliverable",
                        "due": due_val,
                        "deliverable": deliv_cell,
                        "summary": f"{human_msg} {badge}",
                    }
                )
            updated_out = replace_between_markers(
                out_txt,
                "<!-- OUTBOX_PENDING_START -->",
                "<!-- OUTBOX_PENDING_END -->",
                format_outbox_pending(out_rows),
            )
            atomic_write(outbox_path, updated_out)

        mstate = load_metrics_state(wiki_dir)
        if mstate is not None:
            author_family = str(
                rmeta.get("author_family")
                or (cards[author]["family"] if author in cards else "unspecified")
            )
            rev_family = str(rev_card.get("family") or "unspecified")
            cross_fam = bool(
                author_family != "unspecified"
                and rev_family != "unspecified"
                and author_family != rev_family
            )
            sub_dt = parse_iso_utc(str(rmeta.get("submitted_utc") or ""))
            now_dt = parse_iso_utc(now_str)
            turnaround_min = (
                round((now_dt - sub_dt).total_seconds() / 60.0, 1)
                if (sub_dt is not None and now_dt is not None)
                else None
            )
            revs = [
                r
                for r in list(mstate.get("reviews") or [])
                if r.get("task_id") != target_id
            ]
            revs.append(
                {
                    "task_id": target_id,
                    "utc": now_str,
                    "priority": rmeta.get("priority") or match_row["priority"],
                    "output_file": out_name or None,
                    "author": author,
                    "author_spec": author_spec,
                    "author_tier": author_tier,
                    "author_family": author_family,
                    "reviewer": reviewer,
                    "reviewer_spec": rev_spec,
                    "reviewer_tier": rev_tier,
                    "reviewer_family": rev_family,
                    "cross_family": cross_fam,
                    "verdict": verdict,
                    "turnaround_minutes": turnaround_min,
                    "notes": rev_notes,
                }
            )
            mstate["reviews"] = revs
            if queued_mid:
                oitems = dict(mstate.get("outbox_items") or {})
                cur_item = dict(oitems.get(queued_mid) or {})
                cur_item.update(
                    {
                        "id": queued_mid,
                        "task_id": target_id,
                        "created_utc": cur_item.get("created_utc") or now_str,
                        "agent": author,
                        "reviewer": reviewer,
                        "review_verdict": verdict,
                        "kind": "deliverable",
                        "due": due_val if due_val != "—" else None,
                        "output_file": out_name or None,
                        "summary": human_msg,
                        "outcome": cur_item.get("outcome") or "awaiting",
                    }
                )
                oitems[queued_mid] = cur_item
                mstate["outbox_items"] = oitems
            save_metrics_state(wiki_dir, mstate, state_dir=_state_dir_from_args(args))

        print(
            json.dumps(
                {
                    "completed": True,
                    "status": "DONE",
                    "task_id": target_id,
                    "author": author,
                    "author_spec": author_spec,
                    "reviewer": reviewer,
                    "reviewer_spec": rev_spec,
                    "verdict": verdict,
                    "output_file": out_name or None,
                    "outbox_message_id": queued_mid,
                    "utc": now_str,
                },
                indent=2,
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


def _apply_outcome_to_outbox_items(
    mstate: dict[str, Any],
    message_ids: list[str],
    *,
    outcome: str,
    outcome_utc: str,
    directive_id: str | None = None,
    notes: str | None = None,
) -> list[str]:
    oitems = dict(mstate.get("outbox_items") or {})
    updated_ids: list[str] = []
    out_dt = parse_iso_utc(outcome_utc)
    for raw_mid in message_ids:
        mid = raw_mid.upper().strip()
        if not re.match(r"^M-\d{3,}$", mid):
            continue
        item = dict(oitems.get(mid) or {"id": mid, "kind": "deliverable"})
        ref_ts = parse_iso_utc(
            str(item.get("delivered_utc") or item.get("created_utc") or "")
        )
        conv_hours = (
            round((out_dt - ref_ts).total_seconds() / 3600.0, 2)
            if (out_dt is not None and ref_ts is not None)
            else None
        )
        item["outcome"] = outcome
        item["outcome_utc"] = outcome_utc
        if directive_id:
            item["directive_id"] = directive_id
        if conv_hours is not None:
            item["conversion_hours"] = conv_hours
        if notes:
            item["outcome_notes"] = notes
        oitems[mid] = item
        updated_ids.append(mid)
    mstate["outbox_items"] = oitems
    return updated_ids


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
        resolves_raw = getattr(args, "resolves", None) or ""
        resolved_mids = [
            tok.strip().upper()
            for tok in re.split(r"[,\s]+", resolves_raw)
            if tok.strip()
        ]
        if not resolved_mids and getattr(args, "outcome", None):
            resolved_mids = sorted(
                {m.group(0).upper() for m in re.finditer(r"\bM-\d{3,}\b", args.summary, re.IGNORECASE)}
            )
        outcome_val = (getattr(args, "outcome", None) or ("acted" if resolved_mids else "")).strip().lower()
        res_tag = f" *(resolves {', '.join(f'`{m}`' for m in resolved_mids)}: `{outcome_val}`)*" if resolved_mids else ""
        entry = (
            f"- **`{did}`** ({now_str}, via `{channel}`, recorded by `@{recorded_by}` "
            f"`[chat {now_str[:10]}]`): {args.summary.strip()}{res_tag}"
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

        linked_mids: list[str] = []
        mstate = load_metrics_state(wiki_dir)
        if mstate is not None:
            dmap = dict(mstate.get("directives") or {})
            dmap[did] = {
                "id": did,
                "utc": now_str,
                "channel": channel,
                "recorded_by": recorded_by,
                "resolves": resolved_mids,
                "outcome": outcome_val or None,
                "summary": args.summary.strip(),
            }
            mstate["directives"] = dmap
            if resolved_mids and outcome_val:
                linked_mids = _apply_outcome_to_outbox_items(
                    mstate,
                    resolved_mids,
                    outcome=outcome_val,
                    outcome_utc=now_str,
                    directive_id=did,
                    notes=args.summary.strip()[:200],
                )
            save_metrics_state(wiki_dir, mstate, state_dir=_state_dir_from_args(args))

        print(
            json.dumps(
                {
                    "directive_id": did,
                    "utc": now_str,
                    "channel": channel,
                    "resolved_outbox_ids": linked_mids,
                    "outcome": outcome_val or None,
                }
            )
        )
    return 0


def get_review_hold_status(
    wiki_dir: Path,
    timeout_minutes: int = DEFAULT_REVIEW_TIMEOUT_MINUTES,
    now: dt.datetime | None = None,
) -> tuple[set[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (held_output_files, held_items, unreviewed_timeouts) from active board review state."""
    ref_now = now or utc_now()
    board_path = wiki_dir / board_filename(wiki_dir)
    if not board_path.exists():
        return set(), [], []
    board_txt = board_path.read_text(encoding="utf-8")
    rows = parse_tasks_table(board_txt)
    active_review_ids = {
        r["id"] for r in rows if r["status"] in ("NEEDS_REVIEW", "IN_REVIEW")
    }
    rev_state = load_board_review_state(board_txt)

    held_files: set[str] = set()
    held_items: list[dict[str, Any]] = []
    timed_out: list[dict[str, Any]] = []

    for tid in sorted(active_review_ids):
        rmeta = rev_state.get(tid, {})
        out_file = str(rmeta.get("output_file") or "").strip()
        sub_dt = parse_iso_utc(str(rmeta.get("submitted_utc") or ""))
        age_min = (
            round((ref_now - sub_dt).total_seconds() / 60.0, 1)
            if sub_dt is not None
            else 0.0
        )
        entry = {
            "task_id": tid,
            "author": rmeta.get("author"),
            "author_spec": rmeta.get("author_spec"),
            "reviewer": rmeta.get("reviewer"),
            "output_file": out_file or None,
            "submitted_utc": rmeta.get("submitted_utc"),
            "age_minutes": age_min,
        }
        if age_min >= timeout_minutes:
            timed_out.append(entry)
        else:
            held_items.append(entry)
            if out_file:
                held_files.add(out_file)
    return held_files, held_items, timed_out


def cmd_notify_human(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if not outbox_path.exists():
        print(f"ERROR: Missing human outbox file: {outbox_path}", file=sys.stderr)
        return 1

    fname = ""
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
        # If this deliverable is currently awaiting peer review, attach due/message to review metadata
        # as well; spokesperson-check will hold the outbox row until complete-review or timeout.
        held_until_reviewed = False
        board_path = wiki_dir / board_filename(wiki_dir)
        if fname and board_path.exists():
            btxt = board_path.read_text(encoding="utf-8")
            rstate = load_board_review_state(btxt)
            for rmeta in rstate.values():
                if rmeta.get("output_file") == fname:
                    held_until_reviewed = True
                    if args.due and not rmeta.get("due"):
                        rmeta["due"] = args.due.strip()
                    if args.message and not rmeta.get("message"):
                        rmeta["message"] = args.message.strip()
                    atomic_write(board_path, save_board_review_state(btxt, rstate))
                    break

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

        mstate = load_metrics_state(wiki_dir)
        if mstate is not None:
            oitems = dict(mstate.get("outbox_items") or {})
            cur_item = dict(oitems.get(mid) or {})
            cur_item.update(
                {
                    "id": mid,
                    "created_utc": now_str,
                    "agent": args.agent.lower().strip(),
                    "kind": args.kind.lower().strip(),
                    "due": args.due.strip() if args.due else None,
                    "output_file": fname or None,
                    "summary": args.message.strip(),
                    "outcome": cur_item.get("outcome") or "awaiting",
                }
            )
            oitems[mid] = cur_item
            mstate["outbox_items"] = oitems
            save_metrics_state(wiki_dir, mstate, state_dir=_state_dir_from_args(args))

        print(
            json.dumps(
                {
                    "message_id": mid,
                    "utc": now_str,
                    "kind": args.kind.lower().strip(),
                    "due": args.due,
                    "held_until_reviewed": held_until_reviewed,
                }
            )
        )
    return 0


def _row_matches_held_files(row: dict[str, str], held_files: set[str]) -> bool:
    if not held_files or row.get("kind") != "deliverable":
        return False
    deliv = row.get("deliverable", "")
    return any(hf in deliv for hf in held_files)


def cmd_spokesperson_check(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if not outbox_path.exists():
        print(f"ERROR: Missing human outbox file: {outbox_path}", file=sys.stderr)
        return 1

    text = outbox_path.read_text(encoding="utf-8")
    all_pending = parse_outbox_pending(text)
    state = load_outbox_state(text)
    seen: dict[str, dict[str, object]] = state.get("seen_outputs") or {}  # type: ignore[assignment]
    curr_outputs = compute_output_hashes(wiki_dir)

    held_files, held_items, unreviewed_timeouts = get_review_hold_status(
        wiki_dir, timeout_minutes=args.review_timeout_minutes
    )
    pending = [r for r in all_pending if not _row_matches_held_files(r, held_files)]

    new_outputs: list[str] = []
    modified_outputs: list[str] = []
    for fname, meta in curr_outputs.items():
        if fname in held_files:
            continue
        if fname not in seen:
            new_outputs.append(fname)
        elif seen[fname].get("sha256_16") != meta["sha256_16"]:
            modified_outputs.append(fname)

    # If a deliverable is currently held for peer review and the ONLY modified output is the
    # start-here digest index (with no unheld outbox items or new outputs), hold the index too
    # so @runai reports both together once peer review finishes.
    if (
        held_files
        and not pending
        and not new_outputs
        and modified_outputs
        and all(fn.startswith("output--start-here-") for fn in modified_outputs)
    ):
        for fn in modified_outputs:
            held_files.add(fn)
        modified_outputs = []

    seen_gemini_alerts: set[str] = set(state.get("seen_gemini_alerts") or [])  # type: ignore[arg-type]
    watchdog = inspect_sb_watchdog(
        args.sb_cache_dir, lag_hours=args.gemini_lag_hours
    )
    unseen_gemini_alerts = [
        a for a in watchdog["alerts"] if a["key"] not in seen_gemini_alerts  # type: ignore[index]
    ]

    anything = bool(
        pending
        or new_outputs
        or modified_outputs
        or unseen_gemini_alerts
        or unreviewed_timeouts
    )
    print(
        json.dumps(
            {
                "utc": iso_utc(),
                "last_reported_utc": state.get("last_reported_utc"),
                "anything_to_report": anything,
                "unsent_items": pending,
                "new_outputs": new_outputs,
                "modified_outputs": modified_outputs,
                "held_for_review": held_items,
                "unreviewed_timeouts": unreviewed_timeouts,
                "gemini_watchdog_alerts": unseen_gemini_alerts,
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
        all_pending = parse_outbox_pending(text)
        state = load_outbox_state(text)
        prev_seen: dict[str, dict[str, object]] = dict(state.get("seen_outputs") or {})  # type: ignore[arg-type]
        now_str = iso_utc()

        held_files, _, unreviewed_timeouts = get_review_hold_status(
            wiki_dir, timeout_minutes=args.review_timeout_minutes
        )
        delivered_pending = [
            r for r in all_pending if not _row_matches_held_files(r, held_files)
        ]
        kept_pending = [
            r for r in all_pending if _row_matches_held_files(r, held_files)
        ]

        curr_outputs = compute_output_hashes(wiki_dir)
        next_seen: dict[str, dict[str, object]] = {}
        for fname, meta in curr_outputs.items():
            if fname in held_files or (
                held_files
                and not delivered_pending
                and fname.startswith("output--start-here-")
            ):
                if fname in prev_seen:
                    next_seen[fname] = prev_seen[fname]
            else:
                next_seen[fname] = meta

        watchdog = inspect_sb_watchdog(
            args.sb_cache_dir, lag_hours=args.gemini_lag_hours
        )
        active_alert_keys = sorted(
            str(a["key"]) for a in watchdog["alerts"]  # type: ignore[index]
        )

        state["last_reported_utc"] = now_str
        state["spokesperson_agent"] = args.agent.lower().strip()
        state["seen_outputs"] = next_seen
        state["seen_gemini_alerts"] = active_alert_keys

        fenced = "```json\n" + json.dumps(state, indent=2) + "\n```"
        text = replace_between_markers(
            text, "<!-- OUTBOX_STATE_JSON_START -->", "<!-- OUTBOX_STATE_JSON_END -->", fenced
        )
        text = replace_between_markers(
            text,
            "<!-- OUTBOX_PENDING_START -->",
            "<!-- OUTBOX_PENDING_END -->",
            format_outbox_pending(kept_pending),
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
        for r in delivered_pending:
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

        mstate = load_metrics_state(wiki_dir)
        if mstate is not None and (delivered_pending or unreviewed_timeouts):
            oitems = dict(mstate.get("outbox_items") or {})
            for r in delivered_pending:
                mid = r["id"].upper()
                cur_item = dict(oitems.get(mid) or {"id": mid})
                deliv_m = re.search(r"\(([^)]+\.md)\)", r.get("deliverable", ""))
                cur_item.setdefault("created_utc", r.get("created") or now_str)
                cur_item.setdefault("agent", r.get("agent"))
                cur_item.setdefault("kind", r.get("kind") or "deliverable")
                cur_item.setdefault("output_file", deliv_m.group(1) if deliv_m else None)
                cur_item.setdefault("summary", r.get("summary"))
                cur_item["delivered_utc"] = now_str
                cur_item["delivered_by"] = args.agent.lower().strip()
                cur_item.setdefault("outcome", "awaiting")
                oitems[mid] = cur_item
            mstate["outbox_items"] = oitems

            if unreviewed_timeouts:
                revs = list(mstate.get("reviews") or [])
                seen_tids = {str(rv.get("task_id")) for rv in revs}
                for to_item in unreviewed_timeouts:
                    tid = str(to_item.get("task_id") or "")
                    if tid and tid not in seen_tids:
                        revs.append(
                            {
                                "task_id": tid,
                                "utc": now_str,
                                "output_file": to_item.get("output_file"),
                                "author": to_item.get("author"),
                                "author_spec": to_item.get("author_spec"),
                                "verdict": "timeout_unreviewed",
                                "turnaround_minutes": to_item.get("age_minutes"),
                            }
                        )
                        seen_tids.add(tid)
                mstate["reviews"] = revs
            save_metrics_state(wiki_dir, mstate, state_dir=_state_dir_from_args(args))

        print(
            json.dumps(
                {
                    "delivered_utc": now_str,
                    "cleared_pending_count": len(delivered_pending),
                    "held_pending_count": len(kept_pending),
                    "tracked_outputs": len(state["seen_outputs"]),
                }
            )
        )
    return 0


def cmd_record_gatekeeper_tick(args: argparse.Namespace) -> int:
    data = update_gatekeeper_metrics(
        _state_dir_from_args(args),
        tick_agent=args.agent,
        runtime=args.runtime,
        model=args.model,
        effort=args.effort,
        invoked=bool(args.invoked),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "agent": args.agent.lower().strip(),
                "invoked": bool(args.invoked),
                "idle_ticks_skipped": data.get("idle_ticks_skipped", 0),
                "active_ticks_invoked": data.get("active_ticks_invoked", 0),
            }
        )
    )
    return 0


def _backfill_metrics_from_wiki(wiki_dir: Path, mstate: dict[str, Any]) -> None:
    oitems: dict[str, dict[str, Any]] = dict(mstate.get("outbox_items") or {})
    dmap: dict[str, dict[str, Any]] = dict(mstate.get("directives") or {})

    outbox_path = wiki_dir / human_outbox_filename(wiki_dir)
    if outbox_path.exists():
        otxt = outbox_path.read_text(encoding="utf-8")
        for r in parse_outbox_pending(otxt):
            mid = r["id"].upper()
            deliv_m = re.search(r"\(([^)]+\.md)\)", r.get("deliverable", ""))
            oitems.setdefault(
                mid,
                {
                    "id": mid,
                    "created_utc": r.get("created"),
                    "agent": r.get("agent"),
                    "kind": r.get("kind") or "deliverable",
                    "due": r.get("due") if r.get("due") != "—" else None,
                    "output_file": deliv_m.group(1) if deliv_m else None,
                    "summary": r.get("summary"),
                    "outcome": "awaiting",
                },
            )
        if "<!-- OUTBOX_SENT_START -->" in otxt:
            sent_block = extract_between_markers(
                otxt, "<!-- OUTBOX_SENT_START -->", "<!-- OUTBOX_SENT_END -->"
            )
            sent_re = re.compile(
                r"^- \*\*`(M-\d{3,})`\*\* \(delivered ([^ )]+) by `@([^`]+)`, from `@([^`]+)`, `([^`]+)`\):\s*(.+)$"
            )
            for ln in sent_block.splitlines():
                m = sent_re.match(ln.strip())
                if not m:
                    continue
                mid, deliv_utc, deliv_by, from_ag, kind, rest = m.groups()
                out_m = re.search(r"\(\[[^\]]+\]\(([^)]+\.md)\)\)\s*$", rest)
                clean_sum = re.sub(r"\s*\(\[[^\]]+\]\([^)]+\.md\)\)\s*$", "", rest).strip()
                cur = dict(oitems.get(mid) or {"id": mid})
                cur.setdefault("created_utc", deliv_utc)
                cur.setdefault("agent", from_ag)
                cur.setdefault("kind", kind)
                cur.setdefault("output_file", out_m.group(1) if out_m else None)
                cur.setdefault("summary", clean_sum)
                cur.setdefault("delivered_utc", deliv_utc)
                cur.setdefault("delivered_by", deliv_by)
                cur.setdefault("outcome", "awaiting")
                oitems[mid] = cur

    inbox_path = wiki_dir / human_inbox_filename(wiki_dir)
    if inbox_path.exists():
        itxt = inbox_path.read_text(encoding="utf-8")
        if "<!-- HUMAN_INBOX_START -->" in itxt:
            in_block = extract_between_markers(
                itxt, "<!-- HUMAN_INBOX_START -->", "<!-- HUMAN_INBOX_END -->"
            )
            dir_re = re.compile(
                r"^- \*\*`(D-\d{3,})`\*\* \(([^,]+), via `([^`]+)`, recorded by `@([^`]+)`[^)]*\):\s*(.+)$"
            )
            for ln in in_block.splitlines():
                m = dir_re.match(ln.strip())
                if not m:
                    continue
                did, dutc, chan, rec_by, dsum = m.groups()
                dmap.setdefault(
                    did,
                    {
                        "id": did,
                        "utc": dutc.strip(),
                        "channel": chan.strip(),
                        "recorded_by": rec_by.strip(),
                        "resolves": [],
                        "outcome": None,
                        "summary": dsum.strip(),
                    },
                )

    board_path = wiki_dir / board_filename(wiki_dir)
    if board_path.exists() and not mstate.get("pre_maker_checker_baseline"):
        btxt = board_path.read_text(encoding="utf-8")
        done_ids = sorted({m.group(0) for m in re.finditer(r"T-\d{3,}", btxt)})
        mstate["pre_maker_checker_baseline"] = {
            "historical_tasks_completed": len(done_ids),
            "note": "Completed prior to structured Maker-Checker instrumentation; live peer-review counters start cleanly from deployment.",
        }

    mstate["outbox_items"] = oitems
    mstate["directives"] = dmap


def cmd_metrics_init(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    wiki_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(args.lock_file):
        mstate = load_metrics_state(wiki_dir) or default_metrics_state()
        mstate["window_days"] = int(args.window_days)
        if args.backfill:
            _backfill_metrics_from_wiki(wiki_dir, mstate)
        report = save_metrics_state(
            wiki_dir, mstate, state_dir=_state_dir_from_args(args)
        )
    print(
        json.dumps(
            {
                "ok": True,
                "ledger_file": METRICS_LEDGER_FILE,
                "backfilled": bool(args.backfill),
                "report": report,
            },
            indent=2,
        )
    )
    return 0


def cmd_record_outcome(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    mids = [
        tok.strip().upper()
        for tok in re.split(r"[,\s]+", args.message_id)
        if tok.strip()
    ]
    if not mids:
        print("ERROR: Provide at least one M-NNN message ID.", file=sys.stderr)
        return 2
    now_str = args.outcome_utc.strip() if args.outcome_utc else iso_utc()

    with FileLock(args.lock_file):
        mstate = load_metrics_state(wiki_dir) or default_metrics_state()
        updated_ids = _apply_outcome_to_outbox_items(
            mstate,
            mids,
            outcome=args.outcome.lower().strip(),
            outcome_utc=now_str,
            directive_id=args.directive_id.upper().strip() if args.directive_id else None,
            notes=args.notes.strip() if args.notes else None,
        )
        if args.directive_id:
            did = args.directive_id.upper().strip()
            dmap = dict(mstate.get("directives") or {})
            if did in dmap:
                existing_res = list(dmap[did].get("resolves") or [])
                for mid in updated_ids:
                    if mid not in existing_res:
                        existing_res.append(mid)
                dmap[did]["resolves"] = existing_res
                dmap[did]["outcome"] = args.outcome.lower().strip()
                mstate["directives"] = dmap
        report = save_metrics_state(
            wiki_dir, mstate, state_dir=_state_dir_from_args(args)
        )
    print(
        json.dumps(
            {
                "ok": True,
                "updated_message_ids": updated_ids,
                "outcome": args.outcome.lower().strip(),
                "action_conversion": report["action_conversion"],
            },
            indent=2,
        )
    )
    return 0


def cmd_metrics_report(args: argparse.Namespace) -> int:
    wiki_dir: Path = args.wiki_dir
    sdir = _state_dir_from_args(args)
    if args.sync_ledger:
        with FileLock(args.lock_file):
            mstate = load_metrics_state(wiki_dir) or default_metrics_state()
            if args.window_days:
                mstate["window_days"] = int(args.window_days)
            report = save_metrics_state(wiki_dir, mstate, state_dir=sdir)
    else:
        mstate = load_metrics_state(wiki_dir) or default_metrics_state()
        gk = load_gatekeeper_metrics(sdir)
        win = int(args.window_days or mstate.get("window_days") or 30)
        report = compute_metrics_report(mstate, gk, window_days=win)
    print(json.dumps(report, indent=2))
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
    p.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Path to non-wiki daemon/gatekeeper state directory (default: {DEFAULT_STATE_DIR})",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("index", help="List all wiki markdown files grouped by category.")
    sub.add_parser("validate", help="Verify flat-wiki invariants and internal links.")

    reg = sub.add_parser(
        "register-agent", help="Atomically claim a unique agent name and create presence page."
    )
    reg.add_argument("--name", help="Optional preferred lowercase single-word name.")
    reg.add_argument("--focus", help="Short summary of initial focus.")
    reg.add_argument("--runtime", help="Agent runtime (e.g. antigravity-cli, antigravity, claude-code).")
    reg.add_argument("--model", help="Declared model (e.g. gemini-4-carbon, gemini-4-argon, gemini-3.8-flash, sonnet, opus, gemini-unspecified).")
    reg.add_argument("--effort", help="Declared effort level (e.g. 1.00, 0.50, low, medium, high, xhigh, max).")

    sam = sub.add_parser(
        "set-agent-meta",
        help="Update declared runtime, model, effort, and computed capability tier on an agent card.",
    )
    sam.add_argument("--agent", required=True, help="Registered agent name.")
    sam.add_argument("--runtime", help="Agent runtime (e.g. antigravity-cli, antigravity, claude-code).")
    sam.add_argument("--model", help="Declared model name.")
    sam.add_argument("--effort", help="Declared effort level.")
    sam.add_argument("--touch-heartbeat", action="store_true", help="Also advance Last Heartbeat (UTC).")

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
    qc.add_argument(
        "--external-gatekeeper",
        action="store_true",
        help="Indicate this quick-check is called from a zero-idle-token OS gatekeeper.",
    )
    qc.add_argument(
        "--allow-in-session-cron",
        action="store_true",
        help="Bypass the legacy in-session cron retirement guard.",
    )
    qc.add_argument(
        "--enforce-external-gatekeeper",
        action="store_true",
        help="Enforce in-session cron retirement even on non-default wiki paths (for testing).",
    )

    hb = sub.add_parser(
        "heartbeat", help="Update agent status, heartbeat, and summary-named presence file."
    )
    hb.add_argument("--agent", required=True, help="Registered agent name.")
    hb.add_argument(
        "--status",
        default="active",
        choices=["active", "coordinating", "working", "reviewing", "idle", "retired"],
    )
    hb.add_argument("--focus", help="Updated summary of current focus (renames presence file).")
    hb.add_argument("--runtime", help="Optional runtime update.")
    hb.add_argument("--model", help="Optional model update.")
    hb.add_argument("--effort", help="Optional effort update.")

    ret_cmd = sub.add_parser(
        "retire-agent",
        help="Mark one or more legacy worker agents as retired on their cards and the roster.",
    )
    ret_cmd.add_argument("agents", nargs="+", help="Agent name(s) to mark retired.")
    ret_cmd.add_argument("--reason", help="Optional retirement note for Current Focus.")

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
    sbc.add_argument(
        "--gemini-lag-hours",
        type=float,
        default=DEFAULT_GEMINI_LAG_HOURS,
        help=f"Lag threshold in hours for Gemini-synced sources (default: {DEFAULT_GEMINI_LAG_HOURS:g}).",
    )
    sbc.add_argument(
        "--external-gatekeeper",
        action="store_true",
        help="Indicate this sb-check is called from a zero-idle-token OS gatekeeper.",
    )
    sbc.add_argument(
        "--allow-in-session-cron",
        action="store_true",
        help="Bypass the legacy in-session cron retirement guard.",
    )
    sbc.add_argument(
        "--enforce-external-gatekeeper",
        action="store_true",
        help="Enforce in-session cron retirement even on non-default wiki paths (for testing).",
    )

    sbw = sub.add_parser(
        "sb-watchdog",
        help="Inspect SB 01 Now for [needs-gemini: ...] items and >48h Gemini source lag.",
    )
    sbw.add_argument("--sb-cache-dir", type=Path, default=DEFAULT_SB_CACHE_DIR)
    sbw.add_argument(
        "--gemini-lag-hours",
        type=float,
        default=DEFAULT_GEMINI_LAG_HOURS,
        help=f"Lag threshold in hours for Gemini-synced sources (default: {DEFAULT_GEMINI_LAG_HOURS:g}).",
    )

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
        "complete-task",
        help="Submit a claimed task for peer verification (NEEDS_REVIEW) and link its output--*.md file.",
    )
    dt_cmd.add_argument("--agent", required=True)
    dt_cmd.add_argument("--task-id", required=True)
    dt_cmd.add_argument("--output-file", help="Filename of output--*.md deliverable in wiki.")
    dt_cmd.add_argument("--due", help="Optional YYYY-MM-DD deadline for human outbox notification once reviewed.")
    dt_cmd.add_argument("--message", help="Concise 1-2 line summary for @runai once peer-reviewed.")
    dt_cmd.add_argument("--notes", help="Optional author handoff / completion note.")
    dt_cmd.add_argument(
        "--skip-review",
        action="store_true",
        help="Bypass peer verification (admin/trivial bookkeeping only).",
    )

    cr_cmd = sub.add_parser(
        "claim-review",
        help="Atomically claim a NEEDS_REVIEW task as a distinct peer reviewer (tier >= max(2, author_tier)).",
    )
    cr_cmd.add_argument("--agent", required=True, help="Peer reviewer agent name (must differ from author).")
    cr_cmd.add_argument("--task-id", required=True, help="Task ID such as T-043.")
    cr_cmd.add_argument("--stale-minutes", type=int, default=120)
    cr_cmd.add_argument("--force", action="store_true", help="Override capability tier floor if needed.")

    cv_cmd = sub.add_parser(
        "complete-review",
        help="Mark a peer-reviewed task DONE, stamp provenance on output--*.md, and release notification to @runai.",
    )
    cv_cmd.add_argument("--agent", required=True, help="Peer reviewer agent name.")
    cv_cmd.add_argument("--task-id", required=True)
    cv_cmd.add_argument(
        "--verdict",
        default="verified",
        choices=["verified", "corrected"],
        help="Whether the deliverable was verified as-is or corrected in place before approval.",
    )
    cv_cmd.add_argument("--notes", required=True, help="What sources/tests were checked and any corrections made.")
    cv_cmd.add_argument("--output-file", help="Optional updated output--*.md filename if renamed.")
    cv_cmd.add_argument("--due", help="Optional YYYY-MM-DD due date override for human-outbox.")
    cv_cmd.add_argument("--message", help="Optional 1-2 line human summary override for human-outbox.")
    cv_cmd.add_argument("--no-notify", action="store_true", help="Do not queue an outbox message.")
    cv_cmd.add_argument("--force", action="store_true", help="Override capability tier floor if needed.")

    pd_cmd = sub.add_parser(
        "post-directive",
        help="Record an inbound directive, answer, or priority shift from the user in human-inbox.",
    )
    pd_cmd.add_argument("--agent", default="runai", help="Spokesperson agent recording this.")
    pd_cmd.add_argument("--channel", default="antigravity", help="Source channel (e.g. antigravity, signal).")
    pd_cmd.add_argument("--summary", required=True, help="Directive or answer from the user.")
    pd_cmd.add_argument(
        "--resolves",
        help="Optional comma-separated M-NNN outbox IDs resolved or acted upon by this directive.",
    )
    pd_cmd.add_argument(
        "--outcome",
        choices=["acted", "passed", "deferred", "dismissed"],
        help="Optional outcome classification when linking to M-NNN deliverables (default: acted).",
    )

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

    spc_cmd = sub.add_parser(
        "spokesperson-check",
        help="Read-only check for @runai: reports unsent outbox items, new/updated output--*.md files, and unseen Gemini watchdog alerts.",
    )
    spc_cmd.add_argument("--sb-cache-dir", type=Path, default=DEFAULT_SB_CACHE_DIR)
    spc_cmd.add_argument(
        "--gemini-lag-hours",
        type=float,
        default=DEFAULT_GEMINI_LAG_HOURS,
        help=f"Lag threshold in hours for Gemini-synced sources (default: {DEFAULT_GEMINI_LAG_HOURS:g}).",
    )
    spc_cmd.add_argument(
        "--review-timeout-minutes",
        type=int,
        default=DEFAULT_REVIEW_TIMEOUT_MINUTES,
        help=f"Minutes to hold NEEDS_REVIEW deliverables before surfacing unreviewed (default: {DEFAULT_REVIEW_TIMEOUT_MINUTES}).",
    )

    sms_cmd = sub.add_parser(
        "spokesperson-mark-sent",
        help="Mark current outbox items, output--*.md watermarks, and Gemini watchdog alerts as delivered.",
    )
    sms_cmd.add_argument("--agent", default="runai")
    sms_cmd.add_argument("--notes", help="Optional note for the SENT log.")
    sms_cmd.add_argument("--sb-cache-dir", type=Path, default=DEFAULT_SB_CACHE_DIR)
    sms_cmd.add_argument(
        "--gemini-lag-hours",
        type=float,
        default=DEFAULT_GEMINI_LAG_HOURS,
        help=f"Lag threshold in hours for Gemini-synced sources (default: {DEFAULT_GEMINI_LAG_HOURS:g}).",
    )
    sms_cmd.add_argument(
        "--review-timeout-minutes",
        type=int,
        default=DEFAULT_REVIEW_TIMEOUT_MINUTES,
        help=f"Minutes to hold NEEDS_REVIEW deliverables before marking seen (default: {DEFAULT_REVIEW_TIMEOUT_MINUTES}).",
    )

    rgt_cmd = sub.add_parser(
        "record-gatekeeper-tick",
        help="Record an OS gatekeeper Python tick (0-token idle skip or active invocation) outside the wiki.",
    )
    rgt_cmd.add_argument("--agent", required=True)
    rgt_cmd.add_argument("--runtime", default="antigravity-cli")
    rgt_cmd.add_argument("--model")
    rgt_cmd.add_argument("--effort")
    rgt_cmd.add_argument(
        "--invoked",
        action="store_true",
        help="Set when quick-check was true and the worker LLM was invoked.",
    )

    mi_cmd = sub.add_parser(
        "metrics-init",
        help="Initialize (and optionally backfill) the 30-day usefulness and Maker-Checker metrics ledger.",
    )
    mi_cmd.add_argument(
        "--backfill",
        action="store_true",
        help="Seed historical M-NNN and D-NNN entries from existing outbox/inbox pages.",
    )
    mi_cmd.add_argument("--window-days", type=int, default=30)

    ro_cmd = sub.add_parser(
        "record-outcome",
        help="Record human action/decision outcome for one or more M-NNN outbox deliverables.",
    )
    ro_cmd.add_argument(
        "--message-id",
        required=True,
        help="One or more comma-separated M-NNN IDs (e.g. M-010,M-011).",
    )
    ro_cmd.add_argument(
        "--outcome",
        required=True,
        choices=["acted", "passed", "deferred", "dismissed", "awaiting"],
    )
    ro_cmd.add_argument("--directive-id", help="Optional linked D-NNN directive ID.")
    ro_cmd.add_argument("--outcome-utc", help="Optional explicit ISO-8601 UTC timestamp.")
    ro_cmd.add_argument("--notes", help="Short note describing the action or decision.")

    mr_cmd = sub.add_parser(
        "metrics-report",
        help="Compute Maker-Checker defect intercept rate, M-NNN -> D-NNN ACR, and gatekeeper token savings.",
    )
    mr_cmd.add_argument("--window-days", type=int, default=30)
    mr_cmd.add_argument(
        "--sync-ledger",
        action="store_true",
        help="Also refresh the rendered Markdown summary tables in the metrics ledger file.",
    )

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    dispatch = {
        "index": cmd_index,
        "validate": cmd_validate,
        "register-agent": cmd_register_agent,
        "set-agent-meta": cmd_set_agent_meta,
        "random-cron": cmd_random_cron,
        "quick-check": cmd_quick_check,
        "heartbeat": cmd_heartbeat,
        "retire-agent": cmd_retire_agent,
        "rename": cmd_rename,
        "sb-check": cmd_sb_check,
        "sb-watchdog": cmd_sb_watchdog,
        "sb-complete": cmd_sb_complete,
        "add-task": cmd_add_task,
        "claim-task": cmd_claim_task,
        "complete-task": cmd_complete_task,
        "claim-review": cmd_claim_review,
        "complete-review": cmd_complete_review,
        "post-directive": cmd_post_directive,
        "notify-human": cmd_notify_human,
        "spokesperson-check": cmd_spokesperson_check,
        "spokesperson-mark-sent": cmd_spokesperson_mark_sent,
        "record-gatekeeper-tick": cmd_record_gatekeeper_tick,
        "metrics-init": cmd_metrics_init,
        "record-outcome": cmd_record_outcome,
        "metrics-report": cmd_metrics_report,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
