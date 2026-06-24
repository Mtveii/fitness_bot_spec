GOALS = {
    "cut":      {"kcal_delta": -400, "protein_per_kg": 2.1, "fat_per_kg": 0.9},
    "bulk":     {"kcal_delta": +250, "protein_per_kg": 1.9, "fat_per_kg": 0.9},
    "recomp":   {"kcal_delta": -150, "protein_per_kg": 2.35, "fat_per_kg": 0.9},
    "maintain": {"kcal_delta": 0,    "protein_per_kg": 1.7, "fat_per_kg": 0.9},
}


def daily_targets(tdee_value: float, weight_kg: float, goal: str) -> dict:
    """
    Возвращает {calories, protein_g, fat_g, carbs_g}
    """
    g = GOALS.get(goal, GOALS["maintain"])

    calories = tdee_value + g["kcal_delta"]
    protein_g = weight_kg * g["protein_per_kg"]
    fat_g = weight_kg * g["fat_per_kg"]

    # Углеводы = остаток калорий
    carbs_calories = calories - (protein_g * 4) - (fat_g * 9)
    carbs_g = max(0, carbs_calories / 4)

    return {
        "calories": round(calories),
        "protein_g": round(protein_g),
        "fat_g": round(fat_g),
        "carbs_g": round(carbs_g),
    }
