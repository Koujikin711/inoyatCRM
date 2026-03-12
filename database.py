# -*- coding: utf-8 -*-
# БД для обучающей платформы: aiosqlite, таблицы users, lessons, user_video_sent

import aiosqlite
from datetime import datetime

try:
    from config import DB_PATH, QUIZ_AFTER_MINUTES
except ImportError:
    DB_PATH = "bot_platform.db"
    QUIZ_AFTER_MINUTES = 1440


async def init_db():
    """Создание таблиц при старте."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                surname TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                current_lesson INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_num INTEGER UNIQUE NOT NULL,
                file_id TEXT NOT NULL,
                title TEXT NOT NULL,
                question TEXT NOT NULL,
                option1 TEXT NOT NULL,
                option2 TEXT NOT NULL,
                option3 TEXT NOT NULL,
                correct_num INTEGER NOT NULL CHECK(correct_num IN (1, 2, 3)),
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_video_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lesson_num INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await conn.commit()


async def add_user_pending(user_id: int, surname: str, name: str) -> None:
    """Добавить пользователя со статусом pending после регистрации."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO users (user_id, surname, name, status, current_lesson, created_at, updated_at) VALUES (?, ?, ?, 'pending', 1, ?, ?)",
            (user_id, surname.strip(), name.strip(), now, now),
        )
        await conn.commit()


async def get_pending_users():
    """Список заявок на модерацию (user_id, surname, name)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT user_id, surname, name FROM users WHERE status = 'pending' ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()


async def set_user_status(user_id: int, status: str) -> None:
    """Изменить статус пользователя: pending, active, rejected."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
            (status, now, user_id),
        )
        await conn.commit()


async def get_user(user_id: int):
    """Получить пользователя по user_id (row или None)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT user_id, surname, name, status, current_lesson, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchone()


async def save_lesson(lesson_num: int, file_id: str, title: str, question: str, option1: str, option2: str, option3: str, correct_num: int) -> None:
    """Сохранить или обновить урок (из группы-архива)."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """INSERT INTO lessons (lesson_num, file_id, title, question, option1, option2, option3, correct_num, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(lesson_num) DO UPDATE SET
               file_id=excluded.file_id, title=excluded.title, question=excluded.question,
               option1=excluded.option1, option2=excluded.option2, option3=excluded.option3,
               correct_num=excluded.correct_num""",
            (lesson_num, file_id, title, question, option1, option2, option3, correct_num, now),
        )
        await conn.commit()


async def get_lesson(lesson_num: int):
    """Получить урок по номеру (для отправки видео и викторины)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT lesson_num, file_id, title, question, option1, option2, option3, correct_num FROM lessons WHERE lesson_num = ?",
            (lesson_num,),
        ) as cur:
            return await cur.fetchone()


async def get_next_lesson_num() -> int:
    """Максимальный номер урока + 1 (для автонумерации при добавлении из группы)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT COALESCE(MAX(lesson_num), 0) + 1 FROM lessons") as cur:
            row = await cur.fetchone()
            return row[0] if row else 1


async def save_video_sent(user_id: int, lesson_num: int, message_id: int) -> None:
    """Записать отправку видео пользователю (для удаления через 24ч и викторины)."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO user_video_sent (user_id, lesson_num, message_id, sent_at) VALUES (?, ?, ?, ?)",
            (user_id, lesson_num, message_id, now),
        )
        await conn.commit()


async def get_due_video_sends():
    """Список записей, по которым прошло QUIZ_AFTER_MINUTES (user_id, lesson_num, message_id)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT id, user_id, lesson_num, message_id FROM user_video_sent
               WHERE datetime(sent_at) <= datetime('now', ?)
               ORDER BY sent_at ASC""",
            (f"-{QUIZ_AFTER_MINUTES} minutes",),
        ) as cur:
            return await cur.fetchall()


async def delete_video_sent_record(record_id: int) -> None:
    """Удалить запись после обработки (чтобы не обрабатывать повторно)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM user_video_sent WHERE id = ?", (record_id,))
        await conn.commit()


async def advance_user_lesson(user_id: int) -> None:
    """Перевести пользователя на следующий урок."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET current_lesson = current_lesson + 1, updated_at = ? WHERE user_id = ?",
            (datetime.now().isoformat(), user_id),
        )
        await conn.commit()


async def get_stats_by_lesson():
    """Статистика: сколько человек на каком уроке (lesson_num, count)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT current_lesson AS lesson_num, COUNT(*) AS cnt
               FROM users WHERE status = 'active' GROUP BY current_lesson ORDER BY current_lesson"""
        ) as cur:
            return await cur.fetchall()


async def get_all_users():
    """Список всех пользователей для админа (user_id, surname, name, status, current_lesson)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT user_id, surname, name, status, current_lesson FROM users ORDER BY created_at DESC"
        ) as cur:
            return await cur.fetchall()


async def get_active_users_for_lesson(lesson_num: int):
    """Пользователи, которые сейчас на уроке lesson_num (для массовой рассылки при добавлении урока — опционально)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT user_id FROM users WHERE status = 'active' AND current_lesson = ?",
            (lesson_num,),
        ) as cur:
            return await cur.fetchall()
