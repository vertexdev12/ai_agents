"""Telegram handlerlar (aiogram 3). Bitta Dispatcher BARCHA agent botlarini tinglaydi:
guruhda har bir xabarni oltita bot ko'radi, lekin `DedupeMiddleware` uni faqat bir marta qayta ishlatadi,
javoblar esa har bir agentning o'z boti nomidan yuboriladi."""
import asyncio
import io
import logging
from collections import OrderedDict, defaultdict
from datetime import date

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from parliament.agents import AGENTS, parse_mentions
from parliament.config import settings
from parliament.db import Database
from parliament.delivery import send_turn
from parliament.hub import BotHub
from parliament.llm import BaseLLM, LLMError
from parliament.orchestrator import Parliament, looks_english
from parliament.ticker import finish_session

log = logging.getLogger(__name__)
router = Router()

GROUPS = ("group", "supergroup")
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_usage: dict[int, tuple[date, int]] = {}
_seen: OrderedDict = OrderedDict()
_bg: set[asyncio.Task] = set()


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


# ---------- Middleware ----------
class DedupeMiddleware(BaseMiddleware):
    """Guruhda bir xil xabarni bir nechta bot oladi (privacy mode o'chiq bo'lsa) — faqat birinchisi ishlaydi."""

    async def __call__(self, handler, event: Message, data):
        if event.chat.type in GROUPS:
            key = (event.chat.id, event.message_id)
            if key in _seen:
                return None
            _seen[key] = None
            while len(_seen) > 2000:
                _seen.popitem(last=False)
        return await handler(event, data)


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        user = event.from_user
        if user is None or user.is_bot:
            return None
        in_group = event.chat.type in GROUPS
        if in_group and settings.allowed_chat_set and event.chat.id not in settings.allowed_chat_set:
            return None
        if user.id in settings.allowed_set or (event.text or "").startswith("/myid"):
            return await handler(event, data)
        if in_group:
            return None  # guruhda ruxsatsizlarga jim turamiz
        if not settings.allowed_set:
            await event.answer(
                f"⚙️ Bot hali sozlanmagan. Sizning Telegram ID'ingiz: {user.id}\n"
                f"Railway Variables (yoki .env) ga qo'shing: ALLOWED_USER_IDS={user.id}"
            )
        else:
            await event.answer(f"Bu bot yopiq. Sizning ID'ingiz: {user.id}")
        return None


router.message.outer_middleware(DedupeMiddleware())
router.message.outer_middleware(AccessMiddleware())


# ---------- Yordamchilar ----------
async def _typing(bot: Bot, chat_id: int) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(4)


def _within_limit(user_id: int) -> bool:
    today = date.today()
    d, n = _usage.get(user_id, (today, 0))
    n = n + 1 if d == today else 1
    _usage[user_id] = (today, n)
    return n <= settings.daily_message_limit


async def _resolve_lead(message: Message, bot: Bot, hub: BotHub, db: Database) -> tuple[str | None, str]:
    """Asosiy javob beruvchi: @mention > agent xabariga reply > (shaxsiy chatda) shu bot agenti."""
    forced, cleaned = parse_mentions(message.text or message.caption or "", hub.usernames)
    if forced:
        return forced, cleaned
    rt = message.reply_to_message
    if rt and rt.from_user and rt.from_user.is_bot:
        speaker = await db.speaker_of(message.chat.id, rt.message_id, rt.from_user.id)
        if speaker in AGENTS:
            return speaker, cleaned
        if rt.from_user.id in hub.agent_by_bot_id:
            return hub.agent_by_bot_id[rt.from_user.id], cleaned
    if message.chat.type == "private":
        dedicated = hub.dedicated_agent(bot)
        if dedicated:
            return dedicated, cleaned
    return None, cleaned


async def _handle(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament,
                  text: str, forced: str | None) -> None:
    chat_id, user = message.chat.id, message.from_user
    async with _locks[chat_id]:
        typing = asyncio.create_task(_typing(bot, chat_id))
        try:
            reply = await parliament.respond(chat_id, text, forced, user.first_name)
        except LLMError as e:
            log.warning("Parlament javob bera olmadi: %s", e)
            await message.answer("🏛 Parlament hozir ovoz bera olmayapti. Birozdan keyin qayta urinib ko'ring.")
            return
        finally:
            typing.cancel()

        await db.add_message(chat_id, "user", text, message.message_id, user_id=user.id, user_name=user.first_name)
        for i, turn in enumerate(reply.turns):
            if i:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(0.8)  # jonli guruh chati hissi
            await send_turn(hub, db, chat_id, turn.agent, turn.text, via=bot)
        if reply.start_session_minutes:
            await db.start_session(chat_id, reply.start_session_minutes, text[:80])

    if looks_english(text):
        spawn(parliament.extract_mistakes(chat_id, user.id, text))
    for agent in reply.note_agents:
        spawn(parliament.update_note(chat_id, agent))


# ---------- Buyruqlar ----------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    roster = "\n".join(f"{a.emoji} {a.title} — {a.blurb}" for a in AGENTS.values())
    await message.answer(
        "🏛 AI Parlamentga xush kelibsiz!\n\n"
        f"{roster}\n\n"
        "Murojaat: agent botining @username'i, yoki @tutor, @code, @cop, @joker, @security, @mastermind — "
        "o'sha batafsil javob beradi. Agent xabariga reply qilsangiz ham bo'ladi. Hech kimni belgilamasangiz, mos "
        "agent o'zi tanlanadi; qolganlar qisqa replika qo'shadi.\n\n"
        "🎤 Ovozli xabar yuborsangiz, Tutor ingliz tilingizni baholaydi.\n\n"
        "/session 25 Mavzu — vaqt chegaralangan sessiya\n/stop — sessiyani tugatish\n/xulosa — suhbat xulosasi\n"
        "/mistakes — xatolar daftari\n/notes — agentlar nimani eslab qolgan\n/status — botlar holati\n"
        "/agents — agentlar\n/reset — hammasini tozalash"
    )


@router.message(Command("agents"))
async def cmd_agents(message: Message) -> None:
    lines = [f"{a.emoji} {a.title}\n   {a.blurb}\n   Murojaat: " + ", ".join("@" + x for x in a.aliases)
             for a in AGENTS.values()]
    await message.answer("\n\n".join(lines))


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Sizning Telegram ID'ingiz: {message.from_user.id}\nChat ID: {message.chat.id}")


@router.message(Command("status"))
async def cmd_status(message: Message, hub: BotHub) -> None:
    in_group = message.chat.type in GROUPS
    lines = []
    for key, agent in AGENTS.items():
        own = hub.own.get(key)
        bot_txt = f"@{hub.username_of[key]}" if own and key in hub.username_of else "umumiy bot (zaxira)"
        key_txt = "alohida kalit" if settings.agent_api_key(key) else "umumiy kalit"
        where = ""
        if own and in_group:
            try:
                cm = await own.get_chat_member(message.chat.id, own.id)
                where = " ✅ guruhda" if cm.status in ("member", "administrator", "creator") else " ⚠️ guruhda emas"
            except Exception:  # noqa: BLE001
                where = " ⚠️ guruhda emas"
        lines.append(f"{agent.emoji} {agent.title}: {bot_txt}, {key_txt}{where}")
    await message.answer("🔧 Holat\n" + "\n".join(lines))


@router.message(Command("reset"))
async def cmd_reset(message: Message, db: Database) -> None:
    await db.clear_chat(message.chat.id)
    await message.answer("🧹 Suhbat tarixi, sessiyalar, xatolar daftari va agent yozuvlari tozalandi.")


@router.message(Command("session"))
async def cmd_session(message: Message, command: CommandObject, bot: Bot, hub: BotHub, db: Database) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit() or not (5 <= int(parts[0]) <= 180):
        await message.answer("Foydalanish: /session 25 SQL indekslari\n(5 dan 180 daqiqagacha; mavzu ixtiyoriy)")
        return
    minutes, topic = int(parts[0]), (parts[1] if len(parts) > 1 else "")
    await db.start_session(message.chat.id, minutes, topic)
    await send_turn(
        hub, db, message.chat.id, "timecop",
        f"Soat yurdi: {minutes} daqiqa" + (f", mavzu: «{topic}»" if topic else "") +
        ". Chalg'ima! 2 daqiqa qolganda ogohlantiraman, vaqt tugaganda Mastermind xulosa chiqaradi.", via=bot,
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament) -> None:
    sess = await db.active_session(message.chat.id)
    if not sess:
        await message.answer("Faol sessiya yo'q. Boshlash: /session 25 Mavzu")
        return
    await finish_session(hub, db, parliament, sess, early=True, via=bot)


@router.message(Command("xulosa"))
async def cmd_summary(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament) -> None:
    typing = asyncio.create_task(_typing(bot, message.chat.id))
    try:
        summary = await parliament.summarize(message.chat.id)
    except LLMError as e:
        log.warning("Xulosa yaratilmadi: %s", e)
        await message.answer("Hozir xulosa chiqara olmadim. Birozdan keyin urinib ko'ring.")
        return
    finally:
        typing.cancel()
    if summary:
        await send_turn(hub, db, message.chat.id, "mastermind", summary, via=bot)
    else:
        await message.answer("Xulosa chiqarish uchun suhbat hali juda qisqa.")


@router.message(Command("mistakes"))
async def cmd_mistakes(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament) -> None:
    typing = asyncio.create_task(_typing(bot, message.chat.id))
    try:
        review = await parliament.weekly_review(message.chat.id, days=30)
    except LLMError as e:
        log.warning("Hisobot yaratilmadi: %s", e)
        await message.answer("Hozir hisobot tayyorlay olmadim. Birozdan keyin urinib ko'ring.")
        return
    finally:
        typing.cancel()
    if review:
        await send_turn(hub, db, message.chat.id, "tutor", "📒 Xatolar daftari (30 kun)\n\n" + review, via=bot)
    else:
        await message.answer("Xatolar daftari hali bo'sh. Menga inglizcha yozing yoki ovozli xabar yuboring 😉")


@router.message(Command("notes"))
async def cmd_notes(message: Message, db: Database) -> None:
    notes = await db.get_notes(message.chat.id)
    if not notes:
        await message.answer("Agentlar hali hech narsa yozib olmagan (bir necha suhbatdan keyin paydo bo'ladi).")
        return
    text = "\n\n".join(f"{AGENTS[k].emoji} {AGENTS[k].title}:\n{v[:500]}" for k, v in notes.items() if k in AGENTS)
    await message.answer("📝 Agentlar sizning haqingizda eslab qolganlari (o'chirish: /reset)\n\n" + text)


async def _ping(llm: BaseLLM) -> tuple[bool, str]:
    try:
        return True, (await llm.text("Sen test botsan.", "Faqat 'ok' deb javob ber.", max_tokens=50))[:30]
    except LLMError as e:
        return False, str(e)[:160]


@router.message(Command("llmtest"))
async def cmd_llmtest(message: Message, llms: dict[str, BaseLLM]) -> None:
    groups: dict[int, tuple[BaseLLM, list[str]]] = {}
    for agent, llm in llms.items():
        groups.setdefault(id(llm), (llm, []))[1].append(agent)
    results = await asyncio.gather(*[_ping(llm) for llm, _ in groups.values()])
    model = settings.openai_model if settings.llm_provider.strip().lower() == "openai" else settings.gemini_model
    lines = [f"{'✅' if ok else '❌'} {', '.join(agents)} — {detail}"
             for (_, agents), (ok, detail) in zip(groups.values(), results)]
    await message.answer(f"🔌 {settings.llm_provider} / {model}\n" + "\n".join(lines))


# ---------- Asosiy suhbat ----------
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament) -> None:
    if not _within_limit(message.from_user.id):
        await message.answer("Bugungi xabarlar limiti tugadi. Ertaga davom etamiz 🙂")
        return
    forced, cleaned = await _resolve_lead(message, bot, hub, db)
    await _handle(message, bot, hub, db, parliament, cleaned or "(javob bering)", forced)


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot, hub: BotHub, db: Database, parliament: Parliament) -> None:
    chat_id, user, voice = message.chat.id, message.from_user, message.voice
    if (voice.duration or 0) > settings.voice_max_seconds:
        await message.answer(f"Ovozli xabar juda uzun. {settings.voice_max_seconds} soniyagacha yuboring 🙏")
        return
    if not _within_limit(user.id):
        await message.answer("Bugungi xabarlar limiti tugadi. Ertaga davom etamiz 🙂")
        return

    forced, _ = await _resolve_lead(message, bot, hub, db)
    typing = asyncio.create_task(_typing(bot, chat_id))
    routed_text: str | None = None
    try:
        buf = io.BytesIO()
        await bot.download(voice, destination=buf)
        audio = (buf.getvalue(), voice.mime_type or "audio/ogg")
        if forced and forced != "tutor":
            routed_text = await parliament.transcribe(audio)   # boshqa agentga reply qilingan ovoz — matnga aylanadi
        else:
            async with _locks[chat_id]:
                fb = await parliament.voice_feedback(chat_id, user.id, audio)
                await db.add_message(chat_id, "user", f"[Ovozli xabar] {fb.transcript}", message.message_id,
                                     user_id=user.id, user_name=user.first_name)
                await send_turn(hub, db, chat_id, "tutor", f"🎤 «{fb.transcript}»\n\n{fb.feedback}", via=bot)
            if await db.bump_agent_turn(chat_id, "tutor") >= settings.notes_update_every:
                spawn(parliament.update_note(chat_id, "tutor"))
    except LLMError as e:
        log.warning("Ovozni tahlil qilib bo'lmadi: %s", e)
        await message.answer("Ovozli xabarni tahlil qila olmadim 😔 Qayta yuboring yoki yozib yuboring.")
        return
    except Exception:  # noqa: BLE001
        log.exception("Ovoz bilan ishlashda xato")
        await message.answer("Ovozli xabarni olishda xato bo'ldi 😔 Qayta urinib ko'ring.")
        return
    finally:
        typing.cancel()

    if routed_text:
        await message.answer(f"🎤 {routed_text}")
        await _handle(message, bot, hub, db, parliament, routed_text, forced)
