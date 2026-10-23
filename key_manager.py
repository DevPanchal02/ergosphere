import time
import json
import asyncio
from collections import deque
from datetime import datetime
from typing import Tuple, Union, Optional
import config

class GeminiKeyManager:
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

    def _update_gate_semaphore(self):
        if self.max_parallel > 0:
            self._gate_semaphore = asyncio.Semaphore(self.max_parallel)
        else:
            self._gate_semaphore = None

    def _get_day_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d")

    def _resolve_pos(self, key_ref: Union[int, str]) -> Optional[int]:
        if isinstance(key_ref, int):
            return key_ref if 0 <= key_ref < self.num_keys else None
        return self.label_to_pos.get(key_ref)

    def _check_day_rollover(self, now: float):
        today = self._get_day_str(now)
        if today != self.current_day:
            self.current_day = today
            self.rpd_count = [0] * self.num_keys
            self.error_count = [0] * self.num_keys
            config.log.info("Reset daily Gemini key request and error counters for %s", today)

    def _clean_tokens(self, pos: int, now: float):
        if pos >= len(self.tokens_history):
            return
        dq = self.tokens_history[pos]
        cutoff = now - config.TPM_WINDOW_SECONDS
        while dq and dq[0][0] <= cutoff:
            dq.popleft()

    def get_key_actual_rpm(self, pos: int, now: float) -> int:
        if pos >= len(self.key_requests):
            return 0
        dq = self.key_requests[pos]
        cutoff = now - 60.0
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return len(dq)

    def get_tpm_usage(self, pos: int, now: float) -> int:
        if pos >= len(self.tokens_history) or pos >= len(self.inflight_tokens):
            return 0
        self._clean_tokens(pos, now)
        return sum(tok for _, tok in self.tokens_history[pos]) + self.inflight_tokens[pos]

    def get_tpm_wait(self, pos: int, now: float, needed_tokens: int) -> float:
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
        if self.rpm_limit <= 0 or pos >= len(self.last_used):
            return 0.0
        min_interval = 60.0 / self.rpm_limit
        return max(0.0, (self.last_used[pos] + min_interval) - now)

    def get_key_penalty(self, pos: int, now: float, needed_tokens: int) -> float:
        if pos >= self.num_keys or self.quarantined[pos]:
            return 999999.0
        if self.rpd_limit > 0 and self.rpd_count[pos] >= self.rpd_limit:
            return 888888.0
        return max(self.get_rpm_wait(pos, now), self.get_tpm_wait(pos, now, needed_tokens))

    async def acquire_gate(self):
        if self._gate_semaphore:
            await self._gate_semaphore.acquire()
        self._inflight_requests += 1

    def release_gate(self):
        self._inflight_requests = max(0, self._inflight_requests - 1)
        if self._gate_semaphore:
            self._gate_semaphore.release()

    async def acquire_key(self, estimated_tokens: int = 0, excluded_keys: Optional[set] = None) -> Tuple[str, str, float, bool]:
        if not self.keys:
            raise RuntimeError("No Gemini keys configured in environment.")

        excluded = excluded_keys or set()
        async with self.lock:
            now = time.time()
            self._check_day_rollover(now)

            available_indices = [
                i for i in range(self.num_keys)
                if not self.quarantined[i] and f"G{self.keys[i][0]}" not in excluded
            ]

            if not available_indices:
                raise RuntimeError("All configured Gemini keys are quarantined or exhausted.")

            ready_candidates = [
                i for i in available_indices
                if self.get_key_penalty(i, now, estimated_tokens) <= 0.0
            ]

            if ready_candidates:
                chosen_pos = min(ready_candidates, key=lambda i: self.rpd_count[i])
                wait_time = 0.0
            else:
                all_penalties = [
                    (self.get_key_penalty(i, now, estimated_tokens), self.rpd_count[i], i)
                    for i in available_indices
                ]
                all_penalties.sort()
                wait_time, _, chosen_pos = all_penalties[0]

            use_fallback = (
                self.key_fallback[chosen_pos] or
                wait_time > 15.0 or
                (self.rpd_limit > 0 and self.rpd_count[chosen_pos] >= self.rpd_limit)
            )

            start_at = now + wait_time
            self.last_used[chosen_pos] = start_at
            self.rpd_count[chosen_pos] += 1
            self.key_requests[chosen_pos].append(now)
            if estimated_tokens > 0:
                self.inflight_tokens[chosen_pos] += estimated_tokens

        if wait_time > 0 and not use_fallback:
            await asyncio.sleep(wait_time)

        n, key = self.keys[chosen_pos]
        return f"G{n}", key, max(wait_time, 0.0), use_fallback

    def record_usage(self, key_ref: Union[int, str], reserved_tokens: int, actual_tokens: int):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            now = time.time()
            self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)
            if actual_tokens > 0:
                self.tokens_history[pos].append((now, actual_tokens))

    def release_reservation(self, key_ref: Union[int, str], reserved_tokens: int):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            self.inflight_tokens[pos] = max(0, self.inflight_tokens[pos] - reserved_tokens)

    def record_error(self, key_ref: Union[int, str]):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            self.error_count[pos] += 1

    def quarantine_key(self, key_ref: Union[int, str], reason: str):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            self.quarantined[pos] = True
            self.quarantine_reason[pos] = reason
            n, _ = self.keys[pos]
            config.log.error("Gemini Key G%d QUARANTINED. Reason: %s", n, reason)

    def unquarantine_key(self, key_ref: Union[int, str]):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            self.quarantined[pos] = False
            self.quarantine_reason[pos] = ""
            n, _ = self.keys[pos]
            config.log.info("Gemini Key G%d unquarantined and restored to service.", n)

    def set_key_fallback(self, key_ref: Union[int, str], enable: bool):
        pos = self._resolve_pos(key_ref)
        if pos is not None and pos < self.num_keys:
            self.key_fallback[pos] = bool(enable)
            self.save_settings()

    def update_limits(self, rpm: Optional[int] = None, tpm: Optional[int] = None,
                      rpd: Optional[int] = None, max_parallel: Optional[int] = None):
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
        total_tpm_used = sum(self.get_tpm_usage(i, now) for i in range(self.num_keys))
        max_pool_tpm = self.num_keys * self.tpm_limit
        max_pool_rpm = self.num_keys * self.rpm_limit
        healthy_keys = sum(1 for q in self.quarantined if not q)

        keys_info = []
        next_pick = None
        lowest_penalty = float("inf")

        for i, (n, _) in enumerate(self.keys):
            cd_wait = self.get_rpm_wait(i, now)
            tpm_wait = self.get_tpm_wait(i, now, 0)
            tpm_used = self.get_tpm_usage(i, now)
            actual_rpm = self.get_key_actual_rpm(i, now)
            penalty = self.get_key_penalty(i, now, 0)

            if not self.quarantined[i] and penalty < lowest_penalty:
                lowest_penalty = penalty
                next_pick = f"G{n}"

            if self.quarantined[i]:
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
                "quarantined": self.quarantined[i],
                "fallback": self.key_fallback[i],
            })

        return {
            "healthy_keys": f"{healthy_keys}/{self.num_keys}",
            "healthy_count": healthy_keys,
            "pool_tpm_available": max(0, max_pool_tpm - total_tpm_used),
            "pool_tpm_max": max_pool_tpm,
            "pool_rpm_max": max_pool_rpm,
            "inflight_requests": self._inflight_requests,
            "balancer": {
                "algorithm": "Wait = max(RPM Cooldown, TPM Window) → Tiebreaker: Lowest RPD",
                "next_key": next_pick or "None",
                "next_wait": "READY" if lowest_penalty <= 0.05 else f"{lowest_penalty:.1f}s",
            },
            "limits": {
                "rpm": self.rpm_limit,
                "tpm": self.tpm_limit,
                "rpd": self.rpd_limit,
                "max_parallel": self.max_parallel
            },
            "keys": keys_info
        }

key_manager = GeminiKeyManager()