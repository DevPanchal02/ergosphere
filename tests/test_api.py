import logging
import unittest
from fastapi.testclient import TestClient

import analytics
import config
from controller import controller
from key_manager import key_manager
from main import app


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mute debug and info logs to keep test output clean
        config.log.setLevel(logging.CRITICAL)
        cls.client = TestClient(app)

        # Preserve real keys from environment and use mock keys for testing
        cls.real_keys = list(key_manager.keys)
        key_manager.set_keys([(1, "mock-k1"), (2, "mock-k2")])

    @classmethod
    def tearDownClass(cls):
        # Restore real keys when the suite finishes
        key_manager.set_keys(cls.real_keys)

    def setUp(self):
        # Snapshot state before each test
        self.orig_daily_cap = controller.daily_cap
        self.orig_day_usd = controller.ledger["day_usd"]
        key_manager.quarantined = [False] * key_manager.num_keys

    def tearDown(self):
        # Restore state after each test
        controller.daily_cap = self.orig_daily_cap
        controller.ledger["day_usd"] = self.orig_day_usd
        key_manager.quarantined = [False] * key_manager.num_keys

    # --- Core Proxy Health & Budget Tests ---

    def test_health_check_reports_status(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["keys_alive"], "2/2")

    def test_health_check_fails_if_all_keys_die(self):
        key_manager.quarantined = [True] * key_manager.num_keys

        res = self.client.get("/health")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["status"], "unhealthy")

    def test_blocks_paid_calls_when_budget_is_maxed(self):
        controller.daily_cap = 1.00
        controller.ledger["day_usd"] = 1.50

        res = self.client.post("/v1/chat/completions", json={
            "model": "mimo-v2.6-pro",
            "messages": [{"role": "user", "content": "hi"}]
        })

        self.assertEqual(res.status_code, 402)
        self.assertIn("Daily spend cap", res.json()["error"]["message"])

    def test_dashboard_page_loads(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("HORIZON PROXY", res.text)

    # --- Privacy & Path Doxxing Tests ---

    def test_sanitizer_strips_windows_usernames_and_drives(self):
        # Windows path must hide drive root, username, and keep only the last 3 folders
        dirty_path = r"C:\Users\JohnDoe\Desktop\Private\event_horizon\projects\scanner"
        clean_dir, os_name = analytics.sanitize_path(dirty_path)

        self.assertEqual(os_name, "windows")
        self.assertEqual(clean_dir, "event_horizon/projects/scanner")
        self.assertNotIn("JohnDoe", clean_dir)
        self.assertNotIn("C:", clean_dir)

    def test_sanitizer_strips_macos_and_linux_user_homes(self):
        # Mac path should strip /Users/username/ without hardcoding usernames
        mac_path = "/Users/alice_smith/repos/crypto_bot/backend"
        clean_mac, os_mac = analytics.sanitize_path(mac_path)

        self.assertEqual(os_mac, "macos")
        self.assertEqual(clean_mac, "repos/crypto_bot/backend")
        self.assertNotIn("alice_smith", clean_mac)

        # Linux path should strip /home/username/ and keep the local folder
        linux_path = "/home/deployer/my_custom_project"
        clean_linux, os_linux = analytics.sanitize_path(linux_path)

        self.assertEqual(os_linux, "linux")
        self.assertEqual(clean_linux, "my_custom_project")
        self.assertNotIn("deployer", clean_linux)

    def test_sanitizer_handles_empty_or_scratchpad_paths(self):
        # Ad-hoc or missing paths should default safely without crashing
        clean_dir, os_name = analytics.sanitize_path("")
        self.assertEqual(clean_dir, "terminal/adhoc")
        self.assertEqual(os_name, "unknown")

    # --- Caller Identification Tests ---

    def test_detects_agent_tool_from_semantic_bearer_key(self):
        # Agent-made tools pass their project key in the Authorization header
        headers = {"Authorization": "Bearer sk-agent_tool-projects/ios_app"}
        caller_type, rough_dir, _ = analytics.detect_caller_and_os(headers, {}, "127.0.0.1")

        self.assertEqual(caller_type, "Agent-Tool")
        self.assertEqual(rough_dir, "projects/ios_app")

    def test_detects_user_from_custom_project_bearer_key(self):
        # Your own manual scripts outside OpenCode pass a user key
        headers = {"Authorization": "Bearer sk-user-crypto_bot/trading"}
        caller_type, rough_dir, _ = analytics.detect_caller_and_os(headers, {}, "192.168.1.150")

        self.assertEqual(caller_type, "User")
        self.assertEqual(rough_dir, "crypto_bot/trading")

    def test_detects_opencode_agent_from_system_prompt(self):
        # OpenCode agents identify themselves via their system instructions
        body = {
            "messages": [
                {"role": "system", "content": "You are the builder for one developer. Workspace is projects/ios_test."}
            ]
        }
        caller_type, rough_dir, _ = analytics.detect_caller_and_os({}, body, "127.0.0.1")

        self.assertEqual(caller_type, "Agent:builder")
        self.assertEqual(rough_dir, "projects/ios_test")

    def test_detects_subtool_via_openai_user_parameter(self):
        # Standard OpenAI SDK calls passing user='agent_tool:parser'
        body = {"user": "agent_tool:parser"}
        caller_type, rough_dir, _ = analytics.detect_caller_and_os({}, body, "127.0.0.1")

        self.assertEqual(caller_type, "Agent-Tool")
        self.assertEqual(rough_dir, "parser")


if __name__ == "__main__":
    unittest.main()