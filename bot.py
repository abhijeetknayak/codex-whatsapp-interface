#!/usr/bin/env python3
"""WhatsApp Cloud API bridge to named local Codex CLI sessions."""
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()
APP_SECRET = os.getenv('META_APP_SECRET', '')
VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', '')
ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN', '')
PHONE_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID', '')
ALLOWED = {n.strip().lstrip('+') for n in os.getenv('ALLOWED_WHATSAPP_NUMBERS', '').split(',') if n.strip()}
PROJECT_DIR = Path(os.getenv('CODEX_PROJECT_DIR', str(Path.home()))).expanduser().resolve()
DB = Path(os.getenv('BOT_DB_PATH', './bot.sqlite3')).expanduser().resolve()
GRAPH_VERSION = os.getenv('META_GRAPH_VERSION', 'v23.0')
CODEX_BIN = os.getenv('CODEX_BIN', 'codex')
TASK_TIMEOUT = int(os.getenv('CODEX_TIMEOUT_SECONDS', '600'))
NAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,31}$')

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('whatsapp-codex')
jobs = queue.Queue(maxsize=100)


def db_connect():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY)')
    conn.execute('''CREATE TABLE IF NOT EXISTS chats (
        sender TEXT NOT NULL, name TEXT NOT NULL,
        thread_id TEXT, PRIMARY KEY(sender, name))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS active_chats (
        sender TEXT PRIMARY KEY, name TEXT NOT NULL)''')
    conn.commit()
    return conn


def send_text(recipient, text):
    endpoint = f'https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_ID}/messages'
    chunks = [text[i:i+3000] for i in range(0, len(text), 3000)] or ['(No response)']
    for chunk in chunks[:8]:
        r = requests.post(endpoint, headers={'Authorization': f'Bearer {ACCESS_TOKEN}'},
                          json={'messaging_product': 'whatsapp', 'to': recipient,
                                'type': 'text', 'text': {'body': chunk}}, timeout=20)
        if not r.ok:
            log.error(f'Failed to send message to {recipient}: {r.text}')
        r.raise_for_status()


def ensure_default(conn, sender):
    conn.execute('INSERT OR IGNORE INTO chats(sender,name,thread_id) VALUES(?, ?, NULL)', (sender, 'main'))
    conn.execute('INSERT OR IGNORE INTO active_chats(sender,name) VALUES(?, ?)', (sender, 'main'))


def get_active(sender):
    with db_connect() as conn:
        ensure_default(conn, sender)
        name = conn.execute('SELECT name FROM active_chats WHERE sender=?', (sender,)).fetchone()[0]
        row = conn.execute('SELECT thread_id FROM chats WHERE sender=? AND name=?', (sender, name)).fetchone()
        if row is None:
            conn.execute('UPDATE active_chats SET name=? WHERE sender=?', ('main', sender))
            name, row = 'main', conn.execute('SELECT thread_id FROM chats WHERE sender=? AND name=?', (sender, 'main')).fetchone()
        return name, row[0]


def create_chat(sender, name):
    with db_connect() as conn:
        ensure_default(conn, sender)
        cur = conn.execute('INSERT OR IGNORE INTO chats(sender,name,thread_id) VALUES(?,?,NULL)', (sender, name))
        if cur.rowcount == 0:
            return False
        conn.execute('UPDATE active_chats SET name=? WHERE sender=?', (name, sender))
        return True


def use_chat(sender, name):
    with db_connect() as conn:
        ensure_default(conn, sender)
        if conn.execute('SELECT 1 FROM chats WHERE sender=? AND name=?', (sender, name)).fetchone() is None:
            return False
        conn.execute('UPDATE active_chats SET name=? WHERE sender=?', (name, sender))
        return True


def list_chats(sender):
    with db_connect() as conn:
        ensure_default(conn, sender)
        active = conn.execute('SELECT name FROM active_chats WHERE sender=?', (sender,)).fetchone()[0]
        rows = conn.execute('SELECT name,thread_id FROM chats WHERE sender=? ORDER BY name COLLATE NOCASE', (sender,)).fetchall()
    return 'Codex chats:\n' + '\n'.join(f'{"*" if name == active else " "} {name} ({"started" if thread else "empty"})' for name, thread in rows) + '\n* = active'


def rename_chat(sender, old, new):
    with db_connect() as conn:
        ensure_default(conn, sender)
        if conn.execute('SELECT 1 FROM chats WHERE sender=? AND name=?', (sender, new)).fetchone():
            return 'That chat name already exists.'
        row = conn.execute('SELECT thread_id FROM chats WHERE sender=? AND name=?', (sender, old)).fetchone()
        if row is None:
            return 'Chat not found.'
        conn.execute('INSERT INTO chats(sender,name,thread_id) VALUES(?,?,?)', (sender, new, row[0]))
        conn.execute('UPDATE active_chats SET name=? WHERE sender=? AND name=?', (new, sender, old))
        conn.execute('DELETE FROM chats WHERE sender=? AND name=?', (sender, old))
    return f'Renamed {old} to {new}.'


def delete_chat(sender, name):
    with db_connect() as conn:
        ensure_default(conn, sender)
        if name == 'main':
            return 'You cannot delete main. Use /reset to reset its history.'
        cur = conn.execute('DELETE FROM chats WHERE sender=? AND name=?', (sender, name))
        if not cur.rowcount:
            return 'Chat not found.'
        conn.execute('UPDATE active_chats SET name=? WHERE sender=? AND name=?', ('main', sender, name))
    return f'Deleted chat {name} from this bot. Its Codex history may still exist on disk.'


def reset_chat(sender):
    name, _ = get_active(sender)
    with db_connect() as conn:
        conn.execute('UPDATE chats SET thread_id=NULL WHERE sender=? AND name=?', (sender, name))
    return f'Reset {name}. Your next message starts a fresh Codex thread.'


def save_thread(sender, name, thread_id):
    with db_connect() as conn:
        conn.execute('UPDATE chats SET thread_id=? WHERE sender=? AND name=?', (thread_id, sender, name))


def run_codex(sender, prompt):
    name, thread_id = get_active(sender)
    # Deliberately no --sandbox or --ask-for-approval override.
    # Codex inherits user/project/managed configuration for this Ubuntu account.
    # Non-interactive approvals still cannot be accepted through this bot.
    cmd = [CODEX_BIN, 'exec', '--json', '--skip-git-repo-check']
    if thread_id:
        cmd += ['resume', thread_id, '-']
    else:
        cmd += ['-']
    proc = subprocess.run(cmd, cwd=PROJECT_DIR, input=prompt, text=True, capture_output=True,
                          timeout=TASK_TIMEOUT, check=False)
    found_thread, replies, errors = None, [], []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') == 'thread.started':
            found_thread = event.get('thread_id')
        item = event.get('item') or {}
        if event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            replies.append(item.get('text', ''))
        if event.get('type') == 'turn.failed':
            errors.append(str(event.get('error', 'Unknown error')))
    if found_thread:
        save_thread(sender, name, found_thread)
    if proc.returncode != 0 or errors:
        log.error('Codex failed (exit %s): %s', proc.returncode, proc.stderr[-1000:])
        return f'Codex failed in chat {name}. Check the Ubuntu bot logs.'
    return f'[{name}]\n' + (replies[-1] if replies else 'Codex completed without a text response.')



def get_server_sessions():
    codex_home = Path(
        os.getenv("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()

    titles = {}

    # 1. Read titles from Codex's local database.
    db_path = codex_home / "state_5.sqlite"

    if db_path.is_file():
        try:
            conn = sqlite3.connect(
                db_path.as_uri() + "?mode=ro",
                uri=True
            )
            try:
                for sid, title in conn.execute(
                    "SELECT id, title FROM threads"
                ):
                    if sid and title and title.strip():
                        titles[str(sid)] = title.strip()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning("Could not read Codex titles: %s", e)

    # 2. Prefer explicitly assigned conversation names.
    index_path = codex_home / "session_index.jsonl"

    if index_path.is_file():
        try:
            with index_path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue

                    sid = entry.get("id")
                    name = entry.get("thread_name")

                    if sid and isinstance(name, str) and name.strip():
                        titles[str(sid)] = name.strip()
        except OSError as e:
            log.warning("Could not read session index: %s", e)

    # 3. Match titles to saved session IDs.
    sessions = []

    for path in (codex_home / "sessions").rglob("rollout-*.jsonl"):
        try:
            with path.open(encoding="utf-8") as f:
                event = json.loads(f.readline())

            if event.get("type") != "session_meta":
                continue

            meta = event.get("payload", {})
            sid = meta.get("id")

            if not sid:
                continue

            cwd = meta.get("cwd") or "unknown"
            title = titles.get(str(sid))

            if not title:
                title = (
                    f"Unnamed - {Path(cwd).name} "
                    f"({str(sid)[:8]})"
                )

            sessions.append({
                "id": sid,
                "title": title,
                "cwd": cwd,
                "timestamp": meta.get("timestamp", ""),
            })

        except (OSError, ValueError, TypeError):
            continue

    return sorted(
        sessions,
        key=lambda s: s["timestamp"],
        reverse=True
    )

HELP = ("Named Codex chats (one active at a time):\n"
        "/new NAME - create and switch\n"
        "/list - show all chats\n"
        "/use NAME - switch chats\n"
        "/server - show available sessions\n"
        "/import NAME SESSION_ID - import a session\n"
        "/rename OLD NEW - rename chat\n"
        "/delete NAME - delete bot chat\n"
        "/reset - fresh history for active chat\n"
        "/status - active chat and project\n"
        "/help - this help\n"
        "Other text is sent to the active Codex chat. Permissions: inherited from Codex config.")


def handle_message(sender, message):
    s = message.strip()
    if not s:
        return 'Send a message or /help.'
    if not s.startswith('/'):
        return run_codex(sender, s)
    parts = s.split()
    command = parts[0].lower()

    if command == "/server":
        sessions = get_server_sessions()

        if not sessions:
            return "No saved Codex sessions found."

        lines = ["Codex conversations:"]

        for i, session in enumerate(sessions[:20], 1):
            lines.append(f"{i}. {session['title']}")

        lines.append("\nUse /import NAME NUMBER to import a chat.")

        return "\n".join(lines)
    
    if command == "/import":
        if len(parts) != 3:
            return "Usage: /import NAME NUMBER"

        name = parts[1]

        if not NAME_RE.fullmatch(name):
            return "Invalid chat name."

        try:
            index = int(parts[2]) - 1
        except ValueError:
            return "Please provide a chat number."

        sessions = get_server_sessions()[:20]

        if index < 0 or index >= len(sessions):
            return "Invalid chat number. Send /server."

        session = sessions[index]

        if not create_chat(sender, name):
            return "Chat name already exists."

        save_thread(sender, name, session["id"])

        return f"Imported: {session['title']}\nActive chat: {name}"
        
    if command == '/help':
        return HELP
    
    if command == '/list':
        return list_chats(sender)
    if command == '/status':
        name, thread = get_active(sender)
        return f'Online. Active: {name} ({"started" if thread else "empty"}). Project: {PROJECT_DIR}. Permissions: Codex configuration (no bot override).'
    if command == '/reset':
        return reset_chat(sender)
    if command in ('/new', '/use', '/delete'):
        if len(parts) != 2 or not NAME_RE.fullmatch(parts[1]):
            return f'Usage: {command} NAME (letters, numbers, _ or -; starts with a letter; up to 32 chars)'
        name = parts[1]
        if command == '/new':
            return f'Created and selected {name}.' if create_chat(sender, name) else 'That name exists. Use /use NAME.'
        if command == '/use':
            return f'Switched to {name}.' if use_chat(sender, name) else 'Chat not found. Send /list.'
        return delete_chat(sender, name)
    if command == '/rename':
        if len(parts) != 3 or not all(NAME_RE.fullmatch(x) for x in parts[1:]):
            return 'Usage: /rename OLD_NAME NEW_NAME'
        return rename_chat(sender, parts[1], parts[2])
    return 'Unknown command. Send /help.'


def worker():
    while True:
        sender, body = jobs.get()
        try:
            send_text(sender, handle_message(sender, body))
        except subprocess.TimeoutExpired:
            log.exception('Codex timeout')
            try:
                send_text(sender, 'Codex timed out. Try a smaller request.')
            except Exception:
                log.exception('Cannot send timeout reply')
        except Exception:
            log.exception('Job processing failed')
            try:
                send_text(sender, 'An error occurred. Check the Ubuntu bot logs.')
            except Exception:
                log.exception('Cannot send error reply')
        finally:
            jobs.task_done()


def valid_signature(body, supplied):
    if not APP_SECRET or not supplied.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


@app.get('/webhook')
def verify_webhook():
    if (request.args.get('hub.mode') == 'subscribe' and VERIFY_TOKEN
            and request.args.get('hub.verify_token') == VERIFY_TOKEN):
        return request.args.get('hub.challenge', ''), 200
    abort(403)


@app.post('/webhook')
def receive_webhook():
    raw = request.get_data()
    if not valid_signature(raw, request.headers.get('X-Hub-Signature-256', '')):
        abort(403)
    payload = request.get_json(silent=True) or {}
    if payload.get('object') != 'whatsapp_business_account':
        return 'ok', 200
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            if change.get('field') != 'messages':
                continue
            for msg in change.get('value', {}).get('messages', []):
                sender = str(msg.get('from', ''))
                if sender not in ALLOWED or msg.get('type') != 'text':
                    continue
                msg_id, body = msg.get('id'), msg.get('text', {}).get('body', '').strip()
                if not msg_id or not body:
                    continue
                try:
                    with db_connect() as conn:
                        conn.execute('INSERT INTO messages(id) VALUES(?)', (msg_id,))
                except sqlite3.IntegrityError:
                    continue
                try:
                    jobs.put_nowait((sender, body))
                except queue.Full:
                    log.error('Queue full for message %s', msg_id)
    return 'ok', 200


if __name__ == '__main__':
    missing = [name for name, value in {
        'META_APP_SECRET': APP_SECRET,
        'WHATSAPP_VERIFY_TOKEN': VERIFY_TOKEN,
        'WHATSAPP_ACCESS_TOKEN': ACCESS_TOKEN,
        'WHATSAPP_PHONE_NUMBER_ID': PHONE_ID,
        'ALLOWED_WHATSAPP_NUMBERS': ALLOWED,
    }.items() if not value]
    if missing:
        raise SystemExit('Missing .env configuration: ' + ', '.join(missing))
    if not PROJECT_DIR.is_dir():
        raise SystemExit(f'CODEX_PROJECT_DIR is not a directory: {PROJECT_DIR}')
    DB.parent.mkdir(parents=True, exist_ok=True)
    db_connect().close()
    threading.Thread(target=worker, daemon=True).start()
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '8000')), threaded=True)
