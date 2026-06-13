"""Unified LLM client over free-tier Gemini + Groq.

Design goals for a 24/7 free-tier system:
  * Rate-limit aware: a token-bucket per provider keeps us under free RPM caps.
  * Resilient: failover from primary -> secondary provider on error/quota.
  * Cheap: on-disk cache keyed by (model, prompt) with TTL, so re-runs of the
    same agent within a cycle don't burn quota.
  * JSON-first: `complete_json` enforces and parses structured output, which is
    what every agent needs.
  * Degrades gracefully: if no key is configured, returns None and callers fall
    back to their quant-only path.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR, get_settings
from ..logging_conf import get_logger

log = get_logger("llm.client")


class _RateLimiter:
    """Simple sliding-window limiter: at most `rpm` calls per 60s."""

    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, rpm)
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.time()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.rpm:
                sleep_for = 60 - (now - self.calls[0]) + 0.05
                log.debug("Rate limit hit; sleeping %.1fs", sleep_for)
                time.sleep(max(0.0, sleep_for))
            self.calls.append(time.time())


class LLMClient:
    def __init__(self) -> None:
        s = get_settings()
        self.s = s
        self.cache_dir: Path = CACHE_DIR / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl = float(s.get("llm", "cache_ttl_hours", default=12)) * 3600

        self._gemini = None
        self._groq = None
        self._cooldown: dict[str, float] = {}  # provider -> epoch until usable
        self._gemini_rl = _RateLimiter(int(s.get("llm", "gemini_rpm", default=14)))
        self._groq_rl = _RateLimiter(int(s.get("llm", "groq_rpm", default=28)))
        self.gemini_model = s.get("llm", "default_model_gemini", default="gemini-2.0-flash")
        self.groq_model = s.get("llm", "default_model_groq", default="llama-3.3-70b-versatile")

        # provider order
        primary = s.llm_primary
        order = ["gemini", "groq"] if primary == "gemini" else ["groq", "gemini"]
        self.providers = [p for p in order if getattr(s, f"{p}_api_key")]

    @property
    def available(self) -> bool:
        """True only if at least one provider is configured AND not cooling down."""
        return any(not self._in_cooldown(p) for p in self.providers)

    # -- provider lazy init -----------------------------------------
    def _ensure_gemini(self):
        if self._gemini is None:
            from google import genai

            self._gemini = genai.Client(api_key=self.s.gemini_api_key)
        return self._gemini

    def _ensure_groq(self):
        if self._groq is None:
            from groq import Groq

            # max_retries=0: don't let the SDK do its own slow backoff on 429 —
            # our cooldown + provider failover handles quota exhaustion instantly.
            self._groq = Groq(api_key=self.s.groq_api_key, max_retries=0)
        return self._groq

    # -- provider cooldown (free-tier quota handling) ----------------
    def _in_cooldown(self, provider: str) -> bool:
        return time.time() < self._cooldown.get(provider, 0.0)

    def _note_error(self, provider: str, exc: Exception) -> None:
        msg = str(exc)
        low = msg.lower()
        if "429" in msg or "rate_limit" in low or "quota" in low or "resource_exhausted" in low:
            secs = 90.0
            m = re.search(r"try again in ([0-9.]+)s", msg)
            if m:
                secs = min(float(m.group(1)) + 5, 3600)
            elif "per day" in low or "tpd" in low:
                secs = 1800.0  # daily cap hit — cool down 30 min, run rule-based
            self._cooldown[provider] = time.time() + secs
            log.warning("LLM %s rate-limited — cooling down %.0fs, falling back", provider, secs)

    # -- cache -------------------------------------------------------
    def _cache_key(self, model: str, prompt: str, system: str) -> Path:
        h = hashlib.sha256(f"{model}\x00{system}\x00{prompt}".encode()).hexdigest()[:32]
        return self.cache_dir / f"{h}.json"

    def _cache_get(self, key: Path) -> str | None:
        if key.exists() and (time.time() - key.stat().st_mtime) < self.cache_ttl:
            try:
                return json.loads(key.read_text())["text"]
            except Exception:  # noqa: BLE001
                return None
        return None

    def _cache_put(self, key: Path, text: str) -> None:
        try:
            key.write_text(json.dumps({"text": text}))
        except Exception:  # noqa: BLE001
            pass

    # -- core completion --------------------------------------------
    def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        use_cache: bool = True,
        max_tokens: int = 2048,
    ) -> str | None:
        if not self.available:
            return None

        for provider in self.providers:
            if self._in_cooldown(provider):
                continue
            model = self.gemini_model if provider == "gemini" else self.groq_model
            ckey = self._cache_key(f"{provider}:{model}", prompt, system)
            if use_cache:
                cached = self._cache_get(ckey)
                if cached is not None:
                    return cached
            try:
                if provider == "gemini":
                    text = self._call_gemini(model, prompt, system, temperature, max_tokens)
                else:
                    text = self._call_groq(model, prompt, system, temperature, max_tokens)
                if text:
                    self._cache_put(ckey, text)
                    return text
            except Exception as exc:  # noqa: BLE001
                self._note_error(provider, exc)
                if not self._in_cooldown(provider):
                    log.warning("LLM provider %s failed (%s); trying next", provider, exc)
        return None

    def _call_gemini(self, model, prompt, system, temperature, max_tokens) -> str:
        from google.genai import types

        self._gemini_rl.acquire()
        client = self._ensure_gemini()
        cfg = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system or None,
        )
        resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
        return (resp.text or "").strip()

    def _call_groq(self, model, prompt, system, temperature, max_tokens) -> str:
        self._groq_rl.acquire()
        client = self._ensure_groq()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
        )
        return (resp.choices[0].message.content or "").strip()

    # -- JSON helper -------------------------------------------------
    def complete_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.1,
        use_cache: bool = True,
        default: Any = None,
    ) -> Any:
        sys2 = (system + "\n\n" if system else "") + (
            "Respond ONLY with a single valid JSON object. No markdown, no prose, "
            "no code fences. If you are unsure, still return your best estimate."
        )
        text = self.complete(prompt, sys2, temperature, use_cache)
        if not text:
            return default
        parsed = _extract_json(text)
        return parsed if parsed is not None else default


def _extract_json(text: str) -> Any:
    text = text.strip()
    # strip code fences
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # find first {...} or [...] block
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                continue
    return None


_client: LLMClient | None = None


def llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
