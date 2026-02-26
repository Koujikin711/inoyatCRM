from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def main_owner_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📅 Аналитика")],
        [KeyboardButton(text="💸 Приход"), KeyboardButton(text="🚫 Уволить")],
        [KeyboardButton(text="📁 Скачать Архив")]
    ], resize_keyboard=True)

def lead_card_kb(client_phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Ответить", callback_data=f"reply_{client_phone}")],
        [InlineKeyboardButton(text="🏁 Завершить", callback_data=f"finish_{client_phone}")]
    ])

def accept_manager_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_{user_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{user_id}")]
    ])

def finish_choice_kb(client_phone):
    """Успешно закрыть сделку или отказ."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Успешно", callback_data=f"finish_ok_{client_phone}")],
        [InlineKeyboardButton(text="❌ Отказ", callback_data=f"finish_no_{client_phone}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="finish_cancel")]
    ])

def leads_for_pay_kb(leads):
    """Список лидов для записи прихода (id, client_phone)."""
    rows = []
    for lead in leads:
        lid, phone = lead[0], lead[1]
        rows.append([InlineKeyboardButton(text=f"📱 {phone}", callback_data=f"pay_lead_{lid}")])
    if not rows:
        return None
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="pay_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def managers_to_fire_kb(managers):
    """Список менеджеров для увольнения."""
    rows = []
    for uid, fio in managers:
        rows.append([InlineKeyboardButton(text=f"{fio or uid}", callback_data=f"fire_{uid}")])
    if not rows:
        return None
    rows.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="fire_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def confirm_fire_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, уволить", callback_data=f"fire_confirm_{user_id}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="fire_cancel")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_state")]
    ])