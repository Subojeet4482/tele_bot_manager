"""/start, /menu, /cancel and the two main screens."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase


class StartHandlers(HandlerBase):
    def _welcome(self, user_id: int) -> str:
        owner = self.deps.access.is_owner(user_id)
        commands = (
            "/menu /list /refresh /all /alll /multi /click /allblock /broadcast /message "
            "/usermessage /botmessage /channelmessage /cancel"
        )
        if owner:
            commands += " /logs"
        return (
            "👋 Telegram Account Manager\n"
            f"You are the {'👑 owner' if owner else '🛡 admin'}. "
            "You only see and control the accounts you connect yourself.\n\n"
            "Everything is on the buttons below. Commands work too:\n" + commands
        )

    async def start(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        user = update.effective_user
        await self.deps.access.remember_name(user.id, user.full_name)
        await ui.show(update, self._welcome(user.id), keyboards.start_keyboard(self.is_owner(update)))

    async def home_screen(self, update, context) -> None:
        await ui.show(update, self._welcome(self.uid(update)), keyboards.start_keyboard(self.is_owner(update)))

    async def menu_screen(self, update, context) -> None:
        await ui.show(update, "⚙️ Menu — pick what you want to do.", keyboards.menu_keyboard(self.is_owner(update)))

    async def home(self, update, context) -> None:
        if await self.entry(update):
            await self.home_screen(update, context)

    async def menu(self, update, context) -> None:
        if await self.entry(update):
            await self.menu_screen(update, context)

    async def cancel(self, update, context) -> None:
        if not await self.guard.allow(update):
            return
        stopped = self.deps.bulk.cancel(self.uid(update))
        text = "❌ Cancelled." + (" The running bulk action is being stopped." if stopped else "")
        await ui.show(update, text, keyboards.back("menu", "⚙️ Menu"))

    async def moved(self, update, context) -> None:
        """/delay and /onalltime were replaced by step-by-step buttons."""
        if not await self.guard.allow(update):
            return
        command = update.effective_message.text.split()[0].split("@")[0]
        if command == "/onalltime":
            text = "🕒 The daily broadcast now lives in the menu, with one question at a time."
            markup = keyboards.back("sched|home", "🕒 Open daily broadcast")
        else:
            text = "⏱ The delay is now asked every time you run /all, /alll or /allblock."
            markup = keyboards.back("menu", "⚙️ Menu")
        await ui.show(update, text, markup)
