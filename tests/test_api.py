import logging
import unittest
from fastapi.testclient import TestClient

import config
from controller import controller
from key_manager import key_manager
from main import app

class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mute logging so test runs stay clean
        config.log.setLevel(logging.CRITICAL)
        cls.client = TestClient(app)
        
        # Save our real environment keys so we can restore them at the end
        cls.real_keys = list(key_manager.keys)
        # Give our tests a clean two-key pool to work with
        key_manager.set_keys([(1, "mock-k1"), (2, "mock-k2")])

    @classmethod
    def tearDownClass(cls):
        # Put back our real keys when all tests finish
        key_manager.set_keys(cls.real_keys)

    def setUp(self):
        # Reset budget and quarantine flags before every test
        self.orig_daily_cap = controller.daily_cap
        self.orig_day_usd = controller.ledger["day_usd"]
        key_manager.quarantined = [False] * key_manager.num_keys

    def tearDown(self):
        # Reset budget back to what it was
        controller.daily_cap = self.orig_daily_cap
        controller.ledger["day_usd"] = self.orig_day_usd
        key_manager.quarantined = [False] * key_manager.num_keys

    # Check that the health check shows our keys and reports healthy
    def test_health_check_reports_status(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["keys_alive"], "2/2")

    # If all our keys get blocked or banned, the health check should return 503
    def test_health_check_fails_if_all_keys_die(self):
        # Mark all keys as broken
        key_manager.quarantined = [True] * key_manager.num_keys

        res = self.client.get("/health")
        self.assertEqual(res.status_code, 503)
        data = res.json()
        self.assertIsNotNone(data)
        self.assertEqual(data["status"], "unhealthy")

    # Stop paid models if we already hit our spending limit for the day
    def test_blocks_paid_calls_when_budget_is_maxed(self):
        controller.daily_cap = 1.00
        controller.ledger["day_usd"] = 1.50

        res = self.client.post("/v1/chat/completions", json={
            "model": "mimo-v2.6-pro",
            "messages": [{"role": "user", "content": "hi"}]
        })

        self.assertEqual(res.status_code, 402)
        self.assertIn("Daily spend cap", res.json()["error"]["message"])

    # Quick check to make sure the main dashboard page opens without crashing
    def test_dashboard_page_loads(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("HORIZON PROXY", res.text)

if __name__ == "__main__":
    unittest.main()