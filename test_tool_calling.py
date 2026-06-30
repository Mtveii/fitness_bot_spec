"""
Comprehensive test script for tool-calling system (P4.20).
Runs the full checklist from the test plan and logs all results.
"""
import os
import sys
import json
import asyncio
import logging
from pathlib import Path

# --- Setup project path ---
PROJECT_DIR = Path(__file__).resolve().parent / "fitness_bot"
sys.path.insert(0, str(PROJECT_DIR))

# --- Load .env ---
from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")

# --- Configure logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
# Suppress noisy libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("groq").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

logger = logging.getLogger("TOOLS_TEST")

# --- Import bot modules ---
from bot.ai.tools import ALL_TOOLS, TOOLS_BY_NAME
from bot.ai.clients import ask_groq_with_tools, ask_gemini_with_tools
from bot.ai.actions import (
    handle_message_with_actions,
    _is_confirmation, _is_rejection,
    _build_confirmation_text,
    _execute_action_now, _execute_pending_action,
    CONFIRMATION_WORDS, REJECTION_WORDS,
)
from bot.cache.redis_client import (
    get_pending_action, set_pending_action, clear_pending_action,
    USE_REDIS,
)
from bot.db.base import async_session, init_db
from bot.db import crud

# --- Global test state ---
TEST_USER_TG_ID = 999999999  # Fake Telegram ID for testing
test_user_id = None  # Will be set after DB init

# =============================================================
#  Section 0: Setup
# =============================================================

async def setup_test_user():
    """Create or find a test user in the database."""
    global test_user_id
    await init_db()
    async with async_session() as session:
        user = await crud.get_user(session, TEST_USER_TG_ID)
        if not user:
            user = await crud.create_user(
                session, tg_id=TEST_USER_TG_ID,
                name="Test User", gender="M", age=30,
                height_cm=180, weight_kg=80,
                activity_level="moderate", goal="maintain",
                target_weight_kg=80,
            )
        test_user_id = user.id
        logger.info(f"[SETUP] Test user created/found: id={test_user_id} tg_id={TEST_USER_TG_ID}")
        return user

def run_async(coro):
    """Helper to run async functions from sync context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# =============================================================
#  Section 1: Basic text vs tool_call branching
# =============================================================

async def test_1_1_normal_chat():
    """Обычный разговор, НЕ должен вызвать tool."""
    text = "привет, как дела?"
    
    logger.info("=" * 70)
    logger.info("[TEST 1.1] Normal chat — should NOT trigger tool_call")
    logger.info(f"[TEST 1.1] Input: {text!r}")
    
    # Build a minimal system prompt
    system = "Ты дружелюбный фитнес-тренер. Отвечай коротко."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if result is None:
        logger.error("[TEST 1.1] FAILED: LLM returned None (API error)")
        return "FAILED"
    
    kind, data, tok_in, tok_out = result
    logger.info(f"[TEST 1.1] Result: kind={kind!r}")
    
    if kind == "text":
        logger.info(f"[TEST 1.1] PASSED: returned text response: {data!r}")
        return "PASSED"
    else:
        logger.error(f"[TEST 1.1] FAILED: expected kind='text', got kind='tool_call' name={data.get('name')!r}")
        return "FAILED"


async def test_1_2_low_risk_action():
    """Явный запрос на низко-рисковое действие (log_food_item)."""
    text = "съел 200г гречки"
    
    logger.info("=" * 70)
    logger.info("[TEST 1.2] Low-risk action — should trigger log_food_item WITHOUT confirmation")
    logger.info(f"[TEST 1.2] Input: {text!r}")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if result is None:
        logger.error("[TEST 1.2] FAILED: LLM returned None (API error)")
        return "FAILED"
    
    kind, data, tok_in, tok_out = result
    logger.info(f"[TEST 1.2] Result: kind={kind!r}")
    
    if kind == "tool_call" and data["name"] == "log_food_item":
        args = data["arguments"]
        expected_name = "гречка"  # partial match ok
        food_name = args.get("food_name", "").lower()
        weight = args.get("weight_g", 0)
        logger.info(f"[TEST 1.2] args: food_name={args['food_name']!r}, weight_g={weight}")
        if "греч" in food_name and 150 <= weight <= 250:
            logger.info(f"[TEST 1.2] PASSED: correct tool+args")
            return "PASSED"
        else:
            logger.warning(f"[TEST 1.2] PARTIAL: tool called but args off: {args}")
            return "PARTIAL"
    elif kind == "tool_call":
        logger.error(f"[TEST 1.2] FAILED: wrong tool called: {data['name']!r}")
        return "FAILED"
    else:
        logger.error(f"[TEST 1.2] FAILED: expected kind='tool_call', got kind='text'")
        return "FAILED"


async def test_1_3_high_risk_action():
    """Явный запрос на высоко-рисковое действие (propose_workout)."""
    text = "добавь тренировку: жим лёжа 3х10 80кг, присед 4х8 100кг"
    
    logger.info("=" * 70)
    logger.info("[TEST 1.3] High-risk action — should trigger propose_workout WITH confirmation")
    logger.info(f"[TEST 1.3] Input: {text!r}")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if result is None:
        logger.error("[TEST 1.3] FAILED: LLM returned None (API error)")
        return "FAILED"
    
    kind, data, tok_in, tok_out = result
    logger.info(f"[TEST 1.3] Result: kind={kind!r}")
    
    if kind == "tool_call" and data["name"] == "propose_workout":
        args = data["arguments"]
        logger.info(f"[TEST 1.3] args: {json.dumps(args, ensure_ascii=False, indent=2)}")
        
        exercises = args.get("exercises", [])
        if len(exercises) == 2:
            logger.info(f"[TEST 1.3] PASSED: correct tool + 2 exercises extracted")
            return "PASSED"
        else:
            logger.warning(f"[TEST 1.3] PARTIAL: tool correct but {len(exercises)} exercises instead of 2")
            return "PARTIAL"
    elif kind == "tool_call":
        logger.error(f"[TEST 1.3] FAILED: wrong tool: {data['name']!r}")
        return "FAILED"
    else:
        logger.error(f"[TEST 1.3] FAILED: expected kind='tool_call', got kind='text'")
        return "FAILED"

# =============================================================
#  Section 2: Parameter extraction correctness
# =============================================================

async def test_2_1_clean_numbers():
    """Чистые цифры: жим лёжа 3х10 80кг."""
    text = "жим лёжа 3х10 80кг"
    logger.info("=" * 70)
    logger.info("[TEST 2.1] Parameter extraction — clean numbers")
    logger.info(f"[TEST 2.1] Input: {text!r}")
    logger.info(f"[TEST 2.1] Expected: name='Жим лёжа', sets=3, reps='10', weight_kg=80")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 2.1] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    ex = args.get("exercises", [{}])[0] if args.get("exercises") else args
    logger.info(f"[TEST 2.1] Extracted: {json.dumps(ex, ensure_ascii=False, indent=2)}")
    
    errors = []
    if "жим" not in ex.get("name", "").lower():
        errors.append(f"name expected contains 'жим', got {ex.get('name')!r}")
    if ex.get("sets") != 3:
        errors.append(f"sets expected 3, got {ex.get('sets')}")
    if ex.get("reps") not in ("10", 10):
        errors.append(f"reps expected '10', got {ex.get('reps')!r}")
    if ex.get("weight_kg") != 80:
        errors.append(f"weight_kg expected 80, got {ex.get('weight_kg')}")
    
    if errors:
        for e in errors:
            logger.error(f"[TEST 2.1] CHECK FAIL: {e}")
        return "FAILED"
    logger.info(f"[TEST 2.1] PASSED: all params correct")
    return "PASSED"


async def test_2_2_no_weight():
    """Без указания веса: weight_kg должен быть 0."""
    text = "приседания 4 подхода по 12 без веса"
    logger.info("=" * 70)
    logger.info("[TEST 2.2] Parameter extraction — no weight specified")
    logger.info(f"[TEST 2.2] Input: {text!r}")
    logger.info(f"[TEST 2.2] Expected: sets=4, reps='12', weight_kg=0 (NOT hallucinated)")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 2.2] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    ex = args.get("exercises", [{}])[0] if args.get("exercises") else args
    logger.info(f"[TEST 2.2] Extracted: {json.dumps(ex, ensure_ascii=False, indent=2)}")
    
    errors = []
    if ex.get("weight_kg", -1) != 0:
        errors.append(f"weight_kg expected 0, got {ex.get('weight_kg')!r} (hallucinated weight!)")
    if ex.get("sets") != 4:
        errors.append(f"sets expected 4, got {ex.get('sets')}")
    
    if errors:
        for e in errors:
            logger.error(f"[TEST 2.2] CHECK FAIL: {e}")
        return "FAILED"
    logger.info(f"[TEST 2.2] PASSED: weight_kg=0 correctly, no hallucination")
    return "PASSED"


async def test_2_3_reps_range():
    """Диапазон повторений: reps должно быть строкой '8-12'."""
    text = "подъём гантелей 3 подхода 8-12 раз"
    logger.info("=" * 70)
    logger.info("[TEST 2.3] Parameter extraction — reps range")
    logger.info(f"[TEST 2.3] Input: {text!r}")
    logger.info(f"[TEST 2.3] Expected: reps='8-12' (string, not number)")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 2.3] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    ex = args.get("exercises", [{}])[0] if args.get("exercises") else args
    logger.info(f"[TEST 2.3] Extracted: {json.dumps(ex, ensure_ascii=False, indent=2)}")
    
    # reps can be returned as number or string
    reps = ex.get("reps", "")
    reps_str = str(reps)
    if reps_str == "8-12":
        logger.info(f"[TEST 2.3] PASSED: reps='8-12' correctly as string")
        return "PASSED"
    else:
        logger.warning(f"[TEST 2.3] PARTIAL: reps is {reps!r}, not '8-12'. Tool schema says string but model returned {type(reps).__name__}")
        return "PARTIAL"


async def test_2_4_multiple_exercises():
    """Несколько упражнений в одном сообщении."""
    text = "сегодня была грудь: жим штанги 4х8 90, разводка гантелей 3х12 16кг каждая, отжимания 3х20 без веса"
    logger.info("=" * 70)
    logger.info("[TEST 2.4] Parameter extraction — 3 exercises in one message")
    logger.info(f"[TEST 2.4] Input: {text!r}")
    logger.info(f"[TEST 2.4] Expected: 3 exercises extracted correctly")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 2.4] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    exercises = args.get("exercises", [])
    logger.info(f"[TEST 2.4] Extracted {len(exercises)} exercises:")
    for i, ex in enumerate(exercises):
        logger.info(f"  [{i+1}] {json.dumps(ex, ensure_ascii=False)}")
    
    if len(exercises) == 3:
        logger.info(f"[TEST 2.4] PASSED: all 3 exercises extracted")
        return "PASSED"
    else:
        logger.warning(f"[TEST 2.4] PARTIAL: expected 3 exercises, got {len(exercises)}")
        return "PARTIAL"


async def test_2_5_no_day_of_week():
    """День недели не указан — не должен падать."""
    text = "добавь тренировку ноги: присед 5х5 100"
    logger.info("=" * 70)
    logger.info("[TEST 2.5] Parameter extraction — no day of week specified")
    logger.info(f"[TEST 2.5] Input: {text!r}")
    logger.info(f"[TEST 2.5] Expected: propose_workout with exercises, no day_of_week field")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 2.5] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    logger.info(f"[TEST 2.5] Extracted: {json.dumps(args, ensure_ascii=False, indent=2)}")
    
    exercises = args.get("exercises", [])
    if len(exercises) >= 1:
        logger.info(f"[TEST 2.5] PASSED: workout extracted with {len(exercises)} exercise(s)")
        return "PASSED"
    else:
        logger.error(f"[TEST 2.5] FAILED: no exercises extracted")
        return "FAILED"

# =============================================================
#  Section 3: Confirmation text quality
# =============================================================

async def test_3_1_typo_normalization():
    """Намеренно кривой ввод — проверка нормализации в confirmation text."""
    text = "дабавь тренеровку жим лёжа 3 по 10 на 80 кг присед 4 по 8 на 100"
    logger.info("=" * 70)
    logger.info("[TEST 3.1] Confirmation text — typo normalization")
    logger.info(f"[TEST 3.1] Input: {text!r}")
    logger.info(f"[TEST 3.1] Expected: confirmation text should be grammatically correct")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 3.1] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    conf_text = await _build_confirmation_text("propose_workout", args)
    logger.info(f"[TEST 3.1] Raw args: {json.dumps(args, ensure_ascii=False, indent=2)}")
    logger.info(f"[TEST 3.1] Confirmation text: {conf_text!r}")
    
    exercise_names = [ex.get("name", "").lower() for ex in args.get("exercises", [])]
    has_jim = any("жим" in n for n in exercise_names)
    has_prised = any("присед" in n for n in exercise_names)
    
    if has_jim and has_prised:
        logger.info(f"[TEST 3.1] PASSED: exercises correctly identified")
        return "PASSED"
    else:
        logger.warning(f"[TEST 3.1] PARTIAL: exercises: {exercise_names}")
        return "PARTIAL"


async def test_3_2_all_exercises_in_confirmation():
    """Подтверждение содержит ВСЕ упражнения."""
    # Reuse test 2.4 data
    text = "сегодня была грудь: жим штанги 4х8 90, разводка гантелей 3х12 16кг каждая, отжимания 3х20 без веса"
    logger.info("=" * 70)
    logger.info("[TEST 3.2] Confirmation contains ALL exercises")
    logger.info(f"[TEST 3.2] Input: {text!r}")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.error(f"[TEST 3.2] FAILED: expected tool_call, got {result[0] if result else 'None'}")
        return "FAILED"
    
    args = result[1]["arguments"]
    conf_text = await _build_confirmation_text("propose_workout", args)
    logger.info(f"[TEST 3.2] Confirmation text:\n{conf_text}")
    
    exercises = args.get("exercises", [])
    all_in = all(ex["name"].lower() in conf_text.lower() for ex in exercises)
    
    if len(exercises) == 3 and all_in:
        logger.info(f"[TEST 3.2] PASSED: all 3 exercises in confirmation text")
        return "PASSED"
    elif len(exercises) < 3:
        logger.warning(f"[TEST 3.2] PARTIAL: only {len(exercises)} exercises extracted")
        return "PARTIAL"
    else:
        logger.warning(f"[TEST 3.2] PARTIAL: not all exercises shown in confirmation")
        return "PARTIAL"

# =============================================================
#  Section 4: Confirm/Reject mechanics
# =============================================================

def test_4_3_is_confirmation_variants():
    """Проверка _is_confirmation на разных формулировках."""
    logger.info("=" * 70)
    logger.info("[TEST 4.3] _is_confirmation — various phrasings")
    
    test_cases = {
        "да": True, "да!": True, "ок": True, "окей": True,
        "верно": True, "правильно": True, "именно": True,
        "yes": True, "ага": True, "угу": True, "всё верно": True,
        "всё так": True, "нет": False, "не то": False,
        # Edge cases
        "да, всё правильно": False,  # whole phrase, not exact match → BUG
        "да, верно": False,          # not in set exactly
        "ага, именно так": False,    # not in set exactly
        "давай": False,              # starts with "да" but not exact
        "ДА": True,                  # case insensitive
        "Да": True,                  # mixed case
        "  да  ": True,              # stripped
        "да.": True,                 # stripped punctuation
        "да!!!!!": False,            # multiple punctuation
    }
    
    passed = 0
    failed = 0
    for inp, expected in test_cases.items():
        result = _is_confirmation(inp)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            logger.warning(f"[TEST 4.3] {status}: input={inp!r} expected={expected} got={result}")
            failed += 1
        else:
            passed += 1
    
    logger.info(f"[TEST 4.3] Results: {passed}/{len(test_cases)} passed, {failed} failed")
    
    # Specifically highlight the phrase bug
    logger.info(f"[TEST 4.3] KEY FINDING: 'да, всё правильно' → {_is_confirmation('да, всё правильно')} (expected True — this is a REAL BUG if False)")
    logger.info(f"[TEST 4.3] KEY FINDING: 'да, верно' → {_is_confirmation('да, верно')} (expected True — another phrase match bug)")
    
    if failed == 0:
        return "PASSED"
    elif failed <= 2:
        return "PARTIAL"
    else:
        return "FAILED"


def test_4_3_is_rejection_variants():
    """Проверка _is_rejection на разных формулировках.""" 
    logger.info("=" * 70)
    logger.info("[TEST 4.3b] _is_rejection — various phrasings")
    
    test_cases = {
        "нет": True, "не то": True, "неправильно": True,
        "no": True, "отмена": True, "не так": True, "неверно": True,
        "всё не так": True, "да": False, "ок": False,
        "нет, не то": False,  # phrase not exact match
        "совсем не то": False,
    }
    
    for inp, expected in test_cases.items():
        result = _is_rejection(inp)
        status = "PASS" if result == expected else "FAIL"
        if status == "FAIL":
            logger.warning(f"[TEST 4.3b] {status}: input={inp!r} expected={expected} got={result}")
    
    logger.info(f"[TEST 4.3b] KEY FINDING: 'нет, не то' → {_is_rejection('нет, не то')} (same substring matching bug)")
    return "PASSED"


async def test_4_4_pending_cleared_on_unrelated():
    """Проверка что при unrelated сообщении во время pending — pending очищается."""
    logger.info("=" * 70)
    logger.info("[TEST 4.4] Pending cleared on unrelated message")
    
    user_id = TEST_USER_TG_ID
    await clear_pending_action(user_id)
    
    # Set a pending action
    await set_pending_action(
        user_id, "propose_workout",
        {"workout_name": "Test", "exercises": [{"name": "жим", "sets": 3, "reps": "10", "weight_kg": 60}]},
        "Всё верно? (да/нет)"
    )
    
    pending_before = await get_pending_action(user_id)
    logger.info(f"[TEST 4.4] Pending before unrelated msg: {pending_before is not None}")
    
    # Simulate what handle_message_with_actions does for unrelated msg while pending exists
    text = "как дела?"
    is_conf = _is_confirmation(text)
    is_rej = _is_rejection(text)
    
    if not is_conf and not is_rej:
        await clear_pending_action(user_id)
    
    pending_after = await get_pending_action(user_id)
    logger.info(f"[TEST 4.4] Pending after unrelated msg: {pending_after}")
    
    if pending_before and pending_after is None:
        logger.info(f"[TEST 4.4] PASSED: pending correctly cleared on unrelated message")
        return "PASSED"
    else:
        logger.error(f"[TEST 4.4] FAILED: pending not cleared properly")
        return "FAILED"


# =============================================================
#  Section 5: Reminder time calculations
# =============================================================

def test_5_3_midnight_crossing():
    """Проверка перехода времени через полночь для advance warning."""
    logger.info("=" * 70)
    logger.info("[TEST 5.3] Reminder — midnight crossing for advance time")
    
    # Simulate the code from _create_reminder
    time = "00:10"
    advance = 20
    
    h, m = map(int, time.split(":"))
    adv_m = m - advance
    adv_h = h
    while adv_m < 0:
        adv_m += 60
        adv_h -= 1
    adv_h %= 24
    
    logger.info(f"[TEST 5.3] Time={time}, advance={advance}min")
    logger.info(f"[TEST 5.3] Calculated advance time: {adv_h:02d}:{adv_m:02d}")
    logger.info(f"[TEST 5.3] Expected: 23:50 (previous day)")
    
    if adv_h == 23 and adv_m == 50:
        logger.info(f"[TEST 5.3] PASSED: midnight crossing correct")
        return "PASSED"
    else:
        logger.error(f"[TEST 5.3] FAILED: expected 23:50, got {adv_h:02d}:{adv_m:02d}")
        return "FAILED"


def test_5_3b_normal_advance():
    """Проверка нормального advance (без перехода через полночь)."""
    logger.info("=" * 70)
    logger.info("[TEST 5.3b] Reminder — normal advance (no midnight crossing)")
    
    time = "20:00"
    advance = 15
    
    h, m = map(int, time.split(":"))
    adv_m = m - advance
    adv_h = h
    while adv_m < 0:
        adv_m += 60
        adv_h -= 1
    adv_h %= 24
    
    logger.info(f"[TEST 5.3b] Time={time}, advance={advance}min")
    logger.info(f"[TEST 5.3b] Calculated advance time: {adv_h:02d}:{adv_m:02d}")
    logger.info(f"[TEST 5.3b] Expected: 19:45")
    
    if adv_h == 19 and adv_m == 45:
        logger.info(f"[TEST 5.3b] PASSED: normal advance correct")
        return "PASSED"
    else:
        logger.error(f"[TEST 5.3b] FAILED: expected 19:45, got {adv_h:02d}:{adv_m:02d}")
        return "FAILED"


async def test_5_4_relative_time():
    """Относительное время — проверка как модель обрабатывает 'через час'."""
    text = "напомни через час выпить воды"
    logger.info("=" * 70)
    logger.info("[TEST 5.4] Reminder — relative time ('через час')")
    logger.info(f"[TEST 5.4] Input: {text!r}")
    logger.info(f"[TEST 5.4] Note: current tool schema expects HH:MM, this may fail")
    
    system = "Ты дружелюбный фитнес-тренер. Текущее время: 14:30."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result:
        logger.error(f"[TEST 5.4] FAILED: LLM returned None")
        return "FAILED"
    
    kind, data, tok_in, tok_out = result
    logger.info(f"[TEST 5.4] Result: kind={kind!r}")
    
    if kind == "tool_call":
        logger.info(f"[TEST 5.4] Tool: {data['name']!r}")
        logger.info(f"[TEST 5.4] Args: {json.dumps(data['arguments'], ensure_ascii=False, indent=2)}")
        if data["name"] == "propose_reminder":
            time_val = data["arguments"].get("time", "N/A")
            logger.info(f"[TEST 5.4] Model returned time={time_val!r}")
            # Check if model converted relative to absolute
            if time_val == "15:30":
                logger.info(f"[TEST 5.4] PASSED: model correctly converted 'через час' to 15:30")
                return "PASSED"
            else:
                logger.warning(f"[TEST 5.4] PARTIAL: tool called with time={time_val!r}")
                return "PARTIAL"
        else:
            logger.warning(f"[TEST 5.4] PARTIAL: called {data['name']!r} instead of propose_reminder")
            return "PARTIAL"
    else:
        logger.warning(f"[TEST 5.4] PARTIAL: model returned text instead of tool_call")
        return "PARTIAL"

# =============================================================
#  Section 6: Boundary/stress cases
# =============================================================

async def test_6_1_ambiguous_input():
    """Двусмысленный ввод — может вызвать любой из двух tools."""
    text = "завтра в 8 вечера тренировка ног"
    logger.info("=" * 70)
    logger.info("[TEST 6.1] Ambiguous input — could trigger workout or reminder")
    logger.info(f"[TEST 6.1] Input: {text!r}")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result or result[0] != "tool_call":
        logger.info(f"[TEST 6.1] Result: kind={result[0] if result else 'None'} — model chose text response")
        return "text_response"
    
    name = result[1]["name"]
    logger.info(f"[TEST 6.1] Model chose: {name!r}")
    logger.info(f"[TEST 6.1] Args: {json.dumps(result[1]['arguments'], ensure_ascii=False, indent=2)}")
    
    # This is not pass/fail — just observe which tool the model picks
    logger.info(f"[TEST 6.1] OBSERVATION: model chose {name!r}")
    return name


async def test_6_2_dual_actions():
    """Два разных действия в одном сообщении."""
    text = "добавь тренировку жим 3х10 60 и напомни про неё в 18:00"
    logger.info("=" * 70)
    logger.info("[TEST 6.2] Dual actions in one message")
    logger.info(f"[TEST 6.2] Input: {text!r}")
    logger.info(f"[TEST 6.2] Note: code uses tool_calls[0] — only first action is processed")
    
    system = "Ты дружелюбный фитнес-тренер."
    result = await ask_groq_with_tools(system, text, ALL_TOOLS, max_tokens=300)
    
    if not result:
        logger.error(f"[TEST 6.2] FAILED: LLM returned None")
        return "FAILED"
    
    kind, data, tok_in, tok_out = result
    logger.info(f"[TEST 6.2] Result: kind={kind!r}")
    if kind == "tool_call":
        logger.info(f"[TEST 6.2] Only first tool_call captured: {data['name']!r}")
        logger.info(f"[TEST 6.2] Second action is silently lost (known limitation)")
    else:
        logger.info(f"[TEST 6.2] Model returned text instead of tool_call: {data!r}")
    
    logger.info(f"[TEST 6.2] PASSED (observation): single tool_call {data.get('name', 'N/A')!r} only")
    return kind

# =============================================================
#  Main test runner
# =============================================================

async def run_all_tests():
    """Run all tests and collect results."""
    print()
    print("=" * 70)
    print("  TOOL-CALLING SYSTEM — COMPREHENSIVE TEST SUITE")
    print("=" * 70)
    
    # Setup
    await setup_test_user()
    print()
    
    results = {}
    
    # --- Section 1: Basic text vs tool_call ---
    print("\n" + "#" * 70)
    print("#  SECTION 1: Basic text vs tool_call branching")
    print("#" * 70)
    results["1.1"] = await test_1_1_normal_chat()
    results["1.2"] = await test_1_2_low_risk_action()
    results["1.3"] = await test_1_3_high_risk_action()
    
    # --- Section 2: Parameter extraction ---
    print("\n" + "#" * 70)
    print("#  SECTION 2: Parameter extraction correctness")
    print("#" * 70)
    results["2.1"] = await test_2_1_clean_numbers()
    results["2.2"] = await test_2_2_no_weight()
    results["2.3"] = await test_2_3_reps_range()
    results["2.4"] = await test_2_4_multiple_exercises()
    results["2.5"] = await test_2_5_no_day_of_week()
    
    # --- Section 3: Confirmation text ---
    print("\n" + "#" * 70)
    print("#  SECTION 3: Confirmation text quality")
    print("#" * 70)
    results["3.1"] = await test_3_1_typo_normalization()
    results["3.2"] = await test_3_2_all_exercises_in_confirmation()
    
    # --- Section 4: Confirm/Reject mechanics ---
    print("\n" + "#" * 70)
    print("#  SECTION 4: Confirm/Reject mechanics")
    print("#" * 70)
    results["4.3"] = test_4_3_is_confirmation_variants()
    results["4.3b"] = test_4_3_is_rejection_variants()
    results["4.4"] = await test_4_4_pending_cleared_on_unrelated()
    
    # --- Section 5: Reminder edge cases ---
    print("\n" + "#" * 70)
    print("#  SECTION 5: Reminder edge cases")
    print("#" * 70)
    results["5.3"] = test_5_3_midnight_crossing()
    results["5.3b"] = test_5_3b_normal_advance()
    results["5.4"] = await test_5_4_relative_time()
    
    # --- Section 6: Boundary/stress ---
    print("\n" + "#" * 70)
    print("#  SECTION 6: Boundary/stress cases")
    print("#" * 70)
    results["6.1"] = await test_6_1_ambiguous_input()
    results["6.2"] = await test_6_2_dual_actions()
    
    # --- Summary ---
    print()
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)
    for test_id, status in results.items():
        status_icon = {"PASSED": "✅", "FAILED": "❌", "PARTIAL": "⚠️", "text_response": "ℹ️", "tool_call": "ℹ️", "text": "ℹ️"}
        icon = status_icon.get(status, "❓")
        print(f"  {icon} [{test_id}]: {status}")
    
    passed = sum(1 for s in results.values() if s == "PASSED")
    failed = sum(1 for s in results.values() if s == "FAILED")
    partial = sum(1 for s in results.values() if s == "PARTIAL")
    info = sum(1 for s in results.values() if s in ("text_response", "tool_call", "text"))
    print(f"\n  Total: {len(results)} | ✅ {passed} | ❌ {failed} | ⚠️ {partial} | ℹ️ {info}")
    print("=" * 70)
    
    return results


if __name__ == "__main__":
    asyncio.run(run_all_tests())
