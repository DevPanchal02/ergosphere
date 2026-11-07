import logging
import unittest
from unittest.mock import patch

import config
import analytics


class TestAnalytics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Silence verbose logger output during test execution
        config.log.setLevel(logging.CRITICAL)

    # --- 1. Master Plan Verification Matrix Tests ---

    def test_master_plan_verification_matrix(self):
        # Validates all 5 source environments defined in Section 4.2 of the Master Plan
        matrix_cases = [
            # Windows PC: strips drive and username, pops file name, keeps last 3 directories
            (r"C:\Users\Developer\workspace\client-hub\api\server.py", "workspace/client-hub/api", "windows"),
            # Docker Sandbox: strips leading slash, preserves last 3 directories
            ("/workspace/open-code/packages/core", "open-code/packages/core", "docker"),
            # WSL2: strips /mnt/c/ and username, keeps last 3 directories
            ("/mnt/c/Users/Developer/projects/infra/terraform", "projects/infra/terraform", "wsl"),
            # macOS Remote: strips /Users/charlie/, pops App.swift, keeps last 3 directories
            ("/Users/charlie/Development/mobile/ios/App.swift", "Development/mobile/ios", "macos"),
            # Ad-hoc / Scratch: pops test_tool.py, single remaining directory
            ("/tmp/test_tool.py", "tmp", "unknown"),
        ]

        for raw_path, expected_dir, expected_os in matrix_cases:
            with self.subTest(raw_path=raw_path):
                clean_dir, detected_os = analytics.sanitize_path(raw_path)
                self.assertEqual(clean_dir, expected_dir)
                if expected_os != "unknown":
                    self.assertEqual(detected_os, expected_os)

    # --- 2. Path Sanitizer Edge Cases ---

    def test_sliding_window_clamps_to_three_directories(self):
        # Deep directory trees must be clamped strictly to the final 3 directory segments
        deep_path = "/home/developer/repos/enterprise/crm/backend/services/billing/worker.py"
        clean_dir, detected_os = analytics.sanitize_path(deep_path)
        # 6 segments after popping worker.py -> last 3: backend, services, billing
        self.assertEqual(clean_dir, "backend/services/billing")
        self.assertEqual(detected_os, "linux")

    def test_shallow_paths_preserve_all_segments(self):
        # Paths with fewer than 3 segments should not be truncated
        short_path = "C:\\projects\\agent\\main.py"
        clean_dir, detected_os = analytics.sanitize_path(short_path)
        self.assertEqual(clean_dir, "projects/agent")
        self.assertEqual(detected_os, "windows")

    def test_empty_and_adhoc_paths_fallback_safely(self):
        # Empty or missing paths must return a clean ad-hoc placeholder without raising exceptions
        for empty_val in ("", "   ", "-", "unknown", "none"):
            with self.subTest(val=empty_val):
                clean_dir, detected_os = analytics.sanitize_path(empty_val)
                self.assertEqual(clean_dir, "terminal/adhoc")
                self.assertEqual(detected_os, "unknown")

    def test_sanitize_caller_alias_compatibility(self):
        # Verifies the backwards-compatibility alias points to sanitize_path
        res_dir, res_os = analytics.sanitize_caller("/workspace/core/engine")
        self.assertEqual(res_dir, "workspace/core/engine")

    # --- 3. Bearer Token Auth & Positional Metadata Parsing ---

    def test_bearer_token_metadata_unpacking(self):
        # Parses Bearer tokens with positional split: sk_master-<caller/role>-<os>-<project>
        auth = "Bearer sk_horizon_master-agent:planner-docker-repos/app/core"
        valid, role, os_hint, proj = analytics.parse_bearer_token(auth, master_key="sk_horizon_master")

        self.assertTrue(valid)
        self.assertEqual(role, "agent:planner")
        self.assertEqual(os_hint, "docker")
        self.assertEqual(proj, "repos/app/core")

    def test_bearer_token_two_part_compatibility(self):
        # Supports legacy two-part tokens like sk-agent_tool-projects/ios_app
        auth = "Bearer sk-agent_tool-projects/ios_app"
        valid, role, os_hint, proj = analytics.parse_bearer_token(auth, master_key=None)

        self.assertTrue(valid)
        self.assertEqual(role, "agent_tool")
        self.assertEqual(proj, "projects/ios_app")

    def test_master_key_verification_enforcement(self):
        # Rejects tokens that do not start with configured master key secret
        with patch.object(config, "MASTER_KEY", "sk_horizon_secret"):
            # Valid master key
            self.assertTrue(analytics.verify_master_key("Bearer sk_horizon_secret-user-win-app"))
            # Invalid master key
            self.assertFalse(analytics.verify_master_key("Bearer sk_other_key-user-win-app"))
            # Missing header
            self.assertFalse(analytics.verify_master_key(""))

    # --- 4. Dynamic OpenCode Persona Sniffing ---

    def test_dynamic_persona_sniffing_from_system_prompt(self):
        # Sniffs arbitrary agent roles from instructions without hardcoded enums
        personas = [
            ("You are the code-architect for this team.", "Agent:code-architect"),
            ("You are a security-auditor reviewing pull requests.", "Agent:security-auditor"),
            ("You are the planner. Outline next steps.", "Agent:planner"),
            ("You are an experienced QA-tester.", "Agent:qa-tester"),
        ]

        for prompt_text, expected_role in personas:
            with self.subTest(prompt=prompt_text):
                body = {"messages": [{"role": "system", "content": prompt_text}]}
                caller_type, _, _ = analytics.detect_caller_and_os({}, body, "127.0.0.1")
                self.assertEqual(caller_type, expected_role)

    def test_network_origin_os_fallback(self):
        # Resolves OS from client host IP when path syntax has no hints
        _, _, os_win = analytics.detect_caller_and_os({}, {}, "127.0.0.1")
        self.assertEqual(os_win, "windows")

        _, _, os_docker = analytics.detect_caller_and_os({}, {}, "172.18.0.4")
        self.assertEqual(os_docker, "docker")

        _, _, os_mac = analytics.detect_caller_and_os({}, {}, "192.168.1.105")
        self.assertEqual(os_mac, "macos")

    # --- 5. Context & Turn Derivation ---

    def test_derive_request_context_session_and_turns(self):
        # Computes deterministic session ID hash and counts user/tool turn steps
        body = {
            "messages": [
                {"role": "system", "content": "You are the builder."},
                {"role": "user", "content": "Build the login component."},
                {"role": "assistant", "content": "Executing component creation..."},
                {"role": "tool", "content": "File created."},
                {"role": "assistant", "content": "Component created."},
            ]
        }

        session_id, turn_index, tokens_by_role = analytics.derive_request_context({}, body)

        # Hash must be non-empty and reproducible for same root prompt
        self.assertTrue(len(session_id) >= 8)
        # Turns: 1 user + 1 tool = 2
        self.assertEqual(turn_index, 2)
        # Tokens partitioned by role
        self.assertIn("system", tokens_by_role)
        self.assertIn("user", tokens_by_role)
        self.assertIn("tool", tokens_by_role)
        self.assertGreater(tokens_by_role["user"], 0)


if __name__ == "__main__":
    unittest.main()