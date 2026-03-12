# -*- coding: utf-8 -*-
# Конфигурация бота: обучающая платформа + CRM для админа

import os

# ID администратора (владелец). На Amvera можно задать переменную ADMIN_ID=6428583782
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6428583782"))

# ID группы-архива, откуда бот берёт видео и вопросы (из ссылки web.telegram.org)
ARCHIVE_GROUP_ID = -516022514

# Токен бота (из действующего CRM ИНОЯТА)
BOT_TOKEN = "8634696282:AAEBajKKapvJpsLZx649GyUX9kCh5jThWHM"

# Путь к SQLite БД
DB_PATH = "bot_platform.db"

# Через сколько минут после видео показывать опрос (для теста поставь 1, для прода — 1440 = 24ч)
QUIZ_AFTER_MINUTES = 1
