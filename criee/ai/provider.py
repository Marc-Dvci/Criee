"""One gateway for every model call.

Order of resolution for a request: a committed recording (the demo inputs), the runtime cache,
then a live call when the budget allows it. A live call is refused after `CRIEE_AI_UNTIL`, past
the daily call cap, past the daily dollar budget, or past a visitor's hourly allowance; callers then
use their deterministic path. Each live call reserves the most one call can cost before it is sent,
and settles to its billed tokens after, so concurrent calls cannot overrun the dollar budget.
The API key stays on the server: no route returns it and the browser never calls the model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from ..config import ROOT, Config

log = logging.getLogger("criee.ai")

RECORDINGS = ROOT / "data" / "ai_recordings"
MAX_OUTPUT_TOKENS = 8192
# the most one call can cost: four photos and a voice note in, MAX_OUTPUT_TOKENS out
RESERVE_IN_TOKENS = 12_000


class AIUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Media:
    mime: str
    data: bytes


Part = Union[str, Media]


def _parts_json(parts: list[Part]) -> list[dict]:
    out = []
    for p in parts:
        if isinstance(p, str):
            out.append({"text": p})
        else:
            out.append({"inlineData": {"mimeType": p.mime, "data": base64.b64encode(p.data).decode()}})
    return out


def request_key(task: str, payload: Any) -> str:
    def norm(x: Any) -> Any:
        if isinstance(x, Media):
            return {"mime": x.mime, "sha256": hashlib.sha256(x.data).hexdigest()}
        if isinstance(x, dict):
            return {k: norm(v) for k, v in sorted(x.items())}
        if isinstance(x, (list, tuple)):
            return [norm(v) for v in x]
        return x
    return hashlib.sha256((task + json.dumps(norm(payload), sort_keys=True)).encode()).hexdigest()[:32]


class Budget:
    def __init__(self, cfg: Config, kv_get, kv_set):
        self.cfg, self.kv_get, self.kv_set = cfg, kv_get, kv_set
        self._visitors: dict[str, list[float]] = {}

    def live_allowed(self, visitor: Optional[str]) -> Optional[str]:
        """None when a live call may go ahead, else the reason it may not."""
        cfg = self.cfg
        if cfg.ai_provider != "gemini" or not cfg.gemini_api_key:
            return "no_live_model"
        today = datetime.now(timezone.utc).date()
        if cfg.ai_until and today > cfg.ai_until:
            return "after_cutoff"
        if int(self.kv_get(f"ai_calls:{today.isoformat()}") or 0) >= cfg.ai_daily_cap:
            return "daily_cap"
        if self.usd_today() + self.reserve_usd() > cfg.ai_daily_usd:
            return "daily_usd"
        if visitor:
            now = time.time()
            hits = [t for t in self._visitors.get(visitor, []) if now - t < 3600]
            self._visitors[visitor] = hits
            if len(hits) >= cfg.ai_per_visitor_hourly:
                return "visitor_cap"
        return None

    def spend(self, visitor: Optional[str]) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        self.kv_set(f"ai_calls:{today}", str(int(self.kv_get(f"ai_calls:{today}") or 0) + 1))
        self._add_usd(self.reserve_usd())
        if visitor:
            self._visitors.setdefault(visitor, []).append(time.time())

    def settle(self, usage: Optional[dict]) -> float:
        """Replace a call's reservation with what its tokens cost. Thinking tokens bill as output."""
        cost = self.cost_usd(usage) if usage else self.reserve_usd()
        self._add_usd(cost - self.reserve_usd())
        return cost

    def cost_usd(self, usage: dict) -> float:
        out = int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
        return (int(usage.get("promptTokenCount") or 0) * self.cfg.ai_usd_in_per_m
                + out * self.cfg.ai_usd_out_per_m) / 1e6

    def reserve_usd(self) -> float:
        return self.cost_usd({"promptTokenCount": RESERVE_IN_TOKENS, "candidatesTokenCount": MAX_OUTPUT_TOKENS})

    def usd_today(self) -> float:
        return int(self.kv_get(f"ai_usd_micro:{datetime.now(timezone.utc).date().isoformat()}") or 0) / 1e6

    def _add_usd(self, usd: float) -> None:
        k = f"ai_usd_micro:{datetime.now(timezone.utc).date().isoformat()}"
        self.kv_set(k, str(int(self.kv_get(k) or 0) + round(usd * 1e6)))

    def calls_today(self) -> int:
        return int(self.kv_get(f"ai_calls:{datetime.now(timezone.utc).date().isoformat()}") or 0)


class AI:
    def __init__(self, cfg: Config, kv_get, kv_set, cache_dir: Optional[Path] = None):
        self.cfg = cfg
        self.budget = Budget(cfg, kv_get, kv_set)
        self.cache_dir = cache_dir or cfg.data_dir / "ai_cache"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))
        self.last_source: Optional[str] = None

    @property
    def model(self) -> str:
        return self.cfg.gemini_model

    def _recorded(self, task: str, key: str) -> Optional[dict]:
        for root in (RECORDINGS, self.cache_dir):
            f = root / task / f"{key}.json"
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        return None

    def _store(self, task: str, key: str, value: dict, record: bool) -> None:
        root = RECORDINGS if record else self.cache_dir
        d = root / task
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")

    async def _generate(self, body: dict) -> dict:
        if self.cfg.gemini_endpoint == "vertex":
            url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{self.model}:generateContent"
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.cfg.gemini_api_key}
        # one retry, only after a fast failure (refused connection, 500/503): a slow call is not repeated,
        # so a visitor waits at most one read timeout before the deterministic path takes over
        r = None
        for attempt in range(2):
            try:
                r = await self._http.post(url, json=body, headers=headers)
            except httpx.TimeoutException as e:
                raise AIUnavailable(f"model unreachable: {type(e).__name__}")
            except httpx.TransportError as e:
                if attempt == 1:
                    raise AIUnavailable(f"model unreachable: {type(e).__name__}")
                await asyncio.sleep(1.5)
                continue
            if r.status_code in (500, 503) and attempt == 0:
                await asyncio.sleep(1.5)
                continue
            break
        if r.status_code >= 500 or r.status_code == 429:
            raise AIUnavailable(f"model busy {r.status_code}")
        if r.status_code >= 400:
            raise AIUnavailable(f"model error {r.status_code}: {r.text[:300]}")
        return r.json()

    async def json(self, task: str, system: str, parts: list[Part], schema: dict, visitor: Optional[str] = None,
                   thinking: str = "LOW", record: bool = False) -> dict:
        """Structured output. Raises AIUnavailable when no recording exists and a live call is not allowed."""
        key = request_key(task, {"system": system, "parts": list(parts), "schema": schema, "model": self.model})
        hit = self._recorded(task, key)
        if hit is not None:
            self.last_source = "recorded"
            return hit["output"]
        why = self.budget.live_allowed(visitor)
        if why:
            raise AIUnavailable(why)
        self.budget.spend(visitor)
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": _parts_json(parts)}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                     "temperature": 0.2, "thinkingConfig": {"thinkingLevel": thinking},
                                     "mediaResolution": "MEDIA_RESOLUTION_MEDIUM", "maxOutputTokens": MAX_OUTPUT_TOKENS}}
        t0 = time.perf_counter()
        res = await self._generate(body)
        ms = round((time.perf_counter() - t0) * 1000)
        self.budget.settle(res.get("usageMetadata"))
        try:
            text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"] if not p.get("thought"))
            out = json.loads(text)
        except (KeyError, IndexError, ValueError) as e:
            raise AIUnavailable(f"unparseable model output: {e}")
        self._store(task, key, {"task": task, "model": self.model, "ms": ms, "usage": res.get("usageMetadata"),
                                "output": out}, record)
        self.last_source = "live"
        return out

    async def turn(self, task: str, system: str, contents: list[dict], tools: list[dict],
                   visitor: Optional[str] = None, record: bool = False) -> dict:
        """One function-calling turn. `contents` is the full conversation in Gemini's format; the model
        parts are returned unchanged so their thought signatures can be echoed back."""
        key = request_key(task, {"system": system, "contents": contents, "tools": tools, "model": self.model})
        hit = self._recorded(task, key)
        if hit is not None:
            self.last_source = "recorded"
            return hit["output"]
        why = self.budget.live_allowed(visitor)
        if why:
            raise AIUnavailable(why)
        self.budget.spend(visitor)
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "tools": [{"functionDeclarations": tools}],
                "generationConfig": {"temperature": 0.1, "thinkingConfig": {"thinkingLevel": "LOW"},
                                     "maxOutputTokens": MAX_OUTPUT_TOKENS}}
        res = await self._generate(body)
        self.budget.settle(res.get("usageMetadata"))
        try:
            content = res["candidates"][0]["content"]
        except (KeyError, IndexError):
            raise AIUnavailable("empty model turn")
        self._store(task, key, {"task": task, "model": self.model, "output": content}, record)
        self.last_source = "live"
        return content

    async def aclose(self) -> None:
        await self._http.aclose()
