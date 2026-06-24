MET = {
    "strength": 5.0,
    "cardio": 7.0,
    "hiit": 8.5,
    "stretch": 2.5,
}


def workout_calories(met_type: str, weight_kg: float, duration_hours: float) -> float:
    """MET × вес (кг) × часы"""
    met_value = MET.get(met_type, 5.0)
    return met_value * weight_kg * duration_hours


def total_volume(sets: list[dict]) -> float:
    """
    Сумма (вес × повторения) по всем подходам.
    sets: [{"weight": 80, "reps": 8}, ...]
    """
    return sum(s["weight"] * s["reps"] for s in sets)
