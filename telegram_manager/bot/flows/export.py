"""Session export for anybody (not only admins): log in with the number + code, get the session
string of THAT account back, nothing is stored. Also the helper that shows a session string."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import suppress

from telethon.errors import PasswordHashInvalidError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError

from telegram_manager.bot.flows.common import static
from telegram_manager.bot.wizard.engine import ATTEMPT_KEY
from telegram_manager.bot.wizard.spec import Step, WizardAbort, WizardContext, WizardSpec
from telegram_manager.constants import EXPORT_AUTODELETE, EXPORT_COOLDOWN, WIZARD_TTL
from telegram_manager.errors import describe_error
from telegram_manager.parsing import parse_int

logger = logging.getLogger(__name__)

_last_request: dict[int, float] = {}
_pending: set[asyncio.Task] = set()


async def _delete_later(message, seconds: int) -> None:
    await asyncio.sleep(seconds)
    with suppress(Exception):
        await message.delete()


async def send_session(context, chat_id: int, session: str) -> None:
    """The session string as a tap-to-copy block; the message deletes itself after a few minutes."""
    minutes = max(1, EXPORT_AUTODELETE // 60)
    text = (
        "🔑 <b>Your session string</b>\n\n"
        f"<code>{html.escape(session)}</code>\n\n"
        "⚠️ Anyone who has this text can use your Telegram account. Never share it.\n"
        f"🗑 This message deletes itself in {minutes} minutes — copy it now."
    )
    message = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    task = asyncio.ensure_future(_delete_later(message, EXPORT_AUTODELETE))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _expire_attempt(manager, context, attempt) -> None:
    """A phone login nobody finished must not keep a Telegram connection open."""
    await asyncio.sleep(WIZARD_TTL + 5)
    if context.user_data.get(ATTEMPT_KEY) is attempt:
        context.user_data.pop(ATTEMPT_KEY, None)
        await manager.abort_login(attempt)


def build_export_specs(deps) -> list[WizardSpec]:
    manager = deps.manager

    async def parse_api_id(wc: WizardContext, raw: str) -> int:
        return parse_int(raw, "API ID")

    async def parse_api_hash(wc: WizardContext, raw: str) -> str:
        value = raw.strip()
        if len(value) < 20:
            raise ValueError("That API hash looks too short.")
        return value

    async def drop_attempt(wc: WizardContext) -> None:
        await manager.abort_login(wc.context.user_data.pop(ATTEMPT_KEY, None))

    async def parse_phone(wc: WizardContext, raw: str) -> str:
        phone = re.sub(r"[\s().-]", "", raw)
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        elif phone.isdigit() and 11 <= len(phone) <= 15:
            phone = "+" + phone
        if not re.fullmatch(r"\+\d{7,15}", phone):
            raise ValueError("Send the number with country code, like +919876543210.")
        wait = EXPORT_COOLDOWN - (time.monotonic() - _last_request.get(wc.user_id, -EXPORT_COOLDOWN))
        if wait > 0:
            raise ValueError(f"Please wait {int(wait) + 1} seconds before asking for another code.")
        await drop_attempt(wc)
        attempt = await manager.begin_login(wc.user_id, wc.answers["api_id"], wc.answers["api_hash"], phone)
        _last_request[wc.user_id] = time.monotonic()
        if len(_last_request) > 2000:
            cutoff = time.monotonic() - EXPORT_COOLDOWN
            for key in [k for k, v in _last_request.items() if v < cutoff]:
                _last_request.pop(key, None)
        wc.context.user_data[ATTEMPT_KEY] = attempt
        task = asyncio.ensure_future(_expire_attempt(manager, wc.context, attempt))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return phone

    async def parse_code(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with /export.")
        code = re.sub(r"\D", "", raw)
        if not code:
            raise ValueError("Send the code Telegram gave you (digits only).")
        try:
            wc.answers["session"] = await manager.export_after_code(attempt, code)
        except SessionPasswordNeededError:
            wc.answers["needs_password"] = True
            return "ok"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            raise ValueError("That code is wrong or expired. Send the newest one, or go Back for a new code.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        return "ok"

    async def parse_password(wc: WizardContext, raw: str) -> str:
        attempt = wc.context.user_data.get(ATTEMPT_KEY)
        if attempt is None:
            raise WizardAbort("⌛ The login expired. Start again with /export.")
        try:
            wc.answers["session"] = await manager.export_after_password(attempt, raw)
        except PasswordHashInvalidError as exc:
            raise ValueError("Wrong password. Try again.") from exc
        wc.context.user_data.pop(ATTEMPT_KEY, None)
        return "ok"

    async def rewind(wc: WizardContext, target_key: str) -> None:
        wc.answers.pop("needs_password", None)
        await drop_attempt(wc)

    async def finish(wc: WizardContext) -> None:
        from telegram_manager.bot import keyboards

        session = wc.answers.pop("session", None)
        if not session:
            await wc.show("❌ No session was created. Try again with /export.", keyboards.public_keyboard(True))
            return
        try:
            await send_session(wc.context, wc.update.effective_chat.id, session)
        except Exception as exc:
            logger.warning("Could not send an exported session: %s", describe_error(exc))
            await wc.show(f"❌ Could not send it: {describe_error(exc)}", keyboards.public_keyboard(True))
            return
        await wc.show(
            "✅ Done. Your session string is in the message above.\n\n"
            "I did not save it, your API hash, your number or your password anywhere.",
            keyboards.public_keyboard(True),
        )

    spec = WizardSpec(
        name="ex_otp",
        title="📤 Export my session",
        steps=[
            Step(
                "api_id",
                static(
                    "🔢 Send your API ID (from my.telegram.org → API development tools).\n\n"
                    "I will log in to YOUR account once, give you its session string, and keep nothing."
                ),
                parse_api_id,
            ),
            Step(
                "api_hash",
                static("🔐 Send your API hash.\nI delete your message right after reading it."),
                parse_api_hash,
                sensitive=True,
            ),
            Step("phone", static("📞 Send your phone number with country code, like +919876543210."), parse_phone),
            Step(
                "code",
                static(
                    "📨 Telegram sent you a login code.\n\nSend it with spaces between the digits (like 1 2 3 4 5) — "
                    "Telegram cancels codes that are sent as plain text."
                ),
                parse_code,
                sensitive=True,
                back_key="phone",
            ),
            Step(
                "password",
                static("🔒 This account has two-step verification. Send its password."),
                parse_password,
                sensitive=True,
                skip=lambda wc: not wc.answers.get("needs_password"),
                back_key="phone",
            ),
        ],
        finish=finish,
        public=True,
        back_to="home",
        on_rewind=rewind,
    )
    return [spec]
