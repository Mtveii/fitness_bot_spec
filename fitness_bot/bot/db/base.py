import os
import sqlite3
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Миграция: добавляем новые колонки если их нет
    db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        migrations = [
            ("wake_time", "TEXT NOT NULL DEFAULT '07:00'"),
            ("workout_time", "TEXT NOT NULL DEFAULT '18:00'"),
        ]

        for col_name, col_def in migrations:
            if col_name not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                print(f"Migration: added column users.{col_name}")

        conn.commit()
        conn.close()
