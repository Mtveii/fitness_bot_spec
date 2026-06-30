import logging
import datetime
import io
import json
import hashlib

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.base import async_session
from bot.db.models import User, MealLog
from bot.config import GEMINI_API_KEY
from bot.cache.redis_client import cache_get, cache_set
from bot.nutrition.usda import usda_search, enrich_with_usda

logger = logging.getLogger(__name__)

PHOTO_CACHE_TTL = 86400 * 7


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()

    if not user:
        await update.message.reply_text("Сначала напиши /start, чтобы я тебя узнал.")
        return

    await update.message.reply_text("Анализирую фото...")

    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = io.BytesIO()
    await photo_file.download_to_memory(photo_bytes)
    photo_bytes.seek(0)

    photo_bytes.seek(0)
    photo_md5 = hashlib.md5(photo_bytes.read()).hexdigest()
    photo_bytes.seek(0)

    cache_key = f"photo_cache:{photo_md5}"
    food_info = await cache_get(cache_key)

    if food_info and food_info.get("food_name"):
        logger.info(f"Photo cache hit: {photo_md5[:8]}")
    else:
        food_info = await _analyze_food_photo(photo_bytes)
        if food_info and food_info.get("food_name"):
            await cache_set(cache_key, food_info, ttl=PHOTO_CACHE_TTL)

    if food_info and food_info.get("food_name"):
        has_macros = food_info.get("protein") or food_info.get("fat") or food_info.get("carbs")
        if not has_macros and USDA_API_KEY:
            usda_data = await usda_search(food_info["food_name"])
            if usda_data:
                weight = food_info.get("estimated_weight_g", 200)
                food_info = enrich_with_usda(food_info, usda_data, weight)

    if not food_info or not food_info.get("food_name"):
        await update.message.reply_text(
            "Не удалось распознать еду на фото. Попробуй написать текстом, что ты съел."
        )
        return

    food_name = food_info["food_name"]
    estimated_calories = food_info.get("estimated_calories", 0)
    estimated_weight = food_info.get("estimated_weight_g", 200)
    protein = food_info.get("protein", None)
    fat = food_info.get("fat", None)
    carbs = food_info.get("carbs", None)

    async with async_session() as session:
        meal = MealLog(
            user_id=user.id,
            date=datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
            food_name=food_name,
            weight_g=estimated_weight,
            calories=estimated_calories,
            protein=protein,
            fat=fat,
            carbs=carbs,
            source="photo",
        )
        session.add(meal)
        await session.commit()

    macros_parts = []
    if protein is not None:
        macros_parts.append(f"белки: {protein:.1f}г")
    if fat is not None:
        macros_parts.append(f"жиры: {fat:.1f}г")
    if carbs is not None:
        macros_parts.append(f"углеводы: {carbs:.1f}г")
    macros_str = ", ".join(macros_parts)

    await update.message.reply_text(
        f"Распознано: {food_name}\n"
        f"~{estimated_weight}г, ~{estimated_calories:.0f} ккал\n"
        f"{macros_str}\n\n"
        f"Если данные не точные — просто напиши текстом правку."
    )


async def _analyze_food_photo(photo_bytes: io.BytesIO) -> dict:
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            "Что за еда на этом фото? Ответь ТОЛЬКО в JSON формате:\n"
            '{"food_name": "...", "estimated_calories": число, '
            '"estimated_weight_g": число, "protein": число или null, '
            '"fat": число или null, "carbs": число или null}\n'
            "Если не можешь распознать — верни {\"food_name\": null}"
        )

        import asyncio
        from functools import partial

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            partial(
                client.models.generate_content,
                model="gemini-2.0-flash",
                contents=[prompt, types.Part.from_bytes(
                    data=photo_bytes.getvalue(),
                    mime_type="image/jpeg"
                )],
            )
        )
        text = response.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        logger.warning(f"Photo analysis failed: {e}")
        return None
