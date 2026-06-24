import os
import signal
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    filters,
)

from bot.db.base import init_db
from bot.handlers.onboarding import get_onboarding_handler
from bot.handlers.profile import get_me_handler
from bot.handlers.food import get_food_handler
from bot.handlers.workout import get_new_workout_handler, get_log_workout_handler
from bot.handlers.commands import (
    get_today_handler, get_weight_handler, get_sleep_handler,
    get_steps_handler, get_week_handler, get_settings_handler,
    get_cancel_handler, get_export_handler,
)
from bot.scheduler.reminders import scheduler
from apscheduler.triggers.cron import CronTrigger
from bot.cache.redis_client import get_today_state, update_today_state
from bot.queue.throttle import debounce_message, is_rate_limited

load_dotenv()

ADMIN_ID = 5149883442

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Привет, {user.first_name}! Я твой строгий фитнес-бот.\n\n"
        "Я считаю всё: калории, белки, жиры, углеводы.\n"
        "Фото еды — распознаю. Текстом — тоже пойму.\n"
        "Тренировки, шаги, сон — всё в одном месте.\n\n"
        "Начни с /onboarding\n"
        "Команды: /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — приветствие\n"
        "/help — справка\n"
        "/onboarding — настройка профиля\n"
        "/me — мой профиль\n"
        "/today — сводка дня\n"
        "/log [текст] — записать еду\n"
        "/workout — лог тренировки\n"
        "/new_workout — создать программу\n"
        "/weight [кг] — обновить вес\n"
        "/sleep [отбой] [подъём] — записать сон\n"
        "/steps [n] — записать шаги\n"
        "/week — недельный отчёт\n"
        "/cancel — отменить последнее действие\n"
        "/export — экспорт данных за неделю (CSV)\n"
        "/settings — настройки\n\n"
        "📸 Отправь фото еды — распознаю\n"
        "💬 Просто пиши — я понимаю контекст!\n"
        "   «Съел 200г гречки в 9 утра»\n"
        "   «Лёг в 23, встал в 7»\n"
        "   «Прошёл 8000 шагов»\n"
        "   «Вес 85.5»"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    await update.message.reply_text("🔍 Распознаю...")

    from bot.ai.vision import analyze_photo
    result = await analyze_photo(bytes(photo_bytes))

    if not result:
        await update.message.reply_text(
            "Не распознал. Попробуй текстом: /log 200г гречки"
        )
        return

    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        await crud.add_meal_log(
            session, user_id=user.id,
            food_name=result["food_name"],
            weight_g=result["weight_g"],
            calories=result["calories"],
            protein=result["protein"],
            fat=result["fat"],
            carbs=result["carbs"],
            source="ai",
        )

    from bot.cache.redis_client import invalidate_context
    await invalidate_context(update.effective_user.id)

    await update_today_state(
        update.effective_user.id,
        calories_in=result["calories"],
        protein=result["protein"],
        fat=result["fat"],
        carbs=result["carbs"],
    )

    today = await get_today_state(update.effective_user.id)

    from bot.handlers.food import format_progress_bar, get_targets_for_user
    from bot.db.base import async_session as sess
    from bot.db import crud as c

    async with sess() as session:
        user = await c.get_user(session, update.effective_user.id)
    targets = await get_targets_for_user(user) if user else {"calories": 2000, "protein_g": 150}

    cal_pct = (today["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (today["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0

    await update.message.reply_text(
        f"📸 {result['food_name']} (~{result['weight_g']:.0f}г)\n\n"
        f"🔥 {result['calories']:.0f} ккал | 🥩 {result['protein']:.1f}г | "
        f"🧈 {result['fat']:.1f}г | 🍞 {result['carbs']:.1f}г\n\n"
        f"📊 За день: {today['calories_in']:.0f} / {targets['calories']} "
        f"{format_progress_bar(today['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
        f"🥩 Белок: {today['protein']:.0f} / {targets['protein_g']}г "
        f"{format_progress_bar(today['protein'], targets['protein_g'])} {prot_pct:.0f}%"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    chat = update.message.chat

    # Debounce: быстрые подряд-сообщения склеиваются в один запрос к ИИ (P1.8)
    await debounce_message(user_id, text, lambda uid, t: _send_ai_reply(chat, uid, t, context.bot))


_last_admin_alert: dict[int, float] = {}
ADMIN_ALERT_COOLDOWN = 300  # 5 минут


async def _send_ai_reply(chat, user_id: int, text: str, bot) -> None:
    """Реальный вызов ИИ с поддержанием 'печатает...' пока думает (P2.10)."""
    if is_rate_limited(user_id):
        await chat.send_message("⏳ Слишком много сообщений подряд, подожди немного.")
        return

    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await chat.send_action("typing")
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_typing.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue

    typing_task = asyncio.create_task(_keep_typing())
    try:
        from bot.ai.trainer import chat_with_trainer
        answer = await chat_with_trainer(user_id, text)
    finally:
        stop_typing.set()
        typing_task.cancel()

    if answer is None:
        # P3.14: admin alert
        import time
        now = time.monotonic()
        last = _last_admin_alert.get(user_id, 0)
        if now - last > ADMIN_ALERT_COOLDOWN:
            _last_admin_alert[user_id] = now
            try:
                await bot.send_message(ADMIN_ID, f"🔴 Оба ИИ-провайдера упали для user {user_id}")
            except Exception:
                pass
        await chat.send_message("⚠️ ИИ временно недоступен, попробуй позже.")
        return

    await chat.send_message(answer)


# ─── P3.15: Health check ────────────────────────────────────

async def health(request):
    from aiohttp import web
    return web.json_response({"status": "ok"})


async def start_health_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logger.info("Health check server started on :8080")


async def post_init(app):
    await init_db()
    scheduler.start()
    await start_health_server()

    from bot.scheduler.reminders import reset_all_today_states
    async def midnight_reset():
        await reset_all_today_states(app.bot)
    scheduler.add_job(midnight_reset, CronTrigger(hour=0, minute=5), id="midnight_reset", replace_existing=True)

    logger.info("Database initialized, scheduler started, health check running")


async def post_shutdown(app):
    from bot.cache.redis_client import redis
    if redis:
        await redis.close()
    logger.info("Graceful shutdown complete")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set in .env")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(get_onboarding_handler())
    app.add_handler(get_me_handler())

    app.add_handler(get_food_handler())
    app.add_handler(get_cancel_handler())
    app.add_handler(get_export_handler())

    app.add_handler(get_new_workout_handler())
    app.add_handler(get_log_workout_handler())

    app.add_handler(get_today_handler())
    app.add_handler(get_weight_handler())
    app.add_handler(get_sleep_handler())
    app.add_handler(get_steps_handler())
    app.add_handler(get_week_handler())
    app.add_handler(get_settings_handler())

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGTERM, signal.SIGINT),
    )


if __name__ == "__main__":
    main()
