"""
Единая точка инициализации Groq/Gemini клиентов + гонка провайдеров.
P2.12 — system/user split для prompt caching.
P3.13 — возврат токенов (text, provider, tok_in, tok_out).
"""
import os
import asyncio
import logging

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GROQ_MODEL = "llama-3.3-70b-versatile"
GEMINI_MODEL = "gemini-2.0-flash"

GROQ_TIMEOUT = float(os.getenv("AI_GROQ_TIMEOUT", "6.0"))
GEMINI_TIMEOUT = float(os.getenv("AI_GEMINI_TIMEOUT", "8.0"))

_groq_client = None
_gemini_configured = False
_gemini_models_cache: dict[str, object] = {}


def get_groq_client():
    global _groq_client
    if _groq_client is None and GROQ_API_KEY:
        import groq
        _groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY, timeout=GROQ_TIMEOUT)
    return _groq_client


def get_gemini_model(model_name: str = GEMINI_MODEL):
    global _gemini_configured
    import google.generativeai as genai
    if not _gemini_configured and GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_configured = True
    return genai.GenerativeModel(model_name)


def get_gemini_model_with_system(system: str, model_name: str = GEMINI_MODEL):
    global _gemini_configured
    import google.generativeai as genai
    if not _gemini_configured and GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        _gemini_configured = True
    key = str(hash(system))
    if key not in _gemini_models_cache:
        _gemini_models_cache[key] = genai.GenerativeModel(
            model_name, system_instruction=system
        )
    return _gemini_models_cache[key]


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


async def _ask_gemini(system: str, user_text: str):
    """Returns (text, provider, tok_in, tok_out) or None."""
    if not GEMINI_API_KEY:
        return None
    try:
        model = get_gemini_model_with_system(system)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, lambda: model.generate_content(user_text)
        )
        text = response.text.strip()
        tok_in = getattr(getattr(response, "usage_metadata", None), "prompt_token_count", 0) or 0
        tok_out = getattr(getattr(response, "usage_metadata", None), "candidates_token_count", 0) or 0
        return text, "gemini", tok_in, tok_out
    except Exception as e:
        logger.warning(f"Gemini failed: {e}")
        return None


async def ask_ai_race(system: str, user_text: str, max_tokens: int = 350):
    """
    Запускает Groq и Gemini ОДНОВРЕМЕННО.
    Возвращает (text, provider, tok_in, tok_out) или None.
    """
    tasks = {}
    if GROQ_API_KEY:
        tasks["groq"] = asyncio.create_task(
            asyncio.wait_for(_ask_groq(system, user_text, max_tokens), timeout=GROQ_TIMEOUT)
        )
    if GEMINI_API_KEY:
        tasks["gemini"] = asyncio.create_task(
            asyncio.wait_for(_ask_gemini(system, user_text), timeout=GEMINI_TIMEOUT)
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

    return None
