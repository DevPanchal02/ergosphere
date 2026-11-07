import os
import json
import time
import asyncio
import sqlite3
from datetime import datetime
from typing import Optional
import config


class SpendController:
    """
    Manages daily and per-run dollar spend caps. Replaces spend_limits.json with
    the spend_state SQLite table and preloads spend via indexed queries on requests.
    """

    def __init__(self):
        self.daily_cap = config.DEFAULT_DAILY_SPEND_LIMIT_USD
        self.run_cap = config.DEFAULT_RUN_SPEND_LIMIT_USD
        self.ledger = {
            "day": self.get_today_str(time.time()),
            "day_usd": 0.0,
            "run_usd": 0.0,
            "run_last": 0.0,
            "run_n": 0,
        }
        self.load_limits()

    def get_today_str(self, ts: float) -> str:
        """Formats timestamp into YYYY-MM-DD string in configured local timezone."""
        return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d")

    def load_limits(self):
        """Loads spend limits and ledger state from SQLite spend_state, falling back to JSON."""
        loaded_from_db = False

        if os.path.exists(config.DB_PATH):
            try:
                with sqlite3.connect(config.DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT daily_cap_usd, run_cap_usd, day_usd, run_usd, run_last_ts, run_request_count, current_day "
                        "FROM spend_state WHERE id = 1;"
                    )
                    row = cursor.fetchone()
                    if row:
                        self.daily_cap = float(row[0])
                        self.run_cap = float(row[1])
                        # If row applies to today, restore active run ledger
                        if row[6] == self.ledger["day"]:
                            self.ledger["day_usd"] = float(row[2])
                            self.ledger["run_usd"] = float(row[3])
                            self.ledger["run_last"] = float(row[4])
                            self.ledger["run_n"] = int(row[5])
                        loaded_from_db = True
                        config.log.info("Loaded custom spend caps from SQLite: daily=$%.2f, run=$%.2f", self.daily_cap, self.run_cap)
            except Exception:
                pass

        # Fallback to spend_limits.json if database was empty or uninitialized
        if not loaded_from_db:
            try:
                with open(config.SPEND_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.daily_cap = float(data.get("daily_usd", self.daily_cap))
                    self.run_cap = float(data.get("run_usd", self.run_cap))
                    config.log.info("Loaded custom spend caps from JSON fallback: daily=$%.2f, run=$%.2f", self.daily_cap, self.run_cap)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                pass

    def save_limits(self, daily_usd: float, run_usd: float):
        """Persists budget caps into SQLite spend_state and updates JSON backup."""
        self.daily_cap = round(daily_usd, 2)
        self.run_cap = round(run_usd, 2)

        # 1. Update SQLite spend_state table
        try:
            import db
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_spend_state(
                self.daily_cap, self.run_cap, self.ledger["day"],
                self.ledger["day_usd"], self.ledger["run_usd"],
                self.ledger["run_last"], self.ledger["run_n"]
            ))
        except RuntimeError:
            # Synchronous execution fallback (e.g., during tests or CLI scripts)
            if os.path.exists(config.DB_PATH):
                try:
                    with sqlite3.connect(config.DB_PATH) as conn:
                        conn.execute("""
                        INSERT INTO spend_state (id, daily_cap_usd, run_cap_usd, day_usd, run_usd, run_last_ts, run_request_count, current_day)
                        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            daily_cap_usd = excluded.daily_cap_usd,
                            run_cap_usd = excluded.run_cap_usd,
                            current_day = excluded.current_day;
                        """, (
                            self.daily_cap, self.run_cap, self.ledger["day_usd"],
                            self.ledger["run_usd"], self.ledger["run_last"], self.ledger["run_n"],
                            self.ledger["day"]
                        ))
                        conn.commit()
                except Exception:
                    pass

        # 2. Maintain JSON file backup for legacy compatibility
        try:
            with open(config.SPEND_FILE, "w", encoding="utf-8") as f:
                json.dump({"daily_usd": self.daily_cap, "run_usd": self.run_cap}, f, indent=2)
            config.log.info("Persisted new spend caps: daily=$%.2f, run=$%.2f", self.daily_cap, self.run_cap)
        except OSError as e:
            config.log.warning("Could not persist spend caps to disk: %s", e)

    def _roll(self, now: float):
        """Rolls daily spend counter on day transitions and resets run accumulator on idle timeout."""
        current_day = self.get_today_str(now)
        if current_day != self.ledger["day"]:
            self.ledger.update(day=current_day, day_usd=0.0)

        if self.ledger["run_last"] and (now - self.ledger["run_last"] > config.RUN_IDLE_SECONDS):
            self.ledger.update(run_usd=0.0, run_n=0, run_last=0.0)

    def check_spend(self, now: float) -> Optional[str]:
        """Validates current spend against daily and run caps; returns error description if blocked."""
        self._roll(now)
        if self.daily_cap and self.ledger["day_usd"] >= self.daily_cap:
            return (
                f"Daily spend cap ${self.daily_cap:.2f} reached (${self.ledger['day_usd']:.2f} spent today). "
                f"Resets at midnight ({config.TZ_NOTE}), or adjust limit in dashboard."
            )

        if self.run_cap and self.ledger["run_usd"] >= self.run_cap:
            return (
                f"Run spend cap ${self.run_cap:.2f} reached (${self.ledger['run_usd']:.2f} this run, {self.ledger['run_n']} reqs). "
                f"Resets after {config.RUN_IDLE_SECONDS / 60:.0f}m idle."
            )

        self.ledger["run_last"] = now
        return None

    def record_cost(self, cost: float, now: float):
        """Records request cost into active run ledger and updates spend_state."""
        self._roll(now)
        self.ledger["day_usd"] += cost
        self.ledger["run_usd"] += cost
        self.ledger["run_n"] += 1
        self.ledger["run_last"] = now

        # Update SQLite spend_state
        try:
            import db
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_spend_state(
                self.daily_cap, self.run_cap, self.ledger["day"],
                self.ledger["day_usd"], self.ledger["run_usd"],
                self.ledger["run_last"], self.ledger["run_n"]
            ))
        except RuntimeError:
            pass

    def preload_spend(self):
        """Preloads today's accumulated spend via indexed SQL aggregation on requests table."""
        loaded = False
        if os.path.exists(config.DB_PATH):
            try:
                with sqlite3.connect(config.DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT COALESCE(SUM(cost_usd), 0.0) FROM requests WHERE day = ?;", (self.ledger["day"],))
                    row = cursor.fetchone()
                    if row and row[0] is not None:
                        self.ledger["day_usd"] = round(float(row[0]), 4)
                        loaded = True
                        config.log.info("Preloaded today's spend from SQLite: $%.4f", self.ledger["day_usd"])
            except Exception as e:
                config.log.warning("Could not preload spend from SQLite requests table: %s", e)

        # Fallback to reading legacy diagnostics files if database has no rows yet
        if not loaded:
            import analytics
            rows = analytics.read_day_rows(self.ledger["day"])
            self.ledger["day_usd"] = round(sum(float(e.get("cost") or 0.0) for e in rows), 4)
            config.log.info("Preloaded today's spend from legacy logs: $%.4f (%d requests)", self.ledger["day_usd"], len(rows))


controller = SpendController()
controller.preload_spend()