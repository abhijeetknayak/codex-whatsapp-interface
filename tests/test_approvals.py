"""Approval integration: signed WhatsApp decisions -> app-server -> completion."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from appserver_runner import approval_result, run_codex_interactive
from codex_api import CodexCancelled
from store import Store

FAKE = str(Path(__file__).with_name("fake_appserver.py"))


class TestApprovals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.store = Store(self.cwd / "store.sqlite3", str(self.cwd))
        self.sender = "491234567890"
        self.store.enqueue_turn("msg-1", self.sender, "request_command")
        self.turn = self.store.claim_turn()

    def start(self, prompt="request_command", timeout=8):
        response = []
        err = []
        def worker():
            try:
                response.append(run_codex_interactive(
                    FAKE, self.cwd, prompt, None, None, None, timeout,
                    lambda: self.store.is_cancel_requested(self.turn["id"]),
                    self.store, self.turn, approval_timeout=3))
            except BaseException as exc:
                err.append(exc)
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        return th, response, err

    def wait_for_approval(self, deadline=4):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            approvals = self.store.pending_approvals(self.sender)
            if approvals:
                return approvals[0]
            time.sleep(0.05)
        self.fail("No approval request was relayed to SQLite")

    def test_command_approval_accepted(self):
        th, results, errors = self.start()
        approval = self.wait_for_approval()
        self.assertIn("echo hello", approval["details"])
        outbox = self.store.take_outbox()
        self.assertIn(f"/approve {approval['id']}", outbox["body"])
        self.assertEqual(self.store.decide_approval("wrong-sender", approval["id"], True)["state"], "missing")
        self.assertEqual(self.store.decide_approval(self.sender, approval["id"], True)["state"], "approved")
        th.join(4)
        self.assertFalse(errors, str(errors))
        self.assertEqual(results[0]["response"], "APPROVED")
        self.assertEqual(self.store.decide_approval(self.sender, approval["id"], False)["state"], "approved")

    def test_file_rejected(self):
        th, results, errors = self.start("request_file")
        approval = self.wait_for_approval()
        self.assertIn("README.md", approval["details"])
        self.store.decide_approval(self.sender, approval["id"], False)
        th.join(4)
        self.assertFalse(errors, str(errors))
        self.assertEqual(results[0]["response"], "DECLINED")

    def test_network_request_grants_once_and_only_requested(self):
        th, results, errors = self.start("request_permissions")
        approval = self.wait_for_approval()
        self.assertIn("enabled", approval["details"])
        self.store.decide_approval(self.sender, approval["id"], True)
        th.join(4)
        self.assertFalse(errors, str(errors))
        self.assertEqual(results[0]["response"], "APPROVED")
        response = approval_result("item/permissions/requestApproval", True,
                                   {"permissions": {"network": {"enabled": True}}})
        self.assertEqual(response, {"scope": "turn", "permissions": {"network": {"enabled": True}}})
        response = approval_result("item/permissions/requestApproval", False,
                                   {"permissions": {"network": {"enabled": True}}})
        self.assertEqual(response["permissions"], {})

    def test_cancel_while_waiting(self):
        th, results, errors = self.start("request_command")
        approval = self.wait_for_approval()
        self.store.cancel_turn(self.sender, self.turn["id"])
        th.join(4)
        self.assertTrue(errors and isinstance(errors[0], CodexCancelled), str(errors))
        self.assertEqual(self.store.approval_decision(approval["id"]), "stale")

    def test_approval_expires(self):
        th, results, errors = self.start("request_command", timeout=7)
        approval = self.wait_for_approval()
        th.join(6)
        self.assertFalse(errors, str(errors))
        self.assertEqual(results[0]["response"], "DECLINED")
        self.assertEqual(self.store.approval_decision(approval["id"]), "expired")

    def test_crash_recovery_invalidates_approvals(self):
        th, results, errors = self.start("request_command")
        approval = self.wait_for_approval()
        recovered = Store(self.store.path, str(self.cwd))
        self.assertEqual(recovered.approval_decision(approval["id"]), "stale")
        self.store.cancel_turn(self.sender, self.turn["id"])
        th.join(4)


if __name__ == "__main__":
    unittest.main()
