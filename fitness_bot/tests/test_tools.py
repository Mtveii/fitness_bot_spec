import json
import pytest
from bot.tools.definitions import (
    TOOL_DEFINITIONS, HIGH_RISK_TOOLS, LOW_RISK_TOOLS,
    CONFIRM_TOOLS, ALL_TOOL_NAMES,
)


class TestToolDefinitions:
    def test_six_core_tools_exist(self):
        names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        core = {"log_food_item", "propose_workout", "propose_reminder",
                "mark_time_observation", "update_preference", "query_stats"}
        assert core.issubset(names)

    def test_extract_profile_info_exists(self):
        names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        assert "extract_profile_info" in names

    def test_confirm_reject_tools_exist(self):
        names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        assert "confirm_action" in names
        assert "reject_action" in names

    def test_all_high_risk_tools_have_confirmation(self):
        for td in TOOL_DEFINITIONS:
            name = td["function"]["name"]
            if name in HIGH_RISK_TOOLS:
                desc = td["function"]["description"]
                assert "подтверждения" in desc or "Подтверждение" in desc

    def test_risk_separation_no_overlap(self):
        assert HIGH_RISK_TOOLS.isdisjoint(LOW_RISK_TOOLS)
        assert HIGH_RISK_TOOLS.isdisjoint(CONFIRM_TOOLS)
        assert LOW_RISK_TOOLS.isdisjoint(CONFIRM_TOOLS)

    def test_all_tools_accounted_for(self):
        defined = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        categorized = HIGH_RISK_TOOLS | LOW_RISK_TOOLS | CONFIRM_TOOLS
        assert defined == categorized


class TestRiskMetadata:
    def test_requires_confirmation_function_works(self):
        from bot.tools.definitions import requires_confirmation
        for name in HIGH_RISK_TOOLS:
            assert requires_confirmation(name) is True, f"{name} should require confirmation"
        for name in LOW_RISK_TOOLS:
            assert requires_confirmation(name) is False, f"{name} should not require confirmation"


class TestToolSchemas:
    def test_log_food_required_params(self):
        td = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "log_food_item")
        props = td["function"]["parameters"]["properties"]
        assert "food_name" in props
        assert "weight_g" in props

    def test_mark_time_observation_enum(self):
        td = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "mark_time_observation")
        obs_type = td["function"]["parameters"]["properties"]["observation_type"]
        assert "wake" in obs_type["enum"]
        assert "sleep" in obs_type["enum"]
        assert "meal" in obs_type["enum"]

    def test_query_stats_period_enum(self):
        td = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "query_stats")
        period = td["function"]["parameters"]["properties"]["period"]
        assert "day" in period["enum"]
        assert "week" in period["enum"]
        assert "month" in period["enum"]
        assert "custom" in period["enum"]
