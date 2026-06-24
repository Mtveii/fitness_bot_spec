import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from bot.db.base import async_session
from bot.db import crud
from bot.calculators.workout_metrics import workout_calories, total_volume
from bot.cache.redis_client import update_today_state

EXERCISE_TYPES = ["compound", "isolation", "cardio"]
EXERCISE_TYPE_LABELS = {"compound": "Базовое", "isolation": "Изолирующее", "cardio": "Кардио"}

NEW_WORKOUT_NAME, NEW_WORKOUT_DAY, NEW_WORKOUT_EXERCISE, NEW_WORKOUT_TYPE, \
    NEW_WORKOUT_MUSCLES, NEW_WORKOUT_SETS, NEW_WORKOUT_WEIGHT, NEW_WORKOUT_REST = range(8)

LOG_WORKOUT_SELECT, LOG_WORKOUT_SET, LOG_WORKOUT_RPE, LOG_WORKOUT_FEEL, LOG_WORKOUT_DONE = range(8, 13)


# ─── /new_workout — создание программы ──────────────────────

async def new_workout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Название программы (например «Грудь/Трицепс»):")
    return NEW_WORKOUT_NAME


async def new_workout_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["program_name"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(d, callback_data=d) for d in row]
        for row in [["Пн", "Вт", "Ср"], ["Чт", "Пт", "Сб"], ["Вс"]]
    ])
    await update.message.reply_text("День недели:", reply_markup=keyboard)
    return NEW_WORKOUT_DAY


async def new_workout_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["program_day"] = query.data
    context.user_data["exercises"] = []
    await query.edit_message_text("Название упражнения (или «готово»):")
    return NEW_WORKOUT_EXERCISE


async def new_workout_exercise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() in ("готово", "done", "стоп"):
        if not context.user_data["exercises"]:
            await update.message.reply_text("Добавь хотя бы одно упражнение.")
            return NEW_WORKOUT_EXERCISE
        return await save_program(update, context)

    context.user_data["current_exercise"] = {"name": text}
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Базовое", callback_data="compound"),
         InlineKeyboardButton("Изолирующее", callback_data="isolation"),
         InlineKeyboardButton("Кардио", callback_data="cardio")]
    ])
    await update.message.reply_text("Тип упражнения:", reply_markup=keyboard)
    return NEW_WORKOUT_TYPE


async def new_workout_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["current_exercise"]["type"] = query.data
    await query.edit_message_text("Целевые мышцы (через запятую, например «грудь, трицепс»):")
    return NEW_WORKOUT_MUSCLES


async def new_workout_muscles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    context.user_data["current_exercise"]["muscles"] = [m.strip() for m in text.split(",")]
    await update.message.reply_text("Подходы × повторения (например «4×8-10»):")
    return NEW_WORKOUT_SETS


async def new_workout_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    match = re.match(r"(\d+)\s*[x×*]\s*(\d+(?:-\d+)?)", text)
    if not match:
        await update.message.reply_text("Формат: 4×8-10 или 3x12")
        return NEW_WORKOUT_SETS
    context.user_data["current_exercise"]["sets"] = int(match.group(1))
    context.user_data["current_exercise"]["reps"] = match.group(2)
    await update.message.reply_text("Плановый вес (кг) или 0:")
    return NEW_WORKOUT_WEIGHT


async def new_workout_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        weight = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("Введи число:")
        return NEW_WORKOUT_WEIGHT

    context.user_data["current_exercise"]["weight"] = weight
    await update.message.reply_text("Отдых между подходами (сек, например 90):")
    return NEW_WORKOUT_REST


async def new_workout_rest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        rest = int(update.message.text.strip())
    except ValueError:
        rest = 90

    ex = context.user_data["current_exercise"]
    ex["rest"] = rest
    context.user_data["exercises"].append(ex)

    await update.message.reply_text(
        f"✅ {ex['name']} ({EXERCISE_TYPE_LABELS.get(ex['type'], ex['type'])})\n"
        f"   {ex['sets']}×{ex['reps']} @ {ex['weight']}кг | Отдых: {ex['rest']}сек\n"
        f"   Мышцы: {', '.join(ex['muscles'])}\n\n"
        "Ещё упражнение? Название или «готово»:"
    )
    return NEW_WORKOUT_EXERCISE


async def save_program(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return ConversationHandler.END

        from bot.db.models import WorkoutProgram, Exercise
        program = WorkoutProgram(
            user_id=user.id,
            name=context.user_data["program_name"],
            day_of_week=context.user_data["program_day"],
        )
        session.add(program)
        await session.flush()

        for ex_data in context.user_data["exercises"]:
            exercise = Exercise(
                program_id=program.id,
                name=ex_data["name"],
                type=ex_data.get("type", "compound"),
                muscle_groups=ex_data.get("muscles", []),
                planned_sets=ex_data["sets"],
                planned_reps=ex_data["reps"],
                planned_weight_kg=ex_data["weight"],
                rest_seconds=ex_data.get("rest", 90),
            )
            session.add(exercise)

        await session.commit()

    count = len(context.user_data["exercises"])
    await update.message.reply_text(
        f"💪 Программа «{context.user_data['program_name']}» сохранена!\n"
        f"Упражнений: {count}\n"
        f"День: {context.user_data['program_day']}"
    )
    return ConversationHandler.END


# ─── /workout — лог тренировки ──────────────────────────────

async def log_workout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return ConversationHandler.END
        programs = await crud.get_user_programs(session, user.id)

    if not programs:
        await update.message.reply_text("Нет программ. Создай: /new_workout")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(p.name, callback_data=str(p.id))]
        for p in programs
    ])
    await update.message.reply_text("Выбери программу:", reply_markup=keyboard)
    return LOG_WORKOUT_SELECT


async def log_workout_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    program_id = int(query.data)

    async with async_session() as session:
        from sqlalchemy import select
        from bot.db.models import WorkoutProgram, Exercise
        result = await session.execute(
            select(WorkoutProgram).where(WorkoutProgram.id == program_id)
        )
        program = result.scalar_one_or_none()
        if not program:
            await query.edit_message_text("Программа не найдена.")
            return ConversationHandler.END

        ex_result = await session.execute(
            select(Exercise).where(Exercise.program_id == program_id)
        )
        exercises = list(ex_result.scalars().all())

    if not exercises:
        await query.edit_message_text("В программе нет упражнений.")
        return ConversationHandler.END

    context.user_data["workout_program"] = program.name
    context.user_data["workout_exercises"] = [
        {"name": e.name, "sets": e.planned_sets, "reps": e.planned_reps,
         "weight": e.planned_weight_kg, "muscle_groups": e.muscle_groups or []}
        for e in exercises
    ]
    context.user_data["workout_results"] = []
    context.user_data["current_ex_idx"] = 0
    context.user_data["current_set_num"] = 1

    ex = exercises[0]
    await query.edit_message_text(
        f"🏋️ {ex.name}\n"
        f"Мышцы: {', '.join(ex.muscle_groups) if ex.muscle_groups else '—'}\n"
        f"План: {ex.planned_sets}×{ex.planned_reps} @ {ex.planned_weight_kg}кг\n\n"
        f"Подход 1/{ex.planned_sets} — вес×повторения (например «80×8»):"
    )
    return LOG_WORKOUT_SET


async def log_workout_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    match = re.match(r"(\d+(?:[.,]?\d+)?)\s*[x×*]\s*(\d+)", text)
    if not match:
        await update.message.reply_text("Формат: 80×8 или 60*12")
        return LOG_WORKOUT_SET

    weight = float(match.group(1).replace(",", "."))
    reps = int(match.group(2))

    context.user_data["workout_results"].append({"weight": weight, "reps": reps})

    await update.message.reply_text("RPE (1-10, или «-» если не знаешь):")
    return LOG_WORKOUT_RPE


async def log_workout_rpe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text in ("-", "—", "0", ""):
        rpe = 5.0
    else:
        try:
            rpe = float(text)
            if not 1 <= rpe <= 10:
                rpe = 5.0
        except ValueError:
            rpe = 5.0

    context.user_data["workout_results"][-1]["rpe"] = rpe

    ex_idx = context.user_data["current_ex_idx"]
    set_num = context.user_data["current_set_num"]
    exercises = context.user_data["workout_exercises"]
    current_ex = exercises[ex_idx]

    if set_num < current_ex["sets"]:
        context.user_data["current_set_num"] = set_num + 1
        await update.message.reply_text(
            f"✅ Подход {set_num} записан!\n\n"
            f"Подход {set_num + 1}/{current_ex['sets']} — вес×повторения:"
        )
        return LOG_WORKOUT_SET
    else:
        context.user_data["current_set_num"] = 1
        context.user_data["current_ex_idx"] = ex_idx + 1

        if ex_idx + 1 < len(exercises):
            next_ex = exercises[ex_idx + 1]
            await update.message.reply_text(
                f"✅ Упражнение завершено!\n\n"
                f"Следующее: {next_ex['name']}\n"
                f"План: {next_ex['sets']}×{next_ex['reps']} @ {next_ex['weight']}кг\n\n"
                f"Подход 1/{next_ex['sets']} — вес×повторения:"
            )
            return LOG_WORKOUT_SET
        else:
            await update.message.reply_text(
                "Все упражнения записаны!\n"
                "Оценка самочувствия после тренировки (1-10):"
            )
            return LOG_WORKOUT_FEEL


async def log_workout_feel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        feel = int(update.message.text.strip())
        if not 1 <= feel <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Число от 1 до 10:")
        return LOG_WORKOUT_FEEL

    context.user_data["workout_feel"] = feel
    await update.message.reply_text("Длительность тренировки в минутах:")
    return LOG_WORKOUT_DONE


async def log_workout_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        duration = int(update.message.text.strip())
    except ValueError:
        duration = 60

    sets = context.user_data["workout_results"]
    vol = total_volume(sets)
    kcal = workout_calories("strength", 70, duration / 60)

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if user:
            kcal = workout_calories("strength", user.weight_kg, duration / 60)

        log = await crud.add_workout_log(
            session, user_id=user.id,
            workout_name=context.user_data["workout_program"],
            duration_minutes=duration,
            total_volume=vol,
            subjective_feel=context.user_data["workout_feel"],
            calories_burned=kcal,
        )

        exercises = context.user_data["workout_exercises"]
        set_idx = 0
        for ex in exercises:
            for set_num in range(1, ex["sets"] + 1):
                if set_idx >= len(sets):
                    break
                s = sets[set_idx]
                await crud.add_exercise_set(
                    session, log_id=log.id,
                    exercise_name=ex["name"],
                    set_number=set_num,
                    weight_kg=s["weight"],
                    reps=s["reps"],
                    rpe=s.get("rpe", 5),
                )
                set_idx += 1

    await update_today_state(
        update.effective_user.id,
        calories_out=kcal,
        workout_kcal=kcal,
    )

    # Формат ответа по спеке
    lines = ["🏋️ Тренировка завершена!\n"]
    set_num = 1
    for s in sets:
        rpe_str = f" (RPE {s['rpe']:.0f})" if s.get("rpe") and s["rpe"] != 5 else ""
        lines.append(f"{set_num}️⃣ {s['weight']}кг × {s['reps']}{rpe_str}")
        set_num += 1

    lines.append(f"\n✅ Объём: {vol:.0f}кг | 🔥 {kcal:.0f} ккал")
    lines.append(f"⏱ {duration} мин | 💯 {context.user_data['workout_feel']}/10")

    await update.message.reply_text("\n".join(lines))

    try:
        from bot.ai.analyzer import check_performance_drop, check_progression
        exercises = context.user_data.get("workout_exercises", [])
        for ex in exercises:
            drop = await check_performance_drop(update.effective_user.id, ex["name"])
            if drop:
                await update.message.reply_text(f"⚠️ Спад на «{ex['name']}»:\n{drop}")

            prog = await check_progression(update.effective_user.id, ex["name"])
            if prog:
                await update.message.reply_text(prog)
    except Exception:
        pass

    return ConversationHandler.END


def get_new_workout_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("new_workout", new_workout_start)],
        states={
            NEW_WORKOUT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_name)],
            NEW_WORKOUT_DAY: [CallbackQueryHandler(new_workout_day)],
            NEW_WORKOUT_EXERCISE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_exercise)],
            NEW_WORKOUT_TYPE: [CallbackQueryHandler(new_workout_type, pattern="^(compound|isolation|cardio)$")],
            NEW_WORKOUT_MUSCLES: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_muscles)],
            NEW_WORKOUT_SETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_sets)],
            NEW_WORKOUT_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_weight)],
            NEW_WORKOUT_REST: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_workout_rest)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )


def get_log_workout_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("workout", log_workout_start)],
        states={
            LOG_WORKOUT_SELECT: [CallbackQueryHandler(log_workout_select)],
            LOG_WORKOUT_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_workout_set)],
            LOG_WORKOUT_RPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_workout_rpe)],
            LOG_WORKOUT_FEEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_workout_feel)],
            LOG_WORKOUT_DONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, log_workout_done)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
