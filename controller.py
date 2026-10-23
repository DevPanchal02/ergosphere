import json
import time
from datetime import datetime
from typing import Optional
import config

class SpendController:
    def __init__(self):
        self.daily_cap = config.DEFAULT_DAILY_SPEND_LIMIT_USD
        self.run_cap = config.DEFAULT_RUN_SPEND_LIMIT_USD
        self.ledger = {
            "day": self.get_today_str(time.time()),
            "day_usd": 0.0,
            "run_usd": 0.0,
            "run_last": 0.0,
            "run_n": 0
        }
        self.load_limits()

    def get_today_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d")

    def load_limits(self):
        try:
            with open(config.SPEND_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.daily_cap = float(data.get("daily_usd", self.daily_cap))
                self.run_cap = float(data.get("run_usd", self.run_cap))
                config.log.info("Loaded custom spend caps: daily=$%.2f, run=$%.2f", self.daily_cap, self.run_cap)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def save_limits(self, daily_usd: float, run_usd: float):
        self.daily_cap = round(daily_usd, 2)
        self.run_cap = round(run_usd, 2)
        try:
            with open(config.SPEND_FILE, "w", encoding="utf-8") as f:
                json.dump({"daily_usd": self.daily_cap, "run_usd": self.run_cap}, f, indent=2)
            config.log.info("Persisted new spend caps: daily=$%.2f, run=$%.2f", self.daily_cap, self.run_cap)
        except OSError as e:
            config.log.warning("Could not persist spend caps to disk: %s", e)

    def _roll(self, now: float):
        current_day = self.get_today_str(now)
        if current_day != self.ledger["day"]:
            self.ledger.update(day=current_day, day_usd=0.0)

        if self.ledger["run_last"] and (now - self.ledger["run_last"] > config.RUN_IDLE_SECONDS):
            self.ledger.update(run_usd=0.0, run_n=0, run_last=0.0)

    def check_spend(self, now: float) -> Optional[str]:
        self._roll(now)
        if self.daily_cap and self.ledger["day_usd"] >= self.daily_cap:
            return (f"Daily spend cap ${self.daily_cap:.2f} reached (${self.ledger['day_usd']:.2f} spent today). "
                    f"Resets at midnight ({config.TZ_NOTE}), or adjust limit in dashboard.")

        if self.run_cap and self.ledger["run_usd"] >= self.run_cap:
            return (f"Run spend cap ${self.run_cap:.2f} reached (${self.ledger['run_usd']:.2f} this run, {self.ledger['run_n']} reqs). "
                    f"Resets after {config.RUN_IDLE_SECONDS / 60:.0f}m idle.")

        self.ledger["run_last"] = now
        return None

    def record_cost(self, cost: float, now: float):
        self._roll(now)
        self.ledger["day_usd"] += cost
        self.ledger["run_usd"] += cost
        self.ledger["run_n"] += 1
        self.ledger["run_last"] = now

    def preload_spend(self):
        import analytics
        rows = analytics.read_day_rows(self.ledger["day"])
        self.ledger["day_usd"] = round(sum(float(e.get("cost") or 0.0) for e in rows), 4)
        config.log.info("Preloaded today's spend from disk: $%.4f (%d requests found)", self.ledger["day_usd"], len(rows))

controller = SpendController()
controller.preload_spend()