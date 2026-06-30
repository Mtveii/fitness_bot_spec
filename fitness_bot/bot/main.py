import os
import time
import signal
import asyncio
import logging
from datetime import datetime, UTC, timedelta, date
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from bot.db.base import init_db, async_session
from bot.db import crud
from bot.handlers.onboarding import get_onboarding_handler, onb_restart_callback
from bot.handlers.profile import get_me_handler
from bot.handlers.food import get_food_handler
from bot.handlers.workout import (
    is_workout_message, is_new_workout_message,
    workout_ai_start, workout_ai_continue,
    new_workout_ai_start, new_workout_ai_continue,
    is_manual_mode, handle_rpe_input,
)
from bot.handlers.commands import (
    get_today_handler, get_weight_handler, get_sleep_handler,
    get_steps_handler, get_week_handler, get_settings_handler,
    get_cancel_handler, get_export_handler,
    get_progress_handler, get_suggest_handler, get_debug_handler,
    today, cancel_command,
)
from bot.handlers.admin import admin_entry, admin_callback, admin_text_input, _is_admin, _admin_main_kb
from bot.scheduler.reminders import scheduler
from apscheduler.triggers.cron import CronTrigger
from bot.calculators.tdee import bmr, tdee
from bot.calculators.nutrition import daily_targets
from bot.handlers.commands import format_progress_bar
from bot.cache.redis_client import get_today_state, update_today_state
from bot.queue.throttle import is_rate_limited
from bot.config import ADMIN_ID

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INTENT_KEYWORDS = {
    "today": ["сколько калорий", "сколько я съел", "мои калории", "баланс калорий",
              "сколько осталось", "что я ел", "что я съел", "мой прогресс"],
    "cancel": ["отмени", "удали последнее", "убери последний", "отмена записи"],
    "weight_query": ["мой вес", "сколько я вешу", "текущий вес"],
    "steps_query": ["мои шаги", "шаги сегодня", "сколько прошёл"],
}


def _match_hardcoded_intent(text: str) -> str | None:
    low = text.lower()
    for intent, phrases in INTENT_KEYWORDS.items():
        if any(p in low for p in phrases):
            return intent
    return None


async def _get_proactive_suggestion(user_id: int) -> str | None:
    from datetime import datetime, UTC
    from bot.db.base import async_session
    from bot.db import crud
    from bot.calculators.tdee import bmr as _bmr, tdee as _tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return None
        today_workout = await crud.get_today_workout(session, user.id)
        last_sleep = await crud.get_last_sleep(session, user.id)

    state = await get_today_state(user_id)
    bmr_val = _bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = _tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    suggestions = []

    if state["calories_in"] == 0:
        suggestions.append(("high", "Ты сегодня ещё ничего не ел. Запиши приём пищи."))
    if state["protein"] < targets["protein_g"] * 0.5:
        deficit = targets["protein_g"] - state["protein"]
        suggestions.append(("high", f"Белка мало! Нужно ещё ~{deficit:.0f}г. Добавь творог, курицу или яйца."))
    if not today_workout:
        suggestions.append(("medium", "Сегодня ещё не тренировался."))
    if not last_sleep or (datetime.now(UTC).replace(tzinfo=None) - last_sleep.date.replace(tzinfo=None)).days > 1:
        suggestions.append(("medium", "Нет данных о сне за последние дни."))
    if state["steps"] == 0:
        suggestions.append(("low", "Шагов пока 0. Прошёл немного — напиши число."))

    if not suggestions:
        return None

    priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda x: priority_order[x[0]])

    lines = [s[1] for s in suggestions[:3]]
    return "Что можно сделать:\n" + "\n".join(lines)


# ─── Menu ─────────────────────────────────────────────────

def _build_main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Сегодня", callback_data="menu_today"),
         InlineKeyboardButton("🍽 Записать еду", callback_data="menu_food")],
        [InlineKeyboardButton("🏋️ Тренировка", callback_data="menu_workout"),
         InlineKeyboardButton("⚖️ Вес", callback_data="menu_weight")],
        [InlineKeyboardButton("😴 Сон", callback_data="menu_sleep"),
         InlineKeyboardButton("👟 Шаги", callback_data="menu_steps")],
        [InlineKeyboardButton("🧍 Профиль", callback_data="menu_me"),
         InlineKeyboardButton("💡 Рекомендации", callback_data="menu_suggest")],
        [InlineKeyboardButton("📈 Графики", callback_data="menu_charts"),
         InlineKeyboardButton("📅 Неделя", callback_data="menu_week")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
         InlineKeyboardButton("💬 Тренер", callback_data="menu_chat")],
        [InlineKeyboardButton("🔬 Отладка", callback_data="menu_debug"),
         InlineKeyboardButton("❓ Помощь", callback_data="menu_help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Админ-панель", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    await update.message.reply_text(
        f"Привет, {update.effective_user.first_name}!",
        reply_markup=_build_main_menu(is_admin),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    await update.message.reply_text("📌 Главное меню:", reply_markup=_build_main_menu(is_admin))


# ─── Menu callback router ──────────────────────────────────


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "menu_main":
        await _show_main_menu(q, q.from_user.id)

    elif d == "menu_today":
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
            if not user:
                await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
                return
            today_workout = await crud.get_today_workout(session, user.id)
            last_sleep = await crud.get_last_sleep(session, user.id)
        state = await get_today_state(q.from_user.id)
        bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
        tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
        targets = daily_targets(tdee_val, user.weight_kg, user.goal)
        now = datetime.now(UTC)
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        cal_pct = (state["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
        prot_pct = (state["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0
        fat_pct = (state["fat"] / targets["fat_g"] * 100) if targets["fat_g"] > 0 else 0
        carb_pct = (state["carbs"] / targets["carbs_g"] * 100) if targets["carbs_g"] > 0 else 0
        sleep_text = "😴 —"
        if last_sleep:
            days_ago = (now.replace(tzinfo=None) - last_sleep.date.replace(tzinfo=None)).days
            if days_ago <= 1:
                sleep_text = f"😴 {last_sleep.duration_hours:.1f}ч"
        workout_text = ""
        if today_workout:
            workout_text = f"🏋️ {today_workout.workout_name}\n   Объём {today_workout.total_volume:.0f}кг (+{today_workout.calories_burned:.0f}ккал)\n"
        balance = state["calories_in"] - targets["calories"]
        from bot.handlers.commands import format_progress_bar
        text = (
            f"📅 {day_names[now.weekday()]}, {now.strftime('%d.%m')}\n\n"
            f"🔥 {state['calories_in']:.0f}/{targets['calories']:.0f} ккал {format_progress_bar(state['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
            f"🥩 {state['protein']:.0f}/{targets['protein_g']:.0f}г {format_progress_bar(state['protein'], targets['protein_g'])} {prot_pct:.0f}%\n"
            f"🧈 {state['fat']:.0f}/{targets['fat_g']:.0f}г {format_progress_bar(state['fat'], targets['fat_g'])} {fat_pct:.0f}%\n"
            f"🍞 {state['carbs']:.0f}/{targets['carbs_g']:.0f}г {format_progress_bar(state['carbs'], targets['carbs_g'])} {carb_pct:.0f}%\n\n"
            f"👟 {state['steps']}\n{workout_text}{sleep_text}\n\n"
            f"⚖️ {balance:+.0f} ккал"
        )
        await q.edit_message_text(text, reply_markup=_BACK())

    elif d == "menu_food":
        await q.edit_message_text(
            "🍽 Как записать еду?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📷 Фото еды", callback_data="menu_food_photo")],
                [InlineKeyboardButton("✏️ Текстом", callback_data="menu_food_text")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "menu_food_photo":
        context.user_data["awaiting_photo"] = True
        await q.edit_message_text(
            "📷 Отправь фото еды:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_food")],
            ]),
        )

    elif d == "menu_food_text":
        await q.edit_message_text(
            "✏️ Введи что съел:\n\n"
            "Примеры:\n"
            "• 200г гречки с курицей\n"
            "• 150г творога\n"
            "• поел кашу 300г",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_food")],
            ]),
        )

    elif d == "menu_workout":
        await q.edit_message_text(
            "🏋️ Тренировки:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Начать тренировку", callback_data="menu_workout_log")],
                [InlineKeyboardButton("➕ Создать программу", callback_data="menu_workout_new")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "menu_workout_log":
        await q.edit_message_text(
            "🏋️ Опиши тренировку:\n"
            "Упражнения, подходы, вес, длительность.\n"
            "Пример: «Жим лёжа 80кг 4×8, присед 100кг 5×5, 45 мин»\n\n"
            "Когда закончишь — напиши «всё».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_workout")],
            ]),
        )
        context.user_data["workout_session"] = {"exercises": [], "duration_minutes": 45, "calories_burned": 0}

    elif d == "menu_workout_new":
        await q.edit_message_text(
            "➕ Создай программу: /new_workout",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_workout")],
            ]),
        )

    elif d == "menu_sleep":
        await q.edit_message_text(
            "😴 Запиши сон:\n\n"
            "Формат: /sleep 23:00 07:00",
            reply_markup=_BACK(),
        )

    elif d == "menu_steps":
        await q.edit_message_text(
            "👟 Запиши шаги:\n\n"
            "Формат: /steps 8000\n"
            "Или просто напиши: прошёл 8000",
            reply_markup=_BACK(),
        )

    elif d == "menu_weight":
        await q.edit_message_text(
            "⚖️ Обнови вес:\n\n"
            "Формат: /weight 85.5\n"
            "Или просто напиши: вес 85.5",
            reply_markup=_BACK(),
        )

    elif d == "menu_charts":
        await q.edit_message_text(
            "📈 Какой график показать?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏋️ Вес и тренировки", callback_data="chart_workout_weight")],
                [InlineKeyboardButton("🔥 Калории (баланс)", callback_data="chart_calories")],
                [InlineKeyboardButton("😴 Сон", callback_data="chart_sleep")],
                [InlineKeyboardButton("📅 Прогресс", callback_data="menu_progress")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "chart_workout_weight":
        await q.edit_message_text("📈 Загружаю график...")
        await _send_workout_weight_chart(update, context)

    elif d == "chart_calories":
        await q.edit_message_text("📈 Загружаю график...")
        await _send_calories_chart(update, context)

    elif d == "chart_sleep":
        await q.edit_message_text("📈 Загружаю график...")
        await _send_sleep_chart(update, context)

    elif d.startswith("menu_prog_"):
        await q.edit_message_text("📈 Загружаю график...")
        days = int(d.split("_")[2])
        from bot.handlers.commands import progress
        context.args = [str(days)]
        await progress(update, context)
        await q.message.delete()

    elif d == "menu_progress":
        await q.edit_message_text(
            "📈 Графики за период:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 7 дней", callback_data="menu_prog_7")],
                [InlineKeyboardButton("📅 14 дней", callback_data="menu_prog_14")],
                [InlineKeyboardButton("📅 30 дней", callback_data="menu_prog_30")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "menu_suggest":
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
            if not user:
                await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
                return
            programs = await crud.get_user_programs(session, user.id)
            today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            recent_logs = await crud.get_workout_logs_between(session, user.id, today_start - timedelta(days=14), today_start + timedelta(days=1))
        if not programs:
            await q.edit_message_text("🏋️ У тебя пока нет программ. Создай: /new_workout", reply_markup=_BACK())
            return
        muscle_volume, muscle_count = {}, {}
        for log in recent_logs:
            if log.exercise_sets:
                for es in log.exercise_sets:
                    vol = es.weight_kg * es.reps
                    name_lower = es.exercise_name.lower()
                    for program in programs:
                        for ex in program.exercises:
                            if ex.name.lower() in name_lower:
                                for mg in (ex.muscle_groups or []):
                                    muscle_volume[mg] = muscle_volume.get(mg, 0) + vol
                                    muscle_count[mg] = muscle_count.get(mg, 0) + 1
        all_muscles = set()
        for p in programs:
            for ex in p.exercises:
                for mg in (ex.muscle_groups or []):
                    all_muscles.add(mg)
        muscle_avg = {}
        for mg in all_muscles:
            muscle_avg[mg] = muscle_volume.get(mg, 0) / muscle_count.get(mg, 1) if muscle_count.get(mg, 0) > 0 else 0
        sorted_m = sorted(muscle_avg.items(), key=lambda x: x[1])
        lines = ["🏋️ Рекомендации:\n"]
        for mg, avg_vol in sorted_m[:5]:
            lines.append(f"  {'⚠️' if avg_vol == 0 else '📉'} {mg} — {'не тренировался' if avg_vol == 0 else f'ср. объём {avg_vol:.0f}кг'}")
        today_name = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][datetime.now(UTC).weekday()]
        for p in programs:
            p_days = p.day_of_week if isinstance(p.day_of_week, list) else [p.day_of_week]
            if any(d.lower() == today_name.lower() for d in p_days):
                ex_names = ", ".join(e.name for e in p.exercises)
                lines.append(f"\n📅 Сегодня ({today_name}): {p.name}\n   {ex_names}")
                break
        await q.edit_message_text("\n".join(lines), reply_markup=_BACK())

    elif d == "menu_week":
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
            if not user:
                await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
                return
            week_start = datetime.now(UTC) - timedelta(days=7)
            meals = await crud.get_meals_between(session, user.id, week_start, datetime.now(UTC))
            workouts = await crud.get_workout_logs_between(session, user.id, week_start, datetime.now(UTC))
            sleep_logs = await crud.get_sleep_between(session, user.id, week_start, datetime.now(UTC))
            weight_history = await crud.get_weight_history(session, user.id, days=7)
        if not meals:
            await q.edit_message_text("📊 Нет данных за неделю. Начни с /log", reply_markup=_BACK())
            return
        days_with_meals = len(set(m.date.date() for m in meals))
        days = max(days_with_meals, 1)
        total_cal = sum(m.calories for m in meals)
        total_protein = sum(m.protein for m in meals)
        total_fat = sum(m.fat for m in meals)
        total_carbs = sum(m.carbs for m in meals)
        total_workout_vol = sum(w.total_volume or 0 for w in workouts)
        total_workout_kcal = sum(w.calories_burned or 0 for w in workouts)
        avg_sleep = sum(s.duration_hours for s in sleep_logs) / max(len(sleep_logs), 1) if sleep_logs else 0
        weight_text = ""
        if len(weight_history) >= 2:
            first = weight_history[0].weight_kg
            last = weight_history[-1].weight_kg
            weight_text = f"\n⚖️ Вес: {first:.1f} → {last:.1f}кг ({last - first:+.1f}кг)"
        bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
        tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
        targets = daily_targets(tdee_val, user.weight_kg, user.goal)
        lines = [
            f"📊 Неделя ({days_with_meals} дн. с едой)\n",
            f"🔥 Среднее: {total_cal / days:.0f} ккал/день",
            f"🥩 Средний белок: {total_protein / days:.0f}г/день",
            f"🧈 Средние жиры: {total_fat / days:.0f}г/день",
            f"🍞 Средние углеводы: {total_carbs / days:.0f}г/день",
            f"🏋️ Тренировок: {len(workouts)} | Объём: {total_workout_vol:.0f}кг",
            f"🔥 Сожжено на тренировках: {total_workout_kcal:.0f}ккал",
        ]
        if avg_sleep > 0:
            emoji = "😴" if avg_sleep >= 7 else "⚠️"
            lines.append(f"{emoji} Средний сон: {avg_sleep:.1f}ч")
        if weight_text:
            lines.append(weight_text)
        avg_deficit = targets["calories"] - (total_cal / 7)
        lines.append(f"\n📉 Средний дефицит: {avg_deficit:+.0f} ккал/день")
        await q.edit_message_text("\n".join(lines), reply_markup=_BACK())

    elif d == "menu_me":
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
        if not user:
            await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
            return
        bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
        tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
        targets = daily_targets(tdee_val, user.weight_kg, user.goal)
        supp_text = "\n".join(f"  • {s['name']} {s['dose']} ({', '.join(s.get('times', []))})" for s in (user.supplements or [])) or "  нет"
        GOAL_NAMES = {"cut": "Похудение", "bulk": "Набор", "recomp": "Рельеф", "maintain": "Поддержка"}
        ACT_NAMES = {"sedentary": "Сидячий", "light": "Лёгкий", "moderate": "Средний", "high": "Высокий"}
        text = (
            f"👤 {user.name}\n\n📋 Профиль:\n"
            f"  Пол: {'Муж' if user.gender == 'M' else 'Жен'} | {user.age} лет\n"
            f"  Рост: {user.height_cm} см\n"
            f"  Вес: {user.weight_kg} кг → цель {user.target_weight_kg} кг\n"
            f"  Активность: {ACT_NAMES.get(user.activity_level, user.activity_level)}\n"
            f"  Цель: {GOAL_NAMES.get(user.goal, user.goal)}\n\n"
            f"📊 Расчёты:\n  BMR: {bmr_val:.0f} ккал\n  TDEE: {tdee_val:.0f} ккал\n\n"
            f"🎯 Дневные нормы:\n  Калории: {targets['calories']} ккал\n"
            f"  Белок: {targets['protein_g']}г | Жиры: {targets['fat_g']}г | Углеводы: {targets['carbs_g']}г\n\n"
            f"💊 Добавки:\n{supp_text}\n\n"
            f"⏰ Подъём: {user.sleep_schedule.get('preferred_wake', '—')} | Сон: {user.sleep_schedule.get('preferred_sleep', '—')}"
        )
        await q.edit_message_text(text, reply_markup=_BACK())

    elif d == "menu_debug":
        from bot.handlers.commands import get_activity_log
        log = await get_activity_log(q.from_user.id)
        await q.edit_message_text(log[:4096], reply_markup=_BACK())

    elif d == "menu_settings":
        await _show_settings_menu(q, q.from_user.id)

    elif d == "menu_settings_ai":
        personality_names = {
            "strict": "💪 Строгий", "friendly": "😊 Дружелюбный",
            "motivating": "🔥 Мотивирующий", "sarcastic": "😂 Саркастичный",
            "scientific": "🔬 Научный", "gentle": "🤗 Нежный",
        }
        await q.edit_message_text(
            "🤖 Стиль ИИ-тренера:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(v, callback_data=f"menu_settings_ai_set_{k}")]
                for k, v in personality_names.items()
            ] + [[InlineKeyboardButton("◀️ Назад", callback_data="menu_settings")]]),
        )

    elif d.startswith("menu_settings_ai_set_"):
        personality = d.split("_")[-1]
        names = {"strict": "Строгий", "friendly": "Дружелюбный", "motivating": "Мотивирующий",
                 "sarcastic": "Саркастичный", "scientific": "Научный", "gentle": "Нежный"}
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
            if user:
                await crud.update_user(session, q.from_user.id, ai_personality=personality)
        from bot.cache.redis_client import invalidate_context
        await invalidate_context(q.from_user.id)
        await q.edit_message_text(
            f"🤖 Стиль: {names.get(personality, personality)} ✅",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_settings")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "menu_settings_reset":
        from bot.db.base import async_session
        from bot.db import crud
        async with async_session() as session:
            await crud.update_user(session, q.from_user.id, settings=None)
        await q.edit_message_text(
            "🔄 Настройки сброшены к дефолту.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_settings")],
                [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
            ]),
        )

    elif d == "menu_settings_notif":
        from bot.db.base import async_session
        from bot.db import crud
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
        notif = (user.settings or {}).get("notifications", {})
        notif_items = [
            ("supplements", "Добавки"), ("nutrition_deficit", "Недобор калорий"),
            ("workout_reminder", "Тренировки"), ("sleep", "Сон"),
            ("weekly_report", "Недельный отчёт"), ("water", "Вода"),
            ("weigh_in", "Взвешивание"), ("steps", "Шаги"),
        ]
        rows = [
            [InlineKeyboardButton(
                f"{'✅' if notif.get(k, True) else '❌'} {label}",
                callback_data=f"menu_settings_notif_toggle_{k}",
            )]
            for k, label in notif_items
        ]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_settings")])
        await q.edit_message_text("🔔 Уведомления:", reply_markup=InlineKeyboardMarkup(rows))

    elif d.startswith("menu_settings_notif_toggle_"):
        key = d.replace("menu_settings_notif_toggle_", "")
        from bot.db.base import async_session
        from bot.db import crud
        async with async_session() as session:
            user = await crud.get_user(session, q.from_user.id)
            if user:
                settings = user.settings or {}
                notif = settings.setdefault("notifications", {})
                notif[key] = not notif.get(key, True)
                await crud.update_user(session, q.from_user.id, settings=settings)
        await _show_settings_menu(q, q.from_user.id)

    elif d == "menu_help":
        await q.edit_message_text(
            "❓ Команды:\n"
            "/menu — главное меню\n"
            "/onboarding — настройка профиля\n"
            "/log — записать еду\n"
            "/today — сводка за сегодня\n"
            "/weight [кг] — обновить вес\n"
            "/sleep [отбой] [подъём] — записать сон\n"
            "/steps [n] — записать шаги\n"
            "/progress [дней] — графики\n"
            "/week — неделя\n"
            "/suggest — что тренировать\n"
            "/workout — тренировка\n"
            "/new_workout — создать программу тренировок\n"
            "/me — мой профиль\n"
            "/settings — настройки\n"
            "/debug — отладка\n"
            "/export — экспорт CSV\n\n"
            "Просто пиши текстом — понимаю без команд.",
            reply_markup=_BACK(),
        )

    elif d == "menu_chat":
        await q.edit_message_text(
            "💬 Напиши сообщение — отвечу как тренер.\n\n"
            "Примеры:\n"
            "• Как дела? — покажу статус\n"
            "• Что тренировать? — подскажу\n"
            "• Сколько белка осталось? — посчитаю",
            reply_markup=_BACK(),
        )

    elif d == "menu_admin":
        if not _is_admin(q.from_user.id):
            await q.answer("Нет доступа.", show_alert=True)
            return
        await q.edit_message_text("Админ-панель", reply_markup=_admin_main_kb())


async def _show_main_menu(q, user_id: int) -> None:
    from bot.db.base import async_session
    from bot.db import crud
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    is_admin = user and user.role == "admin"

    await q.edit_message_text("📌 Главное меню:", reply_markup=_build_main_menu(is_admin))


async def _show_settings_menu(q, user_id: int) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    settings = user.settings if user else {}
    personality = user.ai_personality if user else "friendly"
    notif = settings.get("notifications", {})
    notif_vals = notif.values()
    notif_status = "все вкл" if notif_vals and all(notif_vals) else "частично" if any(notif_vals) else "все выкл"
    await q.edit_message_text(
        f"⚙️ Настройки\n\n"
        f"🤖 Стиль ИИ: {personality}\n"
        f"🔔 Уведомления: {notif_status}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Уведомления", callback_data="menu_settings_notif")],
            [InlineKeyboardButton("🤖 Стиль ИИ", callback_data="menu_settings_ai")],
            [InlineKeyboardButton("🔄 Сбросить всё", callback_data="menu_settings_reset")],
            [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")],
        ]),
    )


def _menu_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ В меню", callback_data="menu_main")]
    ])

_BACK = _menu_back_kb

# ─── Charts ────────────────────────────────────────────────

async def _send_workout_weight_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
            return
        weight_history = await crud.get_weight_history(session, user.id, days=30)
        workouts = await crud.get_workout_logs_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=30), datetime.now(UTC)
        )

    if not weight_history and not workouts:
        await q.edit_message_text("Недостаточно данных для графика.", reply_markup=_BACK())
        return

    from bot.calculators.charts import workout_weight_chart
    buf = workout_weight_chart(
        [w.date for w in weight_history],
        [w.weight_kg for w in weight_history],
        [w.date for w in workouts],
        [w.total_volume or 0 for w in workouts],
    )
    await q.message.delete()
    await context.bot.send_photo(
        chat_id=q.message.chat_id, photo=buf,
        caption="Вес и объём тренировок за 30 дней",
        reply_markup=_BACK(),
    )


async def _send_calories_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from bot.db.base import async_session
    from bot.db import crud
    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
            return
        meals = await crud.get_meals_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=14), datetime.now(UTC)
        )

    if not meals:
        await q.edit_message_text("Недостаточно данных для графика.", reply_markup=_BACK())
        return

    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    target_cal = targets["calories"]

    daily_totals = {}
    for m in meals:
        day = m.date.date()
        daily_totals[day] = daily_totals.get(day, 0) + m.calories

    days_sorted = sorted(daily_totals.keys())
    deficits = [target_cal - daily_totals[d] for d in days_sorted]

    from bot.calculators.charts import deficit_chart
    buf = deficit_chart(days_sorted, deficits)
    await q.message.delete()
    await context.bot.send_photo(
        chat_id=q.message.chat_id, photo=buf,
        caption="Баланс калорий за 14 дней",
        reply_markup=_BACK(),
    )


async def _send_sleep_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user_id = q.from_user.id
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await q.edit_message_text("Сначала /onboarding", reply_markup=_BACK())
            return
        sleep_logs = await crud.get_sleep_between(
            session, user.id,
            datetime.now(UTC) - timedelta(days=14), datetime.now(UTC)
        )

    if not sleep_logs:
        await q.edit_message_text("Недостаточно данных для графика.", reply_markup=_BACK())
        return

    from bot.calculators.charts import sleep_chart
    buf = sleep_chart(
        [s.date for s in sleep_logs],
        [s.duration_hours for s in sleep_logs],
    )
    await q.message.delete()
    await context.bot.send_photo(
        chat_id=q.message.chat_id, photo=buf,
        caption="Динамика сна за 14 дней",
        reply_markup=_BACK(),
    )


# ─── Help ──────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Все команды бота:\n\n"
        "/menu — главное меню\n"
        "/onboarding — настройка профиля\n"
        "/me — мой профиль\n"
        "/today — сводка за сегодня\n"
        "/log — записать еду\n"
        "/weight [кг] — обновить вес\n"
        "/sleep [отбой] [подъём] — записать сон\n"
        "/steps [n] — записать шаги\n"
        "/progress [дней] — графики\n"
        "/week — неделя\n"
        "/suggest — что тренировать\n"
        "/workout — тренировка\n"
        "/new_workout — создать программу тренировок\n"
        "/settings — настройки\n"
        "/export — экспорт CSV\n\n"
        "Просто пиши текстом — понимаю без команд."
    )


# ─── Photo handler ─────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logger.info(f"[PHOTO] user={user_id} — received photo")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        photo_bytes = await file.download_as_bytearray()
        logger.info(f"[PHOTO] user={user_id} — downloaded {len(photo_bytes)} bytes, size={photo.width}x{photo.height}")
    except Exception as e:
        logger.error(f"[PHOTO] user={user_id} — download failed: {type(e).__name__}: {e}")
        await update.message.reply_text("Ошибка загрузки фото.")
        return

    await update.message.reply_text("Распознаю...")

    try:
        from bot.ai.vision import analyze_photo
    except Exception as e:
        logger.error(f"[PHOTO] user={user_id} — import vision failed: {type(e).__name__}: {e}")
        await update.message.reply_text("Ошибка импорта модуля распознавания.")
        return

    try:
        result = await analyze_photo(bytes(photo_bytes))
        logger.info(f"[PHOTO] user={user_id} — analyze_photo returned: {result}")
    except Exception as e:
        logger.error(f"[PHOTO] user={user_id} — analyze_photo crashed: {type(e).__name__}: {e}", exc_info=True)
        await update.message.reply_text("Ошибка распознавания. Попробуй текстом: /log 200г гречки")
        return

    if not result:
        await update.message.reply_text(
            "Не распознал. Попробуй текстом: /log 200г гречки"
        )
        return

    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        await crud.add_meal_log(
            session, user_id=user.id,
            food_name=result["food_name"],
            weight_g=result["weight_g"],
            calories=result["calories"],
            protein=result["protein"],
            fat=result["fat"],
            carbs=result["carbs"],
            source="ai",
        )

    from bot.cache.redis_client import invalidate_context
    await invalidate_context(user_id)

    await update_today_state(
        user_id,
        calories_in=result["calories"],
        protein=result["protein"],
        fat=result["fat"],
        carbs=result["carbs"],
    )

    today_state = await get_today_state(user_id)

    from bot.handlers.food import format_progress_bar, get_targets_for_user

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
    targets = await get_targets_for_user(user) if user else {"calories": 2000, "protein_g": 150}

    cal_pct = (today_state["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (today_state["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0

    await update.message.reply_text(
        f"{result['food_name']} (~{result['weight_g']:.0f}г)\n\n"
        f"{result['calories']:.0f} ккал | {result['protein']:.1f}г бел | "
        f"{result['fat']:.1f}г жир | {result['carbs']:.1f}г угл\n\n"
        f"За день: {today_state['calories_in']:.0f} / {targets['calories']} "
        f"{format_progress_bar(today_state['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
        f"Белок: {today_state['protein']:.0f} / {targets['protein_g']}г "
        f"{format_progress_bar(today_state['protein'], targets['protein_g'])} {prot_pct:.0f}%"
    )


# ─── Text handler ──────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    chat = update.message.chat

    if await admin_text_input(update, context):
        return

    if context.user_data.get("pending_rpe") is not None:
        await handle_rpe_input(update, context)
        return

    if is_manual_mode(context) or context.user_data.get("new_workout_ai_history") is not None:
        await new_workout_ai_continue(update, context)
        return

    if context.user_data.get("workout_session") is not None:
        await workout_ai_continue(update, context)
        return

    intent = _match_hardcoded_intent(text)
    if intent == "today":
        await today(update, context)
        return
    if intent == "cancel":
        await cancel_command(update, context)
        return
    if intent == "weight_query":
        async with async_session() as session:
            user = await crud.get_user(session, user_id)
        if user:
            await chat.send_message(f"Твой текущий вес: {user.weight_kg} кг")
            return
    if intent == "steps_query":
        state = await get_today_state(user_id)
        await chat.send_message(f"Шаги сегодня: {state['steps']}")
        return

    from bot.ai.analyzer import analyze_message

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            await chat.send_message("Сначала /onboarding")
            return

    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    state = await get_today_state(user_id)

    if is_new_workout_message(text):
        await new_workout_ai_start(update, context)
        return

    if is_workout_message(text):
        await workout_ai_start(update, context)
        return

    analysis = analyze_message(text, state, targets)

    if analysis["response"]:
        await chat.send_message(analysis["response"])
        return

    if analysis["action"]:
        action = analysis["action"]
        if analysis["intent"] == "food":
            from bot.handlers.food import search_food
            food_data = await search_food(action["food_name"])
            if food_data:
                factor = action["weight_g"] / 100
                async with async_session() as session:
                    user_db = await crud.get_user(session, user_id)
                    if user_db:
                        await crud.add_meal_log(
                            session, user_id=user_db.id,
                            food_name=action["food_name"], weight_g=action["weight_g"],
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
                new_state = await get_today_state(user_id)
                await chat.send_message(
                    f"Записано: {action['food_name']} {action['weight_g']}г\n"
                    f"{food_data['calories'] * factor:.0f}ккал | "
                    f"{food_data['protein'] * factor:.1f}г бел\n"
                    f"Всего: {new_state['calories_in']:.0f}ккал"
                )
                return

        elif analysis["intent"] == "steps":
            kcal = action["steps"] * 0.04 * (user.weight_kg / 70)
            await update_today_state(user_id, steps=action["steps"], calories_out=kcal)
            await invalidate_context(user_id)
            await chat.send_message(f"Записано: {action['steps']} шагов (+{kcal:.0f}ккал)")
            return

        elif analysis["intent"] == "weight":
            async with async_session() as session:
                user_db = await crud.get_user(session, user_id)
                if user_db:
                    await crud.add_weight(session, user_db.id, action["weight_kg"])
                    await crud.update_user(session, user_db.tg_id, weight_kg=action["weight_kg"])
                    await invalidate_context(user_id)
            diff = user.target_weight_kg - action["weight_kg"]
            await chat.send_message(
                f"Вес: {action['weight_kg']}кг\n"
                f"До цели: {diff:+.1f}кг"
            )
            return

    await _send_ai_reply(chat, user_id, text, context.bot)


async def quick_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "qck_food":
        await q.message.reply_text("Введи что съел, например: 200г гречки с курицей")
    elif data == "qck_workout":
        await q.message.reply_text(
            "🏋️ Опиши тренировку:\n"
            "Упражнения, подходы, вес, длительность.\n"
            "Пример: «Жим лёжа 80кг 4×8, присед 100кг 5×5, 45 мин»\n\n"
            "Когда закончишь — напиши «всё»."
        )
        context.user_data["workout_session"] = {"exercises": [], "duration_minutes": 45, "calories_burned": 0}
    elif data == "qck_steps":
        await q.message.reply_text("Сколько прошёл? Напиши число.")
    elif data == "qck_sleep":
        await q.message.reply_text("Формат: /sleep 23:00 07:00")
    elif data == "qck_weight":
        await q.message.reply_text("Сколько весишь? Напиши число.")
    elif data == "qck_week":
        from bot.handlers.commands import week
        await week(update, context)


_last_admin_alert: dict[int, float] = {}
ADMIN_ALERT_COOLDOWN = 300


async def _send_ai_reply(chat, user_id: int, text: str, bot) -> None:
    if is_rate_limited(user_id):
        await chat.send_message("Слишком много сообщений подряд, подожди немного.")
        return

    await chat.send_chat_action("typing")

    from bot.ai.trainer import _build_prompt
    from bot.ai.actions import handle_message_with_actions

    t_start = time.monotonic()
    built = await _build_prompt(user_id, text)
    if built is None:
        await chat.send_message("Сначала выполни /onboarding, чтобы я знал твои параметры.")
        return
    system_prompt, user_text = built

    result = await handle_message_with_actions(user_id, user_text, system_prompt, bot)
    elapsed = time.monotonic() - t_start

    if result is None:
        logger.warning(f"[AI] user={user_id} elapsed={elapsed:.2f}s FAILED")
        now = time.monotonic()
        last = _last_admin_alert.get(user_id, 0)
        if now - last > ADMIN_ALERT_COOLDOWN:
            _last_admin_alert[user_id] = now
            try:
                await bot.send_message(ADMIN_ID, f"Оба ИИ-провайдера упали для user {user_id}")
            except Exception:
                pass
        await chat.send_message("ИИ временно недоступен, попробуй позже.")
        return

    logger.info(f"[AI] user={user_id} elapsed={elapsed:.2f}s len={len(result)}")
    await chat.send_message(result)


# ─── Health check ──────────────────────────────────────────

async def health(request):
    from aiohttp import web
    return web.json_response({"status": "ok"})


async def start_health_server():
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    for port in range(8080, 8090):
        try:
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logger.info(f"Health check server started on :{port}")
            return
        except OSError:
            continue
    logger.warning("Could not start health server - all ports busy")


async def post_init(app):
    await init_db()
    scheduler.start()
    await start_health_server()

    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("menu", "📌 Открыть главное меню"),
        BotCommand("today", "📊 Сводка за сегодня"),
        BotCommand("log", "🍽 Записать еду"),
        BotCommand("steps", "👟 Записать шаги"),
        BotCommand("workout", "🏋️ Записать тренировку"),
        BotCommand("weight", "⚖️ Обновить вес"),
        BotCommand("sleep", "😴 Записать сон"),
        BotCommand("week", "📅 Недельная сводка"),
        BotCommand("progress", "📈 Графики прогресса"),
        BotCommand("new_workout", "➕ Создать программу тренировок"),
        BotCommand("suggest", "💡 Что тренировать"),
        BotCommand("me", "🧍 Мой профиль"),
        BotCommand("settings", "⚙️ Настройки"),
        BotCommand("help", "❓ Помощь"),
    ])

    from bot.scheduler.reminders import reset_all_today_states, restore_all_schedulers
    async def midnight_reset():
        from bot.db.base import async_session
        from bot.db import crud
        from datetime import datetime, UTC, timedelta, date
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = today_start - timedelta(days=1)
        async with async_session() as session:
            users = await crud.get_all_users(session)
        for user in users:
            try:
                state = await get_today_state(user.tg_id)
                if any(v for v in state.values()):
                    async with async_session() as session:
                        await crud.save_daily_summary(session, user.id, today_start, state)
                else:
                    state = await get_today_state(user.tg_id, day=yesterday.date())
                    if any(v for v in state.values()):
                        async with async_session() as session:
                            await crud.save_daily_summary(session, user.id, today_start, state)
            except Exception as e:
                logger.warning(f"Failed to save daily summary for {user.tg_id}: {e}")
        await reset_all_today_states(app.bot)
    scheduler.add_job(midnight_reset, CronTrigger(hour=0, minute=0), id="midnight_reset", replace_existing=True)

    await restore_all_schedulers(app.bot)

    logger.info("Database initialized, scheduler started, health check running")


async def post_shutdown(app):
    from bot.cache.redis_client import redis
    if redis:
        await redis.close()
    logger.info("Graceful shutdown complete")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set in .env")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_entry))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.add_handler(get_onboarding_handler())
    app.add_handler(get_me_handler())

    app.add_handler(get_food_handler())
    app.add_handler(get_cancel_handler())
    app.add_handler(get_export_handler())

    app.add_handler(CommandHandler("new_workout", new_workout_ai_start))
    app.add_handler(CommandHandler("workout", workout_ai_start))

    app.add_handler(get_today_handler())
    app.add_handler(get_weight_handler())
    app.add_handler(get_sleep_handler())
    app.add_handler(get_steps_handler())
    app.add_handler(get_week_handler())
    app.add_handler(get_settings_handler())
    app.add_handler(get_progress_handler())
    app.add_handler(get_suggest_handler())
    app.add_handler(get_debug_handler())

    app.add_handler(CallbackQueryHandler(onb_restart_callback, pattern="^onb_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_|^chart_"))
    app.add_handler(CallbackQueryHandler(quick_button_callback, pattern="^qck_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=(signal.SIGTERM, signal.SIGINT),
    )


if __name__ == "__main__":
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    main()
