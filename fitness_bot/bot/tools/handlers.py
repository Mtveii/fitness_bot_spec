import json
import datetime
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    User, MealLog, TimeObservation, ObservationType, ObservationSource,
    UserMemoryProfile, WeightHistory, WorkoutLog, ExerciseSet, SleepLog, SupplementLog
)
from bot.db.base import async_session
from bot.cache.redis_client import cache_get, cache_set, cache_delete
from bot.config import PENDING_TTL


async def handle_log_food(args: dict, user_id: int, context: dict) -> dict:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if not user:
            return {"error": "User not found"}
        meal = MealLog(
            user_id=user_id,
            date=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            food_name=args.get("food_name", ""),
            weight_g=args.get("weight_g", 0),
            calories=args.get("calories"),
            protein=args.get("protein"),
            fat=args.get("fat"),
            carbs=args.get("carbs"),
        )
        session.add(meal)
        await session.commit()
    return {"status": "ok", "meal": args.get("food_name"), "weight_g": args.get("weight_g")}


async def handle_propose_workout(args: dict, user_id: int, context: dict) -> dict:
    pending = {
        "type": "workout",
        "user_id": user_id,
        "data": args,
        "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
    }
    await cache_set(f"pending_action:{user_id}", pending, ttl=PENDING_TTL)
    return {
        "status": "pending",
        "message": "Новая тренировка требует подтверждения",
        "summary": f"{args.get('workout_name', 'Тренировка')} — {len(args.get('exercises', []))} упражнений",
    }


async def handle_propose_reminder(args: dict, user_id: int, context: dict) -> dict:
    pending = {
        "type": "reminder",
        "user_id": user_id,
        "data": args,
        "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
    }
    await cache_set(f"pending_action:{user_id}", pending, ttl=PENDING_TTL)
    return {
        "status": "pending",
        "message": "Напоминание требует подтверждения",
        "summary": f"{args.get('text', 'Напоминание')} в {args.get('time', '??:??')}",
    }


async def handle_mark_time_observation(args: dict, user_id: int, context: dict) -> dict:
    obs_type_str = args.get("observation_type", "meal")
    obs_type = ObservationType(obs_type_str)
    source = ObservationSource.explicit
    observed_at = datetime.datetime.fromisoformat(args.get("observed_at")) if args.get("observed_at") else datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    confidence = args.get("confidence", 0.8)
    async with async_session() as session:
        obs = TimeObservation(
            user_id=user_id,
            observation_type=obs_type,
            observed_at=observed_at,
            source=source,
            confidence=confidence,
            raw_text=args.get("raw_text", ""),
        )
        session.add(obs)
        await session.commit()
    return {"status": "ok", "observation_type": obs_type_str, "observed_at": str(observed_at)}


ALLOWED_SETTINGS_KEYS = {"reply_format", "notification_style", "language", "units"}


async def handle_update_preference(args: dict, user_id: int, context: dict) -> dict:
    key = args.get("key", "")
    value = args.get("value", "")
    if key == "communication_tone":
        valid_tones = {"strict", "friendly", "sarcastic", "harsh"}
        if value.split("+")[0] not in valid_tones:
            return {"error": f"Invalid tone: {value}. Valid: {valid_tones}"}
        async with async_session() as session:
            profile = await session.execute(
                select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
            )
            profile = profile.scalar_one_or_none()
            if not profile:
                profile = UserMemoryProfile(user_id=user_id)
                session.add(profile)
            profile.communication_tone = value
            await session.commit()
        return {"status": "ok", "updated": f"{key} = {value}"}
    if key == "gender_switch":
        if value not in ("male", "female"):
            return {"error": f"Invalid gender_switch value: {value}. Must be 'male' or 'female'"}
        async with async_session() as session:
            profile = await session.execute(
                select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
            )
            profile = profile.scalar_one_or_none()
            if not profile:
                profile = UserMemoryProfile(user_id=user_id)
                session.add(profile)
            current = profile.communication_tone or "friendly"
            base = current.split("+")[0] if "+" in current else current
            profile.communication_tone = base + "+" + value
            await session.commit()
        return {"status": "ok", "updated": f"gender_switch = {value}"}
    if key not in ALLOWED_SETTINGS_KEYS:
        return {"error": f"Unknown setting key: {key}. Allowed: {ALLOWED_SETTINGS_KEYS}"}
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user:
            settings = user.settings or {}
            if isinstance(settings, str):
                settings = json.loads(settings)
            settings[key] = value
            user.settings = settings
            await session.commit()
    return {"status": "ok", "updated": f"{key} = {value}"}


async def handle_query_stats(args: dict, user_id: int, context: dict) -> dict:
    period = args.get("period", "day")
    metrics = args.get("metrics", ["calories", "weight"])
    today = datetime.date.today()
    if period == "day":
        start = today
        end = today
    elif period == "week":
        start = today - datetime.timedelta(days=today.weekday())
        end = today
    elif period == "month":
        start = today.replace(day=1)
        end = today
    else:
        start = datetime.date.fromisoformat(args.get("start_date", str(today)))
        end = datetime.date.fromisoformat(args.get("end_date", str(today)))

    async with async_session() as session:
        result = {"period": period, "start": str(start), "end": str(end)}

        if "calories" in metrics:
            from sqlalchemy import func
            query = select(
                func.sum(MealLog.calories), func.sum(MealLog.protein),
                func.sum(MealLog.fat), func.sum(MealLog.carbs)
            ).where(
                MealLog.user_id == user_id,
                MealLog.date >= start,
                MealLog.date <= end + datetime.timedelta(days=1)
            )
            row = (await session.execute(query)).one()
            result["calories"] = row[0] or 0
            result["protein"] = row[1] or 0
            result["fat"] = row[2] or 0
            result["carbs"] = row[3] or 0

        if "weight" in metrics:
            query = select(WeightHistory).where(
                WeightHistory.user_id == user_id,
                WeightHistory.date >= start,
                WeightHistory.date <= end + datetime.timedelta(days=1)
            ).order_by(WeightHistory.date)
            weights = (await session.execute(query)).scalars().all()
            result["weight_entries"] = [{"date": str(w.date), "weight_kg": w.weight_kg} for w in weights]

        if "workouts" in metrics:
            query = select(WorkoutLog).where(
                WorkoutLog.user_id == user_id,
                WorkoutLog.date >= start,
                WorkoutLog.date <= end + datetime.timedelta(days=1)
            ).order_by(WorkoutLog.date)
            workouts = (await session.execute(query)).scalars().all()
            result["workouts"] = [{"date": str(w.date), "name": w.workout_name, "duration": w.duration_minutes} for w in workouts]

        if "sleep" in metrics:
            query = select(SleepLog).where(
                SleepLog.user_id == user_id,
                SleepLog.date >= start,
                SleepLog.date <= end + datetime.timedelta(days=1)
            )
            sleeps = (await session.execute(query)).scalars().all()
            result["sleep"] = [{"date": str(s.date), "hours": s.duration_hours} for s in sleeps]

        return result


async def handle_extract_profile(args: dict, user_id: int, context: dict) -> dict:
    extracted = {k: v for k, v in args.items() if v is not None and v != "" and v != []}
    existing = context.get("profile_data", {})
    merged = {**existing, **extracted}
    context["profile_data"] = merged

    required = ["gender", "age", "height_cm", "weight_kg", "activity_level", "goal"]
    missing = [f for f in required if f not in merged]
    return {
        "status": "partial" if missing else "complete",
        "extracted": extracted,
        "missing": missing,
        "profile": merged,
    }


async def handle_confirm(args: dict, user_id: int, context: dict) -> dict:
    pending = await cache_get(f"pending_action:{user_id}")
    if not pending:
        return {"status": "error", "message": "Нет ожидающих действий"}
    action_type = pending.get("type")
    data = pending.get("data", {})

    if action_type == "workout":
        result = await _save_workout(data, user_id)
    elif action_type == "reminder":
        result = await _save_reminder(data, user_id)
    else:
        result = {"status": "ok", "message": "Действие подтверждено"}

    await cache_delete(f"pending_action:{user_id}")
    return result


async def handle_reject(args: dict, user_id: int, context: dict) -> dict:
    pending = await cache_get(f"pending_action:{user_id}")
    if not pending:
        return {"status": "error", "message": "Нет ожидающих действий"}
    await cache_delete(f"pending_action:{user_id}")
    return {"status": "cancelled", "message": "Действие отменено"}


async def _save_workout(data: dict, user_id: int) -> dict:
    async with async_session() as session:
        log = WorkoutLog(
            user_id=user_id,
            date=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            workout_name=data.get("workout_name", "Тренировка"),
            duration_minutes=data.get("duration_minutes"),
            calories_burned=data.get("calories_burned"),
        )
        session.add(log)
        await session.flush()

        exercises = data.get("exercises", [])
        for i, ex in enumerate(exercises):
            if isinstance(ex, dict):
                es = ExerciseSet(
                    log_id=log.id,
                    exercise_name=ex.get("name", "Unknown"),
                    set_number=ex.get("set_number", i + 1),
                    weight_kg=ex.get("weight_kg", 0),
                    reps=ex.get("reps", 0),
                    rpe=ex.get("rpe"),
                )
                session.add(es)

        await session.commit()
    return {"status": "ok", "workout": data.get("workout_name")}


async def _save_reminder(data: dict, user_id: int) -> dict:
    reminders = await cache_get(f"reminders:{user_id}") or []
    text = data.get("text")
    time = data.get("time")
    for existing in reminders:
        if existing.get("text") == text and existing.get("time") == time:
            return {"status": "ok", "reminder": text, "note": "already exists"}
    reminders.append({
        "text": text,
        "time": time,
        "days": data.get("days", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
    })
    await cache_set(f"reminders:{user_id}", reminders, ttl=86400 * 30)
    return {"status": "ok", "reminder": text}
