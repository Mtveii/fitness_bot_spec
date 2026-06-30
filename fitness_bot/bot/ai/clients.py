import asyncio
import logging
from typing import Optional, Callable

from bot.config import (
    GROQ_API_KEY, GEMINI_API_KEY, GROQ_MODEL, GEMINI_MODEL,
    AI_TIMEOUT, AI_RACE_TIMEOUT, STREAM_TIMEOUT
)
from bot.ai.circuit_breaker import circuit_breaker

logger = logging.getLogger(__name__)

_groq_client = None
_gemini_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import AsyncGroq
        _groq_client = AsyncGroq(api_key=GROQ_API_KEY)
    return _groq_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


async def ask_groq(messages: list, tools: Optional[list] = None,
                   temperature: float = 0.7, max_tokens: int = 1024) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    for attempt in range(2):
        try:
            client = _get_groq_client()
            kwargs = {
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            resp = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=AI_TIMEOUT
            )
            choice = resp.choices[0]
            return {
                "provider": "groq",
                "content": choice.message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    }
                    for tc in (choice.message.tool_calls or [])
                ],
                "usage": {
                    "input_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                    "output_tokens": resp.usage.completion_tokens if resp.usage else 0,
                }
            }
        except asyncio.TimeoutError:
            logger.warning("Groq timeout")
            circuit_breaker.record_failure("groq")
            return None
        except Exception as e:
            err_str = str(e).lower()
            is_validation = (
                "invalid" in err_str and ("tool" in err_str or "function" in err_str)
            ) or "validation" in err_str
            if is_validation and attempt == 0 and tools:
                logger.warning(f"Groq validation error on attempt 1, retrying with stricter prompt: {e}")
                system_note = {"role": "system", "content": "ВАЖНО: Строго следуй JSON Schema для tool calls. Не добавляй лишних полей. Arguments должны быть валидным JSON."}
                messages = messages + [system_note]
                continue
            logger.warning(f"Groq error: {e}")
            circuit_breaker.record_failure("groq")
            return None
    circuit_breaker.record_success("groq")


async def ask_gemini(messages: list, tools: Optional[list] = None,
                     temperature: float = 0.7, max_tokens: int = 1024) -> Optional[dict]:
    if not GEMINI_API_KEY:
        return None
    try:
        from google.genai import types
        client = _get_gemini_client()
        system_msg = ""
        contents = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                system_msg = m.get("content", "")
                continue
            contents.append(types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=m.get("content", ""))]
            ))
        config_kwargs = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_msg:
            config_kwargs["system_instruction"] = system_msg
        if tools:
            from bot.tools.definitions import gemini_tool_defs
            config_kwargs["tools"] = gemini_tool_defs(tools)

        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            ),
            timeout=AI_TIMEOUT
        )
        tool_calls = []
        if resp.candidates:
            cand = resp.candidates[0]
            if cand.content and cand.content.parts:
                for i, part in enumerate(cand.content.parts):
                    if part.function_call:
                        tc = part.function_call
                        import json
                        args = json.dumps({k: v for k, v in tc.args.items()})
                        tool_calls.append({
                            "id": f"gemini_{tc.name}_{i}",
                            "function": {"name": tc.name, "arguments": args}
                        })
        circuit_breaker.record_success("gemini")
        return {
            "provider": "gemini",
            "content": resp.text if hasattr(resp, 'text') else "",
            "tool_calls": tool_calls,
            "usage": {
                "input_tokens": resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0,
                "output_tokens": resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0,
            }
        }
    except asyncio.TimeoutError:
        logger.warning("Gemini timeout")
        circuit_breaker.record_failure("gemini")
        return None
    except Exception as e:
        logger.warning(f"Gemini error: {e}")
        circuit_breaker.record_failure("gemini")
        return None


async def ask_ai_race(messages: list, tools: Optional[list] = None,
                      temperature: float = 0.7, max_tokens: int = 1024) -> dict:
    available = circuit_breaker.get_available_providers(["groq", "gemini"])

    async def _with_name(coro, name):
        return name, await coro

    tasks = []
    if "groq" in available:
        tasks.append(
            asyncio.create_task(_with_name(ask_groq(messages, tools, temperature, max_tokens), "groq"))
        )
    if "gemini" in available:
        tasks.append(
            asyncio.create_task(_with_name(ask_gemini(messages, tools, temperature, max_tokens), "gemini"))
        )
    if not tasks:
        tasks = [
            asyncio.create_task(_with_name(ask_groq(messages, tools, temperature, max_tokens), "groq")),
        ]

    done, pending = await asyncio.wait(tasks, timeout=AI_RACE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    best = None
    for task in done:
        if task.cancelled():
            continue
        try:
            name, result = task.result()
        except Exception:
            continue
        if result and isinstance(result, dict) and result.get("content"):
            best = result
            break

    if best:
        return best

    for task in done:
        if task.cancelled():
            continue
        try:
            name, result = task.result()
        except Exception:
            continue
        if result and isinstance(result, dict):
            return result

    return {"provider": "none", "content": "", "tool_calls": [], "usage": {}}


async def ask_ai_stream(messages: list, on_token: Callable[[str], None],
                        temperature: float = 0.7, max_tokens: int = 1024):
    if not GROQ_API_KEY:
        return
    try:
        client = _get_groq_client()
        stream = await asyncio.wait_for(
            client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            ),
            timeout=STREAM_TIMEOUT,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else ""
            if delta:
                await on_token(delta)
    except asyncio.TimeoutError:
        logger.warning("Stream timeout")
    except Exception as e:
        logger.warning(f"Stream error: {e}")
