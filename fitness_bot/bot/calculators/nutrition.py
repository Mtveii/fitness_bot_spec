MACRO_SPLITS = {
    "lose": {"protein": 0.40, "fat": 0.30, "carbs": 0.30},
    "maintain": {"protein": 0.30, "fat": 0.25, "carbs": 0.45},
    "gain": {"protein": 0.30, "fat": 0.20, "carbs": 0.50},
}

CALORIES_PER_GRAM = {"protein": 4, "fat": 9, "carbs": 4}


def calc_macros(target_calories: float, goal: str) -> dict:
    split = MACRO_SPLITS.get(goal, MACRO_SPLITS["maintain"])
    result = {}
    for macro, fraction in split.items():
        cal_from_macro = target_calories * fraction
        grams = round(cal_from_macro / CALORIES_PER_GRAM[macro], 1)
        result[macro + "_g"] = grams
        result[macro + "_cal"] = round(cal_from_macro, 1)
    return result
