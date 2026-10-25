"""Sozlamalar (.env yoki Railway Variables).

Har bir agent uchun ikkita ixtiyoriy o'zgaruvchi (agent kaliti katta harfda):
  MASTERMIND_BOT_TOKEN / MASTERMIND_API_KEY, TUTOR_BOT_TOKEN / TUTOR_API_KEY, CODE_..., TIMECOP_..., JOKER_..., SECURITY_...
Yangi agent qo'shilsa, config.py ga tegish shart emas.
"""
import os

from dotenv import dotenv_values
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_env() -> dict[str, str]:
    merged = {k.upper(): v for k, v in dotenv_values(".env").items() if v}
    merged.update({k.upper(): v for k, v in os.environ.items() if v})
    return merged


ENV = _load_env()


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    bot_token: str = ""                     # bitta bot rejimi (yoki MASTERMIND_BOT_TOKEN o'rniga)
    allowed_user_ids: str = ""
    allowed_chat_ids: str = ""              # ixtiyoriy: faqat shu guruhlarda ishlaydi
    database_url: str = "sqlite+aiosqlite:///./data/parliament.db"
    timezone: str = "Asia/Tashkent"

    # LLM
    llm_provider: str = "gemini"            # gemini | openai
    gemini_api_key: str = ""                # umumiy (zaxira) kalit — alohida kaliti yo'q agentlar shuni ishlatadi
    gemini_model: str = "gemini-3.5-flash-lite"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout: int = 60
    llm_min_interval: float = 1.0           # HAR BIR kalit uchun so'rovlar orasidagi minimal pauza (soniya)
    llm_concurrency: int = 3

    # Parlament
    history_messages: int = 20
    max_chorus: int = 2
    daily_message_limit: int = 200
    notes_update_every: int = 4             # agent shuncha marta gapirgach, o'z yozuvlarini yangilaydi
    voice_max_seconds: int = 120

    # Time Cop
    cop_after_minutes: int = 25
    cop_min_messages: int = 10
    cop_gap_minutes: int = 20
    cop_cooldown_minutes: int = 15
    cop_deadline_minutes: int = 10

    @field_validator("database_url")
    @classmethod
    def fix_pg_scheme(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # ---- yordamchi xossalar ----
    @property
    def allowed_set(self) -> set[int]:
        return _ids(self.allowed_user_ids)

    @property
    def allowed_chat_set(self) -> set[int]:
        return _ids(self.allowed_chat_ids)

    @property
    def shared_api_key(self) -> str:
        return self.openai_api_key if self.llm_provider.strip().lower() == "openai" else self.gemini_api_key

    def agent_token(self, key: str) -> str:
        return ENV.get(f"{key.upper()}_BOT_TOKEN", "")

    def agent_api_key(self, key: str) -> str:
        return ENV.get(f"{key.upper()}_API_KEY", "")

    @property
    def primary_token(self) -> str:
        return self.agent_token("mastermind") or self.bot_token


settings = Settings()
