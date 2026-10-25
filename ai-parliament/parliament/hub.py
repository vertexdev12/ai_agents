"""BotHub: har bir agent o'z Telegram boti bilan gapiradi. Tokeni yo'q agent umumiy (asosiy) bot orqali,
nomi sarlavhada ko'rsatilib gapiradi — shuning uchun tizimni bitta bot bilan boshlab, keyin kengaytirish mumkin."""
import logging

from aiogram import Bot

from parliament.agents import AGENTS
from parliament.config import Settings

log = logging.getLogger(__name__)


class BotHub:
    def __init__(self, s: Settings):
        if not s.primary_token:
            raise RuntimeError("MASTERMIND_BOT_TOKEN (yoki BOT_TOKEN) kiritilmagan")
        by_token: dict[str, Bot] = {}

        def get(token: str) -> Bot:
            if token not in by_token:
                by_token[token] = Bot(token)
            return by_token[token]

        self.primary: Bot = get(s.primary_token)
        self.own: dict[str, Bot] = {}                 # agent -> o'zining alohida boti
        for key in AGENTS:
            token = s.agent_token(key)
            if token:
                self.own[key] = get(token)
        self.all_bots: list[Bot] = list(by_token.values())
        self.agent_by_bot_id: dict[int, str] = {b.id: k for k, b in self.own.items()}
        self.usernames: dict[str, str] = {}           # bot username (kichik harf) -> agent
        self.username_of: dict[str, str] = {}         # agent -> bot username

    async def init(self) -> None:
        """Botlarni tekshiradi (get_me). Yaroqsiz token bo'lsa, o'sha agent umumiy bot orqali gapiradigan bo'ladi."""
        for key, bot in list(self.own.items()):
            try:
                me = await bot.get_me()
            except Exception as e:  # noqa: BLE001
                log.error("%s boti ishlamadi (token noto'g'ri?): %s — umumiy bot orqali gapiradi", key, e)
                self.own.pop(key)
                self.agent_by_bot_id.pop(bot.id, None)
                continue
            if me.username:
                self.usernames[me.username.lower()] = key
                self.username_of[key] = me.username
        # token ro'yxatidan chiqib ketgan botlar (yaroqsiz) polling'dan olib tashlanadi
        used = {self.primary.id} | {b.id for b in self.own.values()}
        self.all_bots = [b for b in self.all_bots if b.id in used]

    def dedicated_agent(self, bot: Bot) -> str | None:
        """Bu bot faqat bitta agentga tegishlimi (shaxsiy chatda to'g'ridan-to'g'ri murojaat uchun)."""
        return self.agent_by_bot_id.get(bot.id)

    async def close(self) -> None:
        for bot in self.all_bots:
            await bot.session.close()
