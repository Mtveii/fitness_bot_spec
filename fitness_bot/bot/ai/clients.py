"""
Единая точка инициализации Groq/Gemini клиентов + гонка провайдеров.
Gemini переведён на новый SDK google-genai (нативный async, без ThreadPoolExecutor).
Цель: ответ за 3-4 секунды.
"""
import os
import asyncio
import logging
import httpx

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GROQ_MODEL = "llama-3.1-8b-instant"
GEMINI_MODEL = "gemini-2.0-flash"

# Агрессивные таймауты под цель 3-4с: если провайдер не ответил за это время —
# не ждём, берём то что успело прийти от другого (или None)
GROQ_TIMEOUT = float(os.getenv("AI_GROQ_TIMEOUT", "3.5"))
GEMINI_TIMEOUT = float(os.getenv("AI_GEMINI_TIMEOUT", "10.0"))

_groq_client = None
_gemini_client = None
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Singleton HTTP-клиент с keep-alive — избегает нового TCP+TLS на каждый вызов."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _http_client


def get_groq_client():
    global _groq_client
    if _groq_client is None and GROQ_API_KEY:
        import groq
        _groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY, timeout=GROQ_TIMEOUT)
    return _groq_client


def get_gemini_client():
    """Новый SDK google-genai — единый клиент, нативный async через client.aio."""
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        from google import genai
        from google.genai import types
        _gemini_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),  # мс
        )
    return _gemini_client


async def _ask_groq(system: str, user_text: str, max_tokens: int):
    """Returns (text, provider, tok_in, tok_out) or None."""
    client = get_groq_client()
    if not client:
        return None
    try:
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        usage = response.usage
        return (
            response.choices[0].message.content.strip(),
            "groq",
            usage.prompt_tokens if usage else 0,
            usage.completion_tokens if usage else 0,
        )
    except Exception as e:
        logger.warning(f"Groq failed: {e}")
        return None


async def _ask_gemini(system: str, user_text: str, max_tokens: int):
    """Returns (text, provider, tok_in, tok_out) or None. Нативный async, без executor."""
    client = get_gemini_client()
    if not client:
        return None
    try:
        from google.genai import types
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.3,
                max_output_tokens=max_tokens,
                http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),
            ),
        )
        text = response.text.strip()
        usage = getattr(response, "usage_metadata", None)
        tok_in = getattr(usage, "prompt_token_count", 0) or 0
        tok_out = getattr(usage, "candidates_token_count", 0) or 0
        return text, "gemini", tok_in, tok_out
    except Exception as e:
        logger.warning(f"Gemini failed: {e}")
        return None


async def ask_ai_race(system: str, user_text: str, max_tokens: int = 150):
    """
    Запускает Groq и Gemini ОДНОВРЕМЕННО.
    Возвращает (text, provider, tok_in, tok_out) или None.
    Максимальное время ожидания = max(GROQ_TIMEOUT, GEMINI_TIMEOUT), не сумма.
    """
    tasks = {}
    if GROQ_API_KEY:
        tasks["groq"] = asyncio.create_task(
            asyncio.wait_for(_ask_groq(system, user_text, max_tokens), timeout=GROQ_TIMEOUT)
        )
    if GEMINI_API_KEY:
        tasks["gemini"] = asyncio.create_task(
            asyncio.wait_for(_ask_gemini(system, user_text, max_tokens), timeout=GEMINI_TIMEOUT)
        )

    if not tasks:
        return None

    pending = set(tasks.values())

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                value = task.result()
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"AI provider error/timeout: {e}")
                value = None
            if value:
                for p in pending:
                    p.cancel()
                return value
        if not pending:
            return None


async def ask_groq_with_tools(system: str, user_text: str, tools: list, max_tokens: int = 300, _retry: bool = False):
    """
    Groq с function calling.
    Возвращает ('text', текст, tok_in, tok_out) или ('tool_call', {name, arguments}, tok_in, tok_out).
    Возвращает None при ошибке.
    При tool_use_failed делает один ретрай с усиленным напоминанием о JSON Schema.
    """
    client = get_groq_client()
    if not client:
        return None
    try:
        import json as _json
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=0.3,
        )
        usage = response.usage
        message = response.choices[0].message
        tok_in = usage.prompt_tokens if usage else 0
        tok_out = usage.completion_tokens if usage else 0

        if message.tool_calls:
            calls = []
            for call in message.tool_calls:
                args = _json.loads(call.function.arguments)
                calls.append({"name": call.function.name, "arguments": args})
            return ("tool_calls", calls, tok_in, tok_out)

        return ("text", message.content.strip(), tok_in, tok_out)
    except Exception as e:
        err_str = str(e)
        if "tool_use_failed" in err_str and not _retry:
            logger.warning(f"Groq tool_use_failed, retrying once: {e}")
            retry_system = system + "\n\nВАЖНО: при вызове функции строго следуй JSON Schema, не добавляй лишние поля."
            return await ask_groq_with_tools(retry_system, user_text, tools, max_tokens, _retry=True)
        logger.warning(f"Groq tools call failed: {e}")
        return None


async def ask_gemini_with_tools(system: str, user_text: str, tools: list, max_tokens: int = 300):
    """
    Gemini с function calling (новый google-genai SDK).
    Возвращает ('text', текст, tok_in, tok_out) или ('tool_calls', [{name, arguments}], tok_in, tok_out).
    Возвращает None при ошибке.
    """
    client = get_gemini_client()
    if not client:
        return None
    try:
        from google.genai import types

        gemini_tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["function"]["name"],
                    description=t["function"]["description"],
                    parameters=t["function"]["parameters"],
                )
                for t in tools
            ])
        ]

        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=gemini_tools,
                temperature=0.3,
                max_output_tokens=max_tokens,
                http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),
            ),
        )

        candidate = response.candidates[0]
        usage = getattr(response, "usage_metadata", None)
        tok_in = getattr(usage, "prompt_token_count", 0) or 0
        tok_out = getattr(usage, "candidates_token_count", 0) or 0

        calls = []
        for part in candidate.content.parts:
            if part.function_call:
                calls.append({
                    "name": part.function_call.name,
                    "arguments": dict(part.function_call.args),
                })

        if calls:
            return ("tool_calls", calls, tok_in, tok_out)

        text = response.text.strip()
        return ("text", text, tok_in, tok_out)
    except Exception as e:
        logger.warning(f"Gemini tools call failed: {e}")
        return None