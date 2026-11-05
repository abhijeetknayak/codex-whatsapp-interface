import unittest
import time
import threading
from pathlib import Path
from codex_api import (
    CodexCancelled, rpc_call, account_usage, list_server_threads, load_models, last_saved_exchange, run_codex,
)

FAKE = str(Path(__file__).parent / "fake_codex.py")

class CodexProcessTests(unittest.TestCase):
    def test_model_list_through_app_server(self):
        models = load_models(FAKE, Path("/does/not/exist"))
        self.assertEqual(models[0]["id"], "mock-pro")
        self.assertEqual(models[0]["efforts"], ["medium", "high"])

    def test_quota_windows(self):
        text = account_usage(FAKE)
        self.assertIn("5-hour: 13%", text)
        self.assertIn("Weekly: 42%", text)

    def test_named_sessions_and_history(self):
        sessions = list_server_threads(FAKE)
        self.assertEqual(sessions[0]["title"], "Genuine archived title")
        text = last_saved_exchange(FAKE, sessions[0]["id"])
        self.assertIn("What is my project?", text)
        self.assertIn("mock project", text)

    def test_cancellation_stops_only_live_codex_process(self):
        cancelled = threading.Event()
        trigger = threading.Thread(target=lambda: (time.sleep(0.5), cancelled.set()), daemon=True)
        trigger.start()
        started = time.monotonic()
        with self.assertRaises(CodexCancelled):
            run_codex(FAKE, Path(__file__).parent, "sleep_forever", None, None, None, 15,
                      should_cancel=cancelled.is_set)
        self.assertLess(time.monotonic() - started, 5)
        # A different session/process still works afterward.
        self.assertIn("still works", run_codex(
            FAKE, Path(__file__).parent, "still works", None, None, None, 10)["response"])


    def test_workspace_write_applied_to_new_and_resumed_tasks(self):
        output = run_codex(FAKE, Path(__file__).parent, "print_cli_args", None,
                           None, None, 10, sandbox="workspace-write")
        self.assertIn("--sandbox workspace-write", output["response"])
        self.assertNotIn("--yolo", output["response"])
        resumed = run_codex(FAKE, Path(__file__).parent, "print_cli_args", output["thread_id"],
                            None, None, 10, sandbox="workspace-write")
        self.assertIn("--sandbox workspace-write", resumed["response"])
        self.assertIn("resume", resumed["response"])

    def test_read_only_and_inherit_modes(self):
        output = run_codex(FAKE, Path(__file__).parent, "print_cli_args", None,
                           None, None, 10, sandbox="read-only")
        self.assertIn("--sandbox read-only", output["response"])
        inherited = run_codex(FAKE, Path(__file__).parent, "print_cli_args", None,
                              None, None, 10, sandbox="inherit")
        self.assertNotIn("--sandbox", inherited["response"])
        with self.assertRaises(Exception):
            run_codex(FAKE, Path(__file__).parent, "print_cli_args", None,
                      None, None, 10, sandbox="danger-full-access")

    def test_cli_run_and_resume_with_override(self):
        result = run_codex(FAKE, Path(__file__).parent, "Run ls", None, "mock-pro", "high", 10)
        self.assertEqual(result["response"], "Mock response: Run ls")
        self.assertEqual(result["input_tokens"], 23)
        resumed = run_codex(FAKE, Path(__file__).parent, "Run tests", result["thread_id"], "mock-pro", "medium", 10)
        self.assertIn("Run tests", resumed["response"])
        with_warning = run_codex(FAKE, Path(__file__).parent, "Show warning but finish", result["thread_id"], None, None, 10)
        self.assertIn("finish", with_warning["response"])

if __name__ == "__main__":
    unittest.main()
