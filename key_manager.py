import time
import json
import asyncio
from collections import deque
from datetime import datetime
from typing import Tuple, Union, Optional, List, Dict
import config


class GeminiKeyManager:
    """
    Manages key rotation, RPM pacing, TPM sliding windows, and persistent fleet state
    for Google Gemini API keys across system restarts.
    """

    def __init__(self):
        self.rpm_limit = config.GEMINI_RPM_LIMIT
        self.tpm_limit = config.GEMINI_TPM_LIMIT
        self.rpd_limit = config.GEMINI_RPD_LIMIT
        self.max_parallel = config.GEMINI_PARALLEL

        self.lock = asyncio.Lock()
        self._gate_semaphore: Optional[asyncio.Semaphore] = None
        self._inflight_requests = 0
        self.current_day = self._get_day_str(time.time())

        self.set_keys(config.GEMINI_KEYS)
        self._update_gate_semaphore()
        self.load_settings()

    def set_keys(self, keys: list):
        """Initializes internal key rotation lists and position lookup maps."""
        self.keys = list(keys)
        self.num_keys = len(self.keys)
        self.last_used = [0.0] * self.num_keys
        self.key_requests = [deque() for _ in range(self.num_keys)]  # Rolling timestamps for RPM
        self.tokens_history = [deque() for _ in range(self.num_keys)]
        self.inflight_tokens = [0] * self.num_keys
        self.rpd_count = [0] * self.num_keys
        self.error_count = [0] * self.num_keys
        self.quarantined = [False] * self.num_keys
        self.quarantine_reason = [""] * self.num_keys
        self.key_fallback = [False] * self.num_keys
        self.label_to_pos = {f"G{n}": i for i, (n, _) in enumerate(self.keys)}

    async def init_fleet_state(self):
        """Restores key quarantine flags, daily RPD usage, and error counters from SQLite."""
        try:
            import db
            state = await db.load_fleet_state(self.current_day)
            for key_label, info in state.items():
                pos = self._resolve_pos(key_label)
                if pos is not None and pos < self.num_keys:
                    self.quarantined[pos] = info.get("quarantined", False)
                    self.quarantine_reason[pos] = info.get("quarantine_reason", "")
                    self.rpd_count[pos] = info.get("rpd_count", 0)
                    self.error_count[pos] = info.get("error_count", 0)
            config.log.info("Restored fleet state from SQLite for %s (%d keys found)", self.current_day, len(state))
        except Exception as e:
            config.log.warning("Could not restore fleet state from database: %s", e)

    def _update_gate_semaphore(self):
        """Constructs or resets the concurrency gate semaphore based on configured limits."""
        if self.max_parallel > 0:
            self._gate_semaphore = asyncio.Semaphore(self.max_parallel)
        else:
            self._gate_semaphore = None

    def _get_day_str(self, ts: float) -> str:
        """Formats timestamp into YYYY-MM-DD string in the configured local timezone."""
        return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d")

    def _resolve_pos(self, key_ref: Union[int, str]) -> Optional[int]:
        """Maps key label (e.g. 'G2') or integer index to internal list position."""
        if isinstance(key_ref, int):
            return key_ref if 0 <= key_ref < self.num_keys else None
        return self.label_to_pos.get(key_ref)

    def _check_day_rollover(self, now: float):
        """Resets daily usage and error counters when local date crosses midnight."""
        today = self._get_day_str(now)
        if today != self.current_day:
            self.current_day = today
            self.rpd_count = [0] * self.num_keys
            self.error_count = [0] * self.num_keys
            config.log.info("Reset daily Gemini key request and error counters for %s", today)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.init_fleet_state())
            except RuntimeError:
                pass

    def _clean_tokens(self, pos: int, now: float):
        """Purges token usage timestamps older than the 60-second sliding TPM window."""
        if pos >= len(self.tokens_history):
            return
        dq = self.tokens_history[pos]
        cutoff = now - config.TPM_WINDOW_SECONDS
        while dq and dq[0][0] <= cutoff:
            dq.popleft()

    def get_key_actual_rpm(self, pos: int, now: float) -> int:
        """Calculates rolling 60-second RPM rate for a specific key."""
        if pos >= len(self.key_requests):
            return 0
        dq = self.key_requests[pos]
        cutoff = now - 60.0
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return len(dq)

    def get_tpm_usage(self, pos: int, now: float) -> int:
        """Returns total active tokens within sliding window including reserved in-flight tokens."""
        if pos >= len(self.tokens_history) or pos >= len(self.inflight_tokens):
            return 0
        self._clean_tokens(pos, now)
        return sum(tok for _, tok in self.tokens_history[pos]) + self.inflight_tokens[pos]

    def get_tpm_wait(self, pos: int, now: float, needed_tokens: int) -> float:
        """Computes seconds to wait until enough TPM capacity frees up for the incoming call."""
        self._clean_tokens(pos, now)
        used = self.get_tpm_usage(pos, now)
        if self.tpm_limit <= 0 or used + needed_tokens <= self.tpm_limit or used == 0:
            return 0.0

        needed_freed = (used + needed_tokens) - self.tpm_limit
        freed = 0
        for ts, count in self.tokens_history[pos]:
            freed += count
            if freed >= needed_freed:
                return max(0.0, (ts + config.TPM_WINDOW_SECONDS) - now)

        return config.TPM_WINDOW_SECONDS

    def get_rpm_wait(self, pos: int, now: float) -> float:
        """Calculates pacing cooldown required to strictly obey requests-per-minute limit."""
        if self.rpm_limit <= 0 or pos >= len(self.last_used):
            return 0.0
        min_interval = 60.0 / self.rpm_limit
        return max(0.0, (self.last_used[pos] + min_interval) - now)

    def get_key_penalty(self, pos: int, now: float, needed_tokens: int) -> float:
        """Ranks key readiness; quarantined or exhausted keys receive extreme penalties."""
        if pos >= self.num_keys or (self.quarantined[pos] if pos < len(self.quarantined) else False):
            return 999999.0
        if self.rpd_limit > 0 and (self.rpd_count[pos] if pos < len(self.rpd_count) else 0) >= self.rpd_limit:
            return 888888.0
        return max(self.get_rpm_wait(pos, now), self.get_tpm_wait(pos, now, needed_tokens))

    async def acquire_gate(self):
        """Limits system-wide concurrent requests when concurrency gate is configured."""
        if self._gate_semaphore:
            await self._gate_semaphore.acquire()
        self._inflight_requests += 1

    def release_gate(self):
        """Releases concurrency gate slot upon request completion or abort."""
        self._inflight_requests = max(0, self._inflight_requests - 1)
        if self._gate_semaphore:
            self._gate_semaphore.release()

    async def acquire_key(self, estimated_tokens: int = 0, excluded_keys: Optional[set] = None) -> Tuple[str, str, float, bool]:
        """
        Selects optimal healthy key via wait penalty minimization and least-daily-usage tiebreaking.
        Returns: (key_label, raw_api_key, pacing_wait_s, use_fallback_model)
        """
        if not self.keys:
            raise RuntimeError("No Gemini keys configured in environment.")

        excluded = excluded_keys or set()
        async with self.lock:
            now = time.time()
            self._check_day_rollover(now)

            available_indices = [
                i for i in range(self.num_keys)
                if not (self.quarantined[i] if i < len(self.quarantined) else False)
                and f"G{self.keys[i][0]}" not in excluded
            ]

            if not available_indices:
                raise RuntimeError("All configured Gemini keys are quarantined or exhausted.")

            ready_candidates = [
                i for i in available_indices
                if self.get_key_penalty(i, now, estimated_tokens) <= 0.0
            ]

            if ready_candidates:
                # Lowest daily calls -> Least recently used
                chosen_pos = min(
                    ready_candidates,
                    key=lambda i: (
                        self.rpd_count[i] if i < len(self.rpd_count) else 0,
                        self.last_used[i] if i < len(self.last_used) else 0.0
                    )
                )
                wait_time = 0.0
            else:
                all_penalties = [
                    (
                        self.get_key_penalty(i, now, estimated_tokens),
                        self.rpd_count[i] if i < len(self.rpd_count) else 0,
                        self.last_used[i] if i < len(self.last_used) else 0.0,
                        i
                    )
                    for i in available_indices
                ]
                all_penalties.sort()
                wait_time, _, _, chosen_pos = all_penalties[0]

            is_congested = (
                wait_time > 15.0 or
                (self.rpd_limit > 0 and (self.rpd_count[chosen_pos] if chosen_pos < len(self.rpd_count) else 0) >= self.rpd_limit)
            )
            has_fallback = self.key_fallback[chosen_pos] if chosen_pos < len(self.key_fallback) else False
            use_fallback = bool(has_fallback or is_congested)

            start_at = now + wait_time
            if chosen_pos < len(self.last_used):
                self.last_used[chosen_pos] = start_at
            if chosen_pos < len(self.rpd_count):
                self.rpd_count[chosen_pos] += 1
            if chosen_pos < len(self.key_requests):
                self.key_requests[chosen_pos].append(start_at)
            if estimated_tokens > 0 and chosen_pos < len(self.inflight_tokens):
                self.inflight_tokens[chosen_pos] += estimated_tokens

            # Asynchronously persist updated usage to fleet_state
            n, key = self.keys[chosen_pos]
            key_label = f"G{n}"
            rpd_val = self.rpd_count[chosen_pos] if chosen_pos < len(self.rpd_count) else 0
            err_val = self.error_count[chosen_pos] if chosen_pos < len(self.error_count) else 0
            self._persist_key_usage(key_label, rpd_val, err_val)

        if wait_time > 0 and not is_congested:
            await asyncio.sleep(wait_time)

        return key_label, key, max(wait_time, 0.0), use_fallback

    def record_usage(self, key_ref: Union[int, str], reserved_tokens: int, actual_tokens: int):
        """Deducts in-flight token reservation and logs actual token throughput into sliding history."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            now = time.time()
            if pos < len(self.inflight_tokens):
                self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)
            if actual_tokens > 0 and pos < len(self.tokens_history):
                self.tokens_history[pos].append((now, actual_tokens))
            n, _ = self.keys[pos]
            rpd_val = self.rpd_count[pos] if pos < len(self.rpd_count) else 0
            err_val = self.error_count[pos] if pos < len(self.error_count) else 0
            self._persist_key_usage(f"G{n}", rpd_val, err_val)

    def release_reservation(self, key_ref: Union[int, str], reserved_tokens: int):
        """Cleans up in-flight token reservation on request failure or cancellation."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            if pos < len(self.inflight_tokens):
                self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)

    def record_error(self, key_ref: Union[int, str]):
        """Increments error counter and synchronizes state to SQLite."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            if pos < len(self.error_count):
                self.error_count[pos] += 1
            n, _ = self.keys[pos]
            rpd_val = self.rpd_count[pos] if pos < len(self.rpd_count) else 0
            err_val = self.error_count[pos] if pos < len(self.error_count) else 0
            self._persist_key_usage(f"G{n}", rpd_val, err_val)

    def quarantine_key(self, key_ref: Union[int, str], reason: str):
        """Disables dead or revoked key in memory and persists status directly to fleet_state."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            if pos < len(self.quarantined):
                self.quarantined[pos] = True
            if pos < len(self.quarantine_reason):
                self.quarantine_reason[pos] = reason
            n, _ = self.keys[pos]
            key_label = f"G{n}"
            config.log.error("Gemini Key %s QUARANTINED. Reason: %s", key_label, reason)
            self._persist_key_quarantine(key_label, True, reason)

    def unquarantine_key(self, key_ref: Union[int, str]):
        """Restores quarantined key back to service in memory and database."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            if pos < len(self.quarantined):
                self.quarantined[pos] = False
            if pos < len(self.quarantine_reason):
                self.quarantine_reason[pos] = ""
            n, _ = self.keys[pos]
            key_label = f"G{n}"
            config.log.info("Gemini Key %s unquarantined and restored to service.", key_label)
            self._persist_key_quarantine(key_label, False, "")

    def _persist_key_quarantine(self, key_label: str, quarantined: bool, reason: str):
        """Dispatches non-blocking database task to persist quarantine changes."""
        try:
            import db
            loop = asyncio.get_running_loop()
            loop.create_task(db.save_key_quarantine(key_label, quarantined, reason, self.current_day))
        except RuntimeError:
            pass

    def _persist_key_usage(self, key_label: str, rpd: int, errs: int):
        """Dispatches non-blocking database task to persist key usage counters."""
        try:
            import db
            loop = asyncio.get_running_loop()
            loop.create_task(db.update_key_usage(key_label, rpd, errs, self.current_day))
        except RuntimeError:
            pass

    def set_key_fallback(self, key_ref: Union[int, str], enable: bool):
        """Forces or disables Gemini 3.1 fallback routing on a per-key basis."""
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            while len(self.key_fallback) <= pos:
                self.key_fallback.append(False)
            self.key_fallback[pos] = bool(enable)
            self.save_settings()

    def update_limits(self, rpm: Optional[int] = None, tpm: Optional[int] = None,
                      rpd: Optional[int] = None, max_parallel: Optional[int] = None):
        """Updates runtime pacing and concurrency thresholds."""
        if rpm is not None:
            self.rpm_limit = max(1, int(rpm))
        if tpm is not None:
            self.tpm_limit = max(0, int(tpm))
        if rpd is not None:
            self.rpd_limit = max(0, int(rpd))
        if max_parallel is not None:
            self.max_parallel = max(0, int(max_parallel))
            self._update_gate_semaphore()
        self.save_settings()

    def save_settings(self):
        """Persists runtime limits to JSON file for fallback durability."""
        try:
            payload = {
                "rpm": self.rpm_limit,
                "tpm": self.tpm_limit,
                "rpd": self.rpd_limit,
                "max_parallel": self.max_parallel,
                "key_fallback": self.key_fallback,
            }
            with open(config.RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except OSError as e:
            config.log.warning("Could not persist runtime settings: %s", e)

    def load_settings(self):
        """Loads runtime limits from local configuration file if present."""
        try:
            with open(config.RUNTIME_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.rpm_limit = int(data.get("rpm", self.rpm_limit))
                self.tpm_limit = int(data.get("tpm", self.tpm_limit))
                self.rpd_limit = int(data.get("rpd", self.rpd_limit))
                self.max_parallel = int(data.get("max_parallel", self.max_parallel))
                fb = data.get("key_fallback")
                if isinstance(fb, list) and len(fb) == self.num_keys:
                    self.key_fallback = [bool(x) for x in fb]
                self._update_gate_semaphore()
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass

    def get_pool_status(self, now: float) -> dict:
        """Assembles comprehensive pool metrics, balancer targets, and per-key health states."""
        total_tpm_used = sum(self.get_tpm_usage(i, now) for i in range(self.num_keys))
        max_pool_tpm = self.num_keys * self.tpm_limit
        max_pool_rpm = self.num_keys * self.rpm_limit
        healthy_keys = sum(1 for q in self.quarantined if not q)

        keys_info = []
        available_indices = [
            i for i in range(self.num_keys)
            if not (self.quarantined[i] if i < len(self.quarantined) else False)
        ]
        ready_candidates = [i for i in available_indices if self.get_key_penalty(i, now, 0) <= 0.0]

        if ready_candidates:
            best_idx = min(
                ready_candidates,
                key=lambda i: (
                    self.rpd_count[i] if i < len(self.rpd_count) else 0,
                    self.last_used[i] if i < len(self.last_used) else 0.0
                )
            )
            next_pick = f"G{self.keys[best_idx][0]}"
            lowest_penalty = 0.0
        elif available_indices:
            all_pens = [
                (
                    self.get_key_penalty(i, now, 0),
                    self.rpd_count[i] if i < len(self.rpd_count) else 0,
                    self.last_used[i] if i < len(self.last_used) else 0.0,
                    i
                )
                for i in available_indices
            ]
            all_pens.sort()
            lowest_penalty, _, _, best_idx = all_pens[0]
            next_pick = f"G{self.keys[best_idx][0]}"
        else:
            next_pick = "None"
            lowest_penalty = 0.0

        for i, (n, _) in enumerate(self.keys):
            cd_wait = self.get_rpm_wait(i, now)
            tpm_wait = self.get_tpm_wait(i, now, 0)
            tpm_used = self.get_tpm_usage(i, now)
            actual_rpm = self.get_key_actual_rpm(i, now)
            penalty = self.get_key_penalty(i, now, 0)
            is_quarantined = self.quarantined[i] if i < len(self.quarantined) else False
            is_fb = self.key_fallback[i] if i < len(self.key_fallback) else False

            if is_quarantined:
                st_text = "DEAD"
                st_type = "err"
            elif tpm_wait > 0:
                st_text = f"TPM {tpm_wait:.0f}s"
                st_type = "warn"
            elif cd_wait > 0:
                st_text = f"{cd_wait:.1f}s"
                st_type = "busy"
            else:
                st_text = "READY"
                st_type = "ready"

            keys_info.append({
                "label": f"G{n}",
                "status_text": st_text,
                "status_type": st_type,
                "wait_s": 0.0 if penalty <= 0.05 else round(penalty, 1),
                "rpm_used": actual_rpm,
                "rpm_limit": self.rpm_limit,
                "rpm_wait": round(cd_wait, 1),
                "tpm_used": tpm_used,
                "tpm_limit": self.tpm_limit,
                "tpm_pct": min(100, int((tpm_used / max(1, self.tpm_limit)) * 100)),
                "rpd_used": self.rpd_count[i] if i < len(self.rpd_count) else 0,
                "errs": self.error_count[i] if i < len(self.error_count) else 0,
                "quarantined": is_quarantined,
                "fallback": is_fb,
            })

        return {
            "healthy_keys": f"{healthy_keys}/{self.num_keys}",
            "healthy_count": healthy_keys,
            "pool_tpm_available": max(0, max_pool_tpm - total_tpm_used),
            "pool_tpm_max": max_pool_tpm,
            "pool_rpm_max": max_pool_rpm,
            "inflight_requests": self._inflight_requests,
            "balancer": {
                "algorithm": "Wait = max(RPM Cooldown, TPM Window) -> Tiebreaker: Lowest RPD",
                "next_key": next_pick or "None",
                "next_wait": "READY" if lowest_penalty <= 0.05 else f"{lowest_penalty:.1f}s",
            },
            "limits": {
                "rpm": self.rpm_limit,
                "tpm": self.tpm_limit,
                "rpd": self.rpd_limit,
                "max_parallel": self.max_parallel,
            },
            "keys": keys_info,
        }


key_manager = GeminiKeyManager()