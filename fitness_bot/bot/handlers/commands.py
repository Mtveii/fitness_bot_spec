import logging
from telegram import Update
from telegram.ext import ContextTypes

from bot.db.models import User
from bot.db.base import async_session
from bot.config import ADMIN_ID
from bot.cache.redis_client import cache_get

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()

    if user:
        await update.message.reply_text(
            "С возвращением! Я помню тебя. Просто напиши, что у тебя нового "
            "(ел, тренировался, спал, самочувствие) — я всё пойму."
        )
    else:
        context.user_data["onboarding"] = True
        context.user_data["profile_data"] = {}
        await update.message.reply_text(
            "Привет! Я твой персональный фитнес-ассистент. Расскажи о себе "
            "свободным текстом — я пойму и запомню.\n\n"
            "Например: «Меня зовут Антон, мне 28 лет, я мужчина, рост 180, "
            "вес 80, хочу похудеть, активность средняя».\n\n"
            "Рассказывай всё, что считаешь нужным, я сам заполню профиль."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я ИИ-фитнес-ассистент. Просто пиши мне как человеку:\n"
        "• \u202f«Съел 200г курицы с рисом» — запишу еду\n"
        "• \u202f«Потренировал грудь и трицепс» — запишу тренировку\n"
        "• \u202f«Лёг спать в 23:00» — запомню паттерн сна\n"
        "• \u202f«Как у меня дела за неделю?» — покажу статистику\n"
        "• \u202f«Общайся жёстче» — сменю тон общения\n\n"
        "Я сам решаю, что важно запомнить, а что — просто разговор."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.tg_id == tg_id))
        user = result.scalar_one_or_none()
    if not user:
        await update.message.reply_text("Сначала напиши /start, чтобы я тебя узнал.")
        return
    context.user_data["force_stats"] = True
    await update.message.reply_text("Собираю твою статистику...")
