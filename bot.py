
import asyncio
import html
import sqlite3
import json
import re
import random
import logging
from pathlib import Path
from uuid import uuid4
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional, Dict, List, Any, Tuple
import yt_dlp

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
from database import (
    count_all_videos,
    count_user_vocabulary,
    count_videos_today,
    create_premium_payload,
    delete_vocabulary_word,
    get_current_streak,
    get_due_vocabulary,
    get_learning_dates,
    get_longest_streak,
    get_progress_stats,
    get_saved_word_by_text,
    get_timezone_by_name,
    get_user_local_date,
    get_user_timezone,
    get_or_create_user,
    get_user_vocabulary,
    get_vocabulary_word,
    get_video_history,
    get_video_lesson,
    init_database,
    is_privileged_user,
    is_valid_premium_payment,
    record_premium_payment,
    reserve_ai_teacher_request,
    save_lesson_candidate,
    save_streak_milestone,
    save_video,
    save_word,
    schedule_word_review,
    stage_lesson_candidates,
    update_user_level,
    update_user_settings,
)
from video_service import (
    extract_best_subtitle,
    get_subtitles,
    get_video_title,
    is_valid_url,
    parse_vtt,
    try_subtitle_tracks,
)
from lesson_service import (
    analyze_transcript,
    format_lesson as format_lesson_service,
    openai_client,
    validate_analysis_payload,
)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# Initialize bot
# ============================================================

bot = Bot(token=config.BOT_TOKEN)

storage = MemoryStorage()

dp = Dispatcher(storage=storage)

# ============================================================
# State Management
# ============================================================

class UserState(StatesGroup):
    choosing_action = State()
    choosing_level = State()
    onboarding_level = State()
    onboarding_goal = State()
    onboarding_dialect = State()
    onboarding_pace = State()
    waiting_for_url = State()
    choosing_vocabulary_page = State()
    practicing_word = State()
    waiting_for_reminder_time = State()
    waiting_for_timezone = State()
    ai_teacher_chat = State()


# ============================================================
# Onboarding / Profile Setup
# ============================================================

ONBOARDING_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
ONBOARDING_GOALS = {
    "🗣 Speaking": "Speaking",
    "🎯 IELTS": "IELTS",
    "📚 General English": "General English",
    "💼 Business English": "Business English",
}
ONBOARDING_DIALECTS = {
    "🇺🇸 American English": "American",
    "🇬🇧 British English": "British",
}
ONBOARDING_PACES = {
    "🌱 Relaxed": (10, 5),
    "⚡ Regular": (20, 10),
    "🔥 Intensive": (20, 10),
}


def init_onboarding_storage() -> None:
    """Add persistent onboarding state without requiring database.py changes."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "onboarding_completed" not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0"
            )

        marker = conn.execute(
            "SELECT 1 FROM bot_migrations WHERE name = ?",
            ("onboarding_v1",),
        ).fetchone()
        if not marker:
            # Existing accounts keep their current profiles and skip the new onboarding.
            conn.execute(
                "UPDATE users SET onboarding_completed = 1 "
                "WHERE onboarding_completed = 0"
            )
            conn.execute(
                "INSERT INTO bot_migrations (name) VALUES (?)",
                ("onboarding_v1",),
            )

        conn.execute(
            "CREATE TABLE IF NOT EXISTS onboarding_profiles ("
            "telegram_id INTEGER PRIMARY KEY, "
            "pace TEXT NOT NULL DEFAULT 'Regular', "
            "completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)"
            ")"
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Could not initialize onboarding storage")
        raise
    finally:
        conn.close()


def is_onboarding_completed(telegram_id: int) -> bool:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT onboarding_completed FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def complete_onboarding(telegram_id: int, pace: str) -> bool:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE users SET onboarding_completed = 1, last_active = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO onboarding_profiles (telegram_id, pace) VALUES (?, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET pace = excluded.pace, "
            "completed_at = CURRENT_TIMESTAMP",
            (telegram_id, pace),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Could not complete onboarding")
        return False
    finally:
        conn.close()


def get_onboarding_pace(telegram_id: int) -> str:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT pace FROM onboarding_profiles WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row[0] if row else "Regular"
    finally:
        conn.close()


def get_onboarding_level_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="A1"), types.KeyboardButton(text="A2")],
            [types.KeyboardButton(text="B1"), types.KeyboardButton(text="B2")],
            [types.KeyboardButton(text="C1"), types.KeyboardButton(text="C2")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_onboarding_goal_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🗣 Speaking"), types.KeyboardButton(text="🎯 IELTS")],
            [types.KeyboardButton(text="📚 General English")],
            [types.KeyboardButton(text="💼 Business English")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_onboarding_dialect_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🇺🇸 American English")],
            [types.KeyboardButton(text="🇬🇧 British English")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_onboarding_pace_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🌱 Relaxed")],
            [types.KeyboardButton(text="⚡ Regular")],
            [types.KeyboardButton(text="🔥 Intensive")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def start_onboarding(message: types.Message, state: FSMContext) -> None:
    first_name = html.escape(message.from_user.first_name or "there") if message.from_user else "there"
    await message.answer(
        f"<b>Welcome to Speakly, {first_name}! 👋</b>\n\n"
        "Let's personalize your English learning experience.\n\n"
        "It will only take a few seconds.",
        parse_mode="HTML",
        reply_markup=get_onboarding_level_keyboard(),
    )
    await state.set_state(UserState.onboarding_level)


# ============================================================
# AI Analysis
# ============================================================

# ============================================================
# Formatting
# ============================================================

def _legacy_format_lesson(
    analysis: Dict[str, Any],
    video_title: str
) -> str:
    """Format analysis results as Telegram message."""

    if not analysis:

        return (
            "❌ Could not analyze transcript. "
            "Please try another video."
        )

    message = (
        "🎬 <b>English breakdown</b>\n"
    )

    message += (
        f"<b>{html.escape(video_title)}</b>\n"
        f"📊 Level: {html.escape(str(analysis.get('estimated_level') or 'Not estimated'))}\n\n"
    )

    message += (
        "━" * 30 +
        "\n\n"
    )

    # --------------------------------------------------------
    # Expressions
    # --------------------------------------------------------

    expressions = analysis.get(
        "expressions",
        []
    )

    if expressions:

        message += (
            "🧠 <b>Interesting expressions</b>\n\n"
        )

        for expr in expressions[:10]:

            expression = html.escape(
                str(
                    expr.get(
                        "expression",
                        ""
                    )
                )
            )

            message += (
                f"🔥 <b>{expression}</b>\n"
            )

            if expr.get("transcription"):

                message += (
                    "/"
                    + html.escape(
                        str(
                            expr.get(
                                "transcription"
                            )
                        )
                    )
                    + "/\n"
                )

            message += (
                "🇷🇺 "
                + html.escape(
                    str(
                        expr.get(
                            "translation",
                            ""
                        )
                    )
                )
                + "\n"
            )

            message += (
                "📈 "
                + html.escape(
                    str(
                        expr.get(
                            "cefr",
                            ""
                        )
                    )
                )
                + "\n"
            )

            if expr.get("example"):

                message += (
                    '💬 <i>"'
                    + html.escape(
                        str(
                            expr.get(
                                "example",
                                ""
                            )
                        )
                    )
                    + '"</i>\n'
                )

            if expr.get("explanation"):

                message += (
                    "🧩 "
                    + html.escape(
                        str(
                            expr.get(
                                "explanation",
                                ""
                            )
                        )
                    )
                    + "\n"
                )

            message += "\n"

        message += (
            "━" * 30 +
            "\n\n"
        )

    # --------------------------------------------------------
    # Vocabulary
    # --------------------------------------------------------

    vocabulary = analysis.get(
        "vocabulary",
        []
    )

    if vocabulary:

        message += (
            "📚 <b>Vocabulary</b>\n\n"
        )

        for i, word in enumerate(
            vocabulary[:10],
            1
        ):

            message += (
                f"<b>{i}. "
                + html.escape(
                    str(
                        word.get(
                            "word",
                            ""
                        )
                    )
                )
                + "</b>\n"
            )

            if word.get("transcription"):

                message += (
                    "/"
                    + html.escape(
                        str(
                            word.get(
                                "transcription"
                            )
                        )
                    )
                    + "/\n"
                )

            message += (
                "🇷🇺 "
                + html.escape(
                    str(
                        word.get(
                            "translation",
                            ""
                        )
                    )
                )
                + "\n"
            )

            message += (
                "📈 "
                + html.escape(
                    str(
                        word.get(
                            "cefr",
                            ""
                        )
                    )
                )
                + "\n"
            )

            if word.get("example"):

                message += (
                    '💬 <i>"'
                    + html.escape(
                        str(
                            word.get(
                                "example",
                                ""
                            )
                        )
                    )
                    + '"</i>\n'
                )

            message += "\n"

        message += (
            "━" * 30 +
            "\n\n"
        )

    # --------------------------------------------------------
    # Natural English
    # --------------------------------------------------------

    natural_english = analysis.get(
        "natural_english",
        []
    )

    if natural_english:

        message += (
            "🗣️ <b>Natural English</b>\n\n"
        )

        for item in natural_english[:5]:

            phrase = html.escape(
                str(
                    item.get(
                        "phrase",
                        ""
                    )
                )
            )

            meaning = html.escape(
                str(
                    item.get(
                        "meaning",
                        ""
                    )
                )
            )

            message += (
                f"🔥 <b>{phrase}</b> = {meaning}\n"
            )

            if item.get("explanation"):

                message += (
                    "💡 "
                    + html.escape(
                        str(
                            item.get(
                                "explanation",
                                ""
                            )
                        )
                    )
                    + "\n"
                )

            message += "\n"

        message += (
            "━" * 30 +
            "\n\n"
        )

    recommendations = analysis.get("native_recommendations", [])
    if recommendations:
        message += "🇺🇸 <b>Native English (extra suggestions)</b>\n\n"
        for item in recommendations[:5]:
            instead_of = html.escape(str(item.get("instead_of", "")))
            natural = html.escape(str(item.get("natural", "")))
            meaning = html.escape(str(item.get("meaning", "")))
            message += f"Instead of <i>{instead_of}</i>: <b>{natural}</b>"
            if meaning:
                message += f" = {meaning}"
            if item.get("example"):
                message += f"\n💬 <i>{html.escape(str(item['example']))}</i>"
            message += "\n\n"

    if analysis.get("ielts"):
        ielts = analysis["ielts"]
        message += "🎓 <b>IELTS Focus</b>\n\n"
        if ielts.get("estimated_level"):
            message += f"📈 Estimated level: {html.escape(str(ielts['estimated_level']))}\n\n"
        for section in ("vocabulary", "collocations", "speaking_expressions", "academic_alternatives"):
            items = ielts.get(section, []) if isinstance(ielts, dict) else []
            if items:
                message += f"<b>{html.escape(section.replace('_', ' ').title())}</b>\n"
                for item in items[:5]:
                    if isinstance(item, dict):
                        value = item.get("word") or item.get("phrase") or item.get("expression") or item.get("alternative") or ""
                        detail = item.get("meaning") or item.get("example") or ""
                        message += f"• {html.escape(str(value))}"
                        if detail:
                            message += f" — {html.escape(str(detail))}"
                        message += "\n"
                    elif isinstance(item, str):
                        message += f"• {html.escape(item)}\n"
                message += "\n"
        quiz = ielts.get("mini_quiz", [])
        if quiz:
            message += "<b>Mini quiz</b>\n"
            for index, item in enumerate(quiz[:3], 1):
                if isinstance(item, dict):
                    question = html.escape(str(item.get("question", "")))
                    answer = html.escape(str(item.get("answer", "")))
                    message += f"{index}. {question}\nAnswer: {answer}\n"
            message += "\n<b>IELTS Speaking Practice</b>\n"
            for index, question in enumerate(ielts.get("practice_questions", [])[:5], 1):
                message += f"{index}. {html.escape(str(question))}\n"

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    summary = analysis.get(
        "summary"
    )

    if summary:

        message += (
            "📖 <b>Summary</b>\n\n"
        )

        message += html.escape(
            str(summary)
        )

    return message


def format_lesson(analysis: Dict[str, Any], video_title: str) -> str:
    """Compatibility wrapper; lesson rendering lives in lesson_service."""

    return format_lesson_service(analysis, video_title)


# ============================================================
# Keyboards
# ============================================================

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(
                    text="🎬 Analyze Video"
                ),
                types.KeyboardButton(
                    text="📖 Review Today"
                ),
            ],
            [
                types.KeyboardButton(
                    text="📚 My Words"
                ),
                types.KeyboardButton(
                    text="💎 Word of the Day"
                ),
            ],
            [
                types.KeyboardButton(
                    text="🎯 Daily Challenge"
                ),
                types.KeyboardButton(
                    text="🤖 AI Teacher"
                ),
            ],
            [
                types.KeyboardButton(
                    text="📊 Progress"
                ),
                types.KeyboardButton(
                    text="🔥 Streak"
                ),
            ],
            [
                types.KeyboardButton(
                    text="🎬 History"
                ),
                types.KeyboardButton(
                    text="⚙️ Settings"
                ),
            ],
            [
                types.KeyboardButton(
                    text="⭐ Premium"
                )
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_level_keyboard() -> ReplyKeyboardMarkup:

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="A1"),
                types.KeyboardButton(text="A2"),
            ],
            [
                types.KeyboardButton(text="B1"),
                types.KeyboardButton(text="B2"),
            ],
            [
                types.KeyboardButton(text="C1"),
                types.KeyboardButton(text="C2"),
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="A1"), types.KeyboardButton(text="A2"),
             types.KeyboardButton(text="B1"), types.KeyboardButton(text="B2"),
             types.KeyboardButton(text="C1"), types.KeyboardButton(text="C2")],
            [types.KeyboardButton(text="Goal: IELTS"), types.KeyboardButton(text="Goal: Speaking")],
            [types.KeyboardButton(text="Goal: General"), types.KeyboardButton(text="Goal: Business")],
            [types.KeyboardButton(text="Goal: American English")],
            [types.KeyboardButton(text="Dialect: American"), types.KeyboardButton(text="Dialect: British")],
            [types.KeyboardButton(text="Difficulty: Adaptive"), types.KeyboardButton(text="Difficulty: Advanced")],
            [types.KeyboardButton(text="Review target: 10"), types.KeyboardButton(text="Review target: 20")],
            [types.KeyboardButton(text="New word target: 5"), types.KeyboardButton(text="New word target: 10")],
            [types.KeyboardButton(text="🔔 Enable reminders"), types.KeyboardButton(text="🔕 Disable reminders")],
            [types.KeyboardButton(text="⏰ Set reminder time")],
            [types.KeyboardButton(text="🌐 Set timezone")],
            [types.KeyboardButton(text="◀️ Back")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_back_keyboard() -> ReplyKeyboardMarkup:

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(
                    text="◀️ Back"
                )
            ]
        ],
        resize_keyboard=True,
    )


def get_add_to_vocabulary_inline(
    word_id: int
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Add to vocabulary",
                    callback_data=f"add_word_{word_id}",
                )
            ]
        ]
    )


def get_vocabulary_navigation_inline(
    page: int,
    total_pages: int,
    word_ids: Optional[List[int]] = None,
) -> InlineKeyboardMarkup:

    keyboard = [
        [
            InlineKeyboardButton(
                text="🔎 Details",
                callback_data=f"word_view_{word_id}",
            ),
            InlineKeyboardButton(
                text="🗑 Delete",
                callback_data=f"word_delete_{word_id}",
            ),
        ]
        for word_id in (word_ids or [])
    ]

    navigation = []

    if page > 1:

        navigation.append(
            InlineKeyboardButton(
                text="◀️ Previous",
                callback_data=f"vocab_page_{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"{page}/{total_pages}",
            callback_data="vocab_page_info",
        )
    )

    if page < total_pages:

        navigation.append(
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"vocab_page_{page + 1}",
            )
        )

    keyboard.append(navigation)
    keyboard.append([
        InlineKeyboardButton(
            text="🧠 Practice",
            callback_data=f"practice_start_{page}",
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


def get_history_keyboard(
    videos: List[Dict[str, Any]], page: int, total_pages: int
) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            text=f"Open: {str(video['title'] or 'Untitled')[:40]}",
            callback_data=f"history_open_{video['id']}",
        )]
        for video in videos
    ]
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"history_page_{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="history_info"))
    if page < total_pages:
        navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"history_page_{page + 1}"))
    keyboard.append(navigation)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_challenge_keyboard(question_index: int, options: List[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{chr(65 + index)}. {option[:45]}",
                callback_data=f"challenge_answer_{question_index}_{index}",
            )]
            for index, option in enumerate(options)
        ]
    )


def get_word_of_day_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Save", callback_data="wordday_save"),
                InlineKeyboardButton(text="Practice", callback_data="wordday_practice"),
            ]
        ]
    )


async def send_wordday_practice_prompt(
    message: types.Message,
    practice_prompt: str,
) -> None:
    """Transition a text or photo Word-of-the-Day message into practice."""

    if message.photo:
        await message.edit_caption(
            caption="🧠 Practice started in the message below.",
            reply_markup=None,
        )
        await message.answer(
            practice_prompt,
            parse_mode="HTML",
            reply_markup=get_practice_keyboard(),
        )
    else:
        await message.edit_text(
            practice_prompt,
            parse_mode="HTML",
            reply_markup=get_practice_keyboard(),
        )


def get_lesson_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Save All", callback_data="lesson_save_all")],
            [InlineKeyboardButton(text="🧠 Start Practice", callback_data="lesson_practice")],
            [InlineKeyboardButton(text="🎓 IELTS Mode", callback_data="lesson_ielts")],
            [InlineKeyboardButton(text="🔄 Analyze Again", callback_data="lesson_again")],
        ]
    )


def get_premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"⭐ Buy Premium · {config.PREMIUM_PRICE_STARS} Stars",
                callback_data="premium_buy",
            )]
        ]
    )


def get_practice_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Show answer",
                    callback_data="show_answer",
                )
            ]
        ]
    )


def get_multiple_choice_keyboard(options: List[str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{chr(65 + index)}. {option[:45]}",
                                  callback_data=f"exercise_choice_{index}")]
            for index, option in enumerate(options)
        ]
    )


def build_practice_prompt(
    word: Dict[str, Any],
    exercise_type: str,
    vocabulary: List[Dict[str, Any]],
) -> Tuple[str, Optional[InlineKeyboardMarkup], Optional[int]]:
    english = html.escape(str(word.get("word", "")))
    translation = html.escape(str(word.get("translation", "")))
    example = html.escape(str(word.get("example") or ""))


    if exercise_type == "translation":
        return f"🇷🇺 Translate into English:\n\n<b>{translation}</b>", get_practice_keyboard(), None

    if exercise_type == "multiple_choice":
        distractors = [
            str(item["translation"])
            for item in vocabulary
            if item.get("id") != word.get("id") and item.get("translation")
        ]
        options = list(dict.fromkeys([str(word["translation"]), *distractors]))[:4]
        while len(options) < 4:
            options.append("I don't know")
        random.shuffle(options)
        correct_index = options.index(str(word["translation"]))
        text = f"What does <b>{english}</b> mean?"
        return text, get_multiple_choice_keyboard(options), correct_index

    if exercise_type == "fill_blank" and example:
        blanked = re.sub(re.escape(str(word["word"])), "______", example, count=1, flags=re.IGNORECASE)
        if blanked != example:
            return f"✍️ Fill in the blank:\n\n<i>{html.escape(blanked)}</i>", get_practice_keyboard(), None

    if exercise_type == "context" and example:
        return (
            f"🗣 What does <b>{english}</b> mean in this sentence?\n\n"
            f"<i>{html.escape(example)}</i>",
            get_practice_keyboard(),
            None,
        )

    return f"🧠 Recall the meaning:\n\n<b>{english}</b>", get_practice_keyboard(), None


def get_practice_feedback_keyboard(word_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Again", callback_data=f"feedback_again_{word_id}"),
                InlineKeyboardButton(text="🟡 Hard", callback_data=f"feedback_hard_{word_id}"),
                InlineKeyboardButton(text="🟢 Easy", callback_data=f"feedback_easy_{word_id}"),
            ]
        ]
    )


def get_review_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Start Review", callback_data="review_start")]
        ]
    )


def get_word_detail_keyboard(word_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Delete", callback_data=f"word_delete_{word_id}")]
        ]
    )


# ============================================================
# Command Handlers
# ============================================================

@dp.message(Command("start"))
async def start_command(
    message: types.Message,
    state: FSMContext
):

    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    user = get_or_create_user(telegram_id)

    if not is_onboarding_completed(telegram_id):
        await start_onboarding(message, state)
        return

    first_name = html.escape(message.from_user.first_name or "there")
    welcome_text = f"""
<b>Your English Studio</b>

Welcome back, {first_name}.

Short, personal practice that builds over time.

<b>Your level:</b> {html.escape(str(user["level"]))}
<b>Your focus:</b> {html.escape(str(user["learning_goal"]))}

Choose a session below.
"""

    image_path = Path(__file__).resolve().parent / "assets" / "welcome_study.png"
    if image_path.is_file():
        await message.answer_photo(
            photo=FSInputFile(image_path),
            caption=welcome_text,
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(),
        )
    else:
        await message.answer(
            welcome_text,
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(),
        )

    await state.set_state(UserState.choosing_action)


@dp.message(UserState.onboarding_level, F.text.in_(ONBOARDING_LEVELS))
async def onboarding_level_handler(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return

    update_user_level(message.from_user.id, message.text)
    await state.update_data(onboarding_level=message.text)
    await message.answer(
        "<b>What's your main goal? 🎯</b>\n\n"
        "Choose what you want Speakly to focus on most.",
        parse_mode="HTML",
        reply_markup=get_onboarding_goal_keyboard(),
    )
    await state.set_state(UserState.onboarding_goal)


@dp.message(UserState.onboarding_goal, F.text.in_(list(ONBOARDING_GOALS.keys())))
async def onboarding_goal_handler(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return

    goal = ONBOARDING_GOALS[message.text]
    update_user_settings(message.from_user.id, learning_goal=goal)
    await state.update_data(onboarding_goal=goal)
    await message.answer(
        "<b>Which English do you want to learn? 🇺🇸</b>\n\n"
        "This changes vocabulary, examples, expressions and pronunciation focus.",
        parse_mode="HTML",
        reply_markup=get_onboarding_dialect_keyboard(),
    )
    await state.set_state(UserState.onboarding_dialect)


@dp.message(UserState.onboarding_dialect, F.text.in_(list(ONBOARDING_DIALECTS.keys())))
async def onboarding_dialect_handler(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return

    dialect = ONBOARDING_DIALECTS[message.text]
    update_user_settings(message.from_user.id, dialect=dialect)
    await state.update_data(onboarding_dialect=dialect)
    await message.answer(
        "<b>How intense should your learning be? 🔥</b>\n\n"
        "Choose the pace that feels right for you.",
        parse_mode="HTML",
        reply_markup=get_onboarding_pace_keyboard(),
    )
    await state.set_state(UserState.onboarding_pace)


@dp.message(UserState.onboarding_pace, F.text.in_(list(ONBOARDING_PACES.keys())))
async def onboarding_pace_handler(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return

    pace = message.text
    pace_name = pace.split(" ", 1)[1]
    review_target, new_word_target = ONBOARDING_PACES[pace]
    telegram_id = message.from_user.id

    update_user_settings(
        telegram_id,
        daily_review_target=review_target,
        daily_new_word_target=new_word_target,
    )

    data = await state.get_data()
    level = data.get("onboarding_level", get_or_create_user(telegram_id)["level"])
    goal = data.get("onboarding_goal", get_or_create_user(telegram_id)["learning_goal"])
    dialect = data.get("onboarding_dialect", get_or_create_user(telegram_id)["dialect"])

    if not complete_onboarding(telegram_id, pace_name):
        await message.answer("Something went wrong while saving your profile. Please send /start and try again.")
        await state.clear()
        return

    await message.answer(
        "<b>🎉 Your Speakly profile is ready!</b>\n\n"
        f"📈 <b>Level:</b> {html.escape(str(level))}\n"
        f"🎯 <b>Goal:</b> {html.escape(str(goal))}\n"
        f"🌎 <b>English:</b> {html.escape(str(dialect))}\n"
        f"🔥 <b>Pace:</b> {html.escape(pace_name)}\n\n"
        "Your lessons will now adapt to your preferences.",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(),
    )
    await state.set_state(UserState.choosing_action)


@dp.message(Command("help"))
async def help_command(
    message: types.Message
):

    help_text = """
<b>How to use this bot:</b>

1️⃣ <b>Analyze Video</b>

Send a YouTube link. I'll extract subtitles and create a lesson.

2️⃣ <b>My Words</b>

View all your saved vocabulary with examples.

3️⃣ <b>Practice</b>

Use meaning, translation, multiple-choice, fill-in-the-blank, and context exercises.

4️⃣ <b>Review Today</b>

Review due words with spaced repetition.

5️⃣ <b>Daily Challenge</b>

Complete one five-question Premium quiz each day.

6️⃣ <b>AI Teacher</b>

Ask questions using your level and saved vocabulary as context.

7️⃣ <b>History and Word of the Day</b>

Reopen recent lessons or study today's personalized word.

8️⃣ <b>Progress</b>

Check goals, accuracy, activity, mastery, and your streak.

9️⃣ <b>Settings</b>

Set your level, learning goal, dialect, review targets, timezone, and reminders.

<b>Supported sources:</b>

• YouTube
• Other platforms supported by yt-dlp

<b>Tips:</b>

✅ Videos with English subtitles work best.

✅ Automatic English captions are also supported.

✅ Practice regularly for better retention.
"""

    await message.answer(
        help_text,
        parse_mode="HTML"
    )


# ============================================================
# Main Menu
# ============================================================

@dp.message(F.text == "🎬 Analyze Video")
async def analyze_video_handler(
    message: types.Message,
    state: FSMContext
):

    await message.answer(
        """
📹 Send me a link to a YouTube video or another supported platform.

I'll extract the English subtitles and create a personalized lesson.
""",
        parse_mode="HTML",
    )

    await state.set_state(
        UserState.waiting_for_url
    )


@dp.message(F.text == "📚 My Words")
async def my_words_handler(
    message: types.Message,
    state: FSMContext
):

    if message.from_user is None:
        return

    telegram_id = message.from_user.id

    total_words = count_user_vocabulary(
        telegram_id
    )

    if total_words == 0:

        await message.answer(
            """
📚 You haven't saved any words yet.

Analyze a video and save words to build your vocabulary!
""",
            parse_mode="HTML",
        )

        return

    await show_vocabulary_page(
        message,
        telegram_id,
        page=1
    )

    await state.set_state(
        UserState.choosing_vocabulary_page
    )


@dp.message(F.text == "📖 Review Today")
async def review_today_handler(message: types.Message):
    if message.from_user is None:
        return

    due_words = get_due_vocabulary(message.from_user.id)
    if due_words:
        await message.answer(
            f"📖 <b>Today's Review</b>\n\n"
            f"You have {len(due_words)} words to review.",
            parse_mode="HTML",
            reply_markup=get_review_start_keyboard(),
        )
        return

    conn = sqlite3.connect(config.DB_PATH)
    try:
        next_review = conn.execute(
            "SELECT MIN(next_review) FROM vocabulary WHERE telegram_id = ?",
            (message.from_user.id,),
        ).fetchone()[0]
    finally:
        conn.close()

    if next_review:
        next_review_date = datetime.fromisoformat(str(next_review)).replace(
            tzinfo=timezone.utc
        ).astimezone(get_user_timezone(message.from_user.id))
        if next_review_date.date() == get_user_local_date(message.from_user.id):
            next_text = f"today at {next_review_date.strftime('%H:%M')}"
        elif next_review_date.date() == get_user_local_date(message.from_user.id) + timedelta(days=1):
            next_text = f"tomorrow at {next_review_date.strftime('%H:%M')}"
        else:
            next_text = next_review_date.strftime("%Y-%m-%d %H:%M")
        text = f"🎉 You're all caught up!\n\nNext review: {next_text}."
    else:
        text = "🎉 You're all caught up!\n\nSave a word to start your review plan."
    await message.answer(text)


async def show_vocabulary_page(
    message: types.Message,
    telegram_id: int,
    page: int = 1
):

    total_words = count_user_vocabulary(
        telegram_id
    )

    per_page = 5

    total_pages = (
        total_words +
        per_page -
        1
    ) // per_page

    if page < 1 or page > total_pages:
        page = 1

    offset = (
        page -
        1
    ) * per_page

    words = get_user_vocabulary(
        telegram_id,
        limit=per_page,
        offset=offset
    )

    if not words:

        await message.answer(
            "No words found."
        )

        return

    vocab_text = (
        f"📚 <b>Your Vocabulary</b> "
        f"({page}/{total_pages})\n\n"
    )

    for word in words:

        vocab_text += (
            "🔥 <b>"
            + html.escape(
                str(word["word"])
            )
            + "</b>\n"
        )

        if word["transcription"]:

            vocab_text += (
                "/"
                + html.escape(
                    str(
                        word["transcription"]
                    )
                )
                + "/\n"
            )

        vocab_text += (
            "🇷🇺 "
            + html.escape(
                str(
                    word["translation"]
                )
            )
            + "\n"
        )

        if word["cefr"]:

            vocab_text += (
                "📈 "
                + html.escape(
                    str(
                        word["cefr"]
                    )
                )
                + "\n"
            )

        if word["example"]:

            vocab_text += (
                '💬 <i>"'
                + html.escape(
                    str(
                        word["example"]
                    )
                )
                + '"</i>\n'
            )

        vocab_text += "\n"

    keyboard = get_vocabulary_navigation_inline(
        page,
        total_pages,
        [word["id"] for word in words],
    )

    await message.answer(
        vocab_text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.message(F.text == "💎 Word of the Day")
async def word_of_day_handler(message: types.Message):
    if message.from_user is None:
        return
    telegram_id = message.from_user.id
    user = get_or_create_user(telegram_id)
    word_date = get_user_local_date(telegram_id).isoformat()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT payload_json FROM word_of_day WHERE telegram_id = ? AND word_date = ?",
            (telegram_id, word_date),
        ).fetchone()
    finally:
        conn.close()

    if row:
        try:
            word_data = json.loads(row[0])
        except json.JSONDecodeError:
            word_data = None
    else:
        saved_words = get_user_vocabulary(telegram_id, limit=100)
        if saved_words:
            day_number = datetime.now(get_user_timezone(telegram_id)).timetuple().tm_yday
            chosen = saved_words[(day_number - 1) % len(saved_words)]
            word_data = {
                "word": chosen["word"], "transcription": chosen["transcription"],
                "translation": chosen["translation"], "example": chosen["example"],
                "cefr": chosen["cefr"],
            }
        elif config.OPENAI_API_KEY:
            try:
                response = await openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "Return one useful English vocabulary item as JSON."},
                        {"role": "user", "content": (
                            f"Generate a natural {user['dialect']} English word useful for a "
                            f"{user['level']} learner with goal {user['learning_goal']}. Return fields: "
                            "word, transcription (IPA), translation (Russian), cefr, example."
                        )},
                    ],
                    temperature=0.5,
                    timeout=config.OPENAI_TIMEOUT,
                )
                word_data = json.loads(response.choices[0].message.content or "{}")
                if not all(word_data.get(key) for key in ("word", "translation", "example")):
                    word_data = None
            except Exception:
                logger.exception("Could not generate Word of the Day")
                word_data = None
        else:
            word_data = None

        if word_data:
            conn = sqlite3.connect(config.DB_PATH)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO word_of_day (telegram_id, word_date, payload_json) "
                    "VALUES (?, ?, ?)",
                    (telegram_id, word_date, json.dumps(word_data, ensure_ascii=False)),
                )
                conn.commit()
                stored = conn.execute(
                    "SELECT payload_json FROM word_of_day WHERE telegram_id = ? AND word_date = ?",
                    (telegram_id, word_date),
                ).fetchone()
                if stored:
                    word_data = json.loads(stored[0])
            finally:
                conn.close()

    if not word_data:
        await message.answer("I couldn't prepare today's word. Please try again later.")
        return

    text = (
        f"💎 <b>{html.escape(str(word_data['word']).upper())}</b>\n\n"
        f"/{html.escape(str(word_data.get('transcription') or ''))}/\n"
        f"🇷🇺 {html.escape(str(word_data['translation']))}\n\n"
        f"💬 <i>{html.escape(str(word_data['example']))}</i>\n"
        f"📈 {html.escape(str(word_data.get('cefr') or user['level']))}"
    )
    image_path = Path(__file__).resolve().parent / "assets" / "word_of_day.png"
    if image_path.is_file():
        await message.answer_photo(
            photo=FSInputFile(image_path),
            caption=text,
            parse_mode="HTML",
            reply_markup=get_word_of_day_keyboard(),
        )
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=get_word_of_day_keyboard())


@dp.callback_query(F.data.in_({"wordday_save", "wordday_practice"}))
async def word_of_day_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT payload_json FROM word_of_day WHERE telegram_id = ? AND word_date = ?",
            (callback.from_user.id, get_user_local_date(callback.from_user.id).isoformat()),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        await callback.answer("Today's word is unavailable.", show_alert=True)
        return
    try:
        word_data = json.loads(row[0])
    except json.JSONDecodeError:
        await callback.answer("Today's word is unavailable.", show_alert=True)
        return

    if callback.data == "wordday_save":
        result = save_word(
            callback.from_user.id,
            str(word_data["word"]),
            str(word_data.get("transcription") or ""),
            str(word_data["translation"]),
            str(word_data.get("example") or ""),
            str(word_data.get("cefr") or ""),
        )
        await callback.answer("✅ Saved" if result == "saved" else "Already saved" if result == "exists" else "Could not save", show_alert=False)
        return

    save_word(
        callback.from_user.id,
        str(word_data["word"]),
        str(word_data.get("transcription") or ""),
        str(word_data["translation"]),
        str(word_data.get("example") or ""),
        str(word_data.get("cefr") or ""),
    )
    stored_word = get_saved_word_by_text(callback.from_user.id, str(word_data["word"]))
    if not stored_word:
        await callback.answer("Could not start practice for this word.", show_alert=True)
        return
    await state.update_data(
        practice_words=[stored_word], practice_index=0,
        practice_current_word=stored_word, practice_mode="wordday",
        practice_type="meaning", practice_answered=False, practice_is_correct=None,
    )
    await state.set_state(UserState.practicing_word)
    message = callback.message
    if isinstance(message, types.Message):
        practice_prompt = (
            f"Recall the Russian meaning:\n\n"
            f"<b>{html.escape(str(stored_word['word']))}</b>"
        )
        await send_wordday_practice_prompt(message, practice_prompt)
    await callback.answer()


@dp.message(F.text == "🎯 Daily Challenge")
async def daily_challenge_handler(message: types.Message):
    if message.from_user is None:
        return
    telegram_id = message.from_user.id
    user = get_or_create_user(telegram_id)
    if not user["is_premium"]:
        await message.answer("🎯 Daily Challenge is available to Premium members.")
        return
    challenge_date = get_user_local_date(telegram_id).isoformat()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT questions_json, current_index, score, completed "
            "FROM daily_challenges WHERE telegram_id = ? AND challenge_date = ?",
            (telegram_id, challenge_date),
        ).fetchone()
    finally:
        conn.close()

    if row and row[3]:
        await message.answer(
            f"🎯 <b>Daily Challenge Complete</b>\n\nScore: {row[2]}/{len(json.loads(row[0]))}\n"
            f"Accuracy: {round(row[2] * 100 / len(json.loads(row[0])))}%",
            parse_mode="HTML",
        )
        return

    if row:
        questions = json.loads(row[0])
        question_index = row[1]
    else:
        vocabulary = get_user_vocabulary(telegram_id, limit=100)
        if len(vocabulary) < 5:
            await message.answer("Save at least 5 words to unlock your daily vocabulary challenge.")
            return
        generator = random.Random(f"{telegram_id}:{challenge_date}")
        chosen_words = generator.sample(vocabulary, 5)
        all_translations = list(dict.fromkeys(
            str(word["translation"]) for word in vocabulary if word.get("translation")
        ))
        questions = []
        for word in chosen_words:
            distractors = [value for value in all_translations if value != word["translation"]]
            options = [str(word["translation"]), *generator.sample(distractors, min(3, len(distractors)))]
            while len(options) < 4:
                options.append("I don't know")
            generator.shuffle(options)
            questions.append({
                "word_id": word["id"],
                "word": word["word"],
                "translation": word["translation"],
                "options": options,
                "correct_index": options.index(str(word["translation"])),
            })
        question_index = 0
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO daily_challenges "
                "(telegram_id, challenge_date, questions_json) VALUES (?, ?, ?)",
                (telegram_id, challenge_date, json.dumps(questions, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

    question = questions[question_index]
    await message.answer(
        f"🎯 <b>Daily Challenge</b> · {question_index + 1}/{len(questions)}\n\n"
        f"What does <b>{html.escape(str(question['word']))}</b> mean?",
        parse_mode="HTML",
        reply_markup=get_challenge_keyboard(question_index, question["options"]),
    )


@dp.callback_query(F.data.startswith("challenge_answer_"))
async def daily_challenge_answer_callback(callback: types.CallbackQuery):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return
    try:
        _, _, question_raw, selected_raw = callback.data.split("_")
        question_index = int(question_raw)
        selected_index = int(selected_raw)
    except (ValueError, TypeError):
        await callback.answer("Invalid answer.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    challenge_date = get_user_local_date(telegram_id).isoformat()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT questions_json, current_index, score, completed "
            "FROM daily_challenges WHERE telegram_id = ? AND challenge_date = ?",
            (telegram_id, challenge_date),
        ).fetchone()
        if not row or row[3] or row[1] != question_index:
            conn.rollback()
            await callback.answer("This question is no longer active.", show_alert=True)
            return
        questions = json.loads(row[0])
        question = questions[question_index]
        if selected_index < 0 or selected_index >= len(question["options"]):
            conn.rollback()
            await callback.answer("Invalid choice.", show_alert=True)
            return
        is_correct = int(selected_index == question["correct_index"])
        next_index = question_index + 1
        score = row[2] + is_correct
        completed = int(next_index >= len(questions))
        conn.execute(
            "INSERT INTO daily_challenge_answers "
            "(telegram_id, challenge_date, question_index, word_id, selected_index, is_correct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, challenge_date, question_index, question["word_id"], selected_index, is_correct),
        )
        conn.execute(
            "UPDATE daily_challenges SET current_index = ?, score = ?, completed = ? "
            "WHERE telegram_id = ? AND challenge_date = ? AND current_index = ? AND completed = 0",
            (next_index, score, completed, telegram_id, challenge_date, question_index),
        )
        conn.commit()
    except (sqlite3.Error, json.JSONDecodeError, IndexError, KeyError):
        conn.rollback()
        logger.exception("Error recording daily challenge answer")
        await callback.answer("Could not record your answer. Please retry.", show_alert=True)
        return
    finally:
        conn.close()

    rating = "easy" if is_correct else "again"
    practice_conn = sqlite3.connect(config.DB_PATH)
    try:
        practice_conn.execute(
            "INSERT INTO practice_sessions "
            "(telegram_id, word_id, difficulty, exercise_type, is_correct) "
            "VALUES (?, ?, ?, 'daily_challenge', ?)",
            (telegram_id, question["word_id"], rating, is_correct),
        )
        practice_conn.commit()
    except sqlite3.Error:
        practice_conn.rollback()
        logger.exception("Could not save challenge practice session")
    finally:
        practice_conn.close()
    schedule_word_review(telegram_id, question["word_id"], rating)

    challenge_streak = get_current_streak(telegram_id)
    if save_streak_milestone(telegram_id, challenge_streak):
        challenge_message = callback.message
        if isinstance(challenge_message, types.Message):
            await challenge_message.answer(
                f"🏆 <b>{challenge_streak}-day streak!</b> Keep the momentum going.",
                parse_mode="HTML",
            )

    message = callback.message
    if isinstance(message, types.Message):
        if completed:
            await message.edit_text(
                f"🎯 <b>Daily Challenge Complete</b>\n\nScore: {score}/{len(questions)}\n"
                f"Accuracy: {round(score * 100 / len(questions))}%",
                parse_mode="HTML",
            )
        else:
            next_question = questions[next_index]
            await message.edit_text(
                f"🎯 <b>Daily Challenge</b> · {next_index + 1}/{len(questions)}\n\n"
                f"What does <b>{html.escape(str(next_question['word']))}</b> mean?",
                parse_mode="HTML",
                reply_markup=get_challenge_keyboard(next_index, next_question["options"]),
            )
    await callback.answer("Correct!" if is_correct else "Keep practicing!")


@dp.message(F.text == "🎬 History")
async def history_handler(message: types.Message):
    if message.from_user is None:
        return
    telegram_id = message.from_user.id
    total_videos = count_all_videos(telegram_id)
    if not total_videos:
        await message.answer("🎬 No analyzed videos yet.")
        return
    page = 1
    per_page = 5
    videos = get_video_history(telegram_id, per_page, 0)
    text = "🎬 <b>Recent lessons</b>\n\n" + "\n".join(
        f"• {html.escape(str(video['title'] or 'Untitled'))} — "
        f"{html.escape(str(video['created_at']))}\n"
        f"  📚 {video['vocabulary_count']} words · "
        f"📈 {html.escape(str(video['estimated_level'] or 'Level unknown'))}"
        for video in videos
    )
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_history_keyboard(videos, page, (total_videos + 4) // 5),
    )


@dp.callback_query(F.data.startswith("history_"))
async def history_callback(callback: types.CallbackQuery):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    message = callback.message
    if not isinstance(message, types.Message):
        await callback.answer("Unable to open history.", show_alert=True)
        return

    if callback.data.startswith("history_open_"):
        try:
            video_id = int(callback.data.removeprefix("history_open_"))
        except ValueError:
            await callback.answer("Invalid lesson.", show_alert=True)
            return
        lesson = get_video_lesson(telegram_id, video_id)
        if not lesson:
            await callback.answer("Lesson is unavailable.", show_alert=True)
            return
        lesson_text = format_lesson(lesson["analysis"], str(lesson["title"] or "Untitled"))
        chunks = []
        current_chunk = ""
        for line in lesson_text.splitlines(keepends=True):
            if current_chunk and len(current_chunk) + len(line) > 3900:
                chunks.append(current_chunk)
                current_chunk = ""
            current_chunk += line
        if current_chunk:
            chunks.append(current_chunk)
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")
        await callback.answer()
        return

    if callback.data == "history_info":
        await callback.answer("Use the arrows to browse lessons.")
        return

    try:
        page = int(callback.data.removeprefix("history_page_"))
    except ValueError:
        await callback.answer("Invalid history page.", show_alert=True)
        return

    total_videos = count_all_videos(telegram_id)
    total_pages = (total_videos + 4) // 5
    if page < 1 or page > total_pages:
        await callback.answer("Invalid history page.", show_alert=True)
        return
    videos = get_video_history(telegram_id, 5, (page - 1) * 5)
    text = "🎬 <b>Recent lessons</b>\n\n" + "\n".join(
        f"• {html.escape(str(video['title'] or 'Untitled'))} — "
        f"{html.escape(str(video['created_at']))}\n"
        f"  📚 {video['vocabulary_count']} words · "
        f"📈 {html.escape(str(video['estimated_level'] or 'Level unknown'))}"
        for video in videos
    )
    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_history_keyboard(videos, page, total_pages),
    )
    await callback.answer()

@dp.message(F.text == "📊 Progress")
async def progress_handler(
    message: types.Message
):
    if message.from_user is None:
        return

    telegram_id = message.from_user.id

    user = get_or_create_user(telegram_id)

    vocab_count = count_user_vocabulary(telegram_id)
    videos_count = count_all_videos(telegram_id)

    streak = get_current_streak(telegram_id)
    longest_streak = get_longest_streak(telegram_id)

    stats = get_progress_stats(telegram_id)

    reviews_target = max(
        1,
        int(user["daily_review_target"])
    )

    new_words_target = max(
        1,
        int(user["daily_new_word_target"])
    )

    reviews_progress = min(
        100,
        round(
            stats["reviews_today"] * 100 / reviews_target
        )
    )

    new_words_progress = min(
        100,
        round(
            stats["new_words_today"] * 100 / new_words_target
        )
    )

    # =========================
    # LAST 7 DAYS
    # =========================

    weekly_start = (
        get_user_local_date(telegram_id)
        - timedelta(days=6)
    )

    weekly_activity = stats["weekly_activity"]

    week_values = []

    for offset in range(7):
        current_day = (
            weekly_start
            + timedelta(days=offset)
        )

        day_key = current_day.isoformat()

        value = int(
            weekly_activity.get(day_key, 0)
        )

        week_values.append(value)

    max_activity = max(
        week_values,
        default=0
    )

    week_chart_lines = []

    for offset, value in enumerate(week_values):
        current_day = (
            weekly_start
            + timedelta(days=offset)
        )

        day_name = current_day.strftime("%a")

        if max_activity > 0 and value > 0:
            bar_length = max(
                1,
                round(
                    value / max_activity * 10
                )
            )
        else:
            bar_length = 0

        bar = "█" * bar_length

        if not bar:
            bar = "—"

        week_chart_lines.append(
            f"{day_name:<3} {bar:<10} {value}"
        )

    week_chart = "\n".join(
        week_chart_lines
    )

    # =========================
    # TODAY'S TARGET BARS
    # =========================

    new_words_filled = round(
        new_words_progress / 10
    )

    reviews_filled = round(
        reviews_progress / 10
    )

    new_words_bar = (
        "█" * new_words_filled
        + "░" * (10 - new_words_filled)
    )

    reviews_bar = (
        "█" * reviews_filled
        + "░" * (10 - reviews_filled)
    )

    # =========================
    # PROGRESS MESSAGE
    # =========================

    daily_goal_completed = (
        stats["new_words_today"] >= new_words_target
        and
        stats["reviews_today"] >= reviews_target
    )

    goal_message = (
        "\n🔥 <b>Daily goal completed!</b>"
        if daily_goal_completed
        else ""
    )

    progress_text = f"""
📊 <b>Learning at a glance</b>

{html.escape(str(user["level"]))} · {html.escape(str(user["learning_goal"]))}

🔥 <b>{streak}</b> day streak · personal best <b>{longest_streak}</b>
📚 {vocab_count} words · 🧠 {stats["mastered"]} mastered · 🎬 {videos_count} lessons
🎯 {stats["accuracy"]}% review accuracy · {stats["due"]} due now

<b>Today's targets</b>
New words  {stats["new_words_today"]}/{new_words_target}  <code>{new_words_bar}</code>
Reviews    {stats["reviews_today"]}/{reviews_target}  <code>{reviews_bar}</code>

<b>Last 7 days</b>
<code>{week_chart}</code>
{goal_message}
"""

    await message.answer(
        progress_text,
        parse_mode="HTML"
    )
    
@dp.message(F.text == "🔥 Streak")
async def streak_handler(message: types.Message):
    if message.from_user is None:
        return
    current = get_current_streak(message.from_user.id)
    longest = get_longest_streak(message.from_user.id)
    milestones = (3, 7, 14, 30, 60, 100)
    next_milestone = next((milestone for milestone in milestones if milestone > current), None)
    progress = (
        f"\nNext milestone: {next_milestone} days ({next_milestone - current} to go)."
        if next_milestone else "\n🏆 All listed streak milestones reached!"
    )
    await message.answer(
        f"🔥 <b>Learning streak</b>\n\nCurrent: {current} days\nLongest: {longest} days{progress}",
        parse_mode="HTML",
    )


@dp.message(F.text == "⭐ Premium")
async def premium_handler(message: types.Message):
    if message.from_user is None:
        return
    telegram_id = message.from_user.id
    user = get_or_create_user(telegram_id)
    if is_privileged_user(telegram_id):
        await message.answer(
            "⭐ <b>Premium access included</b>\n\nOwner and admin accounts have all Premium features without payment.",
            parse_mode="HTML",
        )
        return
    if user["is_premium"]:
        text = "⭐ <b>Premium active</b>\n\nYour account has Premium access."
        keyboard = None
    else:
        text = (
            "⭐ <b>Premium</b>\n\n"
            f"Free plan: {config.FREE_DAILY_LIMIT} videos/day, "
            f"{config.FREE_DAILY_NEW_WORD_LIMIT} new words/day, and "
            f"{config.FREE_DAILY_AI_TEACHER_LIMIT} AI Teacher requests/day.\n\n"
            f"Upgrade once for {config.PREMIUM_PRICE_STARS} Telegram Stars to unlock Premium."
        )
        keyboard = get_premium_keyboard()
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "premium_buy")
async def premium_buy_callback(callback: types.CallbackQuery):
    if callback.from_user is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    if is_privileged_user(telegram_id):
        await callback.answer("Owner/admin accounts already have Premium access.", show_alert=True)
        return
    user = get_or_create_user(telegram_id)
    if user["is_premium"]:
        await callback.answer("Premium is already active.", show_alert=True)
        return

    message = callback.message
    if not isinstance(message, types.Message):
        await callback.answer("Unable to start checkout here.", show_alert=True)
        return

    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="English Learning Bot Premium",
            description="One-time Premium upgrade for your English learning account.",
            payload=create_premium_payload(telegram_id),
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(
                label="Premium access",
                amount=config.PREMIUM_PRICE_STARS,
            )],
        )
    except Exception:
        logger.exception("Could not create Telegram Stars Premium invoice")
        await callback.answer("Checkout is temporarily unavailable. Please try again later.", show_alert=True)
        return

    await callback.answer("Invoice sent. Complete payment to activate Premium.")


@dp.pre_checkout_query()
async def premium_pre_checkout(query: types.PreCheckoutQuery):
    valid = is_valid_premium_payment(
        query.from_user.id,
        query.invoice_payload,
        query.currency,
        query.total_amount,
    )
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="This Premium invoice is invalid or no longer available.",
        )


@dp.message(F.successful_payment)
async def premium_successful_payment(message: types.Message):
    if message.from_user is None or message.successful_payment is None:
        return

    payment = message.successful_payment
    try:
        result = record_premium_payment(
            telegram_id=message.from_user.id,
            payload=payment.invoice_payload,
            currency=payment.currency,
            total_amount=payment.total_amount,
            telegram_charge_id=payment.telegram_payment_charge_id,
            provider_charge_id=payment.provider_payment_charge_id,
        )
        if result == "activated":
            await message.answer(
                "⭐ <b>Premium activated!</b> Your account now has Premium access.",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(),
            )
        elif result == "duplicate":
            await message.answer("This payment was already processed. Premium access remains active.")
        else:
            logger.error(
                "Could not activate Premium after payment from user %s (result=%s)",
                message.from_user.id,
                result,
            )
            await message.answer(
                "We couldn't verify this payment automatically. Please contact the bot owner with your Telegram payment receipt."
            )
    except Exception as e:
        logger.exception("Error processing premium payment: %s", e)
        await message.answer(
            "An error occurred while processing your payment. Please contact support."
        )

    payment = message.successful_payment
    result = record_premium_payment(
        telegram_id=message.from_user.id,
        payload=payment.invoice_payload,
        currency=payment.currency,
        total_amount=payment.total_amount,
        telegram_charge_id=payment.telegram_payment_charge_id,
        provider_charge_id=payment.provider_payment_charge_id,
    )
    if result == "activated":
        await message.answer(
            "⭐ <b>Premium activated!</b> Your account now has Premium access.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(),
        )
    elif result == "duplicate":
        await message.answer("This payment was already processed. Premium access remains active.")
    else:
        logger.error(
            "Could not activate Premium after payment from user %s (result=%s)",
            message.from_user.id,
            result,
        )
        await message.answer(
            "We couldn't verify this payment automatically. Please contact the bot owner with your Telegram payment receipt."
        )


@dp.message(F.text == "⚙️ Settings")
async def settings_handler(
    message: types.Message,
    state: FSMContext
):

    if message.from_user is None:
        return

    user = get_or_create_user(
        message.from_user.id
    )

    settings_text = f"""
⚙️ <b>Settings</b>

<b>Level:</b> {html.escape(str(user["level"]))}
<b>Goal:</b> {html.escape(str(user["learning_goal"]))}
<b>Dialect:</b> {html.escape(str(user["dialect"]))}
<b>Vocabulary:</b> {html.escape(str(user["vocabulary_difficulty"]))}
<b>Daily review target:</b> {user["daily_review_target"]}
<b>Timezone:</b> {html.escape(str(user["timezone"]))}
<b>Daily reminders:</b> {"On" if user["notifications_enabled"] else "Off"}

Choose a setting:
"""

    await message.answer(
        settings_text,
        parse_mode="HTML",
        reply_markup=get_settings_keyboard()
    )

    await state.set_state(
        UserState.choosing_level
    )


@dp.message(
    UserState.choosing_level,
    F.text.in_(config.CEFR_LEVELS)
)
async def set_level_handler(
    message: types.Message,
    state: FSMContext
):

    if message.from_user is None:
        await message.answer("❌ Unable to identify you. Please try again.")
        return

    telegram_id = message.from_user.id

    level = message.text
    if level is None:
        return

    update_user_level(
        telegram_id,
        level
    )

    await message.answer(
        f"✅ Your level has been set to <b>{level}</b>",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )

    await state.set_state(
        UserState.choosing_action
    )


@dp.message(F.text.in_([
    "Goal: IELTS", "Goal: Speaking", "Goal: General", "Goal: Business",
    "Goal: American English", "Dialect: American", "Dialect: British",
    "Difficulty: Adaptive", "Difficulty: Advanced", "Review target: 10",
    "Review target: 20", "New word target: 5", "New word target: 10",
    "🔔 Enable reminders", "🔕 Disable reminders",
]))
async def update_settings_handler(message: types.Message):
    if message.from_user is None or message.text is None:
        return

    text = message.text
    if text.startswith("Goal: "):
        goal = text.removeprefix("Goal: ")
        goal_names = {
            "IELTS": "IELTS",
            "Speaking": "Speaking",
            "General": "General English",
            "Business": "Business English",
            "American English": "American English",
        }
        settings = {"learning_goal": goal_names[goal]}
    elif text.startswith("Dialect: "):
        settings = {"dialect": text.removeprefix("Dialect: ")}
    elif text.startswith("Difficulty: "):
        settings = {"vocabulary_difficulty": text.removeprefix("Difficulty: ")}
    elif text.startswith("Review target: "):
        settings = {"daily_review_target": int(text.rsplit(" ", 1)[1])}
    elif text.startswith("New word target: "):
        settings = {"daily_new_word_target": int(text.rsplit(" ", 1)[1])}
    else:
        settings = {"notifications_enabled": int(text == "🔔 Enable reminders")}

    if update_user_settings(message.from_user.id, **settings):
        await message.answer("✅ Setting updated.", reply_markup=get_settings_keyboard())
    else:
        await message.answer("Could not update that setting. Please try again.")


@dp.message(F.text == "⏰ Set reminder time")
async def reminder_time_prompt(message: types.Message, state: FSMContext):
    await state.set_state(UserState.waiting_for_reminder_time)
    await message.answer("Send the reminder time in 24-hour HH:MM format in your selected timezone.")


@dp.message(UserState.waiting_for_reminder_time)
async def set_reminder_time(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return
    try:
        datetime.strptime(message.text.strip(), "%H:%M")
    except ValueError:
        await message.answer("Use a valid 24-hour time, for example 09:30.")
        return

    update_user_settings(
        message.from_user.id,
        notification_time=message.text.strip(),
        notifications_enabled=1,
    )
    await state.set_state(UserState.choosing_action)
    await message.answer(
        f"🔔 Daily reminder enabled for {message.text.strip()}.",
        reply_markup=get_main_menu_keyboard(),
    )


@dp.message(F.text == "🌐 Set timezone")
async def timezone_prompt(message: types.Message, state: FSMContext):
    await state.set_state(UserState.waiting_for_timezone)
    await message.answer("Send an IANA timezone such as Europe/Moscow or America/New_York.")


@dp.message(UserState.waiting_for_timezone)
async def set_timezone(message: types.Message, state: FSMContext):
    if message.from_user is None or message.text is None:
        return
    timezone_name = message.text.strip()
    try:
        get_timezone_by_name(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        await message.answer("Timezone not recognized. Use an IANA name such as Europe/Moscow.")
        return
    update_user_settings(message.from_user.id, timezone=timezone_name)
    await state.set_state(UserState.choosing_action)
    await message.answer(
        f"🌐 Timezone set to {html.escape(timezone_name)}.",
        reply_markup=get_main_menu_keyboard(),
    )


@dp.message(F.text == "◀️ Back")
async def back_handler(
    message: types.Message,
    state: FSMContext
):

    await message.answer(
        "🇺🇸 English Learning Bot",
        reply_markup=get_main_menu_keyboard()
    )

    await state.set_state(
        UserState.choosing_action
    )


# ============================================================
# URL Processing
# ============================================================

@dp.message(UserState.waiting_for_url)
async def url_handler(
    message: types.Message,
    state: FSMContext
):

    if message.from_user is None:
        await message.answer("Unable to identify you. Please try again.")
        return

    telegram_id = message.from_user.id

    user = get_or_create_user(
        telegram_id
    )

    url = (
        message.text or ""
    ).strip()

    # --------------------------------------------------------
    # URL validation
    # --------------------------------------------------------

    if not await is_valid_url(url):

        await message.answer(
            """
❌ Please send a valid URL.

Example:
https://www.youtube.com/watch?v=...
""",
            parse_mode="HTML"
        )

        return

    # --------------------------------------------------------
    # Daily limit
    # --------------------------------------------------------

    if not user["is_premium"]:

        videos_today = count_videos_today(
            telegram_id
        )

        if videos_today >= config.FREE_DAILY_LIMIT:

            await message.answer(
                f"""
⏰ You've reached your daily limit
({config.FREE_DAILY_LIMIT} videos).

Come back tomorrow or upgrade to Premium! 🚀
""",
                parse_mode="HTML"
            )

            return

    # --------------------------------------------------------
    # Processing
    # --------------------------------------------------------

    status_msg = await message.answer(
        "🔎 Getting video information..."
    )

    try:

        # ----------------------------------------------------
        # Title
        # ----------------------------------------------------

        title = await get_video_title(
            url
        )

        if not title:
            title = "Untitled Video"

        # ----------------------------------------------------
        # Subtitles
        # ----------------------------------------------------

        await bot.edit_message_text(
            "📝 Getting English subtitles...",
            status_msg.chat.id,
            status_msg.message_id
        )

        transcript = await get_subtitles(
            url
        )

        if not transcript:

            await bot.edit_message_text(
                """
❌ Could not get English subtitles for this video.

The video may not have English subtitles or automatic English captions.

Try another YouTube video.
""",
                status_msg.chat.id,
                status_msg.message_id,
                parse_mode="HTML"
            )

            return

        logger.info(
            f"Transcript length: {len(transcript)}"
        )

        # ----------------------------------------------------
        # Cleaning
        # ----------------------------------------------------

        await bot.edit_message_text(
            "🧹 Cleaning transcript...",
            status_msg.chat.id,
            status_msg.message_id
        )

        # ----------------------------------------------------
        # AI
        # ----------------------------------------------------

        await bot.edit_message_text(
            "🧠 Analyzing with AI...",
            status_msg.chat.id,
            status_msg.message_id
        )

        analysis = await analyze_transcript(
            transcript,
            user["level"],
            user,
        )

        if not analysis:

            await bot.edit_message_text(
                """
❌ AI analysis failed.

Please try again later.
""",
                status_msg.chat.id,
                status_msg.message_id,
                parse_mode="HTML"
            )

            return

        # ----------------------------------------------------
        # Lesson
        # ----------------------------------------------------

        await bot.edit_message_text(
            "✨ Creating your lesson...",
            status_msg.chat.id,
            status_msg.message_id
        )

        staged_candidates = stage_lesson_candidates(
            telegram_id,
            analysis.get("vocabulary", []),
        )
        await state.update_data(
            last_lesson=analysis,
            last_candidates=staged_candidates,
            last_video_url=url,
            last_video_title=title,
            last_transcript=transcript[:6000],
        )
        lesson_analysis = dict(analysis)
        lesson_analysis["vocabulary"] = []
        lesson_analysis["ielts"] = None
        lesson_text = format_lesson(
            lesson_analysis,
            title
        )
        lesson_text += (
            f"\n\n🔥 {len(analysis.get('expressions', []))} useful expressions"
            f"\n📚 {len(staged_candidates)} vocabulary words"
            f"\n🗣 {len(analysis.get('natural_english', []))} natural expressions"
        )

        # ----------------------------------------------------
        # Delete processing message
        # ----------------------------------------------------

        await bot.delete_message(
            status_msg.chat.id,
            status_msg.message_id
        )

        # ----------------------------------------------------
        # Telegram message limit
        # ----------------------------------------------------

        if len(lesson_text) > 4096:

            chunks = [
                lesson_text[i:i + 4096]
                for i in range(
                    0,
                    len(lesson_text),
                    4096
                )
            ]

            for chunk in chunks:

                await message.answer(
                    chunk,
                    parse_mode="HTML"
                )

        else:

            await message.answer(
                lesson_text,
                parse_mode="HTML"
            )

        for candidate in staged_candidates:
            candidate_text = (
                f"📚 <b>{html.escape(str(candidate['word']))}</b>\n"
                f"/{html.escape(str(candidate.get('transcription') or ''))}/\n"
                f"🇷🇺 {html.escape(str(candidate['translation']))}\n"
                f"📈 {html.escape(str(candidate.get('cefr') or ''))}\n"
                f"{html.escape(str(candidate.get('priority') or 'Useful'))} · "
                f"{html.escape(str(candidate.get('category') or 'Vocabulary'))}"
            )
            if candidate.get("example"):
                candidate_text += (
                    f"\n💬 <i>{html.escape(str(candidate['example']))}</i>"
                )
            await message.answer(
                candidate_text,
                parse_mode="HTML",
                reply_markup=get_add_to_vocabulary_inline(
                    int(candidate["candidate_id"])
                ),
            )

        # ----------------------------------------------------
        # Save video
        # ----------------------------------------------------

        save_video(
            telegram_id,
            url,
            title,
            analysis,
            user["level"],
        )

        await message.answer(
            "💾 <b>Lesson created.</b> Save words, start practice, or open IELTS mode.",
            parse_mode="HTML",
            reply_markup=get_lesson_actions_keyboard(),
        )

    except Exception as e:

        logger.exception(
            f"Error processing URL: {e}"
        )

        try:

            await bot.edit_message_text(
                """
❌ An error occurred while processing the video.

Please try again.
""",
                status_msg.chat.id,
                status_msg.message_id,
                parse_mode="HTML"
            )

        except Exception:

            logger.exception(
                "Could not update processing message"
            )

    finally:

        await state.set_state(
            UserState.choosing_action
        )

        await message.answer(
            "What would you like to do next?",
            reply_markup=get_main_menu_keyboard()
        )


# ============================================================
# Vocabulary Management
# ============================================================

@dp.callback_query(F.data.in_({
    "lesson_save_all", "lesson_practice", "lesson_ielts", "lesson_again"
}))
async def lesson_actions_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return
    data = await state.get_data()
    candidates = data.get("last_candidates", [])
    analysis = data.get("last_lesson", {})
    message = callback.message

    if callback.data == "lesson_save_all":
        saved = 0
        limited = False
        for candidate in candidates:
            result = save_lesson_candidate(
                callback.from_user.id,
                int(candidate.get("candidate_id", -1)),
            )
            saved += int(result == "saved")
            limited = limited or result == "limit"
        response = f"Saved {saved} new words."
        if limited:
            response += " Free daily word limit reached."
        await callback.answer(response, show_alert=True)
        return

    if callback.data == "lesson_again":
        await state.set_state(UserState.waiting_for_url)
        if isinstance(message, types.Message):
            await message.answer("Send another video URL to analyze.")
        await callback.answer()
        return

    if callback.data == "lesson_ielts":
        user = get_or_create_user(callback.from_user.id)
        if not user["is_premium"]:
            await callback.answer("IELTS Mode is available to Premium members.", show_alert=True)
            return
        if not isinstance(analysis, dict) or not isinstance(analysis.get("ielts"), dict):
            await callback.answer("IELTS content is unavailable for this lesson.", show_alert=True)
            return
        ielts_analysis = {
            "ielts": analysis["ielts"],
            "estimated_level": analysis.get("estimated_level"),
        }
        text = format_lesson(
            ielts_analysis,
            f"IELTS lesson: {data.get('last_video_title', 'Video')}",
        )
        if isinstance(message, types.Message):
            await message.answer(text[:3900], parse_mode="HTML")
        await callback.answer()
        return

    if callback.data == "lesson_practice":
        saved_words = []
        for candidate in candidates:
            save_lesson_candidate(
                callback.from_user.id,
                int(candidate.get("candidate_id", -1)),
            )
            saved_word = get_saved_word_by_text(
                callback.from_user.id,
                str(candidate.get("word", "")),
            )
            if saved_word:
                saved_words.append(saved_word)
        if not saved_words:
            await callback.answer("No lesson vocabulary is available to practice.", show_alert=True)
            return
        saved_words = saved_words[:5]
        user_vocabulary = get_user_vocabulary(callback.from_user.id, limit=50)
        first_word = saved_words[0]
        practice_text, keyboard, correct_index = build_practice_prompt(
            first_word, "meaning", user_vocabulary
        )
        await state.update_data(
            practice_words=saved_words,
            practice_vocabulary=user_vocabulary,
            practice_index=0,
            practice_current_word=first_word,
            practice_mode="lesson",
            practice_type="meaning",
            practice_correct_index=correct_index,
            practice_options=[],
            practice_answered=False,
            practice_is_correct=None,
        )
        await state.set_state(UserState.practicing_word)
        if isinstance(message, types.Message):
            await message.answer(practice_text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()


@dp.callback_query(
    F.data.startswith("add_word_")
)
async def add_word_callback(
    callback: types.CallbackQuery
):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    try:
        candidate_id = int(callback.data.removeprefix("add_word_"))
    except ValueError:
        await callback.answer("Invalid word action.", show_alert=True)
        return

    result = save_lesson_candidate(callback.from_user.id, candidate_id)
    messages = {
        "saved": "✅ Saved to your vocabulary",
        "exists": "Already saved",
        "limit": "Free daily vocabulary limit reached",
        "missing": "This lesson word has expired.",
        "error": "Could not save this word. Please try again.",
    }
    await callback.answer(messages.get(result, "Could not save this word."), show_alert=False)


@dp.callback_query(F.data == "review_start")
async def review_start_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    if callback.from_user is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    words = get_due_vocabulary(callback.from_user.id)
    if not words:
        await callback.answer("No words are due right now.", show_alert=True)
        return

    await state.update_data(
        practice_words=words,
        practice_index=0,
        practice_current_word=words[0],
        practice_mode="review",
        practice_type="meaning",
    )
    await state.set_state(UserState.practicing_word)

    message = callback.message
    if isinstance(message, types.Message):
        await message.edit_text(
            "📖 <b>Review</b>\n\n"
            "Recall the Russian meaning, then reveal the answer.\n\n"
            f"<b>{html.escape(str(words[0]['word']))}</b>",
            parse_mode="HTML",
            reply_markup=get_practice_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("word_view_"))
async def word_view_callback(callback: types.CallbackQuery):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    try:
        word_id = int(callback.data.removeprefix("word_view_"))
    except ValueError:
        await callback.answer("Invalid word.", show_alert=True)
        return

    word = get_vocabulary_word(callback.from_user.id, word_id)
    if not word:
        await callback.answer("Word not found.", show_alert=True)
        return

    created_at = html.escape(str(word.get("created_at") or "Unknown"))
    next_review = html.escape(str(word.get("next_review") or "Not scheduled"))
    text = (
        f"📚 <b>{html.escape(str(word['word']))}</b>\n"
        f"/{html.escape(str(word.get('transcription') or ''))}/\n"
        f"🇷🇺 {html.escape(str(word['translation']))}\n"
        f"📈 {html.escape(str(word.get('cefr') or ''))}\n"
        f"💬 <i>{html.escape(str(word.get('example') or ''))}</i>\n\n"
        f"Added: {created_at}\n"
        f"Reviews: {word.get('review_count', 0)}\n"
        f"Next review: {next_review}\n"
        f"Difficulty: {html.escape(str(word.get('difficulty') or 'new'))}"
    )
    message = callback.message
    if isinstance(message, types.Message):
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=get_word_detail_keyboard(word_id),
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("word_delete_"))
async def word_delete_callback(callback: types.CallbackQuery):
    if callback.from_user is None or callback.data is None:
        await callback.answer("User data unavailable.", show_alert=True)
        return

    try:
        word_id = int(callback.data.removeprefix("word_delete_"))
    except ValueError:
        await callback.answer("Invalid word.", show_alert=True)
        return

    if not delete_vocabulary_word(callback.from_user.id, word_id):
        await callback.answer("Word not found.", show_alert=True)
        return

    message = callback.message
    if isinstance(message, types.Message):
        await message.edit_text("🗑 Word deleted from your vocabulary.")
    await callback.answer("Deleted")


@dp.callback_query(
    F.data.startswith("vocab_page_")
)
async def vocab_page_callback(
    callback: types.CallbackQuery,
    state: FSMContext
):

    if callback.from_user is None:
        await callback.answer("❌ User data unavailable.", show_alert=True)
        return

    if callback.data is None:
        await callback.answer("❌ Unknown page action.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    action = callback.data.replace(
        "vocab_page_",
        ""
    )

    if action == "info":

        await callback.answer(
            "📚 Use navigation arrows",
            show_alert=False
        )

        return

    try:
        page = int(action)

    except ValueError:

        await callback.answer(
            "❌ Invalid page",
            show_alert=True
        )

        return

    total_words = count_user_vocabulary(
        telegram_id
    )

    per_page = 5

    total_pages = (
        total_words +
        per_page -
        1
    ) // per_page

    if page < 1 or page > total_pages:

        await callback.answer(
            "❌ Invalid page",
            show_alert=True
        )

        return

    offset = (
        page -
        1
    ) * per_page

    words = get_user_vocabulary(
        telegram_id,
        limit=per_page,
        offset=offset
    )

    vocab_text = (
        f"📚 <b>Your Vocabulary</b> "
        f"({page}/{total_pages})\n\n"
    )

    for word in words:

        vocab_text += (
            "🔥 <b>"
            + html.escape(
                str(word["word"])
            )
            + "</b>\n"
        )

        if word["transcription"]:

            vocab_text += (
                "/"
                + html.escape(
                    str(
                        word["transcription"]
                    )
                )
                + "/\n"
            )

        vocab_text += (
            "🇷🇺 "
            + html.escape(
                str(
                    word["translation"]
                )
            )
            + "\n"
        )

        if word["cefr"]:

            vocab_text += (
                "📈 "
                + html.escape(
                    str(
                        word["cefr"]
                    )
                )
                + "\n"
            )

        if word["example"]:

            vocab_text += (
                '💬 <i>"'
                + html.escape(
                    str(
                        word["example"]
                    )
                )
                + '"</i>\n'
            )

        vocab_text += "\n"

    keyboard = get_vocabulary_navigation_inline(
        page,
        total_pages,
        [word["id"] for word in words],
    )

    message = callback.message

    if message is None or not isinstance(message, types.Message):

        await callback.answer(
            "❌ Unable to update page",
            show_alert=True
        )

        return

    try:

        await message.edit_text(
            vocab_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:

        logger.error(
            f"Error editing message: {e}"
        )

        await callback.answer(
            "❌ Error updating page",
            show_alert=True
        )

        return

    await callback.answer()


# ============================================================
# Practice
# ============================================================

@dp.callback_query(
    F.data.startswith("practice_start_")
)
async def practice_start_callback(
    callback: types.CallbackQuery,
    state: FSMContext
):

    if callback.from_user is None:
        await callback.answer("❌ User data unavailable.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        page = int(callback.data.removeprefix("practice_start_")) if callback.data else 1
    except ValueError:
        page = 1

    vocabulary = get_user_vocabulary(
        telegram_id,
        limit=50,
        offset=0,
    )
    page_words = get_user_vocabulary(
        telegram_id,
        limit=5,
        offset=max(0, page - 1) * 5,
    )
    words = page_words

    if not words:

        await callback.answer(
            "No words to practice",
            show_alert=True
        )

        return

    exercise_types = ["meaning", "translation", "multiple_choice", "fill_blank", "context"]
    exercise_type = exercise_types[0]
    practice_text, keyboard, correct_index = build_practice_prompt(
        words[0], exercise_type, vocabulary
    )
    await state.update_data(
        practice_words=words,
        practice_index=0,
        practice_current_word=words[0],
        practice_vocabulary=vocabulary,
        practice_mode="free",
        practice_type=exercise_type,
        practice_correct_index=correct_index,
        practice_options=[
            button.text[3:]
            for row in keyboard.inline_keyboard
            for button in row
        ] if exercise_type == "multiple_choice" and keyboard is not None else [],
        practice_answered=False,
        practice_is_correct=None,
    )

    if not isinstance(callback.message, types.Message):
        await callback.answer(
            "Unable to display practice mode.",
            show_alert=True
        )
        return

    try:

        await callback.message.edit_text(
            practice_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    except Exception as e:

        logger.error(
            f"Error editing message: {e}"
        )

    await state.set_state(
        UserState.practicing_word
    )

    await callback.answer()


@dp.callback_query(
    UserState.practicing_word,
    F.data == "show_answer"
)
async def show_answer_callback(
    callback: types.CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    word = data.get(
        "practice_current_word"
    )

    if not word or data.get("practice_answered"):

        await callback.answer(
            "Error loading word",
            show_alert=True
        )

        return

    answer_text = f"""
✅ <b>Answer</b>

<b>{html.escape(str(word["word"]))}</b>

/{html.escape(str(word["transcription"] or ""))}/

🇷🇺 {html.escape(str(word["translation"]))}

📈 {html.escape(str(word["cefr"] or ""))}

💬 <i>"{html.escape(str(word["example"] or ""))}"</i>

How difficult was this?
"""

    keyboard = get_practice_feedback_keyboard(
        word["id"]
    )
    await state.update_data(practice_answered=True, practice_is_correct=None)

    message = callback.message
    if isinstance(message, types.Message):
        try:
            await message.edit_text(
                answer_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(
                f"Error editing message: {e}"
            )

    await callback.answer()


@dp.callback_query(
    UserState.practicing_word,
    F.data.startswith("exercise_choice_")
)
async def exercise_choice_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    if data.get("practice_answered"):
        await callback.answer("This answer was already recorded.", show_alert=True)
        return
    try:
        selected_index = int(str(callback.data).removeprefix("exercise_choice_"))
    except ValueError:
        await callback.answer("Invalid answer.", show_alert=True)
        return
    correct_index = data.get("practice_correct_index")
    word = data.get("practice_current_word")
    options = data.get("practice_options", [])
    if correct_index is None or not word or selected_index < 0 or selected_index >= len(options):
        await callback.answer("This question is no longer active.", show_alert=True)
        return

    is_correct = selected_index == correct_index
    await state.update_data(practice_answered=True, practice_is_correct=is_correct)
    text = (
        f"{'✅ Correct' if is_correct else '❌ Not quite'}\n\n"
        f"<b>{html.escape(str(word['word']))}</b> = "
        f"{html.escape(str(word['translation']))}\n\n"
        "How difficult was this?"
    )
    message = callback.message
    if isinstance(message, types.Message):
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_practice_feedback_keyboard(int(word["id"])),
        )
    await callback.answer()


@dp.callback_query(
    UserState.practicing_word,
    F.data.startswith("feedback_")
)
@dp.callback_query(
    UserState.practicing_word,
    F.data.startswith("feedback_")
)
async def feedback_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user is None:
        await callback.answer("❌ User data unavailable.", show_alert=True)
        return

    if callback.data is None:
        await callback.answer("❌ Unknown practice action.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    action_parts = callback.data.split("_")

    if len(action_parts) < 3:
        await callback.answer("Invalid practice action.", show_alert=True)
        return

    difficulty = action_parts[1]
    try:
        word_id = int(action_parts[2])
    except (IndexError, ValueError):
        await callback.answer("Invalid practice action.", show_alert=True)
        return

    if difficulty not in {"again", "hard", "easy"}:
        await callback.answer("Invalid practice rating.", show_alert=True)
        return

    data = await state.get_data()
    current_word = data.get("practice_current_word")
    if not current_word or int(current_word.get("id", -1)) != word_id:
        await callback.answer("This answer was already recorded.", show_alert=True)
        return

    # Save practice session
    session_saved = False
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO practice_sessions
            (telegram_id, word_id, difficulty, exercise_type, is_correct, callback_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                word_id,
                difficulty,
                data.get("practice_type", "meaning"),
                int(data.get("practice_is_correct", difficulty != "again")),
                callback.id,
            )
        )
        conn.commit()
        session_saved = True
    except sqlite3.IntegrityError:
        logger.info("Duplicate practice callback ignored: %s", callback.id)
    except Exception as e:
        logger.error(f"Error saving practice session: {e}")
    finally:
        conn.close()

    if not session_saved:
        await callback.answer("This answer was already recorded.", show_alert=True)
        return

    schedule_word_review(telegram_id, word_id, difficulty)

    # Next word
    practice_words = data.get("practice_words", [])
    practice_index = data.get("practice_index", 0) + 1

    if practice_index < len(practice_words):
        next_word = practice_words[practice_index]
        exercise_types = ["meaning", "translation", "multiple_choice", "fill_blank", "context"]
        practice_mode = data.get("practice_mode", "free")
        exercise_type = (
            "meaning"
            if practice_mode == "review"
            else exercise_types[practice_index % len(exercise_types)]
        )
        practice_text, keyboard, correct_index = build_practice_prompt(
            next_word,
            exercise_type,
            data.get("practice_vocabulary", practice_words),
        )
        await state.update_data(
            practice_index=practice_index,
            practice_current_word=next_word,
            practice_type=exercise_type,
            practice_correct_index=correct_index,
            practice_options=[
                button.text[3:]
                for row in keyboard.inline_keyboard
                for button in row
            ] if exercise_type == "multiple_choice" and keyboard is not None else [],
            practice_answered=False,
            practice_is_correct=None,
        )

        if not isinstance(callback.message, types.Message):
            await callback.answer()
            return

        try:
            await callback.message.edit_text(
                practice_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Error editing message: {e}")

        await callback.answer()
    else:
        if not isinstance(callback.message, types.Message):
            await callback.answer()
            return

        try:
            await callback.message.delete()
        except Exception as e:
            logger.error(f"Error deleting message: {e}")

        await callback.message.answer(
            f"""
✅ <b>Great job!</b>

You practiced {len(practice_words)} words.

Keep learning! 💪
""",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )

        await state.set_state(UserState.choosing_action)
        await state.update_data(practice_current_word=None)

    # Check streak milestone
    current_streak = get_current_streak(telegram_id)
    if save_streak_milestone(telegram_id, current_streak):
        if isinstance(callback.message, types.Message):
            await callback.message.answer(
                f"🏆 <b>{current_streak}-day streak!</b> Keep the momentum going.",
                parse_mode="HTML",
            )


# ============================================================
# Catch-all Handler
# ============================================================

@dp.message(F.text == "🤖 AI Teacher")
async def ai_teacher_start_handler(message: types.Message, state: FSMContext):
    await state.set_state(UserState.ai_teacher_chat)
    await message.answer(
        "🤖 Ask an English question, request examples, or ask me to quiz you on saved words. "
        "Send /start to leave AI Teacher mode."
    )


@dp.message(UserState.ai_teacher_chat)
async def ai_teacher_message_handler(message: types.Message, state: FSMContext):
    if message.from_user is None or not message.text:
        return

    if message.text.strip() == "/start":
        await state.clear()
        await message.answer("AI Teacher closed.", reply_markup=get_main_menu_keyboard())
        await state.set_state(UserState.choosing_action)
        return

    if not config.OPENAI_API_KEY:
        await message.answer("AI Teacher is unavailable because OPENAI_API_KEY is not configured.")
        return

    user = get_or_create_user(message.from_user.id)
    if not reserve_ai_teacher_request(message.from_user.id, bool(user["is_premium"])):
        await message.answer("You've used today's 5 free AI Teacher requests. Premium users have unlimited requests.")
        return

    words = get_user_vocabulary(message.from_user.id, limit=8)
    recent_videos = get_video_history(message.from_user.id, limit=3)
    profile_context = {
        "level": user["level"],
        "goal": user["learning_goal"],
        "dialect": user["dialect"],
        "saved_vocabulary": [
            {"word": word["word"], "translation": word["translation"]}
            for word in words
        ],
        "recent_lessons": [video["title"] for video in recent_videos],
    }

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a concise personal English teacher. Adapt explanations and examples "
                        "to the learner profile. Prefer their saved vocabulary and goal when relevant. "
                        "Do not claim access to context beyond the provided profile."
                    ),
                },
                {"role": "user", "content": f"Learner context: {json.dumps(profile_context, ensure_ascii=False)}\n\nQuestion: {message.text[:1500]}"},
            ],
            temperature=0.5,
            max_tokens=800,
            timeout=config.OPENAI_TIMEOUT,
        )
        answer = response.choices[0].message.content or "I couldn't form an answer. Please try rephrasing."
        await message.answer(answer[:4000])
    except Exception as e:
        logger.exception("AI Teacher request failed: %s", e)
        await message.answer("I couldn't reach the AI teacher right now. Please try again shortly.")


@dp.message()
async def echo_handler(
    message: types.Message,
    state: FSMContext
):

    current_state = await state.get_state()

    if current_state == UserState.waiting_for_url:

        await url_handler(
            message,
            state
        )

    else:

        await message.answer(
            """
❓ I didn't understand that.

Use the menu buttons or send a video URL.
""",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )


# ============================================================
# Main
# ============================================================

async def reminder_loop():
    """Send opted-in due-review reminders once per user's local day."""

    while True:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            users = conn.execute(
                "SELECT telegram_id, notification_time, timezone, last_reminder_date "
                "FROM users WHERE notifications_enabled = 1"
            ).fetchall()
        finally:
            conn.close()

        for telegram_id, reminder_time, timezone_name, last_sent in users:
            try:
                user_timezone = get_timezone_by_name(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                user_timezone = timezone.utc
            local_now = datetime.now(user_timezone)
            local_date = local_now.date().isoformat()
            if last_sent == local_date or local_now.strftime("%H:%M") < reminder_time:
                continue

            due_count = len(get_due_vocabulary(telegram_id))
            if not due_count:
                continue
            try:
                await bot.send_message(
                    telegram_id,
                    f"🔔 Daily reminder\n\n⏰ You have {due_count} words waiting for review.",
                )
            except Exception:
                logger.exception("Could not send daily review reminder to user %s", telegram_id)
                continue

            conn = sqlite3.connect(config.DB_PATH)
            try:
                conn.execute(
                    "UPDATE users SET last_reminder_date = ? "
                    "WHERE telegram_id = ? AND notifications_enabled = 1",
                    (local_date, telegram_id),
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.sleep(30)


async def main():

    logger.info(
        "Initializing database..."
    )

    init_database()
    init_onboarding_storage()

    logger.info(
        "Starting bot..."
    )

    reminder_task = asyncio.create_task(reminder_loop())

    try:

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types()
        )

    except Exception as e:

        logger.exception(
            f"Error during polling: {e}"
        )

    finally:

        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by user"
        )