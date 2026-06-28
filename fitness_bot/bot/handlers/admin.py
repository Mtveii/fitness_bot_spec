"""
Admin panel v3: pure callback router, no ConversationHandler.
"""
import asyncio
import logging
from datetime import datetime, UTC, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.db.base import async_session
from bot.db import crud
from bot.config import ADMIN_ID

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пользователи", callback_data="adm_users")],
        [InlineKeyboardButton("Статистика ИИ", callback_data="adm_ai")],
        [InlineKeyboardButton("Сводка за неделю", callback_data="adm_summary")],
        [InlineKeyboardButton("Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton("Система", callback_data="adm_system")],
        [InlineKeyboardButton("Назад в меню", callback_data="menu_main")],
    ])


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="adm_menu")]])


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text("Админ-панель", reply_markup=_admin_main_kb())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    d = q.data

    if not _is_admin(q.from_user.id):
        await q.answer("Недостаточно прав.", show_alert=True)
        return

    if d == "adm_menu":
        await q.edit_message_text("Админ-панель", reply_markup=_admin_main_kb())
    elif d == "adm_users":
        await _show_users(q)
    elif d.startswith("adm_user_"):
        await _show_user_detail(q, int(d.split("_")[2]))
    elif d.startswith("adm_msg_"):
        tg_id = int(d.split("_")[2])
        context.user_data["msg_target"] = tg_id
        await q.edit_message_text(f"Введи сообщение для {tg_id}:")
    elif d.startswith("adm_reset_"):
        tg_id = int(d.split("_")[2])
        from bot.cache.redis_client import reset_today_state
        await reset_today_state(tg_id)
        await q.edit_message_text(f"Today-state сброшен для {tg_id}.", reply_markup=_back_kb())
    elif d.startswith("adm_delete_"):
        await _delete_user(q, int(d.split("_")[2]))
    elif d == "adm_ai":
        await _show_ai_stats(q)
    elif d == "adm_summary":
        await _show_summary(q)
    elif d == "adm_broadcast":
        context.user_data["awaiting_broadcast"] = True
        await q.edit_message_text("Введи текст рассылки:")
    elif d == "adm_bcast_confirm":
        await _broadcast_send(q, context)
    elif d == "adm_system":
        await _show_system(q)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if handled."""
    if context.user_data.pop("awaiting_broadcast", False):
        text = update.message.text.strip()
        context.user_data["broadcast_text"] = text
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Отправить всем", callback_data="adm_bcast_confirm")],
            [InlineKeyboardButton("Отмена", callback_data="adm_menu")],
        ])
        await update.message.reply_text(f"Предпросмотр:\n\n{text}\n\nОтправить?", reply_markup=kb)
        return True

    if "msg_target" in context.user_data:
        tg_id = context.user_data.pop("msg_target")
        try:
            await context.bot.send_message(chat_id=tg_id, text=f"От тренера:\n\n{update.message.text}")
            await update.message.reply_text(f"Отправлено {tg_id}", reply_markup=_back_kb())
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}", reply_markup=_back_kb())
        return True

    return False


async def _show_users(q) -> None:
    async with async_session() as session:
        users = await crud.get_all_users(session)
    if not users:
        await q.edit_message_text("Нет пользователей.", reply_markup=_back_kb())
        return
    buttons = []
    for u in users[:15]:
        buttons.append([InlineKeyboardButton(
            f"{u.name} ({u.tg_id})", callback_data=f"adm_user_{u.tg_id}"
        )])
    buttons.append([InlineKeyboardButton("Назад", callback_data="adm_menu")])
    await q.edit_message_text(f"Всего: {len(users)}", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_user_detail(q, tg_id: int) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, tg_id)
        if not user:
            await q.edit_message_text("Не найден.", reply_markup=_back_kb())
            return
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        meals = await crud.get_meals_between(session, user.id, today, datetime.now(UTC))
        workouts = await crud.get_workout_logs_between(session, user.id, today, datetime.now(UTC))
        last_sleep = await crud.get_last_sleep(session, user.id)

    cal_today = sum(m.calories for m in meals)
    prot_today = sum(m.protein for m in meals)
    supps = user.supplements or []
    supp_str = ", ".join(f"{s['name']} {s['dose']}" for s in supps) if supps else "нет"

    lines = [
        f"Имя: {user.name}",
        f"ID: {user.tg_id} | Пол: {user.gender} | Возраст: {user.age}",
        f"Рост: {user.height_cm}см | Вес: {user.weight_kg}кг -> {user.target_weight_kg}кг",
        f"Активность: {user.activity_level} | Цель: {user.goal}",
        f"",
        f"Сегодня:",
        f"  Калории: {cal_today:.0f}ккал",
        f"  Белок: {prot_today:.0f}г",
        f"  Еда: {len(meals)} | Тренировки: {len(workouts)}",
        f"  Сон: {last_sleep.duration_hours:.1f}ч" if last_sleep else "  Сон: -",
        f"",
        f"Добавки: {supp_str}",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Написать", callback_data=f"adm_msg_{tg_id}")],
        [InlineKeyboardButton("Сбросить today", callback_data=f"adm_reset_{tg_id}")],
        [InlineKeyboardButton("Удалить", callback_data=f"adm_delete_{tg_id}")],
        [InlineKeyboardButton("К списку", callback_data="adm_users")],
    ])
    await q.edit_message_text("\n".join(lines), reply_markup=kb)


async def _delete_user(q, tg_id: int) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, tg_id)
        if user:
            from sqlalchemy import delete as sa_delete
            from bot.db.models import User
            await session.execute(sa_delete(User).where(User.tg_id == tg_id))
            await session.commit()
            await q.edit_message_text(f"{tg_id} удален.", reply_markup=_back_kb())
        else:
            await q.edit_message_text("Не найден.", reply_markup=_back_kb())


async def _show_ai_stats(q) -> None:
    async with async_session() as session:
        week_ago = datetime.now(UTC) - timedelta(days=7)
        stats = await crud.get_ai_usage_summary(session, since=week_ago)
        from sqlalchemy import select, func
        from bot.db.models import AIUsageLog
        total_r = await session.execute(
            select(func.count(AIUsageLog.id)).where(AIUsageLog.timestamp >= week_ago)
        )
        total = total_r.scalar() or 0

    lines = [f"ИИ за неделю: {total} запросов\n"]
    if stats:
        for s in stats:
            lines.append(f"  {s['provider']}: {s['requests']} запросов, {s['tokens_in']}->{s['tokens_out']} токенов")
    else:
        lines.append("  Нет данных.")
    await q.edit_message_text("\n".join(lines), reply_markup=_back_kb())


async def _show_summary(q) -> None:
    async with async_session() as session:
        users = await crud.get_all_users(session)
        from bot.db.models import MealLog, WorkoutLog, SleepLog, WeightHistory
        week_ago = datetime.now(UTC) - timedelta(days=7)
        from sqlalchemy import func as sa_func
        meals_r = await session.execute(select(sa_func.count(MealLog.id)).where(MealLog.date >= week_ago))
        workouts_r = await session.execute(select(sa_func.count(WorkoutLog.id)).where(WorkoutLog.date >= week_ago))
        sleep_r = await session.execute(select(sa_func.count(SleepLog.id)).where(SleepLog.date >= week_ago))
        weight_r = await session.execute(select(sa_func.count(WeightHistory.id)).where(WeightHistory.date >= week_ago))
        total_cal_r = await session.execute(
            select(sa_func.coalesce(sa_func.sum(MealLog.calories), 0)).where(MealLog.date >= week_ago)
        )

    lines = [
        "Сводка за неделю:\n",
        f"Пользователей: {len(users)}",
        f"Записей еды: {meals_r.scalar() or 0}",
        f"  Калорий: {total_cal_r.scalar() or 0:.0f}",
        f"Тренировок: {workouts_r.scalar() or 0}",
        f"Записей сна: {sleep_r.scalar() or 0}",
        f"Взвешиваний: {weight_r.scalar() or 0}",
    ]
    await q.edit_message_text("\n".join(lines), reply_markup=_back_kb())


async def _broadcast_send(q, context) -> None:
    text = context.user_data.pop("broadcast_text", "")
    if not text:
        await q.edit_message_text("Пусто.", reply_markup=_back_kb())
        return
    async with async_session() as session:
        users = await crud.get_all_users(session)
    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u.tg_id, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await q.edit_message_text(f"Рассылка: {sent} отправлено, {failed} ошибок", reply_markup=_back_kb())


async def _show_system(q) -> None:
    import os
    from bot.cache.redis_client import USE_REDIS
    from bot.scheduler.reminders import scheduler

    jobs = scheduler.get_jobs()
    job_types = {}
    for j in jobs:
        prefix = j.id.split("_")[0] if "_" in j.id else j.id
        job_types[prefix] = job_types.get(prefix, 0) + 1
    job_summary = ", ".join(f"{k}: {v}" for k, v in sorted(job_types.items()))

    lines = [
        "Система:\n",
        f"Redis: {'OK' if USE_REDIS else 'нет (файлы)'}",
        f"Всего джоб: {len(jobs)}",
        f"  {job_summary}",
        f"",
        f"API ключи:",
        f"  BOT_TOKEN: {'OK' if os.getenv('BOT_TOKEN') else 'нет'}",
        f"  GROQ: {'OK' if os.getenv('GROQ_API_KEY') else 'нет'}",
        f"  GEMINI: {'OK' if os.getenv('GEMINI_API_KEY') else 'нет'}",
        f"  USDA: {'OK' if os.getenv('USDA_API_KEY') else 'нет'}",
    ]
    await q.edit_message_text("\n".join(lines), reply_markup=_back_kb())
