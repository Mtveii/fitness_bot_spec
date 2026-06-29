import os
import io
import json
import asyncio
import hashlib
import logging
from bot.cache.redis_client import get_photo_cache, set_photo_cache
from bot.ai.clients import get_gemini_client, GEMINI_API_KEY, GEMINI_MODEL, GEMINI_TIMEOUT

logger = logging.getLogger(__name__)

VISION_TIMEOUT = float(os.getenv("AI_VISION_TIMEOUT", "15.0"))


async def analyze_photo(photo_bytes: bytes) -> dict | None:
    photo_hash = hashlib.md5(photo_bytes).hexdigest()
    logger.info(f"[VISION] start | {len(photo_bytes)} bytes | hash={photo_hash[:8]}")

    cached = await get_photo_cache(photo_hash)
    if cached:
        logger.info("[VISION] cache hit")
        return cached

    client = get_gemini_client()
    if not client:
        logger.warning("[VISION] GEMINI_API_KEY not set")
        return None

    try:
        from google.genai import types
        import PIL.Image
    except ImportError as e:
        logger.error(f"[VISION] import failed: {e}")
        return None

    try:
        img = PIL.Image.open(io.BytesIO(photo_bytes))
        logger.info(f"[VISION] PIL image opened: {img.size} {img.mode}")
    except Exception as e:
        logger.error(f"[VISION] PIL.Image.open failed: {e}")
        return None

    prompt = (
        "Распознай еду на фото. Верни ТОЛЬКО JSON без markdown:\n"
        '{"food_name": "название", "weight_g": число, "calories": число, '
        '"protein": число, "fat": число, "carbs": число}\n'
        "Вес — примерная оценка в граммах. КБЖУ — на всю порцию."
    )

    logger.info(f"[VISION] calling Gemini (timeout={VISION_TIMEOUT}s)...")
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=[prompt, img],
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),
                ),
            ),
            timeout=VISION_TIMEOUT,
        )
        logger.info(f"[VISION] Gemini responded")
    except asyncio.TimeoutError:
        logger.error(f"[VISION] Gemini timeout after {VISION_TIMEOUT}s")
        return None
    except Exception as e:
        logger.error(f"[VISION] Gemini call failed: {type(e).__name__}: {e}")
        return None

    try:
        text = response.text.strip()
        logger.info(f"[VISION] raw response: {text[:300]}")
    except Exception as e:
        logger.error(f"[VISION] response.text failed: {e}")
        return None

    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"[VISION] JSON parse failed: {e} | text={text[:200]}")
        return None

    required_keys = {"food_name", "weight_g", "calories", "protein", "fat", "carbs"}
    if not required_keys.issubset(result.keys()):
        logger.warning(f"[VISION] missing keys: {required_keys - result.keys()}")
        return None

    try:
        from bot.handlers.food import search_food_usda
        usda_match = await search_food_usda(result["food_name"])
        if usda_match:
            scale = result["weight_g"] / 100.0
            result["calories"] = usda_match["calories"] * scale
            result["protein"] = usda_match["protein"] * scale
            result["fat"] = usda_match["fat"] * scale
            result["carbs"] = usda_match["carbs"] * scale
            result["source"] = "usda_refined"
        else:
            result["source"] = "ai_estimate"
    except Exception as e:
        logger.warning(f"[VISION] USDA refine skipped: {e}")
        result["source"] = "ai_estimate"

    await set_photo_cache(photo_hash, result)
    logger.info(f"[VISION] done: {result['food_name']} ~{result['weight_g']}g")
    return result
