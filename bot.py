import os
import asyncio
from typing import Optional, Dict
from psycopg_pool import AsyncConnectionPool
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ========= НАСТРОЙКИ =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MOD_GROUP_ID = int(os.getenv("MOD_GROUP_ID", "-1003173446264"))

# ========= ПОДКЛЮЧЕНИЕ К БД =========
POOL: Optional[AsyncConnectionPool] = None

INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'ru'
);

CREATE TABLE IF NOT EXISTS tickets (
    id BIGSERIAL PRIMARY KEY,
    ticket_id TEXT UNIQUE,
    user_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

async def init_db():
    global POOL
    POOL = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=5,
    )
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(INIT_SQL)
            await conn.commit()
    print("✅ База данных инициализирована")


# ========= ЛОГИКА БОТА =========

active_reply: Dict[int, str] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING;",
                (user_id,)
            )
            await conn.commit()
    await update.message.reply_text("Привет! Бот успешно подключён к базе данных 🧠")

async def create_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reason = " ".join(context.args) if context.args else "Без причины"

    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO tickets (ticket_id, user_id, category, reason) VALUES (%s, %s, %s, %s) RETURNING ticket_id;",
                (f"T-{user_id}-{int(asyncio.get_event_loop().time())}", user_id, "Общее", reason)
            )
            await conn.commit()

    await update.message.reply_text("✅ Тикет создан!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Доступные команды:\n"
        "/start — запуск\n"
        "/ticket <причина> — создать тикет\n"
        "/help — помощь"
    )

# ========= ГЛАВНЫЙ ЗАПУСК =========

async def main():
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ticket", create_ticket))
    app.add_handler(CommandHandler("help", help_command))

    print("🚀 Бот запущен")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
