"""Ma'lumotlar bazasi: tarix, Time Cop sessiyalari, ingliz xatolari daftari, agent yozuvlari."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, delete, distinct, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    speaker: Mapped[str] = mapped_column(String(16))              # "user" yoki agent kaliti
    content: Mapped[str] = mapped_column(Text)
    tg_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bot_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)   # xabarni qaysi bot yuborgan/qabul qilgan
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FocusSession(Base):
    __tablename__ = "focus_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    topic: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    warned: Mapped[bool] = mapped_column(Boolean, default=False)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)


class Mistake(Base):
    """Tutor'ning xatolar daftari."""
    __tablename__ = "mistakes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    category: Mapped[str] = mapped_column(String(24), default="other")
    wrong: Mapped[str] = mapped_column(Text)
    correct: Mapped[str] = mapped_column(Text)
    rule: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentNote(Base):
    """Har bir agentning foydalanuvchi haqidagi shaxsiy yozuvlari (chat bo'yicha)."""
    __tablename__ = "agent_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    agent: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text, default="")
    turns: Mapped[int] = mapped_column(Integer, default=0)       # oxirgi yangilashdan beri gapirgan marta
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = create_async_engine(url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            if self.url.startswith("sqlite"):
                await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.run_sync(Base.metadata.create_all)
            await self._migrate(conn)

    async def _migrate(self, conn) -> None:
        """Eski bazaga yangi ustunlarni qo'shadi (create_all buni qilmaydi)."""
        wanted = {"user_id": "BIGINT", "user_name": "VARCHAR(64)", "bot_id": "BIGINT"}
        if self.url.startswith("sqlite"):
            res = await conn.exec_driver_sql("PRAGMA table_info(messages)")
            have = {row[1] for row in res.fetchall()}
            for col, typ in wanted.items():
                if col not in have:
                    await conn.exec_driver_sql(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
        else:
            for col, typ in wanted.items():
                await conn.exec_driver_sql(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col} {typ}")

    async def close(self) -> None:
        await self.engine.dispose()

    # ---------- Xabarlar ----------
    async def add_message(self, chat_id: int, speaker: str, content: str, tg_message_id: int | None = None,
                          bot_id: int | None = None, user_id: int | None = None, user_name: str | None = None) -> None:
        async with self.session_factory() as s:
            s.add(Message(chat_id=chat_id, speaker=speaker, content=content, tg_message_id=tg_message_id,
                          bot_id=bot_id, user_id=user_id, user_name=(user_name or None) and user_name[:64]))
            await s.commit()

    async def recent_messages(self, chat_id: int, n: int) -> list[Message]:
        stmt = select(Message).where(Message.chat_id == chat_id).order_by(Message.id.desc()).limit(n)
        async with self.session_factory() as s:
            rows = list((await s.execute(stmt)).scalars().all())
        return rows[::-1]

    async def messages_since(self, chat_id: int, since: datetime, limit: int = 60) -> list[Message]:
        stmt = (select(Message).where(Message.chat_id == chat_id, Message.created_at >= since)
                .order_by(Message.id).limit(limit))
        async with self.session_factory() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def speaker_of(self, chat_id: int, tg_message_id: int, bot_id: int | None = None) -> str | None:
        """Reply qilingan Telegram xabari qaysi agentniki ekanini aniqlaydi."""
        stmt = select(Message.speaker).where(Message.chat_id == chat_id, Message.tg_message_id == tg_message_id)
        if bot_id is not None:
            stmt = stmt.where(Message.bot_id == bot_id)
        async with self.session_factory() as s:
            return (await s.execute(stmt)).scalars().first()

    async def user_run_stats(self, chat_id: int, gap_minutes: int) -> tuple[datetime | None, int]:
        stmt = (select(Message.created_at).where(Message.chat_id == chat_id, Message.speaker == "user")
                .order_by(Message.id.desc()).limit(80))
        async with self.session_factory() as s:
            times = list((await s.execute(stmt)).scalars().all())
        run, prev = [], utcnow()
        for t in times:
            if prev - t > timedelta(minutes=gap_minutes):
                break
            run.append(t)
            prev = t
        return (run[-1], len(run)) if run else (None, 0)

    async def last_speaker_time(self, chat_id: int, speaker: str) -> datetime | None:
        stmt = (select(Message.created_at).where(Message.chat_id == chat_id, Message.speaker == speaker)
                .order_by(Message.id.desc()).limit(1))
        async with self.session_factory() as s:
            return (await s.execute(stmt)).scalars().first()

    async def active_chats(self, since: datetime) -> list[int]:
        stmt = select(distinct(Message.chat_id)).where(Message.speaker == "user", Message.created_at >= since)
        async with self.session_factory() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def clear_chat(self, chat_id: int) -> None:
        async with self.session_factory() as s:
            for model in (Message, FocusSession, Mistake, AgentNote):
                await s.execute(delete(model).where(model.chat_id == chat_id))
            await s.commit()

    # ---------- Sessiyalar ----------
    async def active_session(self, chat_id: int) -> FocusSession | None:
        stmt = (select(FocusSession).where(FocusSession.chat_id == chat_id, FocusSession.closed.is_(False))
                .order_by(FocusSession.id.desc()).limit(1))
        async with self.session_factory() as s:
            return (await s.execute(stmt)).scalars().first()

    async def start_session(self, chat_id: int, minutes: int, topic: str) -> FocusSession:
        async with self.session_factory() as s:
            await s.execute(update(FocusSession)
                            .where(FocusSession.chat_id == chat_id, FocusSession.closed.is_(False)).values(closed=True))
            now = utcnow()
            sess = FocusSession(chat_id=chat_id, topic=topic[:200], started_at=now,
                                deadline_at=now + timedelta(minutes=minutes))
            s.add(sess)
            await s.commit()
            return sess

    async def close_session(self, session_id: int) -> None:
        async with self.session_factory() as s:
            await s.execute(update(FocusSession).where(FocusSession.id == session_id).values(closed=True))
            await s.commit()

    async def mark_warned(self, session_id: int) -> None:
        async with self.session_factory() as s:
            await s.execute(update(FocusSession).where(FocusSession.id == session_id).values(warned=True))
            await s.commit()

    async def due_sessions(self, now: datetime) -> list[FocusSession]:
        stmt = select(FocusSession).where(FocusSession.closed.is_(False), FocusSession.deadline_at <= now)
        async with self.session_factory() as s:
            return list((await s.execute(stmt)).scalars().all())

    async def sessions_to_warn(self, now: datetime, lead_minutes: int = 2) -> list[FocusSession]:
        stmt = select(FocusSession).where(
            FocusSession.closed.is_(False), FocusSession.warned.is_(False),
            FocusSession.deadline_at > now, FocusSession.deadline_at <= now + timedelta(minutes=lead_minutes))
        async with self.session_factory() as s:
            return list((await s.execute(stmt)).scalars().all())

    # ---------- Xatolar daftari ----------
    async def add_mistakes(self, chat_id: int, user_id: int | None, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self.session_factory() as s:
            for r in rows:
                s.add(Mistake(chat_id=chat_id, user_id=user_id, **r))
            await s.commit()
        return len(rows)

    async def mistakes_since(self, chat_id: int, since: datetime) -> list[Mistake]:
        stmt = select(Mistake).where(Mistake.chat_id == chat_id, Mistake.created_at >= since).order_by(Mistake.id)
        async with self.session_factory() as s:
            return list((await s.execute(stmt)).scalars().all())

    # ---------- Agent yozuvlari ----------
    async def get_notes(self, chat_id: int) -> dict[str, str]:
        stmt = select(AgentNote).where(AgentNote.chat_id == chat_id)
        async with self.session_factory() as s:
            return {n.agent: n.notes for n in (await s.execute(stmt)).scalars().all() if n.notes}

    async def _note_row(self, s, chat_id: int, agent: str) -> AgentNote:
        row = (await s.execute(select(AgentNote).where(AgentNote.chat_id == chat_id, AgentNote.agent == agent))
               ).scalars().first()
        if row is None:
            row = AgentNote(chat_id=chat_id, agent=agent, notes="", turns=0)
            s.add(row)
        return row

    async def bump_agent_turn(self, chat_id: int, agent: str) -> int:
        async with self.session_factory() as s:
            row = await self._note_row(s, chat_id, agent)
            row.turns = (row.turns or 0) + 1
            await s.commit()
            return row.turns

    async def set_note(self, chat_id: int, agent: str, notes: str) -> None:
        async with self.session_factory() as s:
            row = await self._note_row(s, chat_id, agent)
            row.notes, row.turns, row.updated_at = notes, 0, utcnow()
            await s.commit()
