"""A small Gemini client (REST) that rotates through several API keys.

A key that is rate-limited (429) rests for a minute; a key Google rejects is set aside until it
is added again. The model's answer is returned exactly as received, because Gemini 3 needs its
function-call "thought signatures" sent back unchanged."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
RATE_LIMIT_REST_SECONDS = 60
REQUEST_TIMEOUT = 60

Post = Callable[[str, dict, dict], Awaitable[tuple[int, dict]]]


class GeminiError(Exception):
    """Gemini refused or failed; the text is safe to show (it never contains a key)."""


class NoKeysError(GeminiError):
    pass


async def _httpx_post(url: str, headers: dict, body: dict) -> tuple[int, dict]:
    import httpx

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=body)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, {}


def _error_text(data: dict) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    return str((error or {}).get("message") or "") if isinstance(error, dict) else ""


class GeminiClient:
    def __init__(self, post: Post | None = None) -> None:
        self._post = post or _httpx_post
        self._rest_until: dict[str, float] = {}  # key -> time it may be used again
        self._rejected: set[str] = set()
        self._next = 0

    def forget(self, key: str) -> None:
        self._rest_until.pop(key, None)
        self._rejected.discard(key)

    def _usable(self, keys: list[str]) -> list[str]:
        now = time.monotonic()
        usable = [k for k in keys if k not in self._rejected and self._rest_until.get(k, 0) <= now]
        if not usable:
            return []
        start = self._next % len(usable)
        self._next += 1
        return usable[start:] + usable[:start]  # take turns, so no key is worn out first

    async def generate(
        self,
        keys: list[str],
        model: str,
        system: str,
        contents: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.4,
    ) -> dict:
        """Returns the model's content: {"role": "model", "parts": [...]}."""
        if not keys:
            raise NoKeysError("No Gemini API key is saved. Add one in /aisettings → Keys.")
        body: dict[str, Any] = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": {"temperature": temperature},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": tools}]
        url = f"{API_ROOT}/{model.removeprefix('models/')}:generateContent"
        problems: list[str] = []
        for key in self._usable(keys):
            try:
                status, data = await self._post(url, {"x-goog-api-key": key, "Content-Type": "application/json"}, body)
            except (asyncio.TimeoutError, OSError) as exc:
                problems.append(f"network problem ({type(exc).__name__})")
                continue
            except Exception as exc:  # httpx errors and anything else the transport raises
                problems.append(f"connection problem ({type(exc).__name__})")
                continue
            if status == 200:
                return self._content(data)
            reason = _error_text(data)
            if status == 429:
                self._rest_until[key] = time.monotonic() + RATE_LIMIT_REST_SECONDS
                problems.append("a key hit its limit and is resting for a minute")
            elif status in (401, 403) or (status == 400 and "api key" in reason.lower()):
                self._rejected.add(key)
                problems.append(f"a key was rejected by Google ({reason[:80] or status})")
            elif status >= 500:
                problems.append(f"Gemini is having trouble (HTTP {status})")
            else:
                raise GeminiError(f"Gemini said: {reason[:300] or 'HTTP ' + str(status)}")
        if not problems:
            raise GeminiError("All your Gemini keys are resting or rejected. Add a new key in /aisettings → Keys.")
        raise GeminiError("Could not get an answer: " + "; ".join(dict.fromkeys(problems)) + ".")

    @staticmethod
    def _content(data: dict) -> dict:
        candidates = data.get("candidates") or []
        content = (candidates[0].get("content") if candidates else None) or {}
        if not content.get("parts"):
            reason = (candidates[0].get("finishReason") if candidates else None) or (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiError(f"Gemini gave no answer ({reason or 'empty reply'}).")
        return {"role": "model", "parts": content["parts"]}

    async def check_key(self, key: str, model: str) -> str | None:
        """None if the key works, otherwise why not. Used by the 'Test keys' button."""
        try:
            await self.generate([key], model, "Reply with: ok", [{"role": "user", "parts": [{"text": "ping"}]}], temperature=0)
        except GeminiError as exc:
            return str(exc)
        finally:
            self.forget(key)
        return None
