import os
import shutil
import logging
import unittest

import config
import analytics

class TestAnalyticsSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.log.setLevel(logging.CRITICAL)

    # Verifies personal paths are sanitized and the correct OS is identified across platforms
    def test_caller_path_sanitization_and_os_detection(self):
        # 1. Windows test
        win_path = r"C:\Users\developer\event-horizon\projects\arena-synergy\backend\ingest.py"
        path, detected_os = analytics.sanitize_caller(win_path)
        self.assertEqual(path, "event-horizon/projects/arena-synergy/backend/ingest.py")
        self.assertEqual(detected_os, "windows")

        # 2. macOS test
        mac_path = "/Users/developer/event-horizon/projects/arena-synergy/backend/ingest.py"
        path, detected_os = analytics.sanitize_caller(mac_path)
        self.assertEqual(path, "event-horizon/projects/arena-synergy/backend/ingest.py")
        self.assertEqual(detected_os, "macos")

        # 3. Linux test
        linux_path = "/home/ubuntu/event-horizon/projects/task-app/main.py"
        path, detected_os = analytics.sanitize_caller(linux_path)
        self.assertEqual(path, "event-horizon/projects/task-app/main.py")
        self.assertEqual(detected_os, "linux")

    # Verifies full payload is captured for high-value requests and ignored for low-cost ones
    def test_incident_trap_trigger(self):
        test_dir = os.path.join(config.LOGS_DIR, "test_incidents")
        analytics.config.INCIDENTS_DIR = test_dir

        # Standard low-cost call: ignored
        analytics.capture_incident_payload("rid-low", cost=0.001, prompt_tokens=500, payload={"prompt": "hi"})
        
        # High-cost call ($0.12 > $0.05 threshold): captured
        analytics.capture_incident_payload("rid-high", cost=0.12, prompt_tokens=1000, payload={"prompt": "heavy task"})

        created_files = []
        for root, _, files in os.walk(test_dir):
            created_files.extend(files)

        self.assertEqual(len(created_files), 1)
        self.assertTrue(created_files[0].startswith("rid-high"))

        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)

if __name__ == "__main__":
    unittest.main()