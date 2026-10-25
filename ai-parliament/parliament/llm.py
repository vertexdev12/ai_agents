"""LLM qatlami: Gemini yoki OpenAI. Har bir agent o'z API kaliti (o'z limiti) bilan ishlashi mumkin.
Parlament faqat text()/json() ni biladi — provayderni almashtirish uchun shu faylga tegish yetarli."""
import asyncio
import json as jsonlib
import logging
import re
import time
from typing import TypeVar

from pydantic import BaseModel

from parliament.agents import AGENTS
from parliament.config import Settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
RETRY_CODES = {429, 500, 502, 503, 504}
Audio = tuple[bytes, str]  # (bayt, mime)


class LLMError(Exception):
    """LLM javob bera olmadi (timeout, kvota, noto'g'ri model nomi va h.k.)."""


class BaseLLM:
    def __init__(self, s: Settings, api_key: str):
        self.s, self.api_key = s, api_key
        self._sem = asyncio.Semaphore(s.llm_concurrency)
        self._rl_lock = asyncio.Lock()
        self._last_call = 0.0

    async def _complete(self, system: str, prompt: str, max_tokens: int, schema: type[BaseModel] | None,
                        search: bool, audio: Audio | None) -> tuple[str, list[str]]:
        raise NotImplementedError

    async def _throttle(self) -> None:
        async with self._rl_lock:
            wait = self._last_call + self.s.llm_min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _run(self, make_call):
        """Pauza, timeout va vaqtinchalik xatolarda (429/5xx) qayta urinish."""
        last: Exception | None = None
        async with self._sem:
            for attempt in range(3):
                await self._throttle()
                try:
                    return await asyncio.wait_for(make_call(), timeout=self.s.llm_timeout)
                except asyncio.TimeoutError as e:
                    last, delay = e, 2 * (attempt + 1)
                except Exception as e:  # noqa: BLE001
                    code = getattr(e, "code", None) or getattr(e, "status_code", None)
                    if code not in RETRY_CODES:
                        raise LLMError(f"{type(e).__name__}: {e}") from e
                    last = e
                    delay = (3, 8, 15)[attempt] if code == 429 else 2 * (attempt + 1)
                await asyncio.sleep(delay)
        raise LLMError(f"LLM javob bermadi: {last}")

    async def text_with_sources(self, system: str, prompt: str, max_tokens: int = 1200, search: bool = False,
                                audio: Audio | None = None) -> tuple[str, list[str]]:
        out, sources = await self._complete(system, prompt, max_tokens, None, search, audio)
        out = (out or "").strip()
        if not out:
            raise LLMError("LLM bo'sh javob qaytardi")
        return out, sources

    async def text(self, system: str, prompt: str, max_tokens: int = 1200, search: bool = False,
                   audio: Audio | None = None) -> str:
        return (await self.text_with_sources(system, prompt, max_tokens, search, audio))[0]

    async def json(self, system: str, prompt: str, schema: type[T], max_tokens: int = 1200,
                   audio: Audio | None = None) -> T:
        hint = f"\n\nFaqat JSON qaytar, shu sxemaga mos:\n{jsonlib.dumps(schema.model_json_schema(), ensure_ascii=False)}"
        raw, _ = await self._complete(system, prompt + hint, max_tokens, schema, False, audio)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
        try:
            return schema.model_validate_json(raw)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"JSON noto'g'ri: {e}") from e


class GeminiLLM(BaseLLM):
    def __init__(self, s: Settings, api_key: str):
        super().__init__(s, api_key)
        from google import genai
        from google.genai import types

        self._types = types
        self.client = genai.Client(api_key=api_key)

    @staticmethod
    def _sources(resp) -> list[str]:
        try:
            out, seen = [], set()
            for c in resp.candidates[0].grounding_metadata.grounding_chunks or []:
                web = getattr(c, "web", None)
                if web and web.uri and web.uri not in seen:
                    seen.add(web.uri)
                    out.append(f"{web.title or 'manba'} — {web.uri}")
            return out[:3]
        except Exception:  # noqa: BLE001
            return []

    async def _complete(self, system, prompt, max_tokens, schema, search, audio):
        types = self._types
        # Yangi Gemini modellarida temperature/top_p eskirgan — yubormaymiz.
        # "Thinking" tokenlari ham max_output_tokens ichida — shuning uchun pastki chegara.
        cfg = {"system_instruction": system, "max_output_tokens": max(max_tokens, 1500)}
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            cfg["response_schema"] = schema
        if search:
            cfg["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        config = types.GenerateContentConfig(**cfg)
        if audio:
            data, mime = audio
            contents = [types.Content(role="user", parts=[types.Part.from_bytes(data=data, mime_type=mime),
                                                          types.Part(text=prompt)])]
        else:
            contents = prompt
        resp = await self._run(
            lambda: self.client.aio.models.generate_content(model=self.s.gemini_model, contents=contents, config=config)
        )
        return (resp.text or ""), (self._sources(resp) if search else [])


class OpenAILLM(BaseLLM):
    def __init__(self, s: Settings, api_key: str):
        super().__init__(s, api_key)
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)

    async def _complete(self, system, prompt, max_tokens, schema, search, audio):
        if audio:
            raise LLMError("Ovozni tahlil qilish hozircha faqat Gemini bilan ishlaydi (LLM_PROVIDER=gemini).")
        kwargs = {
            "model": self.s.openai_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_completion_tokens": max(max_tokens, 1500),
        }
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await self._run(lambda: self.client.chat.completions.create(**kwargs))
        return (resp.choices[0].message.content or ""), []   # OpenAI uchun qidiruv ulanmagan


def make_llm(s: Settings, api_key: str) -> BaseLLM:
    return OpenAILLM(s, api_key) if s.llm_provider.strip().lower() == "openai" else GeminiLLM(s, api_key)


def build_llms(s: Settings) -> dict[str, BaseLLM]:
    """Agent -> LLM. Alohida kaliti bor agentlar o'z limiti bilan ishlaydi; qolganlari umumiy kalitni baham ko'radi."""
    by_key: dict[str, BaseLLM] = {}
    out: dict[str, BaseLLM] = {}
    for agent in AGENTS:
        api_key = s.agent_api_key(agent) or s.shared_api_key
        if not api_key:
            raise RuntimeError(f"{agent} uchun API kalit yo'q: {agent.upper()}_API_KEY yoki umumiy kalitni kiriting")
        if api_key not in by_key:
            by_key[api_key] = make_llm(s, api_key)
        out[agent] = by_key[api_key]
    return out
