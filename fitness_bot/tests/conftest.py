import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from bot.db.base import Base
from bot.db.models import User
import bot.tools.handlers as handlers_mod


TEST_USER_ID = 1


@pytest_asyncio.fixture(autouse=True)
async def test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        user = User(
            id=TEST_USER_ID, tg_id=99999, name="Test", gender="M",
            age=25, height_cm=175, weight_kg=70, target_weight_kg=65,
            activity_level="moderate", goal="lose",
        )
        session.add(user)
        await session.commit()

    orig = handlers_mod.async_session
    handlers_mod.async_session = session_factory

    yield session_factory

    handlers_mod.async_session = orig
    await engine.dispose()
