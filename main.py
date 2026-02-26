import asyncio
import json
import os
import re
import sys
import tempfile
import traceback
import requests
import pandas as pd
from datetime import datetime
from aiohttp import web, ClientSession
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from db import Database
import kb

# --- КОНФИГУРАЦИЯ ---
# CRM ИНОЯТА — Telegram + WhatsApp (Green API)
# Деплой по push в GitHub (webhook)
TOKEN = "8634696282:AAEBajKKapvJpsLZx649GyUX9kCh5jThWHM"
GREEN_URL = "https://7103.api.greenapi.com"
GREEN_ID = "7103530127"
GREEN_TOKEN = "fd8a594875de4d378f56426f27abe1ebc1a79ae12f6d42e29b"
OWNER_ID = 1583163832 

bot = None
dp = Dispatcher()
db = None

class Form(StatesGroup):
    waiting_for_fio = State()
    writing_answer = State()
    finish_paid_total = State()
    finish_paid_amount = State()
    finish_reject = State()
    pay_lead_amount = State()
    analytics_period = State()

# --- WEBHOOK ПРИЕМ СООБЩЕНИЙ ---
# Общая сессия для запросов к Green API (опрос уведомлений)
_green_session: ClientSession = None

async def _process_incoming_wa_message(body: dict) -> bool:
    """Обработать одно входящее сообщение из WA (из вебхука или receiveNotification). Возвращает True если обработано."""
    if db is None or bot is None:
        print("--- WA: пропуск, бот или БД не инициализированы ---")
        return False
    type_wh = (body.get("typeWebhook") or "").strip()
    if type_wh.lower() not in ("incomingmessagereceived", "incomingfilemessagereceived"):
        return False
    sender_data = body.get("senderData") or {}
    raw_chat_id = (sender_data.get("chatId") or "").split("@")[0].strip()
    chat_id = _normalize_phone(raw_chat_id)
    text = _extract_text_from_message(body)
    print(f"--- WA: обрабатываю сообщение от +{chat_id}, текст: {text[:50]!r}... ---")
    sys.stdout.flush()
    db.cur.execute(
        "SELECT manager_id FROM leads WHERE client_phone=? ORDER BY created_at DESC LIMIT 1",
        (chat_id,)
    )
    res = db.cur.fetchone()
    if res:
        target_manager = res[0]
        prefix = "📩 Сообщение"
    else:
        target_manager = db.get_next_manager()
        prefix = "🔥 НОВЫЙ ЛИД"
        if target_manager:
            db.cur.execute("INSERT INTO leads (client_phone, manager_id) VALUES (?, ?)", (chat_id, target_manager))
            db.conn.commit()
    if target_manager:
        msg = f"{prefix}\n📱 Номер: +{chat_id}\n📝: {text}"
        try:
            await bot.send_message(target_manager, msg, reply_markup=kb.lead_card_kb(chat_id))
            print(f"--- WA: отправлено менеджеру {target_manager} ---")
        except Exception as send_err:
            print(f"--- WA: ошибка отправки менеджеру {target_manager}: {send_err} ---")
            try:
                await bot.send_message(OWNER_ID, f"⚠️ Не удалось отправить менеджеру {target_manager}:\n{msg[:200]}")
            except Exception:
                pass
    else:
        msg = f"{prefix}\n📱 Номер: +{chat_id}\n📝: {text}\n⚠️ Нет активных менеджеров."
        print(f"--- WA: нет активных менеджеров, отправляю владельцу {OWNER_ID} ---")
        sys.stdout.flush()
        try:
            await bot.send_message(OWNER_ID, msg)
            print("--- WA: отправлено владельцу ---")
        except Exception as send_err:
            print(f"--- WA: ошибка отправки владельцу: {send_err} ---")
    return True

# --- ОПРОС GREEN API (receiveNotification) — как в рабочем CRM, без вебхука ---
async def check_wa_polling():
    """Фоновая задача: опрашивать receiveNotification и обрабатывать входящие сообщения."""
    global _green_session
    receive_url = f"{GREEN_URL}/waInstance{GREEN_ID}/receiveNotification/{GREEN_TOKEN}"
    delete_url = f"{GREEN_URL}/waInstance{GREEN_ID}/deleteNotification/{GREEN_TOKEN}"
    loop_count = 0
    while True:
        try:
            loop_count += 1
            if loop_count % 120 == 1 and loop_count > 1:
                print("--- WA polling: работаю, жду сообщений из Green API... ---")
                sys.stdout.flush()
            if not _green_session or _green_session.closed:
                await asyncio.sleep(2)
                continue
            if db is None or bot is None:
                await asyncio.sleep(2)
                continue
            async with _green_session.get(receive_url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    if loop_count % 30 == 1:
                        print(f"--- WA polling: HTTP {resp.status} ---")
                        sys.stdout.flush()
                    await asyncio.sleep(1)
                    continue
                try:
                    j = await resp.json()
                except Exception as e:
                    print(f"--- WA polling: ответ не JSON: {e} ---")
                    await asyncio.sleep(1)
                    continue
            if not j:
                await asyncio.sleep(1)
                continue
            rid = j.get("receiptId")
            body = j.get("body") or {}
            type_wh = (body.get("typeWebhook") or "").strip()
            print(f"--- WA polling: уведомление typeWebhook={type_wh!r}, receiptId={rid} ---")
            sys.stdout.flush()
            if type_wh.lower() not in ("incomingmessagereceived", "incomingfilemessagereceived"):
                if rid:
                    try:
                        async with _green_session.delete(f"{delete_url}/{rid}", timeout=aiohttp.ClientTimeout(total=10)) as _:
                            pass
                    except Exception:
                        pass
                await asyncio.sleep(0.3)
                continue
            try:
                await _process_incoming_wa_message(body)
            except Exception as e:
                print(f"--- WA polling: ошибка обработки: {e} ---")
                traceback.print_exc()
            if rid:
                try:
                    async with _green_session.delete(f"{delete_url}/{rid}", timeout=aiohttp.ClientTimeout(total=10)) as _:
                        pass
                except Exception:
                    pass
            await asyncio.sleep(0.3)
        except asyncio.TimeoutError:
            await asyncio.sleep(1)
        except Exception as e:
            print(f"--- WA polling: {e} ---")
            await asyncio.sleep(2)

def _normalize_phone(chat_id: str) -> str:
    """Нормализация номера для единого поиска в БД (8/7XXXXXXXXXX, 9XXXXXXXXX)."""
    digits = re.sub(r"\D", "", str(chat_id))
    if not digits:
        return (chat_id or "").strip()
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits

def _extract_text_from_message(data: dict) -> str:
    """Достать текст из messageData (текст, расширенный текст, иначе — подпись к медиа или метка)."""
    md = data.get("messageData") or {}
    # Обычное текстовое сообщение
    try:
        t = md.get("textMessageData", {}) or {}
        if isinstance(t.get("textMessage"), str):
            return t["textMessage"]
    except Exception:
        pass
    # Текст с ссылкой (extendedTextMessageData)
    try:
        e = md.get("extendedTextMessageData", {}) or {}
        if isinstance(e.get("text"), str):
            return e["text"]
    except Exception:
        pass
    # Медиа с подписью
    for key in ("imageMessageData", "videoMessageData", "documentMessageData", "audioMessageData"):
        block = md.get(key) or {}
        if isinstance(block.get("caption"), str):
            return block["caption"]
    # Реакция, стикер и т.д.
    if md.get("reactionMessageData"):
        return "[реакция]"
    if md.get("stickerMessageData"):
        return "[стикер]"
    return "[медиа]"

async def handle_webhook(request):
    # Сразу отвечаем 200, чтобы Green API не повторял запрос
    try:
        body = await request.read()
        print(f"--- WEBHOOK POST получен, размер тела: {len(body) if body else 0} ---")
        sys.stdout.flush()
        if not body:
            return web.Response(text="OK", status=200)
        try:
            data = body.decode("utf-8") if isinstance(body, bytes) else body
            if isinstance(data, str):
                data = json.loads(data)
        except Exception as e:
            print(f"--- WEBHOOK: не JSON, ошибка {e} ---")
            return web.Response(text="OK", status=200)
        print(f"--- ВХОДЯЩИЙ ЗАПРОС ИЗ WA: {data} ---")
        sys.stdout.flush()
        
        type_wh = (data.get("typeWebhook") or "").strip()
        if type_wh.lower() != "incomingmessagereceived":
            print(f"--- WEBHOOK: пропуск, typeWebhook={type_wh} ---")
            return web.Response(text="OK", status=200)
        
        if db is None or bot is None:
            print("--- WEBHOOK: пропуск, БД или бот не инициализированы ---")
            sys.stdout.flush()
            return web.Response(text="OK", status=200)
        
        await _process_incoming_wa_message(data)
    except Exception as e:
        print(f"--- Ошибка в вебхуке: {e} ---")
        traceback.print_exc()
    
    return web.Response(text="OK", status=200)

# --- ОТПРАВКА В WHATSAPP (GREEN API) ---
async def send_whatsapp_message(chat_id: str, text: str) -> bool:
    """Отправить текстовое сообщение в WhatsApp через Green API."""
    chat_id_full = f"{chat_id}@c.us" if "@" not in chat_id else chat_id
    url = f"{GREEN_URL}/waInstance{GREEN_ID}/sendMessage/{GREEN_TOKEN}"
    payload = {"chatId": chat_id_full, "message": text}
    try:
        async with ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                return resp.status == 200
    except Exception as e:
        print(f"Ошибка отправки в WA: {e}")
        return False

# --- ХЕНДЛЕРЫ БОТА ---
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if is_owner(msg.from_user.id):
        await msg.answer("Добро пожаловать! Выберите действие:", reply_markup=kb.main_owner_kb())
        return
    db.cur.execute("SELECT id, status FROM users WHERE id=?", (msg.from_user.id,))
    row = db.cur.fetchone()
    if row and row[1] == "active":
        await msg.answer("Вы в системе. Ожидайте лидов — они будут приходить с кнопками «Ответить» и «Завершить».")
        return
    if row and row[1] == "pending":
        await msg.answer("Ваша заявка на рассмотрении. Ожидайте решения владельца.")
        return
    await state.set_state(Form.waiting_for_fio)
    await msg.answer("Введите ваше ФИО для регистрации менеджером:")

@dp.message(Form.waiting_for_fio, F.text)
async def process_fio(msg: Message, state: FSMContext):
    fio = (msg.text or "").strip()
    if not fio:
        await msg.answer("Введите ФИО текстом.")
        return
    db.add_user(msg.from_user.id, fio, "manager")
    db.set_user_status(msg.from_user.id, "pending")
    await state.clear()
    await msg.answer("Заявка отправлена. Ожидайте одобрения владельца.")
    await bot.send_message(
        OWNER_ID,
        f"🆕 Заявка менеджера:\n👤 {fio}\n🆔 ID: {msg.from_user.id}",
        reply_markup=kb.accept_manager_kb(msg.from_user.id)
    )

# --- ВЛАДЕЛЕЦ: СТАТИСТИКА И АНАЛИТИКА ---
@dp.message(F.text == "📊 Статистика")
async def owner_stats(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    count, total_paid, total_debt = db.get_stats() or (0, 0, 0)
    total_paid = total_paid or 0
    total_debt = total_debt or 0
    await msg.answer(
        f"📊 Статистика (всего):\n"
        f"• Лидов: {count}\n"
        f"• Сумма оплат: {total_paid:.2f}\n"
        f"• Долг: {total_debt:.2f}"
    )

@dp.message(F.text == "📅 Аналитика")
async def owner_analytics(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return
    await state.set_state(Form.analytics_period)
    await msg.answer("Введите период в формате: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\nНапример: 01.02.2025 - 28.02.2025")

@dp.message(Form.analytics_period, F.text)
async def process_analytics_period(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return
    text = (msg.text or "").strip()
    try:
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError("Нужен формат: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ")
        d1 = datetime.strptime(parts[0].strip(), "%d.%m.%Y").strftime("%Y-%m-%d 00:00:00")
        d2 = datetime.strptime(parts[1].strip(), "%d.%m.%Y").strftime("%Y-%m-%d 23:59:59")
    except Exception:
        await msg.answer("Неверный формат дат. Пример: 01.02.2025 - 28.02.2025")
        return
    await state.clear()
    count, total_paid, total_debt = db.get_stats(d1, d2) or (0, 0, 0)
    total_paid = total_paid or 0
    total_debt = total_debt or 0
    await msg.answer(
        f"📅 Аналитика за период {parts[0].strip()} — {parts[1].strip()}:\n"
        f"• Лидов: {count}\n"
        f"• Оплачено: {total_paid:.2f}\n"
        f"• Долг: {total_debt:.2f}"
    )

# --- ВЛАДЕЛЕЦ: ПРИХОД ---
@dp.message(F.text == "💸 Приход")
async def owner_pay(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return
    await state.clear()
    leads = db.get_leads_in_progress()
    keyboard = kb.leads_for_pay_kb(leads)
    if not keyboard:
        await msg.answer("Нет лидов в работе.")
        return
    await msg.answer("Выберите лид для записи прихода:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("pay_lead_"))
async def pay_lead_select(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    if cb.data == "pay_cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return
    lead_id = int(cb.data.replace("pay_lead_", ""))
    await state.set_state(Form.pay_lead_amount)
    await state.update_data(lead_id=lead_id)
    await cb.message.edit_text(f"Введите сумму прихода по лиду #{lead_id}:")
    await cb.answer()

@dp.message(Form.pay_lead_amount, F.text)
async def pay_lead_amount(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return
    try:
        amount = float((msg.text or "").replace(",", ".").strip())
        if amount <= 0:
            raise ValueError("Сумма должна быть больше 0")
    except ValueError:
        await msg.answer("Введите число, например: 5000 или 5000.50")
        return
    data = await state.get_data()
    lead_id = data.get("lead_id")
    await state.clear()
    if db.add_payment_to_lead(lead_id, amount):
        await msg.answer(f"✅ Приход {amount:.2f} записан по лиду #{lead_id}.")
    else:
        await msg.answer("Ошибка: лид не найден.")

# --- ВЛАДЕЛЕЦ: УВОЛИТЬ ---
@dp.message(F.text == "🚫 Уволить")
async def owner_fire(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return
    await state.clear()
    managers = db.get_active_managers()
    keyboard = kb.managers_to_fire_kb(managers)
    if not keyboard:
        await msg.answer("Нет активных менеджеров.")
        return
    await msg.answer("Выберите менеджера для увольнения:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("fire_"))
async def fire_manager_cb(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    if cb.data == "fire_cancel":
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return
    if cb.data.startswith("fire_confirm_"):
        user_id = int(cb.data.replace("fire_confirm_", ""))
        db.set_user_status(user_id, "fired")
        await cb.message.edit_text("Менеджер уволен.")
        try:
            await bot.send_message(user_id, "Вам прекращён доступ в CRM.")
        except Exception:
            pass
        await cb.answer()
        return
    user_id = int(cb.data.replace("fire_", ""))
    await cb.message.edit_text("Подтвердите увольнение:", reply_markup=kb.confirm_fire_kb(user_id))
    await cb.answer()

# --- ВЛАДЕЛЕЦ: СКАЧАТЬ АРХИВ ---
@dp.message(F.text == "📁 Скачать Архив")
async def owner_archive(msg: Message):
    if not is_owner(msg.from_user.id):
        return
    rows = db.get_all_leads_for_export()
    if not rows:
        await msg.answer("Нет данных для выгрузки.")
        return
    df = pd.DataFrame(rows, columns=[
        "id", "client_phone", "manager_id", "status", "total_price", "paid_amount", "debt", "reject_reason", "created_at"
    ])
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    df.to_excel(path, index=False)
    await msg.answer_document(FSInputFile(path), caption="Архив лидов")
    try:
        os.remove(path)
    except Exception:
        pass

# --- CALLBACK: ОТВЕТИТЬ ЛИДУ (ОТПРАВКА В WA) ---
@dp.callback_query(F.data.startswith("reply_"))
async def reply_lead(cb: CallbackQuery, state: FSMContext):
    client_phone = cb.data.replace("reply_", "")
    await state.set_state(Form.writing_answer)
    await state.update_data(client_phone=client_phone)
    await cb.message.answer("Введите сообщение для клиента (оно будет отправлено в WhatsApp):")
    await cb.answer()

@dp.message(Form.writing_answer, F.text)
async def send_reply_to_wa(msg: Message, state: FSMContext):
    data = await state.get_data()
    client_phone = data.get("client_phone")
    await state.clear()
    if not client_phone:
        await msg.answer("Сессия сброшена. Нажмите «Ответить» у нужного лида снова.")
        return
    ok = await send_whatsapp_message(client_phone, msg.text)
    if ok:
        await msg.answer("✅ Сообщение отправлено в WhatsApp.")
    else:
        await msg.answer("❌ Не удалось отправить. Проверьте Green API.")

# --- CALLBACK: ЗАВЕРШИТЬ СДЕЛКУ ---
@dp.callback_query(F.data.startswith("finish_"))
async def finish_lead(cb: CallbackQuery, state: FSMContext):
    if cb.data == "finish_cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return
    if cb.data.startswith("finish_ok_"):
        client_phone = cb.data.replace("finish_ok_", "")
        row = db.get_lead_by_phone_manager(client_phone, cb.from_user.id)
        if not row:
            await cb.answer("Лид не найден или уже закрыт.", show_alert=True)
            return
        lead_id = row[0]
        await state.set_state(Form.finish_paid_total)
        await state.update_data(lead_id=lead_id, client_phone=client_phone)
        await cb.message.answer("Введите сумму сделки (итого):")
        await cb.answer()
        return
    if cb.data.startswith("finish_no_"):
        client_phone = cb.data.replace("finish_no_", "")
        row = db.get_lead_by_phone_manager(client_phone, cb.from_user.id)
        if not row:
            await cb.answer("Лид не найден или уже закрыт.", show_alert=True)
            return
        lead_id = row[0]
        await state.set_state(Form.finish_reject)
        await state.update_data(lead_id=lead_id)
        await cb.message.answer("Введите причину отказа:")
        await cb.answer()
        return
    # finish_{phone} — показать выбор Успешно/Отказ
    client_phone = cb.data.replace("finish_", "")
    await cb.message.edit_reply_markup(reply_markup=kb.finish_choice_kb(client_phone))
    await cb.answer()

@dp.message(Form.finish_paid_total, F.text)
async def finish_total_entered(msg: Message, state: FSMContext):
    try:
        total = float((msg.text or "").replace(",", ".").strip())
        if total < 0:
            raise ValueError("Сумма не может быть отрицательной")
    except ValueError:
        await msg.answer("Введите число, например: 10000")
        return
    await state.update_data(total=total)
    await state.set_state(Form.finish_paid_amount)
    await msg.answer("Введите оплаченную сумму:")

@dp.message(Form.finish_paid_amount, F.text)
async def finish_paid_entered(msg: Message, state: FSMContext):
    try:
        paid = float((msg.text or "").replace(",", ".").strip())
        if paid < 0:
            raise ValueError("Сумма не может быть отрицательной")
    except ValueError:
        await msg.answer("Введите число, например: 5000")
        return
    data = await state.get_data()
    lead_id = data.get("lead_id")
    total = data.get("total", 0)
    await state.clear()
    if lead_id:
        db.close_lead(lead_id, "done", total=total, paid=paid)
        await msg.answer(f"✅ Сделка закрыта. Итого: {total}, Оплачено: {paid}, Долг: {total - paid:.2f}")
    else:
        await msg.answer("Ошибка: данные сессии потеряны.")

@dp.message(Form.finish_reject, F.text)
async def finish_reject_entered(msg: Message, state: FSMContext):
    reason = (msg.text or "").strip() or "Не указана"
    data = await state.get_data()
    lead_id = data.get("lead_id")
    await state.clear()
    if lead_id:
        db.close_lead(lead_id, "rejected", reason=reason)
        await msg.answer("✅ Лид закрыт как отказ.")
    else:
        await msg.answer("Ошибка: данные сессии потеряны.")

# --- CALLBACK: ПРИНЯТЬ / ОТКЛОНИТЬ МЕНЕДЖЕРА ---
@dp.callback_query(F.data.startswith("accept_"))
async def accept_manager(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    user_id = int(cb.data.replace("accept_", ""))
    db.set_user_status(user_id, "active")
    await cb.message.edit_text(f"✅ Менеджер {user_id} принят.")
    try:
        await bot.send_message(user_id, "Вас приняли в CRM. Ожидайте лидов.")
    except Exception:
        pass
    await cb.answer()

@dp.callback_query(F.data.startswith("decline_"))
async def decline_manager(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    user_id = int(cb.data.replace("decline_", ""))
    db.set_user_status(user_id, "declined")
    await cb.message.edit_text(f"❌ Заявка менеджера {user_id} отклонена.")
    try:
        await bot.send_message(user_id, "К сожалению, ваша заявка отклонена.")
    except Exception:
        pass
    await cb.answer()

# --- ОТМЕНА ЛЮБОГО FSM ---
@dp.callback_query(F.data == "cancel_state")
async def cancel_state(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("Действие отменено.")
    except Exception:
        await cb.message.answer("Действие отменено.")

# --- ИСПРАВЛЕННЫЙ БЛОК ЗАПУСКА ---
async def start_bot():
    await dp.start_polling(bot)

async def main():
    global bot, db, _green_session
    print("CRM ИНОЯТА: запуск приложения...")
    sys.stdout.flush()
    
    try:
        db = Database("/data/marketing_crm.db")
        print("БД инициализирована: /data/marketing_crm.db")
    except Exception as e:
        print(f"Ошибка БД: {e}")
        traceback.print_exc()
        db = None
    sys.stdout.flush()
    
    try:
        bot = Bot(token=TOKEN)
        print("Бот инициализирован")
    except Exception as e:
        print(f"Ошибка бота: {e}")
        traceback.print_exc()
        bot = None
    sys.stdout.flush()
    
    _green_session = ClientSession()
    asyncio.create_task(check_wa_polling())
    print("Опрос Green API (receiveNotification) запущен — сообщения из WA будут приходить без вебхука.")
    sys.stdout.flush()
    
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="CRM ИНОЯТА OK"))
    app.router.add_get("/ping", lambda r: web.Response(text="pong"))
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/webhook", lambda r: web.Response(text="Webhook is working! Send POST request."))
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 80))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Веб-сервер запущен на порту {port}. Путь: /webhook")
    sys.stdout.flush()
    
    if bot:
        await start_bot()
    else:
        print("Бот не запущен — работают только вебхуки. Ожидание...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass