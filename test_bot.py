import os
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import bot
import config


class BotRegressionTests(unittest.TestCase):
    def test_wordday_photo_practice_edits_caption_not_text(self):
        message = SimpleNamespace(
            photo=[object()],
            edit_caption=AsyncMock(),
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        )

        bot.asyncio.run(
            bot.send_wordday_practice_prompt(cast(bot.types.Message, message), "Practice prompt")
        )

        message.edit_caption.assert_awaited_once()
        message.answer.assert_awaited_once()
        message.edit_text.assert_not_awaited()

    def test_main_menu_is_compact_and_keeps_learning_actions(self):
        keyboard = bot.get_main_menu_keyboard()
        rows = keyboard.keyboard
        labels = {button.text for row in rows for button in row}

        self.assertLessEqual(len(rows), 6)
        self.assertTrue({
            "🎬 Analyze Video",
            "📖 Review Today",
            "📚 My Words",
            "💎 Word of the Day",
            "🎯 Daily Challenge",
            "🤖 AI Teacher",
            "📊 Progress",
            "🔥 Streak",
            "🎬 History",
            "⚙️ Settings",
            "⭐ Premium",
        }.issubset(labels))
        self.assertTrue(os.path.isfile(os.path.join("assets", "welcome_study.png")))
        self.assertTrue(os.path.isfile(os.path.join("assets", "word_of_day.png")))

    def test_config_allows_missing_environment_values(self):
        with patch.dict(os.environ, {}, clear=True), patch("dotenv.load_dotenv", return_value=False):
            import importlib
            import config as config_module

            reloaded = importlib.reload(config_module)

            self.assertEqual(reloaded.BOT_TOKEN, "")
            self.assertEqual(reloaded.OPENAI_API_KEY, "")

    def test_parse_vtt_keeps_colons_in_caption_text(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Time: to learn.
"""

        self.assertEqual(bot.parse_vtt(vtt), "Time: to learn.")

    def test_parse_vtt_ignores_vtt_metadata_lines(self):
        vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Kind: captions
Language: en
Hello world

00:00:03.000 --> 00:00:05.000
Nice to meet you.
"""

        self.assertEqual(bot.parse_vtt(vtt), "Hello world Nice to meet you.")

    def test_get_subtitles_reads_subtitle_url(self):
        class Response:
            def read(self):
                return b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello world"

        class YoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {"subtitles": {"en": [{"url": "https://example.test/sub.vtt"}]}}

            def urlopen(self, url):
                self.requested_url = url
                return Response()

        with patch.object(bot.yt_dlp, "YoutubeDL", YoutubeDL):
            self.assertEqual(
                bot.asyncio.run(bot.get_subtitles("https://example.test/video")),
                "Hello world",
            )

    def test_current_streak_counts_each_date_once(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                connection = sqlite3.connect(database_path)
                connection.execute(
                    "INSERT INTO users (telegram_id) VALUES (?)", (1,)
                )
                connection.execute(
                    "INSERT INTO practice_sessions (telegram_id, word_id, created_at) VALUES (?, ?, ?)",
                    (1, 1, f"{date.today()} 10:00:00"),
                )
                connection.execute(
                    "INSERT INTO practice_sessions (telegram_id, word_id, created_at) VALUES (?, ?, ?)",
                    (1, 1, f"{date.today() - timedelta(days=1)} 09:00:00"),
                )
                connection.execute(
                    "INSERT INTO practice_sessions (telegram_id, word_id, created_at) VALUES (?, ?, ?)",
                    (1, 1, f"{date.today() - timedelta(days=1)} 10:00:00"),
                )
                connection.commit()
                connection.close()

                self.assertEqual(bot.get_current_streak(1), 2)
        finally:
            os.unlink(database_path)

    def test_database_migration_preserves_existing_vocabulary(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, "
                "level TEXT NOT NULL DEFAULT 'B1', is_premium INTEGER DEFAULT 0, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "CREATE TABLE vocabulary (id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL, "
                "word TEXT NOT NULL, transcription TEXT, translation TEXT NOT NULL, "
                "example TEXT, cefr TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "INSERT INTO users (telegram_id) VALUES (42)"
            )
            connection.execute(
                "INSERT INTO vocabulary (telegram_id, word, translation) "
                "VALUES (42, 'overwhelmed', 'перегруженный')"
            )
            connection.commit()
            connection.close()

            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                word = bot.get_user_vocabulary(42)[0]
                self.assertEqual(word["word"], "overwhelmed")
                self.assertEqual(word["review_count"], 0)
                self.assertIn("next_review", word)
        finally:
            os.unlink(database_path)

    def test_duplicate_vocabulary_and_review_schedule(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                bot.get_or_create_user(77)
                self.assertEqual(
                    bot.save_word(77, "overwhelmed", "/.../", "перегруженный", "", "B2"),
                    "saved",
                )
                self.assertEqual(
                    bot.save_word(77, "OVERWHELMED", "/.../", "перегруженный", "", "B2"),
                    "exists",
                )
                word = bot.get_user_vocabulary(77)[0]
                self.assertTrue(bot.schedule_word_review(77, word["id"], "easy"))
                reviewed = bot.get_vocabulary_word(77, word["id"])
                if reviewed is None:
                    self.fail("The reviewed vocabulary row should still exist")
                self.assertEqual(reviewed["review_count"], 1)
                self.assertEqual(reviewed["interval_days"], 1)
                self.assertEqual(reviewed["difficulty"], "easy")
        finally:
            os.unlink(database_path)

    def test_lesson_candidate_save_delete_and_history_round_trip(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                bot.get_or_create_user(88)
                staged = bot.stage_lesson_candidates(88, [{
                    "word": "overwhelmed",
                    "transcription": "/.../",
                    "translation": "перегруженный",
                    "example": "I felt overwhelmed.",
                    "cefr": "B2",
                }])
                candidate_id = staged[0]["candidate_id"]
                self.assertEqual(bot.save_lesson_candidate(88, candidate_id), "saved")
                self.assertEqual(bot.save_lesson_candidate(88, candidate_id), "exists")
                saved_word = bot.get_user_vocabulary(88)[0]
                self.assertTrue(bot.delete_vocabulary_word(88, saved_word["id"]))
                self.assertFalse(bot.delete_vocabulary_word(88, saved_word["id"]))

                analysis = {
                    "vocabulary": [{"word": "focus", "translation": "сосредоточиться"}],
                    "summary": "A lesson",
                }
                video_id = bot.save_video(88, "https://example.test/video", "Lesson", analysis, "B2")
                if video_id is None:
                    self.fail("The saved video should have a database ID")
                history = bot.get_video_history(88)
                self.assertEqual(history[0]["vocabulary_count"], 1)
                lesson = bot.get_video_lesson(88, int(video_id))
                if lesson is None:
                    self.fail("The saved lesson should be retrievable")
                self.assertEqual(lesson["analysis"]["summary"], "A lesson")
        finally:
            os.unlink(database_path)

    def test_ai_teacher_daily_limit_is_atomic_and_plan_aware(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                bot.get_or_create_user(99)
                for _ in range(config.FREE_DAILY_AI_TEACHER_LIMIT):
                    self.assertTrue(bot.reserve_ai_teacher_request(99, False))
                self.assertFalse(bot.reserve_ai_teacher_request(99, False))
                self.assertTrue(bot.reserve_ai_teacher_request(99, True))
        finally:
            os.unlink(database_path)

    def test_free_vocabulary_limit_and_premium_bypass(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path), patch.object(
                config, "FREE_DAILY_NEW_WORD_LIMIT", 1
            ):
                bot.init_database()
                bot.get_or_create_user(101)
                self.assertEqual(
                    bot.save_word(101, "first", "", "первый", "", "A1"),
                    "saved",
                )
                self.assertEqual(
                    bot.save_word(101, "second", "", "второй", "", "A1"),
                    "limit",
                )
                connection = sqlite3.connect(database_path)
                connection.execute(
                    "UPDATE users SET is_premium = 1 WHERE telegram_id = 101"
                )
                connection.commit()
                connection.close()
                self.assertEqual(
                    bot.save_word(101, "second", "", "второй", "", "A1"),
                    "saved",
                )
        finally:
            os.unlink(database_path)

    def test_owner_and_admin_have_premium_without_payment(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path), \
                    patch.object(config, "OWNER_ID", 202), \
                    patch.object(config, "ADMIN_IDS", {303}), \
                    patch.object(config, "FREE_DAILY_NEW_WORD_LIMIT", 1):
                bot.init_database()
                owner = bot.get_or_create_user(202)
                admin = bot.get_or_create_user(303)
                self.assertTrue(owner["is_premium"])
                self.assertTrue(admin["is_premium"])
                self.assertEqual(
                    bot.save_word(303, "first", "", "первый", "", "A1"),
                    "saved",
                )
                self.assertEqual(
                    bot.save_word(303, "second", "", "второй", "", "A1"),
                    "saved",
                )
        finally:
            os.unlink(database_path)

    def test_stars_payment_validation_and_idempotent_activation(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path), \
                    patch.object(config, "OWNER_ID", 1), \
                    patch.object(config, "ADMIN_IDS", set()):
                bot.init_database()
                user_id = 404
                bot.get_or_create_user(user_id)
                payload = bot.create_premium_payload(user_id)
                self.assertTrue(bot.is_valid_premium_payment(
                    user_id, payload, "XTR", config.PREMIUM_PRICE_STARS
                ))
                self.assertFalse(bot.is_valid_premium_payment(
                    user_id, payload, "USD", config.PREMIUM_PRICE_STARS
                ))
                self.assertFalse(bot.is_valid_premium_payment(
                    user_id, payload, "XTR", config.PREMIUM_PRICE_STARS - 1
                ))
                self.assertEqual(
                    bot.record_premium_payment(
                        user_id, payload, "XTR", config.PREMIUM_PRICE_STARS,
                        "telegram-charge-1", "provider-charge-1",
                    ),
                    "activated",
                )
                self.assertEqual(
                    bot.record_premium_payment(
                        user_id, payload, "XTR", config.PREMIUM_PRICE_STARS,
                        "telegram-charge-1", "provider-charge-1",
                    ),
                    "duplicate",
                )
                self.assertTrue(bot.get_or_create_user(user_id)["is_premium"])
                connection = sqlite3.connect(database_path)
                payment_count = connection.execute(
                    "SELECT COUNT(*) FROM premium_payments"
                ).fetchone()[0]
                connection.close()
                self.assertEqual(payment_count, 1)
        finally:
            os.unlink(database_path)

    def test_practice_prompt_supports_all_exercise_types(self):
        words = [
            {
                "id": index,
                "word": word,
                "translation": translation,
                "example": example,
                "transcription": "/ipa/",
                "cefr": "B2",
            }
            for index, (word, translation, example) in enumerate([
                ("overwhelmed", "перегруженный", "I felt overwhelmed after school."),
                ("relieved", "испытавший облегчение", "She felt relieved."),
                ("curious", "любопытный", "He was curious about it."),
                ("efficient", "эффективный", "It was an efficient process."),
                ("hesitate", "колебаться", "Don't hesitate to ask."),
            ], 1)
        ]
        for exercise_type in ("meaning", "translation", "multiple_choice", "fill_blank", "context"):
            text, keyboard, correct_index = bot.build_practice_prompt(
                words[0], exercise_type, words
            )
            self.assertTrue(text)
            if keyboard is None:
                self.fail("Practice prompts should include a keyboard")
            if exercise_type == "multiple_choice":
                self.assertIsNotNone(correct_index)
                self.assertEqual(len(keyboard.inline_keyboard), 4)
            if exercise_type == "fill_blank":
                self.assertIn("______", text)

    def test_streak_dates_use_configured_timezone(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as database:
            database_path = database.name

        try:
            with patch.object(config, "DB_PATH", database_path):
                bot.init_database()
                bot.get_or_create_user(111)
                bot.update_user_settings(111, timezone="America/New_York")
                connection = sqlite3.connect(database_path)
                connection.execute(
                    "INSERT INTO practice_sessions (telegram_id, word_id, created_at) VALUES (?, ?, ?)",
                    (111, 1, "2026-09-26 03:30:00"),
                )
                connection.execute(
                    "INSERT INTO practice_sessions (telegram_id, word_id, created_at) VALUES (?, ?, ?)",
                    (111, 1, "2026-09-27 02:00:00"),
                )
                connection.commit()
                connection.close()
                self.assertEqual(
                    bot.get_learning_dates(111),
                    [date(2026, 9, 25), date(2026, 9, 26)],
                )
        finally:
            os.unlink(database_path)

    def test_utc_timezone_works_without_iana_database(self):
        with patch.object(
            bot,
            "ZoneInfo",
            side_effect=bot.ZoneInfoNotFoundError("timezone database unavailable"),
        ):
            resolved_timezone = bot.get_timezone_by_name("UTC")
        self.assertEqual(resolved_timezone.utcoffset(None), timedelta(0))

    def test_invalid_ai_json_is_retried_once(self):
        valid_payload = {
            "expressions": [],
            "vocabulary": [],
            "natural_english": [],
            "native_recommendations": [],
            "ielts": {"collocations": [], "practice_questions": []},
            "estimated_level": "B2",
            "summary": "Short summary",
        }
        responses = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(valid_payload)))]),
        ]
        with patch.object(
            bot.openai_client.chat.completions,
            "create",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as create:
            result = bot.asyncio.run(bot.analyze_transcript("A short transcript", "B2"))
        if result is None:
            self.fail("The valid AI retry response should be accepted")
        self.assertEqual(result["summary"], "Short summary")
        self.assertEqual(create.await_count, 2)

    def test_format_lesson_escapes_dynamic_html(self):
        lesson = bot.format_lesson(
            {
                "expressions": [{
                    "expression": "<script>",
                    "translation": "a & b",
                    "example": "Use <this>.",
                }],
                "summary": "A <short> summary",
            },
            "Title & more",
        )

        self.assertIn("&lt;script&gt;", lesson)
        self.assertIn("a &amp; b", lesson)
        self.assertNotIn("<script>", lesson)


if __name__ == "__main__":
    unittest.main()