import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import PROFILE_RECALCULATE_INTERVAL_HOURS, NOTIFICATION_CHECK_INTERVAL_MINUTES

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def recalculate_all_profiles():
    from bot.db.base import async_session
    from bot.db.models import User
    from bot.memory.profile_calculator import recalculate_profile
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(select(User.id))
        user_ids = result.scalars().all()

    for uid in user_ids:
        try:
            await recalculate_profile(uid)
        except Exception as e:
            logger.error(f"Error recalculating profile for user {uid}: {e}")

    logger.info(f"Profiles recalculated for {len(user_ids)} users")


async def check_dynamic_notifications():
    from bot.db.base import async_session
    from bot.db.models import User, UserMemoryProfile
    from sqlalchemy import select
    from bot.cache.redis_client import cache_get

    now = datetime.datetime.utcnow()
    current_hour = now.hour
    current_minute = now.minute

    async with async_session() as session:
        result = await session.execute(
            select(User.id, UserMemoryProfile.avg_wake_time, UserMemoryProfile.avg_sleep_time,
                   UserMemoryProfile.busy_hours)
            .join(UserMemoryProfile, User.id == UserMemoryProfile.user_id)
        )
        rows = result.all()

    for row in rows:
        user_id, wake, sleep, busy = row
        if busy and isinstance(busy, list):
            busy_hours = [int(h.split(":")[0]) for h in busy if ":" in h]
            if current_hour in busy_hours:
                continue

        reminders = await cache_get(f"reminders:{user_id}")
        if not reminders:
            continue

        for reminder in reminders:
            try:
                h, m = reminder["time"].split(":")
                if int(h) == current_hour and int(m) == current_minute:
                    logger.info(f"Notification for user {user_id}: {reminder['text']}")
            except (ValueError, KeyError):
                continue

import datetime


def setup_scheduler():
    scheduler.add_job(
        recalculate_all_profiles,
        "interval",
        hours=PROFILE_RECALCULATE_INTERVAL_HOURS,
        id="recalc_profiles",
        replace_existing=True,
    )
    scheduler.add_job(
        check_dynamic_notifications,
        "interval",
        minutes=NOTIFICATION_CHECK_INTERVAL_MINUTES,
        id="check_notifications",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started with profile recalculation and notification check")
