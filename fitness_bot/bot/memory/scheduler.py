import logging
import datetime
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
    from bot.db.models import User, UserMemoryProfile, MealLog, TimeObservation, ObservationType
    from sqlalchemy import select, func
    from bot.cache.redis_client import cache_get

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    current_hour = now.hour
    current_minute = now.minute

    async with async_session() as session:
        result = await session.execute(
            select(User.id, UserMemoryProfile.avg_wake_time, UserMemoryProfile.avg_sleep_time,
                   UserMemoryProfile.busy_hours, UserMemoryProfile.avg_meal_times)
            .join(UserMemoryProfile, User.id == UserMemoryProfile.user_id)
        )
        rows = result.all()

    for row in rows:
        user_id, wake, sleep, busy, meal_times = row
        if busy and isinstance(busy, list):
            busy_hours = [int(h.split(":")[0]) for h in busy if ":" in h]
            if current_hour in busy_hours:
                continue

        reminders = await cache_get(f"reminders:{user_id}")
        if reminders:
            for reminder in reminders:
                try:
                    h, m = reminder["time"].split(":")
                    if int(h) == current_hour and int(m) == current_minute:
                        logger.info(f"Notification for user {user_id}: {reminder['text']}")
                except (ValueError, KeyError):
                    continue

        if meal_times and isinstance(meal_times, list):
            today = now.date()
            for meal_time_str in meal_times:
                try:
                    meal_hour = int(meal_time_str.split(":")[0])
                    expected_minutes = meal_hour * 60
                    current_minutes = current_hour * 60 + current_minute
                    if current_minutes == expected_minutes + 60:
                        async with async_session() as s:
                            meal_count = await s.scalar(
                                select(func.count(MealLog.id)).where(
                                    MealLog.user_id == user_id,
                                    func.date(MealLog.date) == today,
                                    func.strftime("%H", MealLog.date) == f"{meal_hour:02d}",
                                )
                            )
                            if meal_count == 0:
                                user_row = await s.execute(
                                    select(User.tg_id).where(User.id == user_id)
                                )
                                tg_id = user_row.scalar_one_or_none()
                                if tg_id:
                                    await _send_missed_meal_notification(
                                        tg_id, meal_time_str,
                                        busy_hours if busy and isinstance(busy, list) else [],
                                        current_hour
                                    )
                except (ValueError, IndexError):
                    continue


async def _send_missed_meal_notification(tg_id: int, expected_time: str,
                                         busy_hours: list, current_hour: int):
    from bot.ai.clients import ask_ai_race
    from bot.ai.prompts import SYSTEM_PROMPT
    from bot.config import BOT_TOKEN
    from telegram import Bot

    busy_context = ""
    if busy_hours and current_hour in busy_hours:
        busy_context = f"Сейчас пользователь в обычно занятом промежутке ({current_hour}:00)."

    prompt = (
        f"Пользователь обычно ест около {expected_time}, сейчас {current_hour}:00 — "
        f"прошёл час с ожидаемого времени, а он не записал приём пищи в лог. "
        f"{busy_context}"
        f"Напиши короткое (1-2 предложения) заботливое напоминание в свободной форме. "
        f"Учти контекст: пользователь мог быть занят. Не пиши жёстко 'ты пропустил еду'. "
        f"Лучше мягко: 'учитывая твой обычный график, возможно, ты был занят — "
        f"не забудь поесть, когда появится возможность'."
    )
    response = await ask_ai_race(
        [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        tools=None, temperature=0.8, max_tokens=256
    )
    message = response.get("content", "")
    if not message:
        if busy_context:
            message = ("Учитывая твой обычный график, возможно, ты был занят — "
                       "не забудь поесть, когда появится возможность.")
        else:
            message = ("Судя по твоему обычному расписанию, сейчас самое время поесть. "
                       "Не забывай про питание!")

    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=tg_id, text=message)
        logger.info(f"Sent missed meal notification to tg_id={tg_id}")
    except Exception as e:
        logger.error(f"Failed to send missed meal notification to tg_id={tg_id}: {e}")


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
