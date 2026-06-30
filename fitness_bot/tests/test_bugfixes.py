import pytest
from bot.handlers.messages import _should_use_tools, CONFIRM_KEYWORDS


class TestConfirmKeywordsFix:
    def test_short_confirmations_trigger_tools(self):
        assert _should_use_tools("да") is True
        assert _should_use_tools("нет") is True
        assert _should_use_tools("ок") is True
        assert _should_use_tools("ага") is True
        assert _should_use_tools("угу") is True

    def test_long_confirmations_trigger_tools(self):
        assert _should_use_tools("согласен") is True
        assert _should_use_tools("конечно") is True
        assert _should_use_tools("отмена") is True
        assert _should_use_tools("отменяю") is True
        assert _should_use_tools("подтверждаю") is True

    def test_case_insensitive(self):
        assert _should_use_tools("ДА") is True
        assert _should_use_tools("Да") is True
        assert _should_use_tools("НЕТ") is True

    def test_greetings_still_blocked(self):
        assert _should_use_tools("привет") is False
        assert _should_use_tools("как дела") is False
        assert _should_use_tools("хай") is False

    def test_very_short_rejected(self):
        assert _should_use_tools("а") is False
        assert _should_use_tools("я") is False
        assert _should_use_tools("") is False
        assert _should_use_tools(None) is False

    def test_confirm_keywords_set_completeness(self):
        expected = {"да", "нет", "ок", "ага", "угу", "согласен", "отмена", "отменяю", "конечно"}
        assert CONFIRM_KEYWORDS == expected


class TestReminderDedup:
    @pytest.mark.asyncio
    async def test_duplicate_reminder_not_added(self):
        from bot.tools.handlers import handle_propose_reminder, handle_confirm, _save_reminder
        from bot.cache.redis_client import cache_set, cache_get, cache_delete
        import datetime

        uid = 8888
        await cache_delete(f"pending_action:{uid}")
        await cache_delete(f"reminders:{uid}")

        pending = {
            "type": "reminder",
            "user_id": uid,
            "data": {"text": "Пить воду", "time": "10:00", "days": ["mon"]},
            "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
        }
        await cache_set(f"pending_action:{uid}", pending, ttl=300)
        await handle_confirm({"confirmation_text": "да"}, uid, {})

        pending2 = {
            "type": "reminder",
            "user_id": uid,
            "data": {"text": "Пить воду", "time": "10:00", "days": ["mon"]},
            "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
        }
        await cache_set(f"pending_action:{uid}", pending2, ttl=300)
        await handle_confirm({"confirmation_text": "да"}, uid, {})

        reminders = await cache_get(f"reminders:{uid}")
        assert len(reminders) == 1

        await cache_delete(f"pending_action:{uid}")
        await cache_delete(f"reminders:{uid}")
