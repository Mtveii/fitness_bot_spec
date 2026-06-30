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
    result: dict
    is_pending: bool = False
    pending_summary: str = ""


async def handle_tool_calls(tool_calls: list, user_id: int, context: dict) -> list[ToolResult]:
    results = []
    has_pending = await cache_get(f"pending_action:{user_id}")

    low_risk_calls = []
    high_risk_call = None

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func_info = tc.get("function", {})
        name = func_info.get("name", "")
        try:
            args = json.loads(func_info.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        if has_pending and name not in CONFIRM_TOOLS:
            results.append(ToolResult(
                tool_name=name,
                result={"status": "deferred", "message": "Сначала подтвердите или отмените ожидающее действие"},
                is_pending=True,
            ))
            continue

        if name in HIGH_RISK_TOOLS:
            high_risk_call = (name, args)
            continue

        raw = await execute_tool(name, args, user_id, context)
        parsed = raw if isinstance(raw, dict) else json.loads(raw)
        is_pending = parsed.get("status") == "pending"
        results.append(ToolResult(
            tool_name=name,
            result=parsed,
            is_pending=is_pending,
            pending_summary=parsed.get("summary", "") if is_pending else "",
        ))

    if high_risk_call and not has_pending:
        name, args = high_risk_call
        raw = await execute_tool(name, args, user_id, context)
        parsed = raw if isinstance(raw, dict) else json.loads(raw)
        is_pending = parsed.get("status") == "pending"
        results.append(ToolResult(
            tool_name=name,
            result=parsed,
            is_pending=is_pending,
            pending_summary=parsed.get("summary", "") if is_pending else "",
        ))

    return results
