# -*- coding: utf-8 -*-
"""
Обучающая платформа для учеников + CRM для админа.
Регистрация (ФИ) -> модерация -> уроки из группы-архива, через 24ч викторина.
aiogram 3.x, aiosqlite, APScheduler.
"""

import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import ADMIN_ID, ARCHIVE_GROUP_ID, BOT_TOKEN, DB_PATH, QUIZ_AFTER_MINUTES
from database import (
    add_user_pending,
    advance_user_lesson,
    delete_video_sent_record,
    get_active_users_for_lesson,
    get_all_users,
    get_due_video_sends,
    get_lesson,
    get_next_lesson_num,
    get_stats_by_lesson,
    get_user,
    init_db,
    save_lesson,
    save_video_sent,
    set_user_status,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# Клавиатура владельца: одна кнопка «Статистика»
def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📊 Статистика")]],
        resize_keyboard=True,
    )


# --- FSM для регистрации ---
class RegStates(StatesGroup):
    surname = State()
    name = State()


# --- Разбор описания урока из подписи группы ---
# Формат: Заголовок урока | Вопрос | Вариант1, Вариант2, Вариант3 | Номер_Правильного_Ответа(1-3)
def parse_lesson_caption(caption: str):
    if not caption or "|" not in caption:
        return None
    parts = [p.strip() for p in caption.split("|")]
    if len(parts) < 4:
        return None
    title = parts[0]
    question = parts[1]
    opts_str = parts[2]
    try:
        correct_num = int(parts[3].strip())
        if correct_num not in (1, 2, 3):
            return None
    except ValueError:
        return None
    options = [o.strip() for o in opts_str.split(",")]
    if len(options) < 3:
        return None
    option1, option2, option3 = options[0], options[1], options[2]
    # Номер урока из заголовка (Урок 1, Урок 2, ...) или None
    m = re.search(r"[Уу]рок\s*(\d+)", title, re.I)
    lesson_num = int(m.group(1)) if m else None
    return {
        "title": title,
        "question": question,
        "option1": option1,
        "option2": option2,
        "option3": option3,
        "correct_num": correct_num,
        "lesson_num": lesson_num,
    }


async def send_lesson_to_waiting_users(lesson_num: int) -> int:
    """
    Отправить урок всем активным пользователям, которые сейчас на этом уроке
    (в т.ч. тем, кого допустили до появления видео). Возвращает кол-во отправленных.
    """
    lesson = await get_lesson(lesson_num)
    if not lesson:
        return 0
    users = await get_active_users_for_lesson(lesson_num)
    sent = 0
    for row in users:
        user_id = row["user_id"]
        try:
            msg = await bot.send_video(
                user_id,
                video=lesson["file_id"],
                caption=lesson["title"],
                protect_content=True,
            )
            await save_video_sent(user_id, lesson_num, msg.message_id)
            sent += 1
        except Exception as e:
            logger.warning("Не удалось отправить урок %s пользователю %s: %s", lesson_num, user_id, e)
    return sent


# --- Регистрация: Фамилия и Имя ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    # Владелец: показываем кнопку «Статистика» и выходим
    if user_id == ADMIN_ID:
        print(f"[INOYAT_CRM] cmd_start: user_id={user_id} == ADMIN_ID -> панель админа")
        logger.info("cmd_start: user_id=%s совпадает с ADMIN_ID -> панель админа", user_id)
        await state.clear()
        await message.answer(
            "Панель администратора. Нажмите кнопку ниже:",
            reply_markup=admin_keyboard(),
        )
        return
    print(f"[INOYAT_CRM] cmd_start: user_id={user_id}, ADMIN_ID={ADMIN_ID} -> не админ, запрашиваем ФИО")
    logger.info("cmd_start: user_id=%s, ADMIN_ID=%s -> не админ, запрашиваем ФИО", user_id, ADMIN_ID)
    user = await get_user(user_id)
    if user:
        status = user["status"]
        if status == "pending":
            await message.answer("Ваша заявка на рассмотрении. Ожидайте решения администратора.")
            return
        if status == "rejected":
            await message.answer("Ваша заявка была отклонена. Если есть вопросы — обратитесь к администратору.")
            return
        if status == "active":
            await message.answer("Вы уже зарегистрированы. Продолжайте обучение.")
            return
    await state.set_state(RegStates.surname)
    await message.answer("Добро пожаловать! Для доступа к урокам введите вашу <b>Фамилию</b>.", parse_mode="HTML")


@dp.message(F.text, RegStates.surname)
async def reg_surname(message: types.Message, state: FSMContext):
    surname = (message.text or "").strip()
    if not surname:
        await message.answer("Введите фамилию текстом.")
        return
    await state.update_data(reg_surname=surname)
    await state.set_state(RegStates.name)
    await message.answer("Теперь введите ваше <b>Имя</b>.", parse_mode="HTML")


@dp.message(F.text, RegStates.name)
async def reg_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Введите имя текстом.")
        return
    data = await state.get_data()
    surname = data.get("reg_surname", "")
    await state.clear()
    await add_user_pending(message.from_user.id, surname, name)
    full_name = f"{surname} {name}"
    await message.answer(
        f"Спасибо, {full_name}! Ваша заявка отправлена на проверку. Вы получите уведомление после одобрения."
    )
    # Уведомление админу с кнопками Допустить / Отклонить
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Допустить", callback_data=f"mod_allow_{message.from_user.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_reject_{message.from_user.id}"),
        ]
    ])
    await bot.send_message(
        ADMIN_ID,
        f"Новая заявка: <b>{full_name}</b> (id: {message.from_user.id})",
        parse_mode="HTML",
        reply_markup=kb,
    )


# --- Модерация заявок ---
@dp.callback_query(F.data.startswith("mod_allow_"))
async def mod_allow(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Доступ запрещён.", show_alert=True)
        return
    user_id = int(cb.data.replace("mod_allow_", ""))
    await set_user_status(user_id, "active")
    user = await get_user(user_id)
    full_name = f"{user['surname']} {user['name']}"
    await cb.message.edit_text(f"✅ Заявка одобрена: {full_name}")
    await cb.answer()

    # Приветствие и Урок 1
    try:
        interval_text = "1 минуту" if QUIZ_AFTER_MINUTES == 1 else f"{QUIZ_AFTER_MINUTES // 60} ч"
        await bot.send_message(
            user_id,
            f"Поздравляем! Вам открыт доступ к урокам. Ниже первое видео — через {interval_text} оно исчезнет и вам будет задан вопрос по материалу.",
        )
        lesson = await get_lesson(1)
        if lesson:
            msg = await bot.send_video(
                user_id,
                video=lesson["file_id"],
                caption=lesson["title"],
                protect_content=True,
            )
            await save_video_sent(user_id, 1, msg.message_id)
        else:
            await bot.send_message(user_id, "Урок 1 пока не добавлен. Ожидайте.")
    except Exception as e:
        logger.exception("Ошибка отправки приветствия/урока 1: %s", e)
        await bot.send_message(ADMIN_ID, f"Не удалось отправить урок 1 пользователю {user_id}: {e}")


@dp.callback_query(F.data.startswith("mod_reject_"))
async def mod_reject(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Доступ запрещён.", show_alert=True)
        return
    user_id = int(cb.data.replace("mod_reject_", ""))
    await set_user_status(user_id, "rejected")
    user = await get_user(user_id)
    full_name = f"{user['surname']} {user['name']}"
    await cb.message.edit_text(f"❌ Заявка отклонена: {full_name}")
    await cb.answer()
    try:
        await bot.send_message(user_id, "К сожалению, ваша заявка была отклонена.")
    except Exception:
        pass


# --- Сообщения из группы-архива: парсим урок и сохраняем ---
@dp.message(F.chat.id == ARCHIVE_GROUP_ID, F.video, F.caption)
async def on_archive_video(message: types.Message):
    caption = message.caption
    parsed = parse_lesson_caption(caption)
    if not parsed:
        logger.warning("Группа: не распознан формат подписи: %s", (caption or "")[:100])
        return
    lesson_num = parsed["lesson_num"] or await get_next_lesson_num()
    file_id = message.video.file_id
    await save_lesson(
        lesson_num,
        file_id,
        parsed["title"],
        parsed["question"],
        parsed["option1"],
        parsed["option2"],
        parsed["option3"],
        parsed["correct_num"],
    )
    logger.info("Сохранён урок %s из группы: %s", lesson_num, parsed["title"])
    # Кто уже допущен и ждёт этот урок — сразу получают видео
    sent = await send_lesson_to_waiting_users(lesson_num)
    if sent:
        logger.info("Урок %s отправлен %s пользователям", lesson_num, sent)


# --- Владелец прислал видео в ЛС (с подписью в формате урока) или переслал из группы — сохранить и разослать ждущим ---
@dp.message(F.chat.id == ADMIN_ID, F.video, F.caption)
async def on_admin_video(message: types.Message):
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat_id = getattr(getattr(origin, "sender_chat", None), "id", None) or getattr(getattr(origin, "chat", None), "id", None)
        if chat_id != ARCHIVE_GROUP_ID:
            return
    caption = message.caption
    parsed = parse_lesson_caption(caption)
    if not parsed:
        await message.reply(
            "Неверный формат подписи. Нужно:\n"
            "Заголовок | Вопрос | Вариант1, Вариант2, Вариант3 | 1-3\n"
            "Например: Урок 1. Введение | Что такое X? | Да, Нет, Не знаю | 2"
        )
        return
    lesson_num = parsed["lesson_num"] or await get_next_lesson_num()
    file_id = message.video.file_id
    await save_lesson(
        lesson_num,
        file_id,
        parsed["title"],
        parsed["question"],
        parsed["option1"],
        parsed["option2"],
        parsed["option3"],
        parsed["correct_num"],
    )
    await message.reply(f"Урок {lesson_num} сохранён: {parsed['title']}")
    sent = await send_lesson_to_waiting_users(lesson_num)
    if sent:
        await message.reply(f"Видео отправлено {sent} пользователям, которые ждут этот урок.")
    else:
        await message.reply("Ждущих этот урок пока нет.")


# --- Админ: /admin или кнопка «Статистика» ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await _send_admin_panel(message)


@dp.message(F.text == "📊 Статистика", F.from_user.id == ADMIN_ID)
async def admin_btn_statistics(message: types.Message):
    await _send_admin_panel(message)


async def _send_admin_panel(message: types.Message):
    """Показать админу инлайн-кнопки: Статистика по урокам, Список пользователей."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика по урокам", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📋 Список пользователей", callback_data="admin_list")],
    ])
    await message.answer("Выберите:", reply_markup=kb)


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    rows = await get_stats_by_lesson()
    if not rows:
        text = "Нет активных учеников по урокам."
    else:
        lines = ["<b>Статистика в реальном времени</b>\n"]
        for r in rows:
            lines.append(f"Урок {r['lesson_num']}: {r['cnt']} чел.")
        text = "\n".join(lines)
    await cb.message.edit_text(text, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "admin_list")
async def admin_list(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    rows = await get_all_users()
    if not rows:
        text = "Нет пользователей."
    else:
        lines = ["<b>Список пользователей</b>\n"]
        for r in rows:
            status_ru = {"pending": "⏳ на модерации", "active": "✅ активен", "rejected": "❌ отклонён"}.get(r["status"], r["status"])
            lines.append(f"• {r['surname']} {r['name']} — {status_ru}, урок {r['current_lesson']} (id: {r['user_id']})")
        text = "\n".join(lines)
    await cb.message.edit_text(text, parse_mode="HTML")
    await cb.answer()


# --- Фоновая задача: через 24ч удалить видео и отправить викторину ---
async def job_24h():
    due = await get_due_video_sends()
    for row in due:
        record_id, user_id, lesson_num, message_id = row["id"], row["user_id"], row["lesson_num"], row["message_id"]
        try:
            await bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception as e:
            logger.warning("Не удалось удалить сообщение %s у %s: %s", message_id, user_id, e)
        await delete_video_sent_record(record_id)

        lesson = await get_lesson(lesson_num)
        if not lesson:
            await bot.send_message(user_id, "Ошибка: урок не найден. Обратитесь к администратору.")
            continue

        # Викторина: три кнопки с вариантами ответа
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=lesson["option1"], callback_data=f"quiz_ans_{user_id}_{lesson_num}_1")],
            [InlineKeyboardButton(text=lesson["option2"], callback_data=f"quiz_ans_{user_id}_{lesson_num}_2")],
            [InlineKeyboardButton(text=lesson["option3"], callback_data=f"quiz_ans_{user_id}_{lesson_num}_3")],
        ])
        await bot.send_message(
            user_id,
            f"<b>{lesson['title']}</b>\n\n{lesson['question']}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await asyncio.sleep(0)


# --- Ответ на викторину ---
@dp.callback_query(F.data.startswith("quiz_ans_"))
async def quiz_answer(cb: CallbackQuery):
    parts = cb.data.replace("quiz_ans_", "").split("_")
    if len(parts) != 3:
        await cb.answer()
        return
    user_id, lesson_num, choice = int(parts[0]), int(parts[1]), int(parts[2])
    if cb.from_user.id != user_id:
        await cb.answer("Это не ваш вопрос.", show_alert=True)
        return

    lesson = await get_lesson(lesson_num)
    if not lesson:
        await cb.answer("Ошибка данных.")
        return

    correct = lesson["correct_num"]
    await cb.message.edit_reply_markup(reply_markup=None)

    if choice == correct:
        await cb.message.answer("Поздравляем! Ответ верный. Отправляем следующий урок.")
        await advance_user_lesson(user_id)
        user = await get_user(user_id)
        next_num = user["current_lesson"]
        next_lesson = await get_lesson(next_num)
        if next_lesson:
            msg = await bot.send_video(
                user_id,
                video=next_lesson["file_id"],
                caption=next_lesson["title"],
                protect_content=True,
            )
            await save_video_sent(user_id, next_num, msg.message_id)
        else:
            await bot.send_message(user_id, "Курс пройден! Новых уроков пока нет.")
    else:
        await cb.message.answer("Нужно повторить материал! Отправляем то же видео ещё на 24 часа.")
        msg = await bot.send_video(
            user_id,
            video=lesson["file_id"],
            caption=lesson["title"],
            protect_content=True,
        )
        await save_video_sent(user_id, lesson_num, msg.message_id)

    await cb.answer()


# --- Точка входа ---
async def main():
    await init_db()
    msg = f"[INOYAT_CRM] ADMIN_ID={ADMIN_ID} (владелец получает заявки и /admin)"
    logger.info(msg)
    print(msg)
    scheduler.add_job(job_24h, "interval", minutes=1)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
