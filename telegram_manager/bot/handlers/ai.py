"""The AI: /ai, plain messages, approval buttons and the settings screen (/aisettings)."""
from __future__ import annotations

import logging
import re
import time
from contextlib import suppress

from telegram_manager.ai import tools
from telegram_manager.ai.agent import Agent
from telegram_manager.ai.config import (
    DEFAULT_PROMPT,
    MODE_AUTO,
    MODE_CONFIRM,
    MODEL_PRESETS,
    NAME_MAX,
    PROMPT_MAX,
    AISettings,
    mask_key,
)
from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase

logger = logging.getLogger(__name__)

WAIT_KEY = "ai_wait"
WAIT_SECONDS = 600
KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
HEX_RE = re.compile(r"\b[0-9a-fA-F]{32}\b")
PREFIX_RE = re.compile(r"^\s*ai\s*[,:\-]?\s+", re.IGNORECASE)
MAX_KEYS = 30
Btn, _markup = keyboards.Btn, keyboards._markup


def _row(*pairs: tuple[str, str]) -> list:
    return [Btn(label, callback_data=data) for label, data in pairs]


class AIHandlers(HandlerBase):
    def __init__(self, deps, engine) -> None:
        super().__init__(deps, engine)
        self.agent = Agent(deps)

    # --- settings access -----------------------------------------------------------------
    async def _settings(self, uid: int) -> AISettings:
        return AISettings.from_dict(await self.store.settings.get_ai(uid))

    async def _keys(self, uid: int) -> list[str]:
        return (await self.agent.load(uid))[1]

    async def _save_keys(self, uid: int, keys: list[str]) -> None:
        box = self.manager.secret_box
        await self.store.settings.set_ai(uid, keys_enc=[box.encrypt(k) for k in keys])

    # --- entry points used by the text handler ------------------------------------------
    @staticmethod
    def has_prefix(text: str) -> bool:
        return PREFIX_RE.match(text) is not None

    async def pre_route(self, update, context, text: str) -> bool:
        """Settings answers and secrets typed into the chat. True when this message was one of those."""
        uid = self.uid(update)
        wait = context.user_data.get(WAIT_KEY)
        if wait and time.time() - wait[1] < WAIT_SECONDS:
            context.user_data.pop(WAIT_KEY, None)
            await self._apply_wait(update, context, wait[0], text)
            return True
        context.user_data.pop(WAIT_KEY, None)
        message = update.effective_message
        found_keys = KEY_RE.findall(text)
        if found_keys:
            added = await self._add_keys(uid, found_keys)
            with suppress(Exception):
                await message.delete()
            await message.reply_text(f"🔑 {added} new Gemini key(s) saved. Message deleted so the key does not stay in the chat.")
            return True
        creds = self._api_creds(text)
        if creds:
            await self.manager.save_api(uid, *creds)
            with suppress(Exception):
                await message.delete()
            await message.reply_text(f"✅ API ID {creds[0]} and its hash are saved (encrypted). Message deleted.")
            return True
        return False

    @staticmethod
    def _api_creds(text: str) -> tuple[int, str] | None:
        if len(text) > 300 or "api" not in text.lower():
            return None
        digest, ids = HEX_RE.search(text), re.findall(r"\b\d{5,10}\b", text)
        return (int(ids[0]), digest.group(0)) if digest and ids else None

    async def ask(self, update, context, text: str, replied=None) -> bool:
        """Give a message to the AI. False when the AI is switched off."""
        uid = self.uid(update)
        message = update.effective_message
        settings = await self._settings(uid)
        if not settings.enabled or not await self._keys(uid):
            return False  # nothing to ask: the caller shows the normal hint
        text = PREFIX_RE.sub("", text, count=1) if self.has_prefix(text) else text
        with suppress(Exception):
            await context.bot.send_chat_action(update.effective_chat.id, "typing")
        replied_text = None
        if replied is not None:
            replied_text = getattr(replied, "text", None) or getattr(replied, "caption", None)
        try:
            result = await self.agent.run(update, context, uid, text, replied_text)
        except Exception:
            logger.exception("AI turn failed")
            await message.reply_text("⚠️ AI mein kuch gadbad ho gayi. Dobara try karo.")
            return True
        await self._present(update, context, result, settings)
        return True

    async def _present(self, update, context, result, settings: AISettings) -> None:
        message = update.effective_message
        if result.text:
            await message.reply_text(result.text[:4000])
        for token, summary in result.outcome.pending:
            await message.reply_text(
                f"🤖 {settings.name} ko aapki ijazat chahiye:\n\n{summary}",
                reply_markup=keyboards.confirm_pair(f"aia|ok|{token}", f"aia|no|{token}", "✅ Haan, karo"),
            )
        if result.outcome.choices:
            rows = [[Btn(label, callback_data=data)] for label, data in result.outcome.choices[:20]]
            await message.reply_text("Kaunsa?", reply_markup=_markup(rows))
        if result.outcome.wizard:
            answers: dict = {}
            saved = await self.manager.saved_api(self.uid(update))
            if saved:
                answers["api_id"], answers["api_hash"] = saved
            await self.engine.start(update, context, result.outcome.wizard, answers)

    # --- commands ----------------------------------------------------------------------------
    async def ai_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        text = self.command_args(update).strip()
        if not text:
            await self.show_settings(update)
            return
        if await self.pre_route(update, context, text):
            return
        if not await self.ask(update, context, text, update.effective_message.reply_to_message):
            await ui.show(update, "AI abhi chalu nahi hai: /aisettings mein ON karo aur Gemini key jodo.", keyboards.back("ai|home", "🤖 AI"))

    async def settings_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        await self.show_settings(update)

    async def clear_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        self.agent.clear(self.uid(update))
        await ui.show(update, "🧹 AI ki yaad saaf. Nayi baat se shuru.", keyboards.back("ai|home", "🤖 AI"))

    # --- the settings screen --------------------------------------------------------------------
    async def show_settings(self, update) -> None:
        uid = self.uid(update)
        s, keys = await self._settings(uid), await self._keys(uid)
        prompt = s.prompt if len(s.prompt) <= 350 else s.prompt[:350] + "…"
        text = (
            f"🤖 AI — {s.name}  [{'ON ✅' if s.enabled else 'OFF ⛔'}]\n\n"
            f"👤 Aapko bulata hai: {s.address}\n"
            f"🧠 Model: {s.model}\n"
            f"🧩 Mode: {'✋ Confirm — bhejne/delete se pehle puchta hai' if s.mode == MODE_CONFIRM else '⚡ Auto — seedha kar deta hai'}\n"
            f"🔑 Gemini keys: {len(keys)}\n\n"
            f"📝 Prompt:\n{prompt}\n\n"
            f"Kaise use karo: seedha likho (jaise \"account 3 se @user ko hello bhejo\"), ya /ai <baat>. "
            f"Kisi message par reply karke \"ai ...\" likhoge to {s.name} woh message padhkar samjhega."
        )
        rows = [
            _row((("⛔ OFF karo" if s.enabled else "✅ ON karo"), "ai|toggle"), ("🧩 Mode badlo", "ai|mode")),
            _row(("✏️ Prompt", "ai|prompt"), ("👁 Poora prompt", "ai|showprompt")),
            _row(("🏷 AI ka naam", "ai|name"), ("👤 Mujhe kya bulaye", "ai|address")),
            _row(("🧠 Model", "ai|model"), ("🔑 Keys", "ai|keys")),
            _row(("♻️ Prompt reset", "ai|promptreset"), ("🧹 Yaad saaf", "ai|forget")),
            _row(("⬅️ Menu", "menu")),
        ]
        await ui.show(update, text, _markup(rows))

    async def settings_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, arg = (parts[1] if len(parts) > 1 else "home"), (parts[2] if len(parts) > 2 else "")
        s = await self._settings(uid)
        if action == "home":
            await self.show_settings(update)
        elif action == "toggle":
            await self.store.settings.set_ai(uid, enabled=not s.enabled)
            await self.show_settings(update)
        elif action == "mode":
            await self.store.settings.set_ai(uid, mode=MODE_AUTO if s.mode == MODE_CONFIRM else MODE_CONFIRM)
            await self.show_settings(update)
        elif action in ("prompt", "name", "address", "model_custom", "key_add"):
            await self._ask_for(update, context, action)
        elif action == "showprompt":
            await self._send_prompt(update, s)
        elif action == "promptreset":
            await self.store.settings.set_ai(uid, prompt=DEFAULT_PROMPT)
            await self.show_settings(update)
        elif action == "forget":
            self.agent.clear(uid)
            await self.show_settings(update)
        elif action == "model":
            await self._show_models(update, s)
        elif action == "model_set":
            await self.store.settings.set_ai(uid, model=MODEL_PRESETS[int(arg)])
            await self.show_settings(update)
        elif action == "keys":
            await self._show_keys(update, uid)
        elif action == "key_del":
            keys = await self._keys(uid)
            if arg.isdigit() and int(arg) < len(keys):
                self.deps.gemini.forget(keys.pop(int(arg)))
                await self._save_keys(uid, keys)
            await self._show_keys(update, uid)
        elif action == "key_test":
            await self._test_keys(update, uid, s)

    async def _ask_for(self, update, context, field: str) -> None:
        field = "model" if field == "model_custom" else ("keys" if field == "key_add" else field)
        context.user_data[WAIT_KEY] = (field, time.time())
        prompts = {
            "prompt": f"✏️ Naya prompt bhejo (max {PROMPT_MAX} akshar). {{name}} aur {{address}} likhoge to unki jagah AI ka naam aur aapka naam aa jayega.",
            "name": f"🏷 AI ka naya naam bhejo (max {NAME_MAX} akshar).",
            "address": f"👤 AI aapko kya bulaye? Jaise: Mandal Sir (max {NAME_MAX} akshar).",
            "model": "🧠 Model ka naam bhejo, jaise gemini-flash-latest",
            "keys": "🔑 Gemini API key bhejo. Ek se zyada ho to alag lines mein ek saath bhej do. Message turant delete ho jayega.",
        }
        await ui.show(update, prompts[field] + "\n\n(10 minute tak intezaar karunga.)", keyboards.back("ai|home", "❌ Cancel"))

    async def _apply_wait(self, update, context, field: str, text: str) -> None:
        uid, message = self.uid(update), update.effective_message
        text = text.strip()
        if field == "keys":
            tokens = [t for t in re.split(r"[\s,;]+", text) if len(t) >= 20 and t.isascii()]
            with suppress(Exception):
                await message.delete()
            if not tokens:
                await message.reply_text("❌ Koi key nahi mili. /aisettings → 🔑 Keys se dobara try karo.")
                return
            added = await self._add_keys(uid, tokens)
            await message.reply_text(f"🔑 {added} nayi key save hui (message delete kar diya).")
            await self.show_settings(update)
            return
        error = None
        if field == "prompt":
            if len(text) < 20 or len(text) > PROMPT_MAX:
                error = f"Prompt 20 se {PROMPT_MAX} akshar ke beech hona chahiye."
            else:
                await self.store.settings.set_ai(uid, prompt=text)
        elif field in ("name", "address"):
            if not 1 <= len(text) <= NAME_MAX or "\n" in text:
                error = f"1 se {NAME_MAX} akshar, ek line mein."
            else:
                await self.store.settings.set_ai(uid, **{field: text})
        elif field == "model":
            model = text.removeprefix("models/")
            if not re.fullmatch(r"[A-Za-z0-9._\-]{3,80}", model):
                error = "Model ka naam sirf akshar, number, . - _ ho sakta hai."
            else:
                await self.store.settings.set_ai(uid, model=model)
        if error:
            await message.reply_text(f"❌ {error}")
            return
        await message.reply_text("✅ Save ho gaya.")
        await self.show_settings(update)

    async def _add_keys(self, uid: int, new: list[str]) -> int:
        keys = await self._keys(uid)
        added = 0
        for key in dict.fromkeys(new):
            if key not in keys and len(keys) < MAX_KEYS:
                keys.append(key)
                self.deps.gemini.forget(key)
                added += 1
        if added:
            await self._save_keys(uid, keys)
        return added

    async def _send_prompt(self, update, s: AISettings) -> None:
        chat_id = update.effective_chat.id
        text = s.system_prompt()
        for start in range(0, len(text), 3800):
            await update.get_bot().send_message(chat_id, text[start:start + 3800])

    async def _show_models(self, update, s: AISettings) -> None:
        rows = [[Btn(("✅ " if m == s.model else "") + m, callback_data=f"ai|model_set|{i}")] for i, m in enumerate(MODEL_PRESETS)]
        rows.append(_row(("✏️ Dusra model likho", "ai|model_custom")))
        rows.append(_row(("⬅️ Back", "ai|home")))
        await ui.show(
            update,
            f"🧠 Abhi: {s.model}\n\n• gemini-flash-latest — sabse samajhdar, asli kaam ke liye\n"
            "• flash-lite — sasta aur tez, chhote kaam ke liye\n\n-latest naam Google khud naye version par le jata hai.",
            _markup(rows),
        )

    async def _show_keys(self, update, uid: int) -> None:
        keys = await self._keys(uid)
        rows = [[Btn(f"🗑 Key {i + 1} ({mask_key(k)})", callback_data=f"ai|key_del|{i}")] for i, k in enumerate(keys)]
        rows.append(_row(("➕ Key jodo", "ai|key_add"), ("🧪 Test karo", "ai|key_test")))
        rows.append(_row(("⬅️ Back", "ai|home")))
        await ui.show(
            update,
            f"🔑 Gemini keys: {len(keys)}\n\nKeys ek ke baad ek use hoti hain; jo limit par pahunchti hai woh 1 minute aaram karti hai. "
            "Keys encrypted save hoti hain.\n\nZyada keys alag-alag Google project/account ki hon to behtar; Google ke quota rules ka dhyan rakho.",
            _markup(rows),
        )

    async def _test_keys(self, update, uid: int, s: AISettings) -> None:
        keys = await self._keys(uid)
        if not keys:
            await ui.show(update, "Koi key nahi hai.", keyboards.back("ai|keys", "🔑 Keys"))
            return
        await ui.show(update, f"⏳ {len(keys)} key test ho rahi hain…")
        lines = []
        for i, key in enumerate(keys, start=1):
            problem = await self.deps.gemini.check_key(key, s.model)
            lines.append(f"{i}. {mask_key(key)} — {'✅ chal rahi hai' if problem is None else '❌ ' + problem[:120]}")
        await ui.show(update, "\n".join(lines), keyboards.back("ai|keys", "🔑 Keys"))

    # --- approvals and broadcast choice -----------------------------------------------------------
    async def approval_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, token = parts[1], parts[2] if len(parts) > 2 else ""
        settings = await self._settings(uid)
        ctx = tools.ToolContext(self.deps, update, context, uid, settings.mode)
        if action == "no":
            context.user_data.get(tools.PENDING_KEY, {}).pop(token, None)
            await ui.show(update, "❌ Theek hai, nahi kiya.")
            return
        summary = tools.pending_summary(ctx, token)
        result = await tools.execute_pending(ctx, token)
        if result is None:
            await ui.show(update, "⌛ Yeh request purani ho gayi. Dobara bolo.")
            return
        if result.get("error"):
            await ui.show(update, f"❌ {result['error']}")
        elif result.get("message"):
            await ui.show(update, result["message"])
        elif not result.get("status") == "started":
            await ui.show(update, f"✅ Ho gaya: {summary or ''}")

    async def broadcast_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        parts = (update.callback_query.data or "").split("|")
        slot = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        removed = await self.manager.stop_daily_job(self.uid(update), slot)
        await ui.show(update, f"🗑 Daily broadcast #{slot} delete ho gaya." if removed else "Yeh broadcast ab nahi hai.")

    # --- the AI's own chats (/aichats) -------------------------------------------------------------
    async def chats_command(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        uid = self.uid(update)
        live = [s for s in await self.deps.chats.sessions(uid) if s["state"] != "ended"]
        if not live:
            await ui.show(
                update,
                "💬 Abhi koi AI chat nahi chal rahi.\n\nShuru karne ke liye bolo: \"Rahul se @user ko hello bolke chat karo, usse X ke baare mein baat karo\".",
                keyboards.back("ai|home", "🤖 AI"),
            )
            return
        lines, rows = ["💬 AI chats:\n"], []
        for s in live:
            row = await self.store.accounts.get(s["account_id"], owner_id=uid) or {}
            state = "▶️ chal rahi" if s["state"] == "active" else "⏸ ruki (aapka faisla chahiye)"
            mode = "khud reply" if s["autonomy"] == "auto" else "draft"
            lines.append(f"• {s['label']} — account {row.get('number', '?')}, {mode}, {s['replies_left']} reply baaki, {state}")
            rows.append([Btn(f"⛔ {s['label']}", callback_data=f"aic|stop|{s['id']}")])
        rows.append(_row(("⬅️ Back", "ai|home")))
        await ui.show(update, "\n".join(lines), _markup(rows))

    async def chat_button(self, update, context) -> None:
        if not await self.entry(update):
            return
        uid = self.uid(update)
        parts = (update.callback_query.data or "").split("|")
        action, arg = (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
        if action == "stop":
            ended = await self.deps.chats.end(uid, sid=arg)
            await ui.show(update, f"⛔ {ended[0]['label']} ke saath AI chat band." if ended else "Yeh chat pehle hi band hai.")
        elif action == "send":
            await ui.show(update, await self.deps.chats.send_draft(uid, arg))
        elif action == "skip":
            self.deps.chats.skip_draft(arg)
            await ui.show(update, "⏭ Draft chhod diya. Agla message aane par naya draft banega.")
