"""Agent xabarlarini Telegram'ga yuborish: avval agentning o'z boti, bo'lmasa asosiy bot (sarlavha bilan)."""
import html
import logging

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from parliament.agents import AGENTS
from parliament.db import Database
from parliament.hub import BotHub

log = logging.getLogger(__name__)


def split_text(text: str, limit: int = 3500) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


async def send_turn(hub: BotHub, db: Database, chat_id: int, agent_key: str, text: str,
                    via: Bot | None = None) -> None:
    """Agent o'z boti nomidan yozadi (sarlavhasiz). Bot chatda bo'lmasa yoki foydalanuvchi uni ochmagan bo'lsa,
    `via` (yoki asosiy) bot orqali «emoji + nom» sarlavhasi bilan yuboriladi."""
    agent = AGENTS[agent_key]
    own = hub.own.get(agent_key)
    for i, chunk in enumerate(split_text(text.strip())):
        msg, sender = None, None
        if own is not None:
            try:
                msg, sender = await own.send_message(chat_id, html.escape(chunk), parse_mode=ParseMode.HTML), own
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                log.info("%s o'z boti orqali yubora olmadi (%s) — zaxira yo'l", agent_key, e)
        if msg is None:
            sender = via or hub.primary
            header = f"<b>{agent.emoji} {html.escape(agent.title)}</b>\n" if i == 0 else ""
            msg = await sender.send_message(chat_id, header + html.escape(chunk), parse_mode=ParseMode.HTML)
        await db.add_message(chat_id, agent_key, chunk, msg.message_id, bot_id=sender.id)
