import pytest
from bot.handlers.messages import _should_use_tools, SIGNAL_KEYWORDS


class TestPreFilterExtended:
    def test_greeting_no_tools(self):
        assert _should_use_tools("Привет") is False

    def test_thanks_no_tools(self):
        assert _should_use_tools("Спасибо") is False

    def test_how_are_you_no_tools(self):
        assert _should_use_tools("Как дела?") is False

    def test_food_word_triggers(self):
        assert _should_use_tools("ел курицу") is True

    def test_food_exact_keyword(self):
        for kw in ["завтрак", "обед", "ужин", "перекус"]:
            assert _should_use_tools(f"Сделал {kw}") is True

    def test_workout_keywords(self):
        for kw in ["тренировка", "упражнение", "бег", "спорт"]:
            assert _should_use_tools(f"Делаю {kw}") is True

    def test_sleep_keywords(self):
        assert _should_use_tools("спал") is True
        assert _should_use_tools("проснулся") is True
        assert _should_use_tools("лёг спать") is True

    def test_short_keywords_need_context(self):
        assert _should_use_tools("сон") is False
        assert _should_use_tools("вес") is False
        assert _should_use_tools("тон") is False

    def test_weight_keywords(self):
        assert _should_use_tools("вешу 80кг") is True
        assert _should_use_tools("похудел") is True

    def test_stats_keywords(self):
        for kw in ["статистик", "прогресс", "итоги"]:
            assert _should_use_tools(f"Покажи {kw}") is True

    def test_reminder_keywords(self):
        for kw in ["напомни", "напоминание"]:
            assert _should_use_tools(kw) is True

    def test_tone_keywords(self):
        assert _should_use_tools("общайся") is True
        assert _should_use_tools("говори") is True
        assert _should_use_tools("смени тон") is True

    def test_confirm_keywords(self):
        assert _should_use_tools("да") is True
        assert _should_use_tools("нет") is True
        assert _should_use_tools("ок") is True
        assert _should_use_tools("ага") is True
        assert _should_use_tools("подтверждаю") is True
        assert _should_use_tools("отмена") is True
        assert _should_use_tools("согласен") is True

    def test_empty_message(self):
        assert _should_use_tools("") is False

    def test_none_message(self):
        assert _should_use_tools(None) is False

    def test_short_message(self):
        assert _should_use_tools("а") is False
        assert _should_use_tools("я") is False

    def test_long_enough_no_keywords(self):
        assert _should_use_tools("Просто текст без смысла") is False

    def test_digit_triggers(self):
        assert _should_use_tools("200") is False  # too short (< 4 chars)
        assert _should_use_tools("80кг") is True
        assert _should_use_tools("Поел 300г") is True

    def test_mixed_case(self):
        assert _should_use_tools("ЕЛ КУРИЦУ") is True
        assert _should_use_tools("СЪЕЛ рис") is True

    def test_keyword_substring_match(self):
        assert _should_use_tools("тренировал грудь") is True
        assert _should_use_tools("проснулся") is True

    def test_all_keywords_exist(self):
        assert len(SIGNAL_KEYWORDS) > 30


class TestDialogBuffer:
    async def test_empty_buffer_returns_list(self):
        from bot.handlers.messages import _get_dialog_buffer
        buffer = await _get_dialog_buffer(99999999)
        assert isinstance(buffer, list)
        assert len(buffer) == 0

    def test_signal_keywords_completeness(self):
        expected_groups = [
            ["еда", "ел", "ест", "поел", "съел"],
            ["тренировка", "упражнение"],
            ["сон", "спал", "проснулся", "лёг"],
            ["вес", "вешу", "похудел"],
            ["да", "нет", "подтверждаю", "отмена"],
        ]
        for group in expected_groups:
            for kw in group:
                assert kw in SIGNAL_KEYWORDS, f"'{kw}' not in SIGNAL_KEYWORDS"
