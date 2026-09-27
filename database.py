"""SQLite operations for vocabulary and spaced repetition."""

import logging
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config


logger = logging.getLogger(__name__)


def is_privileged_user(telegram_id: int) -> bool:
    return telegram_id == config.OWNER_ID or telegram_id in config.ADMIN_IDS


def create_premium_payload(telegram_id: int) -> str:
    return f"premium_v1:{telegram_id}:{uuid4().hex}"


def is_valid_premium_payment(
    telegram_id: int,
    payload: str,
    currency: str,
    total_amount: int,
) -> bool:
    match = re.fullmatch(r"premium_v1:(\d+):[a-f0-9]{32}", payload)
    return bool(
        match
        and int(match.group(1)) == telegram_id
        and currency == "XTR"
        and total_amount == config.PREMIUM_PRICE_STARS
        and not is_privileged_user(telegram_id)
    )


def record_premium_payment(
    telegram_id: int,
    payload: str,
    currency: str,
    total_amount: int,
    telegram_charge_id: str,
    provider_charge_id: str,
) -> str:
    if not is_valid_premium_payment(telegram_id, payload, currency, total_amount):
        return "invalid"
    if not telegram_charge_id:
        return "invalid"

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO premium_payments "
            "(telegram_id, telegram_payment_charge_id, provider_payment_charge_id, "
            "invoice_payload, currency, total_amount) VALUES (?, ?, ?, ?, ?, ?)",
            (
                telegram_id,
                telegram_charge_id,
                provider_charge_id,
                payload,
                currency,
                total_amount,
            ),
        )
        cursor = conn.execute(
            "UPDATE users SET is_premium = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return "invalid"
        conn.commit()
        return "activated"
    except sqlite3.IntegrityError:
        conn.rollback()
        return "duplicate"
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to record Stars payment")
        return "error"
    finally:
        conn.close()


def init_database() -> None:
    """Create tables and apply additive migrations without removing existing data."""

    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                level TEXT NOT NULL DEFAULT 'B1',
                is_premium INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vocabulary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                transcription TEXT,
                translation TEXT NOT NULL,
                example TEXT,
                cefr TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS video_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS practice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                word_id INTEGER NOT NULL,
                difficulty TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                FOREIGN KEY (word_id) REFERENCES vocabulary(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lesson_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                word TEXT NOT NULL,
                transcription TEXT,
                translation TEXT NOT NULL,
                example TEXT,
                cefr TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_challenges (
                telegram_id INTEGER NOT NULL,
                challenge_date TEXT NOT NULL,
                questions_json TEXT NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, challenge_date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_challenge_answers (
                telegram_id INTEGER NOT NULL,
                challenge_date TEXT NOT NULL,
                question_index INTEGER NOT NULL,
                word_id INTEGER NOT NULL,
                selected_index INTEGER NOT NULL,
                is_correct INTEGER NOT NULL,
                answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, challenge_date, question_index),
                FOREIGN KEY (telegram_id, challenge_date)
                    REFERENCES daily_challenges(telegram_id, challenge_date)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_teacher_usage (
                telegram_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, usage_date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS word_of_day (
                telegram_id INTEGER NOT NULL,
                word_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (telegram_id, word_date),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS streak_achievements (
                telegram_id INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                achieved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, milestone),
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS premium_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                telegram_payment_charge_id TEXT UNIQUE NOT NULL,
                provider_payment_charge_id TEXT,
                invoice_payload TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
            )
        """)

        migrations = {
            "vocabulary": {
                "review_count": "INTEGER NOT NULL DEFAULT 0",
                "repetitions": "INTEGER NOT NULL DEFAULT 0",
                "ease_factor": "REAL NOT NULL DEFAULT 2.5",
                "interval_days": "INTEGER NOT NULL DEFAULT 0",
                "next_review": "TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'",
                "last_review": "TEXT",
                "difficulty": "TEXT NOT NULL DEFAULT 'new'",
            },
            "practice_sessions": {
                "exercise_type": "TEXT NOT NULL DEFAULT 'meaning'",
                "is_correct": "INTEGER",
                "callback_id": "TEXT",
            },
            "video_history": {
                "analysis_json": "TEXT",
                "vocabulary_count": "INTEGER NOT NULL DEFAULT 0",
                "estimated_level": "TEXT",
            },
            "users": {
                "learning_goal": "TEXT NOT NULL DEFAULT 'General English'",
                "dialect": "TEXT NOT NULL DEFAULT 'American'",
                "vocabulary_difficulty": "TEXT NOT NULL DEFAULT 'Adaptive'",
                "daily_review_target": "INTEGER NOT NULL DEFAULT 20",
                "daily_new_word_target": "INTEGER NOT NULL DEFAULT 10",
                "notifications_enabled": "INTEGER NOT NULL DEFAULT 0",
                "notification_time": "TEXT NOT NULL DEFAULT '09:00'",
                "timezone": "TEXT NOT NULL DEFAULT 'UTC'",
                "last_reminder_date": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing_columns = {
                row[1] for row in cursor.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in columns.items():
                if column not in existing_columns:
                    cursor.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_vocabulary_due "
            "ON vocabulary (telegram_id, next_review)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_practice_user_date "
            "ON practice_sessions (telegram_id, created_at)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_practice_callback "
            "ON practice_sessions (callback_id) WHERE callback_id IS NOT NULL"
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Database initialization/migration failed")
        raise
    finally:
        conn.close()


def get_or_create_user(telegram_id: int) -> Dict[str, Any]:
    columns = (
        "id, telegram_id, level, is_premium, created_at, last_active, "
        "learning_goal, dialect, vocabulary_difficulty, daily_review_target, "
        "notifications_enabled, notification_time, timezone, daily_new_word_target"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (telegram_id, level) VALUES (?, ?)",
                (telegram_id, config.DEFAULT_LEVEL),
            )
            conn.commit()
            row = conn.execute(
                f"SELECT {columns} FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
    finally:
        conn.close()

    return {
        "id": row[0], "telegram_id": row[1], "level": row[2],
        "is_premium": bool(row[3]) or is_privileged_user(telegram_id),
        "created_at": row[4], "last_active": row[5], "learning_goal": row[6],
        "dialect": row[7], "vocabulary_difficulty": row[8],
        "daily_review_target": row[9], "notifications_enabled": row[10],
        "notification_time": row[11], "timezone": row[12],
        "daily_new_word_target": row[13],
    }


def update_user_level(telegram_id: int, level: str) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "UPDATE users SET level = ?, last_active = CURRENT_TIMESTAMP "
            "WHERE telegram_id = ?",
            (level, telegram_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_user_settings(telegram_id: int, **settings: Any) -> bool:
    allowed = {
        "learning_goal", "dialect", "vocabulary_difficulty", "daily_review_target",
        "notifications_enabled", "notification_time", "timezone", "daily_new_word_target",
    }
    updates = {key: value for key, value in settings.items() if key in allowed}
    if not updates:
        return False
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            f"UPDATE users SET {assignments}, last_active = CURRENT_TIMESTAMP WHERE telegram_id = ?",
            [*updates.values(), telegram_id],
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Error updating user settings")
        return False
    finally:
        conn.close()


def get_timezone_by_name(timezone_name: Optional[str]) -> tzinfo:
    normalized_name = (timezone_name or "UTC").strip()
    if normalized_name.upper() in {"UTC", "ETC/UTC", "GMT", "Z"}:
        return timezone.utc
    return ZoneInfo(normalized_name)


def get_user_timezone(telegram_id: int) -> tzinfo:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT timezone FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    finally:
        conn.close()
    try:
        return get_timezone_by_name(row[0] if row else "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def get_user_local_date(telegram_id: int) -> date:
    return datetime.now(get_user_timezone(telegram_id)).date()


def save_video(
    telegram_id: int,
    url: str,
    title: str,
    analysis: Optional[Dict[str, Any]] = None,
    estimated_level: Optional[str] = None,
) -> Optional[int]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT INTO video_history "
            "(telegram_id, url, title, analysis_json, vocabulary_count, estimated_level) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                telegram_id,
                url,
                title,
                json.dumps(analysis, ensure_ascii=False) if analysis else None,
                len(analysis.get("vocabulary", [])) if analysis else 0,
                estimated_level,
            ),
        )
        video_id = cursor.lastrowid
        conn.commit()
        return video_id
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Error saving video history")
        return None
    finally:
        conn.close()


def get_video_history(
    telegram_id: int,
    limit: int = 5,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, title, url, created_at, vocabulary_count, estimated_level "
            "FROM video_history WHERE telegram_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (telegram_id, limit, offset),
        ).fetchall()
        keys = ("id", "title", "url", "created_at", "vocabulary_count", "estimated_level")
        return [dict(zip(keys, row)) for row in rows]
    finally:
        conn.close()


def get_video_lesson(telegram_id: int, video_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT title, analysis_json FROM video_history "
            "WHERE telegram_id = ? AND id = ?",
            (telegram_id, video_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[1]:
        return None
    try:
        from lesson_service import validate_analysis_payload

        analysis = validate_analysis_payload(json.loads(row[1]))
    except (json.JSONDecodeError, TypeError):
        return None
    return {"title": row[0], "analysis": analysis} if analysis else None


def count_videos_today(telegram_id: int) -> int:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM video_history "
            "WHERE telegram_id = ? AND DATE(created_at) = DATE('now')",
            (telegram_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def count_all_videos(telegram_id: int) -> int:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM video_history WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def get_learning_dates(telegram_id: int) -> List[date]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT created_at FROM practice_sessions WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchall()
    finally:
        conn.close()
    user_timezone = get_user_timezone(telegram_id)
    dates = set()
    for row in rows:
        if not row[0]:
            continue
        try:
            timestamp = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        dates.add(timestamp.astimezone(user_timezone).date())
    return sorted(dates)


def get_current_streak(telegram_id: int) -> int:
    dates = get_learning_dates(telegram_id)
    if not dates:
        return 0
    today = get_user_local_date(telegram_id)
    if dates[-1] not in {today, today - timedelta(days=1)}:
        return 0
    streak = 1
    for index in range(len(dates) - 1, 0, -1):
        if (dates[index] - dates[index - 1]).days != 1:
            break
        streak += 1
    return streak


def get_longest_streak(telegram_id: int) -> int:
    dates = get_learning_dates(telegram_id)
    longest = current = 0
    previous = None
    for day in dates:
        current = current + 1 if previous and (day - previous).days == 1 else 1
        longest = max(longest, current)
        previous = day
    return longest


def save_streak_milestone(telegram_id: int, streak: int) -> bool:
    if streak not in {3, 7, 14, 30, 60, 100}:
        return False
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO streak_achievements (telegram_id, milestone) VALUES (?, ?)",
            (telegram_id, streak),
        )
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Could not record streak milestone")
        return False
    finally:
        conn.close()


def get_progress_stats(telegram_id: int) -> Dict[str, Any]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        vocabulary = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN repetitions >= 5 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN DATE(created_at) >= DATE('now', '-6 days') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN DATE(created_at) = DATE('now') THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN next_review <= datetime('now') THEN 1 ELSE 0 END) "
            "FROM vocabulary WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        sessions = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN DATE(created_at) = DATE('now') THEN 1 ELSE 0 END), "
            "COUNT(is_correct) FROM practice_sessions WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        activity = conn.execute(
            "SELECT DATE(created_at), COUNT(*) FROM practice_sessions "
            "WHERE telegram_id = ? AND DATE(created_at) >= DATE('now', '-6 days') "
            "GROUP BY DATE(created_at)",
            (telegram_id,),
        ).fetchall()
    finally:
        conn.close()
    accuracy_total = sessions[3] or 0
    accuracy = round((sessions[1] or 0) * 100 / accuracy_total) if accuracy_total else 0
    return {
        "words_learned": vocabulary[0] or 0,
        "mastered": vocabulary[1] or 0,
        "words_this_week": vocabulary[2] or 0,
        "new_words_today": vocabulary[3] or 0,
        "due": vocabulary[4] or 0,
        "reviews": sessions[0] or 0,
        "reviews_today": sessions[2] or 0,
        "accuracy": accuracy,
        "weekly_activity": {day: count for day, count in activity},
    }


def reserve_ai_teacher_request(telegram_id: int, is_premium: bool) -> bool:
    usage_date = get_user_local_date(telegram_id).isoformat()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO ai_teacher_usage (telegram_id, usage_date) VALUES (?, ?)",
            (telegram_id, usage_date),
        )
        row = conn.execute(
            "SELECT request_count FROM ai_teacher_usage WHERE telegram_id = ? AND usage_date = ?",
            (telegram_id, usage_date),
        ).fetchone()
        if not is_premium and row[0] >= config.FREE_DAILY_AI_TEACHER_LIMIT:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE ai_teacher_usage SET request_count = request_count + 1 "
            "WHERE telegram_id = ? AND usage_date = ?",
            (telegram_id, usage_date),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Could not reserve AI Teacher request")
        return False
    finally:
        conn.close()


def save_word(
    telegram_id: int,
    word: str,
    transcription: str,
    translation: str,
    example: str,
    cefr: str,
) -> str:
    """Save a word once; return saved, exists, limit, or error."""

    conn = None
    try:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT id FROM vocabulary "
            "WHERE telegram_id = ? AND lower(word) = lower(?) LIMIT 1",
            (telegram_id, word.strip()),
        )
        if cursor.fetchone():
            conn.rollback()
            return "exists"

        user_row = cursor.execute(
            "SELECT is_premium FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        is_premium = bool(user_row and user_row[0]) or is_privileged_user(telegram_id)
        if not is_premium:
            words_today = cursor.execute(
                "SELECT COUNT(*) FROM vocabulary WHERE telegram_id = ? "
                "AND DATE(created_at) = DATE('now')",
                (telegram_id,),
            ).fetchone()[0]
            if words_today >= config.FREE_DAILY_NEW_WORD_LIMIT:
                conn.rollback()
                return "limit"

        next_review = (
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
        ).isoformat(sep=" ", timespec="seconds")
        cursor.execute(
            "INSERT INTO vocabulary "
            "(telegram_id, word, transcription, translation, example, cefr, "
            "next_review, interval_days, difficulty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'learning')",
            (telegram_id, word.strip(), transcription, translation, example, cefr, next_review),
        )
        conn.commit()
        return "saved"
    except sqlite3.Error:
        logger.exception("Error saving word")
        if conn:
            conn.rollback()
        return "error"
    finally:
        if conn:
            conn.close()


def get_user_vocabulary(
    telegram_id: int,
    limit: int = 10,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, word, transcription, translation, example, cefr, created_at, "
            "review_count, repetitions, ease_factor, interval_days, next_review, "
            "last_review, difficulty FROM vocabulary WHERE telegram_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (telegram_id, limit, offset),
        ).fetchall()
        keys = (
            "id", "word", "transcription", "translation", "example", "cefr",
            "created_at", "review_count", "repetitions", "ease_factor",
            "interval_days", "next_review", "last_review", "difficulty",
        )
        return [dict(zip(keys, row)) for row in rows]
    finally:
        conn.close()


def get_vocabulary_word(telegram_id: int, word_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM vocabulary WHERE telegram_id = ? AND id = ?",
            (telegram_id, word_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_saved_word_by_text(telegram_id: int, word: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM vocabulary WHERE telegram_id = ? AND lower(word) = lower(?)",
            (telegram_id, word),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def stage_lesson_candidates(
    telegram_id: int,
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM lesson_candidates WHERE created_at < datetime('now', '-7 days')"
        )
        staged = []
        for candidate in candidates:
            word = str(candidate.get("word", "")).strip()
            translation = str(candidate.get("translation", "")).strip()
            if not word or not translation:
                continue
            cursor.execute(
                "INSERT INTO lesson_candidates "
                "(telegram_id, word, transcription, translation, example, cefr) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    telegram_id,
                    word,
                    str(candidate.get("transcription", "")),
                    translation,
                    str(candidate.get("example", "")),
                    str(candidate.get("cefr", "")),
                ),
            )
            staged.append({**candidate, "candidate_id": cursor.lastrowid})
        conn.commit()
        return staged
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Error staging lesson vocabulary")
        return []
    finally:
        conn.close()


def save_lesson_candidate(telegram_id: int, candidate_id: int) -> str:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT word, transcription, translation, example, cefr "
            "FROM lesson_candidates WHERE id = ? AND telegram_id = ?",
            (candidate_id, telegram_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "missing"
    return save_word(telegram_id, *row)


def delete_vocabulary_word(telegram_id: int, word_id: int) -> bool:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(
            "DELETE FROM practice_sessions WHERE telegram_id = ? AND word_id = ?",
            (telegram_id, word_id),
        )
        cursor = conn.execute(
            "DELETE FROM vocabulary WHERE telegram_id = ? AND id = ?",
            (telegram_id, word_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Error deleting vocabulary word")
        return False
    finally:
        conn.close()


def get_due_vocabulary(telegram_id: int, limit: int = 1000) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, word, transcription, translation, example, cefr, created_at, "
            "review_count, repetitions, ease_factor, interval_days, next_review, "
            "last_review, difficulty FROM vocabulary "
            "WHERE telegram_id = ? AND next_review <= ? "
            "ORDER BY next_review, created_at LIMIT ?",
            (telegram_id, now, limit),
        ).fetchall()
        keys = (
            "id", "word", "transcription", "translation", "example", "cefr",
            "created_at", "review_count", "repetitions", "ease_factor",
            "interval_days", "next_review", "last_review", "difficulty",
        )
        return [dict(zip(keys, row)) for row in rows]
    finally:
        conn.close()


def schedule_word_review(telegram_id: int, word_id: int, rating: str) -> bool:
    intervals = (1, 3, 7, 14, 30, 60, 90)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT repetitions, ease_factor FROM vocabulary "
            "WHERE telegram_id = ? AND id = ?",
            (telegram_id, word_id),
        ).fetchone()
        if not row:
            return False

        repetitions, ease_factor = row
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if rating == "again":
            repetitions = 0
            interval_days = 0
            next_review = now + timedelta(minutes=10)
            ease_factor = max(1.3, ease_factor - 0.2)
            difficulty = "learning"
        elif rating == "hard":
            repetitions += 1
            interval_days = 1
            next_review = now + timedelta(days=1)
            ease_factor = max(1.3, ease_factor - 0.15)
            difficulty = "hard"
        elif rating == "easy":
            repetitions += 1
            interval_days = intervals[min(repetitions - 1, len(intervals) - 1)]
            next_review = now + timedelta(days=interval_days)
            ease_factor = min(3.0, ease_factor + 0.1)
            difficulty = "easy"
        else:
            return False

        conn.execute(
            "UPDATE vocabulary SET repetitions = ?, ease_factor = ?, "
            "interval_days = ?, next_review = ?, last_review = ?, "
            "review_count = review_count + 1, difficulty = ? "
            "WHERE telegram_id = ? AND id = ?",
            (
                repetitions,
                ease_factor,
                interval_days,
                next_review.isoformat(sep=" ", timespec="seconds"),
                now.isoformat(sep=" ", timespec="seconds"),
                difficulty,
                telegram_id,
                word_id,
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Error scheduling vocabulary review")
        return False
    finally:
        conn.close()


def count_user_vocabulary(telegram_id: int) -> int:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM vocabulary WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()