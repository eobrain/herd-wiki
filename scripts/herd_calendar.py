#!/usr/bin/env python3
"""Concurrency-safe helper for managing the dedicated secondary "Herd" Google Calendar.

Uses pure Python 3.12 stdlib + `cryptography` (RS256 Service Account JWT) so zero
extra pip packages are required, and synchronizes all state with the flat wiki
ledger `~/src/herd/wiki/meta--herd-calendar-events-ledger-and-sync-rules.md`.

Safety invariants:
  - Structurally requires a secondary Google Calendar ID (`*@group.calendar.google.com`)
    and rejects `primary` or any user/work email address so herd agents can NEVER
    write to a primary personal or corporate calendar.
  - Defaults `transparency` to `"transparent"` ("Free") so reminders/deadlines never
    block scheduling links (e.g. Calendly) unless `--busy` is explicitly passed.
  - Tracks `COMPLETED` and `DISMISSED_BY_USER` slugs so events deleted by the user on
    their phone are never recreated by herd agents.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

WIKI_DIR = Path(
    os.environ.get("HERD_WIKI_DIR", str(Path.home() / "src/herd/wiki"))
).resolve()
LOCK_PATH = Path(
    os.environ.get("HERD_WIKI_LOCK", str(Path.home() / ".cache/herd-wiki.lock"))
)
CONFIG_PATH = Path(
    os.environ.get("HERD_CALENDAR_CONFIG", str(Path.home() / ".config/herd/calendar.json"))
)
TOKEN_CACHE_PATH = Path(
    os.environ.get(
        "HERD_CALENDAR_TOKEN_CACHE", str(Path.home() / ".cache/herd-calendar-token.json")
    )
)
DEFAULT_SA_PATH = Path(
    os.environ.get(
        "HERD_CALENDAR_SA_FILE", str(Path.home() / ".config/rclone/second-brain-sa.json")
    )
)
LEDGER_FILE = "meta--herd-calendar-events-ledger-and-sync-rules.md"

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
API_BASE = "https://www.googleapis.com/calendar/v3"
SECONDARY_CALENDAR_SUFFIX = "@group.calendar.google.com"

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def utc_iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def wiki_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def sanitize_cell(text: str) -> str:
    return " ".join(text.strip().splitlines()).replace("|", "/")


def validate_calendar_id(cal_id: str) -> str:
    cid = cal_id.strip()
    if (
        not cid
        or cid.lower() == "primary"
        or not cid.lower().endswith(SECONDARY_CALENDAR_SUFFIX)
    ):
        raise SystemExit(
            f"SAFETY BLOCK: Refusing target calendar_id '{cid}'. "
            f"Herd agents may only write to a dedicated secondary 'Herd' calendar (*{SECONDARY_CALENDAR_SUFFIX})."
        )
    return cid


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Herd Calendar not yet configured ({CONFIG_PATH} missing). "
            "Run `herd_calendar.py setup --calendar-id <id>@group.calendar.google.com` first."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["calendar_id"] = validate_calendar_id(cfg.get("calendar_id", ""))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_access_token(sa_path: Path) -> str:
    now = int(time.time())
    if TOKEN_CACHE_PATH.exists():
        try:
            cached = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("sa_path") == str(sa_path) and cached.get("exp", 0) - 120 > now:
                return cached["access_token"]
        except Exception:
            pass

    sa = json.loads(sa_path.read_text(encoding="utf-8"))
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64url(
        json.dumps(
            {
                "iss": sa["client_email"],
                "scope": CALENDAR_SCOPE,
                "aud": sa["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    private_key = serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
    signature = b64url(private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    assertion = f"{header}.{claims}.{signature}"

    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        sa["token_uri"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"OAuth token exchange failed (HTTP {exc.code}): {detail}") from exc

    access_token = tok_data["access_token"]
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"sa_path": str(sa_path), "access_token": access_token, "exp": now + int(tok_data.get("expires_in", 3600))}),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_CACHE_PATH)
    return access_token


def api_request(
    method: str,
    path: str,
    token: str,
    query: dict[str, str] | None = None,
    payload: dict | None = None,
    allow_statuses: tuple[int, ...] = (),
) -> tuple[int, dict]:
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if exc.code in allow_statuses:
            try:
                return exc.code, (json.loads(raw) if raw.strip() else {})
            except Exception:
                return exc.code, {"raw": raw}
        raise SystemExit(f"Google Calendar API {method} {path} failed (HTTP {exc.code}): {raw}") from exc


def parse_markdown_table_block(text: str, start_marker: str, end_marker: str) -> list[list[str]]:
    if start_marker not in text or end_marker not in text:
        return []
    block = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    rows: list[list[str]] = []
    for line in block.strip().splitlines():
        s = line.strip()
        if not s.startswith("|") or s.startswith("|---"):
            continue
        cols = [c.strip() for c in s.strip("|").split("|")]
        if cols and cols[0].lower() == "slug":
            continue
        rows.append(cols)
    return rows


def replace_block(text: str, start_marker: str, end_marker: str, new_inner: str) -> str:
    before, rest = text.split(start_marker, 1)
    _, after = rest.split(end_marker, 1)
    return f"{before}{start_marker}\n{new_inner.rstrip()}\n{end_marker}{after}"


def load_ledger() -> tuple[str, dict[str, dict], dict[str, dict]]:
    ledger_path = WIKI_DIR / LEDGER_FILE
    text = ledger_path.read_text(encoding="utf-8")
    active: dict[str, dict] = {}
    for cols in parse_markdown_table_block(text, "<!-- CAL_ACTIVE_START -->", "<!-- CAL_ACTIVE_END -->"):
        if len(cols) >= 7:
            slug = cols[0].strip("`")
            active[slug] = {
                "slug": slug,
                "kind": cols[1].strip("`"),
                "when": cols[2],
                "title": cols[3],
                "event_id": cols[4].strip("`"),
                "updated_utc": cols[5].strip("`"),
                "agent": cols[6],
            }
    closed: dict[str, dict] = {}
    for cols in parse_markdown_table_block(text, "<!-- CAL_CLOSED_START -->", "<!-- CAL_CLOSED_END -->"):
        if len(cols) >= 5:
            slug = cols[0].strip("`")
            closed[slug] = {
                "slug": slug,
                "resolution": cols[1].strip("`"),
                "title": cols[2],
                "closed_utc": cols[3].strip("`"),
                "agent": cols[4],
            }
    return text, active, closed


def save_ledger(
    text: str,
    active: dict[str, dict],
    closed: dict[str, dict],
    configured: bool | None = None,
    reconciled_utc: str | None = None,
) -> None:
    if configured is not None:
        val = "`true`" if configured else "`false`"
        text = re.sub(r"- `configured`: .*", f"- `configured`: {val}", text)
    if reconciled_utc is not None:
        text = re.sub(r"- `last_reconciled_utc`: .*", f"- `last_reconciled_utc`: `{reconciled_utc}`", text)

    active_lines = [
        "| Slug | Kind | When | Title | Event ID | Updated (UTC) | By |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in sorted(active.values(), key=lambda r: (r["when"], r["slug"])):
        active_lines.append(
            f"| `{item['slug']}` | `{item['kind']}` | {sanitize_cell(item['when'])} | "
            f"{sanitize_cell(item['title'])} | `{item['event_id']}` | `{item['updated_utc']}` | {sanitize_cell(item['agent'])} |"
        )

    closed_lines = [
        "| Slug | Resolution | Title | Closed (UTC) | By |",
        "|---|---|---|---|---|",
    ]
    for item in sorted(closed.values(), key=lambda r: (r["closed_utc"], r["slug"])):
        closed_lines.append(
            f"| `{item['slug']}` | `{item['resolution']}` | {sanitize_cell(item['title'])} | "
            f"`{item['closed_utc']}` | {sanitize_cell(item['agent'])} |"
        )

    text = replace_block(text, "<!-- CAL_ACTIVE_START -->", "<!-- CAL_ACTIVE_END -->", "\n".join(active_lines))
    text = replace_block(text, "<!-- CAL_CLOSED_START -->", "<!-- CAL_CLOSED_END -->", "\n".join(closed_lines))
    ledger_path = WIKI_DIR / LEDGER_FILE
    tmp = ledger_path.parent / f"tmp-cal-{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(ledger_path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def add_days_ymd(ymd: str, days: int) -> str:
    d = dt.date.fromisoformat(ymd) + dt.timedelta(days=days)
    return d.isoformat()


def format_event_when(ev: dict) -> str:
    start = ev.get("start", {})
    end = ev.get("end", {})
    if "date" in start:
        s_date = start["date"]
        e_date = end.get("date", "")
        if e_date and e_date != add_days_ymd(s_date, 1):
            return f"{s_date}..{add_days_ymd(e_date, -1)}"
        return s_date
    return start.get("dateTime", "unknown")


def cmd_setup(args: argparse.Namespace) -> int:
    cal_id = validate_calendar_id(args.calendar_id)
    sa_path = Path(args.sa_file).expanduser().resolve()
    if not sa_path.exists():
        raise SystemExit(f"Service account file not found: {sa_path}")

    token = get_access_token(sa_path)
    enc_id = urllib.parse.quote(cal_id, safe="")

    # Bind into service account's calendarList (benign if already present or unsupported)
    api_request("POST", "/users/me/calendarList", token, payload={"id": cal_id}, allow_statuses=(400, 403, 404, 409))

    _, meta = api_request("GET", f"/calendars/{enc_id}", token)
    cfg = {
        "calendar_id": cal_id,
        "calendar_summary": meta.get("summary", ""),
        "time_zone": meta.get("timeZone", "America/Los_Angeles"),
        "sa_file": str(sa_path),
        "configured_utc": utc_iso(),
    }
    save_config(cfg)
    with wiki_lock():
        text, active, closed = load_ledger()
        save_ledger(text, active, closed, configured=True)

    print(json.dumps({"ok": True, "config_path": str(CONFIG_PATH), **cfg}, indent=2))
    return 0


def cmd_create_calendar(args: argparse.Namespace) -> int:
    sa_path = Path(args.sa_file).expanduser().resolve()
    token = get_access_token(sa_path)
    _, created = api_request(
        "POST",
        "/calendars",
        token,
        payload={
            "summary": args.title,
            "description": "Dedicated Herd assistant calendar for vetted appointments, deadlines, and reminders.",
            "timeZone": args.timezone,
        },
    )
    cal_id = validate_calendar_id(created["id"])
    enc_id = urllib.parse.quote(cal_id, safe="")
    shared_with: list[dict] = []
    for email in args.share_writer or []:
        _, acl = api_request(
            "POST",
            f"/calendars/{enc_id}/acl",
            token,
            query={"sendNotifications": "true"},
            payload={"role": "writer", "scope": {"type": "user", "value": email.strip()}},
        )
        shared_with.append({"email": email.strip(), "role": acl.get("role", "writer")})

    cfg = {
        "calendar_id": cal_id,
        "calendar_summary": created.get("summary", args.title),
        "time_zone": created.get("timeZone", args.timezone),
        "sa_file": str(sa_path),
        "configured_utc": utc_iso(),
    }
    save_config(cfg)
    with wiki_lock():
        text, active, closed = load_ledger()
        save_ledger(text, active, closed, configured=True)

    print(json.dumps({"ok": True, "shared_with": shared_with, **cfg}, indent=2))
    return 0


def cmd_upsert(args: argparse.Namespace) -> int:
    slug = args.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise SystemExit(f"Invalid --slug '{slug}': must be lowercase kebab-case ([a-z0-9-]).")

    cfg = load_config()
    token = get_access_token(Path(cfg["sa_file"]))
    enc_id = urllib.parse.quote(cfg["calendar_id"], safe="")
    tz = args.timezone or cfg.get("time_zone", "America/Los_Angeles")
    agent_tag = f"@{args.agent.lstrip('@')}"

    if args.date:
        end_exclusive = args.end_date if args.end_date else add_days_ymd(args.date, 1)
        start_obj = {"date": args.date}
        end_obj = {"date": end_exclusive}
    else:
        if not (args.start and args.end):
            raise SystemExit("Provide either `--date YYYY-MM-DD` OR both `--start` and `--end` ISO8601 datetimes.")
        start_obj = {"dateTime": args.start, "timeZone": tz}
        end_obj = {"dateTime": args.end, "timeZone": tz}

    body: dict = {
        "summary": args.title.strip(),
        "description": args.description.strip(),
        "start": start_obj,
        "end": end_obj,
        "transparency": "opaque" if args.busy else "transparent",
        "extendedProperties": {
            "private": {
                "herdSlug": slug,
                "herdKind": args.kind,
                "herdAgent": agent_tag,
            }
        },
    }
    if args.location:
        body["location"] = args.location.strip()

    with wiki_lock():
        text, active, closed = load_ledger()
        if slug in closed and not args.force_reopen:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": True,
                        "slug": slug,
                        "reason": f"Slug is already marked {closed[slug]['resolution']} in {LEDGER_FILE}.",
                    },
                    indent=2,
                )
            )
            return 0

        existing_event: dict | None = None
        if slug in active:
            ev_id = urllib.parse.quote(active[slug]["event_id"], safe="")
            code, ev = api_request("GET", f"/calendars/{enc_id}/events/{ev_id}", token, allow_statuses=(404, 410))
            if code in (404, 410) or ev.get("status") == "cancelled":
                if not args.force_reopen:
                    closed[slug] = {
                        "slug": slug,
                        "resolution": "DISMISSED_BY_USER",
                        "title": active[slug]["title"],
                        "closed_utc": utc_iso(),
                        "agent": "@calendar-sync",
                    }
                    active.pop(slug, None)
                    save_ledger(text, active, closed)
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "skipped": True,
                                "slug": slug,
                                "reason": "Event was deleted on Google Calendar by user; recorded DISMISSED_BY_USER.",
                            },
                            indent=2,
                        )
                    )
                    return 0
            else:
                existing_event = ev

        if existing_event is None:
            _, search_res = api_request(
                "GET",
                f"/calendars/{enc_id}/events",
                token,
                query={"privateExtendedProperty": f"herdSlug={slug}", "showDeleted": "true"},
            )
            items = search_res.get("items", [])
            live = [it for it in items if it.get("status") != "cancelled"]
            cancelled = [it for it in items if it.get("status") == "cancelled"]
            if live:
                existing_event = live[0]
            elif cancelled and not args.force_reopen:
                closed[slug] = {
                    "slug": slug,
                    "resolution": "DISMISSED_BY_USER",
                    "title": cancelled[0].get("summary") or args.title.strip(),
                    "closed_utc": utc_iso(),
                    "agent": "@calendar-sync",
                }
                active.pop(slug, None)
                save_ledger(text, active, closed)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "skipped": True,
                            "slug": slug,
                            "reason": "Cancelled event with matching herdSlug found on Google Calendar; recorded DISMISSED_BY_USER.",
                        },
                        indent=2,
                    )
                )
                return 0

        closed.pop(slug, None)
        if existing_event is not None:
            ev_id = urllib.parse.quote(existing_event["id"], safe="")
            _, saved_ev = api_request("PATCH", f"/calendars/{enc_id}/events/{ev_id}", token, payload=body)
            action = "updated"
        else:
            _, saved_ev = api_request("POST", f"/calendars/{enc_id}/events", token, payload=body)
            action = "created"

        now_s = utc_iso()
        active[slug] = {
            "slug": slug,
            "kind": args.kind,
            "when": format_event_when(saved_ev),
            "title": saved_ev.get("summary", args.title.strip()),
            "event_id": saved_ev["id"],
            "updated_utc": now_s,
            "agent": agent_tag,
        }
        save_ledger(text, active, closed)

    print(
        json.dumps(
            {
                "ok": True,
                "action": action,
                "slug": slug,
                "event_id": saved_ev["id"],
                "when": format_event_when(saved_ev),
                "htmlLink": saved_ev.get("htmlLink", ""),
            },
            indent=2,
        )
    )
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    slug = args.slug.strip().lower()
    cfg = load_config()
    token = get_access_token(Path(cfg["sa_file"]))
    enc_id = urllib.parse.quote(cfg["calendar_id"], safe="")
    agent_tag = f"@{args.agent.lstrip('@')}"
    resolution = args.resolution.upper()

    with wiki_lock():
        text, active, closed = load_ledger()
        entry = active.get(slug)
        event_id = entry["event_id"] if entry else None
        title = entry["title"] if entry else slug

        if not event_id:
            _, search_res = api_request(
                "GET",
                f"/calendars/{enc_id}/events",
                token,
                query={"privateExtendedProperty": f"herdSlug={slug}"},
            )
            items = [it for it in search_res.get("items", []) if it.get("status") != "cancelled"]
            if items:
                event_id = items[0]["id"]
                title = items[0].get("summary", title)

        if event_id:
            ev_enc = urllib.parse.quote(event_id, safe="")
            if args.keep_checked:
                checked_title = title if title.startswith("✓ ") else f"✓ {title}"
                api_request("PATCH", f"/calendars/{enc_id}/events/{ev_enc}", token, payload={"summary": checked_title}, allow_statuses=(404, 410))
            else:
                api_request("DELETE", f"/calendars/{enc_id}/events/{ev_enc}", token, allow_statuses=(404, 410))

        active.pop(slug, None)
        closed[slug] = {
            "slug": slug,
            "resolution": resolution,
            "title": title,
            "closed_utc": utc_iso(),
            "agent": agent_tag,
        }
        save_ledger(text, active, closed)

    print(json.dumps({"ok": True, "slug": slug, "resolution": resolution, "deleted_from_calendar": not args.keep_checked}, indent=2))
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    cfg = load_config()
    token = get_access_token(Path(cfg["sa_file"]))
    enc_id = urllib.parse.quote(cfg["calendar_id"], safe="")

    _, res = api_request(
        "GET",
        f"/calendars/{enc_id}/events",
        token,
        query={"maxResults": "250", "singleEvents": "true", "showDeleted": "true"},
    )
    by_id = {it["id"]: it for it in res.get("items", []) if "id" in it}
    dismissed_now: list[str] = []
    now_s = utc_iso()

    with wiki_lock():
        text, active, closed = load_ledger()
        for slug, item in list(active.items()):
            ev = by_id.get(item["event_id"])
            if ev is None:
                code, single = api_request(
                    "GET",
                    f"/calendars/{enc_id}/events/{urllib.parse.quote(item['event_id'], safe='')}",
                    token,
                    allow_statuses=(404, 410),
                )
                if code in (404, 410) or single.get("status") == "cancelled":
                    ev = {"status": "cancelled"}
                else:
                    ev = single
            if ev.get("status") == "cancelled":
                closed[slug] = {
                    "slug": slug,
                    "resolution": "DISMISSED_BY_USER",
                    "title": item["title"],
                    "closed_utc": now_s,
                    "agent": f"@{args.agent.lstrip('@')}",
                }
                active.pop(slug, None)
                dismissed_now.append(slug)
            else:
                item["when"] = format_event_when(ev)
                item["title"] = ev.get("summary", item["title"])
        save_ledger(text, active, closed, configured=True, reconciled_utc=now_s)

    live_events = [
        {
            "id": it["id"],
            "summary": it.get("summary", ""),
            "when": format_event_when(it),
            "herdSlug": it.get("extendedProperties", {}).get("private", {}).get("herdSlug"),
            "transparency": it.get("transparency", "opaque"),
        }
        for it in res.get("items", [])
        if it.get("status") != "cancelled"
    ]
    live_events.sort(key=lambda e: e["when"])
    print(
        json.dumps(
            {
                "ok": True,
                "reconciled_utc": now_s,
                "dismissed_by_user": dismissed_now,
                "active_herd_count": len(active),
                "live_calendar_events": live_events,
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Manage the dedicated secondary 'Herd' Google Calendar.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_setup = sub.add_parser("setup", help="Bind and verify an existing shared 'Herd' Calendar ID.")
    s_setup.add_argument("--calendar-id", required=True, help="Secondary Calendar ID (...group.calendar.google.com)")
    s_setup.add_argument("--sa-file", default=str(DEFAULT_SA_PATH), help="Path to Service Account JSON key")
    s_setup.set_defaults(func=cmd_setup)

    s_create = sub.add_parser(
        "create-calendar",
        help="Fallback: create the 'Herd' calendar directly under the Service Account and share writer ACLs.",
    )
    s_create.add_argument("--title", default="Herd")
    s_create.add_argument("--timezone", default="America/Los_Angeles")
    s_create.add_argument("--sa-file", default=str(DEFAULT_SA_PATH))
    s_create.add_argument("--share-writer", action="append", help="Email address to grant writer ACL (repeatable)")
    s_create.set_defaults(func=cmd_create_calendar)

    s_up = sub.add_parser("upsert", help="Idempotently create or update an event on the 'Herd' calendar by slug.")
    s_up.add_argument("--slug", required=True, help="Stable kebab-case event slug (e.g. nov-16-western-exterminator)")
    s_up.add_argument("--kind", choices=["appointment", "deadline", "reminder"], required=True)
    s_up.add_argument("--title", required=True, help="Concise mobile calendar title")
    s_up.add_argument("--date", help="All-day start date YYYY-MM-DD")
    s_up.add_argument("--end-date", help="Optional exclusive end date YYYY-MM-DD for multi-day all-day events")
    s_up.add_argument("--start", help="Timed event start ISO8601 (e.g. 2026-11-16T08:00:00-08:00)")
    s_up.add_argument("--end", help="Timed event end ISO8601 (e.g. 2026-11-16T12:00:00-08:00)")
    s_up.add_argument("--timezone", help="IANA timezone (default America/Los_Angeles)")
    s_up.add_argument("--location", help="Optional street address or URL")
    s_up.add_argument("--description", required=True, help="Self-contained mobile summary (no local file:// links)")
    s_up.add_argument("--agent", required=True, help="Herd agent name")
    s_up.add_argument("--busy", action="store_true", help="Mark opaque/Busy (default is transparent/Free)")
    s_up.add_argument("--force-reopen", action="store_true", help="Override COMPLETED/DISMISSED_BY_USER guard")
    s_up.set_defaults(func=cmd_upsert)

    s_comp = sub.add_parser("complete", help="Remove a completed/obsolete event from 'Herd' calendar and close slug.")
    s_comp.add_argument("--slug", required=True)
    s_comp.add_argument("--agent", required=True)
    s_comp.add_argument("--resolution", default="COMPLETED", choices=["COMPLETED", "SUPERSEDED", "CANCELLED"])
    s_comp.add_argument("--keep-checked", action="store_true", help="Keep event on calendar prefixed with '✓ '")
    s_comp.set_defaults(func=cmd_complete)

    s_rec = sub.add_parser("reconcile", help="List live events and detect user-deleted events on Google Calendar.")
    s_rec.add_argument("--agent", default="runai")
    s_rec.set_defaults(func=cmd_reconcile)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
