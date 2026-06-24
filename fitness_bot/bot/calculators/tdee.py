ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "high": 1.725,
}


def bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    """
    Mifflin-St Jeor.
    gender: 'M' or 'F'
    """
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if gender == "M" else base - 161


def tdee(
    bmr_value: float,
    activity_level: str,
    steps: int = 0,
    workout_kcal: float = 0,
    weight_kg: float = 70,
) -> float:
    """
    TDEE = BMR × multiplier + calories_from_steps + workout_kcal
    calories_steps = steps × 0.04 × (weight / 70)
    """
    multiplier = ACTIVITY_MULTIPLIERS.get(activity_level, 1.2)
    calories_steps = steps * 0.04 * (weight_kg / 70)
    return bmr_value * multiplier + calories_steps + workout_kcal
