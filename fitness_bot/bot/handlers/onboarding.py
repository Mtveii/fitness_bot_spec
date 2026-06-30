import json
import logging
import datetime

from telegram import Update
from telegram.ext import ContextTypes

from bot.ai.clients import ask_ai_race
from bot.ai.prompts import ONBOARDING_PROMPT, ONBOARDING_COMPLETION_PROMPT
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
                followup = await ask_ai_race(
                    [{"role": "system", "content": ONBOARDING_PROMPT},
                     {"role": "user", "content":
                         f"Пользователь предоставил: {json.dumps(profile_data, ensure_ascii=False)}.\n"
                         f"Не хватает: {', '.join(missing)}.\n"
                         f"Задай один конкретный содержательный вопрос про {missing[0]}, "
                         f"объяснив ЗАЧЕМ это нужно и в контексте уже известного. "
                         f"Не используй шаблонные фразы."}],
                    tools=None, temperature=0.8, max_tokens=256
                )
                question = followup.get("content", "")
                if not question:
                    question = f"Расскажи о своём {missing[0]} — это поможет точнее рассчитать твои нормы."
                await update.message.reply_text(question)
                return

    content = response.get("content", "")
    if content:
        await update.message.reply_text(content)
    else:
        await update.message.reply_text(
            "Давай сначала закончим настройку профиля, чтобы я мог всё правильно "
            "считать. Расскажи о себе: сколько тебе лет, какой у тебя рост, вес, "
            "уровень активности и цель?"
        )


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
            created_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()

    context.user_data["user_db_id"] = user_id
    context.user_data["onboarding"] = False
    context.user_data.pop("profile_data", None)

    gender_ru = "мужской" if gender == "M" else "женский"
    bmr_val = int(calc_tdee(gender, weight, height, age, "sedentary"))
    completion_prompt = ONBOARDING_COMPLETION_PROMPT.format(
        gender=gender_ru, age=age, height_cm=height, weight_kg=weight,
        activity_level=activity, goal=goal, bmr=bmr_val, tdee=int(tdee),
        target_cal=int(target_cal), protein_g=macros["protein_g"],
        fat_g=macros["fat_g"], carbs_g=macros["carbs_g"],
    )
    ai_response = await ask_ai_race(
        [{"role": "user", "content": completion_prompt}],
        tools=None, temperature=0.8, max_tokens=512
    )
    reply = ai_response.get("content", "")
    if not reply:
        reply = (
            f"Отлично! Я понял: {gender_ru}, {age} лет, {height}см, {weight}кг.\n"
            f"Твой BMR: ~{bmr_val} ккал, TDEE: ~{int(tdee)} ккал, цель: {goal}.\n"
            f"Пиши о своих приёмах пищи, тренировках и сне — я всё запомню!"
        )
    await update.message.reply_text(reply)
