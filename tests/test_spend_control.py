import os
import time
import logging
import unittest
from unittest.mock import patch

import config
from controller import SpendController

class TestSpendControlSafetyContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.log.setLevel(logging.CRITICAL)

    def setUp(self):
        self.ctrl = SpendController()
        self.ctrl.daily_cap = 2.00
        self.ctrl.run_cap = 10.00
        self.ctrl.ledger = {
            "day": self.ctrl.get_today_str(time.time()),
            "day_usd": 0.0,
            "run_usd": 0.0,
            "run_last": 0.0,
            "run_n": 0
        }

    # Halts paid traffic immediately once spend exceeds the cap
    def test_daily_spend_limit_enforced(self):
        now = time.time()
        self.assertIsNone(self.ctrl.check_spend(now))

        self.ctrl.record_cost(1.50, now)
        self.assertIsNone(self.ctrl.check_spend(now))

        self.ctrl.record_cost(0.60, now)
        err = self.ctrl.check_spend(now)
        self.assertIsNotNone(err)
        self.assertIn("Daily spend cap", err)

    # Inactivity cleans the run accumulator
    def test_run_inactivity_reset(self):
        t0 = 1000.0
        self.ctrl.record_cost(0.80, t0)
        self.assertEqual(self.ctrl.ledger["run_usd"], 0.80)

        t_active = t0 + 60.0
        self.assertIsNone(self.ctrl.check_spend(t_active))

        t_idle = t_active + config.RUN_IDLE_SECONDS + 10.0
        self.assertIsNone(self.ctrl.check_spend(t_idle))
        self.assertEqual(self.ctrl.ledger["run_usd"], 0.0)

    # Budget modifications survive restart
    def test_persistence_contract(self):
        test_file = os.path.join(config.LOGS_DIR, "test_spend.json")
        with patch.object(config, "SPEND_FILE", test_file):
            self.ctrl.save_limits(daily_usd=10.00, run_usd=4.00)
            fresh_ctrl = SpendController()
            self.assertEqual(fresh_ctrl.daily_cap, 10.00)
            self.assertEqual(fresh_ctrl.run_cap, 4.00)

            if os.path.exists(test_file):
                os.remove(test_file)

if __name__ == "__main__":
    unittest.main()