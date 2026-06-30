def calc_bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    if gender.upper() == "M":
        return 88.362 + (13.397 * weight_kg) + (4.799 * height_cm) - (5.677 * age)
    return 447.593 + (9.247 * weight_kg) + (3.098 * height_cm) - (4.330 * age)


ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
    "very_active": 1.9,
}


def calc_tdee(gender: str, weight_kg: float, height_cm: float, age: int, activity_level: str) -> float:
    bmr = calc_bmr(gender, weight_kg, height_cm, age)
    mult = ACTIVITY_MULTIPLIERS.get(activity_level, 1.2)
    return round(bmr * mult, 1)


def calc_target_calories(tdee: float, goal: str) -> float:
    if goal == "lose":
        return round(tdee - 500, 1)
    elif goal == "gain":
        return round(tdee + 300, 1)
    return round(tdee, 1)
