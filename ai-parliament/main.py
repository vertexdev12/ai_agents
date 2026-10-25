import asyncio
import logging
import os

from aiogram import Dispatcher
from aiogram.types import BotCommand

from parliament.agents import AGENTS
from parliament.bot import router
from parliament.config import settings
from parliament.db import Database
from parliament.hub import BotHub
from parliament.llm import build_llms
from parliament.orchestrator import Parliament
from parliament.ticker import Ticker

log = logging.getLogger("main")

COMMANDS = [
    BotCommand(command="agents", description="Agentlar ro'yxati"),
    BotCommand(command="session", description="Vaqt chegaralangan sessiya"),
    BotCommand(command="stop", description="Sessiyani tugatish"),
    BotCommand(command="xulosa", description="Suhbat xulosasi"),
    BotCommand(command="mistakes", description="Ingliz xatolari daftari"),
    BotCommand(command="notes", description="Agentlar eslab qolganlari"),
    BotCommand(command="status", description="Botlar holati"),
    BotCommand(command="llmtest", description="API kalitlarini tekshirish"),
    BotCommand(command="reset", description="Hammasini tozalash"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs("data", exist_ok=True)

    db = Database(settings.database_url)
    await db.init()

    hub = BotHub(settings)
    await hub.init()
    llms = build_llms(settings)
    parliament = Parliament(llms, db, settings)

    for key in AGENTS:
        log.info("%s: bot=%s, kalit=%s", key,
                 f"@{hub.username_of[key]}" if key in hub.username_of else "umumiy bot",
                 "alohida" if settings.agent_api_key(key) else "umumiy")

    await hub.primary.set_my_commands(COMMANDS)
    dp = Dispatcher(db=db, hub=hub, llms=llms, parliament=parliament)
    dp.include_router(router)

    ticker = Ticker(hub, db, parliament, settings)
    ticker.start()
    try:
        await dp.start_polling(*hub.all_bots)
    finally:
        ticker.shutdown()
        await hub.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
