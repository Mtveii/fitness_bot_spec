import pytest
import json
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from bot.tools.handlers import (
    handle_extract_profile, handle_log_food, handle_mark_time_observation,
    handle_update_preference, handle_query_stats, handle_propose_workout,
    handle_propose_reminder, handle_confirm, handle_reject,
)
from bot.tools.dispatcher import handle_tool_calls, ToolResult
from bot.tools.registry import execute_tool
from bot.tools.definitions import (
    TOOL_DEFINITIONS, HIGH_RISK_TOOLS, LOW_RISK_TOOLS, CONFIRM_TOOLS,
    get_tool_schema, requires_confirmation, gemini_tool_defs, ALL_TOOL_NAMES,
    ONBOARDING_TOOL_NAMES, MAIN_TOOL_NAMES,
)
from bot.cache.redis_client import cache_set, cache_delete, cache_get


# ── extract_profile_info ──

class TestExtractProfile:
    async def test_partial_profile_returns_missing_fields(self):
        context = {"profile_data": {}}
        result = await handle_extract_profile(
            {"name": "Антон", "gender": "M", "age": 28}, 0, context
        )
        assert result["status"] == "partial"
        assert "height_cm" in result["missing"]
        assert "weight_kg" in result["missing"]
        assert "activity_level" in result["missing"]
        assert "goal" in result["missing"]
        assert result["extracted"]["name"] == "Антон"

    async def test_complete_profile(self):
        context = {"profile_data": {}}
        result = await handle_extract_profile(
            {
                "name": "Антон", "gender": "M", "age": 28,
                "height_cm": 180, "weight_kg": 80,
                "activity_level": "moderate", "goal": "lose",
            }, 0, context
        )
        assert result["status"] == "complete"
        assert result["missing"] == []
        assert result["profile"]["name"] == "Антон"

    async def test_merge_with_existing_profile(self):
        context = {"profile_data": {"name": "Антон", "gender": "M"}}
        result = await handle_extract_profile(
            {"age": 28, "height_cm": 180}, 0, context
        )
        assert result["status"] == "partial"
        merged = result["profile"]
        assert merged["name"] == "Антон"
        assert merged["age"] == 28
        assert merged["height_cm"] == 180

    async def test_empty_fields_ignored(self):
        context = {"profile_data": {}}
        result = await handle_extract_profile(
            {"name": "", "gender": None}, 0, context
        )
        assert result["extracted"] == {}

    async def test_optional_fields_accepted(self):
        context = {"profile_data": {}}
        result = await handle_extract_profile(
            {
                "name": "Антон", "gender": "M", "age": 28,
                "height_cm": 180, "weight_kg": 80,
                "activity_level": "moderate", "goal": "lose",
                "allergies": ["глютен"], "favorite_foods": ["курица", "рис"],
            }, 0, context
        )
        assert result["status"] == "complete"
        assert result["profile"]["allergies"] == ["глютен"]
        assert result["profile"]["favorite_foods"] == ["курица", "рис"]


# ── log_food_item ──

class TestLogFood:
    async def test_log_food_creates_meal(self):
        result = await handle_log_food(
            {"food_name": "Курица", "weight_g": 200, "calories": 330,
             "protein": 50, "fat": 7, "carbs": 0},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert result["meal"] == "Курица"
        assert result["weight_g"] == 200

    async def test_log_food_minimal_fields(self):
        result = await handle_log_food(
            {"food_name": "Яблоко", "weight_g": 150},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert result["meal"] == "Яблоко"


# ── mark_time_observation ──

class TestMarkTimeObservation:
    async def test_mark_wake_observation(self):
        result = await handle_mark_time_observation(
            {
                "observation_type": "wake",
                "observed_at": "2025-01-15 07:30",
                "confidence": 0.9,
                "raw_text": "Проснулся в 7:30",
            }, user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert result["observation_type"] == "wake"

    async def test_mark_meal_observation(self):
        result = await handle_mark_time_observation(
            {
                "observation_type": "meal",
                "observed_at": "2025-01-15 13:00",
                "confidence": 0.7,
                "raw_text": "Поел в обед",
            }, user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert result["observation_type"] == "meal"

    async def test_mark_observation_default_time(self):
        result = await handle_mark_time_observation(
            {"observation_type": "sleep"},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert "observed_at" in result


# ── update_preference ──

class TestUpdatePreference:
    async def test_update_communication_tone(self):
        result = await handle_update_preference(
            {"key": "communication_tone", "value": "sarcastic"},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert "sarcastic" in result["updated"]

    async def test_update_gender_switch(self):
        result = await handle_update_preference(
            {"key": "gender_switch", "value": "female"},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert "female" in result["updated"]

    async def test_update_generic_setting(self):
        result = await handle_update_preference(
            {"key": "reply_format", "value": "short"},
            user_id=1, context={}
        )
        assert result["status"] == "ok"
        assert "reply_format" in result["updated"]


# ── query_stats ──

class TestQueryStats:
    async def test_query_stats_day(self):
        result = await handle_query_stats(
            {"period": "day", "metrics": ["calories"]},
            user_id=1, context={}
        )
        assert "period" in result
        assert result["period"] == "day"
        assert "calories" in result

    async def test_query_stats_week(self):
        result = await handle_query_stats(
            {"period": "week", "metrics": ["calories", "weight"]},
            user_id=1, context={}
        )
        assert result["period"] == "week"
        assert "calories" in result
        assert "weight_entries" in result

    async def test_query_stats_empty_metrics(self):
        result = await handle_query_stats(
            {"period": "day", "metrics": []},
            user_id=1, context={}
        )
        assert result["period"] == "day"


# ── propose_workout / propose_reminder ──

class TestProposeHighRisk:
    async def test_propose_workout_returns_pending(self):
        result = await handle_propose_workout(
            {
                "workout_name": "Грудь + трицепс",
                "exercises": [{"name": "Жим лёжа", "sets": 3, "reps": "8-10", "weight_kg": 80}],
            }, user_id=999, context={}
        )
        assert result["status"] == "pending"
        assert "summary" in result

    async def test_propose_reminder_returns_pending(self):
        result = await handle_propose_reminder(
            {"text": "Поесть", "time": "13:00", "days": ["mon", "wed", "fri"]},
            user_id=998, context={}
        )
        assert result["status"] == "pending"
        assert "summary" in result


# ── confirm / reject ──

class TestConfirmReject:
    async def test_confirm_without_pending_returns_error(self):
        result = await handle_confirm(
            {"confirmation_text": "да"}, user_id=997, context={}
        )
        assert result["status"] == "error"

    async def test_reject_without_pending_returns_error(self):
        result = await handle_reject(
            {"reason": "не нужно"}, user_id=996, context={}
        )
        assert result["status"] == "error"

    async def test_confirm_with_pending_workout(self):
        await cache_set("pending_action:995", {
            "type": "workout",
            "user_id": 995,
            "data": {"workout_name": "Спина", "exercises": []},
            "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
        }, ttl=300)
        result = await handle_confirm(
            {"confirmation_text": "да"}, user_id=995, context={}
        )
        assert result["status"] == "ok"
        pending = await cache_get("pending_action:995")
        assert pending is None

    async def test_confirm_with_pending_reminder(self):
        await cache_set("pending_action:994", {
            "type": "reminder",
            "user_id": 994,
            "data": {"text": "Витамины", "time": "09:00", "days": ["mon"]},
            "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
        }, ttl=300)
        result = await handle_confirm(
            {"confirmation_text": "ок"}, user_id=994, context={}
        )
        assert result["status"] == "ok"

    async def test_reject_with_pending(self):
        await cache_set("pending_action:993", {
            "type": "reminder",
            "user_id": 993,
            "data": {"text": "Тест", "time": "12:00"},
            "created_at": datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat(),
        }, ttl=300)
        result = await handle_reject(
            {"reason": "передумал"}, user_id=993, context={}
        )
        assert result["status"] == "cancelled"
        pending = await cache_get("pending_action:993")
        assert pending is None


# ── registry / execute_tool ──

class TestRegistry:
    async def test_execute_unknown_tool(self):
        result = await execute_tool("nonexistent_tool", {}, 0, {})
        parsed = json.loads(result)
        assert "error" in parsed

    async def test_execute_log_food(self):
        result = await execute_tool(
            "log_food_item",
            {"food_name": "Тест", "weight_g": 100},
            user_id=1, context={}
        )
        assert result["status"] == "ok"

    async def test_execute_extract_profile(self):
        result = await execute_tool(
            "extract_profile_info",
            {"name": "Тест", "gender": "M"},
            user_id=0, context={"profile_data": {}}
        )
        assert result["status"] == "partial"


# ── dispatcher ──

class TestDispatcher:
    async def test_low_risk_executes_immediately(self):
        tool_calls = [{
            "function": {
                "name": "log_food_item",
                "arguments": json.dumps({"food_name": "Суп", "weight_g": 300}),
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=1, context={})
        assert len(results) == 1
        assert results[0].tool_name == "log_food_item"
        assert results[0].result["status"] == "ok"
        assert results[0].is_pending is False

    async def test_high_risk_goes_to_pending(self):
        uid = 7001
        await cache_delete(f"pending_action:{uid}")
        tool_calls = [{
            "function": {
                "name": "propose_workout",
                "arguments": json.dumps({
                    "workout_name": "Ноги",
                    "exercises": [{"name": "Присед", "sets": 3, "reps": "10", "weight_kg": 60}],
                }),
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=uid, context={})
        assert len(results) == 1
        assert results[0].is_pending is True
        assert results[0].result["status"] == "pending"
        await cache_delete(f"pending_action:{uid}")

    async def test_high_risk_deferred_when_existing_pending(self):
        await cache_set("pending_action:801", {
            "type": "workout", "user_id": 801, "data": {}, "created_at": ""
        }, ttl=300)
        tool_calls = [{
            "function": {
                "name": "propose_reminder",
                "arguments": json.dumps({"text": "Тест", "time": "10:00"}),
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=801, context={})
        assert len(results) == 1
        assert results[0].result["status"] == "deferred"
        assert results[0].is_pending is True

    async def test_multiple_high_risk_first_pending_others_deferred(self):
        uid = 7002
        await cache_delete(f"pending_action:{uid}")
        tool_calls = [
            {
                "function": {
                    "name": "propose_workout",
                    "arguments": json.dumps({"workout_name": "A", "exercises": []}),
                }
            },
            {
                "function": {
                    "name": "propose_reminder",
                    "arguments": json.dumps({"text": "B", "time": "12:00"}),
                }
            },
        ]
        results = await handle_tool_calls(tool_calls, user_id=uid, context={})
        assert len(results) == 2
        statuses = [r.result["status"] for r in results]
        assert statuses[0] == "pending"
        assert statuses[1] == "deferred"
        await cache_delete(f"pending_action:{uid}")

    async def test_confirm_tool_clears_pending(self):
        await cache_set("pending_action:803", {
            "type": "workout", "user_id": 803,
            "data": {"workout_name": "X", "exercises": []}, "created_at": ""
        }, ttl=300)
        tool_calls = [{
            "function": {
                "name": "confirm_action",
                "arguments": json.dumps({"confirmation_text": "да"}),
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=803, context={})
        assert len(results) == 1
        assert results[0].result["status"] == "ok"
        pending = await cache_get("pending_action:803")
        assert pending is None

    async def test_reject_tool_clears_pending(self):
        await cache_set("pending_action:804", {
            "type": "reminder", "user_id": 804,
            "data": {"text": "Y", "time": "14:00"}, "created_at": ""
        }, ttl=300)
        tool_calls = [{
            "function": {
                "name": "reject_action",
                "arguments": json.dumps({"reason": "нет"}),
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=804, context={})
        assert len(results) == 1
        assert results[0].result["status"] == "cancelled"

    async def test_invalid_json_in_tool_calls(self):
        tool_calls = [{
            "function": {
                "name": "log_food_item",
                "arguments": "not json {{{",
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=1, context={})
        assert len(results) == 1
        assert results[0].result["status"] == "ok"

    async def test_unknown_tool_name(self):
        tool_calls = [{
            "function": {
                "name": "unknown_function",
                "arguments": "{}",
            }
        }]
        results = await handle_tool_calls(tool_calls, user_id=1, context={})
        assert len(results) == 1
        assert "error" in results[0].result

    async def test_mixed_low_and_high_risk(self):
        uid = 7003
        await cache_delete(f"pending_action:{uid}")
        tool_calls = [
            {
                "function": {
                    "name": "log_food_item",
                    "arguments": json.dumps({"food_name": "Салат", "weight_g": 150}),
                }
            },
            {
                "function": {
                    "name": "propose_workout",
                    "arguments": json.dumps({"workout_name": "Кардио", "exercises": []}),
                }
            },
        ]
        results = await handle_tool_calls(tool_calls, user_id=uid, context={})
        assert len(results) == 2
        low = [r for r in results if r.tool_name == "log_food_item"]
        high = [r for r in results if r.tool_name == "propose_workout"]
        assert len(low) == 1
        assert "error" in low[0].result or low[0].result.get("status") == "ok"
        assert len(high) == 1
        assert high[0].is_pending is True
        await cache_delete(f"pending_action:{uid}")


# ── definitions helpers ──

class TestDefinitions:
    def test_gemini_tool_defs_filters_by_names(self):
        defs = gemini_tool_defs(["log_food_item", "propose_workout"])
        assert len(defs) == 2
        names = [d["name"] for d in defs]
        assert "log_food_item" in names
        assert "propose_workout" in names

    def test_gemini_tool_defs_skips_unknown(self):
        defs = gemini_tool_defs(["log_food_item", "nonexistent"])
        assert len(defs) == 1

    def test_get_tool_schema_all(self):
        schema = get_tool_schema()
        assert len(schema) == len(TOOL_DEFINITIONS)

    def test_get_tool_schema_filtered(self):
        schema = get_tool_schema(["log_food_item", "query_stats"])
        assert len(schema) == 2

    def test_requires_confirmation_true_for_high_risk(self):
        assert requires_confirmation("propose_workout") is True
        assert requires_confirmation("propose_reminder") is True

    def test_requires_confirmation_false_for_low_risk(self):
        assert requires_confirmation("log_food_item") is False
        assert requires_confirmation("query_stats") is False

    def test_requires_confirmation_unknown_tool(self):
        assert requires_confirmation("nonexistent") is False

    def test_all_tool_names_match_definitions(self):
        assert set(ALL_TOOL_NAMES) == {td["function"]["name"] for td in TOOL_DEFINITIONS}

    def test_onboarding_tools_only_extract(self):
        assert ONBOARDING_TOOL_NAMES == ["extract_profile_info"]

    def test_main_tools_exclude_extract(self):
        assert "extract_profile_info" not in MAIN_TOOL_NAMES
        assert "log_food_item" in MAIN_TOOL_NAMES
