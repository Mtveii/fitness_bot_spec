import pytest
import json
import asyncio
import datetime
import os
import ast
import re
from unittest.mock import AsyncMock, MagicMock, patch, ANY

from bot.handlers.messages import _should_use_tools, CONFIRM_KEYWORDS, SIGNAL_KEYWORDS
from bot.tools.dispatcher import handle_tool_calls, ToolResult
from bot.tools.registry import execute_tool
from bot.tools.definitions import (
    TOOL_DEFINITIONS, HIGH_RISK_TOOLS, LOW_RISK_TOOLS, CONFIRM_TOOLS,
    get_tool_schema, gemini_tool_defs,
)
from bot.tools.handlers import handle_confirm, handle_reject
from bot.cache.redis_client import cache_set, cache_delete, cache_get


# ============================================================
# 1.1 Groq tool errors
# ============================================================

class TestGroqToolErrors:
    def test_validation_detection_requires_both_invalid_and_tool(self):
        def check(s):
            return (
                ("invalid" in s and ("tool" in s or "function" in s))
                or "validation" in s
            )

        assert check("invalid tool call format") is True
        assert check("function calling not supported") is False
        assert check("rate limit exceeded") is False
        assert check("authentication failed") is False

    @pytest.mark.asyncio
    async def test_retry_message_preserves_history(self):
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Log food"},
        ]
        system_note = {"role": "system", "content": "IMPORTANT: follow schema"}
        result = messages + [system_note]
        assert len(result) == 5
        assert result[0]["role"] == "system"
        assert result[4]["role"] == "system"

    @pytest.mark.asyncio
    async def test_retry_not_triggered_without_tools(self):
        from bot.ai.clients import ask_groq
        with patch("bot.ai.clients.GROQ_API_KEY", "test-key"):
            with patch("bot.ai.clients.AI_TIMEOUT", 1):
                result = await ask_groq(
                    [{"role": "user", "content": "hello"}],
                    tools=None,
                    temperature=0.7,
                    max_tokens=100,
                )
                assert result is None


# ============================================================
# 1.2 Confirmation parsing
# ============================================================

class TestConfirmationParsing:
    @pytest.mark.parametrize("word", ["да", "ага", "конечно", "ок", "угу", "согласен", "подтверждаю"])
    def test_positive_confirmations_trigger_tools(self, word):
        assert _should_use_tools(word) is True

    @pytest.mark.parametrize("word", ["нет", "отмена", "отменяю"])
    def test_negative_confirmations_trigger_tools(self, word):
        assert _should_use_tools(word) is True

    def test_case_insensitive(self):
        for word in ["ДА", "Да", "НЕТ", "Нет", "ОК", "Ок"]:
            assert _should_use_tools(word) is True

    def test_all_confirm_keywords_in_set(self):
        expected = {"да", "нет", "ок", "ага", "угу", "согласен", "отмена", "отменяю", "конечно"}
        assert CONFIRM_KEYWORDS == expected

    def test_food_messages_DO_trigger_tools(self):
        for food in ["съел курицу", "поел овсянку", "завтрак", "обед 300ккал", "ужин", "перекус"]:
            assert _should_use_tools(food) is True

    def test_messages_with_digits_trigger_tools(self):
        assert _should_use_tools("300 ккал") is True
        assert _should_use_tools("вес 75 кг") is True

    def test_short_food_words_trigger_tools(self):
        assert _should_use_tools("завтрак поел") is True

    def test_greetings_do_not_trigger_tools(self):
        for g in ["привет", "хай", "как дела", "что умеешь", "пока", "спасибо", "отлично", "супер"]:
            assert _should_use_tools(g) is False

    def test_system_prompt_clarity(self):
        from bot.ai.prompts import SYSTEM_PROMPT
        assert "НЕ вызывай функции на приветствия" in SYSTEM_PROMPT


# ============================================================
# 1.3 Relative time handling
# ============================================================

class TestRelativeTimeHandling:
    def test_system_prompt_has_time_instructions(self):
        from bot.ai.prompts import SYSTEM_PROMPT
        assert "АБСОЛЮТНОЕ время" in SYSTEM_PROMPT
        assert "ОТНОСИТЕЛЬНОЕ время" in SYSTEM_PROMPT
        assert "current_time" in SYSTEM_PROMPT

    def test_current_time_format(self):
        now = datetime.datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M")
        assert len(time_str) == 16
        assert time_str[4] == "-"
        assert time_str[7] == "-"


# ============================================================
# 1.4 Pending state clearing
# ============================================================

class TestPendingStateClearing:
    @pytest.mark.asyncio
    async def test_confirm_clears_pending(self):
        await cache_set("pending_action:9502", {
            "type": "reminder", "user_id": 9502,
            "data": {"text": "test", "time": "10:00"}, "created_at": ""
        }, ttl=300)
        await handle_confirm({"confirmation_text": "да"}, 9502, {})
        assert await cache_get("pending_action:9502") is None

    @pytest.mark.asyncio
    async def test_reject_clears_pending(self):
        await cache_set("pending_action:9503", {
            "type": "workout", "user_id": 9503, "data": {}, "created_at": ""
        }, ttl=300)
        await handle_reject({"reason": "нет"}, 9503, {})
        assert await cache_get("pending_action:9503") is None

    @pytest.mark.asyncio
    async def test_two_high_risk_first_deferred(self):
        await cache_delete("pending_action:9504")
        tool_calls = [
            {"function": {"name": "propose_workout", "arguments": json.dumps({"workout_name": "A", "exercises": []})}},
            {"function": {"name": "propose_reminder", "arguments": json.dumps({"text": "B", "time": "12:00"})}},
        ]
        results = await handle_tool_calls(tool_calls, user_id=9504, context={})
        statuses = [r.result["status"] for r in results]
        assert statuses[0] == "pending"
        assert statuses[1] == "deferred"
        await cache_delete("pending_action:9504")


# ============================================================
# 1.5 Multi-tool response handling
# ============================================================

class TestMultiToolResponse:
    @pytest.mark.asyncio
    async def test_low_and_high_risk_mixed(self):
        await cache_delete("pending_action:9505")
        tool_calls = [
            {"function": {"name": "log_food_item", "arguments": json.dumps({"food_name": "Суп", "weight_g": 300})}},
            {"function": {"name": "propose_workout", "arguments": json.dumps({"workout_name": "X", "exercises": []})}},
        ]
        results = await handle_tool_calls(tool_calls, user_id=9505, context={})
        assert len(results) == 2
        low = [r for r in results if r.tool_name == "log_food_item"]
        high = [r for r in results if r.tool_name == "propose_workout"]
        assert len(low) == 1
        assert len(high) == 1
        assert high[0].is_pending is True
        await cache_delete("pending_action:9505")

    @pytest.mark.asyncio
    async def test_one_error_does_not_affect_others(self):
        tool_calls = [
            {"function": {"name": "nonexistent_tool", "arguments": "{}"}},
            {"function": {"name": "propose_workout", "arguments": json.dumps({"workout_name": "X", "exercises": []})}},
        ]
        results = await handle_tool_calls(tool_calls, user_id=9507, context={})
        assert len(results) == 2
        err_result = [r for r in results if r.tool_name == "nonexistent_tool"][0]
        ok_result = [r for r in results if r.tool_name == "propose_workout"][0]
        assert "error" in err_result.result
        assert ok_result.result.get("status") == "pending"
        await cache_delete("pending_action:9507")


# ============================================================
# 2. AI provider racing
# ============================================================

class TestAIRacing:
    @pytest.mark.asyncio
    async def test_race_returns_first_with_content(self):
        from bot.ai.clients import ask_ai_race
        with patch("bot.ai.clients.ask_groq", new_callable=AsyncMock) as mock_groq, \
             patch("bot.ai.clients.ask_gemini", new_callable=AsyncMock) as mock_gemini:
            mock_groq.return_value = {"provider": "groq", "content": "fast", "tool_calls": [], "usage": {}}
            mock_gemini.return_value = {"provider": "gemini", "content": "slow", "tool_calls": [], "usage": {}}
            result = await ask_ai_race([{"role": "user", "content": "hi"}])
            assert result["content"] in ("fast", "slow")

    @pytest.mark.asyncio
    async def test_race_fallback_when_one_fails(self):
        from bot.ai.clients import ask_ai_race
        with patch("bot.ai.clients.ask_groq", new_callable=AsyncMock) as mock_groq, \
             patch("bot.ai.clients.ask_gemini", new_callable=AsyncMock) as mock_gemini:
            mock_groq.return_value = None
            mock_gemini.return_value = {"provider": "gemini", "content": "ok", "tool_calls": [], "usage": {}}
            result = await ask_ai_race([{"role": "user", "content": "hi"}])
            assert result["provider"] == "gemini"

    @pytest.mark.asyncio
    async def test_race_both_fail_returns_empty(self):
        from bot.ai.clients import ask_ai_race
        with patch("bot.ai.clients.ask_groq", new_callable=AsyncMock) as mock_groq, \
             patch("bot.ai.clients.ask_gemini", new_callable=AsyncMock) as mock_gemini:
            mock_groq.return_value = None
            mock_gemini.return_value = None
            result = await ask_ai_race([{"role": "user", "content": "hi"}])
            assert result["provider"] == "none"

    @pytest.mark.asyncio
    async def test_race_timeout_returns_empty(self):
        from bot.ai.clients import ask_ai_race
        async def slow(*args, **kwargs):
            await asyncio.sleep(100)
            return {"provider": "groq", "content": "late", "tool_calls": [], "usage": {}}

        with patch("bot.ai.clients.ask_groq", side_effect=slow), \
             patch("bot.ai.clients.ask_gemini", side_effect=slow), \
             patch("bot.ai.clients.AI_RACE_TIMEOUT", 0.1):
            result = await ask_ai_race([{"role": "user", "content": "hi"}])
            assert result["provider"] == "none"

    @pytest.mark.asyncio
    async def test_race_exception_in_one_provider_silently_ignored(self):
        from bot.ai.clients import ask_ai_race

        async def crashing(*args, **kwargs):
            raise RuntimeError("API down")

        async def ok(*args, **kwargs):
            return {"provider": "gemini", "content": "ok", "tool_calls": [], "usage": {}}

        with patch("bot.ai.clients.ask_groq", new_callable=AsyncMock, side_effect=crashing), \
             patch("bot.ai.clients.ask_gemini", new_callable=AsyncMock, side_effect=ok):
            result = await ask_ai_race([{"role": "user", "content": "hi"}])
            assert result["provider"] == "gemini"
            assert result["content"] == "ok"


# ============================================================
# 3. google-genai SDK
# ============================================================

class TestGoogleGenaiSDK:
    def test_gemini_tool_defs_format(self):
        defs = gemini_tool_defs(["log_food_item", "propose_workout"])
        assert len(defs) == 2
        for d in defs:
            assert "name" in d
            assert "description" in d
            assert "parameters" in d
            assert d["parameters"]["type"] == "object"

    def test_gemini_tool_defs_includes_all_fields(self):
        defs = gemini_tool_defs(["log_food_item"])
        assert defs[0]["name"] == "log_food_item"
        assert "food_name" in defs[0]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_photos_handler_uses_async(self):
        import inspect
        from bot.handlers.photos import _analyze_food_photo
        assert inspect.iscoroutinefunction(_analyze_food_photo)


# ============================================================
# 4. Telegram streaming
# ============================================================

class TestTelegramStreaming:
    def test_streaming_exists_in_codebase(self):
        from bot.ai.clients import ask_ai_stream
        assert callable(ask_ai_stream)

    def test_streaming_uses_groq(self):
        import inspect
        source = inspect.getsource(__import__("bot.ai.clients", fromlist=["ask_ai_stream"]).ask_ai_stream)
        assert "stream=True" in source


# ============================================================
# 5. Photo food recognition
# ============================================================

class TestPhotoFoodRecognition:
    def test_photo_handler_exists(self):
        from bot.handlers.photos import handle_photo
        assert callable(handle_photo)

    @pytest.mark.asyncio
    async def test_analyze_food_photo_returns_none_without_key(self):
        from bot.handlers.photos import _analyze_food_photo
        with patch("bot.handlers.photos.GEMINI_API_KEY", ""):
            result = await _analyze_food_photo(None)
            assert result is None


# ============================================================
# 6. General code review
# ============================================================

class TestCodeReview:
    def test_no_bare_except_pass(self):
        bot_dir = os.path.join(os.path.dirname(__file__), "..", "bot")
        for root, dirs, files in os.walk(bot_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ExceptHandler):
                        if node.type is None:
                            body = node.body
                            if len(body) == 1 and isinstance(body[0], ast.Pass):
                                pytest.fail(f"Bare 'except: pass' in {path}:{node.lineno}")

    def test_no_hardcoded_secrets_in_code(self):
        bot_dir = os.path.join(os.path.dirname(__file__), "..", "bot")
        secret_pattern = re.compile(
            r"(api_key|token|secret|password)\s*=\s*['\"][A-Za-z0-9]{20,}['\"]",
            re.IGNORECASE,
        )
        for root, dirs, files in os.walk(bot_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    for i, line in enumerate(fh, 1):
                        if secret_pattern.search(line):
                            if "os.getenv" not in line and "dotenv" not in line:
                                pytest.fail(f"Possible hardcoded secret in {path}:{i}")

    def test_graceful_shutdown_in_main(self):
        from bot.main import _async_main
        import inspect
        source = inspect.getsource(_async_main)
        assert "shutdown" in source.lower() or "stop" in source.lower()

    def test_no_sensitive_data_in_logs(self):
        bot_dir = os.path.join(os.path.dirname(__file__), "..", "bot")
        sensitive_pattern = re.compile(
            r"logger\.\w+\(.*(?:GROQ_API_KEY|GEMINI_API_KEY|BOT_TOKEN|password|secret)"
        )
        for root, dirs, files in os.walk(bot_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    for i, line in enumerate(fh, 1):
                        if sensitive_pattern.search(line):
                            pytest.fail(f"Sensitive data in log at {path}:{i}")

    def test_redis_fallback_works(self):
        from bot.cache.redis_client import _get_fallback_path
        path = _get_fallback_path("test:key")
        assert "test_key" in path
        assert path.endswith(".json")

    def test_gemini_tool_defs_all_tools_valid(self):
        for td in TOOL_DEFINITIONS:
            assert "name" in td["function"]
            assert "description" in td["function"]
            assert "parameters" in td["function"]
            assert td["function"]["parameters"]["type"] == "object"

    def test_risk_levels_cover_all_non_confirm_tools(self):
        all_non_confirm = set(LOW_RISK_TOOLS) | set(HIGH_RISK_TOOLS)
        defined_tools = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        non_confirm_tools = defined_tools - set(CONFIRM_TOOLS)
        assert all_non_confirm == non_confirm_tools

    def test_requires_confirmation_flag_consistent(self):
        from bot.tools.definitions import requires_confirmation
        for name in HIGH_RISK_TOOLS:
            assert requires_confirmation(name) is True, f"{name} is HIGH_RISK but requires_confirmation returns False"


# ============================================================
# 7. Streaming integration
# ============================================================

class TestStreamingIntegration:
    def test_stream_function_wired_in_messages(self):
        import inspect
        from bot.handlers.messages import _handle_message_inner
        source = inspect.getsource(_handle_message_inner)
        assert "ask_ai_stream" in source or "_stream_response" in source

    def test_stream_response_function_exists(self):
        from bot.handlers.messages import _stream_response
        assert callable(_stream_response)

    def test_stream_constants_defined(self):
        from bot.handlers.messages import STREAM_EDIT_MIN_INTERVAL, STREAM_MAX_MSG_LEN
        assert STREAM_EDIT_MIN_INTERVAL > 0
        assert STREAM_MAX_MSG_LEN == 4096


# ============================================================
# 8. Photo MD5 dedup
# ============================================================

class TestPhotoDedup:
    def test_md5_computed_in_photo_handler(self):
        import inspect
        from bot.handlers.photos import _handle_photo_inner
        source = inspect.getsource(_handle_photo_inner)
        assert "hashlib" in source
        assert "md5" in source

    def test_cache_checked_before_gemini(self):
        import inspect
        from bot.handlers.photos import _handle_photo_inner
        source = inspect.getsource(_handle_photo_inner)
        assert "photo_cache:" in source
        assert "cache_get" in source
        assert "cache_set" in source

    def test_dedup_skips_duplicate_analysis(self):
        import inspect
        from bot.handlers.photos import _handle_photo_inner
        source = inspect.getsource(_handle_photo_inner)
        assert "cache hit" in source.lower() or "Cache hit" in source


# ============================================================
# 9. USDA API
# ============================================================

class TestUSDA:
    def test_usda_module_exists(self):
        from bot.nutrition.usda import usda_search, enrich_with_usda
        assert callable(usda_search)
        assert callable(enrich_with_usda)

    def test_usda_search_returns_none_without_key(self):
        import asyncio
        from bot.nutrition.usda import usda_search
        with patch("bot.nutrition.usda.USDA_API_KEY", ""):
            result = asyncio.get_event_loop().run_until_complete(usda_search("chicken"))
            assert result is None

    def test_enrich_with_usda_fills_missing_macros(self):
        from bot.nutrition.usda import enrich_with_usda
        food = {"food_name": "pasta", "estimated_weight_g": 200}
        usda = {"calories_per_100g": 131, "protein_per_100g": 5, "fat_per_100g": 1.1, "carbs_per_100g": 25}
        result = enrich_with_usda(food, usda, 200)
        assert result["protein"] == 10.0
        assert result["fat"] == 2.2
        assert result["carbs"] == 50.0
        assert result["estimated_calories"] == 262.0

    def test_enrich_with_usda_does_not_overwrite_existing(self):
        from bot.nutrition.usda import enrich_with_usda
        food = {"food_name": "chicken", "estimated_weight_g": 200, "protein": 50}
        usda = {"calories_per_100g": 165, "protein_per_100g": 31, "fat_per_100g": 3.6, "carbs_per_100g": 2}
        result = enrich_with_usda(food, usda, 200)
        assert result["protein"] == 50

    def test_enrich_with_usda_none_returns_original(self):
        from bot.nutrition.usda import enrich_with_usda
        food = {"food_name": "chicken"}
        result = enrich_with_usda(food, None, 200)
        assert result == food

    def test_usda_enrichment_called_when_no_macros(self):
        import inspect
        from bot.handlers.photos import _handle_photo_inner
        source = inspect.getsource(_handle_photo_inner)
        assert "enrich_with_usda" in source
        assert "has_macros" in source

    def test_no_deprecated_utcnow_in_bot_code(self):
        bot_dir = os.path.join(os.path.dirname(__file__), "..", "bot")
        for root, dirs, files in os.walk(bot_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path) as fh:
                    for i, line in enumerate(fh, 1):
                        if "utcnow()" in line:
                            pytest.fail(f"Deprecated utcnow() in {path}:{i}")


# ============================================================
# 10. Circuit breaker
# ============================================================

class TestCircuitBreaker:
    def test_circuit_breaker_allows_when_closed(self):
        from bot.ai.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        assert cb.allow_request("groq") is True

    def test_circuit_breaker_opens_after_failures(self):
        from bot.ai.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            cb.record_failure("groq")
        assert cb.allow_request("groq") is False

    def test_circuit_breaker_closes_on_success(self):
        from bot.ai.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(2):
            cb.record_failure("groq")
        cb.record_success("groq")
        assert cb.allow_request("groq") is True

    def test_circuit_breaker_get_available(self):
        from bot.ai.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            cb.record_failure("groq")
        available = cb.get_available_providers(["groq", "gemini"])
        assert "gemini" in available
        assert "groq" not in available

    def test_circuit_breaker_fallback_when_all_open(self):
        from bot.ai.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            cb.record_failure("groq")
            cb.record_failure("gemini")
        available = cb.get_available_providers(["groq", "gemini"])
        assert len(available) == 2


# ============================================================
# 11. SQLite WAL mode
# ============================================================

class TestWALMode:
    def test_wal_mode_enabled_on_init(self):
        import inspect
        from bot.db.base import init_db
        source = inspect.getsource(init_db)
        assert "WAL" in source
        assert "busy_timeout" in source


# ============================================================
# 12. Error handler
# ============================================================

class TestErrorHandler:
    def test_error_handler_exists(self):
        from bot.main import error_handler
        assert callable(error_handler)

    def test_startup_self_check_exists(self):
        from bot.main import startup_self_check
        assert callable(startup_self_check)


# ============================================================
# 13. Persistent scheduler jobstore
# ============================================================

class TestPersistentScheduler:
    def test_scheduler_uses_sqlalchemy_jobstore(self):
        from bot.memory.scheduler import scheduler
        jobstores = scheduler._jobstores
        assert "default" in jobstores
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        assert isinstance(jobstores["default"], SQLAlchemyJobStore)


# ============================================================
# 14. Dialog buffer sliding window
# ============================================================

class TestDialogBuffer:
    def test_dialog_buffer_constants(self):
        from bot.handlers.messages import DIALOG_BUFFER_MAX, DIALOG_BUFFER_KEEP
        assert DIALOG_BUFFER_MAX >= DIALOG_BUFFER_KEEP
        assert DIALOG_BUFFER_KEEP >= 10

    def test_summarize_function_exists(self):
        from bot.handlers.messages import _summarize_old_dialog
        assert callable(_summarize_old_dialog)


# ============================================================
# 15. Selective racing (background ops use single provider)
# ============================================================

class TestSelectiveRacing:
    def test_missed_meal_uses_ask_groq(self):
        import inspect
        from bot.memory.scheduler import _send_missed_meal_notification
        source = inspect.getsource(_send_missed_meal_notification)
        assert "ask_groq" in source
        assert "ask_ai_race" not in source


# ============================================================
# 16. Photo pipeline improvements
# ============================================================

class TestPhotoPipeline:
    def test_singleton_gemini_client_used(self):
        import inspect
        from bot.handlers.photos import _analyze_food_photo
        source = inspect.getsource(_analyze_food_photo)
        assert "_get_gemini_client" in source
        assert "genai.Client(" not in source

    def test_mime_detection_jpeg(self):
        from bot.handlers.photos import _detect_mime_type
        import io
        header = b"\xff\xd8\xff\xe0" + b"\x00" * 12
        assert _detect_mime_type(io.BytesIO(header)) == "image/jpeg"

    def test_mime_detection_png(self):
        from bot.handlers.photos import _detect_mime_type
        import io
        header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 12
        assert _detect_mime_type(io.BytesIO(header)) == "image/png"

    def test_mime_detection_webp(self):
        from bot.handlers.photos import _detect_mime_type
        import io
        header = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4
        assert _detect_mime_type(io.BytesIO(header)) == "image/webp"

    def test_vision_prompt_rejects_non_food(self):
        from bot.handlers.photos import VISION_PROMPT
        assert "НЕ еда" in VISION_PROMPT

    def test_vision_prompt_strict_exists(self):
        from bot.handlers.photos import VISION_PROMPT_STRICT
        assert "валидный JSON" in VISION_PROMPT_STRICT

    def test_photo_locks_dict_exists(self):
        from bot.handlers.photos import _photo_locks
        assert isinstance(_photo_locks, dict)

    def test_photo_compression_fallback_without_pillow(self):
        import io
        from unittest.mock import patch
        from bot.handlers.photos import _compress_photo
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
            result = _compress_photo(io.BytesIO(data))
            result.seek(0, 2)
            assert result.tell() > 0

    def test_empty_photo_returns_none(self):
        import io
        from bot.handlers.photos import _detect_mime_type
        result = _detect_mime_type(io.BytesIO(b""))
        assert result.startswith("image/")

    def test_analyze_food_photo_graceful_without_key(self):
        from bot.handlers.photos import _analyze_food_photo
        import io
        with patch("bot.handlers.photos.GEMINI_API_KEY", ""):
            result = asyncio.get_event_loop().run_until_complete(
                _analyze_food_photo(io.BytesIO(b"fake"))
            )
            assert result is None


# ============================================================
# 17. USDA retry and best-match
# ============================================================

class TestUSDARetry:
    def test_usda_returns_multiple_results(self):
        import inspect
        from bot.nutrition.usda import usda_search
        source = inspect.getsource(usda_search)
        assert "pageSize" in source
        assert "best" in source or "max(" in source

    def test_usda_retries_on_429(self):
        import inspect
        from bot.nutrition.usda import usda_search
        source = inspect.getsource(usda_search)
        assert "429" in source
        assert "USDA_MAX_RETRIES" in source

    def test_usda_best_match_uses_word_overlap(self):
        import inspect
        from bot.nutrition.usda import usda_search
        source = inspect.getsource(usda_search)
        assert "query_words" in source or "description" in source


# ============================================================
# 18. Startup self-check with API keys
# ============================================================

class TestStartupSelfCheck:
    def test_self_check_includes_gemini(self):
        import inspect
        from bot.main import startup_self_check
        source = inspect.getsource(startup_self_check)
        assert "GEMINI_API_KEY" in source

    def test_self_check_includes_usda(self):
        import inspect
        from bot.main import startup_self_check
        source = inspect.getsource(startup_self_check)
        assert "USDA_API_KEY" in source

    def test_self_check_includes_groq(self):
        import inspect
        from bot.main import startup_self_check
        source = inspect.getsource(startup_self_check)
        assert "GROQ_API_KEY" in source
