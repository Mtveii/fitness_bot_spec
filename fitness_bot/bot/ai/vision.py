import os
import io
import json
import hashlib
import logging
import asyncio
import google.generativeai as genai
from bot.cache.redis_client import get_photo_cache, set_photo_cache

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


async def analyze_photo(photo_bytes: bytes) -> dict | None:
    """
    Распознавание еды по фото через Gemini 2.0 Flash.
    Возвращает {food_name, weight_g, calories, protein, fat, carbs} или None.
    """
    photo_hash = hashlib.md5(photo_bytes).hexdigest()

    cached = await get_photo_cache(photo_hash)
    if cached:
        return cached

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set, skipping photo analysis")
        return None

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        prompt = (
            "Распознай еду на фото. Верни ТОЛЬКО JSON без markdown:\n"
            '{"food_name": "название", "weight_g": число, "calories": число, '
            '"protein": число, "fat": число, "carbs": число}\n'
            "Вес — примерная оценка в граммах. КБЖУ — на всю порцию."
        )

        import PIL.Image
        img = PIL.Image.open(io.BytesIO(photo_bytes))

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content([prompt, img])
        )

        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(text)

        required_keys = {"food_name", "weight_g", "calories", "protein", "fat", "carbs"}
        if not required_keys.issubset(result.keys()):
            logger.warning(f"Invalid Gemini response keys: {result.keys()}")
            return None

        await set_photo_cache(photo_hash, result)
        return result

    except Exception as e:
        logger.error(f"Gemini photo analysis failed: {e}")
        # Groq не поддерживает vision — фоллбэк только для текста
        return None
