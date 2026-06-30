import json
import logging
import datetime

from telegram import Update
from telegram.ext import ContextTypes

from bot.ai.clients import ask_ai_race
from bot.ai.prompts import SYSTEM_PROMPT
from bot.tools.definitions import get_tool_schema, MAIN_TOOL_NAMES, CONFIRM_TOOLS
from bot.tools.dispatcher import handle_tool_calls
from bot.cache.redis_client import cache_get, cache_set
from bot.db.models import User, UserMemoryProfile, TimeObservation, ObservationType, ObservationSource
from bot.db.base import async_session
from bot.config import DIALOG_BUFFER_TTL

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    user_text = update.message.text

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        from bot.handlers.commands import start_command
        await start_command(update, context)
        return

    user_db_id = user.id
    context.user_data["user_db_id"] = user_db_id

    await _log_message_activity(user_db_id)

    tone = await _get_tone(user_db_id)
    history = await _get_dialog_buffer(tg_id)

    profile_summary = await _get_profile_summary(user, user_db_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n\nТон общения: {tone}\n\nПрофиль пользователя:\n{profile_summary}"},
        *history,
        {"role": "user", "content": user_text},
    ]

    tools = get_tool_schema(MAIN_TOOL_NAMES)
    response = await ask_ai_race(messages, tools=tools)

    tool_calls = response.get("tool_calls", [])
    ai_content = response.get("content", "")

    if tool_calls:
        results = await handle_tool_calls(tool_calls, user_db_id, context.user_data)
        tool_messages = []
        for r in results:
            if ai_content:
                tool_messages.append(f"[Tool: {r.tool_name}] -> {json.dumps(r.result, ensure_ascii=False)}")
        if ai_content:
            reply = ai_content
            if tool_messages:
                reply += "\n\n" + "\n".join(tool_messages)
        else:
            pending_summaries = [r.pending_summary for r in results if r.pending_summary]
            if pending_summaries:
                reply = f"Я предлагаю:\n• " + "\n• ".join(pending_summaries) + "\n\nПодтверждаешь? (да/нет)"
            else:
                completed = [r for r in results if not r.is_pending and r.result.get("status") == "ok"]
                if completed:
                    reply = "Готово! Что-то ещё?"
                else:
                    reply = "Понял. Что-то ещё?"
    else:
        reply = ai_content if ai_content else "Понял. Что-то ещё?"

    await _save_dialog_buffer(tg_id, user_text, reply)

    await update.message.reply_text(reply)


async def _log_message_activity(user_db_id: int):
    async with async_session() as session:
        obs = TimeObservation(
            user_id=user_db_id,
            observation_type=ObservationType.message_activity,
            observed_at=datetime.datetime.utcnow(),
            source=ObservationSource.inferred,
            confidence=0.3,
            raw_text="",
        )
        session.add(obs)
        await session.commit()


async def _get_tone(user_db_id: int) -> str:
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_db_id)
        )
        profile = result.scalar_one_or_none()
        if profile and profile.communication_tone:
            return profile.communication_tone
    return "friendly"


async def _get_profile_summary(user: User, user_db_id: int) -> str:
    lines = [
        f"Имя: {user.name}",
        f"Пол: {user.gender}",
        f"Возраст: {user.age}",
        f"Рост: {user.height_cm}см",
        f"Вес: {user.weight_kg}кг",
        f"Цель: {user.goal}",
        f"Активность: {user.activity_level}",
    ]
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_db_id)
        )
        mp = result.scalar_one_or_none()
        if mp:
            if mp.preferences_summary:
                lines.append(f"Сводка предпочтений: {mp.preferences_summary}")
            if mp.avg_wake_time:
                lines.append(f"Типичный подъём: {mp.avg_wake_time}")
            if mp.avg_sleep_time:
                lines.append(f"Типичный отбой: {mp.avg_sleep_time}")
    return "\n".join(lines)


async def _get_dialog_buffer(tg_id: int) -> list:
    key = f"dialog_buffer:{tg_id}"
    data = await cache_get(key)
    if data and isinstance(data, list):
        return data[-6:]
    return []


async def _save_dialog_buffer(tg_id: int, user_text: str, bot_reply: str):
    key = f"dialog_buffer:{tg_id}"
    data = await cache_get(key) or []
    data.append({"role": "user", "content": user_text})
    data.append({"role": "assistant", "content": bot_reply})
    if len(data) > 10:
        data = data[-10:]
    await cache_set(key, data, ttl=DIALOG_BUFFER_TTL)
