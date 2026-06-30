TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "log_food_item",
            "description": "Записать приём пищи (еду). Низкий риск — выполняется сразу.",

            "parameters": {
                "type": "object",
                "properties": {
                    "food_name": {"type": "string", "description": "Название продукта/блюда"},
                    "weight_g": {"type": "number", "description": "Вес в граммах"},
                    "calories": {"type": "number", "description": "Калории (если известны)"},
                    "protein": {"type": "number", "description": "Белки в граммах"},
                    "fat": {"type": "number", "description": "Жиры в граммах"},
                    "carbs": {"type": "number", "description": "Углеводы в граммах"},
                },
                "required": ["food_name", "weight_g"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_workout",
            "description": "Предложить новую тренировку. Высокий риск — требует подтверждения.",

            "parameters": {
                "type": "object",
                "properties": {
                    "workout_name": {"type": "string", "description": "Название тренировки"},
                    "exercises": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "sets": {"type": "integer"},
                                "reps": {"type": "string"},
                                "weight_kg": {"type": "number"},
                            }
                        },
                        "description": "Список упражнений"
                    },
                },
                "required": ["workout_name", "exercises"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "propose_reminder",
            "description": "Создать напоминание. Высокий риск — требует подтверждения.",

            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст напоминания"},
                    "time": {"type": "string", "description": "Время в формате HH:MM"},
                    "days": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Дни недели (mon,tue,wed,thu,fri,sat,sun)"
                    },
                },
                "required": ["text", "time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_time_observation",
            "description": "Зафиксировать наблюдение о времени (подъём, отбой, приём пищи). Низкий риск.",

            "parameters": {
                "type": "object",
                "properties": {
                    "observation_type": {
                        "type": "string",
                        "enum": ["wake", "sleep", "meal"],
                        "description": "Тип наблюдения"
                    },
                    "observed_at": {
                        "type": "string",
                        "description": "Время события в формате YYYY-MM-DD HH:MM"
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Надёжность сигнала 0-1"
                    },
                    "raw_text": {
                        "type": "string",
                        "description": "Исходная фраза пользователя"
                    }
                },
                "required": ["observation_type", "observed_at"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_preference",
            "description": "Обновить предпочтение пользователя (тон общения, формат ответов и т.д.). Низкий риск.",

            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Ключ настройки: communication_tone, gender_switch, reply_format"
                    },
                    "value": {
                        "type": "string",
                        "description": "Новое значение"
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_stats",
            "description": "Получить статистику пользователя за период. Только чтение, низкий риск.",

            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "custom"],
                        "description": "Период статистики"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Начало периода YYYY-MM-DD (для custom)"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Конец периода YYYY-MM-DD (для custom)"
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Какие метрики: calories, protein, weight, workouts, sleep"
                    }
                },
                "required": ["period"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract_profile_info",
            "description": "Извлечь информацию о пользователе из свободного текста для заполнения профиля.",

            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя пользователя"},
                    "gender": {"type": "string", "enum": ["M", "F", ""], "description": "Пол"},
                    "age": {"type": "integer", "description": "Возраст"},
                    "height_cm": {"type": "number", "description": "Рост в см"},
                    "weight_kg": {"type": "number", "description": "Вес в кг"},
                    "target_weight_kg": {"type": "number", "description": "Целевой вес"},
                    "activity_level": {
                        "type": "string",
                        "enum": ["sedentary", "light", "moderate", "active", "very_active"],
                        "description": "Уровень активности"
                    },
                    "goal": {
                        "type": "string",
                        "enum": ["lose", "maintain", "gain"],
                        "description": "Цель"
                    },
                    "allergies": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Аллергии"
                    },
                    "favorite_foods": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Любимые продукты"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_action",
            "description": "Подтвердить ожидающее действие. Вызывай, когда пользователь говорит 'да', 'подтверждаю', 'ок', 'согласен' и подобное.",

            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation_text": {"type": "string", "description": "Подтверждение пользователя"}
                },
                "required": ["confirmation_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reject_action",
            "description": "Отклонить ожидающее действие. Вызывай, когда пользователь отказывается или просит отменить.",

            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Причина отказа"}
                },
                "required": ["reason"]
            }
        }
    },
]

HIGH_RISK_TOOLS = {"propose_workout", "propose_reminder"}
LOW_RISK_TOOLS = {"log_food_item", "mark_time_observation", "update_preference", "query_stats", "extract_profile_info"}
CONFIRM_TOOLS = {"confirm_action", "reject_action"}


def gemini_tool_defs(tool_names: list) -> list:
    mapping = {}
    for td in TOOL_DEFINITIONS:
        name = td["function"]["name"]
        mapping[name] = {
            "name": name,
            "description": td["function"]["description"],
            "parameters": td["function"]["parameters"],
        }
    return [mapping[n] for n in tool_names if n in mapping]


def get_tool_schema(names: list = None) -> list:
    if names is None:
        return TOOL_DEFINITIONS
    return [td for td in TOOL_DEFINITIONS if td["function"]["name"] in names]


ALL_TOOL_NAMES = [td["function"]["name"] for td in TOOL_DEFINITIONS]
ONBOARDING_TOOL_NAMES = ["extract_profile_info"]
MAIN_TOOL_NAMES = [n for n in ALL_TOOL_NAMES if n not in ("extract_profile_info", "confirm_action", "reject_action")]


def requires_confirmation(tool_name: str) -> bool:
    return tool_name in HIGH_RISK_TOOLS
