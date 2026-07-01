import logging
import asyncio
import traceback
from telegram import Bot, BotCommand, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.config import BOT_TOKEN, ADMIN_ID
from bot.handlers.commands import start_command, help_command, stats_command
from bot.handlers.messages import handle_message
from bot.handlers.onboarding import handle_onboarding
from bot.handlers.photos import handle_photo, handle_document_image
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


async def setup_bot_menu():
    bot = Bot(token=BOT_TOKEN)
    commands = [
        BotCommand("start", "Начать / онбординг"),
        BotCommand("help", "Как пользоваться ботом"),
        BotCommand("stats", "Моя статистика"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot menu commands set")


async def error_handler(update: object, context: Exception):
    logger.exception(f"Unhandled exception: {context.error}")
    if ADMIN_ID:
        try:
            from telegram import Bot
            bot = Bot(token=BOT_TOKEN)
            tb = traceback.format_exception(type(context.error), context.error, context.error.__traceback__)
            err_text = "".join(tb)[-3000:]
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=f"Bot error:\n{err_text}"
            )
        except Exception:
            pass


async def startup_self_check():
    from bot.db.base import engine
    from bot.cache.redis_client import get_redis
    from bot.config import GEMINI_API_KEY, USDA_API_KEY, GROQ_API_KEY

    checks = []

    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        checks.append("DB: OK")
    except Exception as e:
        checks.append(f"DB: FAIL ({e})")

    try:
        r = await get_redis()
        if r:
            await r.ping()
            checks.append("Redis: OK")
        else:
            checks.append("Redis: unavailable (file fallback active)")
    except Exception as e:
        checks.append(f"Redis: FAIL ({e})")

    if GEMINI_API_KEY:
        checks.append(f"Gemini: key present ({len(GEMINI_API_KEY)} chars)")
    else:
        checks.append("Gemini: NO KEY — photo recognition disabled")

    if GROQ_API_KEY:
        checks.append(f"Groq: key present ({len(GROQ_API_KEY)} chars)")
    else:
        checks.append("Groq: NO KEY — AI chat disabled")

    if USDA_API_KEY:
        checks.append(f"USDA: key present ({len(USDA_API_KEY)} chars)")
    else:
        checks.append("USDA: NO KEY — macro enrichment disabled")

    logger.info(f"Startup self-check: {'; '.join(checks)}")


def main():
    asyncio.run(_async_main())


async def _async_main():
    await init_db()
    setup_scheduler()
    asyncio.create_task(health_check())
    await setup_bot_menu()
    await startup_self_check()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))

    async def message_router(update, context):
        try:
            if context.user_data.get("onboarding"):
                await handle_onboarding(update, context)
            else:
                await handle_message(update, context)
        except Exception as e:
            logger.exception(f"Handler error: {e}")
            try:
                if update and update.message:
                    await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")
            except Exception:
                pass

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    logger.info("Bot started")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        stop_event = asyncio.Event()
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        from bot.cache.redis_client import get_redis
        r = await get_redis()
        if r:
            await r.aclose()
        from bot.db.base import engine
        await engine.dispose()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
