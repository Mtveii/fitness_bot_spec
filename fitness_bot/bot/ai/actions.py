"""
P4.20 — Диспетчер диалога с function calling.
Заменяет chat_with_trainer: проверяет pending, вызывает модель с tools,
обрабатывает tool_call (с подтверждением или без), логирует токены и историю.
"""
import logging
from bot.ai.tools import ALL_TOOLS, TOOLS_BY_NAME
from bot.ai.clients import ask_groq_with_tools, ask_gemini_with_tools
from bot.cache.redis_client import (
    get_pending_action, set_pending_action, clear_pending_action,
    add_chat_message, invalidate_context, update_today_state,
)
from bot.db.base import async_session
from bot.db import crud

logger = logging.getLogger(__name__)

CONFIRMATION_PHRASES = [
    "да", "ок", "окей", "верно", "правильно", "именно",
    "yes", "ага", "угу", "всё верно", "все верно", "всё правильно",
    "норм", "пойдет", "пойдёт", "подходит", "ага всё так",
]

REJECTION_PHRASES = [
    "нет", "не то", "неправильно", "no", "отмена", "не так",
    "не верно", "неверно", "не правильно", "ошибка", "переделай",
]


def _is_confirmation(text: str) -> bool:
    low = text.strip().lower().rstrip(".!?")
    return any(
        low == p or low.startswith(p + " ") or low.startswith(p + ",")
        for p in CONFIRMATION_PHRASES
    )


def _is_rejection(text: str) -> bool:
    low = text.strip().lower().rstrip(".!?")
    return any(
        low == p or low.startswith(p + " ") or low.startswith(p + ",")
        for p in REJECTION_PHRASES
    )


def _is_likely_correction(text: str, pending_type: str) -> bool:
    """Gрубая проверка — похоже ли сообщение на правку текущего pending-действия."""
    low = text.lower()
    if pending_type == "propose_workout":
        return any(kw in low for kw in ["вес", "кг", "подход", "повтор", "не так", "переделай", "замени"])
    if pending_type == "propose_reminder":
        return any(kw in low for kw in ["время", "час", "минут", "не то время", "перенеси"])
    return False


def _text_needs_tools(text: str) -> bool:
    """Предварительный фильтр — не передаёт tools на очевидно-разговорных сообщениях."""
    low = text.lower()
    has_digit = any(c.isdigit() for c in text)
    keyword_hit = any(kw in low for kw in [
        "тренировк", "подход", "повтор", "напомни", "напомина",
        "съел", "выпил", "поел", "грамм", "г ",
    ])
    return has_digit or keyword_hit


async def handle_message_with_actions(user_id: int, text: str, system_prompt: str, bot) -> str:
    """
    Главная точка входа. Возвращает текст ответа юзеру.
    Обрабатывает: подтверждение/отказ/правку/нерелевантное при pending.
    """
    pending = await get_pending_action(user_id)

    if pending:
        logger.info(f"[TOOLS_TEST] pending_action exists: type={pending.get('type')} payload={pending.get('payload')}")
        logger.info(f"[TOOLS_TEST] user_text={text!r} is_confirmation={_is_confirmation(text)} is_rejection={_is_rejection(text)}")

        if _is_confirmation(text):
            result_text = await _execute_pending_action(user_id, pending, bot)
            await clear_pending_action(user_id)
            await add_chat_message(user_id, "user", text)
            await add_chat_message(user_id, "assistant", result_text)
            return result_text

        if _is_rejection(text):
            await clear_pending_action(user_id)
            reply = "Хорошо, отменил. Расскажи ещё раз, как нужно правильно?"
            await add_chat_message(user_id, "user", text)
            await add_chat_message(user_id, "assistant", reply)
            return reply

        if _is_likely_correction(text, pending["type"]):
            logger.info(f"[TOOLS_TEST] pending cleared — user likely correcting: {text!r}")
            await clear_pending_action(user_id)
        else:
            reply = await _handle_unrelated_message(user_id, text, system_prompt, bot)
            warning = f"\n\n⚠️ Напомню, у тебя ещё не подтверждено: {pending['confirmation_text']}"
            return reply + warning

    tools = ALL_TOOLS if _text_needs_tools(text) else []
    max_tok = 300 if tools else 150
    result = await ask_groq_with_tools(system_prompt, text, tools, max_tokens=max_tok)
    if result is None:
        result = await ask_gemini_with_tools(system_prompt, text, ALL_TOOLS, max_tokens=300)
    if result is None:
        return "⚠️ ИИ временно недоступен, попробуй позже."

    kind, data, tok_in, tok_out = result
    logger.info(f"[TOOLS_TEST] input={text!r} kind={kind} data={data!r}")

    try:
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
            if user:
                await crud.add_ai_usage_log(session, user.id, "groq", tok_in, tok_out)
    except Exception as e:
        logger.warning(f"Failed to log AI usage: {e}")

    if kind == "text":
        await add_chat_message(user_id, "user", text)
        await add_chat_message(user_id, "assistant", data)
        return data

    results = []
    for call in data:
        action_name = call["name"]
        args = call["arguments"]
        tool_def = TOOLS_BY_NAME.get(action_name)

        if tool_def and not tool_def.get("requires_confirmation", True):
            r = await _execute_action_now(user_id, action_name, args, bot)
            results.append(r)
        else:
            confirmation_text = await _build_confirmation_text(action_name, args)
            await set_pending_action(user_id, action_name, args, confirmation_text)
            results.append(confirmation_text)
            break

    combined_reply = "\n".join(results)
    await add_chat_message(user_id, "user", text)
    await add_chat_message(user_id, "assistant", combined_reply)
    return combined_reply


async def _handle_unrelated_message(user_id: int, text: str, system_prompt: str, bot) -> str:
    """Обрабатывает сообщение как новый запрос, НЕ трогая существующий pending."""
    result = await ask_groq_with_tools(system_prompt, text, ALL_TOOLS, max_tokens=300)
    if result is None:
        result = await ask_gemini_with_tools(system_prompt, text, ALL_TOOLS, max_tokens=300)
    if result is None:
        return "⚠️ ИИ временно недоступен."

    kind, data, tok_in, tok_out = result
    if kind == "text":
        await add_chat_message(user_id, "user", text)
        await add_chat_message(user_id, "assistant", data)
        return data

    for call in data:
        action_name = call["name"]
        args = call["arguments"]
        tool_def = TOOLS_BY_NAME.get(action_name)
        if tool_def and not tool_def.get("requires_confirmation", True):
            result_text = await _execute_action_now(user_id, action_name, args, bot)
            await add_chat_message(user_id, "user", text)
            await add_chat_message(user_id, "assistant", result_text)
            return result_text
        return "Сначала подтверди предыдущее действие (да/нет), потом продолжим с этим."

    return "Не понял, уточни."


async def _build_confirmation_text(action_name: str, args: dict) -> str:
    if action_name == "propose_workout":
        exercises_lines = "\n".join(
            f"  • {ex['name']} — {ex['sets']}×{ex['reps']}"
            + (f", {ex['weight_kg']}кг" if ex.get("weight_kg") else "")
            for ex in args["exercises"]
        )
        return (
            f"Понял, записываю тренировку «{args['workout_name']}»:\n"
            f"{exercises_lines}\n\n"
            f"Всё верно? (да/нет)"
        )

    if action_name == "propose_reminder":
        advance = args.get("advance_warning_minutes", 0)
        advance_text = f", предупрежу за {advance} мин" if advance else ""
        return (
            f"Хорошо, новое напоминание: «{args['label']}» в {args['time']}{advance_text}.\n"
            f"Всё верно? (да/нет)"
        )

    return "Не уверен, что правильно понял. Можешь переформулировать?"


async def _execute_pending_action(user_id: int, pending: dict, bot) -> str:
    action_type = pending["type"]
    payload = pending["payload"]

    if action_type == "propose_workout":
        return await _create_workout(user_id, payload)

    if action_type == "propose_reminder":
        return await _create_reminder(user_id, payload, bot)

    return "Что-то пошло не так, попробуй ещё раз."


async def _execute_action_now(user_id: int, action_name: str, args: dict, bot) -> str:
    if action_name == "log_food_item":
        return await _log_food_item(user_id, args)

    if action_name == "propose_workout":
        return await _create_workout(user_id, args)

    if action_name == "propose_reminder":
        return await _create_reminder(user_id, args, bot)

    return "Не знаю, как это сделать."


async def _log_food_item(user_id: int, args: dict) -> str:
    from bot.handlers.food import search_food

    food_name = args["food_name"]
    weight_g = args["weight_g"]

    food_data = await search_food(food_name)
    if not food_data:
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
            if user:
                await crud.add_meal_log(
                    session, user_id=user.id, food_name=food_name, weight_g=weight_g,
                    calories=0, protein=0, fat=0, carbs=0, source="ai",
                )
        await invalidate_context(user_id)
        await update_today_state(user_id, calories_in=0, protein=0, fat=0, carbs=0)
        return f"Записал: {food_name} {weight_g}г (КБЖУ не найдено, уточни позже)."

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
    await update_today_state(
        user_id,
        calories_in=food_data["calories"] * factor,
        protein=food_data["protein"] * factor,
        fat=food_data["fat"] * factor,
        carbs=food_data["carbs"] * factor,
    )
    return (
        f"Записано: {food_name} {weight_g}г\n"
        f"{food_data['calories'] * factor:.0f}ккал | "
        f"{food_data['protein'] * factor:.1f}г бел | "
        f"{food_data['fat'] * factor:.1f}г жир | "
        f"{food_data['carbs'] * factor:.1f}г угл"
    )


async def _create_workout(user_id: int, payload: dict) -> str:
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return "Сначала пройди /onboarding."

        program = await crud.create_workout_program(
            session, user_id=user.id,
            name=payload["workout_name"],
        )
        for ex in payload["exercises"]:
            await crud.add_exercise(
                session, program_id=program.id,
                name=ex["name"], type="compound",
                planned_sets=ex["sets"],
                planned_reps=str(ex["reps"]),
                planned_weight_kg=ex.get("weight_kg", 0),
            )

    return f"✅ Тренировка «{payload['workout_name']}» добавлена."


async def _create_reminder(user_id: int, payload: dict, bot) -> str:
    from bot.scheduler.reminders import scheduler
    from apscheduler.triggers.cron import CronTrigger

    h, m = map(int, payload["time"].split(":"))

    async def send_reminder():
        await bot.send_message(chat_id=user_id, text=f"⏰ {payload['label']}")

    job_id = f"custom_{user_id}_{payload['label'].replace(' ', '_')}_{payload['time']}"
    scheduler.add_job(
        send_reminder, CronTrigger(hour=h, minute=m),
        id=job_id, replace_existing=True,
    )

    advance = payload.get("advance_warning_minutes", 0)
    if advance > 0:
        adv_m = m - advance
        adv_h = h
        while adv_m < 0:
            adv_m += 60
            adv_h -= 1
        adv_h %= 24

        async def send_advance():
            await bot.send_message(chat_id=user_id, text=f"⏰ Через {advance} мин: {payload['label']}")

        scheduler.add_job(
            send_advance, CronTrigger(hour=adv_h, minute=adv_m),
            id=f"{job_id}_advance", replace_existing=True,
        )

    return f"✅ Напоминание «{payload['label']}» на {payload['time']} добавлено."
