def calc_volume(weight_kg: float, reps: int) -> float:
    return round(weight_kg * reps, 1)


def estimate_calories_burned(weight_kg: float, duration_minutes: int, met: float = 5.0) -> float:
    return round(met * 3.5 * weight_kg / 200 * duration_minutes, 1)
