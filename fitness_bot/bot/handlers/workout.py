import re
import logging
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.db.base import async_session
from bot.db import crud
from bot.db.models import WorkoutLog
from bot.calculators.workout_metrics import workout_calories, total_volume
from bot.cache.redis_client import update_today_state, invalidate_context
from bot.ai.workout_ai import ask_workout_ai, analyze_workout, ask_create_program

logger = logging.getLogger(__name__)

WORKOUT_KEYWORDS = [
    "трениров", "упражнен", "подход", "повтор", "жим", "тяга", "присед",
    "отжима", "подтяг", "штан", "гантел", "гриф", "кроссовер",
    "потренировал", "сжыгаю",
    "грудь", "спина", "ноги", "плечи", "руки", "бицепс", "трицепс",
    "турник", "брусь", "тренажер", "кроссфит",
]


def is_workout_message(text: str) -> bool:
    low = text.lower().strip()
    return any(kw in low for kw in WORKOUT_KEYWORDS)


def _calc_volume(exercises: list) -> float:
    vol = 0
    for ex in exercises:
        for s in ex.get("sets", []):
            vol += s.get("weight_kg", 0) * s.get("reps", 0)
    return vol


def _calc_calories(weight_kg: float, duration_minutes: int) -> float:
    return workout_calories("strength", weight_kg, duration_minutes / 60)


def _format_workout_log(exercises: list, duration_minutes: int, total_vol: float, calories: float) -> str:
    lines = ["🏋️ Тренировка:\n"]
    for i, ex in enumerate(exercises, 1):
        sets_str = ", ".join(f"{s['weight_kg']}кг×{s['reps']}" for s in ex["sets"])
        lines.append(f"{i}. {ex['name']}: {sets_str}")
    lines.append(f"\n⏱ {duration_minutes} мин | 📦 Объём: {total_vol:.0f}кг | 🔥 {calories:.0f}ккал")
    return "\n".join(lines)


async def _save_workout(user_id: int, data: dict) -> dict:
    workout_name = data.get("workout_name", "Тренировка")
    duration_minutes = data.get("duration_minutes", 45)
    exercises = data.get("exercises", [])
    calories_burned = data.get("calories_burned", 0)

    vol = _calc_volume(exercises)

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return {"error": "Сначала /onboarding"}

        if not calories_burned and duration_minutes > 0:
            calories_burned = _calc_calories(user.weight_kg, duration_minutes)

        log = await crud.add_workout_log(
            session, user_id=user.id,
            workout_name=workout_name,
            duration_minutes=duration_minutes,
            total_volume=vol,
            subjective_feel=5,
            calories_burned=calories_burned,
        )

        for ex in exercises:
            for set_num, s in enumerate(ex.get("sets", []), 1):
                await crud.add_exercise_set(
                    session, log_id=log.id,
                    exercise_name=ex["name"],
                    set_number=set_num,
                    weight_kg=s.get("weight_kg", 0),
                    reps=s.get("reps", 0),
                )

    await update_today_state(user_id, calories_out=calories_burned, workout_kcal=calories_burned)
    await invalidate_context(user_id)

    return {
        "workout_name": workout_name,
        "duration_minutes": duration_minutes,
        "exercises": exercises,
        "total_volume": vol,
        "calories_burned": calories_burned,
    }


NEW_WORKOUT_KEYWORDS = [
    "создай программ", "новая программ", "добавь программ",
    "создать программу", "новую программу",
    "хочу программу", "нужна программ",
]


def is_new_workout_message(text: str) -> bool:
    low = text.lower().strip()
    return any(kw in low for kw in NEW_WORKOUT_KEYWORDS)


async def _save_program_ai(user_id: int, data: dict) -> dict:
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return {"error": "Сначала /onboarding"}

        from bot.db.models import WorkoutProgram, Exercise
        program = WorkoutProgram(
            user_id=user.id,
            name=data.get("name", "Моя программа"),
            day_of_week=data.get("days", ["Пн"]),
        )
        session.add(program)
        await session.flush()

        for ex_data in data.get("exercises", []):
            exercise = Exercise(
                program_id=program.id,
                name=ex_data.get("name", "Упражнение"),
                type=ex_data.get("type", "compound"),
                muscle_groups=ex_data.get("muscle_groups", []),
                planned_sets=ex_data.get("planned_sets", 3),
                planned_reps=ex_data.get("planned_reps", "8-12"),
                planned_weight_kg=ex_data.get("planned_weight_kg", 0),
                rest_seconds=ex_data.get("rest_seconds", 90),
            )
            session.add(exercise)

        await session.commit()

    days_str = ", ".join(data.get("days", ["Пн"]))
    ex_count = len(data.get("exercises", []))
    return {
        "name": data.get("name", "Моя программа"),
        "days": days_str,
        "exercises_count": ex_count,
        "program_id": program.id,
    }


async def new_workout_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

    text = update.message.text

    if not context.args:
        await update.message.reply_text(
            "🏋️ **Создание программы тренировок**\n\n"
            "Опиши, какую программу хочешь создать. Например:\n"
            "• «Хочу программу на грудь и трицепс, пн/ср/пт»\n"
            "• «Нужна программа для ног, 2 раза в неделю»\n"
            "• «Сплит: грудь+спина, плечи+руки, ноги»\n"
            "• «Full body 3 раза в неделю»\n\n"
            "Я проанализирую и создам программу. "
            "Можешь просто описать словами или сразу указать упражнения."
        )
        context.user_data["new_workout_ai_history"] = ""
        return

    user_text = text[len("/new_workout"):].strip()
    if not user_text:
        user_text = " ".join(context.args)

    context.user_data["new_workout_ai_history"] = ""
    await _process_new_workout_ai(update, context, user_text)


async def new_workout_ai_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text

    if is_manual_mode(context):
        if text.startswith("/cancel"):
            context.user_data.pop("manual_program", None)
            context.user_data.pop("manual_program_step", None)
            await update.message.reply_text("❌ Создание программы отменено.")
            return
        if text.startswith("/"):
            await update.message.reply_text("Используй /cancel чтобы выйти, или отправь «готово» когда закончишь.")
            return
        await _continue_manual_program(update, context)
        return

    if text.startswith("/"):
        await update.message.reply_text("Используй /cancel чтобы выйти из создания программы, или просто опиши её.")
        return
    await _process_new_workout_ai(update, context, text)


async def _process_new_workout_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    history = context.user_data.get("new_workout_ai_history", "")

    user_input = f"Пользователь: {text}"
    ai_input = f"{history}\n{user_input}" if history else user_input

    try:
        result = await ask_create_program(ai_input, user_id=update.effective_user.id)
    except Exception as e:
        logger.exception(f"AI create program failed for user {update.effective_user.id}: {e}")
        await _start_manual_program(update, context)
        return

    if result.get("action") == "save":
        saved = await _save_program_ai(update.effective_user.id, result)
        if "error" in saved:
            await update.message.reply_text(saved["error"])
            context.user_data.pop("new_workout_ai_history", None)
            return

        ex_list = result.get("exercises", [])
        lines = [f"💪 Программа «{saved['name']}» сохранена!\n"]
        lines.append(f"📅 Дни: {saved['days']}")
        lines.append(f"📋 Упражнений: {saved['exercises_count']}\n")
        for i, ex in enumerate(ex_list, 1):
            mg = ", ".join(ex.get("muscle_groups", [])) or "—"
            lines.append(f"{i}. {ex['name']} — {ex.get('planned_sets', 3)}×{ex.get('planned_reps', '8-12')}, {ex.get('planned_weight_kg', 0)}кг")
            lines.append(f"   Мышцы: {mg}")

        await update.message.reply_text("\n".join(lines))
        context.user_data.pop("new_workout_ai_history", None)

    elif result.get("action") == "ask":
        question = result.get("question", "Опиши программу подробнее.")
        context.user_data["new_workout_ai_history"] = ai_input + f"\nИИ: {question}"
        await update.message.reply_text(question)

    else:
        await update.message.reply_text(
            "Не понял. Опиши программу подробнее:\n"
            "• название программы\n"
            "• дни недели (пн/ср/пт)\n"
            "• упражнения и мышцы\n\n"
            "Или используй /cancel для выхода."
        )


# ─── Manual program creation (fallback when AI is down) ─────

async def _start_manual_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to manual step-by-step program creation when AI is unavailable."""
    context.user_data["manual_program_step"] = "name"
    context.user_data["manual_program"] = {
        "name": "",
        "days": [],
        "exercises": [],
    }
    context.user_data.pop("new_workout_ai_history", None)

    await update.message.reply_text(
        "🤖 ИИ временно недоступен. Создам программу вручную.\n\n"
        "Шаг 1 из 3: Введи название программы\n"
        "Например: «Грудь+трицепс» или «Нога+плечи»\n\n"
        "Или отправь /cancel для выхода."
    )


async def _continue_manual_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the next step in manual program creation."""
    text = update.message.text.strip()
    step = context.user_data.get("manual_program_step")
    program = context.user_data.get("manual_program")

    if not step or not program:
        await update.message.reply_text("Ошибка. Начни заново: /new_workout")
        return

    if step == "name":
        if len(text) > 100:
            await update.message.reply_text("Слишком длинное название (макс 100 символов). Попробуй короче.")
            return
        program["name"] = text
        context.user_data["manual_program_step"] = "days"
        await update.message.reply_text(
            f"✅ Название: «{text}»\n\n"
            "Шаг 2 из 3: На какие дни недели?\n"
            "Введи дни через запятую, например:\n"
            "• пн, ср, пт\n"
            "• вт, чт\n"
            "• каждый день\n\n"
            "Или /cancel для выхода."
        )

    elif step == "days":
        day_map = {
            "пн": "Пн", "вт": "Вт", "ср": "Ср", "чт": "Чт",
            "пт": "Пт", "сб": "Сб", "вс": "Вс",
            "понедельник": "Пн", "вторник": "Вт", "среда": "Ср",
            "четверг": "Чт", "пятница": "Пт", "суббота": "Сб", "воскресенье": "Вс",
        }
        if text.lower() == "каждый день":
            program["days"] = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        else:
            parts = re.split(r"[,.\s]+", text)
            days = []
            for p in parts:
                p = p.strip()
                if p in day_map:
                    days.append(day_map[p])
            if not days:
                await update.message.reply_text("Не понял дни. Введи через запятую: пн, ср, пт")
                return
            program["days"] = days

        context.user_data["manual_program_step"] = "exercises"
        days_str = ", ".join(program["days"])
        await update.message.reply_text(
            f"✅ Дни: {days_str}\n\n"
            "Шаг 3 из 3: Введи упражнения (по одному в строке)\n\n"
            "Формат: Название, подходы×повторения, вес(кг)\n"
            "Примеры:\n"
            "• Жим штанги лежа, 4×8-10, 60кг\n"
            "• Подтягивания, 3×8-12\n"
            "• Приседания со штангой, 5×5, 80кг\n\n"
            "Когда закончишь — отправь /done\n"
            "Или /cancel для выхода."
        )

    elif step == "exercises":
        if text.lower() in ("done", "готово", "всё", "все", "хватит", "закончил"):
            await _save_manual_program(update, context)
            return

        match = re.match(
            r"(.+?),\s*(\d+)\s*[×xх]\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*[,;]?\s*(?:(\d+(?:[.,]\d+)?)\s*кг)?",
            text,
            re.IGNORECASE,
        )
        if match:
            name = match.group(1).strip().capitalize()
            sets = int(match.group(2))
            reps_min = int(match.group(3))
            reps_max = int(match.group(4)) if match.group(4) else reps_min
            weight = float(match.group(5).replace(",", ".")) if match.group(5) else 0
            reps_str = f"{reps_min}-{reps_max}" if reps_min != reps_max else str(reps_min)

            program["exercises"].append({
                "name": name,
                "type": "compound",
                "muscle_groups": [],
                "planned_sets": sets,
                "planned_reps": reps_str,
                "planned_weight_kg": weight,
                "rest_seconds": 90,
            })
            count = len(program["exercises"])
            await update.message.reply_text(
                f"✅ {name}: {sets}×{reps_str}, {weight}кг — добавлено ({count})\n\n"
                "Введи следующее или /done для завершения."
            )
        else:
            await update.message.reply_text(
                "❓ Не понял формат. Пример:\n"
                "Жим штанги лежа, 4×8-10, 60кг\n\n"
                "Или /done если всё."
            )


async def _save_manual_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    program = context.user_data.get("manual_program", {})
    exercises = program.get("exercises", [])

    if not exercises:
        await update.message.reply_text(
            "Нет ни одного упражнения. Введи хотя бы одно или отправь /cancel."
        )
        return

    saved = await _save_program_ai(update.effective_user.id, {
        "name": program.get("name", "Моя программа"),
        "days": program.get("days", ["Пн"]),
        "exercises": exercises,
    })

    context.user_data.pop("manual_program", None)
    context.user_data.pop("manual_program_step", None)
    context.user_data.pop("new_workout_ai_history", None)

    lines = [f"💪 Программа «{saved['name']}» сохранена!\n"]
    lines.append(f"📅 Дни: {saved['days']}")
    lines.append(f"📋 Упражнений: {saved['exercises_count']}\n")
    for i, ex in enumerate(exercises, 1):
        lines.append(
            f"{i}. {ex['name']} — {ex['planned_sets']}×{ex['planned_reps']}, {ex['planned_weight_kg']}кг"
        )

    await update.message.reply_text("\n".join(lines))


def is_manual_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return context.user_data.get("manual_program_step") is not None


# ─── AI Workout Logging ─────────────────────────────────────

async def _init_workout_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initialize or reset workout accumulation session."""
    context.user_data["workout_session"] = {
        "exercises": [],
        "duration_minutes": 45,
        "calories_burned": 0,
    }
    context.user_data.pop("workout_ai_history", None)


async def workout_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /workout command."""
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

    await _init_workout_session(context)

    text = update.message.text.strip()
    args_text = text[len("/workout"):].strip() if text.startswith("/workout") else text

    if args_text:
        await update.message.reply_text("🏋️ Разбираю тренировку...")
        await _process_workout_ai(update, context, args_text)
    else:
        await update.message.reply_text(
            "🏋️ Опиши тренировку.\n\n"
            "Можно всё сразу:\n"
            "«Жим лёжа 80кг 4×8, Присед 100кг 5×5, Тяга 60кг 4×10, 45 минут»\n\n"
            "Или по одному упражнению:\n"
            "«Жим лёжа 80кг 4×8» → я добавлю и спрошу «что ещё?»\n\n"
            "Когда закончишь — напиши «всё» или «готово».\n"
            "/cancel — отменить."
        )


async def workout_ai_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle follow-up messages during AI workout logging."""
    text = update.message.text.strip().lower()

    if text in ("всё", "готово", "done", "хватит", "закончил"):
        await _finalize_workout(update, context)
        return

    if text in ("отмена", "отменить", "cancel"):
        context.user_data.pop("workout_session", None)
        context.user_data.pop("workout_ai_history", None)
        await update.message.reply_text("❌ Запись тренировки отменена.")
        return

    await _process_workout_ai(update, context, update.message.text.strip())


async def _process_workout_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    session_data = context.user_data.get("workout_session")
    if not session_data:
        await _init_workout_session(context)
        session_data = context.user_data["workout_session"]

    try:
        result = await ask_workout_ai(text)
    except Exception as e:
        logger.exception(f"AI workout parse failed for user {update.effective_user.id}: {e}")
        await update.message.reply_text(
            "❌ Ошибка анализа. Попробуй ещё раз:\n"
            "Формат: «Жим лёжа 80кг 4×8, Присед 100кг 5×5»\n"
            "Или отправь «отмена» чтобы выйти."
        )
        return

    action = result.get("action")

    if action == "add":
        new_exercises = result.get("exercises", [])
        if new_exercises:
            for ex in new_exercises:
                session_data["exercises"].append(ex)

            if result.get("duration_minutes"):
                session_data["duration_minutes"] = result["duration_minutes"]
            if result.get("calories_burned"):
                session_data["calories_burned"] = result["calories_burned"]

            names = ", ".join(ex["name"] for ex in new_exercises)
            total = len(session_data["exercises"])
            await update.message.reply_text(
                f"✅ Добавлено: {names}\n"
                f"📋 Всего упражнений: {total}\n\n"
                "Что ещё? Напиши следующее упражнение или «всё» для завершения."
            )
        else:
            await update.message.reply_text(
                "❓ Не нашёл упражнения. Пример: «Жим лёжа 80кг 4×8»\n"
                "Или «всё» если закончил."
            )

    elif action == "ask":
        question = result.get("question", "Опиши упражнение подробнее: вес, подходы, повторения.")
        await update.message.reply_text(question)

    else:
        await update.message.reply_text(
            "❓ Не понял. Напиши упражнение в формате:\n"
            "«Жим лёжа 80кг 4×8»\n"
            "Или «всё» чтобы завершить."
        )


async def _finalize_workout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save accumulated workout and show analysis."""
    session_data = context.user_data.pop("workout_session", None)
    if not session_data or not session_data.get("exercises"):
        await update.message.reply_text(
            "Нет упражнений для сохранения. Начни заново: /workout"
        )
        return

    exercises = session_data["exercises"]
    duration = session_data.get("duration_minutes", 45)

    vol = 0
    for ex in exercises:
        for s in ex.get("sets", []):
            vol += s.get("weight_kg", 0) * s.get("reps", 0)

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        from bot.calculators.workout_metrics import workout_calories as calc_cal
        calories = session_data.get("calories_burned", 0) or calc_cal("strength", user.weight_kg, duration / 60)

        log = await crud.add_workout_log(
            session, user_id=user.id,
            workout_name="Тренировка",
            duration_minutes=duration,
            total_volume=vol,
            subjective_feel=5,
            calories_burned=calories,
        )

        for ex in exercises:
            for set_num, s in enumerate(ex.get("sets", []), 1):
                await crud.add_exercise_set(
                    session, log_id=log.id,
                    exercise_name=ex["name"],
                    set_number=set_num,
                    weight_kg=s.get("weight_kg", 0),
                    reps=s.get("reps", 0),
                )

    from bot.cache.redis_client import update_today_state, invalidate_context
    await update_today_state(update.effective_user.id, calories_out=calories, workout_kcal=calories)
    await invalidate_context(update.effective_user.id)

    log_text = _format_workout_log(exercises, duration, vol, calories)
    await update.message.reply_text(log_text)

    try:
        analysis = await analyze_workout("Тренировка", duration, exercises, calories)
        analysis_text = f"\n📊 Анализ:\n{analysis.get('assessment', '')}"
        if analysis.get("pros"):
            analysis_text += "\n✅ " + "\n✅ ".join(analysis["pros"])
        if analysis.get("cons"):
            analysis_text += "\n❌ " + "\n❌ ".join(analysis["cons"])
        if analysis.get("suggestions"):
            analysis_text += "\n💡 " + "\n💡 ".join(analysis["suggestions"])
        await update.message.reply_text(analysis_text)
    except Exception as e:
        logger.warning(f"Analysis failed: {e}")

    await _ask_rpe(update, context, {
        "exercises": exercises,
        "duration_minutes": duration,
        "total_volume": vol,
        "calories_burned": calories,
    })


async def _ask_rpe(update: Update, context: ContextTypes.DEFAULT_TYPE, workout_data: dict) -> None:
    """Ask user for subjective feel (RPE) after logging a workout."""
    context.user_data["pending_rpe"] = workout_data
    await update.message.reply_text(
        "Как оцениваешь тренировку по шкале 1-10?\n"
        "1 — очень легко, 10 — максимальный усилий.\n"
        "Просто напиши число или отправь «пропустить»."
    )


async def handle_rpe_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle RPE input after workout logging."""
    text = update.message.text.strip()
    workout_data = context.user_data.pop("pending_rpe", None)
    if not workout_data:
        return

    rpe = 5
    if text.lower() not in ("пропустить", "skip", "-"):
        try:
            rpe = max(1, min(10, int(text)))
        except (ValueError, TypeError):
            await update.message.reply_text("Введи число от 1 до 10 или отправь «пропустить».")
            context.user_data["pending_rpe"] = workout_data
            return

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if user:
            from sqlalchemy import select
            result = await session.execute(
                select(WorkoutLog).where(
                    WorkoutLog.user_id == user.id
                ).order_by(WorkoutLog.id.desc()).limit(1)
            )
            last_log = result.scalar_one_or_none()
            if last_log:
                await crud.update_workout_rpe(session, last_log.id, rpe)

    await update.message.reply_text(f"✅ Запомнил: {rpe}/10")
    context.user_data.pop("workout_session", None)
