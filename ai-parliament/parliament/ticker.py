"""Fon ishlari: Time Cop sessiyalari (har 30 soniyada) va Tutor'ning haftalik hisoboti (yakshanba).
Holat bazada saqlanadi — qayta deploy qilinganda ham sessiyalar yo'qolmaydi."""
import asyncio
import logging
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from parliament.config import Settings
from parliament.db import Database, FocusSession, utcnow
from parliament.delivery import send_turn
from parliament.hub import BotHub
from parliament.llm import LLMError
from parliament.orchestrator import Parliament

log = logging.getLogger(__name__)


async def finish_session(hub: BotHub, db: Database, parliament: Parliament, sess: FocusSession,
                         early: bool = False, via=None) -> None:
    await db.close_session(sess.id)
    head = ("Sessiya yopildi." if early else "🚨 Bo'ldi, vaqt tugadi!") + " Endi xulosa va qaror vaqti."
    await send_turn(hub, db, sess.chat_id, "timecop", head, via=via)
    try:
        summary = await parliament.summarize(sess.chat_id, since=sess.started_at, topic=sess.topic)
    except LLMError as e:
        log.warning("Sessiya xulosasi yaratilmadi: %s", e)
        summary = None
    if summary:
        await send_turn(hub, db, sess.chat_id, "mastermind", summary, via=via)


class Ticker:
    def __init__(self, hub: BotHub, db: Database, parliament: Parliament, s: Settings):
        self.hub, self.db, self.parliament, self.s = hub, db, parliament, s
        self.scheduler = AsyncIOScheduler(timezone=s.timezone)

    def start(self) -> None:
        self.scheduler.add_job(self.session_tick, IntervalTrigger(seconds=30), id="session_tick",
                               max_instances=1, coalesce=True, misfire_grace_time=30)
        self.scheduler.add_job(self.weekly_review, CronTrigger(day_of_week="sun", hour=19, minute=0),
                               id="weekly_review", max_instances=1, misfire_grace_time=3600)
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def session_tick(self) -> None:
        now = utcnow()
        for sess in await self.db.sessions_to_warn(now):
            try:
                left = max(1, int((sess.deadline_at - now).total_seconds() // 60) + 1)
                await send_turn(self.hub, self.db, sess.chat_id, "timecop",
                                f"⏳ {left} daqiqa qoldi! Xulosaga o'ting: asosiy qarorni hozir yozing.")
            except TelegramAPIError as e:
                log.warning("Ogohlantirish yuborilmadi (chat=%s): %s", sess.chat_id, e)
            await self.db.mark_warned(sess.id)
        for sess in await self.db.due_sessions(now):
            try:
                await finish_session(self.hub, self.db, self.parliament, sess)
            except TelegramAPIError as e:
                log.warning("Sessiyani yopib bo'lmadi (chat=%s): %s", sess.chat_id, e)
            except Exception:  # noqa: BLE001
                log.exception("Sessiya xatosi (chat=%s)", sess.chat_id)

    async def weekly_review(self) -> None:
        """Yakshanba 19:00: o'tgan haftada faol bo'lgan har bir chatga Tutor hisoboti (xato bo'lsa)."""
        for chat_id in await self.db.active_chats(utcnow() - timedelta(days=7)):
            try:
                review = await self.parliament.weekly_review(chat_id)
                if review:
                    await send_turn(self.hub, self.db, chat_id, "tutor", "📒 Haftalik hisobot\n\n" + review)
            except (LLMError, TelegramAPIError) as e:
                log.warning("Haftalik hisobot yuborilmadi (chat=%s): %s", chat_id, e)
            await asyncio.sleep(2)
