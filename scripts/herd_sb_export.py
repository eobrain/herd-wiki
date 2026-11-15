#!/usr/bin/env python3
"""Safe exporter from Herd to Second Brain via a standing Google Doc in `HerdInbox`.

Why an in-place standing Google Doc (`SB Log Herd`) inside `HerdInbox`?
  - GCP Service Accounts have 0 bytes of personal Google Drive storage quota on
    consumer Google accounts, so `files.create` (`POST /drive/v3/files`) fails
    with HTTP 403 `storageQuotaExceeded`.
  - When the user creates a single blank Google Doc (`SB Log Herd`) inside the
    `HerdInbox` subfolder, the user remains the file owner while the Service Account
    inherits `Editor` (`canEdit: true`) permission and updates the Doc in place via
    `PATCH /upload/drive/v3/files/{fileId}?uploadType=media` (0 quota consumed!).

Safety invariants:
  - Hard-verifies on every write that the target Google Doc's `parents` contains
    `inbox_folder_id` and NEVER `root_folder_id_forbidden`.
  - Blocks lines matching secret/credential patterns before uploading.
  - Stores Drive IDs and local fact state outside Git under `~/.config/herd/` and
    `~/.local/state/herd/` (`0600`).
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

CONFIG_PATH = Path(
    os.environ.get(
        "HERD_SB_EXPORT_CONFIG", str(Path.home() / ".config/herd/second-brain-export.json")
    )
)
STATE_DIR = Path(
    os.environ.get("HERD_SIGNAL_STATE_DIR", str(Path.home() / ".local/state/herd"))
)
FACTS_JSONL = STATE_DIR / "sb-export-facts.jsonl"
TOKEN_CACHE = Path(
    os.environ.get(
        "HERD_DRIVE_TOKEN_CACHE", str(Path.home() / ".cache/herd-drive-token.json")
    )
)
LOCK_PATH = Path(
    os.environ.get("HERD_WIKI_LOCK", str(Path.home() / ".cache/herd-wiki.lock"))
)
DEFAULT_SA_PATH = Path(
    os.environ.get(
        "HERD_CALENDAR_SA_FILE", str(Path.home() / ".config/rclone/second-brain-sa.json")
    )
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

SECRET_PATTERNS = [
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}\b"),
    re.compile(r"signalcaptcha://", re.IGNORECASE),
]


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_ymd() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


@contextmanager
def export_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def check_no_secrets(text: str) -> None:
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            raise SystemExit(
                f"SAFETY BLOCK: Fact text matched secret pattern '{pat.pattern}'; refusing to export."
            )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Second Brain export config missing ({CONFIG_PATH}). Run `herd_sb_export.py configure` first."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_PATH.parent, 0o700)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def load_facts() -> list[dict]:
    if not FACTS_JSONL.exists():
        return []
    out: list[dict] = []
    for line in FACTS_JSONL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def save_facts(items: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    tmp = FACTS_JSONL.with_suffix(".tmp")
    tmp.write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(FACTS_JSONL)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_drive_token(sa_path: Path) -> str:
    now = int(time.time())
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if cached.get("sa_path") == str(sa_path) and cached.get("exp", 0) - 120 > now:
                return cached["access_token"]
        except Exception:
            pass

    sa = json.loads(sa_path.read_text(encoding="utf-8"))
    hdr = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    clm = b64url(
        json.dumps(
            {
                "iss": sa["client_email"],
                "scope": DRIVE_SCOPE,
                "aud": sa["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
            separators=(",", ":"),
        ).encode()
    )
    key = serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
    sig = b64url(key.sign(f"{hdr}.{clm}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256()))
    body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": f"{hdr}.{clm}.{sig}",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        sa["token_uri"],
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok_data = json.loads(resp.read().decode("utf-8"))

    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_CACHE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "sa_path": str(sa_path),
                "access_token": tok_data["access_token"],
                "exp": now + int(tok_data.get("expires_in", 3600)),
            }
        ),
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_CACHE)
    return tok_data["access_token"]


def drive_get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Drive GET failed (HTTP {exc.code}): {detail}") from exc


def verify_target_doc(cfg: dict, token: str, doc_id: str) -> dict:
    url = (
        f"{DRIVE_API}/files/{urllib.parse.quote(doc_id, safe='')}"
        "?fields=id,name,mimeType,parents,capabilities(canEdit)"
    )
    meta = drive_get_json(url, token)
    parents = meta.get("parents") or []
    inbox_id = cfg["inbox_folder_id"]
    forbid_id = cfg.get("root_folder_id_forbidden")
    if forbid_id and forbid_id in parents:
        raise SystemExit(
            f"SAFETY BLOCK: Refusing to write to file '{meta.get('name')}' inside SecondBrain root folder!"
        )
    if inbox_id not in parents:
        raise SystemExit(
            f"SAFETY BLOCK: Target doc '{doc_id}' is not directly inside configured HerdInbox folder '{inbox_id}'."
        )
    if not meta.get("capabilities", {}).get("canEdit"):
        raise SystemExit(
            f"Target doc '{meta.get('name')}' ({doc_id}) does not grant canEdit permission to the service account."
        )
    return meta


def discover_doc_in_inbox(cfg: dict, token: str) -> dict | None:
    inbox_id = cfg["inbox_folder_id"]
    title = cfg.get("doc_title", "SB Log Herd")
    q = f"'{inbox_id}' in parents and trashed = false"
    url = f"{DRIVE_API}/files?{urllib.parse.urlencode({'q': q, 'fields': 'files(id,name,mimeType,parents,capabilities(canEdit))'})}"
    files = drive_get_json(url, token).get("files", [])
    exact = [f for f in files if f.get("name") == title]
    if exact:
        return exact[0]
    docs = [f for f in files if f.get("mimeType") == "application/vnd.google-apps.document"]
    return docs[0] if len(docs) == 1 else None


def render_markdown(facts: list[dict]) -> str:
    now_s = utc_iso()
    pending = [f for f in facts if not f.get("consolidated")]
    merged = [f for f in facts if f.get("consolidated")][-30:]

    lines = [
        "# SB Log Herd (Standing Herd → Second Brain Outbox)",
        "",
        f"Last updated by Herd (`@runai` / `herd_sb_export.py`): {now_s}.",
        "Note for Second Brain consolidator: keep this standing document in `HerdInbox` (do NOT move it to `Archive/`, because the Service Account updates this user-owned Google Doc in place). Merge items under 'Unmerged Facts' into `SB 01`–`SB 30`.",
        "",
        "## Unmerged Facts (Since Last Second Brain Consolidation)",
        "",
    ]
    if not pending:
        lines.append("- No new unmerged facts.")
    else:
        for item in pending:
            lines.append(
                f"- {item['fact']} [{item['source']} {item['date']}; id={item['id']}]"
            )

    if merged:
        lines.extend(
            [
                "",
                "## Recently Consolidated (Reference Only — Already Merged)",
                "",
            ]
        )
        for item in merged:
            lines.append(
                f"- {item['fact']} [{item['source']} {item['date']}; id={item['id']}]"
            )

    return "\n".join(lines) + "\n"


def upload_doc_content(cfg: dict, token: str, doc_id: str, markdown_text: str) -> dict:
    check_no_secrets(markdown_text)
    verify_target_doc(cfg, token, doc_id)
    url = (
        f"{DRIVE_UPLOAD_API}/files/{urllib.parse.quote(doc_id, safe='')}"
        "?uploadType=media&fields=id,name,modifiedTime"
    )
    req = urllib.request.Request(
        url,
        data=markdown_text.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/markdown; charset=UTF-8",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Drive PATCH upload failed (HTTP {exc.code}): {detail}") from exc


def cmd_configure(args: argparse.Namespace) -> int:
    sa_path = Path(args.sa_file).expanduser().resolve()
    if not sa_path.exists():
        raise SystemExit(f"Service account file missing: {sa_path}")
    existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = {
        "inbox_folder_id": args.inbox_folder_id.strip(),
        "root_folder_id_forbidden": (args.forbid_folder_id or existing.get("root_folder_id_forbidden") or "").strip(),
        "doc_title": args.doc_title.strip(),
        "doc_file_id": existing.get("doc_file_id"),
        "sa_file": str(sa_path),
        "updated_utc": utc_iso(),
    }
    if cfg["root_folder_id_forbidden"] and cfg["inbox_folder_id"] == cfg["root_folder_id_forbidden"]:
        raise SystemExit("SAFETY BLOCK: inbox_folder_id must not equal root_folder_id_forbidden.")
    save_config(cfg)
    print(json.dumps({"ok": True, "doc_title": cfg["doc_title"], "doc_bound": bool(cfg["doc_file_id"])}, indent=2))
    return 0


def cmd_bind_doc(args: argparse.Namespace) -> int:
    cfg = load_config()
    token = get_drive_token(Path(cfg["sa_file"]))
    if args.doc_id:
        meta = verify_target_doc(cfg, token, args.doc_id.strip())
    else:
        found = discover_doc_in_inbox(cfg, token)
        if not found:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": f"No Google Doc named '{cfg.get('doc_title', 'SB Log Herd')}' found inside HerdInbox yet.",
                    },
                    indent=2,
                )
            )
            return 1
        meta = verify_target_doc(cfg, token, found["id"])

    cfg["doc_file_id"] = meta["id"]
    cfg["doc_title"] = meta["name"]
    cfg["bound_utc"] = utc_iso()
    save_config(cfg)
    print(json.dumps({"ok": True, "doc_name": meta["name"], "canEdit": True}, indent=2))
    return 0


def cmd_add_fact(args: argparse.Namespace) -> int:
    fact_text = " ".join(args.fact.strip().splitlines())
    check_no_secrets(fact_text)

    with export_lock():
        facts = load_facts()
        for existing in facts:
            if existing["fact"] == fact_text and not existing.get("consolidated"):
                print(json.dumps({"ok": True, "deduplicated": True, "id": existing["id"]}, indent=2))
                return 0
        fid = f"H-{len(facts) + 1:04d}"
        entry = {
            "id": fid,
            "date": args.date or today_ymd(),
            "source": args.source.strip(),
            "fact": fact_text,
            "agent": f"@{args.agent.lstrip('@')}",
            "created_utc": utc_iso(),
            "consolidated": False,
        }
        facts.append(entry)
        save_facts(facts)
        md = render_markdown(facts)

    if args.local_only:
        print(json.dumps({"ok": True, "id": fid, "uploaded": False, "pending_count": sum(not f.get("consolidated") for f in facts)}, indent=2))
        return 0

    cfg = load_config()
    token = get_drive_token(Path(cfg["sa_file"]))
    doc_id = cfg.get("doc_file_id")
    if not doc_id:
        found = discover_doc_in_inbox(cfg, token)
        if not found:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "id": fid,
                        "uploaded": False,
                        "note": "Queued locally; create Google Doc 'SB Log Herd' in HerdInbox and run `herd_sb_export.py sync`.",
                    },
                    indent=2,
                )
            )
            return 0
        doc_id = found["id"]
        cfg["doc_file_id"] = doc_id
        save_config(cfg)

    res = upload_doc_content(cfg, token, doc_id, md)
    print(json.dumps({"ok": True, "id": fid, "uploaded": True, "modifiedTime": res.get("modifiedTime")}, indent=2))
    return 0


def cmd_sync(_args: argparse.Namespace) -> int:
    cfg = load_config()
    token = get_drive_token(Path(cfg["sa_file"]))
    doc_id = cfg.get("doc_file_id")
    if not doc_id:
        found = discover_doc_in_inbox(cfg, token)
        if not found:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": f"No Google Doc found inside HerdInbox yet. Create a blank Google Doc titled '{cfg.get('doc_title', 'SB Log Herd')}' inside HerdInbox.",
                    },
                    indent=2,
                )
            )
            return 1
        doc_id = found["id"]
        cfg["doc_file_id"] = doc_id
        save_config(cfg)

    with export_lock():
        facts = load_facts()
        md = render_markdown(facts)

    res = upload_doc_content(cfg, token, doc_id, md)
    print(
        json.dumps(
            {
                "ok": True,
                "doc_name": res.get("name"),
                "modifiedTime": res.get("modifiedTime"),
                "pending_facts": sum(not f.get("consolidated") for f in facts),
                "total_facts": len(facts),
            },
            indent=2,
        )
    )
    return 0


def cmd_mark_consolidated(args: argparse.Namespace) -> int:
    cutoff = args.through_date.strip()
    changed = 0
    with export_lock():
        facts = load_facts()
        for f in facts:
            if not f.get("consolidated") and f.get("date", "") <= cutoff:
                f["consolidated"] = True
                f["consolidated_utc"] = utc_iso()
                changed += 1
        save_facts(facts)
        md = render_markdown(facts)

    uploaded = False
    if CONFIG_PATH.exists():
        cfg = load_config()
        if cfg.get("doc_file_id") and not args.local_only:
            token = get_drive_token(Path(cfg["sa_file"]))
            upload_doc_content(cfg, token, cfg["doc_file_id"], md)
            uploaded = True

    print(json.dumps({"ok": True, "marked_consolidated": changed, "uploaded": uploaded}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export durable Herd facts to `HerdInbox/SB Log Herd` on Google Drive.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_cfg = sub.add_parser("configure", help="Configure HerdInbox Drive folder ID.")
    s_cfg.add_argument("--inbox-folder-id", required=True)
    s_cfg.add_argument("--forbid-folder-id", help="Root SecondBrain folder ID to hard-block")
    s_cfg.add_argument("--doc-title", default="SB Log Herd")
    s_cfg.add_argument("--sa-file", default=str(DEFAULT_SA_PATH))
    s_cfg.set_defaults(func=cmd_configure)

    s_bind = sub.add_parser("bind-doc", help="Discover and bind the user-created Google Doc inside HerdInbox.")
    s_bind.add_argument("--doc-id", help="Optional explicit Google Doc file ID")
    s_bind.set_defaults(func=cmd_bind_doc)

    s_add = sub.add_parser("add-fact", help="Record one durable fact and sync `SB Log Herd`.")
    s_add.add_argument("--source", required=True, help="Provenance tag (e.g. chat, signal, github, herd-calendar)")
    s_add.add_argument("--date", help="YYYY-MM-DD (defaults to today UTC)")
    s_add.add_argument("--fact", required=True, help="Single-line durable fact (no secrets/keys)")
    s_add.add_argument("--agent", default="runai")
    s_add.add_argument("--local-only", action="store_true", help="Queue locally without uploading yet")
    s_add.set_defaults(func=cmd_add_fact)

    s_sync = sub.add_parser("sync", help="Push current local facts to `SB Log Herd` in HerdInbox.")
    s_sync.set_defaults(func=cmd_sync)

    s_mc = sub.add_parser("mark-consolidated", help="Mark facts through YYYY-MM-DD as merged by Second Brain.")
    s_mc.add_argument("--through-date", required=True, help="YYYY-MM-DD date of latest SB 01 Now consolidation")
    s_mc.add_argument("--local-only", action="store_true")
    s_mc.set_defaults(func=cmd_mark_consolidated)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
