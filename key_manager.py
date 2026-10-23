import time
import asyncio
from collections import deque
from datetime import datetime
from typing import Tuple, Union, Optional
import config

class GeminiKeyManager:
    def __init__(self):
        self.keys = config.GEMINI_KEYS
        self.num_keys = len(self.keys)
        self.lock = asyncio.Lock()
        
        # Instance limits allow live updates from the dashboard without restarts
        self.rpm_limit = config.GEMINI_RPM_LIMIT
        self.tpm_limit = config.GEMINI_TPM_LIMIT
        self.rpd_limit = config.GEMINI_RPD_LIMIT
        self.max_parallel = int(config.os.getenv("GEMINI_PARALLEL", "0"))
        
        # Operational state per key
        self.last_used = [0.0] * self.num_keys
        self.tokens_history = [deque() for _ in range(self.num_keys)]
        self.inflight_tokens = [0] * self.num_keys
        self.rpd_count = [0] * self.num_keys
        self.quarantined = [False] * self.num_keys
        self.quarantine_reason = [""] * self.num_keys
        
        self.current_day = self._get_day_str(time.time())
        self.label_to_pos = {f"G{n}": i for i, (n, _) in enumerate(self.keys)}

    def _get_day_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d")

    # Translates string labels or integer indexes into a valid slot index
    def _resolve_pos(self, key_ref: Union[int, str]) -> Optional[int]:
        if isinstance(key_ref, int):
            return key_ref if 0 <= key_ref < self.num_keys else None
        return self.label_to_pos.get(key_ref)

    # Daily quota reset at local midnight
    def _check_day_rollover(self, now: float):
        today = self._get_day_str(now)
        if today != self.current_day:
            self.current_day = today
            self.rpd_count = [0] * self.num_keys
            config.log.info("Reset daily Gemini key request counters for %s", today)

    # Discards timestamps older than the 60s window
    def _clean_tokens(self, pos: int, now: float):
        dq = self.tokens_history[pos]
        cutoff = now - config.TPM_WINDOW_SECONDS
        while dq and dq[0][0] <= cutoff:
            dq.popleft()

    def get_tpm_usage(self, pos: int, now: float) -> int:
        self._clean_tokens(pos, now)
        return sum(tok for _, tok in self.tokens_history[pos]) + self.inflight_tokens[pos]

    # Calculates required sleep time until enough tokens roll out of the sliding window
    def get_tpm_wait(self, pos: int, now: float, needed_tokens: int) -> float:
        self._clean_tokens(pos, now)
        used = sum(tok for _, tok in self.tokens_history[pos]) + self.inflight_tokens[pos]

        if self.tpm_limit <= 0 or used + needed_tokens <= self.tpm_limit or used == 0:
            return 0.0

        needed_freed = (used + needed_tokens) - self.tpm_limit
        freed = 0
        for ts, count in self.tokens_history[pos]:
            freed += count
            if freed >= needed_freed:
                return max(0.0, (ts + config.TPM_WINDOW_SECONDS) - now)

        return config.TPM_WINDOW_SECONDS

    # Minimum spacing between calls on the same key based on RPM
    def get_rpm_wait(self, pos: int, now: float) -> float:
        if self.rpm_limit <= 0:
            return 0.0
        min_interval = 60.0 / self.rpm_limit
        return max(0.0, (self.last_used[pos] + min_interval) - now)

    # Total wait penalty across RPM and TPM limits
    def get_key_penalty(self, pos: int, now: float, needed_tokens: int) -> float:
        if self.quarantined[pos] or (self.rpd_limit > 0 and self.rpd_count[pos] >= self.rpd_limit):
            return 999999.0
        
        return max(self.get_rpm_wait(pos, now), self.get_tpm_wait(pos, now, needed_tokens))

    # Water-level balancer selecting ready keys with lowest daily utilization
    async def acquire_key(self, estimated_tokens: int = 0) -> Tuple[str, str, float, bool]:
        if not self.keys:
            raise RuntimeError("No Gemini keys configured in environment.")

        async with self.lock:
            now = time.time()
            self._check_day_rollover(now)

            ready_candidates = [
                i for i in range(self.num_keys)
                if self.get_key_penalty(i, now, estimated_tokens) <= 0.0
            ]

            if ready_candidates:
                chosen_pos = min(ready_candidates, key=lambda i: self.rpd_count[i])
                wait_time = 0.0
            else:
                all_penalties = [
                    (self.get_key_penalty(i, now, estimated_tokens), self.rpd_count[i], i)
                    for i in range(self.num_keys)
                ]
                all_penalties.sort()
                wait_time, _, chosen_pos = all_penalties[0]

            # Trigger 3.1 Flash-Lite fallback if the queue wait is excessive or the key hit its day limit
            use_fallback = wait_time > 15.0 or (self.rpd_limit > 0 and self.rpd_count[chosen_pos] >= self.rpd_limit)

            start_at = now + wait_time
            self.last_used[chosen_pos] = start_at
            self.rpd_count[chosen_pos] += 1
            if estimated_tokens > 0:
                self.inflight_tokens[chosen_pos] += estimated_tokens

        if wait_time > 0 and not use_fallback:
            await asyncio.sleep(wait_time)

        n, key = self.keys[chosen_pos]
        return f"G{n}", key, max(wait_time, 0.0), use_fallback

    # Commits real token counts once the response stream finishes
    def record_usage(self, key_ref: Union[int, str], reserved_tokens: int, actual_tokens: int):
        pos = self._resolve_pos(key_ref)
        if pos is not None:
            now = time.time()
            self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)
            if actual_tokens > 0:
                self.tokens_history[pos].append((now, actual_tokens))

    # Releases placeholder token allocations on failed or cancelled calls
    def release_reservation(self, key_ref: Union[int, str], reserved_tokens: int):
        pos = self._resolve_pos(key_ref)
        if pos is not None:
            self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)

    # Disables revoked or failing keys from rotation
    def quarantine_key(self, key_ref: Union[int, str], reason: str):
        pos = self._resolve_pos(key_ref)
        if pos is not None and not self.quarantined[pos]:
            self.quarantined[pos] = True
            self.quarantine_reason[pos] = reason
            n, _ = self.keys[pos]
            config.log.error("CRITICAL: Gemini Key G%d quarantined. Reason: %s", n, reason)

    # Dashboard endpoint to tune rate limits dynamically
    def update_limits(self, rpm: Optional[int] = None, tpm: Optional[int] = None, rpd: Optional[int] = None, max_parallel: Optional[int] = None):
        if rpm is not None:
            self.rpm_limit = max(1, rpm)
        if tpm is not None:
            self.tpm_limit = max(0, tpm)
        if rpd is not None:
            self.rpd_limit = max(0, rpd)
        if max_parallel is not None:
            self.max_parallel = max(0, max_parallel)
        config.log.info("Updated rate limits: RPM=%d, TPM=%d, RPD=%d, Parallel=%d", self.rpm_limit, self.tpm_limit, self.rpd_limit, self.max_parallel)

    # Dashboard metrics payload
    def get_pool_status(self, now: float) -> dict:
        total_tpm_used = sum(self.get_tpm_usage(i, now) for i in range(self.num_keys))
        max_pool_tpm = self.num_keys * self.tpm_limit
        max_pool_rpm = self.num_keys * self.rpm_limit
        healthy_keys = sum(1 for q in self.quarantined if not q)

        return {
            "healthy_keys": f"{healthy_keys}/{self.num_keys}",
            "pool_tpm_available": max(0, max_pool_tpm - total_tpm_used),
            "pool_tpm_max": max_pool_tpm,
            "pool_rpm_max": max_pool_rpm,
            "limits": {
                "rpm": self.rpm_limit,
                "tpm": self.tpm_limit,
                "rpd": self.rpd_limit,
                "max_parallel": self.max_parallel
            },
            "keys": [
                {
                    "label": f"G{n}",
                    "ready_in_s": round(self.get_key_penalty(i, now, 0), 1),
                    "tpm_used": self.get_tpm_usage(i, now),
                    "rpd_used": self.rpd_count[i],
                    "quarantined": self.quarantined[i]
                }
                for i, (n, _) in enumerate(self.keys)
            ]
        }

key_manager = GeminiKeyManager()