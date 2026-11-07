import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import db
import config


class TestDatabaseSafetyContracts(unittest.IsolatedAsyncioTestCase):
    """
    Validates asynchronous SQLite engine pragmas, schema migrations, composite index seeking,
    fleet state durability, and incident payload blackbox storage.
    """

    async def asyncSetUp(self):
        # Create an isolated temporary directory for test database instance
        self.test_dir = tempfile.mkdtemp()
        self.test_db_path = os.path.join(self.test_dir, "test_horizon.db")
        self.conn = await db.init_db(self.test_db_path)

    async def asyncTearDown(self):
        # Gracefully close connection and remove temporary database files
        await db.close_db()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    # --- 1. Engine & Pragmas Verification ---

    async def test_wal_pragmas_and_table_creation(self):
        # Asserts Write-Ahead Logging (WAL) and foreign keys are active
        cursor = await self.conn.execute("PRAGMA journal_mode;")
        row = await cursor.fetchone()
        self.assertEqual(row[0].lower(), "wal")

        cursor = await self.conn.execute("PRAGMA foreign_keys;")
        row = await cursor.fetchone()
        self.assertEqual(row[0], 1)

        # Verify all 4 master plan tables exist
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('requests', 'fleet_state', 'spend_state', 'incidents');"
        )
        tables = {r[0] for r in await cursor.fetchall()}
        self.assertEqual(tables, {"requests", "fleet_state", "spend_state", "incidents"})

    # --- 2. Telemetry Insertion & Indexed Retrieval ---

    async def test_insert_and_retrieve_request_telemetry(self):
        # Inserts complete request telemetry row and verifies retrieval with legacy aliases
        sample_row = {
            "ts": "2026-11-06T12:00:00Z",
            "day": "2026-11-06",
            "rid": "req_alpha_1",
            "session_id": "sess_101",
            "turn_index": 3,
            "caller_type": "Agent:builder",
            "caller_dir": "workspace/client-hub/api",
            "caller_os": "docker",
            "client_host": "172.18.0.2",
            "model_requested": "gemini-3.5-flash-lite",
            "model_dispatched": "gemini-3.5-flash-lite",
            "is_stream": 1,
            "key_label": "G1",
            "duration_total_s": 1.25,
            "gate_wait_s": 0.05,
            "pacing_wait_s": 0.12,
            "ttft_s": 0.35,
            "tokens_per_second": 85.5,
            "status_code": 200,
            "tokens_prompt_total": 450,
            "tokens_prompt_cached": 100,
            "tokens_completion": 80,
            "tokens_reasoning": 10,
            "tokens_by_role": {"system": 50, "user": 400},
            "tools_in_count": 2,
            "tools_called": ["search_code"],
            "cost_usd": 0.00015,
        }

        await db.insert_request(sample_row)

        retrieved = await db.get_day_requests("2026-11-06")
        self.assertEqual(len(retrieved), 1)
        r = retrieved[0]

        # Verify canonical schema fields and legacy UI aliases
        self.assertEqual(r["rid"], "req_alpha_1")
        self.assertEqual(r["caller_type"], "Agent:builder")
        self.assertEqual(r["m"], "gemini-3.5-flash-lite")
        self.assertEqual(r["in"], 450)
        self.assertEqual(r["c_in"], 100)
        self.assertEqual(r["out"], 80)
        self.assertEqual(r["tools_called"], ["search_code"])

    # --- 3. Day Summary & Rolling RPM Aggregates ---

    async def test_day_summary_and_rolling_rpm_queries(self):
        # Inserts a successful request and a failed request to test aggregate grouping
        day_str = "2026-11-06"
        await db.insert_request({
            "ts": "2026-11-06T14:00:00Z", "day": day_str, "rid": "req_ok",
            "model_requested": "mimo", "status_code": 200, "cost_usd": 0.02,
            "tokens_prompt_total": 100, "tokens_completion": 50,
        })
        await db.insert_request({
            "ts": "2026-11-06T14:00:30Z", "day": day_str, "rid": "req_fail",
            "model_requested": "mimo", "status_code": 500, "error_message": "Upstream error",
            "cost_usd": 0.0, "tokens_prompt_total": 50, "tokens_completion": 0,
        })

        summary = await db.get_day_summary(day_str)
        self.assertEqual(summary["total_reqs"], 2)
        self.assertEqual(summary["total_errors"], 1)
        self.assertAlmostEqual(summary["total_cost"], 0.02, places=4)
        self.assertEqual(summary["total_prompt"], 150)
        self.assertEqual(summary["total_completion"], 50)

        # Rolling RPM count since 13:59:00Z should detect both requests
        rpm = await db.get_rolling_rpm(day_str, "2026-11-06T13:59:00Z")
        self.assertEqual(rpm, 2)

    # --- 4. Fleet State Durability Contract ---

    async def test_fleet_state_quarantine_and_usage_persistence(self):
        # Verifies key quarantine and daily counters survive across queries
        day_str = "2026-11-06"
        await db.save_key_quarantine("G2", quarantined=True, reason="401 Revoked Key", day=day_str)
        await db.update_key_usage("G1", rpd_count=120, error_count=2, day=day_str)

        fleet = await db.load_fleet_state(day_str)
        self.assertIn("G1", fleet)
        self.assertIn("G2", fleet)

        # G2 must be quarantined with error message
        self.assertTrue(fleet["G2"]["quarantined"])
        self.assertEqual(fleet["G2"]["quarantine_reason"], "401 Revoked Key")

        # G1 must retain accurate call count
        self.assertFalse(fleet["G1"]["quarantined"])
        self.assertEqual(fleet["G1"]["rpd_count"], 120)
        self.assertEqual(fleet["G1"]["error_count"], 2)

    # --- 5. Spend State Singleton Budget Contract ---

    async def test_spend_state_singleton_enforcement(self):
        # Asserts singleton constraint id=1 and budget preservation
        await db.save_spend_state(daily_cap_usd=5.00, run_cap_usd=2.00, current_day="2026-11-06", day_usd=1.20)
        state1 = await db.load_spend_state()
        self.assertEqual(state1["daily_cap_usd"], 5.00)
        self.assertEqual(state1["day_usd"], 1.20)

        # Update cap and confirm singleton row updates without duplicate rows
        await db.save_spend_state(daily_cap_usd=8.00, run_cap_usd=3.00, current_day="2026-11-06", day_usd=1.50)
        cursor = await self.conn.execute("SELECT COUNT(*) FROM spend_state;")
        row = await cursor.fetchone()
        self.assertEqual(row[0], 1)

        state2 = await db.load_spend_state()
        self.assertEqual(state2["daily_cap_usd"], 8.00)
        self.assertEqual(state2["day_usd"], 1.50)

    # --- 6. Incident Blackbox & Foreign Key Contract ---

    async def test_incident_payload_storage(self):
        # Verifies full payload is saved and correctly linked to requests table
        day_str = "2026-11-06"
        await db.insert_request({
            "ts": "2026-11-06T15:00:00Z", "day": day_str, "rid": "inc_001",
            "model_requested": "mimo", "status_code": 200, "cost_usd": 0.15,
        })

        incident_payload = {
            "headers": {"authorization": "Bearer ***"},
            "body": {"prompt": "Analyze massive document"},
            "response": {"content": "Document analysis result..."},
            "usage": {"in": 50000, "out": 1200},
            "cost_breakdown": {"in_price": 0.435, "out_price": 0.87},
        }

        await db.save_incident("inc_001", cost_usd=0.15, prompt_tokens=50000, payload=incident_payload, day=day_str)

        cursor = await self.conn.execute("SELECT * FROM incidents WHERE rid = 'inc_001';")
        inc_row = await cursor.fetchone()
        self.assertIsNotNone(inc_row)
        self.assertEqual(inc_row["rid"], "inc_001")
        self.assertAlmostEqual(inc_row["cost_usd"], 0.15, places=4)
        self.assertEqual(inc_row["prompt_tokens"], 50000)


if __name__ == "__main__":
    unittest.main()