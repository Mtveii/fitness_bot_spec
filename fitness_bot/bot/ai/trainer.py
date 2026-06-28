"""
P2.12 — system/user split для prompt caching.
P2.11 — кэш build_context() в Redis.
P3.13 — логирование токенов.
"""
import logging
import time
from bot.db.base import async_session
from bot.db import crud
from bot.cache.redis_client import (
    get_today_state, get_cached_context, set_cached_context, invalidate_context,
)
from bot.ai.clients import ask_ai_race

logger = logging.getLogger(__name__)

PERSONALITY_PROMPTS = {
    "friendly": "Ты дружелюбный фитнес-тренер. Коротко, по делу.",
    "strict": "Ты строгий фитнес-тренер. Максимально коротко, без эмодзи.",
    "motivating": "Ты мотивирующий тренер. Энергичный, коротко.",
}

SYSTEM_TEMPLATE = """{personality}

Правила:
- Если вопрос о еде — оцени КБЖУ.
- Если о тренировке — дай совет.
- Если общее — поддержи.
- Отвечай МАКСИМАЛЬНО кратко: 1-2 коротких предложения, без вступлений и воды.
- Не используй markdown."""

ACTION_KEYWORDS = {
    "LOG_FOOD": ("съел", " поел", "выпил", "ккал", "калори", "завтрак", "обед", "ужин", "перекус"),
    "LOG_WORKOUT": ("трениров", "упражнен", "подход", "повтор", "жим", "тяга", "присед"),
    "LOG_SLEEP": ("лег", "спал", "сон", "проснулся", "подъём", "отбой"),
    "LOG_STEPS": ("шаг", "прошёл", "прошел"),
    "UPDATE_WEIGHT": ("вес ", "взвес"),
}


def daily_targets_val(profile: dict) -> float:
    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets
    bmr_val = bmr(profile["gender"], profile["weight"], profile["height"], profile["age"])
    tdee_val = tdee(bmr_val, profile["activity"], weight_kg=profile["weight"])
    targets = daily_targets(tdee_val, profile["weight"], profile["goal"])
    return targets.get("calories", 2000)


async def build_context(user_id: int) -> dict:
    cached = await get_cached_context(user_id)
    if cached:
        return cached

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return {"profile": None, "today": {}, "meals": [], "sleep": None}
        last_meals = await crud.get_last_meal_logs(session, user.id, limit=3)
        last_sleep = await crud.get_last_sleep(session, user.id)

    state = await get_today_state(user_id)

    meals_data = [{"name": m.food_name, "weight": m.weight_g, "kcal": m.calories} for m in last_meals]
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


async def _build_prompt(user_id: int, text: str) -> tuple[str, str] | None:
    """Собирает (system_prompt, user_text). None если юзер не зарегистрирован."""
    t0 = time.monotonic()
    ctx = await build_context(user_id)
    t1 = time.monotonic()
    logger.info(f"[TIMING] user={user_id} build_context={t1-t0:.2f}s")

    if not ctx.get("profile"):
        return None

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

    return system_prompt, user_text


async def _after_ai_response(user_id: int, text: str, answer: str, provider: str, tok_in: int, tok_out: int) -> None:
    """Общий хвост после получения ответа от ИИ: лог токенов, side-effects, история чата."""
    try:
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
            if user:
                await crud.add_ai_usage_log(session, user.id, provider, tok_in, tok_out)
    except Exception as e:
        logger.warning(f"Failed to log AI usage: {e}")

    intent = _detect_intent(text)
    if intent:
        try:
            await _execute_action(user_id, intent, text)
        except Exception as e:
            logger.warning(f"Action execution failed: {e}")

    from bot.cache.redis_client import add_chat_message
    await add_chat_message(user_id, "user", text)
    await add_chat_message(user_id, "assistant", answer)


async def chat_with_trainer(user_id: int, text: str) -> str | None:
    """Чат с ИИ-тренером. Возвращает ответ или None при полном падении ИИ."""
    built = await _build_prompt(user_id, text)
    if built is None:
        return "Сначала выполни /onboarding, чтобы я знал твои параметры."
    system_prompt, user_text = built

    t0 = time.monotonic()
    result = await ask_ai_race(system_prompt, user_text, max_tokens=150)
    t1 = time.monotonic()
    logger.info(f"[TIMING] user={user_id} ask_ai_race={t1-t0:.2f}s provider={result[1] if result else 'none'}")

    if not result:
        return None

    answer, provider, tok_in, tok_out = result
    await _after_ai_response(user_id, text, answer, provider, tok_in, tok_out)
    return answer