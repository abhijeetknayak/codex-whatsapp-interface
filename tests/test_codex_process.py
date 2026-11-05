import unittest
from pathlib import Path
from codex_api import (
    rpc_call, account_usage, list_server_threads, load_models, last_saved_exchange, run_codex,
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
