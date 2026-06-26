from datetime import datetime, UTC
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Text, DateTime, ForeignKey, JSON, func
)
from sqlalchemy.orm import relationship
from bot.db.base import Base


def _utcnow():
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    gender = Column(String(1), nullable=False)  # M / F
    age = Column(Integer, nullable=False)
    height_cm = Column(Float, nullable=False)
    weight_kg = Column(Float, nullable=False)
    target_weight_kg = Column(Float, nullable=False)
    activity_level = Column(String(20), nullable=False)  # sedentary / light / moderate / high
    goal = Column(String(20), nullable=False)  # cut / bulk / recomp / maintain
    allergies = Column(JSON, default=list)
    favorite_foods = Column(JSON, default=list)
    supplements = Column(JSON, default=list)  # [{name, dose, times: ["08:00"]}]
    sleep_schedule = Column(JSON, default=dict)  # {target_hours, preferred_sleep, preferred_wake}
    wake_time = Column(String(5), default="07:00")  # HH:MM
    workout_time = Column(String(5), default="18:00")  # HH:MM
    ai_personality = Column(String(20), default="friendly")  # friendly / strict / motivating
    settings = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_utcnow)

    workout_programs = relationship("WorkoutProgram", back_populates="user", cascade="all, delete-orphan")
    workout_logs = relationship("WorkoutLog", back_populates="user", cascade="all, delete-orphan")
    meal_logs = relationship("MealLog", back_populates="user", cascade="all, delete-orphan")
    sleep_logs = relationship("SleepLog", back_populates="user", cascade="all, delete-orphan")
    supplement_logs = relationship("SupplementLog", back_populates="user", cascade="all, delete-orphan")
    weight_history = relationship("WeightHistory", back_populates="user", cascade="all, delete-orphan")
    ai_usage_logs = relationship("AIUsageLog", back_populates="user", cascade="all, delete-orphan")


class WorkoutProgram(Base):
    __tablename__ = "workout_programs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    day_of_week = Column(String(20), nullable=False)  # monday / tuesday / ...

    user = relationship("User", back_populates="workout_programs")
    exercises = relationship("Exercise", back_populates="program", cascade="all, delete-orphan")


class Exercise(Base):
    __tablename__ = "exercises"

    id = Column(Integer, primary_key=True, autoincrement=True)
    program_id = Column(Integer, ForeignKey("workout_programs.id"), nullable=False)
    name = Column(String(100), nullable=False)
    type = Column(String(20), nullable=False)  # compound / isolation / cardio
    muscle_groups = Column(JSON, default=list)
    planned_sets = Column(Integer, nullable=False)
    planned_reps = Column(String(20), nullable=False)  # "8-10"
    planned_weight_kg = Column(Float, default=0)
    rest_seconds = Column(Integer, default=90)
    notes = Column(Text, default="")

    program = relationship("WorkoutProgram", back_populates="exercises")


class WorkoutLog(Base):
    __tablename__ = "workout_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    workout_name = Column(String(100), nullable=False)
    duration_minutes = Column(Integer, default=0)
    total_volume = Column(Float, default=0)
    subjective_feel = Column(Integer, default=5)  # 1-10
    calories_burned = Column(Float, default=0)

    user = relationship("User", back_populates="workout_logs")
    exercise_sets = relationship("ExerciseSet", back_populates="workout_log", cascade="all, delete-orphan")


class ExerciseSet(Base):
    __tablename__ = "exercise_sets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    log_id = Column(Integer, ForeignKey("workout_logs.id"), nullable=False)
    exercise_name = Column(String(100), nullable=False)
    set_number = Column(Integer, nullable=False)
    weight_kg = Column(Float, nullable=False)
    reps = Column(Integer, nullable=False)
    rpe = Column(Float, default=5)

    workout_log = relationship("WorkoutLog", back_populates="exercise_sets")


class MealLog(Base):
    __tablename__ = "meal_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    food_name = Column(String(200), nullable=False)
    weight_g = Column(Float, nullable=False)
    calories = Column(Float, default=0)
    protein = Column(Float, default=0)
    fat = Column(Float, default=0)
    carbs = Column(Float, default=0)
    source = Column(String(20), default="usda")  # usda / ai / cache

    user = relationship("User", back_populates="meal_logs")


class FoodItem(Base):
    __tablename__ = "food_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), unique=True, nullable=False)
    photo_hash = Column(String(32), nullable=True)
    calories_per_100g = Column(Float, nullable=False)
    protein_per_100g = Column(Float, default=0)
    fat_per_100g = Column(Float, default=0)
    carbs_per_100g = Column(Float, default=0)
    category = Column(String(50), default="general")
    created_at = Column(DateTime, default=_utcnow)


class SleepLog(Base):
    __tablename__ = "sleep_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    sleep_time = Column(DateTime, nullable=False)
    wake_time = Column(DateTime, nullable=False)
    duration_hours = Column(Float, nullable=False)

    user = relationship("User", back_populates="sleep_logs")


class SupplementLog(Base):
    __tablename__ = "supplement_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    supplement_name = Column(String(100), nullable=False)
    dose = Column(String(50), nullable=False)
    taken_at = Column(DateTime, default=_utcnow)

    user = relationship("User", back_populates="supplement_logs")


class WeightHistory(Base):
    __tablename__ = "weight_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=_utcnow)
    weight_kg = Column(Float, nullable=False)

    user = relationship("User", back_populates="weight_history")


class AIUsageLog(Base):
    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String(20), nullable=False)
    tokens_in = Column(Integer, default=0)
    tokens_out = Column(Integer, default=0)
    timestamp = Column(DateTime, default=_utcnow, index=True)

    user = relationship("User", back_populates="ai_usage_logs")
