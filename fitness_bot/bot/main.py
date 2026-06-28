import os
import time
import signal
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from bot.db.base import init_db
from bot.handlers.onboarding import get_onboarding_handler, onb_restart_callback
from bot.handlers.profile import get_me_handler
from bot.handlers.food import get_food_handler
from bot.handlers.workout import get_new_workout_handler, get_log_workout_handler
from bot.handlers.commands import (
    get_today_handler, get_weight_handler, get_sleep_handler,
    get_steps_handler, get_week_handler, get_settings_handler,
    get_cancel_handler, get_export_handler,
    get_progress_handler, get_suggest_handler,
    today, cancel_command,
)
from bot.handlers.admin import admin_entry, admin_callback, admin_text_input, _is_admin
from bot.scheduler.reminders import scheduler
from apscheduler.triggers.cron import CronTrigger
from bot.cache.redis_client import get_today_state, update_today_state
from bot.queue.throttle import is_rate_limited
from bot.config import ADMIN_ID

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INTENT_KEYWORDS = {
    "today": ["сколько калорий", "сколько я съел", "мои калории", "баланс калорий",
              "сколько осталось", "что я ел", "что я съел", "мой прогресс"],
    "cancel": ["отмени", "удали последнее", "убери последний", "отмена записи"],
    "weight_query": ["мой вес", "сколько я вешу", "текущий вес"],
    "steps_query": ["мои шаги", "шаги сегодня", "сколько прошёл"],
}


def _match_hardcoded_intent(text: str) -> str | None:
    low = text.lower()
    for intent, phrases in INTENT_KEYWORDS.items():
        if any(p in low for p in phrases):
            return intent
    return None


async def _get_proactive_suggestion(user_id: int) -> str | None:
    from datetime import datetime, UTC
    from bot.db.base import async_session
    from bot.db import crud
    from bot.calculators.tdee import bmr as _bmr, tdee as _tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return None
        today_workout = await crud.get_today_workout(session, user.id)
        last_sleep = await crud.get_last_sleep(session, user.id)

    state = await get_today_state(user_id)
    bmr_val = _bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = _tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    suggestions = []

    if state["calories_in"] == 0:
        suggestions.append(("high", "Ты сегодня ещё ничего не ел. Запиши приём пищи."))
    if state["protein"] < targets["protein_g"] * 0.5:
        deficit = targets["protein_g"] - state["protein"]
        suggestions.append(("high", f"Белка мало! Нужно ещё ~{deficit:.0f}г. Добавь творог, курицу или яйца."))
    if not today_workout:
        suggestions.append(("medium", "Сегодня ещё не тренировался."))
    if not last_sleep or (datetime.now(UTC).replace(tzinfo=None) - last_sleep.date.replace(tzinfo=None)).days > 1:
        suggestions.append(("medium", "Нет данных о сне за последние дни."))
    if state["steps"] == 0:
        suggestions.append(("low", "Шагов пока 0. Прошёл немного — напиши число."))

    if not suggestions:
        return None

    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda x: priority_order[x[0]])

    lines = [s[1] for s in suggestions[:3]]
    return "Что можно сделать:\n" + "\n".join(lines)


# ─── Start / Menu ──────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    rows = [
        [InlineKeyboardButton("📊 Сегодня", callback_data="menu_today"),
         InlineKeyboardButton("🍽 Записать еду", callback_data="menu_food")],
        [InlineKeyboardButton("🏋️ Тренировка", callback_data="menu_workout"),
         InlineKeyboardButton("⚖️ Вес", callback_data="menu_weight")],
        [InlineKeyboardButton("📈 Графики", callback_data="menu_charts"),
         InlineKeyboardButton("📅 Неделя", callback_data="menu_week")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
         InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        [InlineKeyboardButton("💬 Спросить тренера", callback_data="menu_chat")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Админ-панель", callback_data="menu_admin")])

    kb = InlineKeyboardMarkup(rows)
    await update.message.reply_text(
        f"Привет, {update.effective_user.first_name}!",
        reply_markup=kb,
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    rows = [
        [InlineKeyboardButton("📊 Сегодня", callback_data="menu_today"),
         InlineKeyboardButton("🍽 Записать еду", callback_data="menu_food")],
        [InlineKeyboardButton("🏋️ Тренировка", callback_data="menu_workout"),
         InlineKeyboardButton("⚖️ Вес", callback_data="menu_weight")],
        [InlineKeyboardButton("📈 Графики", callback_data="menu_charts"),
         InlineKeyboardButton("📅 Неделя", callback_data="menu_week")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
         InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        [InlineKeyboardButton("💬 Спросить тренера", callback_data="menu_chat")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Админ-панель", callback_data="menu_admin")])

    await update.message.reply_text("Меню:", reply_markup=InlineKeyboardMarkup(rows))


# ─── Menu callback router ──────────────────────────────────

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "menu_main":
        await _show_main_menu(q, q.from_user.id)

    elif d == "menu_today":
        await today(update, context)
        kb = _menu_back_kb()
        await q.message.reply_text("Сводка выше", reply_markup=kb)

    elif d == "menu_food":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📷 Фото еды", callback_data="menu_food_photo")],
            [InlineKeyboardButton("✏️ Текстом", callback_data="menu_food_text")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
        ])
        await q.edit_message_text(
            "Как записать еду?\n\n"
            "Отправь фото — распознаю\n"
            "Или напиши текстом:",
            reply_markup=kb,
        )

    elif d == "menu_food_photo":
        await q.edit_message_text("Отправь фото еды:")
        context.user_data["awaiting_photo"] = True

    elif d == "menu_food_text":
        await q.edit_message_text(
            "Введи что съел:\n\n"
            "Примеры:\n"
            "- 200г гречки с курицей\n"
            "- съел 150г творога\n"
            "- поел кашу 300г",
        )

    elif d == "menu_workout":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Начать тренировку", callback_data="menu_workout_log")],
            [InlineKeyboardButton("➕ Создать программу", callback_data="menu_workout_new")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
        ])
        await q.edit_message_text("Тренировки:", reply_markup=kb)

    elif d == "menu_workout_log":
        await q.edit_message_text("Начни тренировку: /workout")

    elif d == "menu_workout_new":
        await q.edit_message_text("Создай программу: /new_workout")

    elif d == "menu_sleep":
        await q.edit_message_text(
            "Запиши сон:\n\n"
            "Формат: /sleep 23:00 07:00\n"
            "Где первый параметр — когда лег, второй — когда встал",
        )

    elif d == "menu_steps":
        await q.edit_message_text(
            "Запиши шаги:\n\n"
            "Формат: /steps 8000\n"
            "Или просто напиши: прошёл 8000",
        )

    elif d == "menu_weight":
        await q.edit_message_text(
            "Обнови вес:\n\n"
            "Формат: /weight 85.5\n"
            "Или просто напиши: вес 85.5",
        )

    elif d == "menu_charts":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏋️ Вес и тренировки", callback_data="chart_workout_weight")],
            [InlineKeyboardButton("🔥 Калории (баланс)", callback_data="chart_calories")],
            [InlineKeyboardButton("😴 Сон", callback_data="chart_sleep")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
        ])
        await q.edit_message_text("Какой график показать?", reply_markup=kb)

    elif d == "chart_workout_weight":
        await _send_workout_weight_chart(update, context)

    elif d == "chart_calories":
        await _send_calories_chart(update, context)

    elif d == "chart_sleep":
        await _send_sleep_chart(update, context)

    elif d.startswith("menu_prog_"):
        days = int(d.split("_")[2])
        from bot.handlers.commands import progress
        context.args = [str(days)]
        await progress(update, context)
        kb = _menu_back_kb()
        await q.message.reply_text(f"Графики за {days} дн.", reply_markup=kb)

    elif d == "menu_progress":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 7 дней", callback_data="menu_prog_7")],
            [InlineKeyboardButton("📅 14 дней", callback_data="menu_prog_14")],
            [InlineKeyboardButton("📅 30 дней", callback_data="menu_prog_30")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
        ])
        await q.edit_message_text("Графики за период:", reply_markup=kb)

    elif d == "menu_suggest":
        from bot.handlers.commands import suggest
        await suggest(update, context)
        kb = _menu_back_kb()
        await q.message.reply_text("Рекомендации выше", reply_markup=kb)

    elif d == "menu_week":
        from bot.handlers.commands import week
        await week(update, context)
        kb = _menu_back_kb()
        await q.message.reply_text("Неделя выше", reply_markup=kb)

    elif d == "menu_me":
        from bot.handlers.profile import me
        await me(update, context)
        kb = _menu_back_kb()
        await q.message.reply_text("Профиль выше", reply_markup=kb)

    elif d == "menu_settings":
        await q.edit_message_text("Настройки: /settings")

    elif d == "menu_help":
        kb = _menu_back_kb()
        await q.message.reply_text(
            "Команды:\n"
            "/onboarding - настройка профиля\n"
            "/today - сводка за сегодня\n"
            "/weight [кг] - обновить вес\n"
            "/sleep [отбой] [подъём] - записать сон\n"
            "/steps [n] - записать шаги\n"
            "/progress [дней] - графики\n"
            "/week - неделя\n"
            "/suggest - что тренировать\n"
            "/me - мой профиль\n"
            "/settings - настройки\n"
            "/export - экспорт CSV\n\n"
            "Просто пиши текстом — понимаю без команд.",
            reply_markup=kb,
        )

    elif d == "menu_chat":
        await q.edit_message_text(
            "Просто напиши сообщение — отвечу как тренер.\n\n"
            "Примеры:\n"
            "- Как дела? — покажу статус\n"
            "- Что тренировать? — подскажу\n"
            "- Сколько белка осталось? — посчитаю",
        )

    elif d == "menu_admin":
        await admin_callback(update, context)


async def _show_main_menu(q, user_id: int) -> None:
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    rows = [
        [InlineKeyboardButton("📊 Сегодня", callback_data="menu_today"),
         InlineKeyboardButton("🍽 Записать еду", callback_data="menu_food")],
        [InlineKeyboardButton("🏋️ Тренировка", callback_data="menu_workout"),
         InlineKeyboardButton("⚖️ Вес", callback_data="menu_weight")],
        [InlineKeyboardButton("📈 Графики", callback_data="menu_charts"),
         InlineKeyboardButton("📅 Неделя", callback_data="menu_week")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
         InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
        [InlineKeyboardButton("💬 Спросить тренера", callback_data="menu_chat")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Админ-панель", callback_data="menu_admin")])

    await q.edit_message_text("Меню:", reply_markup=InlineKeyboardMarkup(rows))


def _menu_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Назад", callback_data="menu_main")]
    ])


# ─── Charts ────────────────────────────────────────────────

async def _send_workout_weight_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from datetime import datetime, UTC, timedelta
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.message.reply_text("Сначала /onboarding")
            return
        weight_history = await crud.get_weight_history(session, user.id, days=30)
        workouts = await crud.get_workout_logs_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=30), datetime.now(UTC)
        )

    if not weight_history and not workouts:
        await q.message.reply_text("Недостаточно данных для графика.")
        return

    from bot.calculators.charts import workout_weight_chart
    buf = workout_weight_chart(
        [w.date for w in weight_history],
        [w.weight_kg for w in weight_history],
        [w.date for w in workouts],
        [w.total_volume or 0 for w in workouts],
    )
    await q.message.reply_photo(photo=buf, caption="Вес и объём тренировок за 30 дней")
    kb = _menu_back_kb()
    await q.message.reply_text("Назад в меню:", reply_markup=kb)


async def _send_calories_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from datetime import datetime, UTC, timedelta
    from bot.db.base import async_session
    from bot.db import crud
    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.message.reply_text("Сначала /onboarding")
            return
        meals = await crud.get_meals_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=14), datetime.now(UTC)
        )

    if not meals:
        await q.message.reply_text("Недостаточно данных для графика.")
        return

    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    target_cal = targets["calories"]

    daily_totals = {}
    for m in meals:
        day = m.date.date()
        daily_totals[day] = daily_totals.get(day, 0) + m.calories

    days_sorted = sorted(daily_totals.keys())
    deficits = [target_cal - daily_totals[d] for d in days_sorted]

    from bot.calculators.charts import deficit_chart
    buf = deficit_chart(days_sorted, deficits)
    await q.message.reply_photo(photo=buf, caption="Баланс калорий за 14 дней")
    kb = _menu_back_kb()
    await q.message.reply_text("Назад в меню:", reply_markup=kb)


async def _send_sleep_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from datetime import datetime, UTC, timedelta
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.message.reply_text("Сначала /onboarding")
            return
        sleep_logs = await crud.get_sleep_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=14), datetime.now(UTC)
        )

    if not sleep_logs:
        await q.message.reply_text("Недостаточно данных для графика.")
        return

    from bot.calculators.charts import sleep_chart
    buf = sleep_chart(
        [s.date for s in sleep_logs],
        [s.duration_hours for s in sleep_logs],
    )
    await q.message.reply_photo(photo=buf, caption="Динамика сна за 14 дней")
    kb = _menu_back_kb()
    await q.message.reply_text("Назад в меню:", reply_markup=kb)


# ─── Help ──────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Все команды бота:\n\n"
        "/menu — главное меню\n"
        "/onboarding — настройка профиля\n"
        "/me — мой профиль\n"
        "/today — сводка за сегодня\n"
        "/weight [кг] — обновить вес\n"
        "/sleep [отбой] [подъём] — записать сон\n"
        "/steps [n] — записать шаги\n"
        "/progress [дней] — графики\n"
        "/week — неделя\n"
        "/suggest — что тренировать\n"
        "/settings — настройки\n"
        "/export — экспорт CSV\n\n"
        "Просто пиши текстом — понимаю без команд."
    )


# ─── Photo handler ─────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_bytes = await file.download_as_bytearray()

    await update.message.reply_text("Распознаю...")

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

    today_state = await get_today_state(update.effective_user.id)

    from bot.handlers.food import format_progress_bar, get_targets_for_user

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
    targets = await get_targets_for_user(user) if user else {"calories": 2000, "protein_g": 150}

    cal_pct = (today_state["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (today_state["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0

    await update.message.reply_text(
        f"{result['food_name']} (~{result['weight_g']:.0f}г)\n\n"
        f"{result['calories']:.0f} ккал | {result['protein']:.1f}г бел | "
        f"{result['fat']:.1f}г жир | {result['carbs']:.1f}г угл\n\n"
        f"За день: {today_state['calories_in']:.0f} / {targets['calories']} "
        f"{format_progress_bar(today_state['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
        f"Белок: {today_state['protein']:.0f} / {targets['protein_g']}г "
        f"{format_progress_bar(today_state['protein'], targets['protein_g'])} {prot_pct:.0f}%"
    )


# ─── Text handler ──────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    chat = update.message.chat

    if await admin_text_input(update, context):
        return

    intent = _match_hardcoded_intent(text)
    if intent == "today":
        await today(update, context)
        return
    if intent == "cancel":
        await cancel_command(update, context)
        return
    if intent == "weight_query":
        from bot.db.base import async_session
        from bot.db import crud
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
        if user:
            await chat.send_message(f"Твой текущий вес: {user.weight_kg} кг")
            return
    if intent == "steps_query":
        state = await get_today_state(user_id)
        await chat.send_message(f"Шаги сегодня: {state['steps']}")
        return

    from bot.ai.analyzer import analyze_message
    from bot.calculators.tdee import bmr as _bmr, tdee as _tdee
    from bot.calculators.nutrition import daily_targets
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await chat.send_message("Сначала /onboarding")
            return

    bmr_val = _bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = _tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    state = await get_today_state(user_id)

    analysis = analyze_message(text, state, targets)

    if analysis["response"]:
        await chat.send_message(analysis["response"])
        return

    if analysis["action"]:
        action = analysis["action"]
        if analysis["intent"] == "food":
            from bot.handlers.food import search_food
            food_data = await search_food(action["food_name"])
            if food_data:
                factor = action["weight_g"] / 100
                async with async_session() as session:
                    user_db = await crud.get_user(session, user_id)
                    if user_db:
                        await crud.add_meal_log(
                            session, user_id=user_db.id,
                            food_name=action["food_name"], weight_g=action["weight_g"],
                            calories=food_data["calories"] * factor,
                            protein=food_data["protein"] * factor,
                            fat=food_data["fat"] * factor,
                            carbs=food_data["carbs"] * factor,
                            source="ai",
                        )
                        await invalidate_context(user_id)
                        from bot.cache.redis_client import update_today_state
                        await update_today_state(
                            user_id,
                            calories_in=food_data["calories"] * factor,
                            protein=food_data["protein"] * factor,
                            fat=food_data["fat"] * factor,
                            carbs=food_data["carbs"] * factor,
                        )
                new_state = await get_today_state(user_id)
                await chat.send_message(
                    f"Записано: {action['food_name']} {action['weight_g']}г\n"
                    f"{food_data['calories'] * factor:.0f}ккал | "
                    f"{food_data['protein'] * factor:.1f}г бел\n"
                    f"Всего: {new_state['calories_in']:.0f}ккал"
                )
                return

        elif analysis["intent"] == "steps":
            from bot.cache.redis_client import update_today_state
            kcal = action["steps"] * 0.04 * (user.weight_kg / 70)
            await update_today_state(user_id, steps=action["steps"], calories_out=kcal)
            await invalidate_context(user_id)
            await chat.send_message(f"Записано: {action['steps']} шагов (+{kcal:.0f}ккал)")
            return

        elif analysis["intent"] == "weight":
            async with async_session() as session:
                user_db = await crud.get_user(session, user_id)
                if user_db:
                    await crud.add_weight(session, user_db.id, action["weight_kg"])
                    await crud.update_user(session, user_db.tg_id, weight_kg=action["weight_kg"])
                    await invalidate_context(user_id)
            diff = user.target_weight_kg - action["weight_kg"]
            await chat.send_message(
                f"Вес: {action['weight_kg']}кг\n"
                f"До цели: {diff:+.1f}кг"
            )
            return

    await _send_ai_reply(chat, user_id, text, context.bot)


async def quick_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "qck_food":
        await q.message.reply_text("Введи что съел, например: 200г гречки с курицей")
    elif data == "qck_workout":
        await q.message.reply_text("Начни тренировку: /workout")
    elif data == "qck_steps":
        await q.message.reply_text("Сколько прошёл? Напиши число.")
    elif data == "qck_sleep":
        await q.message.reply_text("Формат: /sleep 23:00 07:00")
    elif data == "qck_weight":
        await q.message.reply_text("Сколько весишь? Напиши число.")
    elif data == "qck_week":
        from bot.handlers.commands import week
        await week(update, context)


_last_admin_alert: dict[int, float] = {}
ADMIN_ALERT_COOLDOWN = 300


async def _send_ai_reply(chat, user_id: int, text: str, bot) -> None:
    if is_rate_limited(user_id):
        await chat.send_message("Слишком много сообщений подряд, подожди немного.")
        return

    from bot.ai.trainer import chat_with_trainer
    t_start = time.monotonic()
    result = await chat_with_trainer(user_id, text)
    elapsed = time.monotonic() - t_start

    if result is None:
        logger.warning(f"[AI] user={user_id} elapsed={elapsed:.2f}s FAILED")
        now = time.monotonic()
        last = _last_admin_alert.get(user_id, 0)
        if now - last > ADMIN_ALERT_COOLDOWN:
            _last_admin_alert[user_id] = now
            try:
                await bot.send_message(ADMIN_ID, f"Оба ИИ-провайдера упали для user {user_id}")
            except Exception:
                pass
        await chat.send_message("ИИ временно недоступен, попробуй позже.")
        return

    logger.info(f"[AI] user={user_id} elapsed={elapsed:.2f}s provider=chat len={len(result)}")
    await chat.send_message(result)


# ─── Health check ──────────────────────────────────────────

async def health(request):
    from aiohttp import web
    return web.json_response({"status": "ok"})


async def start_health_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    for port in range(8080, 8090):
        try:
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logger.info(f"Health check server started on :{port}")
            return
        except OSError:
            continue
    logger.warning("Could not start health server - all ports busy")


async def post_init(app):
    await init_db()
    scheduler.start()
    await start_health_server()

    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("today", "Прогресс за сегодня"),
        BotCommand("workout", "Записать тренировку"),
        BotCommand("weight", "Обновить вес"),
        BotCommand("sleep", "Записать сон"),
        BotCommand("steps", "Записать шаги"),
        BotCommand("week", "Недельный отчёт"),
        BotCommand("help", "Помощь"),
    ])

    from bot.scheduler.reminders import reset_all_today_states, restore_all_schedulers
    async def midnight_reset():
        await reset_all_today_states(app.bot)
    scheduler.add_job(midnight_reset, CronTrigger(hour=0, minute=5), id="midnight_reset", replace_existing=True)

    await restore_all_schedulers(app.bot)

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
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_entry))

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
    app.add_handler(get_progress_handler())
    app.add_handler(get_suggest_handler())

    app.add_handler(CallbackQueryHandler(onb_restart_callback, pattern="^onb_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_|^chart_"))
    app.add_handler(CallbackQueryHandler(quick_button_callback, pattern="^qck_"))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGTERM, signal.SIGINT),
    )


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
