"""
P4.20 — Function calling tool definitions для Groq/Gemini.
Каждый tool — OpenAI-совместимый JSON Schema с флагом requires_confirmation.
"""

WORKOUT_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_workout",
        "description": (
            "Извлекает структурированные данные о новой тренировке из описания юзера. "
            "Вызывай, когда юзер описывает тренировку, которую хочет добавить в программу "
            "(например 'добавь тренировку: жим лёжа 3х10 80кг, присед 4х8 100кг'). "
            "НЕ вызывай, если юзер просто рассказывает о тренировке в прошедшем времени "
            "или оценивает её."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "workout_name": {
                    "type": "string",
                    "description": "Название тренировки, например 'Грудь+трицепс' или 'Ноги'",
                },
                "exercises": {
                    "type": "array",
                    "description": "Список упражнений в тренировке",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Название упражнения"},
                            "sets": {"type": "integer", "description": "Количество подходов"},
                            "reps": {"type": "string", "description": "Повторения, например '8-10' или '10'"},
                            "weight_kg": {"type": "number", "description": "Рабочий вес в кг, 0 если не указан"},
                        },
                        "required": ["name", "sets", "reps"],
                    },
                },
            },
            "required": ["workout_name", "exercises"],
        },
    },
}
WORKOUT_TOOL["requires_confirmation"] = True


REMINDER_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_reminder",
        "description": (
            "Создаёт новое регулярное напоминание по запросу юзера. "
            "Вызывай, когда юзер просит напомнить о чём-то в определённое время "
            "('напомни выпить протеин в 8 вечера', 'напоминай мне о растяжке каждое утро')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Короткое описание напоминания, например 'Выпить протеин'",
                },
                "time": {
                    "type": "string",
                    "description": (
                        "Время напоминания в формате HH:MM (24-часовой). "
                        "Если юзер указал относительное время ('через час', 'через 30 минут'), "
                        "вычисли абсолютное время на основе текущего времени, переданного в system prompt."
                    ),
                },
                "advance_warning_minutes": {
                    "type": "integer",
                    "description": "За сколько минут заранее предупредить (0 = не нужно)",
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["daily", "once"],
                    "description": "Повторяется каждый день или один раз",
                },
            },
            "required": ["label", "time"],
        },
    },
}
REMINDER_TOOL["requires_confirmation"] = True


LOG_FOOD_TOOL = {
    "type": "function",
    "function": {
        "name": "log_food_item",
        "description": (
            "Записывает приём пищи, который юзер упоминает в свободном тексте "
            "(например 'съел 200г гречки', 'выпил протеиновый шейк'). "
            "Вызывай сразу при явном упоминании еды — подтверждение не требуется. "
            "НЕ вызывай, если юзер просто спрашивает о питании или даёт совет."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "food_name": {
                    "type": "string",
                    "description": "Название продукта/блюда, например 'гречка с курицей'",
                },
                "weight_g": {
                    "type": "number",
                    "description": "Вес порции в граммах",
                },
            },
            "required": ["food_name", "weight_g"],
        },
    },
}
LOG_FOOD_TOOL["requires_confirmation"] = False


ALL_TOOLS = [WORKOUT_TOOL, REMINDER_TOOL, LOG_FOOD_TOOL]
TOOLS_BY_NAME = {t["function"]["name"]: t for t in ALL_TOOLS}
