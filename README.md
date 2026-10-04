# Telegram Moderator Bot

Telegram-модератор на Python 3.11 + python-telegram-bot + SQLite.

## Возможности

- временный мут и автоматический размут;
- временный бан и автоматический разбан;
- предупреждения с автоматическим снятием;
- автоматический мут после заданного количества предупреждений;
- кик;
- админ-панель;
- список активных наказаний с кнопкой снятия;
- список пользователей;
- журнал действий;
- настройки каждого чата;
- лог-чат;
- whitelist администраторов через `ADMIN_IDS`;
- восстановление таймеров после перезапуска.

## Команды

Все модерационные команды работают как reply на сообщение пользователя.

- `/panel` — админ-панель
- `/mute 10m причина`
- `/unmute`
- `/warn 7d причина`
- `/unwarn`
- `/warnings`
- `/ban 7d причина` или `/ban причина` для бессрочного
- `/unban`
- `/kick причина`
- `/id`

Форматы времени: `30s`, `10m`, `2h`, `3d`, `1w`.

## Настройка

Создайте `.env`:

```env
BOT_TOKEN=НОВЫЙ_ТОКЕН
ADMIN_IDS=123456789
DATABASE_PATH=/app/data/bot.db
LOG_LEVEL=INFO
```

## Bothost

Рекомендуется хранить SQLite в `/app/data/bot.db`: Bothost сохраняет эту папку между перезапусками и обновлениями.

Подключите GitHub-репозиторий, ветку `main`, укажите `main.py` как точку входа, если Bothost не определит её автоматически.

Добавьте переменную `BOT_TOKEN` и `ADMIN_IDS` в настройках переменных окружения Bothost.

Бот должен быть администратором Telegram-чата с правами ограничения участников и блокировки участников.

## Локальный запуск

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Windows:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

## GitHub

```bash
git init -b main
git add .
git commit -m "Initial moderator bot"
git remote add origin https://github.com/USERNAME/REPOSITORY.git
git push -u origin main
```

Никогда не загружайте `.env` и токен Telegram в Git.

## Логи

В админ-панели можно задать `log_chat_id`. Бот будет отправлять туда действия модераторов.
