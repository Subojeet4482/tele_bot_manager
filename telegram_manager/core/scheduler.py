"""Daily broadcast: each admin can have one recurring job."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from datetime import time as dtime

from telegram_manager.constants import BULK_DELAY_MAX, MAX_TIMES_PER_DAY, OPEN_CHAT_MIN_DELAY
from telegram_manager.core.models import DailyJob

logger = logging.getLogger(__name__)


class SchedulerMixin:
    def daily_job_for(self, owner_id: int) -> DailyJob | None:
        entry = self._daily.get(owner_id)
        return entry[0] if entry else None

    def daily_job_status(self, owner_id: int) -> str:
        job = self.daily_job_for(owner_id)
        if job is None:
            return "No daily broadcast is scheduled."
        accounts = "all accounts" if job.account_ids is None else f"{len(job.account_ids)} chosen account(s)"
        buttons = f"Buttons to click: {' → '.join(job.buttons)}\n" if job.buttons else ""
        return (
            f"Target: {job.target}\n"
            f"Message: {job.text}\n"
            f"{buttons}"
            f"Accounts: {accounts}\n"
            f"Delay between accounts: {job.delay_seconds}s\n"
            f"Starts daily at: {job.start_time.strftime('%I:%M %p').lstrip('0')} ({self.tz.key})\n"
            f"Times per day: {job.times_per_day}"
        )

    async def start_daily_job(self, owner_id: int, job: DailyJob) -> None:
        """Schedule a recurring broadcast from all of this admin's accounts. Replaces the admin's
        existing job and is saved to Firestore so it survives restarts."""
        job.delay_seconds = max(OPEN_CHAT_MIN_DELAY, min(int(job.delay_seconds), BULK_DELAY_MAX))
        job.times_per_day = max(1, min(int(job.times_per_day), MAX_TIMES_PER_DAY))
        # Save first: if Firestore fails, the job that is already running is left alone.
        await self.store.jobs.save(owner_id, job.to_dict())
        self._run_daily_job(owner_id, job)

    async def stop_daily_job(self, owner_id: int) -> bool:
        """Cancel and delete this admin's job. True if one was scheduled."""
        was_running = owner_id in self._daily
        self._cancel_daily(owner_id)
        await self.store.jobs.delete(owner_id)
        return was_running

    async def restore_daily_jobs(self) -> None:
        try:
            rows = await self.store.jobs.list_all()
        except Exception:
            logger.exception("Could not restore the daily jobs")
            return
        for owner_id, data in rows:
            try:
                self._run_daily_job(owner_id, DailyJob.from_dict(data))
                logger.info("Restored a daily job from Firestore")
            except Exception:
                logger.exception("Could not restore one daily job")

    def _run_daily_job(self, owner_id: int, job: DailyJob) -> None:
        self._cancel_daily(owner_id)
        self._daily[owner_id] = (job, asyncio.create_task(self._daily_job_loop(owner_id, job)))

    def _cancel_daily(self, owner_id: int) -> None:
        entry = self._daily.pop(owner_id, None)
        if entry:
            entry[1].cancel()

    def _cancel_all_daily(self) -> None:
        for owner_id in list(self._daily):
            self._cancel_daily(owner_id)

    def _next_run(self, job: DailyJob, now: datetime) -> datetime:
        """Next scheduled slot after `now`. Slots start at job.start_time and are spaced
        24h / times_per_day apart (wall-clock in the configured timezone)."""
        step = 86400 / job.times_per_day
        base = job.start_time.hour * 3600 + job.start_time.minute * 60
        candidates = []
        for day_offset in (-1, 0, 1):
            midnight = datetime.combine(now.date() + timedelta(days=day_offset), dtime(0, 0), tzinfo=self.tz)
            for slot in range(job.times_per_day):
                candidates.append(midnight + timedelta(seconds=base + slot * step))
        return min(c for c in candidates if c.timestamp() > now.timestamp())

    async def _broadcast(self, owner_id: int, job: DailyJob):
        """One round: from every account, or only from the ones picked for this broadcast."""
        extra = {"buttons": job.buttons} if job.buttons else {}
        if job.account_ids is None:
            return await self.send_all_any(owner_id, job.target, job.text, job.delay_seconds, **extra)
        return await self.send_selected(owner_id, job.account_ids, job.target, job.text, job.delay_seconds, True, **extra)

    async def _daily_job_loop(self, owner_id: int, job: DailyJob) -> None:
        while True:
            try:
                now = datetime.now(self.tz)
                next_run = self._next_run(job, now)
                logger.info("Daily job: next broadcast at %s", next_run.isoformat(timespec="minutes"))
                await asyncio.sleep(max(0.0, next_run.timestamp() - now.timestamp()))
                logger.info("Daily job: broadcasting")
                result = await self._broadcast(owner_id, job)
                if result.total == 0:
                    await self.notify(owner_id, "⚠️ Daily broadcast: none of its accounts is connected any more, nothing was sent.")
                if result.errors:
                    await self.notify(owner_id, "⚠️ Daily broadcast finished with issues:\n" + "\n".join(result.errors))
                interval = 86400 / job.times_per_day
                skipped = int((time.time() - next_run.timestamp()) // interval)
                if skipped > 0:
                    logger.warning("Daily job: broadcast took longer than %ds, skipped %d slot(s)", interval, skipped)
                    await self.notify(
                        owner_id,
                        f"⚠️ The daily broadcast took longer than the {int(interval)}s between slots, so "
                        f"{skipped} scheduled run(s) were skipped. Use a smaller delay or fewer runs per day.",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Daily job round failed")
                await asyncio.sleep(30)  # avoid a tight error loop
            await asyncio.sleep(1)  # never run the same slot twice if we woke a hair early
