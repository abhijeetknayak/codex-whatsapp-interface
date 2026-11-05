#!/usr/bin/env python3
"""WhatsApp Codex Bot v2.3: named chats, write sandbox, cancellation, usage and menus.

Run: python bot.py
Production: gunicorn --workers 1 --threads 8 --bind 127.0.0.1:8000 'bot:app'
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

from codex_api import (
    CodexCancelled, CodexError, account_usage, list_server_threads, load_models,
    lookup_session_cwd, last_saved_exchange, run_codex,
)
from store import Store
from appserver_runner import run_codex_interactive

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)  # Make edited .env authoritative on restart
LOG = logging.getLogger("whatsapp_codex")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
CHAT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
PHONE_RE = re.compile(r"^[0-9]{7,20}$")

HELP = """WhatsApp Codex v2.3
/new NAME - create a chat
/chats or /list - list bot chats
/use NAME - switch and show last request + reply
/rename OLD NEW - rename bot chat
/delete NAME - delete bot chat reference (not Codex history)
/reset - start a new session in this chat
/server [PAGE] - browse Codex CLI/VS Code/exec sessions
/import NAME NUMBER - import from the most recent /server list
/model - numbered model picker; /model default
/thinking - numbered reasoning picker; /thinking default
/usage - account 5-hour and weekly rate limits
/status - active chat and configuration
/jobs - queued/running tasks
/cancel NUMBER - cancel a queued or running task
/approvals - list pending approval requests
/approve NUMBER - allow one requested action
/deny NUMBER - reject one requested action
/history [N] - show recent requests and answers
/project [PATH] - show/change project within allowed roots
/help - show commands

Plain text starts a task in the active chat. Switch chats while it runs;
completed replies are labelled with their originating chat.
For numbered menus reply with the number shown. Codex runs with
workspace-write sandbox by default; Codex approvals are relayed to WhatsApp."""


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(os.getenv(name, default))))
    except ValueError:
        return default


class Config:
    def __init__(self):
        self.app_secret = os.getenv("META_APP_SECRET", "")
        self.verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        self.access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        self.phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self.allowed_numbers = {s.strip().lstrip("+") for s in
                                os.getenv("ALLOWED_WHATSAPP_NUMBERS", "").split(",") if s.strip()}
        self.codex_bin = os.getenv("CODEX_BIN", "codex")
        self.codex_sandbox = os.getenv("CODEX_SANDBOX_MODE", "workspace-write").strip().lower()
        self.graph_version = os.getenv("META_GRAPH_VERSION", "v26.0")
        self.project_dir = Path(os.getenv("CODEX_PROJECT_DIR", str(Path.home()))).expanduser().resolve()
        self.codex_home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
        self.db_path = Path(os.getenv("BOT_DB_PATH", str(BASE / "bot.sqlite3"))).expanduser()
        self.allowed_roots = [Path(s.strip()).expanduser().resolve() for s in
                              os.getenv("CODEX_ALLOWED_ROOTS", str(Path.home())).split(",") if s.strip()]
        self.max_workers = _env_int("MAX_CODEX_WORKERS", 2, 1, 4)
        self.task_timeout = _env_int("CODEX_TIMEOUT_SECONDS", 900, 30, 7200)
        self.port = _env_int("PORT", 8000, 1, 65535)
        self.model_fallback = os.getenv("CODEX_MODEL_OPTIONS", "")
        self.approval_timeout = _env_int("APPROVAL_TIMEOUT_SECONDS", 600, 60, 3600)

    def validate(self):
        fields = {"META_APP_SECRET": self.app_secret,
                  "WHATSAPP_VERIFY_TOKEN": self.verify_token,
                  "WHATSAPP_ACCESS_TOKEN": self.access_token,
                  "WHATSAPP_PHONE_NUMBER_ID": self.phone_id,
                  "ALLOWED_WHATSAPP_NUMBERS": self.allowed_numbers}
        missing = [name for name, value in fields.items() if not value]
        if missing:
            raise RuntimeError("Missing settings: " + ", ".join(missing))
        if self.codex_sandbox not in ("workspace-write", "read-only", "inherit"):
            raise RuntimeError("CODEX_SANDBOX_MODE must be workspace-write, read-only, or inherit")
        if not self.project_dir.is_dir():
            raise RuntimeError(f"CODEX_PROJECT_DIR does not exist: {self.project_dir}")
        if not self.is_safe_project(self.project_dir):
            raise RuntimeError("CODEX_PROJECT_DIR must be inside CODEX_ALLOWED_ROOTS")
        if not all(PHONE_RE.fullmatch(x) for x in self.allowed_numbers):
            raise RuntimeError("ALLOWED_WHATSAPP_NUMBERS must contain international digits only")

    def is_safe_project(self, path: Path) -> bool:
        path = Path(path).expanduser().resolve()
        return path.is_dir() and any(path == root or root in path.parents for root in self.allowed_roots)


def last_exchange_text(store: Store, sender: str, name: str, count: int = 1) -> str:
    history = list(reversed(store.history(sender, name, count)))
    if not history:
        return "No WhatsApp tasks recorded in this chat yet. Imported Codex history is still stored in Codex."
    blocks = []
    for job in history:
        prompt = job["prompt"]
        response = job.get("response") or (
            "(Cancelled; file changes may remain)" if job["status"] == "cancelled" else
            f"({job['status']}: {job.get('error') or 'No reply'})"
            if job["status"] in ("failed", "interrupted") else
            "(No completed response yet)")
        blocks.append(f"Task #{job['id']} [{job['status']}]\nYou: {prompt}\nCodex: {response}")
    return "\n\n".join(blocks)


class Bot:
    def __init__(self, settings: Config, store: Store | None = None):
        self.cfg = settings
        self.store = store or Store(settings.db_path, str(settings.project_dir))
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self):
        if self.threads:
            return
        for target, label in [(self.inbox_worker, "inbox"), (self.outbox_worker, "outbox")]:
            t = threading.Thread(target=target, name=label, daemon=True)
            self.threads.append(t)
            t.start()
        for i in range(self.cfg.max_workers):
            t = threading.Thread(target=self.task_worker, name=f"codex-{i}", daemon=True)
            self.threads.append(t)
            t.start()
        LOG.info("Bot background workers started (%d Codex workers)", self.cfg.max_workers)

    def signature_ok(self, raw: bytes, header: str) -> bool:
        if not self.cfg.app_secret or not header.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(self.cfg.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header, expected)

    def on_webhook(self, payload: dict) -> int:
        if payload.get("object") != "whatsapp_business_account":
            return 0
        accepted = 0
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                # Protect against a callback subscribed for multiple phone IDs.
                if metadata.get("phone_number_id") and str(metadata["phone_number_id"]) != self.cfg.phone_id:
                    continue
                for msg in value.get("messages", []):
                    sender = str(msg.get("from") or "")
                    if sender not in self.cfg.allowed_numbers or msg.get("type") != "text":
                        continue
                    msg_id = msg.get("id")
                    text = str((msg.get("text") or {}).get("body") or "").strip()
                    if not isinstance(msg_id, str) or not msg_id or not text:
                        continue
                    if self.store.ingest(msg_id, sender, text):
                        accepted += 1
        return accepted

    def inbox_worker(self):
        while not self.stop_event.is_set():
            item = self.store.take_inbox()
            if not item:
                self.stop_event.wait(0.4)
                continue
            try:
                reply = self.handle(item["sender"], item["body"], item["id"])
                if reply:
                    self.store.reply(item["sender"], reply)
                self.store.finish_inbox(item["id"])
            except Exception:
                LOG.exception("Failed processing incoming message %s", item["id"])
                self.store.reply(item["sender"], "Bot error. Check server logs; your request may not have started.")
                self.store.finish_inbox(item["id"], "error")

    def outbox_worker(self):
        while not self.stop_event.is_set():
            item = self.store.take_outbox()
            if not item:
                self.stop_event.wait(0.4)
                continue
            try:
                self.send_text(item["recipient"], item["body"])
                self.store.mark_outbox(item["id"], "sent")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                info = (exc.response.text if exc.response is not None else str(exc))[:700]
                LOG.error("WhatsApp reply rejected HTTP %s: %s", status, info)
                if (status == 429 or 500 <= status < 600) and item["attempts"] < 4:
                    delay = min(300, 2 ** min(item["attempts"], 8))
                    self.store.mark_outbox(item["id"], "pending", info, delay=delay)
                else:
                    self.store.mark_outbox(item["id"], "failed", info)
            except requests.RequestException as exc:
                # Network outcomes can be ambiguous: do not auto-resend as it may duplicate.
                LOG.error("WhatsApp reply delivery uncertain: %s", exc)
                self.store.mark_outbox(item["id"], "uncertain", str(exc))
            except Exception:
                LOG.exception("Unexpected outbox error")
                self.store.mark_outbox(item["id"], "failed", "Unexpected bot error")

    def send_text(self, recipient: str, body: str):
        """Send exactly one pre-chunked outbox message via WhatsApp Cloud API."""
        endpoint = f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.phone_id}/messages"
        response = requests.post(endpoint,
            headers={"Authorization": f"Bearer {self.cfg.access_token}"},
            json={"messaging_product": "whatsapp", "to": recipient,
                  "type": "text", "text": {"body": body}},
            timeout=20)
        response.raise_for_status()

    def task_worker(self):
        while not self.stop_event.is_set():
            turn = self.store.claim_turn()
            if not turn:
                self.stop_event.wait(0.6)
                continue
            sender, name = turn["sender"], turn["name"]
            try:
                previous = self.store.thread_for_turn(turn)
                output = run_codex_interactive(
                    self.cfg.codex_bin, Path(turn["cwd"]), turn["prompt"],
                    previous, turn["model"], turn["effort"], self.cfg.task_timeout,
                    should_cancel=lambda: self.store.is_cancel_requested(turn["id"]),
                    store=self.store, turn=turn, sandbox=self.cfg.codex_sandbox,
                    approval_timeout=self.cfg.approval_timeout)
                final_status = self.store.finish_turn(
                    turn, output["response"], "done", output["thread_id"],
                    output["input_tokens"], output["output_tokens"])
                if final_status == "cancelled":
                    self.store.reply(sender, f"[{name}] Task #{turn['id']} cancelled. Changes already made may remain.")
                elif final_status == "done":
                    self.store.reply(sender, f"[{name}] Task #{turn['id']} completed\n\n{output['response']}")
            except CodexCancelled as exc:
                LOG.info("Codex task %s cancelled: %s", turn["id"], exc)
                self.store.finish_turn(turn, "", "cancelled", error=str(exc)[:700])
                self.store.reply(sender, f"[{name}] Task #{turn['id']} cancelled. Changes already made may remain.")
            except Exception as exc:
                LOG.exception("Codex task %s failed", turn["id"])
                final_status = self.store.finish_turn(turn, "", "failed", error=str(exc)[:1000])
                if final_status == "cancelled":
                    self.store.reply(sender, f"[{name}] Task #{turn['id']} cancelled. Changes already made may remain.")
                elif final_status == "failed":
                    self.store.reply(sender, f"[{name}] Task #{turn['id']} failed: {str(exc)[:500]}")

    def menu_choice(self, sender: str, name: str, raw: str):
        menu = self.store.menu(sender, name)
        if not menu:
            return None
        try:
            index = int(raw)
        except ValueError:
            return None
        values = menu["options"]
        if index == 0:
            self.store.set_chat_option(sender, name, "model" if menu["kind"] == "model" else "effort", None)
            if menu["kind"] == "model":
                self.store.set_chat_option(sender, name, "effort", None)
            self.store.clear_menu(sender)
            return f"[{name}] {menu['kind']} restored to Codex default."
        if not 1 <= index <= len(values):
            return f"Select 0 through {len(values)}, or send /{menu['kind']} again."
        selected = values[index-1]
        self.store.set_chat_option(sender, name, "model" if menu["kind"] == "model" else "effort", selected["id"])
        # Reset reasoning effort if switching models that don't support current effort.
        if menu["kind"] == "model":
            effort = self.store.current(sender)["effort"]
            if effort and selected.get("efforts") and effort not in selected["efforts"]:
                self.store.set_chat_option(sender, name, "effort", None)
        self.store.clear_menu(sender)
        return f"[{name}] {menu['kind']} set to {selected['name']} ({selected['id']})."

    def model_menu(self, sender: str, chat: dict) -> str:
        models = load_models(self.cfg.codex_bin, self.cfg.codex_home, self.cfg.model_fallback)
        if not models:
            return "No models returned. Verify Codex login or set CODEX_MODEL_OPTIONS in .env."
        options = [{"id": m["id"], "name": m["name"], "efforts": m["efforts"]} for m in models[:30]]
        self.store.put_menu(sender, chat["name"], "model", options)
        lines = [f"[{chat['name']}] Models (current: {chat['model'] or 'Codex default'}):", "0. Codex default"]
        lines.extend(f"{i}. {m['name']} ({m['id']})" for i, m in enumerate(options, 1))
        lines.append("Reply with a number; or /model NUMBER. Menu valid for 30 min.")
        return "\n".join(lines)

    def thinking_menu(self, sender: str, chat: dict) -> str:
        models = load_models(self.cfg.codex_bin, self.cfg.codex_home, self.cfg.model_fallback)
        selected = next((m for m in models if m["id"] == chat["model"]), None)
        if selected is None:
            selected = next((m for m in models if m["default"]), None) or (models[0] if models else None)
        # Do NOT silently offer unsupported levels: catalog may omit support info.
        efforts = selected["efforts"] if selected else []
        if not efforts:
            return ("Codex did not report supported thinking levels for the selected/default model. "
                    "Choose a model using /model first, or update Codex to refresh its model catalog.")
        options = [{"id": e, "name": e.upper()} for e in efforts]
        self.store.put_menu(sender, chat["name"], "thinking", options)
        lines = [f"[{chat['name']}] Thinking (current: {chat['effort'] or 'Codex default'}):",
                 "0. Codex default"]
        lines.extend(f"{i}. {x['name']}" for i, x in enumerate(options, 1))
        lines.append("Reply with a number; or /thinking NUMBER.")
        return "\n".join(lines)

    def server_listing(self, sender: str, page: int) -> str:
        sessions = list_server_threads(self.cfg.codex_bin, limit=160, codex_home=self.cfg.codex_home)
        self.store.set_server_choices(sender, sessions)
        offset, per_page = (page-1)*15, 15
        subset = sessions[offset:offset+per_page]
        if not subset:
            return "No sessions on that page. Try /server 1."
        lines = [f"Saved Codex conversations (page {page}; {len(sessions)} loaded):"]
        for i, row in enumerate(subset, start=offset+1):
            lines.append(f"{i}. {row['title']}")
        lines.append("Use /import NAME NUMBER. Internal review subagents are excluded.")
        if len(sessions) > offset+per_page:
            lines.append(f"Next: /server {page+1}")
        return "\n".join(lines)

    def handle(self, sender: str, message: str, message_id: str = "manual") -> str:
        msg = message.strip()
        if not msg:
            return "Send /help to see commands."
        chat = self.store.current(sender)
        name = chat["name"]
        # Number selections only occur while a matching per-chat menu is open.
        if msg.isdecimal() and self.store.menu(sender, name):
            return self.menu_choice(sender, name, msg)
        if not msg.startswith("/"):
            # Do not erase an open menu if user sends a normal prompt;
            # next number is still treated as a menu response until menu expires.
            task_id, actual_name = self.store.enqueue_turn(message_id, sender, msg)
            return f"[{actual_name}] Task #{task_id} queued. Use /use NAME to switch chats; results arrive with the chat name."

        parts = msg.split()
        command, args = parts[0].lower(), parts[1:]
        if command in ("/help",):
            return HELP
        if command in ("/list", "/chats"):
            return "Chats (* active):\n" + "\n".join(
                f"{'*' if c['active'] else ' '} {c['name']} ({'started' if c['thread_id'] else 'empty'})"
                for c in self.store.chats(sender))
        if command == "/new":
            if len(args) != 1 or not CHAT_NAME.fullmatch(args[0]):
                return "Usage: /new NAME (letters/numbers/_/-; starts with a letter)"
            result = self.store.create_chat(sender, args[0])
            self.store.clear_menu(sender)
            return f"Created and selected {args[0]}." if result else "Name already exists. Use /use NAME."
        if command == "/use":
            if len(args) != 1 or not CHAT_NAME.fullmatch(args[0]):
                return "Usage: /use NAME"
            if not self.store.use_chat(sender, args[0]):
                return "Chat not found. Use /chats."
            self.store.clear_menu(sender)
            summary = last_exchange_text(self.store, sender, args[0])
            if summary.startswith("No WhatsApp tasks recorded"):
                saved = self.store.chat(sender, args[0])
                preview = last_saved_exchange(self.cfg.codex_bin, saved["thread_id"]) if saved and saved["thread_id"] else None
                if preview:
                    summary = preview
            return f"Switched to [{args[0]}].\n\n" + summary
        if command == "/rename":
            if len(args) != 2 or not all(CHAT_NAME.fullmatch(x) for x in args):
                return "Usage: /rename OLD NEW"
            return self.store.rename_chat(sender, args[0], args[1])
        if command == "/delete":
            if len(args) != 1 or not CHAT_NAME.fullmatch(args[0]):
                return "Usage: /delete NAME"
            return self.store.delete_chat(sender, args[0])
        if command == "/reset":
            return self.store.reset_chat(sender) if not args else "Usage: /reset"
        if command == "/status":
            return (f"Active: {name}\nProject: {chat['cwd']}\n"
                    f"Session: {'started' if chat['thread_id'] else 'new'}\n"
                    f"Model: {chat['model'] or 'Codex default'}\n"
                    f"Thinking: {chat['effort'] or 'Codex default'}\n"
                    f"Running/queued: {len(self.store.pending_jobs(sender))}\n"
                    f"Sandbox: {self.cfg.codex_sandbox} (Codex approval rules still apply)")
        if command == "/model":
            if not args:
                return self.model_menu(sender, chat)
            if len(args) == 1 and args[0].lower() == "default":
                self.store.set_chat_option(sender, name, "model", None)
                self.store.set_chat_option(sender, name, "effort", None)
                self.store.clear_menu(sender)
                return f"[{name}] Model and thinking restored to Codex defaults."
            if len(args) == 1 and args[0].isdecimal():
                menu = self.store.menu(sender, name)
                if not menu or menu["kind"] != "model":
                    return "Use /model first to open the options."
                return self.menu_choice(sender, name, args[0])
            return "Usage: /model, /model NUMBER, /model default"
        if command == "/thinking":
            if not args:
                return self.thinking_menu(sender, chat)
            if len(args) == 1 and args[0].lower() == "default":
                self.store.set_chat_option(sender, name, "effort", None)
                self.store.clear_menu(sender)
                return f"[{name}] Thinking restored to Codex default."
            if len(args) == 1 and args[0].isdecimal():
                menu = self.store.menu(sender, name)
                if not menu or menu["kind"] != "thinking":
                    return "Use /thinking first to open the options."
                return self.menu_choice(sender, name, args[0])
            return "Usage: /thinking, /thinking NUMBER, /thinking default"
        if command == "/usage":
            stats = self.store.token_stats(sender)
            return (account_usage(self.cfg.codex_bin)
                    + f"\n\nTracked by this bot: {stats['input']:,} input / {stats['output']:,} output tokens. "
                    "Bot tokens are not the same as account rate-limit percentages.")
        if command == "/jobs":
            jobs = self.store.pending_jobs(sender)
            return ("Active jobs:\n" + "\n".join(
                f"#{j['id']} [{j['name']}] "
                f"{'cancelling' if j['cancel_requested'] else j['status']}: {j['prompt'][:90]}"
                for j in jobs) + "\nCancel using /cancel NUMBER") if jobs else "No queued/running jobs."
        if command == "/cancel":
            if len(args) != 1 or not re.fullmatch(r"[0-9]{1,10}", args[0]) or int(args[0]) < 1:
                return "Usage: /cancel TASK_NUMBER (find the number using /jobs)"
            task_id = int(args[0])
            result = self.store.cancel_turn(sender, task_id)
            state = result["state"]
            if state == "missing":
                return f"Task #{task_id} not found for your WhatsApp number. Use /jobs."
            if state == "queued_cancelled":
                return f"[{result['name']}] Task #{task_id} cancelled before starting."
            if state == "running_requested":
                return (f"[{result['name']}] Cancellation requested for task #{task_id}. "
                        "Stopping Codex; any file changes already made may remain. "
                        "You'll receive a confirmation when it stops.")
            if state == "already_requested":
                return f"[{result['name']}] Task #{task_id} is already cancelling."
            return f"Task #{task_id} already finished ({result['status']})."
        if command == "/approvals":
            pending = self.store.pending_approvals(sender)
            if not pending:
                return "No pending Codex approvals."
            return ("Pending approvals:\n" + "\n".join(
                f"#{a['id']} [{a['name']}] task #{a['turn_id']}: {a['details'][:180]}"
                for a in pending) + "\nUse /approve NUMBER or /deny NUMBER")
        if command in ("/approve", "/deny"):
            if len(args) != 1 or not re.fullmatch(r"[0-9]{1,10}", args[0]) or int(args[0]) < 1:
                return f"Usage: {command} APPROVAL_NUMBER (find it in /approvals)"
            aid = int(args[0])
            outcome = self.store.decide_approval(sender, aid, command == "/approve")
            state = outcome["state"]
            if state == "missing":
                return f"Approval #{aid} not found for your number."
            if state in ("approved", "denied"):
                if (state == "approved") != (command == "/approve"):
                    return f"Approval #{aid} is already {state}; it can no longer be changed."
                action = "Approved" if state == "approved" else "Denied"
                return (f"{action} approval #{aid} for task #{outcome['turn_id']} "
                        f"[{outcome['name']}]. Codex will continue.")
            return f"Approval #{aid} is {state}; it can no longer be changed."
        if command == "/history":
            if len(args) > 1 or (args and not args[0].isdecimal()):
                return "Usage: /history [1-5]"
            count = max(1, min(5, int(args[0]) if args else 3))
            return f"[{name}] History:\n" + last_exchange_text(self.store, sender, name, count)
        if command == "/server":
            if len(args) > 1 or (args and not args[0].isdecimal()):
                return "Usage: /server [PAGE]"
            page = int(args[0]) if args else 1
            if page < 1 or page > 12:
                return "Page must be 1-12."
            try:
                return self.server_listing(sender, page)
            except (CodexError, OSError) as exc:
                return f"Could not list Codex sessions: {exc}"
        if command == "/import":
            if len(args) != 2 or not CHAT_NAME.fullmatch(args[0]) or not args[1].isdecimal():
                return "Usage: /import NAME NUMBER (from /server)"
            choice = self.store.server_choice(sender, int(args[1]))
            if choice is None:
                return "No matching saved choice. Send /server again."
            project = choice.get("cwd") or lookup_session_cwd(self.cfg.codex_home, choice["thread_id"])
            if not project:
                return "Codex did not provide that session's project directory. Import cancelled for safety."
            project = Path(project).expanduser().resolve()
            if not self.cfg.is_safe_project(project):
                return f"Project {project} is outside CODEX_ALLOWED_ROOTS or no longer exists."
            if not self.store.create_chat(sender, args[0], choice["thread_id"], str(project)):
                return "Chat name already exists. Choose a different name."
            self.store.clear_menu(sender)
            return f"Imported [{args[0]}]: {choice['title']}\nProject: {project}\nUse /use {args[0]} to switch."
        if command == "/project":
            if not args:
                return f"[{name}] Project: {chat['cwd']}"
            path = Path(" ".join(args)).expanduser().resolve()
            if not self.cfg.is_safe_project(path):
                return "Project must be an existing directory inside CODEX_ALLOWED_ROOTS."
            if self.store.has_pending(sender, name):
                return "Wait for active tasks before changing project."
            self.store.set_chat_option(sender, name, "cwd", str(path))
            return f"[{name}] Project set to {path}. Existing Codex thread may still refer to its original repo. Use /reset for a fresh thread."
        return "Unknown command. Send /help."


def create_app(bot: Bot) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

    @app.get("/webhook")
    def verify_webhook():
        if (request.args.get("hub.mode") == "subscribe" and bot.cfg.verify_token and
            hmac.compare_digest(request.args.get("hub.verify_token", ""), bot.cfg.verify_token)):
            return request.args.get("hub.challenge", ""), 200
        abort(403)

    @app.post("/webhook")
    def receive_webhook():
        raw = request.get_data(cache=True)
        if not bot.signature_ok(raw, request.headers.get("X-Hub-Signature-256", "")):
            abort(403)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return "bad payload", 400
        accepted = bot.on_webhook(payload)
        LOG.info("Received signed WhatsApp webhook; accepted %d new text messages", accepted)
        return "ok", 200

    @app.get("/health")
    def health():
        # No internal state is exposed; keep this endpoint private behind tunnel path rules.
        return {"ok": True}, 200

    return app


settings = Config()
settings.validate()
bot = Bot(settings)
app = create_app(bot)
if os.getenv("BOT_DISABLE_BACKGROUND") != "1":
    bot.start()  # For both python bot.py and gunicorn --workers 1.

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=settings.port, threaded=True, use_reloader=False)
