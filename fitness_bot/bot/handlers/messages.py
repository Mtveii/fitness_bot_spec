import json
import logging
import datetime
import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from bot.ai.clients import ask_ai_race, ask_ai_stream
from bot.ai.prompts import SYSTEM_PROMPT
from bot.tools.definitions import get_tool_schema, MAIN_TOOL_NAMES, CONFIRM_TOOLS
from bot.tools.dispatcher import handle_tool_calls
from bot.cache.redis_client import cache_get, cache_set
from bot.db.models import User, UserMemoryProfile, TimeObservation, ObservationType, ObservationSource
from bot.db.base import async_session
from sqlalchemy import select
from bot.config import DIALOG_BUFFER_TTL

logger = logging.getLogger(__name__)

STREAM_EDIT_MIN_INTERVAL = 0.5
STREAM_MAX_MSG_LEN = 4096

SIGNAL_KEYWORDS = {
    "еда", "ел", "ест", "поел", "съел", "попил", "выпил", "вода", "завтрак",
    "обед", "ужин", "перекус", "калории", "белки", "жиры", "углеводы", "ккал",
    "тренировка", "тренирова", "упражнение", "занятие", "спорт", "бег", "ходьба",
    "сон", "спал", "проснулся", "уснул", "лёг", "отбой", "подъём",
    "вес", "вешу", "похудел", "поправился",
    "самочувствие", "настроение", "устал", "болит", "болен",
    "напомни", "напоминание", "напомнить",
    "статистик", "прогресс", "итоги", "результат",
    "профиль", "настройки", "тон", "общайся", "говори",
    "да", "нет", "подтверждаю", "отмена", "ок", "согласен", "отменяю",
    "недел", "месяц",
    "/", "log", "weight", "meal", "workout", "sleep",
}


CONFIRM_KEYWORDS = {"да", "нет", "ок", "ага", "угу", "согласен", "отмена", "отменяю", "конечно"}


def _should_use_tools(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower().strip()
    if len(text_lower) < 2:
        return False
    if text_lower in CONFIRM_KEYWORDS:
        return True
    if len(text_lower) < 4:
        return False
    if any(c.isdigit() for c in text):
        return True
    words = set(text_lower.split())
    for kw in SIGNAL_KEYWORDS:
        if len(kw) <= 2 and kw in words:
            return True
        elif len(kw) > 2 and kw in text_lower:
            return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.message or not update.message.text:
        return
    if not update.effective_user:
        return
    tg_id = update.effective_user.id
    user_text = update.message.text

    async with async_session() as session:
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

    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(current_time=current_time_str) + f"\n\nТон общения: {tone}\n\nПрофиль пользователя:\n{profile_summary}"},
        *history,
        {"role": "user", "content": user_text},
    ]

    pending_action = await cache_get(f"pending_action:{user_db_id}")
    if pending_action:
        context.user_data["_pending_reminder"] = pending_action.get("summary", "есть ожидающее действие")

    if _should_use_tools(user_text):
        tools = get_tool_schema(MAIN_TOOL_NAMES)
    else:
        tools = None
    response = await ask_ai_race(messages, tools=tools)

    tool_calls = response.get("tool_calls", [])
    ai_content = response.get("content", "")

    if tool_calls:
        results = await handle_tool_calls(tool_calls, user_db_id, context.user_data)

        tool_result_messages = []
        for r in results:
            tool_result_messages.append({
                "role": "tool",
                "content": json.dumps(r.result, ensure_ascii=False, default=str),
                "tool_call_id": r.tool_call_id,
            })

        has_only_deferred = all(
            r.result.get("status") == "deferred" for r in results
        )
        has_pending_action = any(r.is_pending for r in results)

        if has_only_deferred:
            reply = "Сначала подтверди или отмени ожидающее действие."
        else:
            assistant_msg = {
                "role": "assistant",
                "content": ai_content or None,
                "tool_calls": [
                    {"id": tc.get("id", ""), "type": "function",
                     "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                    for tc in tool_calls
                ],
            }
            followup = await ask_ai_race(
                messages + [assistant_msg] + tool_result_messages,
                tools=None, temperature=0.7, max_tokens=1024
            )
            reply = followup.get("content", "")

            if not reply:
                if has_pending_action:
                    pending_summaries = [r.pending_summary for r in results if r.pending_summary]
                    reply = f"Я предлагаю:\n• " + "\n• ".join(pending_summaries) + "\n\nПодтверждаешь? (да/нет)"
                else:
                    reply = "Готово! Что-то ещё?"
    else:
        reply = ai_content if ai_content else "Понял. Что-то ещё?"

    pending_reminder = context.user_data.pop("_pending_reminder", None)
    if pending_reminder and "подтверд" not in user_text.lower() and "отмен" not in user_text.lower():
        reply += f"\n\n⚠️Кстати, у тебя есть ожидающее действие: {pending_reminder}. Подтверди или отмени его."

    await _save_dialog_buffer(tg_id, user_text, reply)

    if not tool_calls and reply and len(reply) > 10:
        await _stream_response(update, messages, reply)
    else:
        await update.message.reply_text(reply)


async def _log_message_activity(user_db_id: int):
    try:
        async with async_session() as session:
            obs = TimeObservation(
                user_id=user_db_id,
                observation_type=ObservationType.message_activity,
                observed_at=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
                source=ObservationSource.inferred,
                confidence=0.3,
                raw_text="",
            )
            session.add(obs)
            await session.commit()
    except Exception as e:
        logger.warning(f"Failed to log message activity: {e}")


async def _get_tone(user_db_id: int) -> str:
    async with async_session() as session:
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
    data.append({"role": "user", "content": user_text[:500]})
    data.append({"role": "assistant", "content": bot_reply[:1000]})
    if len(data) > 10:
        data = data[-10:]
    await cache_set(key, data, ttl=DIALOG_BUFFER_TTL)


async def _stream_response(update: Update, messages: list, fallback_text: str):
    sent = await update.message.reply_text("...")
    buffer = ""
    last_edit = 0.0
    msg_id = sent.message_id
    chat_id = update.effective_chat.id

    async def on_token(token: str):
        nonlocal buffer, last_edit
        buffer += token
        now = asyncio.get_event_loop().time()
        if now - last_edit >= STREAM_EDIT_MIN_INTERVAL and len(buffer) > 5:
            last_edit = now
            text = buffer[:STREAM_MAX_MSG_LEN]
            try:
                await update.get_bot().edit_message_text(
                    chat_id=chat_id, message_id=msg_id, text=text
                )
            except Exception:
                pass

    await ask_ai_stream(messages, on_token=on_token)

    final = buffer.strip() if buffer.strip() else fallback_text
    if len(final) > STREAM_MAX_MSG_LEN:
        final = final[:STREAM_MAX_MSG_LEN]
    try:
        await update.get_bot().edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=final
        )
    except Exception:
        try:
            await sent.edit_text(final)
        except Exception:
            pass
