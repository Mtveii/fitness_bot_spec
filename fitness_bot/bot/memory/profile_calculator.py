import datetime
import logging
from collections import defaultdict
from sqlalchemy import select, func

from bot.db.base import async_session
from bot.db.models import User, TimeObservation, UserMemoryProfile, ObservationType
from bot.config import OBSERVATION_RETENTION_DAYS

logger = logging.getLogger(__name__)


async def recalculate_profile(user_id: int):
    async with async_session() as session:
        cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=OBSERVATION_RETENTION_DAYS)
        query = select(TimeObservation).where(
            TimeObservation.user_id == user_id,
            TimeObservation.observed_at >= cutoff,
        ).order_by(TimeObservation.observed_at)
        obs_list = (await session.execute(query)).scalars().all()

        wake_obs = [o for o in obs_list if o.observation_type == ObservationType.wake]
        sleep_obs = [o for o in obs_list if o.observation_type == ObservationType.sleep]
        meal_obs = [o for o in obs_list if o.observation_type == ObservationType.meal]
        activity_obs = [o for o in obs_list if o.observation_type == ObservationType.message_activity]

        avg_wake = _weighted_avg_time(wake_obs) if wake_obs else None
        avg_sleep = _weighted_avg_time(sleep_obs) if sleep_obs else None
        avg_meal_times = _find_meal_clusters(meal_obs) if meal_obs else None
        busy_hours = _find_busy_hours(activity_obs) if activity_obs else None

        result = await session.execute(
            select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if not profile:
            profile = UserMemoryProfile(user_id=user_id)
            session.add(profile)

        if avg_wake:
            profile.avg_wake_time = avg_wake
        if avg_sleep:
            profile.avg_sleep_time = avg_sleep
        if avg_meal_times:
            profile.avg_meal_times = avg_meal_times
        if busy_hours:
            profile.busy_hours = busy_hours
        profile.last_summarized_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

        await session.commit()

        await _clean_old_observations(session, user_id, cutoff)

        await _summarize_preferences(user_id)

        logger.info(f"Profile recalculated for user {user_id}: wake={avg_wake}, sleep={avg_sleep}")


async def _summarize_preferences(user_id: int):
    from bot.ai.clients import ask_ai_race
    from bot.ai.prompts import SUMMARIZATION_PROMPT
    from bot.db.models import MealLog, WorkoutLog, SleepLog, WeightHistory

    async with async_session() as session:
        cutoff = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(days=OBSERVATION_RETENTION_DAYS)
        meals = (await session.execute(
            select(MealLog.food_name).where(MealLog.user_id == user_id, MealLog.date >= cutoff).limit(50)
        )).scalars().all()
        workouts = (await session.execute(
            select(WorkoutLog.workout_name).where(WorkoutLog.user_id == user_id, WorkoutLog.date >= cutoff).limit(20)
        )).scalars().all()
        sleeps = (await session.execute(
            select(SleepLog.duration_hours).where(SleepLog.user_id == user_id, SleepLog.date >= cutoff).limit(30)
        )).scalars().all()

        observations_text = f"Еда: {', '.join(set(meals[-20:])) if meals else 'нет данных'}\n"
        observations_text += f"Тренировки: {', '.join(set(workouts[-10:])) if workouts else 'нет данных'}\n"
        if sleeps:
            avg_sleep_h = sum(sleeps) / len(sleeps)
            observations_text += f"Средняя длительность сна: {avg_sleep_h:.1f}ч ({len(sleeps)} записей)\n"

        response = await ask_ai_race(
            [{"role": "user", "content": SUMMARIZATION_PROMPT + "\n\n" + observations_text}],
            tools=None, temperature=0.5, max_tokens=512
        )
        summary_text = response.get("content", "")

        if summary_text:
            profile_result = await session.execute(
                select(UserMemoryProfile).where(UserMemoryProfile.user_id == user_id)
            )
            profile = profile_result.scalar_one_or_none()
            if profile:
                profile.preferences_summary = summary_text
                profile.last_summarized_at = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
                await session.commit()


def _weighted_avg_time(observations: list) -> str:
    total_weight = 0.0
    total_minutes = 0.0
    for obs in observations:
        t = obs.observed_at
        minutes = t.hour * 60 + t.minute
        w = obs.confidence
        total_minutes += minutes * w
        total_weight += w
    if total_weight == 0:
        return "07:00"
    avg_minutes = total_minutes / total_weight
    hours = int(avg_minutes // 60) % 24
    mins = int(avg_minutes % 60)
    return f"{hours:02d}:{mins:02d}"


def _find_meal_clusters(observations: list) -> list:
    hours = defaultdict(float)
    counts = defaultdict(int)
    for obs in observations:
        h = obs.observed_at.hour
        hours[h] += obs.confidence
        counts[h] += 1
    significant = [h for h, w in hours.items() if w >= 1.0 or counts[h] >= 2]
    significant.sort()
    return [f"{h:02d}:00" for h in significant]


def _find_busy_hours(observations: list) -> list:
    if not observations:
        return []
    hour_counts = defaultdict(int)
    for obs in observations:
        hour_counts[obs.observed_at.hour] += 1
    max_count = max(hour_counts.values()) if hour_counts else 1
    threshold = max_count * 0.2
    busy = [h for h, c in hour_counts.items() if c < threshold]
    busy.sort()
    return [f"{h:02d}:00" for h in busy]


async def _clean_old_observations(session, user_id: int, cutoff: datetime.datetime):
    from sqlalchemy import delete
    stmt = delete(TimeObservation).where(
        TimeObservation.user_id == user_id,
        TimeObservation.observed_at < cutoff,
    )
    await session.execute(stmt)
    await session.commit()
