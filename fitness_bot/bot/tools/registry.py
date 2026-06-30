import json
import logging
import asyncio

from bot.tools.definitions import CONFIRM_TOOLS

logger = logging.getLogger(__name__)

TOOL_EXECUTION_TIMEOUT = 10


async def execute_tool(tool_name: str, args: dict, user_id: int, context: dict) -> str:
    logger.info(f"Executing tool {tool_name} for user {user_id}: {args}")
    from bot.tools.handlers import (
        handle_log_food, handle_propose_workout, handle_propose_reminder,
        handle_mark_time_observation, handle_update_preference,
        handle_query_stats, handle_extract_profile, handle_confirm, handle_reject,
    )
    handler_map = {
        "log_food_item": handle_log_food,
        "propose_workout": handle_propose_workout,
        "propose_reminder": handle_propose_reminder,
        "mark_time_observation": handle_mark_time_observation,
        "update_preference": handle_update_preference,
        "query_stats": handle_query_stats,
        "extract_profile_info": handle_extract_profile,
        "confirm_action": handle_confirm,
        "reject_action": handle_reject,
    }
    handler = handler_map.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = await asyncio.wait_for(
            handler(args, user_id, context),
            timeout=TOOL_EXECUTION_TIMEOUT,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(f"Tool {tool_name} timed out after {TOOL_EXECUTION_TIMEOUT}s")
        return {"error": "Tool execution timed out"}
    except Exception as e:
        logger.exception(f"Error executing {tool_name}: {e}")
        return {"error": "Internal tool error"}
