from datetime import datetime, UTC, timedelta
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.db.models import (
    User, WorkoutProgram, Exercise, WorkoutLog, ExerciseSet,
    MealLog, FoodItem, SleepLog, SupplementLog, WeightHistory
)


# ─── User ────────────────────────────────────────────────────

async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def create_user(session: AsyncSession, tg_id: int, **kwargs) -> User:
    user = User(tg_id=tg_id, **kwargs)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def update_user(session: AsyncSession, tg_id: int, **kwargs) -> None:
    await session.execute(update(User).where(User.tg_id == tg_id).values(**kwargs))
    await session.commit()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


# ─── MealLog ─────────────────────────────────────────────────

async def add_meal_log(
    session: AsyncSession, user_id: int, food_name: str, weight_g: float,
    calories: float, protein: float, fat: float, carbs: float,
    source: str = "usda", log_time: datetime | None = None
) -> MealLog:
    log = MealLog(
        user_id=user_id, food_name=food_name, weight_g=weight_g,
        calories=calories, protein=protein, fat=fat, carbs=carbs, source=source,
        date=log_time if log_time else _utcnow()
    )
    session.add(log)
    await session.commit()
    return log


async def get_meals_today(session: AsyncSession, user_id: int) -> list[MealLog]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(MealLog).where(
            MealLog.user_id == user_id,
            MealLog.date >= today
        ).order_by(MealLog.date)
    )
    return list(result.scalars().all())


async def get_meals_between(session: AsyncSession, user_id: int, start: datetime, end: datetime) -> list[MealLog]:
    result = await session.execute(
        select(MealLog).where(
            MealLog.user_id == user_id,
            MealLog.date >= start,
            MealLog.date < end,
        ).order_by(MealLog.date)
    )
    return list(result.scalars().all())


async def get_workout_logs_between(session: AsyncSession, user_id: int, start: datetime, end: datetime) -> list[WorkoutLog]:
    result = await session.execute(
        select(WorkoutLog).where(
            WorkoutLog.user_id == user_id,
            WorkoutLog.date >= start,
            WorkoutLog.date < end,
        ).order_by(WorkoutLog.date)
    )
    return list(result.scalars().all())


async def get_sleep_between(session: AsyncSession, user_id: int, start: datetime, end: datetime) -> list[SleepLog]:
    result = await session.execute(
        select(SleepLog).where(
            SleepLog.user_id == user_id,
            SleepLog.date >= start,
            SleepLog.date < end,
        ).order_by(SleepLog.date)
    )
    return list(result.scalars().all())


async def get_today_workout(session: AsyncSession, user_id: int) -> WorkoutLog | None:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(WorkoutLog).where(
            WorkoutLog.user_id == user_id,
            WorkoutLog.date >= today
        ).order_by(WorkoutLog.date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_last_sleep(session: AsyncSession, user_id: int) -> SleepLog | None:
    result = await session.execute(
        select(SleepLog).where(SleepLog.user_id == user_id)
        .order_by(SleepLog.date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_supplements_today(session: AsyncSession, user_id: int) -> list:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(SupplementLog).where(
            SupplementLog.user_id == user_id,
            SupplementLog.date >= today
        )
    )
    return list(result.scalars().all())


async def get_meal_log(session: AsyncSession, meal_id: int, user_id: int) -> MealLog | None:
    result = await session.execute(
        select(MealLog).where(MealLog.id == meal_id, MealLog.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def delete_meal_log(session: AsyncSession, meal_id: int, user_id: int) -> bool:
    result = await session.execute(
        delete(MealLog).where(MealLog.id == meal_id, MealLog.user_id == user_id)
    )
    await session.commit()
    return result.rowcount > 0


async def get_last_meal_logs(session: AsyncSession, user_id: int, limit: int = 5) -> list[MealLog]:
    result = await session.execute(
        select(MealLog).where(MealLog.user_id == user_id)
        .order_by(MealLog.date.desc()).limit(limit)
    )
    return list(result.scalars().all())


# ─── WorkoutLog ──────────────────────────────────────────────

async def add_workout_log(
    session: AsyncSession, user_id: int, workout_name: str,
    duration_minutes: int = 0, total_volume: float = 0,
    subjective_feel: int = 5, calories_burned: float = 0
) -> WorkoutLog:
    log = WorkoutLog(
        user_id=user_id, workout_name=workout_name,
        duration_minutes=duration_minutes, total_volume=total_volume,
        subjective_feel=subjective_feel, calories_burned=calories_burned
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


async def add_exercise_set(
    session: AsyncSession, log_id: int, exercise_name: str,
    set_number: int, weight_kg: float, reps: int, rpe: float = 5
) -> ExerciseSet:
    es = ExerciseSet(
        log_id=log_id, exercise_name=exercise_name,
        set_number=set_number, weight_kg=weight_kg, reps=reps, rpe=rpe
    )
    session.add(es)
    await session.commit()
    return es


async def get_last_exercise_session(
    session: AsyncSession, user_id: int, exercise_name: str
) -> WorkoutLog | None:
    result = await session.execute(
        select(WorkoutLog)
        .options(selectinload(WorkoutLog.exercise_sets))
        .where(
            WorkoutLog.user_id == user_id,
            WorkoutLog.workout_name.ilike(f"%{exercise_name}%")
        )
        .order_by(WorkoutLog.date.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_workout_logs_today(session: AsyncSession, user_id: int) -> list[WorkoutLog]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(WorkoutLog).where(
            WorkoutLog.user_id == user_id,
            WorkoutLog.date >= today
        ).order_by(WorkoutLog.date)
    )
    return list(result.scalars().all())


async def delete_workout_log(session: AsyncSession, log_id: int, user_id: int) -> bool:
    result = await session.execute(
        delete(WorkoutLog).where(WorkoutLog.id == log_id, WorkoutLog.user_id == user_id)
    )
    await session.commit()
    return result.rowcount > 0


# ─── WeightHistory ───────────────────────────────────────────

async def add_weight(session: AsyncSession, user_id: int, kg: float) -> WeightHistory:
    wh = WeightHistory(user_id=user_id, weight_kg=kg)
    session.add(wh)
    await session.commit()
    return wh


async def get_weight_history(session: AsyncSession, user_id: int, days: int = 30) -> list[WeightHistory]:
    since = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        select(WeightHistory).where(
            WeightHistory.user_id == user_id,
            WeightHistory.date >= since
        ).order_by(WeightHistory.date)
    )
    return list(result.scalars().all())


# ─── SleepLog ────────────────────────────────────────────────

async def add_sleep(
    session: AsyncSession, user_id: int,
    sleep_time: datetime, wake_time: datetime, duration_hours: float
) -> SleepLog:
    log = SleepLog(
        user_id=user_id, sleep_time=sleep_time,
        wake_time=wake_time, duration_hours=duration_hours
    )
    session.add(log)
    await session.commit()
    return log


async def get_sleep_last(session: AsyncSession, user_id: int) -> SleepLog | None:
    result = await session.execute(
        select(SleepLog).where(SleepLog.user_id == user_id)
        .order_by(SleepLog.date.desc()).limit(1)
    )
    return result.scalar_one_or_none()


# ─── FoodItem ────────────────────────────────────────────────

async def get_food_by_name(session: AsyncSession, name: str) -> FoodItem | None:
    result = await session.execute(select(FoodItem).where(FoodItem.name.ilike(name)))
    return result.scalar_one_or_none()


async def add_food_item(session: AsyncSession, **kwargs) -> FoodItem:
    food = FoodItem(**kwargs)
    session.add(food)
    await session.commit()
    return food


# ─── SupplementLog ───────────────────────────────────────────

async def add_supplement_log(
    session: AsyncSession, user_id: int,
    supplement_name: str, dose: str
) -> SupplementLog:
    log = SupplementLog(user_id=user_id, supplement_name=supplement_name, dose=dose)
    session.add(log)
    await session.commit()
    return log


# ─── WorkoutProgram ──────────────────────────────────────────

async def get_user_programs(session: AsyncSession, user_id: int) -> list[WorkoutProgram]:
    result = await session.execute(
        select(WorkoutProgram)
        .options(selectinload(WorkoutProgram.exercises))
        .where(WorkoutProgram.user_id == user_id)
    )
    return list(result.scalars().all())
