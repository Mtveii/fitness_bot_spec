import logging
import datetime
import io
import json
import hashlib
import asyncio
import os

from telegram import Update
from telegram.ext import ContextTypes

from bot.db.base import async_session
from bot.db.models import User, MealLog
from bot.config import GEMINI_API_KEY, USDA_API_KEY
from bot.cache.redis_client import cache_get, cache_set
from bot.nutrition.usda import usda_search, enrich_with_usda

logger = logging.getLogger(__name__)

PHOTO_CACHE_TTL = 86400 * 7
PHOTO_ANALYSIS_TIMEOUT = 30
PHOTO_ANALYSIS_RETRIES = 1

_photo_locks: dict[int, asyncio.Lock] = {}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tiff"}

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


def _is_image_document(document) -> bool:
    if not document:
        return False
    mime_type = getattr(document, "mime_type", "") or ""
    if mime_type.startswith("image/"):
        return True
    file_name = getattr(document, "file_name", "") or ""
    _, ext = os.path.splitext(file_name.lower())
    return ext in IMAGE_EXTENSIONS


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


async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_image_document(update.message.document):
        return
    try:
        await _handle_photo_inner(update, context, from_document=True)
    except Exception as e:
        logger.exception(f"Document photo handler crashed: {e}")
        try:
            if update and update.message:
                await update.message.reply_text(
                    "Произошла ошибка при обработке фото. Попробуй ещё раз."
                )
        except Exception:
            pass


async def _handle_photo_inner(update: Update, context: ContextTypes.DEFAULT_TYPE, from_document: bool = False):
    tg_id = update.effective_user.id

    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "Распознавание фото недоступно — не настроен API ключ Gemini. "
            "Опиши текстом что ты съел — я запишу."
        )
        return

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

        try:
            if from_document:
                doc = update.message.document
                logger.info(
                    f"Photo from document: {doc.file_name or 'unknown'} "
                    f"mime={doc.mime_type} size={doc.file_size}"
                )
                file_ref = await doc.get_file()
            else:
                photo_msg = update.message.photo[-1] if update.message.photo else None
                if not photo_msg:
                    await update.message.reply_text(
                        "Фото не загрузилось. Попробуй отправить ещё раз."
                    )
                    return
                file_ref = await photo_msg.get_file()

            photo_bytes = io.BytesIO()
            await file_ref.download_to_memory(photo_bytes)
            photo_bytes.seek(0)
        except Exception as e:
            logger.exception(f"Failed to download photo: {e}")
            await update.message.reply_text(
                "Не удалось скачать фото. Попробуй отправить ещё раз."
            )
            return

        logger.info(f"Downloaded photo: {photo_bytes.getbuffer().nbytes} bytes")

        if photo_bytes.getbuffer().nbytes == 0:
            await update.message.reply_text(
                "Фото не загрузилось. Попробуй отправить ещё раз."
            )
            return

        try:
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
        except Exception as e:
            logger.exception(f"Vision pipeline error: {e}")
            await update.message.reply_text(
                "Ошибка при распознавании фото. Опиши текстом что ты съел — я запишу."
            )
            return

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

        try:
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
        except Exception as e:
            logger.exception(f"Failed to save meal log: {e}")
            await update.message.reply_text(
                "Распознал еду, но не смог сохранить в дневник. Попробуй ещё раз."
            )
            return

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
    import base64
    from bot.ai.clients import ask_openrouter_vision, ask_nvidia_vision

    mime_type = _detect_mime_type(photo_bytes)

    if mime_type == "image/heic":
        try:
            from PIL import Image
            photo_bytes.seek(0)
            img = Image.open(photo_bytes)
            img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            buf.seek(0)
            photo_bytes = buf
            mime_type = "image/jpeg"
        except Exception as e:
            logger.warning(f"HEIC -> JPEG conversion failed: {e}")

    photo_bytes.seek(0)
    photo_b64 = base64.b64encode(photo_bytes.read()).decode()

    async def _parse_vision_text(text: str, provider: str) -> dict | None:
        if not text:
            return None
        for attempt in range(2):
            try:
                clean = text.replace("```json", "").replace("```", "").strip()
                result = json.loads(clean)
                logger.info(f"Vision provider used: {provider}")
                return result
            except json.JSONDecodeError:
                if attempt == 0:
                    logger.warning(f"{provider} vision: invalid JSON: {text[:200]}")
                    return None
        return None

    if GEMINI_API_KEY:
        from bot.ai.clients import _get_gemini_client
        from google.genai import types
        client = _get_gemini_client()
        for attempt in range(PHOTO_ANALYSIS_RETRIES + 1):
            try:
                prompt = VISION_PROMPT if attempt == 0 else VISION_PROMPT_STRICT
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model="gemini-2.0-flash",
                        contents=[
                            prompt,
                            types.Part.from_bytes(data=photo_bytes.getvalue(), mime_type=mime_type),
                        ],
                    ),
                    timeout=PHOTO_ANALYSIS_TIMEOUT,
                )
                text = (response.text or "").strip()
                result = await _parse_vision_text(text, "gemini")
                if result and result.get("food_name") is not None:
                    return result
            except Exception as e:
                logger.warning(f"Gemini vision attempt {attempt+1} failed: {e}")
                break

    logger.info("Vision: Gemini failed, trying OpenRouter")
    text = await ask_openrouter_vision(photo_b64, mime_type, VISION_PROMPT)
    result = await _parse_vision_text(text or "", "openrouter")
    if result and result.get("food_name") is not None:
        return result

    logger.info("Vision: OpenRouter failed, trying NVIDIA NIM")
    text = await ask_nvidia_vision(photo_b64, mime_type, VISION_PROMPT)
    result = await _parse_vision_text(text or "", "nvidia")
    if result and result.get("food_name") is not None:
        return result

    logger.warning("Vision: all providers failed")
    return None
