import os
import re
import json
import logging
import asyncio
from datetime import datetime, UTC, timedelta, date
from bot.cache.redis_client import get_chat_history, add_chat_message, get_today_state, update_today_state
from bot.db.base import async_session
from bot.db import crud

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

SYSTEM_PROMPT = """Ты строгий тренер {name}, {age}л. Вес:{weight}кг→{target_weight}кг ({weight_trend}).
Подъём:{wake_time}, тренировки:{workout_time}. Цель:{goal}.
КБЖУ:{calories}кк,Б:{protein}г,Ж:{fat}г,У:{carbs}г.
Любимые продукты: {favorite_foods}.

Команды(|):
[[LOG_FOOD|еда|г|время|дата]] [[LOG_WORKOUT|название|мин|кг|ккал|дата]]
[[LOG_SLEEP|сон|подъём|дата]] [[LOG_STEPS|кол-во|дата]]
[[UPDATE_WEIGHT|кг]] [[UPDATE_WAKE|время]] [[UPDATE_WORKOUT_TIME|время]]
[[UPDATE_SETTINGS|ключ|значение]] [[GET_TODAY]] [[SUGGEST_FOOD]]

Даты:сегодня(по умолч),вчера,позавчера,ДД.ММ
Настройки: "будь строже"→[[UPDATE_SETTINGS|ai.personality|strict]]
"напоминания добавок вкл"→[[UPDATE_SETTINGS|notifications.supplements|true]]
"сбрось всё"→[[UPDATE_SETTINGS|reset|all]]
Примеры: "съел 200г гречки"→[[LOG_FOOD|гречка|200|сейчас|сегодня]]
"вчера ел курицу 300г"→[[LOG_FOOD|курица|300|сейчас|вчера]]
"что съесть чтобы закрыть белок"→[[SUGGEST_FOOD]]
Кратко,эмоцией,по-русски."""


def _parse_date(date_str: str) -> datetime:
    """Парсит строку даты в datetime."""
    now = datetime.now(UTC)
    s = date_str.lower().strip()

    if s in ("сейчас", "сегодня", ""):
        return now

    if s == "вчера":
        return now - timedelta(days=1)

    if s == "позавчера":
        return now - timedelta(days=2)

    # ДД.ММ
    m = re.match(r'(\d{1,2})\.(\d{1,2})', s)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        try:
            return now.replace(day=day, month=month, hour=12, minute=0, second=0, microsecond=0)
        except ValueError:
            return now

    # ДД.ММ.ГГГГ
    m = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), 12, 0, 0, tzinfo=UTC)
        except ValueError:
            return now

    return now


async def _get_meals_summary(user_id: int, target_date: datetime | None = None) -> str:
    """КБЖУ за день."""
    if target_date is None:
        target_date = datetime.now(UTC)

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return ""

        from sqlalchemy import select
        from bot.db.models import MealLog
        result = await session.execute(
            select(MealLog).where(
                MealLog.user_id == user.id,
                MealLog.date >= day_start,
                MealLog.date < day_end,
            ).order_by(MealLog.date)
        )
        meals = list(result.scalars().all())

    if not meals:
        return "📋 Нет записей за этот день."

    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)

    total_cal = sum(m.calories for m in meals)
    total_prot = sum(m.protein for m in meals)
    total_fat = sum(m.fat for m in meals)
    total_carbs = sum(m.carbs for m in meals)

    cal_pct = (total_cal / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (total_prot / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0

    date_label = target_date.strftime("%d.%m") if target_date.date() != date.today() else "Сегодня"
    lines = [f"📋 {date_label} ({len(meals)} приёмов):"]
    for m in meals:
        t = m.date.strftime("%H:%M") if m.date else "?"
        lines.append(f"  {t} {m.food_name} {m.weight_g:.0f}г — {m.calories:.0f}кк")

    lines.append("")
    lines.append(f"🔥 Итого: {total_cal:.0f}/{targets['calories']}ккал ({cal_pct:.0f}%)")
    lines.append(f"🥩 Белок: {total_prot:.0f}/{targets['protein_g']}г ({prot_pct:.0f}%)")
    lines.append(f"🧈 Жиры: {total_fat:.0f}/{targets['fat_g']}г")
    lines.append(f"🍞 Углеводы: {total_carbs:.0f}/{targets['carbs_g']}г")

    deficit = targets["calories"] - total_cal
    prot_deficit = targets["protein_g"] - total_prot
    if deficit > 200:
        lines.append(f"\n⚡ Ещё {deficit:.0f}ккал до цели")
    if prot_deficit > 20:
        lines.append(f"🥩 Ещё +{prot_deficit:.0f}г белка")

    return "\n".join(lines)


async def _get_weight_trend(user_id: int) -> str:
    """Тренд веса за 7 дней."""
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return ""

        history = await crud.get_weight_history(session, user.id, days=7)

    if not history:
        return f"{user.weight_kg}кг (без изменений)"

    current = history[-1].weight_kg
    first = history[0].weight_kg
    diff = current - first
    arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
    return f"{current}кг {arrow} {diff:+.1f}кг за 7д"


def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, TypeError):
        return default


def _safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s.strip())
    except (ValueError, TypeError):
        return default


def _validate_action(action_name: str, params: list[str]) -> bool:
    try:
        if action_name == "LOG_FOOD":
            return len(params) >= 2 and params[0].strip() and _safe_float(params[1], -1) >= 0
        elif action_name == "DELETE_FOOD":
            return len(params) == 1 and params[0].strip().isdigit()
        elif action_name == "LOG_WORKOUT":
            return len(params) >= 1 and params[0].strip()
        elif action_name == "LOG_SLEEP":
            return len(params) >= 2
        elif action_name == "LOG_STEPS":
            return len(params) >= 1 and params[0].strip().isdigit()
        elif action_name == "UPDATE_WEIGHT":
            return len(params) == 1 and _safe_float(params[0], -1) >= 0
        elif action_name in ("UPDATE_WAKE", "UPDATE_WORKOUT_TIME"):
            return len(params) == 1 and params[0].strip()
        elif action_name == "UPDATE_SETTINGS":
            return len(params) >= 1 and params[0].strip()
        elif action_name in ("GET_TODAY", "SUGGEST_FOOD"):
            return True
    except Exception:
        return False
    return False


VALID_ACTIONS = {
    "LOG_FOOD", "DELETE_FOOD", "LOG_WORKOUT", "LOG_SLEEP",
    "LOG_STEPS", "UPDATE_WEIGHT", "UPDATE_WAKE", "UPDATE_WORKOUT_TIME",
    "UPDATE_SETTINGS", "GET_TODAY", "SUGGEST_FOOD"
}


async def _execute_action(action_str: str, user_id: int) -> str:
    match = re.match(r'\[\[(\w+)\|(.+?)\]\]', action_str)
    if not match:
        return ""

    action = match.group(1)
    if action not in VALID_ACTIONS:
        return ""

    params = [p.strip() for p in match.group(2).split("|")]
    if not _validate_action(action, params):
        return ""

    try:
        if action == "LOG_FOOD":
            food_name = params[0]
            weight_g = _safe_float(params[1], 100)
            time_str = params[2] if len(params) > 2 else "сейчас"
            date_str = params[3] if len(params) > 3 else "сегодня"

            base_dt = _parse_date(date_str)

            if time_str == "сейчас":
                log_time = base_dt
            else:
                parts = time_str.split(":")
                h = _safe_int(parts[0], 12)
                m = _safe_int(parts[1], 0) if len(parts) > 1 else 0
                log_time = base_dt.replace(hour=min(h, 23), minute=min(m, 59), second=0, microsecond=0)

            from bot.handlers.food import search_food
            food_data = await search_food(food_name)

            if not food_data:
                return f"❌ «{food_name}» не найдено."

            factor = weight_g / 100
            calories = food_data["calories"] * factor
            protein = food_data["protein"] * factor
            fat = food_data["fat"] * factor
            carbs = food_data["carbs"] * factor

            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"
                await crud.add_meal_log(
                    session, user_id=user.id, food_name=food_name, weight_g=weight_g,
                    calories=calories, protein=protein, fat=fat, carbs=carbs,
                    source="ai", log_time=log_time
                )

            # Обновляем today state только если это сегодня
            is_today = log_time.date() == date.today()
            if is_today:
                await update_today_state(
                    user_id, calories_in=calories, protein=protein, fat=fat, carbs=carbs
                )

            date_label = "" if is_today else f" ({log_time.strftime('%d.%m')})"
            result = f"✅ {food_name} {weight_g:.0f}г → {calories:.0f}ккал 🥩{protein:.1f} 🧈{fat:.1f} 🍞{carbs:.1f}{date_label}"
            summary = await _get_meals_summary(user_id, log_time)
            if summary:
                result += f"\n\n{summary}"
            return result

        elif action == "DELETE_FOOD":
            meal_id = _safe_int(params[0])
            async with async_session() as session:
                deleted = await crud.delete_meal_log(session, meal_id, user_id)
            return f"✅ Удалено #{meal_id}" if deleted else f"❌ #{meal_id} не найдено"

        elif action == "LOG_WORKOUT":
            name = params[0]
            duration = _safe_int(params[1] if len(params) > 1 else "0")
            volume = _safe_float(params[2] if len(params) > 2 else "0")
            kcal = _safe_float(params[3] if len(params) > 3 else "0")
            date_str = params[4] if len(params) > 4 else "сегодня"
            log_time = _parse_date(date_str)

            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"
                await crud.add_workout_log(
                    session, user_id=user.id, workout_name=name,
                    duration_minutes=duration, total_volume=volume, calories_burned=kcal
                )

            is_today = log_time.date() == date.today()
            if is_today:
                await update_today_state(user_id, workout_kcal=kcal)

            date_label = "" if is_today else f" ({log_time.strftime('%d.%m')})"
            return f"✅ «{name}» {duration}мин {kcal:.0f}ккал{date_label}"

        elif action == "LOG_SLEEP":
            sleep_str, wake_str = params[0], params[1]
            date_str = params[2] if len(params) > 2 else "сегодня"
            base_dt = _parse_date(date_str)

            sh, sm = map(int, sleep_str.split(":"))
            wh, wm = map(int, wake_str.split(":"))
            sleep_dt = base_dt.replace(hour=min(sh, 23), minute=min(sm, 59), second=0, microsecond=0)
            wake_dt = base_dt.replace(hour=min(wh, 23), minute=min(wm, 59), second=0, microsecond=0)
            if wake_dt <= sleep_dt:
                wake_dt += timedelta(days=1)
            duration = (wake_dt - sleep_dt).total_seconds() / 3600

            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"
                await crud.add_sleep(
                    session, user_id=user.id,
                    sleep_time=sleep_dt, wake_time=wake_dt, duration_hours=duration
                )
            return f"✅ Сон: {sleep_str}→{wake_str} ({duration:.1f}ч)"

        elif action == "LOG_STEPS":
            steps = _safe_int(params[0])
            date_str = params[1] if len(params) > 1 else "сегодня"
            is_today = _parse_date(date_str).date() == date.today()
            if is_today:
                await update_today_state(user_id, steps=steps)
            return f"✅ Шаги: {steps}"

        elif action == "UPDATE_WEIGHT":
            kg = _safe_float(params[0])
            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"
                await crud.add_weight(session, user_id=user.id, kg=kg)
                await crud.update_user(session, user.tg_id, weight_kg=kg)
            return f"✅ Вес: {kg}кг"

        elif action == "UPDATE_WAKE":
            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if user:
                    await crud.update_user(session, user.tg_id, wake_time=params[0])
            return f"✅ Подъём: {params[0]}"

        elif action == "UPDATE_WORKOUT_TIME":
            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if user:
                    await crud.update_user(session, user.tg_id, workout_time=params[0])
            return f"✅ Тренировки: {params[0]}"

        elif action == "GET_TODAY":
            summary = await _get_meals_summary(user_id)
            if summary:
                return summary
            return "📋 Сегодня пока ничего не записано."

        elif action == "UPDATE_SETTINGS":
            key = params[0]
            value = params[1] if len(params) > 1 else ""

            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"

                settings = dict(user.settings or {})

                if key == "reset" and value == "all":
                    from bot.handlers.onboarding import DEFAULT_SETTINGS
                    settings = dict(DEFAULT_SETTINGS)
                    await crud.update_user(session, user.tg_id, settings=settings)
                    return "✅ Настройки сброшены к дефолту"

                parts = key.split(".")
                if len(parts) == 2:
                    section, field = parts
                    if section not in settings:
                        settings[section] = {}
                    if value.lower() in ("true", "вкл", "yes", "да"):
                        settings[section][field] = True
                    elif value.lower() in ("false", "выкл", "no", "нет"):
                        settings[section][field] = False
                    else:
                        try:
                            settings[section][field] = int(value)
                        except ValueError:
                            try:
                                settings[section][field] = float(value)
                            except ValueError:
                                settings[section][field] = value

                    await crud.update_user(session, user.tg_id, settings=settings)
                    return f"✅ Настройка {key} = {settings[section][field]}"

            return "❌ Формат: [[UPDATE_SETTINGS|ключ|значение]]"

        elif action == "SUGGEST_FOOD":
            async with async_session() as session:
                user = await crud.get_user(session, user_id)
                if not user:
                    return "❌ Пройди /onboarding"

            from bot.calculators.tdee import bmr, tdee
            from bot.calculators.nutrition import daily_targets
            bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
            tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
            targets = daily_targets(tdee_val, user.weight_kg, user.goal)

            cal_need = targets["calories"] - today["calories_in"]
            prot_need = targets["protein_g"] - today["protein"]

            fav = user.favorite_foods or ["творог", "курица", "яйца", "гречка"]
            fav_str = ", ".join(fav[:5])

            if prot_need > 20:
                return (
                    f"⚠️ До цели: +{prot_need:.0f}г белка, +{cal_need:.0f}ккал\n"
                    f"Из твоих любимых: {fav_str}\n"
                    f"Запиши: /log [продукт] [граммы]"
                )
            elif cal_need > 200:
                return (
                    f"⚠️ Осталось {cal_need:.0f}ккал\n"
                    f"Варианты: {fav_str}"
                )
            else:
                return f"✅ Ты почти на цели! Осталось {cal_need:.0f}ккал"

    except Exception as e:
        logger.error(f"Action failed: {e}")
        return f"❌ Ошибка: {e}"

    return ""


def _parse_actions(answer: str) -> tuple[str, list[str]]:
    action_pattern = r'\[\[([A-Z_]+)\|([^\]]+?)\]\]'
    actions = []

    def replace_action(m):
        name = m.group(1)
        params = m.group(2)
        if name in VALID_ACTIONS:
            actions.append(f"[[{name}|{params}]]")
        return ""

    clean = re.sub(action_pattern, replace_action, answer).strip()
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean, actions


async def build_context(user_id: int) -> dict:
    from bot.calculators.tdee import bmr, tdee
    from bot.calculators.nutrition import daily_targets

    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return {}

        today_workout = await crud.get_today_workout(session, user.id)
        last_sleep = await crud.get_last_sleep(session, user.id)
        supplements = await crud.get_supplements_today(session, user.id)

        now = datetime.now(UTC)
        week_start = now - timedelta(days=7)
        week_meals = await crud.get_meals_between(session, user.id, week_start, now)
        week_workouts = await crud.get_workout_logs_between(session, user.id, week_start, now)

    today = await get_today_state(user_id)
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    targets = daily_targets(tdee_val, user.weight_kg, user.goal)
    weight_trend = await _get_weight_trend(user_id)

    days_with_meals = len(set(m.date.date() for m in week_meals)) if week_meals else 1
    week_summary = {
        "avg_calories": sum(m.calories for m in week_meals) / max(days_with_meals, 1) if week_meals else 0,
        "avg_protein": sum(m.protein for m in week_meals) / max(days_with_meals, 1) if week_meals else 0,
        "workouts_count": len(week_workouts),
        "total_volume": sum(w.total_volume or 0 for w in week_workouts),
    }

    return {
        "profile": {
            "name": user.name, "goal": user.goal,
            "weight": user.weight_kg,
            "target_weight": user.target_weight_kg,
            "weight_trend": weight_trend,
            "height": user.height_cm,
            "age": user.age, "gender": user.gender,
            "activity_level": user.activity_level,
            "favorite_foods": user.favorite_foods or [],
            "wake_time": user.wake_time or "07:00",
            "workout_time": user.workout_time or "18:00",
        },
        "today": today,
        "targets": targets,
        "workout_today": {
            "name": today_workout.workout_name,
            "volume": today_workout.total_volume,
        } if today_workout else None,
        "sleep_last": last_sleep.duration_hours if last_sleep else None,
        "supplements_taken": len(supplements),
        "week_summary": week_summary,
    }


async def _call_ai(prompt: str) -> str | None:
    if GROQ_API_KEY:
        try:
            import groq
            client = groq.AsyncGroq(api_key=GROQ_API_KEY, timeout=5.0)
            response = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"Groq failed: {e}")

    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-2.0-flash")
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: model.generate_content(prompt)
            )
            return response.text.strip()
        except Exception as e:
            logger.warning(f"Gemini failed: {e}")

    return None


async def chat_with_trainer(user_id: int, message: str) -> str:
    ctx = await build_context(user_id)
    if not ctx:
        return "⚠️ Сначала заверши /onboarding"

    p = ctx["profile"]
    t = ctx["targets"]
    today = ctx["today"]

    cal_pct = (today["calories_in"] / t["calories"] * 100) if t["calories"] > 0 else 0
    prot_pct = (today["protein"] / t["protein_g"] * 100) if t["protein_g"] > 0 else 0

    system_prompt = SYSTEM_PROMPT.format(
        name=p["name"], age=p["age"],
        weight=p["weight"], target_weight=p["target_weight"],
        weight_trend=p["weight_trend"],
        wake_time=p["wake_time"], workout_time=p["workout_time"],
        goal=p["goal"],
        calories=t["calories"], protein=t["protein_g"],
        fat=t["fat_g"], carbs=t["carbs_g"],
        favorite_foods=", ".join(p["favorite_foods"][:5]) if p["favorite_foods"] else "нет",
    )

    workout_info = ""
    if ctx.get("workout_today"):
        workout_info = f" Тр:{ctx['workout_today']['name']}({ctx['workout_today']['volume']:.0f}кг)"
    sleep_info = ""
    if ctx.get("sleep_last"):
        sleep_info = f" Сон:{ctx['sleep_last']:.1f}ч"
    week = ctx.get("week_summary", {})

    context_data = (
        f"К:{today['calories_in']:.0f}/{t['calories']}({cal_pct:.0f}%) "
        f"Б:{today['protein']:.0f}/{t['protein_g']}г({prot_pct:.0f}%) "
        f"Ж:{today['fat']:.0f} У:{today['carbs']:.0f} "
        f"Ш:{today['steps']} Т:{today['workout_kcal']:.0f}ккал"
        f"{workout_info}{sleep_info}"
        f" Нед:ккал{week.get('avg_calories', 0):.0f}/дн,тр{week.get('workouts_count', 0)}"
    )

    history = await get_chat_history(user_id, limit=2)
    history_text = "\n".join(
        f"{'Ч' if m['role'] == 'user' else 'Т'}: {m['content']}" for m in reversed(history)
    )

    full_prompt = f"{system_prompt}\n{context_data}\n{history_text}\nЧеловек: {message}"

    answer = await _call_ai(full_prompt)
    if not answer:
        return "⚠️ ИИ недоступен."

    clean_answer, actions = _parse_actions(answer)

    action_results = []
    for a in actions:
        r = await _execute_action(a, user_id)
        if r:
            action_results.append(r)

    if action_results:
        clean_answer += "\n\n" + "\n".join(action_results)

    await add_chat_message(user_id, "user", message)
    await add_chat_message(user_id, "assistant", clean_answer)

    return clean_answer
