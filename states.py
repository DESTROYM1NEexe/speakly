"""Finite-state definitions used by bot handlers."""

from aiogram.fsm.state import State, StatesGroup


class UserState(StatesGroup):
    choosing_action = State()
    choosing_level = State()
    waiting_for_url = State()
    choosing_vocabulary_page = State()
    practicing_word = State()
    waiting_for_reminder_time = State()
    waiting_for_timezone = State()
    ai_teacher_chat = State()