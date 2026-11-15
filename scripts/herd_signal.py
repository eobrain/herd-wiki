#!/usr/bin/env python3
"""Allowlisted Signal bridge for @runai and the Herd Wiki.

Security invariants:
  1. Dedicated Bot Identity: Hard-refuses configuring or registering if `bot_number`
     equals `allowed_user_number`, protecting the user's personal Signal account.
  2. Strict Single-Sender Allowlist: Only 1:1 direct `dataMessage` texts from
     `allowed_user_number` (and pinned `allowed_user_uuid`) are accepted. Group
     messages, stories, attachments, and any other sender are silently dropped
     before ever reaching the wiki or any LLM context.
  3. Hardcoded Outbound Recipient: `send` has no recipient CLI flag; it can only
     ever message `allowed_user_number` from `~/.config/herd/signal.json`.
  4. Zero Identifiers in Wiki: Phone numbers and UUIDs live strictly in `0600`
     files under `~/.config/herd/` and `~/.local/state/herd/`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SIGNAL_CLI = Path(
    os.environ.get("SIGNAL_CLI_BIN")
    or shutil.which("signal-cli")
    or str(Path.home() / ".local/bin/signal-cli")
)
HERD_WIKI_PY = Path(__file__).resolve().with_name("herd_wiki.py")
CONFIG_PATH = Path(
    os.environ.get("HERD_SIGNAL_CONFIG", str(Path.home() / ".config/herd/signal.json"))
)
STATE_DIR = Path(
    os.environ.get("HERD_SIGNAL_STATE_DIR", str(Path.home() / ".local/state/herd"))
)
SOCKET_PATH = STATE_DIR / "signal-rpc.sock"
INBOX_JSONL = STATE_DIR / "signal-inbox.jsonl"
DROPPED_LOG = STATE_DIR / "signal-dropped.log"
STATE_LOCK = STATE_DIR / "signal-state.lock"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"

E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")


def utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_e164(raw: str) -> str:
    digits = re.sub(r"[\s\-\(\)\.]", "", raw.strip())
    if not digits.startswith("+"):
        if len(digits) == 10:
            digits = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            digits = "+" + digits
    if not E164_RE.match(digits):
        raise SystemExit(f"Invalid phone number '{raw}': expected E.164 format like +12025550123.")
    return digits


def mask_number(num: str) -> str:
    if len(num) <= 6:
        return "***"
    return f"{num[:4]}***{num[-4:]}"


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


@contextmanager
def state_lock():
    ensure_state_dir()
    with open(STATE_LOCK, "a+", encoding="utf-8") as fh:
        os.chmod(STATE_LOCK, 0o600)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Signal config missing ({CONFIG_PATH}). Run `herd_signal.py configure` first."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("bot_number") == cfg.get("allowed_user_number"):
        raise SystemExit("SAFETY BLOCK: bot_number and allowed_user_number must never be equal.")
    return cfg


def save_config(cfg: dict) -> None:
    if cfg.get("bot_number") == cfg.get("allowed_user_number"):
        raise SystemExit("SAFETY BLOCK: Refusing to save identical bot_number and allowed_user_number.")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_PATH.parent, 0o700)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def rpc_call(method: str, params: dict, timeout: float = 20.0) -> dict:
    if not SOCKET_PATH.exists():
        raise RuntimeError(f"Signal JSON-RPC socket not found at {SOCKET_PATH}")
    req_id = f"rpc-{int(time.time() * 1000)}-{os.getpid()}"
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line.decode("utf-8", errors="replace"))
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(f"JSON-RPC {method} error: {msg['error']}")
                    return msg.get("result", {})
    raise RuntimeError(f"No JSON-RPC response received for {method}")


def load_inbox() -> list[dict]:
    if not INBOX_JSONL.exists():
        return []
    items: list[dict] = []
    for line in INBOX_JSONL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def save_inbox(items: list[dict]) -> None:
    ensure_state_dir()
    tmp = INBOX_JSONL.with_suffix(".tmp")
    body = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(INBOX_JSONL)


def cmd_configure(args: argparse.Namespace) -> int:
    bot = normalize_e164(args.bot_number)
    user = normalize_e164(args.allowed_user_number)
    if bot == user:
        raise SystemExit(
            "SAFETY BLOCK: --bot-number cannot be your personal Signal number "
            "(registering it would disconnect your phone's Signal app!)."
        )
    ensure_state_dir()
    existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    cfg = {
        "bot_number": bot,
        "allowed_user_number": user,
        "allowed_user_uuid": existing.get("allowed_user_uuid"),
        "bot_given_name": args.bot_given_name,
        "bot_family_name": args.bot_family_name,
        "verified": existing.get("verified", False) if existing.get("bot_number") == bot else False,
        "updated_utc": utc_iso(),
    }
    save_config(cfg)
    print(
        json.dumps(
            {
                "ok": True,
                "bot_number_masked": mask_number(bot),
                "allowed_user_masked": mask_number(user),
                "verified": cfg["verified"],
            },
            indent=2,
        )
    )
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    cfg = load_config()
    cmd = [str(SIGNAL_CLI), "--scrub-log", "-a", cfg["bot_number"], "register"]
    if args.voice:
        cmd.append("--voice")
    if args.captcha:
        cmd.extend(["--captcha", args.captcha.strip()])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode != 0:
        needs_captcha = "captcha" in out.lower()
        print(
            json.dumps(
                {
                    "ok": False,
                    "needs_captcha": needs_captcha,
                    "captcha_url": "https://signalcaptchas.org/registration/generate.html" if needs_captcha else None,
                    "detail": out,
                },
                indent=2,
            )
        )
        return 1
    print(json.dumps({"ok": True, "method": "voice" if args.voice else "sms", "detail": out or "Verification code sent."}, indent=2))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config()
    code = args.code.strip()
    cmd = [str(SIGNAL_CLI), "--scrub-log", "-a", cfg["bot_number"], "verify", code]
    if args.pin:
        cmd.extend(["--pin", args.pin.strip()])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    out = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "detail": out}, indent=2))
        return 1

    # Set clean bot profile name so the user recognizes it immediately in Signal
    subprocess.run(
        [
            str(SIGNAL_CLI),
            "--scrub-log",
            "-a",
            cfg["bot_number"],
            "updateProfile",
            "--given-name",
            cfg.get("bot_given_name", "Runai"),
            "--family-name",
            cfg.get("bot_family_name", "(Herd)"),
            "--about",
            "Herd Spokesperson (@runai)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    cfg["verified"] = True
    cfg["verified_utc"] = utc_iso()
    save_config(cfg)
    print(json.dumps({"ok": True, "verified": True, "bot_number_masked": mask_number(cfg["bot_number"])}, indent=2))
    return 0


def cmd_install_services(args: argparse.Namespace) -> int:
    cfg = load_config()
    if not cfg.get("verified"):
        raise SystemExit("Account is not verified yet. Run `herd_signal.py verify --code ...` first.")
    ensure_state_dir()
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)

    daemon_unit = f"""[Unit]
Description=Herd Signal CLI JSON-RPC Daemon (@runai)
After=network-online.target

[Service]
Type=simple
UMask=0077
ExecStartPre=/usr/bin/rm -f {SOCKET_PATH}
ExecStart={SIGNAL_CLI} --scrub-log -a {cfg["bot_number"]} daemon --socket {SOCKET_PATH} --receive-mode on-start --no-receive-stdout --ignore-attachments --ignore-stories --ignore-avatars --ignore-stickers
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""

    bridge_unit = f"""[Unit]
Description=Herd Signal Allowlist Bridge -> Herd Wiki (@runai)
After=herd-signal-daemon.service
Requires=herd-signal-daemon.service

[Service]
Type=simple
UMask=0077
ExecStart=/usr/bin/python3 {Path(__file__).resolve()} bridge-loop
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""

    (SYSTEMD_USER_DIR / "herd-signal-daemon.service").write_text(daemon_unit, encoding="utf-8")
    (SYSTEMD_USER_DIR / "herd-signal-bridge.service").write_text(bridge_unit, encoding="utf-8")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "herd-signal-daemon.service"], check=True)
    # Wait up to 10s for UNIX socket to appear
    for _ in range(40):
        if SOCKET_PATH.exists():
            break
        time.sleep(0.25)
    subprocess.run(["systemctl", "--user", "enable", "--now", "herd-signal-bridge.service"], check=True)
    print(
        json.dumps(
            {
                "ok": True,
                "socket": str(SOCKET_PATH),
                "socket_ready": SOCKET_PATH.exists(),
                "services": ["herd-signal-daemon.service", "herd-signal-bridge.service"],
            },
            indent=2,
        )
    )
    return 0


def is_allowlisted_envelope(env: dict, cfg: dict) -> tuple[bool, str]:
    if not isinstance(env, dict):
        return False, "not_dict"
    data_msg = env.get("dataMessage")
    if not isinstance(data_msg, dict):
        return False, "not_data_message"
    if data_msg.get("groupInfo") is not None:
        return False, "group_message_ignored"
    text = (data_msg.get("message") or "").strip()
    if not text:
        return False, "empty_text"

    src_num = env.get("sourceNumber") or env.get("source")
    src_uuid = env.get("sourceUuid")
    allowed_num = cfg["allowed_user_number"]
    allowed_uuid = cfg.get("allowed_user_uuid")

    if src_num and src_num == allowed_num:
        if src_uuid and not allowed_uuid:
            cfg["allowed_user_uuid"] = src_uuid
            save_config(cfg)
        elif allowed_uuid and src_uuid and src_uuid != allowed_uuid:
            return False, "uuid_mismatch"
        return True, "ok"

    if allowed_uuid and src_uuid and src_uuid == allowed_uuid and not src_num:
        return True, "ok_uuid"

    return False, "untrusted_sender"


def handle_verified_message(env: dict, cfg: dict) -> None:
    data_msg = env["dataMessage"]
    text = (data_msg.get("message") or "").strip()
    msg_ts = data_msg.get("timestamp") or env.get("timestamp") or int(time.time() * 1000)

    # Best-effort instant 👀 reaction so the user sees immediate acknowledgement on mobile
    target_author = env.get("sourceNumber") or env.get("sourceUuid") or cfg.get("allowed_user_uuid") or cfg["allowed_user_number"]
    try:
        rpc_call(
            "sendReaction",
            {
                "recipient": [cfg["allowed_user_number"]],
                "emoji": "👀",
                "targetAuthor": target_author,
                "targetTimestamp": msg_ts,
            },
            timeout=10.0,
        )
    except Exception:
        pass

    # Record in Herd Wiki human-inbox (no phone numbers written!)
    directive_id = ""
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(HERD_WIKI_PY),
                "post-directive",
                "--agent",
                "runai",
                "--channel",
                "signal",
                "--summary",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            directive_id = json.loads(proc.stdout).get("directive_id", "")
    except Exception:
        pass

    with state_lock():
        inbox = load_inbox()
        # Deduplicate by Signal message timestamp
        if any(it.get("signal_timestamp") == msg_ts for it in inbox):
            return
        seq = len(inbox) + 1
        inbox.append(
            {
                "id": f"S-{seq:04d}",
                "received_utc": utc_iso(),
                "signal_timestamp": msg_ts,
                "directive_id": directive_id,
                "text": text,
                "replied": False,
            }
        )
        save_inbox(inbox)


def cmd_bridge_loop(_args: argparse.Namespace) -> int:
    ensure_state_dir()
    while True:
        try:
            cfg = load_config()
            if not SOCKET_PATH.exists():
                time.sleep(1.0)
                continue
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(str(SOCKET_PATH))
                buf = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                        if msg.get("method") != "receive":
                            continue
                        params = msg.get("params") or {}
                        env = params.get("envelope") or {}
                        cfg = load_config()
                        ok, reason = is_allowlisted_envelope(env, cfg)
                        if ok:
                            handle_verified_message(env, cfg)
                        elif reason not in ("not_data_message", "empty_text"):
                            with open(DROPPED_LOG, "a", encoding="utf-8") as df:
                                os.chmod(DROPPED_LOG, 0o600)
                                df.write(f"{utc_iso()} dropped={reason}\n")
        except Exception:
            time.sleep(2.0)


def cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config()
    text = args.message.strip()
    if not text:
        raise SystemExit("Refusing to send empty Signal message.")

    if SOCKET_PATH.exists():
        res = rpc_call("send", {"recipient": [cfg["allowed_user_number"]], "message": text}, timeout=25.0)
        sent_ts = res.get("timestamp")
    else:
        proc = subprocess.run(
            [str(SIGNAL_CLI), "--scrub-log", "-a", cfg["bot_number"], "send", "-m", text, cfg["allowed_user_number"]],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise SystemExit(f"signal-cli send failed: {proc.stderr or proc.stdout}")
        sent_ts = int(time.time() * 1000)

    acked_ids: list[str] = []
    target_timestamps: list[int] = []
    if args.reply_to or args.mark_all_replied:
        with state_lock():
            inbox = load_inbox()
            for it in inbox:
                if not it.get("replied") and (args.mark_all_replied or it.get("id") == args.reply_to):
                    it["replied"] = True
                    it["replied_utc"] = utc_iso()
                    acked_ids.append(it["id"])
                    if it.get("signal_timestamp"):
                        target_timestamps.append(int(it["signal_timestamp"]))
            save_inbox(inbox)

    if SOCKET_PATH.exists():
        target_author = cfg.get("allowed_user_uuid") or cfg["allowed_user_number"]
        for ts in target_timestamps:
            try:
                rpc_call(
                    "sendReaction",
                    {
                        "recipient": [cfg["allowed_user_number"]],
                        "emoji": "✅",
                        "targetAuthor": target_author,
                        "targetTimestamp": ts,
                    },
                    timeout=10.0,
                )
            except Exception:
                pass

    print(
        json.dumps(
            {
                "ok": True,
                "recipient_masked": mask_number(cfg["allowed_user_number"]),
                "timestamp": sent_ts,
                "marked_replied": acked_ids,
            },
            indent=2,
        )
    )
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    with state_lock():
        inbox = load_inbox()
    pending = [it for it in inbox if not it.get("replied")]
    print(json.dumps({"ok": True, "pending_count": len(pending), "pending": pending}, indent=2))
    return 0


def cmd_wait_next(args: argparse.Namespace) -> int:
    ensure_state_dir()
    deadline = (time.time() + args.timeout) if args.timeout > 0 else None
    while True:
        with state_lock():
            pending = [it for it in load_inbox() if not it.get("replied")]
        if pending:
            print(json.dumps({"ok": True, "event": "signal_message", "pending": pending}, indent=2))
            return 0
        if deadline and time.time() >= deadline:
            print(json.dumps({"ok": True, "event": "timeout", "pending": []}, indent=2))
            return 0
        time.sleep(1.0)


def cmd_status(_args: argparse.Namespace) -> int:
    configured = CONFIG_PATH.exists()
    cfg = load_config() if configured else {}
    pending = [it for it in load_inbox() if not it.get("replied")] if configured else []
    svc = subprocess.run(
        ["systemctl", "--user", "is-active", "herd-signal-daemon.service", "herd-signal-bridge.service"],
        capture_output=True,
        text=True,
    )
    states = [s.strip() for s in svc.stdout.splitlines() if s.strip()]
    print(
        json.dumps(
            {
                "configured": configured,
                "verified": cfg.get("verified", False),
                "bot_number_masked": mask_number(cfg["bot_number"]) if cfg.get("bot_number") else None,
                "allowed_user_masked": mask_number(cfg["allowed_user_number"]) if cfg.get("allowed_user_number") else None,
                "uuid_pinned": bool(cfg.get("allowed_user_uuid")),
                "socket_exists": SOCKET_PATH.exists(),
                "service_states": states,
                "pending_messages": len(pending),
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Allowlisted Signal bridge for @runai and the Herd Wiki.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_cfg = sub.add_parser("configure", help="Save dedicated bot number and single allowlisted user number.")
    s_cfg.add_argument("--bot-number", required=True, help="Dedicated Signal bot number (NOT your personal Signal)")
    s_cfg.add_argument("--allowed-user-number", required=True, help="Single allowlisted personal Signal number")
    s_cfg.add_argument("--bot-given-name", default="Runai")
    s_cfg.add_argument("--bot-family-name", default="(Herd)")
    s_cfg.set_defaults(func=cmd_configure)

    s_reg = sub.add_parser("register", help="Request SMS or voice OTP from Signal for the bot number.")
    s_reg.add_argument("--voice", action="store_true", help="Request voice call OTP instead of SMS")
    s_reg.add_argument("--captcha", help="signalcaptcha://... token completed by user in browser")
    s_reg.set_defaults(func=cmd_register)

    s_ver = sub.add_parser("verify", help="Complete Signal registration with the 6-digit OTP.")
    s_ver.add_argument("--code", required=True, help="6-digit OTP (e.g. 123-456)")
    s_ver.add_argument("--pin", help="Optional Signal registration lock PIN")
    s_ver.set_defaults(func=cmd_verify)

    s_svc = sub.add_parser("install-services", help="Install and start systemd --user Signal daemon & bridge.")
    s_svc.set_defaults(func=cmd_install_services)

    s_loop = sub.add_parser("bridge-loop", help="Internal daemon loop bridging JSON-RPC socket to Herd Wiki.")
    s_loop.set_defaults(func=cmd_bridge_loop)

    s_send = sub.add_parser("send", help="Send a Signal message strictly to the single allowlisted user.")
    s_send.add_argument("--message", required=True, help="Message body")
    s_send.add_argument("--reply-to", help="Mark specific S-NNNN message replied and react ✅")
    s_send.add_argument("--mark-all-replied", action="store_true", help="Mark all pending Signal messages replied")
    s_send.set_defaults(func=cmd_send)

    s_chk = sub.add_parser("check", help="Check pending unreplied Signal messages.")
    s_chk.set_defaults(func=cmd_check)

    s_wait = sub.add_parser("wait-next", help="Block until an allowlisted Signal message arrives, then exit 0.")
    s_wait.add_argument("--timeout", type=int, default=0, help="Optional timeout in seconds (0 = wait indefinitely)")
    s_wait.set_defaults(func=cmd_wait_next)

    s_stat = sub.add_parser("status", help="Show Signal configuration and service status.")
    s_stat.set_defaults(func=cmd_status)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
