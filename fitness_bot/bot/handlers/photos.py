import logging
import datetime
import io
import json
import hashlib
import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.base import async_session
from bot.db.models import User, MealLog
from bot.config import GEMINI_API_KEY, USDA_API_KEY
from bot.cache.redis_client import cache_get, cache_set
from bot.nutrition.usda import usda_search, enrich_with_usda

logger = logging.getLogger(__name__)

PHOTO_CACHE_TTL = 86400 * 7
PHOTO_ANALYSIS_TIMEOUT = 15
PHOTO_ANALYSIS_RETRIES = 1

_photo_locks: dict[int, asyncio.Lock] = {}

VISION_PROMPT = (
    "Это фото еды или напитка? Если НЕ еда — верни: {\"food_name\": null}\n"
    "Если это еда — определи блюдо и верни ТОЛЬКО валидный JSON:\n"
    '{"food_name": "название блюда", "estimated_calories": число, '
    '"estimated_weight_g": число, "protein": число или null, '
    '"fat": число или null, "carbs": число или null}\n'
    "Оцени реалистичный вес порции по размеру относительно посуды на фото."
)

VISION_PROMPT_STRICT = (
    "Предыдущий ответ НЕ был валидным JSON. Верни ТОЛЬКО валидный JSON без markdown, без комментариев:\n"
    '{"food_name": "название", "estimated_calories": число, "estimated_weight_g": число, '
    '"protein": число или null, "fat": число или null, "carbs": число или null}'
)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await _handle_photo_inner(update, context)
    except Exception as e:
        logger.exception(f"Photo handler crashed: {e}")
        try:
            if update and update.message:
                await update.message.reply_text(
                    "Произошла ошибка при обработке фото. Попробуй ещё раз."
                )
        except Exception:
            pass


async def _handle_photo_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id

    lock = _photo_locks.setdefault(tg_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            from sqlalchemy import select
            result = await session.execute(select(User).where(User.tg_id == tg_id))
            user = result.scalar_one_or_none()

        if not user:
            await update.message.reply_text(
                "Сначала напиши /start, чтобы я тебя узнал."
            )
            return

        await update.message.reply_text("Анализирую фото...")

        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = io.BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)

        if photo_bytes.getbuffer().nbytes == 0:
            await update.message.reply_text(
                "Фото не загрузилось. Попробуй отправить ещё раз."
            )
            return

        photo_md5 = hashlib.md5(photo_bytes.read()).hexdigest()
        photo_bytes.seek(0)

        cache_key = f"photo_cache:{photo_md5}"
        food_info = await cache_get(cache_key)

        if food_info and food_info.get("food_name"):
            logger.info(f"Photo cache hit: {photo_md5[:8]}")
        else:
            photo_bytes_compressed = _compress_photo(photo_bytes)
            food_info = await _analyze_food_photo(photo_bytes_compressed)
            if food_info and food_info.get("food_name"):
                await cache_set(cache_key, food_info, ttl=PHOTO_CACHE_TTL)

        if food_info and food_info.get("food_name"):
            has_macros = (
                food_info.get("protein")
                or food_info.get("fat")
                or food_info.get("carbs")
            )
            if not has_macros and USDA_API_KEY:
                usda_data = await usda_search(food_info["food_name"])
                if usda_data:
                    weight = food_info.get("estimated_weight_g", 200)
                    food_info = enrich_with_usda(food_info, usda_data, weight)

        if not food_info or not food_info.get("food_name"):
            await update.message.reply_text(
                "Не удалось распознать еду на фото. "
                "Опиши текстом что ты съел — я запишу."
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


def _compress_photo(photo_bytes: io.BytesIO) -> io.BytesIO:
    try:
        from PIL import Image

        photo_bytes.seek(0)
        img = Image.open(photo_bytes)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        return buf
    except ImportError:
        logger.debug("Pillow not available, sending original photo")
        photo_bytes.seek(0)
        return photo_bytes
    except Exception as e:
        logger.warning(f"Photo compression failed: {e}")
        photo_bytes.seek(0)
        return photo_bytes


def _detect_mime_type(photo_bytes: io.BytesIO) -> str:
    photo_bytes.seek(0)
    header = photo_bytes.read(12)
    photo_bytes.seek(0)

    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:3] == b"GIF":
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:2] == b"\xff\xd8":
        return "image/jpeg"
    if header[:4] in (b"\x00\x00\x00\x1c", b"\x00\x00\x00\x20"):
        return "image/heic"

    return "image/jpeg"


async def _analyze_food_photo(photo_bytes: io.BytesIO) -> dict | None:
    if not GEMINI_API_KEY:
        logger.warning("Photo analysis skipped: API key not configured")
        return None

    from bot.ai.clients import _get_gemini_client
    from google.genai import types

    client = _get_gemini_client()
    mime_type = _detect_mime_type(photo_bytes)

    for attempt in range(PHOTO_ANALYSIS_RETRIES + 1):
        try:
            prompt = VISION_PROMPT if attempt == 0 else VISION_PROMPT_STRICT
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=[
                        prompt,
                        types.Part.from_bytes(
                            data=photo_bytes.getvalue(), mime_type=mime_type
                        ),
                    ],
                ),
                timeout=PHOTO_ANALYSIS_TIMEOUT,
            )
            text = response.text.strip()
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Photo: invalid JSON from Gemini (attempt {attempt + 1}): "
                f"{text[:200]}"
            )
            if attempt < PHOTO_ANALYSIS_RETRIES:
                photo_bytes.seek(0)
                continue
            return None
        except asyncio.TimeoutError:
            logger.warning("Photo analysis timeout")
            return None
        except AttributeError as e:
            logger.error(f"Photo: Gemini API structure error: {e}")
            return None
        except Exception as e:
            logger.warning(f"Photo analysis failed: {e}")
            return None

    return None
