import pytest
from bot.calculators.tdee import calc_bmr, calc_tdee, calc_target_calories
from bot.calculators.nutrition import calc_macros
from bot.calculators.workout_metrics import calc_volume, estimate_calories_burned


class TestTdee:
    def test_bmr_male(self):
        bmr = calc_bmr("M", 80, 180, 28)
        assert 1800 < bmr < 2000

    def test_bmr_female(self):
        bmr = calc_bmr("F", 60, 165, 25)
        assert 1300 < bmr < 1500

    def test_tdee_sedentary(self):
        tdee = calc_tdee("M", 80, 180, 28, "sedentary")
        bmr = calc_bmr("M", 80, 180, 28)
        assert tdee == round(bmr * 1.2, 1)

    def test_tdee_active(self):
        tdee = calc_tdee("M", 80, 180, 28, "active")
        bmr = calc_bmr("M", 80, 180, 28)
        assert tdee == round(bmr * 1.725, 1)

    def test_target_lose(self):
        tdee = calc_tdee("M", 80, 180, 28, "moderate")
        target = calc_target_calories(tdee, "lose")
        assert target == round(tdee - 500, 1)

    def test_target_gain(self):
        tdee = calc_tdee("M", 80, 180, 28, "moderate")
        target = calc_target_calories(tdee, "gain")
        assert target == round(tdee + 300, 1)

    def test_target_maintain(self):
        tdee = calc_tdee("M", 80, 180, 28, "moderate")
        target = calc_target_calories(tdee, "maintain")
        assert target == round(tdee, 1)


class TestNutrition:
    def test_macros_lose(self):
        macros = calc_macros(2000, "lose")
        assert abs(macros["protein_g"] - 200.0) < 1
        assert abs(macros["fat_g"] - 66.7) < 1
        assert abs(macros["carbs_g"] - 150.0) < 1

    def test_macros_maintain(self):
        macros = calc_macros(2000, "maintain")
        assert abs(macros["protein_g"] - 150.0) < 1

    def test_macros_gain(self):
        macros = calc_macros(2000, "gain")
        assert abs(macros["carbs_g"] - 250.0) < 1


class TestWorkoutMetrics:
    def test_volume(self):
        v = calc_volume(50, 10)
        assert v == 500.0

    def test_calories_burned(self):
        c = estimate_calories_burned(70, 30, met=6.0)
        assert 200 < c < 250
