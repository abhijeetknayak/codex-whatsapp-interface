"""Offline integration tests. No real WhatsApp or Codex accounts required."""
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ["BOT_DISABLE_BACKGROUND"] = "1"
os.environ["META_APP_SECRET"] = "test_app_secret"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "test_verify"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "test_access"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "123456789"
os.environ["ALLOWED_WHATSAPP_NUMBERS"] = "491234567890"
os.environ["CODEX_PROJECT_DIR"] = str(ROOT)
os.environ["CODEX_ALLOWED_ROOTS"] = str(ROOT)
os.environ["BOT_DB_PATH"] = str(ROOT / "tests" / "unused_test_db.sqlite3")

try:
    import flask
except ImportError:
    # Local offline environment lacks Flask. The real package is required
    # at deployment; route logic is exercised with this test-only shim.
    import flask_stub
    sys.modules["flask"] = flask_stub

import bot as botmod  # noqa: E402
from store import Store  # noqa: E402
from codex_api import _normalize_model_list, format_rate_limits  # noqa: E402


MODELS = [
    {"id": "gpt-example-pro", "name": "Example Pro", "efforts": ["low", "medium", "high"],
     "default_effort": "medium", "default": True},
    {"id": "gpt-example-mini", "name": "Example Mini", "efforts": ["low", "medium"],
     "default_effort": "medium", "default": False},
]


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.repo1 = self.dir / "repo1"
        self.repo2 = self.dir / "repo2"
        self.repo1.mkdir()
        self.repo2.mkdir()
        cfg = botmod.Config()
        cfg.project_dir = self.repo1
        cfg.allowed_roots = [self.dir]
        cfg.db_path = self.dir / "store.sqlite3"
        self.store = Store(cfg.db_path, str(cfg.project_dir))
        self.bot = botmod.Bot(cfg, store=self.store)
        self.sender = "491234567890"
        self.app = botmod.create_app(self.bot).test_client()

    def test_webhook_verification_and_signatures(self):
        self.assertEqual(self.app.get("/webhook").status_code, 403)
        ok = self.app.get("/webhook?hub.mode=subscribe&hub.verify_token=test_verify&hub.challenge=hello")
        self.assertEqual((ok.status_code, ok.data), (200, b"hello"))
        payload = {
            "object": "whatsapp_business_account", "entry": [{"changes": [{
                "field": "messages", "value": {"metadata": {"phone_number_id": "123456789"},
                "messages": [{"id": "wamid.test1", "from": self.sender,
                              "type": "text", "text": {"body": "/help"}}]}}]}]
        }
        raw = json.dumps(payload).encode()
        self.assertEqual(self.app.post("/webhook", data=raw).status_code, 403)
        signature = "sha256=" + hmac.new(b"test_app_secret", raw, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
        self.assertEqual(self.app.post("/webhook", data=raw, headers=headers).status_code, 200)
        self.assertEqual(self.app.post("/webhook", data=raw, headers=headers).status_code, 200)
        with self.store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)

    def test_model_and_thinking_choices_per_chat(self):
        with patch.object(botmod, "load_models", return_value=MODELS):
            self.assertIn("Example Pro", self.bot.handle(self.sender, "/model"))
            self.assertIn("model set", self.bot.handle(self.sender, "2"))
            self.assertEqual(self.store.current(self.sender)["model"], "gpt-example-mini")
            self.assertIn("2. MEDIUM", self.bot.handle(self.sender, "/thinking"))
            self.assertIn("thinking set", self.bot.handle(self.sender, "2"))
            self.assertEqual(self.store.current(self.sender)["effort"], "medium")
            self.bot.handle(self.sender, "/new robotics")
            self.assertIsNone(self.store.current(self.sender)["model"])
            self.bot.handle(self.sender, "/use main")
            self.assertEqual(self.store.current(self.sender)["model"], "gpt-example-mini")
            self.assertEqual(self.store.current(self.sender)["effort"], "medium")

    def test_pending_jobs_switch_and_preview(self):
        self.bot.handle(self.sender, "/new robotics")
        response = self.bot.handle(self.sender, "Fix calibration", "wamid.1")
        self.assertIn("queued", response)
        self.bot.handle(self.sender, "/new planning")
        self.assertIn("queued", self.bot.handle(self.sender, "Explain planner", "wamid.2"))
        job = self.store.claim_turn()
        self.assertEqual(job["name"], "robotics")
        # Different chat in same project must wait to avoid conflicting code edits.
        self.assertIsNone(self.store.claim_turn())
        self.store.finish_turn(job, "Fixed files", "done", "thread1", 10, 2)
        job2 = self.store.claim_turn()
        self.assertEqual(job2["name"], "planning")
        self.store.finish_turn(job2, "Planner overview", "done", "thread2")
        self.assertIn("Planner overview", self.bot.handle(self.sender, "/use planning"))
        preview = self.bot.handle(self.sender, "/use robotics")
        self.assertIn("Fix calibration", preview)
        self.assertIn("Fixed files", preview)
        self.assertEqual(self.store.current(self.sender)["thread_id"], "thread1")
        self.assertEqual(self.store.token_stats(self.sender)["input"], 10)

    def test_different_projects_can_run_independently(self):
        self.bot.handle(self.sender, "/new robotics")
        self.bot.handle(self.sender, f"/project {self.repo2}")
        self.bot.handle(self.sender, "Do X", "wamid.3")
        self.bot.handle(self.sender, "/use main")
        self.bot.handle(self.sender, "Do Y", "wamid.4")
        first = self.store.claim_turn()
        second = self.store.claim_turn()
        self.assertIsNotNone(second)
        self.assertNotEqual(first["cwd"], second["cwd"])

    def test_server_import_and_stable_snapshot(self):
        with patch.object(botmod, "list_server_threads", return_value=[
            {"id": "01aaa111-2222-3333-4444-555555555555", "title": "Actual title", "cwd": str(self.repo2)},
            {"id": "01bbb111-2222-3333-4444-555555555555", "title": "Another title", "cwd": str(self.repo1)},
        ]):
            self.assertIn("Actual title", self.bot.handle(self.sender, "/server"))
        self.assertIn("Imported", self.bot.handle(self.sender, "/import restored 1"))
        self.assertEqual(self.store.current(self.sender)["cwd"], str(self.repo2))
        self.assertTrue(self.store.current(self.sender)["thread_id"].startswith("01aaa"))

    def test_rate_limits_only_report_real_windows(self):
        sample = {"rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1793808000},
            "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 1794008000}
        }}}
        msg = format_rate_limits(sample)
        self.assertIn("5-hour: 40% used", msg)
        self.assertIn("Weekly: 60% used", msg)
        self.assertIn("Not reported", format_rate_limits({}))

    def test_options_from_codex_catalog(self):
        entries = _normalize_model_list([
            {"model": "model-a", "displayName": "Model A", "defaultReasoningEffort": "medium",
             "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}]},
            {"model": "guardian-review", "hidden": True},
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["efforts"], ["low", "medium"])

    def test_migration_existing_chats(self):
        old = self.dir / "old.sqlite3"
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE chats(sender TEXT, name TEXT, thread_id TEXT, PRIMARY KEY(sender,name))")
        conn.execute("CREATE TABLE active_chats(sender TEXT PRIMARY KEY,name TEXT)")
        conn.execute("CREATE TABLE chat_model_choices(sender TEXT,name TEXT,model TEXT,PRIMARY KEY(sender,name))")
        conn.execute("INSERT INTO chats VALUES('491234567890','old','old-thread')")
        conn.execute("INSERT INTO chat_model_choices VALUES('491234567890','old','model-from-old')")
        conn.execute("INSERT INTO active_chats VALUES('491234567890','old')")
        conn.commit(); conn.close()
        migrated = Store(old, str(self.repo1))
        self.assertEqual(migrated.current(self.sender)["model"], "model-from-old")
        self.assertEqual(migrated.current(self.sender)["thread_id"], "old-thread")

    def test_outbox_large_messages_are_split_and_ordered(self):
        self.store.reply(self.sender, "x" * 7100)
        first = self.store.take_outbox()
        self.assertEqual(len(first["body"]), 3000)
        # A retry in flight blocks later chunks to the same recipient.
        self.store.mark_outbox(first["id"], "pending", "rate limited", delay=60)
        self.assertIsNone(self.store.take_outbox())
        self.store.mark_outbox(first["id"], "sent")
        second = self.store.take_outbox()
        self.assertEqual(len(second["body"]), 3000)
        self.store.mark_outbox(second["id"], "sent")
        third = self.store.take_outbox()
        self.assertEqual(len(third["body"]), 1100)

    def test_crash_does_not_reexecute_started_tasks(self):
        self.bot.handle(self.sender, "Edit source", "wamid.crash")
        self.assertIsNotNone(self.store.claim_turn())
        restored = Store(self.store.path, str(self.repo1))
        with restored.db() as c:
            self.assertEqual(c.execute("SELECT status FROM turns LIMIT 1").fetchone()[0], "interrupted")
        self.assertIsNone(restored.claim_turn())


if __name__ == "__main__":
    unittest.main()
