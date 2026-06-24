import logging
from bot.db.base import async_session
from bot.db import crud

logger = logging.getLogger(__name__)


async def check_performance_drop(user_id: int, exercise_name: str) -> str | None:
    """Проверяет спад силовых. Возвращает анализ если есть спад."""
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return None

        last_session = await crud.get_last_exercise_session(session, user_id, exercise_name)
        if not last_session or not last_session.exercise_sets:
            return None

        prev_best_weight = max(s.weight_kg for s in last_session.exercise_sets)
        prev_total_reps = sum(s.reps for s in last_session.exercise_sets)

        second_last = None
        all_logs = await crud.get_workout_logs_between(
            session, user_id,
            last_session.date - __import__("datetime").timedelta(days=30),
            last_session.date
        )
        for log in all_logs:
            if log.id != last_session.id and log.exercise_sets:
                ex_sets = [s for s in log.exercise_sets if s.exercise_name == exercise_name]
                if ex_sets:
                    second_last = ex_sets
                    break

        if not second_last:
            return None

        old_best = max(s.weight_kg for s in second_last)
        old_reps = sum(s.reps for s in second_last)

        weight_drop = prev_best_weight < old_best * 0.95
        reps_drop = prev_total_reps < old_reps * 0.9

        if not weight_drop and not reps_drop:
            return None

        context_parts = []
        context_parts.append(f"Упражнение: {exercise_name}")
        context_parts.append(f"Прошлая сессия: {old_best}кг, {old_reps} повторов")
        context_parts.append(f"Текущая: {prev_best_weight}кг, {prev_total_reps} повторов")

        last_sleep = await crud.get_last_sleep(session, user_id)
        if last_sleep:
            context_parts.append(f"Последний сон: {last_sleep.duration_hours:.1f}ч")

        state = await (await __import__("bot.cache.redis_client", fromlist=["get_today_state"])).get_today_state(user_id)
        context_parts.append(f"Калории сегодня: {state.get('calories_in', 0):.0f}")
        context_parts.append(f"Белок сегодня: {state.get('protein', 0):.0f}г")

        return "\n".join(context_parts)

    return None


async def check_progression(user_id: int, exercise_name: str) -> str | None:
    """Проверяет можно ли增加 вес. Возвращает suggestion или None."""
    async with async_session() as session:
        from sqlalchemy import select
        from bot.db.models import WorkoutLog, ExerciseSet

        result = await session.execute(
            select(WorkoutLog).where(WorkoutLog.user_id == user_id)
            .order_by(WorkoutLog.date.desc()).limit(10)
        )
        logs = list(result.scalars().all())

        recent_sets = []
        for log in logs:
            sets_result = await session.execute(
                select(ExerciseSet).where(
                    ExerciseSet.log_id == log.id,
                    ExerciseSet.exercise_name == exercise_name,
                )
            )
            sets = list(sets_result.scalars().all())
            if sets:
                max_weight = max(s.weight_kg for s in sets)
                max_reps = max(s.reps for s in sets)
                recent_sets.append({"weight": max_weight, "reps": max_reps})

        if len(recent_sets) < 3:
            return None

        last_3 = recent_sets[:3]
        same_weight = all(s["weight"] == last_3[0]["weight"] for s in last_3)
        all_top_reps = all(s["reps"] >= 10 for s in last_3)

        if same_weight and all_top_reps:
            new_weight = last_3[0]["weight"] + 2.5
            return f"💪 Попробуй +2.5кг на {exercise_name}: {new_weight}кг в следующий раз!"

    return None
