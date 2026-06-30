import re
import json
import logging
from datetime import datetime, UTC
from bot.ai.clients import ask_ai_race
from bot.db.base import async_session
from bot.db import crud

logger = logging.getLogger(__name__)

WORKOUT_PARSE_PROMPT = """Ты ИИ-ассистент для логирования тренировок. Из сообщения пользователя извлеки данные о тренировке.

Правила:
1. Если нашёл упражнения — верни JSON (БЕЗ markdown):
{"action":"add","exercises":[{"name":"...","sets":[{"weight_kg":ЧИСЛО,"reps":ЧИСЛО}]}],"duration_minutes":ЧИСЛО,"calories_burned":ЧИСЛО}

2. Если данных недостаточно для конкретных упражнений — верни JSON:
{"action":"ask","question":"..."}
   Вопрос должен быть конкретным: что именно нужно уточнить? Вес? Повторения? Название упражнения?

3. Пользователь может отправить:
   - Всю тренировку сразу: "Жим 80кг 4×8, Присед 100кг 5×5, 45 мин"
   - Одно упражнение: "Жим лёжа 80кг 4×8"
   - Просто описание: "пожал лежа 80 на 8 четыре подхода"
   - Группу мышц: "грудь и трицепс" → создай типовые упражнения

4. Если пользователь указал группу мышц без конкретных упражнений — создай 2-3 типовых упражнения для этой группы с примерными весами.

5. Если указано время тренировки ("45 минут", "тренился час") — запиши в duration_minutes.

6. Если указаны калории ("сжег 300 калорий") — запиши в calories_burned.

7. Определяй названия упражнений из описания:
   "жму лежа" → "Жим штанги лежа"
   "тягу сверху" → "Тяга верхнего блока"
   "приседаю" → "Приседания со штангой"
   "бицепс со штангой" → "Сгибание рук со штангой стоя"
   "разводка" → "Разводка гантелей лежа"
   "подтягиваюсь" → "Подтягивания"

8. ВСЕГДА указывай weight_kg и reps для каждого подхода. Если пользователь не указал — спроси в "ask".

9. Если одно сообщение содержит несколько упражнений — распарси все.

10. Не выдумывай данные. Если не знаешь — спроси."""

PROGRAM_CREATE_PROMPT = """Ты ИИ-ассистент для создания программ тренировок. Из сообщения пользователя извлеки данные для программы.

Профиль пользователя:
{profile}

Правила:
1. Если данных достаточно — верни JSON (БЕЗ markdown):
{"action":"save","name":"...","days":["Пн","Ср"],"exercises":[{"name":"...","type":"compound/isolation/cardio","muscle_groups":["грудь"],"planned_sets":4,"planned_reps":"8-10","planned_weight_kg":80,"rest_seconds":90}]}

2. Если данных не хватает — верни JSON с вопросом:
{"action":"ask","question":"..."}
   Вопрос должен быть конкретным: что именно нужно уточнить? Недостающие дни, упражнения, название.

3. Определи дни недели из текста:
   "пн и ср" → ["Пн","Ср"]
   "каждый день" → ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
   "по будням" → ["Пн","Вт","Ср","Чт","Пт"]
   "через день" → ["Пн","Ср","Пт"]
   "2 раза в неделю" → спроси какие дни

4. Если пользователь указал только мышцы (грудь, спина, ноги) — подбери типовые эффективные упражнения для этих групп с учётом цели пользователя (цель: {goal}).

5. Типы упражнений: compound (базовое), isolation (изолирующее), cardio (кардио).

6. Для разных целей подбирай разное количество подходов:
   - bulk (набор массы): 8-12 повторений, 3-4 подхода
   - cut (похудение): 12-15 повторений, 3-4 подхода
   - strength (сила): 3-5 повторений, 4-5 подходов
   - recomp/maintain: 8-12 повторений, 3 подхода

7. Если текст пользователя не содержит информации о программе — спроси, что он хочет создать.

8. История предыдущих сообщений: {history}"""

WORKOUT_ANALYSIS_PROMPT = """Ты фитнес-аналитик. Проанализируй тренировку пользователя.

Данные тренировки:
- Название: {workout_name}
- Длительность: {duration_minutes} мин
- Упражнения: {exercises_text}
- Потрачено калорий: {calories_burned}

Верни ТОЛЬКО JSON БЕЗ markdown:
{{
  "assessment": "короткая оценка программы (1-2 предложения)",
  "pros": ["плюс1", "плюс2"],
  "cons": ["минус1", "минус2"],
  "suggestions": ["совет1", "совет2"],
  "calories_burned_calc": число
}}
"""

EXERCISE_SUGGEST_PROMPT = """Ты ИИ-фитнес-эксперт. Пользователь хочет составить программу тренировок.

Профиль пользователя:
{profile}

Тренировал эти мышцы за последние 14 дней (объём в кг):
{muscle_volume}

Недостаточно тренированные мышцы (отстающие):
{weak_muscles}

Задача: предложи программу тренировок которая:
1. Уделяет приоритет отстающим мышцам
2. Подходит под цель пользователя ({goal})
3. Учитывает его уровень и пол ({gender}, {age} лет)

Верни ТОЛЬКО JSON БЕЗ markdown:
{{
  "suggested_name": "название программы",
  "suggested_days": ["Пн","Ср","Пт"],
  "suggested_exercises": [
    {{
      "name": "Упражнение",
      "type": "compound/isolation",
      "muscle_groups": ["грудь"],
      "planned_sets": 4,
      "planned_reps": "8-12",
      "planned_weight_kg": 0,
      "rest_seconds": 90,
      "note": "почему это упражнение"
    }}
  ],
  "explanation": "короткое объяснение программы (1-2 предложения)"
}}
"""


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return None


async def _get_profile_context(user_id: int) -> str:
    """Get user profile as a formatted string for AI prompts."""
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return ""
    goal_names = {"cut": "похудение", "bulk": "набор массы", "recomp": "рельеф", "maintain": "поддержка"}
    gender = "мужчина" if user.gender == "M" else "женщина"
    goal = goal_names.get(user.goal, user.goal)
    return f"Пол: {gender}, Возраст: {user.age}, Вес: {user.weight_kg}кг, Цель: {goal}"


async def ask_workout_ai(text: str) -> dict:
    system = WORKOUT_PARSE_PROMPT
    result = await ask_ai_race(system, text, max_tokens=800)
    if not result:
        return {"action": "ask", "question": "Напиши упражнение: название, вес, подходы×повторения. Например: «Жим лёжа 80кг 4×8»"}
    answer = result[0]
    parsed = _parse_json(answer)
    if not parsed or "action" not in parsed:
        return {"action": "ask", "question": "Напиши подробнее: какое упражнение, с каким весом, сколько подходов и повторений?"}

    if parsed.get("action") == "add":
        exercises = parsed.get("exercises", [])
        for ex in exercises:
            sets = ex.get("sets", [])
            for s in sets:
                if not s.get("weight_kg"):
                    s["weight_kg"] = 0
                if not s.get("reps"):
                    s["reps"] = 8

    return parsed


async def analyze_workout(workout_name: str, duration_minutes: int, exercises: list, calories_burned: float) -> dict:
    exercises_text = "\n".join(
        f"- {ex['name']}: {', '.join(f'{s["weight_kg"]}кг×{s["reps"]}' for s in ex['sets'])}"
        for ex in exercises
    )
    system = WORKOUT_ANALYSIS_PROMPT.format(
        workout_name=workout_name,
        duration_minutes=duration_minutes,
        exercises_text=exercises_text,
        calories_burned=calories_burned,
    )
    result = await ask_ai_race(system, "Проанализируй тренировку", max_tokens=500)
    if not result:
        return {
            "assessment": "Тренировка записана.",
            "pros": [], "cons": [], "suggestions": [],
            "calories_burned_calc": calories_burned,
        }
    parsed = _parse_json(result[0])
    if not parsed:
        return {
            "assessment": "Тренировка записана.",
            "pros": [], "cons": [], "suggestions": [],
            "calories_burned_calc": calories_burned,
        }
    return parsed


async def ask_create_program(text: str, history: str = "", user_id: int = 0) -> dict:
    profile = ""
    goal = "не указана"
    if user_id:
        profile = await _get_profile_context(user_id)
        if "Цель:" in profile:
            goal = profile.split("Цель:")[-1].strip()

    system = PROGRAM_CREATE_PROMPT.format(
        history=history,
        profile=profile or "нет данных профиля",
        goal=goal,
    )
    result = await ask_ai_race(system, text, max_tokens=1000)
    if not result:
        return {"action": "ask", "question": "Опиши программу: название, дни недели, упражнения."}
    answer = result[0]
    parsed = _parse_json(answer)
    if not parsed or "action" not in parsed:
        return {"action": "ask", "question": "Напиши подробнее: название программы, дни недели, упражнения."}
    return parsed


async def suggest_program(user_id: int) -> dict | None:
    """AI-powered program suggestion based on user profile and weak muscles."""
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return None

        programs = await crud.get_user_programs(session, user.id)
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        recent_logs = await crud.get_workout_logs_between(
            session, user.id,
            today - timedelta(days=14),
            today + timedelta(days=1),
        )

    profile = await _get_profile_context(user_id)

    muscle_volume = {}
    for log in recent_logs:
        if log.exercise_sets:
            for es in log.exercise_sets:
                vol = es.weight_kg * es.reps
                name_lower = es.exercise_name.lower()
                for program in programs:
                    for ex in program.exercises:
                        if ex.name.lower() in name_lower:
                            for mg in (ex.muscle_groups or []):
                                muscle_volume[mg] = muscle_volume.get(mg, 0) + vol

    all_muscles = set()
    for p in programs:
        for ex in p.exercises:
            for mg in (ex.muscle_groups or []):
                all_muscles.add(mg)

    weak_muscles = [m for m in all_muscles if m not in muscle_volume]

    muscle_vol_str = "\n".join(f"{m}: {v:.0f}кг" for m, v in muscle_volume.items()) or "нет данных"
    weak_str = ", ".join(weak_muscles) if weak_muscles else "все мышцы тренируются"

    goal = "не указана"
    if "Цель:" in profile:
        goal = profile.split("Цель:")[-1].strip()

    user_info = f"Пол: {profile.split(',')[0] if ',' in profile else 'не указан'}, Возраст: {user.age}"

    system = EXERCISE_SUGGEST_PROMPT.format(
        profile=profile,
        muscle_volume=muscle_vol_str,
        weak_muscles=weak_str,
        goal=goal,
        gender=user_info.split(",")[0] if "," in user_info else "не указан",
        age=user.age,
    )

    result = await ask_ai_race(system, "Предложи программу тренировок", max_tokens=1500)
    if not result:
        return None

    parsed = _parse_json(result[0])
    return parsed
