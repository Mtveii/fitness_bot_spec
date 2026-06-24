from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.db.base import async_session
from bot.db import crud
from bot.calculators.tdee import bmr, tdee
from bot.calculators.nutrition import daily_targets

GOAL_NAMES = {"cut": "Похудение", "bulk": "Набор", "recomp": "Рельеф", "maintain": "Поддержка"}
ACTIVITY_NAMES = {"sedentary": "Сидячий", "light": "Лёгкий", "moderate": "Средний", "high": "Высокий"}


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)

    if not user:
        await update.message.reply_text(
            "Ты ещё не зарегистрирован.\nНачни с /onboarding"
        )
        return

    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    supplements_text = "\n".join(
        f"  • {s['name']} {s['dose']} ({', '.join(s.get('times', []))})"
        for s in (user.supplements or [])
    ) or "  нет"

    await update.message.reply_text(
        f"👤 {user.name}\n\n"
        f"📋 Профиль:\n"
        f"  Пол: {'Муж' if user.gender == 'M' else 'Жен'}\n"
        f"  Возраст: {user.age}\n"
        f"  Рост: {user.height_cm} см\n"
        f"  Вес: {user.weight_kg} кг → цель {user.target_weight_kg} кг\n"
        f"  Активность: {ACTIVITY_NAMES.get(user.activity_level, user.activity_level)}\n"
        f"  Цель: {GOAL_NAMES.get(user.goal, user.goal)}\n\n"
        f"📊 Расчёты:\n"
        f"  BMR: {bmr_val:.0f} ккал\n"
        f"  TDEE: {tdee_val:.0f} ккал\n\n"
        f"🎯 Дневные нормы:\n"
        f"  Калории: {targets['calories']} ккал\n"
        f"  Белок: {targets['protein_g']}г\n"
        f"  Жиры: {targets['fat_g']}г\n"
        f"  Углеводы: {targets['carbs_g']}г\n\n"
        f"💊 Добавки:\n{supplements_text}\n\n"
        f"⏰ Расписание:\n"
        f"  Подъём: {user.sleep_schedule.get('preferred_wake', '—')}\n"
        f"  Сон: {user.sleep_schedule.get('preferred_sleep', '—')}\n\n"
        f"🤖 Стиль ИИ: {user.ai_personality}"
    )


def get_me_handler() -> CommandHandler:
    return CommandHandler("me", me)
