import pytest
from bot.handlers.messages import _should_use_tools, SIGNAL_KEYWORDS


class TestPreFilter:
    def test_empty_message(self):
        assert not _should_use_tools("")
        assert not _should_use_tools("  ")

    def test_short_message(self):
        assert not _should_use_tools("а")
        assert not _should_use_tools("я")
        assert not _should_use_tools("привет")

    def test_greeting(self):
        assert not _should_use_tools("Привет, как дела?")
        assert not _should_use_tools("Спасибо!")
        assert not _should_use_tools("Ты молодец")

    def test_food_mention(self):
        assert _should_use_tools("Съел 200г курицы с рисом")
        assert _should_use_tools("Поел овсянку")
        assert _should_use_tools("Завтрак: 2 яйца и тост")

    def test_workout_mention(self):
        assert _should_use_tools("Потренировал грудь и трицепс")
        assert _should_use_tools("Была тренировка 40 минут")
        assert _should_use_tools("Сделал упражнение жим лёжа")

    def test_sleep_mention(self):
        assert _should_use_tools("Лёг спать в 23:00")
        assert _should_use_tools("Проснулся в 7 утра")
        assert _should_use_tools("Спал 8 часов")

    def test_weight_mention(self):
        assert _should_use_tools("Мой вес 80 кг")
        assert _should_use_tools("Похудел на 2кг")

    def test_stats_query(self):
        assert _should_use_tools("Как у меня дела за неделю?")
        assert _should_use_tools("Покажи статистику")

    def test_reminder_request(self):
        assert _should_use_tools("Напомни выпить воду в 15:00")

    def test_tone_change(self):
        assert _should_use_tools("Общайся со мной жёстче")
        assert _should_use_tools("Хочу чтобы ты говорил от женского лица")

    def test_message_with_digits(self):
        assert _should_use_tools("80кг")
        assert _should_use_tools("в 15:00")
