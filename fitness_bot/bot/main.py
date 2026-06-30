import logging
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.config import BOT_TOKEN
from bot.handlers.commands import start_command, help_command, stats_command
from bot.handlers.messages import handle_message
from bot.handlers.onboarding import handle_onboarding
from bot.memory.scheduler import setup_scheduler
from bot.db.base import init_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def health_check():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Health check server started on port 8080")


async def main():
    await init_db()
    setup_scheduler()
    asyncio.create_task(health_check())

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))

    async def message_router(update, context):
        if context.user_data.get("onboarding"):
            await handle_onboarding(update, context)
        else:
            await handle_message(update, context)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    logger.info("Bot started")
    await application.run_polling(allowed_updates=["messages"])


if __name__ == "__main__":
    asyncio.run(main())
