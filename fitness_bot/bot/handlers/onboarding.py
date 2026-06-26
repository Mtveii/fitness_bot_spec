import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from bot.db.base import async_session
from bot.db import crud
from bot.calculators.tdee import bmr, tdee
from bot.calculators.nutrition import daily_targets

NAME, GENDER, AGE, HEIGHT, WEIGHT, TARGET_WEIGHT, ACTIVITY, GOAL, \
    ALLERGIES, FAVORITE_FOODS, SLEEP_SCHEDULE, WORKOUT_TIME, SUPPLEMENTS = range(13)

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


async def onboarding_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Давай настроим твой профиль!\n\n"
        "Как тебя зовут?"
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Мужской", callback_data="M"),
         InlineKeyboardButton("Женский", callback_data="F")]
    ])
    await update.message.reply_text("Пол:", reply_markup=keyboard)
    return GENDER


async def get_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["gender"] = query.data
    await query.edit_message_text("Сколько тебе лет?")
    return AGE


async def get_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.message.text.strip())
        if not 10 <= age <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 10 до 100:")
        return AGE
    context.user_data["age"] = age
    await update.message.reply_text("Рост в см (например 175):")
    return HEIGHT


async def get_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        height = float(update.message.text.strip())
        if not 100 <= height <= 250:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи рост от 100 до 250 см:")
        return HEIGHT
    context.user_data["height_cm"] = height
    await update.message.reply_text("Текущий вес в кг:")
    return WEIGHT


async def get_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text.strip())
        if not 20 <= weight <= 300:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи вес от 20 до 300 кг:")
        return WEIGHT
    context.user_data["weight_kg"] = weight
    await update.message.reply_text("Целевой вес в кг:")
    return TARGET_WEIGHT


async def get_target_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        tw = float(update.message.text.strip())
        if not 20 <= tw <= 300:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи вес от 20 до 300 кг:")
        return TARGET_WEIGHT
    context.user_data["target_weight_kg"] = tw
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Сидячий", callback_data="sedentary"),
         InlineKeyboardButton("Лёгкий", callback_data="light")],
        [InlineKeyboardButton("Средний", callback_data="moderate"),
         InlineKeyboardButton("Высокий", callback_data="high")],
    ])
    await update.message.reply_text("Уровень активности:", reply_markup=keyboard)
    return ACTIVITY


async def get_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["activity_level"] = query.data
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Похудение", callback_data="cut"),
         InlineKeyboardButton("Набор", callback_data="bulk")],
        [InlineKeyboardButton("Рельеф", callback_data="recomp"),
         InlineKeyboardButton("Поддержка", callback_data="maintain")],
    ])
    await query.edit_message_text("Какая цель?", reply_markup=keyboard)
    return GOAL


async def get_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["goal"] = query.data
    await query.edit_message_text(
        "Есть ли аллергии или исключения в еде?\n"
        "Напиши «нет» если нет."
    )
    return ALLERGIES


async def get_allergies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    context.user_data["allergies"] = [] if text in ("нет", "no", "—", "-") else [x.strip() for x in text.split(",")]
    await update.message.reply_text(
        "Любимые продукты (через запятую, например «творог, яйца, гречка»):"
    )
    return FAVORITE_FOODS


async def get_favorite_foods(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["favorite_foods"] = [x.strip() for x in text.split(",") if x.strip()]
    await update.message.reply_text(
        "Время подъёма и отхода ко сну (например «07:00 / 23:00»):"
    )
    return SLEEP_SCHEDULE


async def get_sleep_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        parts = text.split("/")
        wake = parts[0].strip()
        sleep = parts[1].strip()
        context.user_data["sleep_schedule"] = {
            "preferred_wake": wake,
            "preferred_sleep": sleep,
            "target_hours": 8,
        }
        context.user_data["wake_time"] = wake
    except (IndexError, ValueError):
        context.user_data["sleep_schedule"] = {"preferred_wake": "07:00", "preferred_sleep": "23:00", "target_hours": 8}
        context.user_data["wake_time"] = "07:00"
    await update.message.reply_text(
        "Во сколько ты обычно тренируешься?\n"
        "(например «18:00» или «утром»)"
    )
    return WORKOUT_TIME


async def get_workout_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    wake = context.user_data.get("wake_time", "07:00")

    time_map = {
        "утром": wake,
        "утро": wake,
        "днём": "12:00",
        "день": "12:00",
        "вечером": "18:00",
        "вечер": "18:00",
    }

    if text in time_map:
        context.user_data["workout_time"] = time_map[text]
    elif ":" in text and len(text) <= 5:
        context.user_data["workout_time"] = text
    else:
        context.user_data["workout_time"] = "18:00"

    await update.message.reply_text(
        "Добавки? Формат: «Креатин 5г 08:00, Магний 400мг 22:00»\n"
        "Или «нет»."
    )
    return SUPPLEMENTS


async def get_supplements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower()
    if text in ("нет", "no", "—", "-"):
        context.user_data["supplements"] = []
    else:
        supplements = []
        for item in text.split(","):
            parts = item.strip().split()
            if len(parts) >= 2:
                name = " ".join(parts[:-2]) if len(parts) > 2 else parts[0]
                dose = parts[-2] if len(parts) > 2 else parts[1]
                time_str = parts[-1] if len(parts) > 2 else "08:00"
                supplements.append({"name": name, "dose": dose, "times": [time_str]})
        context.user_data["supplements"] = supplements

    # Сохраняем пользователя
    d = context.user_data
    bmr_val = bmr(d["gender"], d["weight_kg"], d["height_cm"], d["age"])
    tdee_val = tdee(bmr_val, d["activity_level"], weight_kg=d["weight_kg"])
    targets = daily_targets(tdee_val, d["weight_kg"], d["goal"])

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if user:
            await crud.update_user(session, update.effective_user.id, **d)
        else:
            await crud.create_user(
                session, tg_id=update.effective_user.id,
                settings=DEFAULT_SETTINGS, **d
            )

    await update.message.reply_text(
        f"Профиль сохранён!\n\n"
        f"📊 Твои расчёты:\n"
        f"BMR: {bmr_val:.0f} ккал\n"
        f"TDEE: {tdee_val:.0f} ккал\n"
        f"🎯 Цель по калориям: {targets['calories']} ккал\n"
        f"🥩 Белок: {targets['protein_g']}г\n"
        f"🧈 Жиры: {targets['fat_g']}г\n"
        f"🍞 Углеводы: {targets['carbs_g']}г"
    )

    from bot.scheduler.reminders import setup_scheduler, scheduler

    user_id = update.effective_user.id
    prefix = f"supp_{user_id}_"
    for job in scheduler.get_jobs():
        if job.id.startswith(prefix):
            job.remove()

    setup_scheduler(context.bot, user_id, d)
    if not scheduler.running:
        scheduler.start()

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Онбординг отменён. Можно начать заново: /onboarding")
    return ConversationHandler.END


def get_onboarding_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("onboarding", onboarding_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GENDER: [CallbackQueryHandler(get_gender, pattern="^(M|F)$")],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_age)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_height)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_weight)],
            TARGET_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_target_weight)],
            ACTIVITY: [CallbackQueryHandler(get_activity, pattern="^(sedentary|light|moderate|high)$")],
            GOAL: [CallbackQueryHandler(get_goal, pattern="^(cut|bulk|recomp|maintain)$")],
            ALLERGIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_allergies)],
            FAVORITE_FOODS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_favorite_foods)],
            SLEEP_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_sleep_schedule)],
            WORKOUT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_workout_time)],
            SUPPLEMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_supplements)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
