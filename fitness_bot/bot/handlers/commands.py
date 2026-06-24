import json
import csv
import io
import logging
from datetime import datetime, UTC, timedelta, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from bot.db.base import async_session
from bot.db import crud
from bot.db.models import MealLog, WorkoutLog
from bot.cache.redis_client import get_today_state, update_today_state, decrement_today_state, invalidate_context
from bot.calculators.tdee import bmr, tdee
from bot.calculators.nutrition import daily_targets

logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "notifications": {
        "supplements": True,
        "nutrition_deficit": True,
        "nutrition_reminder_if_no_meals": True,
        "workout_reminder": True,
        "sleep": True,
        "weekly_report": True,
    },
    "ai": {
        "personality": "strict",
        "proactive_analysis": True,
        "auto_adjust_settings": True,
        "language": "ru",
    },
    "nutrition": {
        "deficit_alert_threshold_pct": 80,
    },
    "workout": {
        "auto_suggest_progression": True,
        "performance_drop_alert_pct": 5,
        "log_rpe": True,
    },
}


def format_progress_bar(current: float, target: float, length: int = 10) -> str:
    if target <= 0:
        return "⬜" * length
    pct = min(current / target, 1.0)
    filled = round(pct * length)
    return "🟩" * filled + "⬜" * (length - filled)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return
        today_workout = await crud.get_today_workout(session, user.id)
        last_sleep = await crud.get_last_sleep(session, user.id)

    state = await get_today_state(update.effective_user.id)
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    cal_pct = (state["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (state["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0
    fat_pct = (state["fat"] / targets["fat_g"] * 100) if targets["fat_g"] > 0 else 0
    carb_pct = (state["carbs"] / targets["carbs_g"] * 100) if targets["carbs_g"] > 0 else 0

    now = datetime.now(UTC)
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_name = day_names[now.weekday()]

    sleep_text = "😴 Сон: —"
    if last_sleep:
        days_ago = (now - last_sleep.date.replace(tzinfo=None)).days
        if days_ago <= 1:
            sleep_text = f"😴 Сон: {last_sleep.duration_hours:.1f}ч"

    workout_text = ""
    if today_workout:
        workout_text = (
            f"🏋️ {today_workout.workout_name} — "
            f"объём {today_workout.total_volume:.0f}кг "
            f"(+{today_workout.calories_burned:.0f} ккал)\n"
        )

    balance = state["calories_in"] - targets["calories"]

    await update.message.reply_text(
        f"📅 {day_name}, {now.strftime('%d.%m')}\n\n"
        f"🔥 Калории: {state['calories_in']:.0f} / {targets['calories']} "
        f"{format_progress_bar(state['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
        f"🥩 Белок: {state['protein']:.0f} / {targets['protein_g']}г "
        f"{format_progress_bar(state['protein'], targets['protein_g'])} {prot_pct:.0f}%\n"
        f"🧈 Жиры: {state['fat']:.0f} / {targets['fat_g']}г "
        f"{format_progress_bar(state['fat'], targets['fat_g'])} {fat_pct:.0f}%\n"
        f"🍞 Углеводы: {state['carbs']:.0f} / {targets['carbs_g']}г "
        f"{format_progress_bar(state['carbs'], targets['carbs_g'])} {carb_pct:.0f}%\n\n"
        f"👟 Шаги: {state['steps']}\n"
        f"{workout_text}"
        f"{sleep_text}\n\n"
        f"⚖️ Баланс: {balance:+.0f} ккал"
    )


async def weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /weight 75.5")
        return

    try:
        kg = float(context.args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введи число, например: /weight 75.5")
        return

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        await crud.add_weight(session, user.id, kg)
        await crud.update_user(session, user.tg_id, weight_kg=kg)

    diff = user.target_weight_kg - kg
    await update.message.reply_text(
        f"⚖️ Вес обновлён: {kg} кг\n"
        f"🎯 До цели: {diff:+.1f} кг"
    )


async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Формат: /sleep 23:00 07:00")
        return

    try:
        sleep_str = context.args[0]
        wake_str = context.args[1]
        today = datetime.now(UTC).replace(tzinfo=None)

        sleep_time = today.replace(
            hour=int(sleep_str.split(":")[0]),
            minute=int(sleep_str.split(":")[1])
        )
        wake_time = today.replace(
            hour=int(wake_str.split(":")[0]),
            minute=int(wake_str.split(":")[1])
        )

        if wake_time < sleep_time:
            wake_time += __import__("datetime").timedelta(days=1)

        duration = (wake_time - sleep_time).total_seconds() / 3600

        async with async_session() as session:
            user = await crud.get_user(session, update.effective_user.id)
            if user:
                await crud.add_sleep(session, user.id, sleep_time, wake_time, duration)

        emoji = "😴" if duration >= 7 else "⚠️"
        await update.message.reply_text(
            f"{emoji} Сон: {sleep_str} → {wake_str}\n"
            f"⏱ Длительность: {duration:.1f}ч"
        )

    except Exception:
        await update.message.reply_text("Ошибка. Формат: /sleep 23:00 07:00")


async def steps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /steps 8000")
        return

    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Введи число: /steps 8000")
        return

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

    from bot.calculators.tdee import bmr as _bmr, tdee as _tdee
    kcal = n * 0.04 * (user.weight_kg / 70)
    await update_today_state(update.effective_user.id, steps=n, calories_out=kcal)

    await update.message.reply_text(f"👟 Шаги: {n} (+{kcal:.0f} ккал)")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        now = datetime.now(UTC)
        week_start = now - __import__("datetime").timedelta(days=7)

        meals = await crud.get_meals_between(session, user.id, week_start, now)
        workouts = await crud.get_workout_logs_between(session, user.id, week_start, now)
        sleep_logs = await crud.get_sleep_between(session, user.id, week_start, now)
        weight_history = await crud.get_weight_history(session, user.id, days=7)

    if not meals:
        await update.message.reply_text("📊 Нет данных за неделю. Начни с /log")
        return

    days_with_meals = len(set(m.date.date() for m in meals))
    days = max(days_with_meals, 1)

    total_cal = sum(m.calories for m in meals)
    total_protein = sum(m.protein for m in meals)
    total_fat = sum(m.fat for m in meals)
    total_carbs = sum(m.carbs for m in meals)
    total_workout_vol = sum(w.total_volume or 0 for w in workouts)
    total_workout_kcal = sum(w.calories_burned or 0 for w in workouts)
    avg_sleep = sum(s.duration_hours for s in sleep_logs) / max(len(sleep_logs), 1) if sleep_logs else 0

    weight_text = ""
    if len(weight_history) >= 2:
        first = weight_history[0].weight_kg
        last = weight_history[-1].weight_kg
        diff = last - first
        weight_text = f"\n⚖️ Вес: {first:.1f} → {last:.1f}кг ({diff:+.1f}кг)"

    lines = [
        f"📊 Неделя ({days} дн.)\n",
        f"🔥 Среднее: {total_cal / days:.0f} ккал/день",
        f"🥩 Средний белок: {total_protein / days:.0f}г/день",
        f"🧈 Средние жиры: {total_fat / days:.0f}г/день",
        f"🍞 Средние углеводы: {total_carbs / days:.0f}г/день",
        f"🏋️ Тренировок: {len(workouts)} | Объём: {total_workout_vol:.0f}кг",
        f"🔥 Сожжено на тренировках: {total_workout_kcal:.0f}ккал",
    ]

    if avg_sleep > 0:
        emoji = "😴" if avg_sleep >= 7 else "⚠️"
        lines.append(f"{emoji} Средний сон: {avg_sleep:.1f}ч")

    if weight_text:
        lines.append(weight_text)

    from bot.calculators.tdee import bmr, tdee as calc_tdee
    from bot.calculators.nutrition import daily_targets
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = calc_tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    avg_deficit = targets["calories"] - (total_cal / days)
    lines.append(f"\n📉 Средний дефицит: {avg_deficit:+.0f} ккал/день")

    await update.message.reply_text("\n".join(lines))


# ─── /settings ──────────────────────────────────────────────

SETTINGS_MENU, SETTINGS_NOTIFICATIONS, SETTINGS_AI = range(3)


async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return ConversationHandler.END

    settings = user.settings or DEFAULT_SETTINGS
    personality = settings.get("ai", {}).get("personality", "strict")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Уведомления", callback_data="notif")],
        [InlineKeyboardButton("🤖 Стиль ИИ", callback_data="ai_style")],
        [InlineKeyboardButton("🔄 Сбросить всё", callback_data="reset")],
    ])

    await update.message.reply_text(
        f"⚙️ Настройки\n\n"
        f"🤖 Стиль ИИ: {personality}\n"
        f"🔔 Уведомления: вкл" if all(settings.get("notifications", {}).values()) else
        f"⚙️ Настройки\n\n"
        f"🤖 Стиль ИИ: {personality}\n"
        f"🔔 Уведомления: частично",
        reply_markup=keyboard
    )
    return SETTINGS_MENU


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "notif":
        async with async_session() as session:
            user = await crud.get_user(session, update.effective_user.id)
        settings = user.settings if user else DEFAULT_SETTINGS
        notif = settings.get("notifications", DEFAULT_SETTINGS["notifications"])

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'✅' if notif.get(k, True) else '❌'} {label}",
                callback_data=f"toggle_{k}"
            )]
            for k, label in [
                ("supplements", "Добавки"),
                ("nutrition_deficit", "Недобор калорий"),
                ("workout_reminder", "Тренировки"),
                ("sleep", "Сон"),
                ("weekly_report", "Недельный отчёт"),
            ]
        ])
        keyboard.inline_keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
        await query.edit_message_text("🔔 Уведомления:", reply_markup=keyboard)
        return SETTINGS_NOTIFICATIONS

    elif query.data == "ai_style":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💪 Строгий", callback_data="set_strict"),
             InlineKeyboardButton("😊 Дружелюбный", callback_data="set_friendly")],
            [InlineKeyboardButton("🔥 Мотивирующий", callback_data="set_motivating")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back")],
        ])
        await query.edit_message_text("🤖 Стиль ИИ-тренера:", reply_markup=keyboard)
        return SETTINGS_AI

    elif query.data == "reset":
        async with async_session() as session:
            await crud.update_user(session, update.effective_user.id, settings=DEFAULT_SETTINGS)
        await query.edit_message_text("🔄 Настройки сброшены к дефолту.")
        return ConversationHandler.END

    return SETTINGS_MENU


async def settings_toggle_notif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        await query.edit_message_text("⚙️ Настройки обновлены.")
        return ConversationHandler.END

    key = query.data.replace("toggle_", "")
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if user:
            settings = user.settings or DEFAULT_SETTINGS
            notif = settings.setdefault("notifications", DEFAULT_SETTINGS["notifications"])
            notif[key] = not notif.get(key, True)
            await crud.update_user(session, update.effective_user.id, settings=settings)

    await query.answer(f"{'Включено' if notif.get(key) else 'Выключено'}", show_alert=True)
    return SETTINGS_NOTIFICATIONS


async def settings_set_ai_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back":
        await query.edit_message_text("⚙️ Настройки обновлены.")
        return ConversationHandler.END

    personality = query.data.replace("set_", "")
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if user:
            settings = user.settings or DEFAULT_SETTINGS
            settings.setdefault("ai", {})["personality"] = personality
            await crud.update_user(session, update.effective_user.id, settings=settings)

    names = {"strict": "Строгий", "friendly": "Дружелюбный", "motivating": "Мотивирующий"}
    await query.edit_message_text(f"🤖 Стиль: {names.get(personality, personality)}")
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return ConversationHandler.END


def get_settings_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("settings", settings_start)],
        states={
            SETTINGS_MENU: [CallbackQueryHandler(settings_menu_callback)],
            SETTINGS_NOTIFICATIONS: [CallbackQueryHandler(settings_toggle_notif)],
            SETTINGS_AI: [CallbackQueryHandler(settings_set_ai_style)],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel)],
    )


# ─── /cancel (P4.17) ────────────────────────────────────────

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        meal = await crud.get_last_meal_log(session, user.id)
        workout = await crud.get_last_workout_log(session, user.id)

        target = None
        if meal and workout:
            target = meal if meal.date > workout.date else workout
        else:
            target = meal or workout

        if not target:
            await update.message.reply_text("Нечего отменять.")
            return

        if isinstance(target, MealLog):
            await crud.delete_meal_log(session, target.id, user.id)
            await decrement_today_state(
                user_id,
                calories_in=target.calories,
                protein=target.protein,
                fat=target.fat,
                carbs=target.carbs,
            )
            await invalidate_context(user_id)
            await update.message.reply_text(
                f"✅ Отменено: {target.food_name} ({target.calories:.0f}ккал)"
            )
        else:
            await crud.delete_workout_log(session, target.id, user.id)
            await invalidate_context(user_id)
            await update.message.reply_text(
                f"✅ Отменена тренировка: {target.workout_name}"
            )


def get_cancel_handler() -> CommandHandler:
    return CommandHandler("cancel", cancel_command)


# ─── /export (P4.18) ────────────────────────────────────────

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return
        week_start = datetime.now(UTC) - timedelta(days=7)
        meals = await crud.get_meals_between(session, user.id, week_start, datetime.now(UTC))

    if not meals:
        await update.message.reply_text("Нет данных за неделю.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Дата", "Время", "Продукт", "Граммы", "Ккал", "Белок", "Жир", "Углеводы"])
    for m in meals:
        writer.writerow([
            m.date.strftime("%Y-%m-%d"), m.date.strftime("%H:%M"),
            m.food_name, m.weight_g, m.calories, m.protein, m.fat, m.carbs,
        ])

    buf.seek(0)
    data = buf.getvalue().encode("utf-8-sig")
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=f"export_{date.today().isoformat()}.csv",
    )


def get_export_handler() -> CommandHandler:
    return CommandHandler("export", export_command)


def get_today_handler() -> CommandHandler:
    return CommandHandler("today", today)

def get_weight_handler() -> CommandHandler:
    return CommandHandler("weight", weight)

def get_sleep_handler() -> CommandHandler:
    return CommandHandler("sleep", sleep)

def get_steps_handler() -> CommandHandler:
    return CommandHandler("steps", steps)

def get_week_handler() -> CommandHandler:
    return CommandHandler("week", week)
