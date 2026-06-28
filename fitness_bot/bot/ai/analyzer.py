"""
Умный анализатор сообщений без ИИ.
Понимает естественный язык и даёт обратную связь на основе данных.
"""
import re
from datetime import datetime, UTC, timedelta


MUSCLE_GROUPS = {
    "грудь": ["грудь", "грудные", "жим", "разводка", "бабочка"],
    "спина": ["спина", "тяга", "подтягивания", "ноги", "разгибание"],
    "плечи": ["плечи", "дельты", "жим стоя", "разводка стоя"],
    "руки": ["бицепс", "трицепс", "руки", "сгибание", "разгибание"],
    "ноги": ["ноги", "квадрицепс", "бицепс бедра", "ягодицы", "присед", "жим ногами"],
    "кардио": ["кардио", "бег", "велосипед", "эллипс", "скакалка"],
}


def analyze_message(text: str, user_state: dict, targets: dict) -> dict:
    """
    Анализирует сообщение пользователя и возвращает:
    - intent: распознанное намерение
    - response: ответ (строка или None)
    - action: данные для записи (dict или None)
    """
    low = text.lower().strip()

    # Проверка на еду
    food_match = _parse_food_intent(low)
    if food_match:
        return {"intent": "food", "response": None, "action": food_match}

    # Проверка на шаги
    steps_match = _parse_steps_intent(low)
    if steps_match:
        return {"intent": "steps", "response": None, "action": steps_match}

    # Проверка на вес
    weight_match = _parse_weight_intent(low)
    if weight_match:
        return {"intent": "weight", "response": None, "action": weight_match}

    # Проверка на сон
    sleep_match = _parse_sleep_intent(low)
    if sleep_match:
        return {"intent": "sleep", "response": None, "action": sleep_match}

    # Проверка на "что делать" / "как дела" / general status
    if any(w in low for w in ["как дела", "что делать", "что нужно", "статус", "прогресс"]):
        return {"intent": "status", "response": _generate_status(user_state, targets), "action": None}

    # Проверка на "что ел" / "что сделал"
    if any(w in low for w in ["что я ел", "что я сделал", "что было", "история"]):
        return {"intent": "history", "response": _generate_history(user_state), "action": None}

    # Проверка на совет по тренировке
    if any(w in low for w in ["что тренировать", "какие мышцы", "что качать", "тренировка"]):
        return {"intent": "workout_advice", "response": _generate_workout_advice(user_state), "action": None}

    # Проверка на "сколько осталось"
    if any(w in low for w in ["сколько осталось", "сколько до цели", "дефицит", "профицит"]):
        return {"intent": "deficit", "response": _generate_deficit_info(user_state, targets), "action": None}

    return {"intent": "unknown", "response": None, "action": None}


def _parse_food_intent(text: str) -> dict | None:
    """Парсит сообщения о еде: 'съел 200г гречки', 'поел курицу 150г'"""
    patterns = [
        r"(?:съел|поел|выпил|ел|кушал)\s+(.+)",
        r"(\d+)\s*(?:г|гр|грам)\s+(.+)",
        r"(.+)\s+(\d+)\s*(?:г|гр|gram)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                # Пытаемся определить что число, а что текст
                for g in groups:
                    if re.match(r"\d+", g):
                        weight = int(re.search(r"\d+", g).group())
                        food = [x for x in groups if x != g][0].strip()
                        return {"weight_g": weight, "food_name": food}
                return {"weight_g": int(re.search(r"\d+", groups[0]).group()), "food_name": groups[1].strip()}

    return None


def _parse_steps_intent(text: str) -> dict | None:
    """Парсит сообщения о шагах: 'прошёл 8000', '8000 шагов'"""
    patterns = [
        r"(?:прошл|прошёл|ходил|walk)\s+(\d+)",
        r"(\d+)\s*(?:шаг|步)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            steps = int(match.group(1))
            if 1 <= steps <= 100000:
                return {"steps": steps}

    return None


def _parse_weight_intent(text: str) -> dict | None:
    """Парсит сообщения о весе: 'вес 85.5', 'взвесился 85.5'"""
    patterns = [
        r"(?:вес|взвес|вешу)\s*(\d+(?:[.,]\d+)?)",
        r"(\d+(?:[.,]\d+)?)\s*(?:кг|kg)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            weight = float(match.group(1).replace(",", "."))
            if 20 <= weight <= 300:
                return {"weight_kg": weight}

    return None


def _parse_sleep_intent(text: str) -> dict | None:
    """Парсит сообщения о сне: 'лёг в 23, встал в 7'"""
    patterns = [
        r"(?:лег|лёг|спал|zasнул)\s*(?:в\s*)?(\d{1,2})[:\s]*(\d{0,2})",
        r"(\d{1,2})[:\s]*(\d{2})\s*[-–]\s*(\d{1,2})[:\s]*(\d{2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            groups = match.groups()
            if len(groups) == 4:
                # Формат: HH:MM - HH:MM
                sleep_h, sleep_m, wake_h, wake_m = map(int, groups)
                return {
                    "sleep_time": f"{sleep_h:02d}:{sleep_m:02d}",
                    "wake_time": f"{wake_h:02d}:{wake_m:02d}",
                }
            elif len(groups) == 2:
                # Формат: лег в HH
                sleep_h = int(groups[0])
                sleep_m = int(groups[1]) if groups[1] else 0
                return {"sleep_time": f"{sleep_h:02d}:{sleep_m:02d}", "wake_time": None}

    return None


def _generate_status(state: dict, targets: dict) -> str:
    """Генерирует статус на основе текущего состояния."""
    lines = []

    cal = state.get("calories_in", 0)
    cal_target = targets.get("calories", 2000)
    prot = state.get("protein", 0)
    prot_target = targets.get("protein_g", 150)
    steps = state.get("steps", 0)

    # Калории
    if cal == 0:
        lines.append("🍽 Сегодня ещё ничего не ел.")
    elif cal < cal_target * 0.5:
        remaining = cal_target - cal
        lines.append(f"🍽 Калорий: {cal:.0f}/{cal_target}. Осталось {remaining:.0f}.")
    elif cal < cal_target:
        remaining = cal_target - cal
        lines.append(f"🍽 Почти норма: {cal:.0f}/{cal_target}. Ещё {remaining:.0f}.")
    else:
        over = cal - cal_target
        lines.append(f"⚠️ Перебор: {cal:.0f}/{cal_target} (+{over:.0f})")

    # Белок
    if prot < prot_target * 0.5:
        deficit = prot_target - prot
        lines.append(f"🥩 Белок критически мало! {prot:.0f}/{prot_target}г. Нужно ещё {deficit:.0f}г.")
    elif prot < prot_target:
        deficit = prot_target - prot
        lines.append(f"🥩 Белок: {prot:.0f}/{prot_target}г. Добавь {deficit:.0f}г.")

    # Шаги
    if steps == 0:
        lines.append("👟 Шагов пока 0.")
    elif steps < 5000:
        lines.append(f"👟 Шаги: {steps}. До 5000 ещё {5000 - steps}.")
    elif steps >= 10000:
        lines.append(f"👟 Отлично! {steps} шагов!")
    else:
        lines.append(f"👟 Шаги: {steps}.")

    return "\n".join(lines)


def _generate_history(state: dict) -> str:
    """Генерирует краткую историю за день."""
    cal = state.get("calories_in", 0)
    prot = state.get("protein", 0)
    fat = state.get("fat", 0)
    carbs = state.get("carbs", 0)
    steps = state.get("steps", 0)

    if cal == 0:
        return "Сегодня ещё нет записей."

    lines = [
        f"📊 За сегодня:",
        f"  Калории: {cal:.0f}ккал",
        f"  Белок: {prot:.0f}г | Жиры: {fat:.0f}г | Углеводы: {carbs:.0f}г",
        f"  Шаги: {steps}",
    ]

    return "\n".join(lines)


def _generate_workout_advice(state: dict) -> str:
    """Генерирует совет по тренировке на основе данных."""
    now = datetime.now(UTC)
    day_name = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][now.weekday()]

    return (
        f"📅 Сегодня {day_name}.\n"
        f"Посмотри /suggest чтобы увидеть какие мышцы нуждаются в проработке."
    )


def _generate_deficit_info(state: dict, targets: dict) -> str:
    """Генерирует информацию о дефиците/профиците."""
    cal = state.get("calories_in", 0)
    cal_target = targets.get("calories", 2000)
    balance = cal - cal_target

    if balance < -200:
        return f"📉 Дефицит: {abs(balance):.0f}ккал. Можешь ещё поесть."
    elif balance > 200:
        return f"⚠️ Профицит: +{balance:.0f}ккал. Сегодня перебор."
    else:
        return f"✅ Баланс почти идеальный: {balance:+.0f}ккал."
