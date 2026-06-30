import asyncio
import logging
from typing import Optional, Callable

from bot.config import (
    GROQ_API_KEY, GEMINI_API_KEY, GROQ_MODEL, GEMINI_MODEL,
    AI_TIMEOUT, AI_RACE_TIMEOUT, STREAM_TIMEOUT
)

logger = logging.getLogger(__name__)


async def ask_groq(messages: list, tools: Optional[list] = None,
                   temperature: float = 0.7, max_tokens: int = 1024) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=GROQ_API_KEY)
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
        return None
    except Exception as e:
        logger.warning(f"Groq error: {e}")
        return None


async def ask_gemini(messages: list, tools: Optional[list] = None,
                     temperature: float = 0.7, max_tokens: int = 1024) -> Optional[dict]:
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GEMINI_API_KEY)
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
                for part in cand.content.parts:
                    if part.function_call:
                        tc = part.function_call
                        import json
                        args = json.dumps({k: v for k, v in tc.args.items()})
                        tool_calls.append({
                            "id": tc.name,
                            "function": {"name": tc.name, "arguments": args}
                        })
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
        return None
    except Exception as e:
        logger.warning(f"Gemini error: {e}")
        return None


async def ask_ai_race(messages: list, tools: Optional[list] = None,
                      temperature: float = 0.7, max_tokens: int = 1024) -> dict:
    results = await asyncio.gather(
        ask_groq(messages, tools, temperature, max_tokens),
        ask_gemini(messages, tools, temperature, max_tokens),
        return_exceptions=True
    )
    for r in results:
        if r and not isinstance(r, Exception) and r.get("content"):
            return r
    for r in results:
        if r and not isinstance(r, Exception):
            return r
    return {"provider": "none", "content": "", "tool_calls": [], "usage": {}}


async def ask_ai_stream(messages: list, on_token: Callable[[str], None],
                        temperature: float = 0.7, max_tokens: int = 1024):
    if not GROQ_API_KEY:
        return
    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=GROQ_API_KEY)
        stream = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else ""
            if delta:
                await on_token(delta)
    except Exception as e:
        logger.warning(f"Stream error: {e}")
