import asyncio
import asyncpg
from datetime import datetime
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)

DB_URL = "postgresql://postgres:ZxdYARFdKOaFlLnOGWZISjmATBCTGGrW@ballast.proxy.rlwy.net:25766/railway"
BOT_TOKEN = "8351785031:AAEa4AgLciZGVO0cHm_Aa4SLqBINzbDDjao"

db = None


# === БАЗА ДАННЫХ ===
async def init_db():
    global db
    db = await asyncpg.create_pool(DB_URL)
    print("✅ База данных инициализирована")
    async with db.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            lang TEXT DEFAULT 'ru'
        );
        CREATE TABLE IF NOT EXISTS tickets(
            ticket_id SERIAL PRIMARY KEY,
            user_id BIGINT,
            category TEXT,
            reason TEXT,
            description TEXT,
            status TEXT DEFAULT 'open',
            moderator TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            closed_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages(
            id SERIAL PRIMARY KEY,
            ticket_id INT,
            sender TEXT,
            content TEXT,
            sent_at TIMESTAMP DEFAULT NOW()
        );
        """)
    print("📦 Таблицы проверены / созданы")


async def db_exec(sql, *args):
    async with db.acquire() as con:
        await con.execute(sql, *args)


async def db_one(sql, *args):
    async with db.acquire() as con:
        return await con.fetchrow(sql, *args)


async def db_all(sql, *args):
    async with db.acquire() as con:
        return await con.fetch(sql, *args)


# === ЯЗЫК ===
async def get_user_lang(uid):
    r = await db_one("SELECT lang FROM users WHERE user_id=$1", uid)
    return r["lang"] if r else "ru"


async def set_user_lang(uid, username, lang):
    await db_exec("""
        INSERT INTO users(user_id, username, lang)
        VALUES($1,$2,$3)
        ON CONFLICT(user_id)
        DO UPDATE SET username=EXCLUDED.username, lang=EXCLUDED.lang
    """, uid, username, lang)


# === СТАРТ ===
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or "user"
    await set_user_lang(uid, uname, "ru")

    kb = [[
        KeyboardButton("🛠 Техподдержка"),
        KeyboardButton("💳 Платежи")
    ], [
        KeyboardButton("🧩 HWID"),
        KeyboardButton("🤝 Сотрудничество")
    ], [
        KeyboardButton("❓ FAQ")
    ]]
    await update.message.reply_text(
        "Выберите категорию обращения:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )


# === СОЗДАНИЕ ТИКЕТА ===
async def user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    uname = update.effective_user.username or "user"

    await db_exec("""
        INSERT INTO tickets(user_id, category, reason, description, status)
        VALUES($1,$2,$3,$4,'open')
    """, uid, "Support", "User message", text)

    await update.message.reply_text(
        "✅ Тикет создан! Ожидайте ответа модератора."
    )


# === МОДЕРАТОРСКИЕ ФУНКЦИИ ===
async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🕓 История", callback_data="history")],
        [InlineKeyboardButton("🤖 Автоответы", callback_data="auto")],
        [InlineKeyboardButton("📈 Проверить статус", callback_data="status")]
    ]
    await update.message.reply_text("Панель модерации:", reply_markup=InlineKeyboardMarkup(kb))


async def panel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "stats":
        rows = await db_all("""
            SELECT moderator, COUNT(*) AS total FROM tickets
            WHERE status='closed'
            GROUP BY moderator
        """)
        text = "📊 Статистика закрытых тикетов:\n\n"
        for r in rows:
            text += f"@{r['moderator']} — {r['total']} тикетов\n"
        await q.edit_message_text(text or "Нет данных.")
    elif q.data == "history":
        rows = await db_all("""
            SELECT ticket_id, category, status, created_at FROM tickets
            ORDER BY created_at DESC LIMIT 5
        """)
        txt = "🕓 Последние тикеты:\n\n"
        for r in rows:
            txt += f"#{r['ticket_id']} — {r['category']} ({r['status']})\n"
        await q.edit_message_text(txt or "Нет тикетов.")
    elif q.data == "status":
        await q.edit_message_text("📈 Все тикеты в работе или открыты.")


# === ЗАПУСК ===
async def main():
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, user_message))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CallbackQueryHandler(panel_buttons))

    print("🚀 Бот запущен")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
