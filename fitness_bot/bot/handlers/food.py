import re
import os
import json
import logging
import asyncio
import httpx
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.db.base import async_session
from bot.db import crud
from bot.db.models import User
from bot.cache.redis_client import get_today_state, update_today_state
from bot.calculators.tdee import bmr, tdee
from bot.calculators.nutrition import daily_targets

logger = logging.getLogger(__name__)

USDA_API_KEY = os.getenv("USDA_API_KEY", "")
USDA_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "data")
CACHE_PATH = os.path.join(DATA_DIR, "food_cache.json")
USDA_LOCAL_PATH = os.path.join(DATA_DIR, "usda_foods.json")

_food_cache = {}
if os.path.exists(CACHE_PATH):
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        _food_cache = json.load(f)

_usda_local = []
_usda_names = []
if os.path.exists(USDA_LOCAL_PATH):
    with open(USDA_LOCAL_PATH, "r", encoding="utf-8") as f:
        _usda_local = json.load(f)
        _usda_names = [item["name"] for item in _usda_local]


def _save_cache():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_food_cache, f, ensure_ascii=False, indent=2)


def search_food_local(query: str) -> dict | None:
    """Fuzzy search в локальной USDA базе (224+ продуктов)."""
    if not _usda_local:
        return None

    from thefuzz import process
    result = process.extractOne(query.lower(), _usda_names, score_cutoff=70)
    if result:
        match_name, score, idx = result[0], result[1], result[2]
        food = _usda_local[idx]
        logger.info(f"Local USDA match: '{match_name}' (score={score})")
        return {
            "calories": food.get("calories", 0),
            "protein": food.get("protein", 0),
            "fat": food.get("fat", 0),
            "carbs": food.get("carbs", 0),
        }
    return None


async def search_food_usda(query: str) -> dict | None:
    """Поиск через USDA API."""
    if not USDA_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(USDA_URL, params={
                "api_key": USDA_API_KEY,
                "query": query,
                "pageSize": 3,
            })
            resp.raise_for_status()
            data = resp.json()

        foods = data.get("foods", [])
        if not foods:
            return None

        food = foods[0]
        nutrients = {}
        for n in food.get("foodNutrients", []):
            name = n.get("nutrientName", "")
            value = n.get("value", 0)
            if "Energy" in name or "Calori" in name:
                nutrients["calories"] = value
            elif "Protein" in name:
                nutrients["protein"] = value
            elif "lipid" in name.lower() or "Fat" in name:
                nutrients["fat"] = value
            elif "Carbohydrate" in name:
                nutrients["carbs"] = value

        if not all(k in nutrients for k in ("calories", "protein", "fat", "carbs")):
            return None

        result = {
            "calories": nutrients["calories"],
            "protein": nutrients["protein"],
            "fat": nutrients["fat"],
            "carbs": nutrients["carbs"],
        }

        # Кэшируем
        _food_cache[query.lower().strip()] = result
        _save_cache()

        return result

    except Exception as e:
        logger.error(f"USDA API error: {e}")
        return None


async def estimate_food_with_ai(food_name: str) -> dict | None:
    """Оценивает КБЖУ через Groq (основной) или Gemini (fallback)."""
    prompt = (
        f"Оцени КБЖУ продукта «{food_name}» на 100г.\n"
        "Если это бренд/ресторан — оцени по известным данным.\n"
        "Big Mac ≈ 257ккал, Monster ≈ 42ккал, Coca-Cola Zero ≈ 0.4ккал.\n"
        "Верни ТОЛЬКО JSON без markdown:\n"
        '{"calories": число, "protein": число, "fat": число, "carbs": число}'
    )

    # Groq (основной)
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        try:
            import groq
            client = groq.AsyncGroq(api_key=groq_key)
            response = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.3,
            )
            text = response.choices[0].message.content.strip()
            return _parse_food_json(text, food_name)
        except Exception as e:
            logger.warning(f"Groq food estimate failed: {e}")

    # Gemini (fallback)
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    if gemini_key:
        try:
            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: model.generate_content(prompt)
            )
            text = response.text.strip()
            return _parse_food_json(text, food_name)
        except Exception as e:
            logger.warning(f"Gemini food estimate failed: {e}")

    return None


def _parse_food_json(text: str, food_name: str) -> dict | None:
    """Парсит JSON из ответа ИИ."""
    # Убираем markdown code blocks
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        result = json.loads(text)
        if all(k in result for k in ("calories", "protein", "fat", "carbs")):
            if all(isinstance(result[k], (int, float)) for k in ("calories", "protein", "fat", "carbs")):
                _food_cache[food_name.lower().strip()] = result
                _save_cache()
                return result
    except json.JSONDecodeError:
        # Пытаемся найти JSON в тексте
        match = re.search(r'\{[^}]+\}', text)
        if match:
            try:
                result = json.loads(match.group())
                if all(k in result for k in ("calories", "protein", "fat", "carbs")):
                    _food_cache[food_name.lower().strip()] = result
                    _save_cache()
                    return result
            except json.JSONDecodeError:
                pass
    return None


async def search_food(query: str) -> dict | None:
    """Цепочка: кэш → локальная USDA → USDA API → AI."""
    key = query.lower().strip()

    # 1. Кэш (уже искали раньше)
    if key in _food_cache:
        return _food_cache[key]

    # 2. Локальная USDA база (fuzzy search, без запросов)
    result = search_food_local(query)
    if result:
        _food_cache[key] = result
        _save_cache()
        return result

    # 3. USDA API
    result = await search_food_usda(query)
    if result:
        return result

    # 4. AI
    return await estimate_food_with_ai(query)


def parse_food_input(text: str) -> tuple[float, str] | None:
    patterns = [
        r"(\d+(?:[.,]\d+)?)\s*г\s+(.+)",
        r"(\d+(?:[.,]\d+)?)\s*(?:грамм|грамма)\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            weight = float(match.group(1).replace(",", "."))
            food = match.group(2).strip()
            return weight, food
    return None


def format_progress_bar(current: float, target: float, length: int = 10) -> str:
    if target <= 0:
        return "⬜" * length
    pct = min(current / target, 1.0)
    filled = round(pct * length)
    return "🟩" * filled + "⬜" * (length - filled)


async def get_targets_for_user(user: User) -> dict:
    bmr_val = bmr(user.gender, user.weight_kg, user.height_cm, user.age)
    tdee_val = tdee(bmr_val, user.activity_level, weight_kg=user.weight_kg)
    return daily_targets(tdee_val, user.weight_kg, user.goal)


async def get_food_suggestion(user_id: int) -> str | None:
    """Предложить еду при недоборе белка/калорий."""
    async with async_session() as session:
        user = await crud.get_user(session, user_id)
        if not user:
            return None

    state = await get_today_state(user_id)
    targets = await get_targets_for_user(user)

    protein_deficit = targets["protein_g"] - state["protein"]
    calorie_deficit = targets["calories"] - state["calories_in"]

    if protein_deficit < 20 and calorie_deficit < 200:
        return None

    favorites = user.favorite_foods or []
    suggestions = []

    for fav in favorites:
        food_data = _food_cache.get(fav.lower().strip())
        if food_data and food_data.get("protein", 0) > 10:
            needed_g = round(protein_deficit / (food_data["protein"] / 100))
            cal = food_data["calories"] * needed_g / 100
            suggestions.append(f"🥩 {needed_g}г {fav} → +{food_data['protein'] * needed_g / 100:.0f}г белка, {cal:.0f}ккал")

    if not suggestions and protein_deficit > 20:
        suggestions.append("🥩 200г творога → +36г белка")
        suggestions.append("🥩 180г курицы → +56г белка")

    if not suggestions:
        return None

    return f"⚠️ До цели: +{protein_deficit:.0f}г белка, +{calorie_deficit:.0f}ккал\n\n" + "\n".join(suggestions[:3])


async def log_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /log 200г гречки")
        return

    text = " ".join(context.args)
    parsed = parse_food_input(text)

    if not parsed:
        await update.message.reply_text("Не распознал. Формат: /log 200г гречки")
        return

    weight_g, food_name = parsed

    await update.message.reply_text("🔍 Ищу...")

    food_data = await search_food(food_name)

    if not food_data:
        await update.message.reply_text(
            f"«{food_name}» не найдено.\n"
            "Проверь название или попробуй другое."
        )
        return

    factor = weight_g / 100
    calories = food_data["calories"] * factor
    protein = food_data["protein"] * factor
    fat = food_data["fat"] * factor
    carbs = food_data["carbs"] * factor

    async with async_session() as session:
        user = await crud.get_user(session, update.effective_user.id)
        if not user:
            await update.message.reply_text("Сначала /onboarding")
            return

        await crud.add_meal_log(
            session, user_id=user.id, food_name=food_name, weight_g=weight_g,
            calories=calories, protein=protein, fat=fat, carbs=carbs, source="api"
        )
        targets = await get_targets_for_user(user)

    await update_today_state(
        update.effective_user.id,
        calories_in=calories, protein=protein, fat=fat, carbs=carbs,
    )

    today = await get_today_state(update.effective_user.id)

    cal_pct = (today["calories_in"] / targets["calories"] * 100) if targets["calories"] > 0 else 0
    prot_pct = (today["protein"] / targets["protein_g"] * 100) if targets["protein_g"] > 0 else 0

    response = (
        f"🍽 {food_name.title()} ({weight_g:.0f}г)\n\n"
        f"🔥 {calories:.0f} ккал | 🥩 {protein:.1f}г | 🧈 {fat:.1f}г | 🍞 {carbs:.1f}г\n\n"
        f"📊 За день: {today['calories_in']:.0f} / {targets['calories']} "
        f"{format_progress_bar(today['calories_in'], targets['calories'])} {cal_pct:.0f}%\n"
        f"🥩 Белок: {today['protein']:.0f} / {targets['protein_g']}г "
        f"{format_progress_bar(today['protein'], targets['protein_g'])} {prot_pct:.0f}%"
    )

    suggestion = await get_food_suggestion(update.effective_user.id)
    if suggestion:
        response += f"\n\n{suggestion}"

    await update.message.reply_text(response)


def get_food_handler() -> CommandHandler:
    return CommandHandler("log", log_food)
