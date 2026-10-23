import os
import time
import logging
import asyncio
import unittest
from collections import deque
from unittest.mock import patch

import config
from key_manager import GeminiKeyManager

class TestKeyPoolSafetyContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.log.setLevel(logging.CRITICAL)

    def _build_test_manager(self, count=4):
        km = GeminiKeyManager()
        km.keys = [(i + 1, f"mock-key-{i+1}") for i in range(count)]
        km.num_keys = count
        km.rpm_limit = 15
        km.tpm_limit = 250_000
        km.rpd_limit = 500
        km.last_used = [0.0] * count
        km.tokens_history = [deque() for _ in range(count)]
        km.inflight_tokens = [0] * count
        km.rpd_count = [0] * count
        km.quarantined = [False] * count
        km.quarantine_reason = [""] * count
        km.label_to_pos = {f"G{i+1}": i for i in range(count)}
        return km

    # Prevents alphabetical sorting bugs where Key 10 precedes Key 2
    def test_key_discovery_natural_ordering(self):
        fake_env = {
            "GEMINI_KEY_12": "val-12",
            "GEMINI_KEY_2": "val-2",
            "GEMINI_KEY_1": "val-1",
            "GEMINI_KEY_20": "val-20",
        }
        with patch.dict(os.environ, fake_env, clear=True):
            discovered = config._discover_gemini_keys()
            indices = [num for num, _ in discovered]
            self.assertEqual(indices, [1, 2, 12, 20])

    # Multiple subagents hitting the proxy at the same millisecond must not dogpile on one key
    @patch("asyncio.sleep", return_value=None)
    def test_concurrent_burst_no_dogpile(self, _):
        km = self._build_test_manager(count=4)

        # 4 simultaneous requests asking for 150k tokens each.
        # Since each key holds 250k, the scheduler must distribute each to a separate key.
        async def burst():
            tasks = [km.acquire_key(estimated_tokens=150_000) for _ in range(4)]
            return await asyncio.gather(*tasks)

        results = asyncio.run(burst())
        acquired_keys = [label for label, _, _, _ in results]
        self.assertEqual(len(set(acquired_keys)), 4)

    # Failed calls must not leak reservations, and full keys must regain capacity after 60s
    def test_reservation_leak_and_time_recovery(self):
        km = self._build_test_manager(count=1)
        now = 1000.0

        # Simulating client disconnect or failure mid-turn
        km.inflight_tokens[0] = 100_000
        self.assertEqual(km.get_tpm_usage(0, now), 100_000)
        km.release_reservation("G1", 100_000)
        self.assertEqual(km.get_tpm_usage(0, now), 0)

        # Simulating completed heavy usage
        km.tokens_history[0].append((now, 240_000))
        self.assertGreater(km.get_tpm_wait(0, now, needed_tokens=20_000), 0.0)

        # Fast-forwarding past the 60s sliding window
        t_future = now + 65.0
        self.assertEqual(km.get_tpm_wait(0, t_future, needed_tokens=20_000), 0.0)

    # Revoked or rate-blocked keys must be completely bypassed by rotation
    @patch("asyncio.sleep", return_value=None)
    def test_quarantined_key_excluded(self, _):
        km = self._build_test_manager(count=2)
        km.quarantine_key("G1", "401 Invalid Key")

        async def run():
            assigned = []
            for _ in range(4):
                label, _, _, _ = await km.acquire_key(estimated_tokens=500)
                assigned.append(label)
                km.record_usage(label, 500, 500)
            return assigned

        chosen = asyncio.run(run())
        self.assertTrue(all(label == "G2" for label in chosen))
        self.assertNotIn("G1", chosen)

    # Heavy wait times must flag the fallback model instead of stalling the pipeline
    @patch("asyncio.sleep", return_value=None)
    def test_congested_fallback_trigger(self, _):
        km = self._build_test_manager(count=1)
        now = 1000.0
        km.tokens_history[0].append((now, 250_000))
        km.last_used[0] = now

        async def run():
            return await km.acquire_key(estimated_tokens=10_000)

        with patch("time.time", return_value=now + 1.0):
            _, _, _, use_fallback = asyncio.run(run())
            self.assertTrue(use_fallback)

if __name__ == "__main__":
    unittest.main()