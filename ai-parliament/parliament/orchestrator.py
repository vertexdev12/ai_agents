"""Parlament miyasi.

Har bir foydalanuvchi xabari uchun 2 ta LLM so'rovi:
  1) rejissyor (JSON): asosiy javob beruvchi (agar ko'rsatilmagan bo'lsa) + qolganlarning qisqa replikalari;
  2) asosiy agent: o'z xarakterida va O'Z API KALITI bilan batafsil javob.
Rejissyor so'rovlari barcha kalitlar o'rtasida navbat bilan taqsimlanadi (round-robin).
Qo'shimcha imkoniyatlar: Tutor xatolar daftari va ovozli baholash, Security uchun Google qidiruv,
har bir agentning shaxsiy yozuvlari, Time Cop majburiy aralashuvi.
"""
import asyncio
import itertools
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, Field

from parliament.agents import AGENTS, COMMON_RULES
from parliament.config import Settings
from parliament.db import Database, Message, utcnow
from parliament.llm import Audio, BaseLLM, LLMError

log = logging.getLogger(__name__)

DIRECTOR_SYSTEM = (
    "Sen \"AI parlament\" rejissyorisan: guruh chatini boshqarasan. Foydalanuvchi xabariga qarab kim asosiy javob "
    "berishini va qolgan a'zolardan kimlar qisqa replika qo'shishini hal qilasan. " + COMMON_RULES
)

CATEGORIES = {"grammar", "tense", "preposition", "article", "vocabulary", "spelling", "word_order", "other"}
EN_WORDS = {
    "the", "is", "are", "was", "were", "i", "you", "to", "a", "an", "and", "of", "in", "on", "it", "my", "have",
    "has", "do", "does", "did", "not", "for", "with", "this", "that", "what", "how", "why", "can", "will", "would",
    "be", "been", "at", "he", "she", "we", "they", "me", "your", "am", "but", "so", "if", "when", "go", "went",
}


def looks_english(text: str) -> bool:
    """Arzon filtr: matn inglizcha ko'rinadimi (o'zbekcha xabarlar uchun LLM so'rovini tejaydi)."""
    words = re.findall(r"[A-Za-z']+", text.lower())
    if len(text) < 15 or len(words) < 4:
        return False
    return sum(w in EN_WORDS for w in words) / len(words) >= 0.25


class ChorusLine(BaseModel):
    agent: str
    text: str


class Direction(BaseModel):
    lead: str
    chorus: list[ChorusLine] = Field(default_factory=list)


class MistakeItem(BaseModel):
    category: str = "other"
    wrong: str
    correct: str
    rule: str = ""


class MistakeList(BaseModel):
    is_english: bool = False
    mistakes: list[MistakeItem] = Field(default_factory=list)


class VoiceFeedback(BaseModel):
    transcript: str
    feedback: str
    mistakes: list[MistakeItem] = Field(default_factory=list)


@dataclass
class Turn:
    agent: str
    text: str


@dataclass
class Reply:
    turns: list[Turn] = field(default_factory=list)
    start_session_minutes: int | None = None          # Time Cop majburan aralashgan bo'lsa
    note_agents: list[str] = field(default_factory=list)  # yozuvlarini yangilashi kerak bo'lgan agentlar


class Parliament:
    def __init__(self, llms: dict[str, BaseLLM], db: Database, s: Settings):
        self.llms, self.db, self.s = llms, db, s
        self._pool = itertools.cycle(list({id(l): l for l in llms.values()}.values()))

    def llm_for(self, agent: str) -> BaseLLM:
        return self.llms[agent]

    # ---------- Yordamchilar ----------
    @staticmethod
    def _format_history(messages: list[Message]) -> str:
        if not messages:
            return "(hali suhbat yo'q)"
        lines = []
        for m in messages:
            if m.speaker == "user":
                who = m.user_name or "Foydalanuvchi"
            else:
                who = AGENTS[m.speaker].title if m.speaker in AGENTS else m.speaker
            lines.append(f"{who}: {m.content[:600]}")
        return "\n".join(lines)

    async def _run_stats(self, chat_id: int) -> tuple[int, int]:
        start, count = await self.db.user_run_stats(chat_id, self.s.cop_gap_minutes)
        minutes = int((utcnow() - start).total_seconds() // 60) if start else 0
        return minutes, count

    async def _cop_deadline(self, chat_id: int, minutes: int, count: int) -> int | None:
        if await self.db.active_session(chat_id):
            return None
        if minutes < self.s.cop_after_minutes or count + 1 < self.s.cop_min_messages:
            return None
        last = await self.db.last_speaker_time(chat_id, "timecop")
        if last and (utcnow() - last).total_seconds() / 60 < self.s.cop_cooldown_minutes:
            return None
        return self.s.cop_deadline_minutes

    async def _facts(self, chat_id: int, minutes: int, count: int) -> str:
        facts = f"Joriy suhbat: {minutes} daqiqa, {count + 1} ta foydalanuvchi xabari."
        sess = await self.db.active_session(chat_id)
        if sess:
            left = max(0, int((sess.deadline_at - utcnow()).total_seconds() // 60))
            facts += f" Faol sessiya: «{sess.topic or 'mavzusiz'}», qolgan vaqt: {left} daqiqa."
        return facts

    async def _mistake_digest(self, chat_id: int, days: int = 14) -> str:
        ms = await self.db.mistakes_since(chat_id, utcnow() - timedelta(days=days))
        if not ms:
            return ""
        lines = []
        for cat, n in Counter(m.category for m in ms).most_common(3):
            ex = next(m for m in reversed(ms) if m.category == cat)
            lines.append(f"- {cat} ({n} marta), masalan: «{ex.wrong}» -> «{ex.correct}»")
        return "\n".join(lines)

    # ---------- Rejissyor ----------
    async def _direct(self, history: str, user_text: str, forced: str | None, cop_minutes: int | None,
                      facts: str, user_name: str) -> Direction:
        members = "\n".join(f"- {a.key}: {a.title} — {a.chorus_hint}" for a in AGENTS.values())
        if forced:
            lead_rule = f'lead = "{forced}" (foydalanuvchi o\'zi tanlagan; o\'zgartirma va chorusga qo\'shma).'
        else:
            lead_rule = (
                "mavzuga eng mos a'zoni tanla: ingliz tili -> tutor; kod/SQL/baza -> code; firibgarlik, vahima, "
                "shubhali da'volar -> security; vaqt/rejalashtirish/cho'zilish -> timecop; hazil so'ralsa -> joker; "
                "umumiy yoki murakkab qaror -> mastermind."
            )
        cop_clause = ""
        if cop_minutes:
            cop_clause = (
                f"\nMUHIM: suhbat cho'zildi. Time Cop (timecop) gapirishi SHART: 'Bo'ldi, ko'p yuvordingiz, vaqt "
                f"ketdi!' ruhida to'xtatib, ANIQ muhlat belgilasin: {cop_minutes} daqiqa. Boshqa replika beruvchilar "
                f"buni qisqa qo'llab-quvvatlasin."
            )
        prompt = (
            f"A'ZOLAR:\n{members}\n\n"
            f"VAZIFA: (1) {lead_rule} (2) Qolgan a'zolardan faqat aytadigan chinakam gapi borlari 1-2 gaplik "
            f"(maks. 30 so'z) replika qo'shsin: 0 dan {self.s.max_chorus} tagacha (Time Cop majburiy bo'lsa — "
            f"{self.s.max_chorus + 1} tagacha). Replikalar foydalanuvchi xabariga qarab, a'zoning xarakteridan "
            f"chiqib yoziladi. Lead replika qo'shmaydi. Hech kim aytadigan gapi bo'lmasa, chorus bo'sh bo'lsin."
            f"{cop_clause}\n\nFAKTLAR: {facts}\n\nSUHBAT TARIXI:\n{history}\n\nYANGI XABAR ({user_name}): {user_text}"
        )
        return await next(self._pool).json(DIRECTOR_SYSTEM, prompt, Direction, max_tokens=600)

    def _clean_direction(self, d: Direction | None, forced: str | None, cop_minutes: int | None) -> Direction:
        lead = forced or (d.lead if d and d.lead in AGENTS else "mastermind")
        seen, chorus = {lead}, []
        for line in (d.chorus if d else []):
            text = (line.text or "").strip()
            if line.agent in AGENTS and line.agent not in seen and text:
                seen.add(line.agent)
                chorus.append(ChorusLine(agent=line.agent, text=text[:350]))
        if cop_minutes and lead != "timecop":
            cop = next((c for c in chorus if c.agent == "timecop"), None)
            if cop is None:
                cop = ChorusLine(agent="timecop", text="Bo'ldi, ko'p yuvordingiz, vaqt ketdi!")
                chorus.insert(0, cop)
            if str(cop_minutes) not in cop.text:
                cop.text = f"{cop.text.rstrip()} Muhlat: {cop_minutes} daqiqa."
        limit = self.s.max_chorus + (1 if cop_minutes else 0)
        return Direction(lead=lead, chorus=chorus[:limit])

    # ---------- Asosiy javob ----------
    async def _lead_answer(self, lead: str, chat_id: int, history: str, user_text: str, facts: str,
                           chorus: list[ChorusLine] | None, user_name: str) -> str:
        agent = AGENTS[lead]
        parts = [f"FAKTLAR: {facts}", f"SUHBAT TARIXI:\n{history}"]

        notes = await self.db.get_notes(chat_id)
        if lead == "mastermind":
            if notes:
                lines = "\n".join(f"- {AGENTS[k].title}: {v}" for k, v in notes.items() if k in AGENTS)
                parts.append(f"PARLAMENT A'ZOLARINING YOZUVLARI (foydalanuvchi haqida):\n{lines}")
        elif notes.get(lead):
            parts.append(f"SENING SHAXSIY YOZUVLARING (foydalanuvchi haqida):\n{notes[lead]}")
        if lead == "tutor":
            digest = await self._mistake_digest(chat_id)
            if digest:
                parts.append(f"FOYDALANUVCHINING SO'NGI 14 KUNDAGI TAKRORLANUVCHI XATOLARI:\n{digest}\n"
                             "Agar yangi xabarda shunday xato takrorlansa, buni qattiq ta'kidla.")
        if chorus:
            lines = "\n".join(f"- {AGENTS[c.agent].title}: {c.text}" for c in chorus)
            parts.append(f"PARLAMENT A'ZOLARINING QISQA FIKRLARI:\n{lines}")
        parts.append(f"YANGI XABAR ({user_name}): {user_text}")
        task = "Asosiy javob beruvchi sifatida o'z xarakteringda batafsil, lekin cho'zmasdan (taxminan 120-250 so'z) javob ber."
        if lead == "mastermind" and chorus:
            task += " A'zolarning fikrlarini hisobga olib, yakuniy xulosa va keyingi qadamlarni ayt."
        parts.append(f"VAZIFA: {task}")

        system, prompt, llm = f"{agent.persona}\n\n{COMMON_RULES}", "\n\n".join(parts), self.llm_for(lead)
        if lead == "security":  # faktlarni Google qidiruv bilan tekshiradi
            try:
                text, sources = await llm.text_with_sources(system, prompt, max_tokens=1200, search=True)
            except LLMError as e:
                log.warning("Qidiruvli so'rov ishlamadi, qidiruvsiz urinaman: %s", e)
                text, sources = await llm.text_with_sources(system, prompt, max_tokens=1200)
                text += "\n\n(Qidiruv hozir ishlamadi — javob tekshirilmagan bilimga asoslangan.)"
            if sources:
                text += "\n\nManbalar:\n" + "\n".join(f"• {s}" for s in sources)
            return text
        return await llm.text(system, prompt, max_tokens=1200)

    # ---------- Asosiy kirish nuqtasi ----------
    async def respond(self, chat_id: int, user_text: str, forced_lead: str | None,
                      user_name: str | None = None) -> Reply:
        name = user_name or "Foydalanuvchi"
        history = self._format_history(await self.db.recent_messages(chat_id, self.s.history_messages))
        minutes, count = await self._run_stats(chat_id)
        cop_minutes = None if forced_lead == "timecop" else await self._cop_deadline(chat_id, minutes, count)
        facts = await self._facts(chat_id, minutes, count)

        if forced_lead and forced_lead != "mastermind":
            d_res, a_res = await asyncio.gather(
                self._direct(history, user_text, forced_lead, cop_minutes, facts, name),
                self._lead_answer(forced_lead, chat_id, history, user_text, facts, None, name),
                return_exceptions=True,
            )
            if isinstance(a_res, BaseException):
                raise a_res
            if isinstance(d_res, BaseException):
                log.warning("Rejissyor ishlamadi: %s", d_res)
                d_res = None
            direction, answer = self._clean_direction(d_res, forced_lead, cop_minutes), a_res
        else:
            try:
                d_res = await self._direct(history, user_text, forced_lead, cop_minutes, facts, name)
            except LLMError as e:
                log.warning("Rejissyor ishlamadi: %s", e)
                d_res = None
            direction = self._clean_direction(d_res, forced_lead, cop_minutes)
            with_chorus = direction.chorus if direction.lead == "mastermind" else None
            answer = await self._lead_answer(direction.lead, chat_id, history, user_text, facts, with_chorus, name)

        reply = Reply(start_session_minutes=cop_minutes)
        chorus_turns = [Turn(c.agent, c.text) for c in direction.chorus]
        lead_turn = Turn(direction.lead, answer)
        cop_first = [t for t in chorus_turns if t.agent == "timecop"] if cop_minutes else []
        rest = [t for t in chorus_turns if t not in cop_first]
        reply.turns = (cop_first + rest + [lead_turn]) if direction.lead == "mastermind" else (cop_first + [lead_turn] + rest)

        for t in reply.turns:  # yozuvlarni yangilash navbati
            if await self.db.bump_agent_turn(chat_id, t.agent) >= self.s.notes_update_every:
                reply.note_agents.append(t.agent)
        return reply

    # ---------- Agent yozuvlari ----------
    async def update_note(self, chat_id: int, agent: str) -> None:
        """Agent o'z kalitida, foydalanuvchi haqidagi doimiy faktlarni qisqa yozuvga yangilaydi."""
        try:
            history = self._format_history(await self.db.recent_messages(chat_id, 14))
            old = (await self.db.get_notes(chat_id)).get(agent, "")
            prompt = (
                f"Oldingi yozuvlaring:\n{old or '(yo`q)'}\n\nSo'nggi suhbat:\n{history}\n\n"
                "VAZIFA: foydalanuvchi haqida DOIMIY ahamiyatli faktlarni (maqsadlar, daraja, loyihalar, qiziqishlar, "
                "takrorlanuvchi muammolar) o'z kasbing nuqtai nazaridan 100 so'zgacha yangilab yoz. Mayda gaplarni "
                "tashla. Parol, karta raqami, hujjat raqami kabi maxfiy ma'lumotlarni HECH QACHON yozma. "
                "Faqat yozuv matnini qaytar."
            )
            notes = await self.llm_for(agent).text(f"{AGENTS[agent].persona}\n\n{COMMON_RULES}", prompt, max_tokens=400)
            await self.db.set_note(chat_id, agent, notes[:1200])
        except LLMError as e:
            log.warning("%s yozuvlari yangilanmadi: %s", agent, e)

    # ---------- Tutor: xatolar daftari ----------
    async def _store_mistakes(self, chat_id: int, user_id: int | None, items: list[MistakeItem]) -> int:
        rows = []
        for m in items:
            wrong, correct = m.wrong.strip()[:300], m.correct.strip()[:300]
            if not wrong or not correct or wrong.lower() == correct.lower():
                continue
            cat = m.category.strip().lower()
            rows.append({"category": cat if cat in CATEGORIES else "other", "wrong": wrong,
                         "correct": correct, "rule": m.rule.strip()[:300]})
        return await self.db.add_mistakes(chat_id, user_id, rows)

    async def extract_mistakes(self, chat_id: int, user_id: int | None, text: str) -> int:
        """Inglizcha xabardagi xatolarni daftarga yozadi (Tutor'ning o'z kaliti bilan)."""
        if not looks_english(text):
            return 0
        prompt = (
            "Foydalanuvchi ingliz tilini A2 dan B2 ga o'rganmoqda. Quyidagi matndagi XATOLARNI top. "
            "category: grammar | tense | preposition | article | vocabulary | spelling | word_order | other. "
            "wrong — xato ibora, correct — to'g'ri variant, rule — 1 gaplik o'zbekcha qoida. "
            "Xato bo'lmasa yoki matn inglizcha bo'lmasa, mistakes bo'sh bo'lsin.\n\nMATN: " + text
        )
        try:
            res = await self.llm_for("tutor").json(AGENTS["tutor"].persona, prompt, MistakeList, max_tokens=600)
        except LLMError as e:
            log.warning("Xatolarni ajratib bo'lmadi: %s", e)
            return 0
        return await self._store_mistakes(chat_id, user_id, res.mistakes) if res.is_english or res.mistakes else 0

    async def voice_feedback(self, chat_id: int, user_id: int | None, audio: Audio) -> VoiceFeedback:
        """Ovozli xabar: transkript + qattiqqo'l baho. Xatolar daftarga yoziladi."""
        prompt = (
            "Bu foydalanuvchining ovozli xabari (ingliz tilini A2 dan B2 ga o'rganmoqda). Qaytar: "
            "1) transcript — so'zma-so'z yozuv; "
            "2) feedback — qattiqqo'l ustoz sifatida: grammatika/lug'at xatolari (xato -> to'g'ri), talaffuz va ravonlik "
            "haqidagi kuzatuvlaring (talaffuzni audiodan TAXMINIY baholayotganingni ochiq ayt), umumiy ball 10 dan va "
            "qayta aytish uchun 1 ta topshiriq; inglizcha qismlar sodda, tushuntirish o'zbekcha; 150 so'zgacha; "
            "3) mistakes — grammatik/lug'at xatolari ro'yxati. Agar ovozda ingliz tili bo'lmasa, eshitganingni "
            "transcriptga yoz va feedbackda inglizcha gapirishni talab qil."
        )
        fb = await self.llm_for("tutor").json(
            f"{AGENTS['tutor'].persona}\n\n{COMMON_RULES}", prompt, VoiceFeedback, max_tokens=900, audio=audio)
        await self._store_mistakes(chat_id, user_id, fb.mistakes)
        return fb

    async def transcribe(self, audio: Audio) -> str:
        return await self.llm_for("tutor").text(
            "Sen transkriptorsan.", "Ovozli xabarni so'zma-so'z matnga aylantir (qaysi tilda bo'lsa, shu tilda). "
            "Faqat matnning o'zini yoz.", max_tokens=600, audio=audio)

    async def weekly_review(self, chat_id: int, days: int = 7) -> str | None:
        ms = await self.db.mistakes_since(chat_id, utcnow() - timedelta(days=days))
        if not ms:
            return None
        cats = ", ".join(f"{c} ({n})" for c, n in Counter(m.category for m in ms).most_common())
        sample = "\n".join(f"- «{m.wrong}» -> «{m.correct}» ({m.rule})" for m in ms[-15:])
        prompt = (
            f"So'nggi {days} kundagi xatolar soni: {len(ms)}. Turlar: {cats}.\nNamunalar:\n{sample}\n\n"
            "VAZIFA: haftalik hisobot yoz: (1) eng ko'p takrorlangan 3 ta xato turi va sababi, (2) eng og'ir 3 ta "
            "xato (xato -> to'g'ri), (3) taraqqiyot haqida halol baho (qattiqqo'l, lekin adolatli), (4) 5 ta mashq gap: "
            "xatoli gaplar — foydalanuvchi tuzatsin (javoblarini yozma). 220 so'zgacha."
        )
        return await self.llm_for("tutor").text(f"{AGENTS['tutor'].persona}\n\n{COMMON_RULES}", prompt, max_tokens=900)

    # ---------- Xulosa ----------
    async def summarize(self, chat_id: int, since=None, topic: str = "") -> str | None:
        messages = (await self.db.messages_since(chat_id, since)) if since else (
            await self.db.recent_messages(chat_id, 30))
        if len(messages) < 2:
            return None
        task = (
            "Quyidagi suhbatning xulosasini chiqar: (1) asosiy xulosalar (3-5 band, qisqa), (2) qaror va keyingi "
            "qadamlar, (3) agar ingliz tili xatolari bo'lsa — Tutor nomidan eng muhim 1-2 saboq. Har band boshida «•»."
            + (f" Sessiya mavzusi: {topic}." if topic else "")
        )
        prompt = f"SUHBAT:\n{self._format_history(messages)}\n\nVAZIFA: {task}"
        return await self.llm_for("mastermind").text(
            f"{AGENTS['mastermind'].persona}\n\n{COMMON_RULES}", prompt, max_tokens=900)
