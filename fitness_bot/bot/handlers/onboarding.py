import json
import logging
import datetime

from telegram import Update
from telegram.ext import ContextTypes

from bot.ai.clients import ask_ai_race
from bot.ai.prompts import ONBOARDING_PROMPT
from bot.tools.definitions import get_tool_schema, ONBOARDING_TOOL_NAMES
from bot.tools.registry import execute_tool
from bot.db.models import User
from bot.db.base import async_session
from bot.calculators.tdee import calc_tdee, calc_target_calories
from bot.calculators.nutrition import calc_macros

logger = logging.getLogger(__name__)


async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    tg_id = update.effective_user.id

    profile_data = context.user_data.get("profile_data", {})

    messages = [
        {"role": "system", "content": ONBOARDING_PROMPT},
        {"role": "user", "content": f"Пользователь сказал: {user_text}\n\nТекущие данные профиля: {json.dumps(profile_data, ensure_ascii=False)}"},
    ]

    tools = get_tool_schema(ONBOARDING_TOOL_NAMES)
    response = await ask_ai_race(messages, tools=tools)

    tool_calls = response.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            func_name = tc["function"]["name"]
            func_args = json.loads(tc["function"]["arguments"])
            result = await execute_tool(func_name, func_args, 0, context.user_data)
            parsed = json.loads(result) if isinstance(result, str) else result
            if parsed.get("status") == "complete":
                profile = parsed.get("profile", {})
                await _finish_onboarding(update, context, tg_id, profile)
                return
            elif parsed.get("status") == "partial":
                profile_data = context.user_data.get("profile_data", {})
                missing = parsed.get("missing", [])
                missing_names = {
                    "gender": "пол",
                    "age": "возраст",
                    "height_cm": "рост",
                    "weight_kg": "вес",
                    "activity_level": "уровень активности",
                    "goal": "цель",
                }
                next_field = missing[0] if missing else None
                field_ru = missing_names.get(next_field, next_field)
                if profile_data:
                    known = ", ".join(f"{k}: {v}" for k, v in profile_data.items())
                    await update.message.reply_text(
                        f"Я уже понял: {known}.\n\nРасскажи ещё о {field_ru}?"
                    )
                else:
                    await update.message.reply_text(
                        f"Расскажи о своём {field_ru} — это нужно для расчётов."
                    )
                return

    content = response.get("content", "")
    if content:
        await update.message.reply_text(content)


async def _finish_onboarding(update, context, tg_id, profile):
    gender = profile.get("gender", "M")
    weight = float(profile.get("weight_kg", 70))
    height = float(profile.get("height_cm", 175))
    age = int(profile.get("age", 25))
    activity = profile.get("activity_level", "moderate")
    goal = profile.get("goal", "maintain")
    name = profile.get("name", f"User_{tg_id}")

    tdee = calc_tdee(gender, weight, height, age, activity)
    target_cal = calc_target_calories(tdee, goal)
    macros = calc_macros(target_cal, goal)

    async with async_session() as session:
        user = User(
            tg_id=tg_id,
            name=name,
            gender=gender,
            age=age,
            height_cm=height,
            weight_kg=weight,
            target_weight_kg=float(profile.get("target_weight_kg", weight)),
            activity_level=activity,
            goal=goal,
            allergies=profile.get("allergies"),
            favorite_foods=profile.get("favorite_foods"),
            created_at=datetime.datetime.utcnow(),
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()

    context.user_data["user_db_id"] = user_id
    context.user_data["onboarding"] = False
    context.user_data.pop("profile_data", None)

    await update.message.reply_text(
        f"Отлично! Вот что я понял о тебе:\n\n"
        f"• Пол: {'мужской' if gender == 'M' else 'женский'}\n"
        f"• Возраст: {age}\n"
        f"• Рост: {height}см\n"
        f"• Вес: {weight}кг\n"
        f"• Активность: {activity}\n"
        f"• Цель: {goal}\n\n"
        f"Твои нормы:\n"
        f"• BMR (базовый обмен): ~{calc_tdee(gender, weight, height, age, 'sedentary'):.0f} ккал\n"
        f"• TDEE (с учётом активности): ~{tdee:.0f} ккал\n"
        f"• Целевые калории: ~{target_cal:.0f} ккал\n"
        f"• Белки: {macros['protein_g']}г | Жиры: {macros['fat_g']}г | Углеводы: {macros['carbs_g']}г\n\n"
        f"Теперь просто пиши мне о своих приёмах пищи, тренировках и сне — я всё запомню. "
        f"Если захочешь изменить тон общения — просто скажи!"
    )
