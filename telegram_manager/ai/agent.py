"""One conversation turn: the admin's words -> Gemini -> tools -> a short answer."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from telegram_manager.ai import tools
from telegram_manager.ai.config import AISettings
from telegram_manager.ai.gemini import GeminiError, NoKeysError

logger = logging.getLogger(__name__)

MAX_STEPS = 6  # tool rounds in one turn
MEMORY_TURNS = 16  # remembered messages per admin ("usko" needs the last few)
MAX_USER_TEXT = 3000

# Secrets are never sent to Google: long session-like strings, API hashes and API keys.
_SECRETS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[key hidden]"),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "[hash hidden]"),
    (re.compile(r"[A-Za-z0-9_\-+/=]{70,}"), "[session hidden]"),
]


def scrub(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class AgentResult:
    text: str = ""
    outcome: tools.Outcome = field(default_factory=tools.Outcome)
    error: bool = False


class Agent:
    def __init__(self, deps) -> None:
        self.deps = deps
        self._memory: dict[int, list[dict]] = {}

    def clear(self, owner_id: int) -> None:
        self._memory.pop(owner_id, None)

    async def load(self, owner_id: int) -> tuple[AISettings, list[str]]:
        settings = AISettings.from_dict(await self.deps.store.settings.get_ai(owner_id))
        keys: list[str] = []
        for token in settings.keys_enc:
            try:
                keys.append(self.deps.manager.secret_box.decrypt(token))
            except Exception:
                logger.warning("A saved Gemini key could not be decrypted", exc_info=True)
        return settings, keys

    async def _system(self, settings: AISettings, ctx: tools.ToolContext) -> str:
        now = datetime.now(self.deps.manager.tz)
        count = len(await ctx.store.accounts.list(ctx.owner_id))
        mode = (
            "Risky actions (sending, blocking, deleting, scheduling) are shown to the admin as ✅/❌ buttons first; "
            "after calling such a tool, say briefly that you are waiting for approval."
            if ctx.mode == "confirm"
            else "Auto mode: tools run immediately, so be sure of the account, the person and the text before calling them."
        )
        return (
            f"{settings.system_prompt()}\n\n---\n"
            f"Now: {now.strftime('%A, %d %B %Y, %I:%M %p')} ({now.tzname()}). The admin has {count} account(s).\n{mode}\n"
            "Call a tool whenever the admin asks for something it can do. Never claim an action happened unless a tool "
            "said so. Keep answers short."
        )

    async def run(self, update, context, owner_id: int, text: str, replied_to: str | None = None) -> AgentResult:
        settings, keys = await self.load(owner_id)
        result = AgentResult()
        if not keys:
            result.text, result.error = "Gemini API key nahi hai. /aisettings → 🔑 Keys mein key add karo.", True
            return result
        ctx = tools.ToolContext(self.deps, update, context, owner_id, settings.mode, result.outcome)
        system = await self._system(settings, ctx)
        user_text = scrub(text)[:MAX_USER_TEXT]
        if replied_to:
            user_text = f"[The admin is replying to this message: «{scrub(replied_to)[:1500]}»]\n{user_text}"
        history = self._memory.get(owner_id, [])
        contents = history + [{"role": "user", "parts": [{"text": user_text}]}]
        declarations = tools.declarations()
        answer = ""
        try:
            for _ in range(MAX_STEPS):
                reply = await self.deps.gemini.generate(keys, settings.model, system, contents, declarations)
                calls = [p["functionCall"] for p in reply["parts"] if "functionCall" in p]
                answer = "".join(p.get("text", "") for p in reply["parts"] if not p.get("thought")).strip()
                if not calls:
                    break
                contents.append(reply)  # as received: Gemini 3 wants its thought signatures back
                responses = []
                for call in calls:
                    outcome = await tools.call(ctx, call.get("name", ""), call.get("args") or {})
                    responses.append({"functionResponse": {"name": call.get("name", ""), "response": _for_model(outcome)}})
                contents.append({"role": "user", "parts": responses})
            else:
                answer = answer or "Kaam bahut lamba ho gaya, thoda chhota karke dobara bolo."
        except NoKeysError as exc:
            result.text, result.error = str(exc), True
            return result
        except GeminiError as exc:
            result.text, result.error = f"⚠️ {exc}", True
            return result
        result.text = answer or ("Theek hai." if not (result.outcome.pending or result.outcome.choices or result.outcome.wizard) else "")
        # remember the talk (text only), so "usko" / "wahi account" work next time
        history = history + [{"role": "user", "parts": [{"text": user_text}]}]
        if result.text:
            history.append({"role": "model", "parts": [{"text": result.text}]})
        self._memory[owner_id] = history[-MEMORY_TURNS:]
        return result


def _for_model(outcome: dict) -> dict:
    """What the model sees of a tool result: the facts, not the display text."""
    return {k: v for k, v in outcome.items() if k != "message"} | ({"result": outcome["message"]} if outcome.get("message") else {})
