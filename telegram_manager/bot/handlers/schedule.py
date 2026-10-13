"""Daily broadcast screen (the questions themselves are in flows/schedule.py)."""
from __future__ import annotations

from telegram_manager.bot import keyboards, ui
from telegram_manager.bot.handlers.base import HandlerBase


class ScheduleHandlers(HandlerBase):
    async def home_screen(self, update, context) -> None:
        uid = self.uid(update)
        job = self.manager.daily_job_for(uid)
        text = (
            "🕒 Daily broadcast\n\nSends one message to one user every day, from all your accounts or only "
            "the ones you pick. It can also press inline buttons of the bot's reply.\n\n"
        )
        text += self.manager.daily_job_status(uid)
        await ui.show(update, text, keyboards.schedule_home(job is not None))

    async def home(self, update, context) -> None:
        if await self.entry(update):
            await self.home_screen(update, context)

    async def new(self, update, context) -> None:
        if not await self.entry(update):
            return
        if await self.accounts_or_notice(update) is None:
            return
        await self.engine.start(update, context, "schedule")

    async def off(self, update, context) -> None:
        if not await self.entry(update):
            return
        was_on = await self.manager.stop_daily_job(self.uid(update))
        text = "🗑 The daily broadcast is off." if was_on else "No daily broadcast was scheduled."
        await ui.show(update, text, keyboards.schedule_home(False))
