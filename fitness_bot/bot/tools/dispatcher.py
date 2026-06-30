import json
import logging
from dataclasses import dataclass
from typing import Optional

from bot.tools.definitions import HIGH_RISK_TOOLS, CONFIRM_TOOLS
from bot.tools.registry import execute_tool
from bot.cache.redis_client import cache_get, cache_delete

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    tool_name: str
    tool_call_id: str
    result: dict
    is_pending: bool = False
    pending_summary: str = ""


async def handle_tool_calls(tool_calls: list, user_id: int, context: dict) -> list[ToolResult]:
    results = []
    has_pending = await cache_get(f"pending_action:{user_id}")

    high_risk_deferred = False

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func_info = tc.get("function", {})
        name = func_info.get("name", "")
        tc_id = tc.get("id", "")
        try:
            args = json.loads(func_info.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        if name in CONFIRM_TOOLS:
            parsed = await execute_tool(name, args, user_id, context)
            results.append(ToolResult(tool_name=name, tool_call_id=tc_id, result=parsed))
            has_pending = await cache_get(f"pending_action:{user_id}")
            continue

        if name in HIGH_RISK_TOOLS:
            if has_pending or high_risk_deferred:
                results.append(ToolResult(
                    tool_name=name,
                    tool_call_id=tc_id,
                    result={"status": "deferred", "message": "Сначала подтвердите или отмените ожидающее действие"},
                    is_pending=True,
                ))
                continue
            parsed = await execute_tool(name, args, user_id, context)
            is_pending = parsed.get("status") == "pending"
            if is_pending:
                high_risk_deferred = True
            results.append(ToolResult(
                tool_name=name,
                tool_call_id=tc_id,
                result=parsed,
                is_pending=is_pending,
                pending_summary=parsed.get("summary", "") if is_pending else "",
            ))
            continue

        parsed = await execute_tool(name, args, user_id, context)
        results.append(ToolResult(tool_name=name, tool_call_id=tc_id, result=parsed))

    return results
