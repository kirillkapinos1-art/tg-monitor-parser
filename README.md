# Telegram Lead Monitor & AI Parser

Парсер и мониторинг-радар заявок/заказов из Telegram-каналов и чатов на базе **Telethon (MTProto)**, детерминированного фильтра ключевых слов, **Google Gemini AI** (структурированная оценка релевантности 1–10) и **SQLite-дедупликации**.

## Возможности

- **Двухуровневая фильтрация:**
  1. Быстрый pre-filter по ключевым словам (`KEYWORDS`) и стоп-словам (`STOP_WORDS`).
  2. Опциональный **AI-скоринг (Gemini JSON schema):** отсекает рекламу курсов, спам и нерелевантные посты, выставляет оценку `1..10`, категорию и краткое резюме заказа.
- **Надёжная дедупликация (SQLite WAL):** запоминает и пары `(chat_id, message_id)`, и SHA-256 хеш нормализованного текста — кросспосты одного и того же объявления в разных каналах не дублируются.
- **Два режима работы:**
  - `--backfill N` — просканировать последние `N` постов при старте (можно с `--once` для разового запуска по расписанию).
  - Live-режим (`events.NewMessage`) — мгновенная пересылка подходящих лидов в `TARGET_CHAT` (`me` или отдельный канал/бот-чат) со ссылкой на оригинал.
- **Безопасный `--dry-run`:** вывод совпадений в консоль без отправки сообщений.

## Структура проекта

```text
tg-monitor-parser/
├── monitor.py       # CLI и основной цикл Telethon + backfill/live listener
├── config.py        # Загрузка конфигурации из .env и CLI-флагов
├── filters.py       # Ключевые слова, стоп-слова, SHA-256 хеширование и t.me ссылки
├── scorer.py        # GeminiLeadScorer (structured JSON + fallback моделей)
├── dedup.py         # SeenPostsStore на SQLite (WAL)
├── requirements.txt
├── .env.example
└── tests/
    └── test_monitor_parser.py
```

## Быстрый старт

1. Установите зависимости:
   ```bash
   python -m venv .venv
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Получите `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` на [my.telegram.org/apps](https://my.telegram.org/apps), скопируйте `.env.example` в `.env` и укажите `SOURCE_CHATS`, `KEYWORDS` и (опционально) `GEMINI_API_KEY`.

3. Запустите разовый тест на последних 30 сообщениях без отправки:
   ```bash
   python monitor.py --backfill 30 --once --dry-run
   ```

4. Запустите живой мониторинг:
   ```bash
   python monitor.py --backfill 20
   ```

## Тесты

```bash
pytest
```
