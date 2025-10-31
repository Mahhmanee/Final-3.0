import os
import asyncio
import datetime as dt
from typing import Optional, Dict, List

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, User as TgUser
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MOD_GROUP_ID = int(os.getenv("MOD_GROUP_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not MOD_GROUP_ID:
    raise RuntimeError("MOD_GROUP_ID is not set (e.g. -1001234567890)")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set (postgresql://...)")

LANGS = {"ru": "Русский", "en": "English"}
CATS = {
    "ru": [
        ("🔧 Техническая помощь", "tech"),
        ("💳 Помощь с платежами", "pay"),
        ("🔄 Сброс HWID", "hwid"),
        ("🤝 Сотрудничество", "coop"),
        ("❓ FAQ / Цены / Товары", "faq")
    ],
    "en": [
        ("🔧 Technical Support", "tech"),
        ("💳 Payment Help", "pay"),
        ("🔄 HWID Reset", "hwid"),
        ("🤝 Cooperation", "coop"),
        ("❓ FAQ / Prices / Products", "faq")
    ]
}
CAT_TITLES_RU = {
    "tech": "🔧 Техническая помощь",
    "pay":  "💳 Помощь с платежами",
    "hwid": "🔄 Сброс HWID",
    "coop": "🤝 Сотрудничество",
    "faq":  "❓ FAQ / Цены / Товары"
}

# ================== БАЗА ДАННЫХ ==================
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

POOL: Optional[AsyncConnectionPool] = None

INIT_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'ru',
    username TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
  id BIGSERIAL PRIMARY KEY,
  ticket_id TEXT UNIQUE,
  user_id BIGINT NOT NULL,
  category TEXT NOT NULL,
  reason TEXT,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open',         -- open/closed
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  assigned_to BIGINT,                           -- id модератора, взявшего тикет
  closed_by BIGINT,
  closed_by_name TEXT,
  group_header_msg_id BIGINT
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  from_role TEXT NOT NULL,                      -- 'user' | 'mod' | 'system'
  text TEXT,
  user_msg_id BIGINT,
  group_msg_id BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS autoresponders (
  category TEXT PRIMARY KEY,                    -- tech|pay|hwid|coop|faq
  text TEXT
);
"""

async def db_exec(sql: str, *params) -> None:
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
        await conn.commit()

async def db_one(sql: str, *params) -> Optional[dict]:
    async with POOL.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

async def db_all(sql: str, *params) -> List[dict]:
    async with POOL.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return rows or []

async def init_pool_and_db(_: Application) -> None:
    """Запускается через Application.post_init — безопасно для Railway loop."""
    global POOL
    if POOL is None:
        POOL = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
        await POOL.open()
        async with POOL.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(INIT_SQL)
            await conn.commit()
        # включим автоответчики по умолчанию
        await db_exec(
            "INSERT INTO settings(key,value) VALUES('autoresponders_enabled','1') "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
        )
        print("✅ База данных инициализирована")

def gen_ticket_id(seq: int) -> str:
    today = dt.datetime.now().strftime("%Y%m%d")
    return f"T-{today}-{seq:04d}"

async def set_user_lang(uid: int, lang: str):
    await db_exec(
        "INSERT INTO users(user_id,lang) VALUES(%s,%s) "
        "ON CONFLICT(user_id) DO UPDATE SET lang=EXCLUDED.lang",
        uid, lang
    )

async def get_user_lang(uid: int) -> str:
    r = await db_one("SELECT lang FROM users WHERE user_id=%s", uid)
    return (r and r["lang"]) or "ru"

async def autores_enabled() -> bool:
    r = await db_one("SELECT value FROM settings WHERE key='autoresponders_enabled'")
    return bool(r and r["value"] == "1")

async def set_autores_enabled(enabled: bool):
    await db_exec(
        "INSERT INTO settings(key,value) VALUES('autoresponders_enabled',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        "1" if enabled else "0"
    )

async def get_autoresponder_text(category: str) -> Optional[str]:
    r = await db_one("SELECT text FROM autoresponders WHERE category=%s", category)
    return r["text"] if r else None

async def set_autoresponder_text(category: str, text: str):
    await db_exec(
        "INSERT INTO autoresponders(category,text) VALUES(%s,%s) "
        "ON CONFLICT(category) DO UPDATE SET text=EXCLUDED.text",
        category, text
    )

async def create_ticket(user_id: int, category: str, reason: str, description: str) -> str:
    await db_exec(
        "INSERT INTO tickets(user_id,category,reason,description,status) VALUES(%s,%s,%s,%s,'open')",
        user_id, category, reason, description
    )
    r = await db_one("SELECT id FROM tickets WHERE user_id=%s ORDER BY id DESC LIMIT 1", user_id)
    seq = r["id"]
    t_id = gen_ticket_id(seq)
    await db_exec("UPDATE tickets SET ticket_id=%s WHERE id=%s", t_id, seq)
    return t_id

async def store_group_header(ticket_id: str, msg_id: int):
    await db_exec("UPDATE tickets SET group_header_msg_id=%s WHERE ticket_id=%s", msg_id, ticket_id)

async def mark_assigned(ticket_id: str, mod_id: int):
    await db_exec("UPDATE tickets SET assigned_to=%s WHERE ticket_id=%s", mod_id, ticket_id)

async def get_ticket_user(ticket_id: str) -> Optional[int]:
    r = await db_one("SELECT user_id FROM tickets WHERE ticket_id=%s", ticket_id)
    return r["user_id"] if r else None

async def get_ticket_header(ticket_id: str) -> Optional[int]:
    r = await db_one("SELECT group_header_msg_id FROM tickets WHERE ticket_id=%s", ticket_id)
    return r["group_header_msg_id"] if r else None

async def record_msg(ticket_id: str, role: str, text: str, user_msg_id: int | None, group_msg_id: int | None):
    await db_exec(
        "INSERT INTO messages(ticket_id,from_role,text,user_msg_id,group_msg_id) VALUES(%s,%s,%s,%s,%s)",
        ticket_id, role, text or "", user_msg_id, group_msg_id
    )

async def get_ticket_group_msg_ids(ticket_id: str) -> List[int]:
    rows = await db_all(
        "SELECT group_msg_id FROM messages WHERE ticket_id=%s AND group_msg_id IS NOT NULL",
        ticket_id
    )
    return [r["group_msg_id"] for r in rows if r["group_msg_id"]]

async def ticket_exists(ticket_id: str) -> bool:
    r = await db_one("SELECT 1 AS ok FROM tickets WHERE ticket_id=%s", ticket_id)
    return bool(r)

async def ticket_status(ticket_id: str) -> Optional[str]:
    r = await db_one("SELECT status FROM tickets WHERE ticket_id=%s", ticket_id)
    return r["status"] if r else None

async def close_ticket(ticket_id: str, closed_by: Optional[int], closed_by_name: Optional[str]):
    await db_exec(
        "UPDATE tickets SET status='closed', closed_by=%s, closed_by_name=%s WHERE ticket_id=%s",
        closed_by, closed_by_name, ticket_id
    )

async def ticket_history_text(ticket_id: str, limit: int = 30) -> str:
    rows = await db_all(
        "SELECT from_role, text, created_at FROM messages WHERE ticket_id=%s ORDER BY id ASC",
        ticket_id
    )
    if not rows:
        return f"📜 История по {ticket_id}: сообщений нет."
    rows = rows[-limit:]
    parts = [f"📜 История по {ticket_id} (последние {len(rows)}):", ""]
    for r in rows:
        role = {"user": "👤 Пользователь", "mod": "🛠 Модератор", "system": "📎 Система"}.get(r["from_role"], r["from_role"])
        created = r["created_at"]
        if isinstance(created, dt.datetime):
            created_str = created.strftime("%Y-%m-%d %H:%M:%S")
        else:
            created_str = str(created)
        txt = (r["text"] or "").strip()
        if len(txt) > 600:
            txt = txt[:600] + "…"
        parts.append(f"{role} ({created_str}):\n{txt}\n")
    return "\n".join(parts)

async def stats_text() -> str:
    rows = await db_all("""
        SELECT COALESCE(closed_by_name, closed_by::text) AS who, COUNT(*) AS c
        FROM tickets
        WHERE status='closed' AND closed_by IS NOT NULL
        GROUP BY who
        ORDER BY c DESC
    """)
    if not rows:
        return "📊 Пока никто не закрыл ни одного тикета."
    total = sum(r["c"] for r in rows)
    out = ["📊 Статистика закрытий (всё время):"]
    for r in rows:
        out.append(f"- {r['who']}: {r['c']}")
    out.append(f"Всего: {total}")
    return "\n".join(out)

async def last_tickets(limit: int = 10) -> List[str]:
    rows = await db_all("SELECT ticket_id FROM tickets ORDER BY id DESC LIMIT %s", limit)
    return [r["ticket_id"] for r in rows]

async def open_tickets_count() -> int:
    r = await db_one("SELECT COUNT(*) AS c FROM tickets WHERE status='open'")
    return int(r["c"]) if r else 0

# ================== КНОПКИ ==================
def ticket_keyboard(ticket_id: str, assigned_to: Optional[int]=None) -> InlineKeyboardMarkup:
    assigned_str = f"👨‍💻 В работе у {assigned_to}" if assigned_to else "🤷‍♂️ Свободен"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 История", callback_data=f"t:{ticket_id}:hist"),
         InlineKeyboardButton("✋ Взять тикет", callback_data=f"t:{ticket_id}:take")],
        [InlineKeyboardButton("✉️ Ответить", callback_data=f"t:{ticket_id}:reply"),
         InlineKeyboardButton("✅ Закрыть", callback_data=f"t:{ticket_id}:close")],
        [InlineKeyboardButton(f"{assigned_str}", callback_data=f"t:{ticket_id}:noop")]
    ])

def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="p:stats"),
         InlineKeyboardButton("📜 История", callback_data="p:history")],
        [InlineKeyboardButton("🤖 Автоответчики", callback_data="p:autores"),
         InlineKeyboardButton("📊 Проверить статус", callback_data="p:status")]
    ])

def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Обновить", callback_data="p:stats:refresh")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="p:back")]
    ])

def history_menu_keyboard(ids: List[str]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, tid in enumerate(ids, 1):
        row.append(InlineKeyboardButton(tid, callback_data=f"p:history:show:{tid}"))
        if i % 2 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def autores_menu_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle = "🔘 Автоответчики [ON]" if enabled else "⚪️ Автоответчики [OFF]"
    rows = [[InlineKeyboardButton(CAT_TITLES_RU[c], callback_data=f"ar:cat:{c}")]
            for c in ["tech","pay","hwid","coop","faq"]]
    rows.append([InlineKeyboardButton(toggle, callback_data="ar:toggle")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def autores_cat_keyboard(cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить автоответ", callback_data=f"ar:edit:{cat}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="p:autores")]
    ])

# ================== РЕЖИМ ОТВЕТА МОДЕРАТОРА ==================
active_reply: Dict[int, str] = {}  # mod_id -> ticket_id

# ================== ПОЛЬЗОВАТЕЛЬ ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await db_exec(
        "INSERT INTO users(user_id,username) VALUES(%s,%s) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username",
        uid, update.effective_user.username
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")]
    ])
    await update.effective_message.reply_text("Выберите язык / Choose your language:", reply_markup=kb)

async def cb_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.split(":")[1]
    await set_user_lang(q.from_user.id, lang)
    cats = CATS[lang]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(title, callback_data=f"cat:{code}")]
                               for title, code in cats])
    text = "Выберите нужную услугу:" if lang == "ru" else "Choose the service you need:"
    await q.message.reply_text(text, reply_markup=kb)

async def cb_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = await get_user_lang(uid)
    cat = q.data.split(":")[1]
    context.user_data["new_ticket_cat"] = cat
    context.user_data["stage"] = "reason"
    text = "Пожалуйста, коротко укажите причину обращения:" if lang == "ru" else "Please briefly describe your reason:"
    await q.message.reply_text(text)

async def pm_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    lang = await get_user_lang(uid)
    text = update.effective_message.text or update.effective_message.caption or ""
    stage = context.user_data.get("stage")

    # 1) причина
    if stage == "reason":
        context.user_data["reason"] = text
        context.user_data["stage"] = "description"
        t = "Опишите подробнее вашу проблему:" if lang == "ru" else "Please describe your problem in detail:"
        await update.effective_message.reply_text(t)
        return

    # 2) описание + создание тикета
    if stage == "description":
        cat = context.user_data.get("new_ticket_cat")
        reason = context.user_data.get("reason", "")
        description = text
        t_id = await create_ticket(uid, cat, reason, description)

        confirm = (f"✅ Тикет {t_id} создан.\n"
                   f"Модераторы скоро ответят здесь.\nЧтобы закрыть тикет, используйте /close") if lang == "ru" else \
                  (f"✅ Ticket {t_id} created.\nModerators will reply here soon.\nUse /close to close the ticket.")
        await update.effective_message.reply_text(confirm)

        header = (f"🆕 Новый тикет {t_id}\n"
                  f"Категория: {CAT_TITLES_RU.get(cat, cat)}\n"
                  f"Причина: {reason or '—'}\n"
                  f"Описание: {description or '—'}\n"
                  f"От: @{update.effective_user.username or update.effective_user.full_name} (ID: {uid})")
        hmsg = await context.bot.send_message(MOD_GROUP_ID, header, reply_markup=ticket_keyboard(t_id))
        await store_group_header(t_id, hmsg.message_id)
        await record_msg(t_id, "system", header, None, hmsg.message_id)

        # Автоответчик по категории
        if await autores_enabled():
            atext = await get_autoresponder_text(cat)
            if atext:
                await context.bot.send_message(chat_id=uid, text=atext)

        # В историю
        await record_msg(t_id, "user", f"[Причина] {reason}\n[Описание] {description}",
                         update.effective_message.message_id, None)
        context.user_data.clear()
        return

    # 3) Доп. сообщения пользователя — пересылка в группу в открытый тикет
    r = await db_one(
        "SELECT ticket_id FROM tickets WHERE user_id=%s AND status='open' ORDER BY id DESC LIMIT 1",
        uid
    )
    if not r:
        t = "Чтобы создать тикет, нажмите /start и выберите раздел." if lang == "ru" else \
            "To create a ticket, press /start and choose a section."
        await update.effective_message.reply_text(t)
        return
    t_id = r["ticket_id"]

    head = f"[{t_id}] Сообщение от пользователя @{update.effective_user.username or update.effective_user.full_name} (ID: {uid}):"
    h = await context.bot.send_message(MOD_GROUP_ID, head)
    await record_msg(t_id, "system", head, None, h.message_id)

    copied = await context.bot.copy_message(
        chat_id=MOD_GROUP_ID,
        from_chat_id=uid,
        message_id=update.effective_message.message_id
    )
    await record_msg(t_id, "user", text or "[media]",
                     update.effective_message.message_id, copied.message_id)

# ================== КНОПКИ ТИКЕТА В ГРУППЕ ==================
async def cb_ticket_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data  # t:<ticket_id>:action
    try:
        _, ticket_id, action = data.split(":")
    except ValueError:
        return
    if q.message.chat.id != MOD_GROUP_ID:
        return

    mod: TgUser = q.from_user

    if action == "hist":
        txt = await ticket_history_text(ticket_id, limit=30)
        await q.message.reply_text(txt, reply_to_message_id=q.message.message_id)
        return

    if action == "take":
        await mark_assigned(ticket_id, mod.id)
        active_reply.pop(mod.id, None)
        kb = ticket_keyboard(ticket_id, assigned_to=mod.id)
        try:
            await q.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        await q.message.reply_text(f"Тикет {ticket_id} взят в работу @{mod.username or mod.full_name}")
        return

    if action == "reply":
        active_reply[mod.id] = ticket_id
        await q.message.reply_text(
            f"✍️ Режим ответа включён для {ticket_id}. "
            f"Все ваши сообщения в этой группе будут пересылаться пользователю, пока не введёте /end."
        )
        return

    if action == "close":
        if not await ticket_exists(ticket_id):
            await q.message.reply_text("Тикет не найден.")
            return
        if await ticket_status(ticket_id) == "closed":
            await q.message.reply_text("Тикет уже закрыт.")
            return

        # удалить все групповые сообщения тикета
        gids = await get_ticket_group_msg_ids(ticket_id)
        for mid in gids:
            try:
                await context.bot.delete_message(MOD_GROUP_ID, mid)
                await asyncio.sleep(0.03)
            except Exception:
                pass

        who_name = f"@{mod.username}" if mod.username else mod.full_name
        await close_ticket(ticket_id, mod.id, who_name)
        uid = await get_ticket_user(ticket_id)
        if uid:
            try:
                await context.bot.send_message(uid, f"Тикет {ticket_id} закрыт модератором.")
            except Exception:
                pass
        await q.message.reply_text(f"✅ Тикет {ticket_id} закрыт и сообщения удалены.")
        if active_reply.get(mod.id) == ticket_id:
            active_reply.pop(mod.id, None)
        return

    if action == "noop":
        return

# ================== СООБЩЕНИЯ ОТ МОДЕРОВ ПОЛЬЗОВАТЕЛЮ ==================
async def mod_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    mod_id = update.effective_user.id
    ticket_id = active_reply.get(mod_id)
    if not ticket_id:
        return
    if update.effective_message.text and update.effective_message.text.startswith(("/", ".")):
        return
    uid = await get_ticket_user(ticket_id)
    if not uid:
        return

    await context.bot.copy_message(
        chat_id=uid,
        from_chat_id=MOD_GROUP_ID,
        message_id=update.effective_message.message_id
    )
    text = update.effective_message.text or update.effective_message.caption or "[media]"
    await record_msg(ticket_id, "mod", text, None, update.effective_message.message_id)

async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    mod_id = update.effective_user.id
    if mod_id in active_reply:
        ticket_id = active_reply.pop(mod_id)
        await update.effective_message.reply_text(f"🛑 Режим ответа для {ticket_id} завершён.")
    else:
        await update.effective_message.reply_text("У вас нет активного режима ответа.")

# ================== ЗАКРЫТИЕ СО СТОРОНЫ ПОЛЬЗОВАТЕЛЯ ==================
async def cmd_close_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    r = await db_one(
        "SELECT ticket_id FROM tickets WHERE user_id=%s AND status='open' ORDER BY id DESC LIMIT 1", uid
    )
    if not r:
        await update.effective_message.reply_text("У вас нет открытых тикетов.")
        return
    ticket_id = r["ticket_id"]

    gids = await get_ticket_group_msg_ids(ticket_id)
    for mid in gids:
        try:
            await context.bot.delete_message(MOD_GROUP_ID, mid)
            await asyncio.sleep(0.03)
        except Exception:
            pass

    await close_ticket(ticket_id, None, None)
    await update.effective_message.reply_text(f"✅ Тикет {ticket_id} закрыт.")
    await context.bot.send_message(MOD_GROUP_ID, f"❌ Тикет {ticket_id} закрыт пользователем.")

# ================== ПАНЕЛЬ МОДЕРАЦИИ ==================
async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    await update.effective_message.reply_text("⚙️ Панель управления", reply_markup=panel_keyboard())

async def cb_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id != MOD_GROUP_ID:
        await q.answer(); return
    parts = q.data.split(":")  # p:...
    await q.answer()

    if parts[1] == "stats":
        txt = await stats_text()
        await q.message.edit_text(txt, reply_markup=stats_keyboard())
        return

    if parts[1] == "history":
        ids = await last_tickets(limit=10)
        if not ids:
            await q.message.edit_text("Тикетов пока нет.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="p:back")]]
            ))
            return
        await q.message.edit_text("📜 Выберите тикет:", reply_markup=history_menu_keyboard(ids))
        return

    if parts[1] == "autores":
        en = await autores_enabled()
        await q.message.edit_text("🤖 Настройки автоответчиков", reply_markup=autores_menu_keyboard(en))
        return

    if parts[1] == "status":
        # Проверка статуса: коннект к БД, размер пула, открытые тикеты
        try:
            open_cnt = await open_tickets_count()
            await q.message.edit_text(
                f"🟢 Бот работает\n"
                f"🗄️ База: OK\n"
                f"🎫 Открытых тикетов: {open_cnt}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="p:back")]])
            )
        except Exception as e:
            await q.message.edit_text(
                f"🔴 Проблема: {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="p:back")]])
            )
        return

    if parts[1] == "back":
        await q.message.edit_text("⚙️ Панель управления", reply_markup=panel_keyboard())
        return

    if parts[1] == "stats" and len(parts) >= 3 and parts[2] == "refresh":
        txt = await stats_text()
        await q.message.edit_text(txt, reply_markup=stats_keyboard())
        return

    if parts[1] == "history" and len(parts) >= 3 and parts[2] == "show":
        t_id = parts[3]
        if not await ticket_exists(t_id):
            await q.message.reply_text("Тикет не найден.")
            return
        txt = await ticket_history_text(t_id, limit=50)
        await q.message.reply_text(txt)
        return

# ================== АВТООТВЕТЧИКИ (КНОПКИ + ВВОД) ==================
async def cb_autores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.id != MOD_GROUP_ID:
        await q.answer(); return
    parts = q.data.split(":")  # ar:...
    await q.answer()

    if parts[1] == "toggle":
        en = await autores_enabled()
        await set_autores_enabled(not en)
        en2 = await autores_enabled()
        try:
            await q.message.edit_reply_markup(reply_markup=autores_menu_keyboard(en2))
        except Exception:
            pass
        return

    if parts[1] == "cat":
        cat = parts[2]
        cur = await get_autoresponder_text(cat) or "— не задан —"
        await q.message.reply_text(
            f"{CAT_TITLES_RU.get(cat, cat)}\n\nТекущий автоответ:\n{cur}",
            reply_markup=autores_cat_keyboard(cat)
        )
        return

    if parts[1] == "edit":
        cat = parts[2]
        context.chat_data["edit_autores_cat"] = cat
        await q.message.reply_text(f"✏️ Отправьте новый текст автоответа для: {CAT_TITLES_RU.get(cat, cat)}")
        return

async def mod_group_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    cat = context.chat_data.get("edit_autores_cat")
    if not cat:
        return
    text = update.effective_message.text or ""
    await set_autoresponder_text(cat, text)
    context.chat_data.pop("edit_autores_cat", None)
    await update.effective_message.reply_text("✅ Текст автоответа обновлён.")

# ================== ИСТОРИЯ/СТАТИСТИКА КОМАНДАМИ ==================
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /history <TICKET_ID>")
        return
    t_id = context.args[0]
    if not await ticket_exists(t_id):
        await update.effective_message.reply_text("Тикет не найден.")
        return
    txt = await ticket_history_text(t_id, limit=50)
    await update.effective_message.reply_text(txt)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    txt = await stats_text()
    await update.effective_message.reply_text(txt)

# ================== РЕГИСТРАЦИЯ ХЕНДЛЕРОВ ==================
def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(init_pool_and_db)  # инициализация пула и схемы БД без проблем с loop
        .build()
    )

    # Пользователь
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(cb_lang, pattern="^lang:"))
    app.add_handler(CallbackQueryHandler(cb_category, pattern="^cat:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, pm_user_message))
    app.add_handler(CommandHandler("close", cmd_close_user, filters.ChatType.PRIVATE))

    # Модерация
    app.add_handler(CallbackQueryHandler(cb_ticket_actions, pattern="^t:"))
    app.add_handler(MessageHandler(filters.Chat(MOD_GROUP_ID) & ~filters.COMMAND, mod_group_message))
    app.add_handler(CommandHandler("end", cmd_end, filters.Chat(MOD_GROUP_ID)))
    app.add_handler(CommandHandler("panel", cmd_panel, filters.Chat(MOD_GROUP_ID)))
    app.add_handler(CallbackQueryHandler(cb_panel, pattern="^p:"))
    app.add_handler(CallbackQueryHandler(cb_autores, pattern="^ar:"))
    app.add_handler(MessageHandler(filters.Chat(MOD_GROUP_ID) & filters.TEXT, mod_group_text))
    app.add_handler(CommandHandler("history", cmd_history, filters.Chat(MOD_GROUP_ID)))
    app.add_handler(CommandHandler("stats", cmd_stats, filters.Chat(MOD_GROUP_ID)))

    return app

# ================== ЗАПУСК ==================
if __name__ == "__main__":
    app = build_app()
    print("🚀 Бот запущен")
    app.run_polling(drop_pending_updates=True)
