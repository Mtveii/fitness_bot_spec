import logging
from datetime import datetime, UTC, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def send_message(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send message to {user_id}: {e}")


def _has_notification(user_data: dict, key: str) -> bool:
    settings = user_data.get("settings", {})
    notif = settings.get("notifications", {})
    return notif.get(key, True)


async def check_nutrition_deficit(bot, user_id: int) -> None:
    from bot.cache.redis_client import get_today_state
    from bot.db.base import async_session
    from bot.db import crud
    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return

    if not _has_notification({"settings": user.settings}, "nutrition_deficit"):
        return

    state = await get_today_state(user_id)
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    if state["protein"] < targets["protein_g"] * 0.8:
        deficit = targets["protein_g"] - state["protein"]
        fav = (user.favorite_foods or ["творог", "курица", "яйца"])[:3]
        foods_str = ", ".join(fav)
        await send_message(bot, user_id,
            f"⚠️ Белок −{deficit:.0f}г до цели.\n"
            f"Съешь: {foods_str}"
        )


async def send_weigh_reminder(bot, user_id: int) -> None:
    await send_message(bot, user_id, "⚖️ Время взвешиться! Напиши /weight [кг]")


async def send_supplement_reminder(bot, user_id: int, supplement: dict) -> None:
    await send_message(bot, user_id,
        f"💊 Не забудь: {supplement['name']} {supplement['dose']}"
    )


async def reset_all_today_states(bot) -> None:
    from bot.cache.redis_client import reset_today_state, USE_REDIS
    import json
    from pathlib import Path
    from bot.cache.redis_client import STATE_DIR

    if USE_REDIS:
        return

    if STATE_DIR.exists():
        for f in STATE_DIR.glob("today_*.json"):
            try:
                uid = int(f.stem.split("_")[1])
                await reset_today_state(uid)
            except Exception:
                pass


def setup_scheduler(bot, user_id: int, user_data: dict) -> None:
    supplements = user_data.get("supplements", []) or []
    sleep_schedule = user_data.get("sleep_schedule", {}) or {}

    for supp in supplements:
        for time_str in supp.get("times", []):
            try:
                h, m = map(int, time_str.split(":"))
                scheduler.add_job(
                    send_supplement_reminder, CronTrigger(hour=h, minute=m),
                    args=[bot, user_id, supp],
                    id=f"supp_{user_id}_{supp['name']}_{time_str}",
                    replace_existing=True,
                )
            except Exception as e:
                logger.error(f"Failed to schedule supplement reminder: {e}")

    scheduler.add_job(
        send_weigh_reminder, CronTrigger(day_of_week="mon", hour=7, minute=0),
        args=[bot, user_id],
        id=f"weigh_{user_id}",
        replace_existing=True,
    )

    async def send_weekly_report():
        await send_message(bot, user_id,
            "📊 Недельный отчёт готов! Напиши /week чтобы посмотреть."
        )

    scheduler.add_job(
        send_weekly_report, CronTrigger(day_of_week="sun", hour=20, minute=0),
        id=f"weekly_{user_id}",
        replace_existing=True,
    )

    preferred_sleep = sleep_schedule.get("preferred_sleep")
    if preferred_sleep:
        try:
            h, m = map(int, preferred_sleep.split(":"))
            m -= 30
            if m < 0:
                m += 60
                h -= 1
            if h < 0:
                h += 24

            async def send_sleep_reminder():
                await send_message(bot, user_id, "🌙 Скоро время сна!")

            scheduler.add_job(
                send_sleep_reminder, CronTrigger(hour=h, minute=m),
                id=f"sleep_{user_id}",
                replace_existing=True,
            )
        except Exception:
            pass

    preferred_wake = sleep_schedule.get("preferred_wake")
    if preferred_wake:
        try:
            h, m = map(int, preferred_wake.split(":"))

            async def send_wake_reminder():
                await send_message(bot, user_id, "🌅 Доброе утро!")

            scheduler.add_job(
                send_wake_reminder, CronTrigger(hour=h, minute=m),
                id=f"wake_{user_id}",
                replace_existing=True,
            )
        except Exception:
            pass

    deficit_hour = 21
    deficit_min = 0
    if preferred_sleep:
        try:
            sh, sm = map(int, preferred_sleep.split(":"))
            deficit_hour = (sh - 2) % 24
            deficit_min = sm
        except Exception:
            pass

    async def deficit_check():
        await check_nutrition_deficit(bot, user_id)

    scheduler.add_job(
        deficit_check, CronTrigger(hour=deficit_hour, minute=deficit_min),
        id=f"deficit_{user_id}",
        replace_existing=True,
    )

    # P4.19: water reminders — каждые 2 часа с 8 до 22
    async def send_water_reminder():
        await send_message(bot, user_id, "💧 Не забудь попить воды!")

    for h in range(8, 23, 2):
        scheduler.add_job(
            send_water_reminder, CronTrigger(hour=h, minute=0),
            id=f"water_{user_id}_{h}",
            replace_existing=True,
        )


async def restore_all_schedulers(bot) -> None:
    from bot.db.base import async_session
    from bot.db import crud

    async with async_session() as session:
        users = await crud.get_all_users(session)

    for user in users:
        try:
            setup_scheduler(bot, user.tg_id, {
                "supplements": user.supplements or [],
                "sleep_schedule": user.sleep_schedule or {},
            })
        except Exception as e:
            logger.error(f"Failed to restore scheduler for user {user.tg_id}: {e}")

    logger.info(f"Restored schedulers for {len(users)} users")
