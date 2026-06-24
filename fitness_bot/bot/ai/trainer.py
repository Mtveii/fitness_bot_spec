"""
P2.12 — system/user split для prompt caching.
P2.11 — кэш build_context() в Redis.
P3.13 — логирование токенов.
"""
import logging
from bot.db.base import async_session
from bot.db import crud
from bot.cache.redis_client import (
    get_today_state, get_cached_context, set_cached_context, invalidate_context,
)
from bot.ai.clients import ask_ai_race

logger = logging.getLogger(__name__)

PERSONALITY_PROMPTS = {
    "friendly": "Ты дружелюбный фитнес-тренер. Поддерживаешь, объясняешь мягко. Отвечай на русском.",
    "strict": "Ты строгий тренер. Говоришь по делу, без лишних эмоций. Отвечай на русском.",
    "motivating": "Ты мотивирующий тренер. Энергичный, вдохновляешь на результат. Отвечай на русском.",
}

SYSTEM_TEMPLATE = """{personality}

Правила:
- Если вопрос о еде — оцени КБЖУ.
- Если о тренировке — дай совет.
- Если общее — поддержи.
- Отвечай кратко, 2-4 предложения.
- Не используй markdown."""

ACTION_KEYWORDS = {
    "LOG_FOOD": ("съел", " поел", "выпил", "ккал", "калори", "завтрак", "обед", "ужин", "перекус"),
    "LOG_WORKOUT": ("трениров", "упражнен", "подход", "повтор", "жим", "тяга", "присед"),
    "LOG_SLEEP": ("лег", "спал", "сон", "проснулся", "подъём", "отбой"),
    "LOG_STEPS": ("шаг", "прошёл", "прошел"),
    "UPDATE_WEIGHT": ("вес ", "взвес"),
}


async def build_context(user_id: int) -> dict:
    cached = await get_cached_context(user_id)
    if cached:
        return cached

    async with async_session() as session:
        user = await crud.get_user(session, user_id)

    if not user:
        return {"profile": None, "today": {}, "meals": [], "sleep": None}

    state = await get_today_state(user_id)

    last_meals = await crud.get_last_meal_logs(user_id, limit=3)
    meals_data = [{"name": m.food_name, "weight": m.weight_g, "kcal": m.calories} for m in last_meals]

    last_sleep = await crud.get_last_sleep(user_id)
    sleep_data = {"hours": last_sleep.duration_hours} if last_sleep else None

    ctx = {
        "profile": {
            "gender": user.gender,
            "age": user.age,
            "height": user.height_cm,
            "weight": user.weight_kg,
            "target": user.target_weight_kg,
            "activity": user.activity_level,
            "goal": user.goal,
            "personality": user.ai_personality,
        },
        "today": state,
        "meals": meals_data,
        "sleep": sleep_data,
    }

    await set_cached_context(user_id, ctx)
    return ctx


def _detect_intent(text: str) -> str | None:
    lower = text.lower()
    for intent, keywords in ACTION_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return intent
    return None


async def _execute_action(user_id: int, intent: str, text: str) -> None:
    """Execute side-effect actions detected from user text. Invalidates context cache."""
    if intent == "LOG_FOOD":
        from bot.handlers.food import parse_food_input, search_food
        parsed = parse_food_input(text)
        if parsed:
            weight_g, food_name = parsed
            food_data = await search_food(food_name)
            if food_data:
                factor = weight_g / 100
                async with async_session() as session:
                    user = await crud.get_user(session, user_id)
                    if user:
                        await crud.add_meal_log(
                            session, user_id=user.id, food_name=food_name, weight_g=weight_g,
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

    elif intent == "LOG_STEPS":
        import re
        match = re.search(r"(\d+)", text)
        if match:
            n = int(match.group(1))
            from bot.cache.redis_client import update_today_state
            await update_today_state(user_id, steps=n)
            await invalidate_context(user_id)

    elif intent == "UPDATE_WEIGHT":
        import re
        match = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if match:
            kg = float(match.group(1).replace(",", "."))
            if 20 <= kg <= 300:
                async with async_session() as session:
                    user = await crud.get_user(session, user_id)
                    if user:
                        await crud.add_weight(session, user.id, kg)
                        await crud.update_user(session, user.tg_id, weight_kg=kg)
                        await invalidate_context(user_id)


async def chat_with_trainer(user_id: int, text: str) -> str | None:
    """Чат с ИИ-тренером. Возвращает ответ или None при полном падении ИИ."""
    ctx = await build_context(user_id)

    if not ctx.get("profile"):
        return "Сначала выполни /onboarding, чтобы я знал твои параметры."

    personality = PERSONALITY_PROMPTS.get(
        ctx["profile"].get("personality", "friendly"),
        PERSONALITY_PROMPTS["friendly"],
    )
    system_prompt = SYSTEM_TEMPLATE.format(personality=personality)

    from bot.cache.redis_client import get_chat_history
    history = await get_chat_history(user_id, limit=2)
    history_text = "\n".join(
        f"{'Ч' if m['role'] == 'user' else 'Т'}: {m['content']}" for m in reversed(history)
    )

    today = ctx.get("today", {})
    context_lines = [
        f"Профиль: {ctx['profile']['gender']}, {ctx['profile']['age']}л, "
        f"{ctx['profile']['height']}см, {ctx['profile']['weight']}кг",
        f"Цель: {ctx['profile']['goal']}, Активность: {ctx['profile']['activity']}",
        f"Сегодня: {today.get('calories_in', 0):.0f}ккал, белок {today.get('protein', 0):.0f}г",
    ]
    if ctx.get("meals"):
        meals_str = ", ".join(f"{m['name']} ({m['weight']:.0f}г)" for m in ctx["meals"])
        context_lines.append(f"Последний приём: {meals_str}")
    if ctx.get("sleep"):
        context_lines.append(f"Последний сон: {ctx['sleep']['hours']:.1f}ч")

    context_data = "\n".join(context_lines)
    user_text = f"{context_data}\n{history_text}\nЧеловек: {text}"

    result = await ask_ai_race(system_prompt, user_text, max_tokens=400)

    if not result:
        return None

    answer, provider, tok_in, tok_out = result

    # P3.13: log tokens
    try:
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
            if user:
                await crud.add_ai_usage_log(session, user.id, provider, tok_in, tok_out)
    except Exception as e:
        logger.warning(f"Failed to log AI usage: {e}")

    # Detect and execute side-effect actions
    intent = _detect_intent(text)
    if intent:
        try:
            await _execute_action(user_id, intent, text)
        except Exception as e:
            logger.warning(f"Action execution failed: {e}")

    # Save to chat history
    from bot.cache.redis_client import add_chat_message
    await add_chat_message(user_id, "user", text)
    await add_chat_message(user_id, "assistant", answer)

    return answer
