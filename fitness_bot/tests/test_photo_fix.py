"""Тесты для исправленной логики photos.py: индентация, обработка ошибок."""
import pytest
import asyncio
import hashlib
import io
from unittest.mock import AsyncMock, MagicMock, patch


def _make_update(tg_id=12345, has_photo=True, is_document=False):
    update = MagicMock()
    user = MagicMock()
    user.id = tg_id
    update.effective_user = user
    if is_document:
        doc = MagicMock()
        doc.file_name = "test.jpg"
        doc.mime_type = "image/jpeg"
        doc.file_size = 1000
        doc.get_file = AsyncMock()
        file_ref = MagicMock()
        file_ref.download_to_memory = AsyncMock(side_effect=lambda buf: buf.write(b"\xff\xd8\xff\xe0fake_jpeg_data"))
        doc.get_file.return_value = file_ref
        update.message.document = doc
        update.message.photo = []
        update.message.reply_text = AsyncMock()
    elif has_photo:
        photo_sizes = [MagicMock() for _ in range(3)]
        for p in photo_sizes:
            fr = MagicMock()
            fr.download_to_memory = AsyncMock(side_effect=lambda b, _payload=b"\xff\xd8\xff\xe0fake_jpeg": b.write(_payload))
            p.get_file = AsyncMock(return_value=fr)
        update.message.document = None
        update.message.photo = photo_sizes
        update.message.reply_text = AsyncMock()
    else:
        update.message.document = None
        update.message.photo = []
        update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
@patch("bot.handlers.photos.cache_get", new_callable=AsyncMock, return_value=None)
@patch("bot.handlers.photos.cache_set", new_callable=AsyncMock)
@patch("bot.handlers.photos.usda_search", new_callable=AsyncMock, return_value=None)
@patch("bot.handlers.photos._analyze_food_photo", new_callable=AsyncMock)
@patch("bot.handlers.photos.async_session")
async def test_handle_photo_saves_meal_with_full_path(
    mock_session_cls, mock_analyze, mock_usda, mock_cache_set, mock_cache_get
):
    """Сценарий: пользователь есть в БД, фото распознано — должно сохраниться в БД."""
    from bot.db.models import User
    from bot.handlers import photos

    fake_user = User(id=1, tg_id=12345, name="Test")
    mock_sess = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = fake_user
    mock_sess.execute = AsyncMock(return_value=mock_result)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=None)
    mock_sess.commit = AsyncMock()
    mock_sess.add = MagicMock()
    mock_session_cls.return_value = mock_sess

    mock_analyze.return_value = {
        "food_name": "Паста карбонара",
        "estimated_calories": 650,
        "estimated_weight_g": 300,
        "protein": 25.0,
        "fat": 35.0,
        "carbs": 70.0,
    }

    update = _make_update(has_photo=True)
    context = MagicMock()

    await photos.handle_photo(update, context)

    assert mock_sess.add.called, "Должен был быть сохранён MealLog"
    assert mock_sess.commit.called, "Должен был быть коммит"
    assert update.message.reply_text.await_count >= 2
    last_call = update.message.reply_text.call_args
    text = last_call[0][0]
    assert "Паста карбонара" in text
    assert "650" in text or "300" in text


@pytest.mark.asyncio
@patch("bot.handlers.photos.async_session")
async def test_handle_photo_no_user_returns_help(mock_session_cls):
    """Без /start бот должен ответить, а не падать."""
    from bot.handlers import photos

    mock_sess = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_sess.execute = AsyncMock(return_value=mock_result)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=None)
    mock_session_cls.return_value = mock_sess

    update = _make_update(tg_id=99999, has_photo=True)
    context = MagicMock()

    await photos.handle_photo(update, context)

    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("/start" in t for t in texts)


@pytest.mark.asyncio
@patch("bot.config.GEMINI_API_KEY", "")
@patch("bot.handlers.photos.async_session")
async def test_handle_photo_no_key_skips_gracefully(mock_session_cls):
    """Без ключа Gemini бот не должен падать — должен предупредить."""
    from bot.handlers import photos

    update = _make_update(tg_id=1, has_photo=True)
    context = MagicMock()

    await photos.handle_photo(update, context)

    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("ключ" in t or "текстом" in t for t in texts)


@pytest.mark.asyncio
@patch("bot.handlers.photos.cache_get", new_callable=AsyncMock, return_value=None)
@patch("bot.handlers.photos.usda_search", new_callable=AsyncMock, return_value=None)
@patch("bot.handlers.photos._analyze_food_photo", new_callable=AsyncMock, return_value={"food_name": None})
@patch("bot.handlers.photos.async_session")
async def test_handle_photo_returns_message_when_not_food(
    mock_session_cls, mock_analyze, mock_usda, mock_cache_get
):
    """Если Gemini сказал, что это не еда — пользователь получает вежливый ответ."""
    from bot.db.models import User
    from bot.handlers import photos

    fake_user = User(id=1, tg_id=12345, name="Test")
    mock_sess = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = fake_user
    mock_sess.execute = AsyncMock(return_value=mock_result)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=None)
    mock_sess.commit = AsyncMock()
    mock_session_cls.return_value = mock_sess

    update = _make_update(has_photo=True)
    context = MagicMock()

    await photos.handle_photo(update, context)

    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("не удалось" in t.lower() or "текстом" in t.lower() for t in texts)
    assert not mock_sess.commit.called, "Без еды коммитить нечего"
