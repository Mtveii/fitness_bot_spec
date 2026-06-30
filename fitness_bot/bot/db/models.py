import datetime
from sqlalchemy import (Column, Integer, BigInteger, String, Float, DateTime,
                        JSON, Text, ForeignKey, Enum as SAEnum, UniqueConstraint)
from sqlalchemy.orm import relationship
import enum

from bot.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False, unique=True, index=True)
    name = Column(String(100), nullable=False)
    gender = Column(String(1), nullable=False)
    age = Column(Integer, nullable=False)
    height_cm = Column(Float, nullable=False)
    weight_kg = Column(Float, nullable=False)
    target_weight_kg = Column(Float, nullable=False)
    activity_level = Column(String(20), nullable=False)
    goal = Column(String(20), nullable=False)
    allergies = Column(JSON, nullable=True)
    favorite_foods = Column(JSON, nullable=True)
    supplements = Column(JSON, nullable=True)
    sleep_schedule = Column(JSON, nullable=True)
    ai_personality = Column(String(20), nullable=True)
    settings = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    wake_time = Column(String(5), default="07:00")
    workout_time = Column(String(5), default="18:00")

    disliked_foods = Column(Text, default="[]")
    dietary_preferences = Column(Text, default="[]")
    cooking_level = Column(String(10), default="medium")
    food_notes = Column(Text, default="")
    role = Column(String(10), default="user")

    meal_logs = relationship("MealLog", back_populates="user", lazy="selectin")
    workout_logs = relationship("WorkoutLog", back_populates="user", lazy="selectin")
    sleep_logs = relationship("SleepLog", back_populates="user", lazy="selectin")
    weight_history = relationship("WeightHistory", back_populates="user", lazy="selectin")
    supplement_logs = relationship("SupplementLog", back_populates="user", lazy="selectin")
    time_observations = relationship("TimeObservation", back_populates="user", lazy="selectin")
    memory_profile = relationship("UserMemoryProfile", uselist=False, back_populates="user", lazy="selectin")


class FoodItem(Base):
    __tablename__ = "food_items"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    photo_hash = Column(String(32), nullable=True)
    calories_per_100g = Column(Float, nullable=False)
    protein_per_100g = Column(Float, nullable=True)
    fat_per_100g = Column(Float, nullable=True)
    carbs_per_100g = Column(Float, nullable=True)
    category = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MealLog(Base):
    __tablename__ = "meal_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=True)
    food_name = Column(String(200), nullable=False)
    weight_g = Column(Float, nullable=False)
    calories = Column(Float, nullable=True)
    protein = Column(Float, nullable=True)
    fat = Column(Float, nullable=True)
    carbs = Column(Float, nullable=True)
    source = Column(String(20), nullable=True)

    user = relationship("User", back_populates="meal_logs")


class WorkoutLog(Base):
    __tablename__ = "workout_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=True)
    workout_name = Column(String(100), nullable=False)
    duration_minutes = Column(Integer, nullable=True)
    total_volume = Column(Float, nullable=True)
    subjective_feel = Column(Integer, nullable=True)
    calories_burned = Column(Float, nullable=True)

    user = relationship("User", back_populates="workout_logs")
    exercise_sets = relationship("ExerciseSet", back_populates="workout_log", lazy="selectin")


class WorkoutProgram(Base):
    __tablename__ = "workout_programs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    day_of_week = Column(String(20), nullable=False)

    exercises = relationship("Exercise", back_populates="program", lazy="selectin")


class Exercise(Base):
    __tablename__ = "exercises"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("workout_programs.id"), nullable=False)
    name = Column(String(100), nullable=False)
    type = Column(String(20), nullable=False)
    muscle_groups = Column(JSON, nullable=True)
    planned_sets = Column(Integer, nullable=False)
    planned_reps = Column(String(20), nullable=False)
    planned_weight_kg = Column(Float, nullable=True)
    rest_seconds = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)

    program = relationship("WorkoutProgram", back_populates="exercises")


class ExerciseSet(Base):
    __tablename__ = "exercise_sets"

    id = Column(Integer, primary_key=True)
    log_id = Column(Integer, ForeignKey("workout_logs.id"), nullable=False)
    exercise_name = Column(String(100), nullable=False)
    set_number = Column(Integer, nullable=False)
    weight_kg = Column(Float, nullable=False)
    reps = Column(Integer, nullable=False)
    rpe = Column(Float, nullable=True)

    workout = relationship("WorkoutLog", back_populates="exercise_sets")


class SleepLog(Base):
    __tablename__ = "sleep_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=True)
    sleep_time = Column(DateTime, nullable=False)
    wake_time = Column(DateTime, nullable=False)
    duration_hours = Column(Float, nullable=False)

    user = relationship("User", back_populates="sleep_logs")


class SupplementLog(Base):
    __tablename__ = "supplement_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=True)
    supplement_name = Column(String(100), nullable=False)
    dose = Column(String(50), nullable=False)
    taken_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="supplement_logs")


class WeightHistory(Base):
    __tablename__ = "weight_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=True)
    weight_kg = Column(Float, nullable=False)

    user = relationship("User", back_populates="weight_history")


class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, nullable=False)
    calories_in = Column(Float, nullable=True)
    calories_out = Column(Float, nullable=True)
    protein = Column(Float, nullable=True)
    fat = Column(Float, nullable=True)
    carbs = Column(Float, nullable=True)
    steps = Column(Integer, nullable=True)
    workout_kcal = Column(Float, nullable=True)
    balance = Column(Float, nullable=True)


class AIUsageLog(Base):
    __tablename__ = "ai_usage_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    provider = Column(String(20), nullable=False)
    tokens_in = Column(Integer, nullable=True)
    tokens_out = Column(Integer, nullable=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)


class ObservationType(str, enum.Enum):
    wake = "wake"
    sleep = "sleep"
    meal = "meal"
    message_activity = "message_activity"


class ObservationSource(str, enum.Enum):
    explicit = "explicit"
    inferred = "inferred"


class TimeObservation(Base):
    __tablename__ = "time_observations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    observation_type = Column(SAEnum(ObservationType), nullable=False)
    observed_at = Column(DateTime, nullable=False)
    source = Column(SAEnum(ObservationSource), nullable=False)
    confidence = Column(Float, nullable=False, default=0.5)
    raw_text = Column(Text, nullable=True)

    user = relationship("User", back_populates="time_observations")


class UserMemoryProfile(Base):
    __tablename__ = "user_memory_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    avg_wake_time = Column(String(5), nullable=True)
    avg_sleep_time = Column(String(5), nullable=True)
    avg_meal_times = Column(JSON, nullable=True)
    busy_hours = Column(JSON, nullable=True)
    preferences_summary = Column(Text, nullable=True)
    communication_tone = Column(String(20), nullable=True)
    last_summarized_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="memory_profile")
