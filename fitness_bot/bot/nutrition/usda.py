import logging
import asyncio
import aiohttp

from bot.config import USDA_API_KEY

logger = logging.getLogger(__name__)

USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"
USDA_TIMEOUT = 10
USDA_MAX_RETRIES = 2


async def usda_search(food_name: str) -> dict | None:
    if not USDA_API_KEY:
        return None

    for attempt in range(USDA_MAX_RETRIES + 1):
        try:
            params = {
                "api_key": USDA_API_KEY,
                "query": food_name,
                "dataType": "Foundation,SR Legacy",
                "pageSize": 3,
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{USDA_BASE_URL}/foods/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=USDA_TIMEOUT),
                ) as resp:
                    if resp.status == 429:
                        if attempt < USDA_MAX_RETRIES:
                            wait = 2 ** attempt
                            logger.warning(
                                f"USDA rate limited, retrying in {wait}s"
                            )
                            await asyncio.sleep(wait)
                            continue
                        logger.warning("USDA rate limited, all retries exhausted")
                        return None
                    if resp.status != 200:
                        logger.warning(f"USDA search returned {resp.status}")
                        return None
                    data = await resp.json()

            foods = data.get("foods", [])
            if not foods:
                return None

            if len(foods) > 1:
                query_words = set(food_name.lower().split())
                best = max(
                    foods,
                    key=lambda f: len(
                        query_words & set(f.get("description", "").lower().split())
                    ),
                )
            else:
                best = foods[0]

            nutrients = {
                n["nutrientName"]: n["value"]
                for n in best.get("foodNutrients", [])
            }

            return {
                "fdc_id": best.get("fdcId"),
                "description": best.get("description"),
                "calories_per_100g": nutrients.get("Energy", 0),
                "protein_per_100g": nutrients.get("Protein", 0),
                "fat_per_100g": nutrients.get("Total lipid (fat)", 0),
                "carbs_per_100g": nutrients.get(
                    "Carbohydrate, by difference", 0
                ),
            }
        except asyncio.TimeoutError:
            logger.warning("USDA API timeout")
            if attempt < USDA_MAX_RETRIES:
                await asyncio.sleep(1)
                continue
            return None
        except Exception as e:
            logger.warning(f"USDA API error: {e}")
            if attempt < USDA_MAX_RETRIES:
                await asyncio.sleep(1)
                continue
            return None

    return None


def enrich_with_usda(food_info: dict, usda_data: dict, weight_g: float) -> dict:
    if not usda_data:
        return food_info

    factor = weight_g / 100.0

    if not food_info.get("protein") and usda_data.get("protein_per_100g"):
        food_info["protein"] = round(usda_data["protein_per_100g"] * factor, 1)
    if not food_info.get("fat") and usda_data.get("fat_per_100g"):
        food_info["fat"] = round(usda_data["fat_per_100g"] * factor, 1)
    if not food_info.get("carbs") and usda_data.get("carbs_per_100g"):
        food_info["carbs"] = round(usda_data["carbs_per_100g"] * factor, 1)
    if not food_info.get("estimated_calories") and usda_data.get(
        "calories_per_100g"
    ):
        food_info["estimated_calories"] = round(
            usda_data["calories_per_100g"] * factor, 0
        )

    return food_info
