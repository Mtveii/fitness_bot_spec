import os
import io
import json
import hashlib
import logging
from bot.cache.redis_client import get_photo_cache, set_photo_cache
from bot.ai.clients import get_gemini_client, GEMINI_API_KEY, GEMINI_MODEL, GEMINI_TIMEOUT

logger = logging.getLogger(__name__)


async def analyze_photo(photo_bytes: bytes) -> dict | None:
    """
    Распознавание еды по фото через Gemini 2.0 Flash (нативный async, новый SDK).
    Уточняет КБЖУ через USDA по распознанному названию, если найдётся точное совпадение.
    Возвращает {food_name, weight_g, calories, protein, fat, carbs, source} или None.
    """
    photo_hash = hashlib.md5(photo_bytes).hexdigest()

    cached = await get_photo_cache(photo_hash)
    if cached:
        return cached

    client = get_gemini_client()
    if not client:
        logger.warning("GEMINI_API_KEY not set, skipping photo analysis")
        return None

    try:
        from google.genai import types
        import PIL.Image

        prompt = (
            "Распознай еду на фото. Верни ТОЛЬКО JSON без markdown:\n"
            '{"food_name": "название", "weight_g": число, "calories": число, '
            '"protein": число, "fat": число, "carbs": число}\n'
            "Вес — примерная оценка в граммах. КБЖУ — на всю порцию."
        )

        img = PIL.Image.open(io.BytesIO(photo_bytes))

        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt, img],
            config=types.GenerateContentConfig(
                temperature=0.3,
                httpOptions=types.HttpOptions(timeout=int(GEMINI_TIMEOUT * 1000)),
            ),
        )

        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(text)

        required_keys = {"food_name", "weight_g", "calories", "protein", "fat", "carbs"}
        if not required_keys.issubset(result.keys()):
            logger.warning(f"Invalid Gemini response keys: {result.keys()}")
            return None

        # Уточнение через USDA по распознанному названию (если найдётся точное совпадение)
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
            logger.warning(f"USDA refine skipped: {e}")
            result["source"] = "ai_estimate"

        await set_photo_cache(photo_hash, result)
        return result

    except Exception as e:
        logger.error(f"Gemini photo analysis failed: {e}")
        return None