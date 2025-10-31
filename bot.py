# bot.py
import os
import asyncio
import datetime as dt
from typing import Optional, Dict, List

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, Message, User as TgUser
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler, filters
)

# ====== DB (PostgreSQL, psycopg3 async) ======
from psycopg_pool import AsyncConnectionPool
import psycopg

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MOD_GROUP_ID = int(os.getenv("MOD_GROUP_ID", "0"))  # -100XXXXXXXXXXXX
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ------------ Локальные настройки ------------
LANGS = {"ru": "Русский", "en": "English"}
CATS = {
    "ru": [
        ("🔧 Техническая помощь", "tech"),
        ("💳 Помощь с платежами", "pay"),
        ("🔄 Сброс HWID", "hwid"),
        ("🤝 Сотрудничество", "coop"),
        ("❓ FAQ / Цены / Товары", "faq"),
    ],
    "en": [
        ("🔧 Technical Support", "tech"),
        ("💳 Payment Help", "pay"),
        ("🔄 HWID Reset", "hwid"),
        ("🤝 Cooperation", "coop"),
        ("❓ FAQ / Prices / Products", "faq"),
    ],
}
CAT_TITLES_RU = {
    "tech": "🔧 Техническая помощь",
    "pay": "💳 Помощь с платежами",
    "hwid": "🔄 Сброс HWID",
    "coop": "🤝 Сотрудничество",
    "faq": "❓ FAQ / Цены / Товары",
}

# Активные «режимы ответа» модераторов: mod_id -> ticket_id
active_reply: Dict[int, str] = {}

POOL: Optional[AsyncConnectionPool] = None

# =============== SQL ===============
INIT_SQL = """
-- Безопасное создание таблиц
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  lang TEXT NOT NULL DEFAULT 'ru'
);

CREATE TABLE IF NOT EXISTS tickets (
  id BIGSERIAL PRIMARY KEY,
  ticket_id TEXT UNIQUE,
  user_id BIGINT NOT NULL,
  category TEXT NOT NULL,
  reason TEXT,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  assigned_to BIGINT,
  closed_by BIGINT,
  closed_by_name TEXT,
  group_header_msg_id BIGINT
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  from_role TEXT NOT NULL,           -- 'user' | 'mod' | 'system'
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
  category TEXT PRIMARY KEY,
  text TEXT
);

-- Дозаливаем недостающие поля (если таблицы существовали раньше)
ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reason TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS assigned_to BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS closed_by BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS closed_by_name TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS group_header_msg_id BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS ticket_id TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS from_role TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS text TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS user_msg_id BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS group_msg_id BIGINT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

-- Значения по умолчанию/NOT NULL
UPDATE tickets SET status = 'open' WHERE status IS NULL;
ALTER TABLE tickets ALTER COLUMN status SET DEFAULT 'open';
ALTER TABLE tickets ALTER COLUMN status SET NOT NULL;
UPDATE tickets SET created_at = NOW() WHERE created_at IS NULL;
ALTER TABLE tickets ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE users ALTER COLUMN lang SET DEFAULT 'ru';

-- Включаем автоответчики по умолчанию
INSERT INTO settings(key,value)
VALUES ('autoresponders_enabled', '1')
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;
"""

# ========= DB helpers =========
async def db_exec(sql: str, *params):
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            await conn.commit()

async def db_one(sql: str, *params):
    async with POOL.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()

async def db_all(sql: str, *params):
    async with POOL.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

# ========= Модельные операции =========
def gen_ticket_id(seq: int) -> str:
    today = dt.datetime.now().strftime("%Y%m%d")
    return f"T-{today}-{seq:04d}"

async def autores_enabled() -> bool:
    r = await db_one("SELECT value FROM settings WHERE key='autoresponders_enabled'")
    return (r and r["value"] == "1")

async def set_autores_enabled(enabled: bool):
    await db_exec(
        "INSERT INTO settings(key,value) VALUES('autoresponders_enabled', $1) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        "1" if enabled else "0",
    )

async def get_user_lang(uid: int) -> str:
    r = await db_one("SELECT lang FROM users WHERE user_id=$1", uid)
    return r["lang"] if r and r["lang"] else "ru"

async def set_user_lang(uid: int, username: Optional[str], lang: str):
    await db_exec(
        "INSERT INTO users(user_id, username, lang) VALUES($1,$2,$3) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, lang=EXCLUDED.lang",
        uid, username, lang
    )

async def get_autoresponder_text(cat: str) -> Optional[str]:
    r = await db_one("SELECT text FROM autoresponders WHERE category=$1", cat)
    return r["text"] if r else None

async def set_autoresponder_text(cat: str, text: str):
    await db_exec(
        "INSERT INTO autoresponders(category,text) VALUES($1,$2) "
        "ON CONFLICT(category) DO UPDATE SET text=EXCLUDED.text",
        cat, text
    )

async def create_ticket(user_id: int, category: str, reason: str, description: str) -> str:
    # Вставляем, получаем seq, формируем ticket_id, обновляем
    row = await db_one(
        "INSERT INTO tickets(user_id,category,reason,description,status) "
        "VALUES($1,$2,$3,$4,'open') RETURNING id",
        user_id, category, reason, description
    )
    seq = row["id"]
    t_id = gen_ticket_id(seq)
    await db_exec("UPDATE tickets SET ticket_id=$1 WHERE id=$2", t_id, seq)
    return t_id

async def store_group_header(ticket_id: str, msg_id: int):
    await db_exec(
        "UPDATE tickets SET group_header_msg_id=$1 WHERE ticket_id=$2",
        msg_id, ticket_id
    )

async def mark_assigned(ticket_id: str, mod_id: int):
    await db_exec("UPDATE tickets SET assigned_to=$1 WHERE ticket_id=$2", mod_id, ticket_id)

async def get_ticket_user(ticket_id: str) -> Optional[int]:
    r = await db_one("SELECT user_id FROM tickets WHERE ticket_id=$1", ticket_id)
    return r["user_id"] if r else None

async def get_ticket_header(ticket_id: str) -> Optional[int]:
    r = await db_one("SELECT group_header_msg_id FROM tickets WHERE ticket_id=$1", ticket_id)
    return r["group_header_msg_id"] if r else None

async def record_msg(ticket_id: str, role: str, text: str,
                     user_msg_id: Optional[int], group_msg_id: Optional[int]):
    await db_exec(
        "INSERT INTO messages(ticket_id,from_role,text,user_msg_id,group_msg_id) "
        "VALUES($1,$2,$3,$4,$5)",
        ticket_id, role, text or "", user_msg_id, group_msg_id
    )

async def get_ticket_group_msg_ids(ticket_id: str) -> List[int]:
    rows = await db_all(
        "SELECT group_msg_id FROM messages WHERE ticket_id=$1 AND group_msg_id IS NOT NULL",
        ticket_id
    )
    return [r["group_msg_id"] for r in rows if r["group_msg_id"] is not None]

async def ticket_exists(ticket_id: str) -> bool:
    r = await db_one("SELECT 1 FROM tickets WHERE ticket_id=$1", ticket_id)
    return r is not None

async def ticket_status(ticket_id: str) -> Optional[str]:
    r = await db_one("SELECT status FROM tickets WHERE ticket_id=$1", ticket_id)
    return r["status"] if r else None

async def close_ticket(ticket_id: str, closed_by: Optional[int], closed_by_name: Optional[str]):
    await db_exec(
        "UPDATE tickets SET status='closed', closed_by=$1, closed_by_name=$2 WHERE ticket_id=$3",
        closed_by, closed_by_name, ticket_id
    )

async def ticket_history_text(ticket_id: str, limit: int = 40) -> str:
    rows = await db_all(
        "SELECT from_role,text,created_at FROM messages WHERE ticket_id=$1 ORDER BY id ASC",
        ticket_id
    )
    if not rows:
        return f"📜 История по {ticket_id}: сообщений нет."
    rows = rows[-limit:]
    parts = [f"📜 История по {ticket_id} (последние {len(rows)}):", ""]
    for r in rows:
        role = {"user": "👤 Пользователь", "mod": "🛠 Модератор", "system": "📎 Система"}.get(r["from_role"], r["from_role"])
        txt = (r["text"] or "").strip()
        if len(txt) > 600:
            txt = txt[:600] + "…"
        parts.append(f"{role}:\n{txt}\n")
    return "\n".join(parts)

async def stats_text() -> str:
    rows = await db_all(
        """
        SELECT COALESCE(closed_by_name, closed_by::text) AS who, COUNT(*) AS c
        FROM tickets
        WHERE status='closed' AND closed_by IS NOT NULL
        GROUP BY who
        ORDER BY c DESC
        """
    )
    if not rows:
        return "📊 Пока никто не закрыл ни одного тикета."
    out = ["📊 Статистика закрытий:"]
    for r in rows:
        out.append(f"- {r['who']}: {r['c']}")
    return "\n".join(out)

async def last_tickets(limit: int = 12) -> List[str]:
    rows = await db_all("SELECT ticket_id FROM tickets ORDER BY id DESC LIMIT $1", limit)
    return [r["ticket_id"] for r in rows]

# ========= Клавиатуры =========
def ticket_keyboard(ticket_id: str, assigned_to: Optional[int] = None) -> InlineKeyboardMarkup:
    assigned_str = f"👨‍💻 В работе у {assigned_to}" if assigned_to else "🤷‍♂️ Свободен"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 История", callback_data=f"t:{ticket_id}:hist"),
         InlineKeyboardButton("✋ Взять тикет", callback_data=f"t:{ticket_id}:take")],
        [InlineKeyboardButton("✉️ Ответить", callback_data=f"t:{ticket_id}:reply"),
         InlineKeyboardButton("🛑 Завершить", callback_data=f"t:{ticket_id}:end")],
        [InlineKeyboardButton("✅ Закрыть", callback_data=f"t:{ticket_id}:close")],
        [InlineKeyboardButton(f"{assigned_str}", callback_data=f"t:{ticket_id}:noop")]
    ])

def user_menu_keyboard(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"uclose:{ticket_id}")]
    ])

def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="p:stats"),
         InlineKeyboardButton("📜 История", callback_data="p:history")],
        [InlineKeyboardButton("🤖 Автоответчики", callback_data="p:autores"),
         InlineKeyboardButton("📟 Проверить статус", callback_data="p:status")]
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
            for c in ["tech", "pay", "hwid", "coop", "faq"]]
    rows.append([InlineKeyboardButton(toggle, callback_data="ar:toggle")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="p:back")])
    return InlineKeyboardMarkup(rows)

def autores_cat_keyboard(cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить автоответ", callback_data=f"ar:edit:{cat}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="p:autores")]
    ])

# ========= Пользовательский поток =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username
    # создаём пользователя с дефолтным языком (ru), username
    await set_user_lang(uid, uname, "ru")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")]
    ])
    await update.effective_message.reply_text("Выберите язык / Choose your language:", reply_markup=kb)

async def cb_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.split(":")[1]
    await set_user_lang(q.from_user.id, q.from_user.username, lang)
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
    t = "Пожалуйста, коротко укажите причину обращения:" if lang == "ru" else "Please briefly describe your reason:"
    await q.message.reply_text(t)

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
                   f"Модераторы скоро ответят здесь.") if lang == "ru" else \
                  (f"✅ Ticket {t_id} created.\nModerators will reply here soon.")
        await update.effective_message.reply_text(confirm, reply_markup=user_menu_keyboard(t_id))

        header = (f"🆕 Новый тикет {t_id}\n"
                  f"Категория: {CAT_TITLES_RU.get(cat, cat)}\n"
                  f"Причина: {reason or '—'}\n"
                  f"Описание: {description or '—'}\n"
                  f"От: @{update.effective_user.username or update.effective_user.full_name} (ID: {uid})")
        hmsg = await context.bot.send_message(MOD_GROUP_ID, header, reply_markup=ticket_keyboard(t_id))
        await store_group_header(t_id, hmsg.message_id)
        await record_msg(t_id, "system", header, None, hmsg.message_id)

        # Автоответчик
        if await autores_enabled():
            atext = await get_autoresponder_text(cat)
            if atext:
                try:
                    await context.bot.send_message(chat_id=uid, text=atext)
                except Exception:
                    pass

        # Лог
        await record_msg(
            t_id, "user", f"[Причина] {reason}\n[Описание] {description}",
            update.effective_message.message_id, None
        )
        context.user_data.clear()
        return

    # 3) Доп.сообщения пользователя — в последний открытый тикет
    r = await db_one(
        "SELECT ticket_id FROM tickets WHERE user_id=$1 AND status='open' ORDER BY id DESC LIMIT 1",
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

# ========= Кнопки тикета в группе =========
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
        txt = await ticket_history_text(ticket_id, limit=40)
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
            f"Все ваши сообщения в этой группе будут пересылаться пользователю, пока не нажмёте «🛑 Завершить»."
        )
        return

    if action == "end":
        if active_reply.get(mod.id):
            ended = active_reply.pop(mod.id)
            await q.message.reply_text(f"🛑 Режим ответа для {ended} завершён.")
        else:
            await q.message.reply_text("У вас нет активного режима ответа.")
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
                await asyncio.sleep(0.02)
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

# ========= Пересылка из группы модерации пользователю =========
async def mod_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    mod_id = update.effective_user.id
    ticket_id = active_reply.get(mod_id)
    if not ticket_id:
        return
    # игнорируем команды
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

# ========= Закрытие со стороны пользователя (кнопкой) =========
async def cb_user_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat.type != ChatType.PRIVATE:
        await q.answer(); return
    await q.answer()
    uid = q.from_user.id
    # ищем только открытый тикет последним
    r = await db_one("SELECT ticket_id FROM tickets WHERE user_id=$1 AND status='open' ORDER BY id DESC LIMIT 1", uid)
    if not r:
        await q.message.reply_text("У вас нет открытых тикетов.")
        return
    ticket_id = r["ticket_id"]

    # удалить групповые сообщения
    gids = await get_ticket_group_msg_ids(ticket_id)
    for mid in gids:
        try:
            await context.bot.delete_message(MOD_GROUP_ID, mid)
            await asyncio.sleep(0.02)
        except Exception:
            pass

    await close_ticket(ticket_id, None, None)
    await q.message.reply_text(f"✅ Тикет {ticket_id} закрыт.")
    try:
        await context.bot.send_message(MOD_GROUP_ID, f"❌ Тикет {ticket_id} закрыт пользователем.")
    except Exception:
        pass

# ========= Панель модерации =========
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

    if parts[1] == "stats" and (len(parts) == 2 or parts[2] == "refresh"):
        txt = await stats_text()
        await q.message.edit_text(txt, reply_markup=stats_keyboard())
        return

    if parts[1] == "history":
        ids = await last_tickets(limit=12)
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
        # Простая проверка состояния БД/пула
        try:
            r = await db_one("SELECT COUNT(*) AS c FROM tickets")
            total = r["c"] if r else 0
            txt = f"📟 Сервис OK\nТикетов в базе: {total}"
        except Exception as e:
            txt = f"❌ Ошибка доступа к БД: {e}"
        await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="p:back")]]
        ))
        return

    if parts[1] == "back":
        await q.message.edit_text("⚙️ Панель управления", reply_markup=panel_keyboard())
        return

    if parts[1] == "history" and len(parts) >= 3 and parts[2] == "show":
        t_id = parts[3]
        if not await ticket_exists(t_id):
            await q.message.reply_text("Тикет не найден.")
            return
        txt = await ticket_history_text(t_id, limit=50)
        await q.message.reply_text(txt)
        return

# ========= Автоответчики =========
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

# ========= Команды статистики/истории (доп.) =========
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
    txt = await ticket_history_text(t_id, limit=60)
    await update.effective_message.reply_text(txt)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != MOD_GROUP_ID:
        return
    txt = await stats_text()
    await update.effective_message.reply_text(txt)

# ========= Инициализация =========
async def init_db():
    # создаём пул (без депр. предупреждения)
    global POOL
    if POOL is None:
        POOL = AsyncConnectionPool(DATABASE_URL, open=False)
        await POOL.open()

    # выполняем INIT_SQL
    async with POOL.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(INIT_SQL)
            await conn.commit()

    print("✅ База данных инициализирована")

# ========= MAIN =========
async def main():
    if not BOT_TOKEN or not MOD_GROUP_ID or not DATABASE_URL:
        raise RuntimeError("Заполни BOT_TOKEN, MOD_GROUP_ID и DATABASE_URL в переменных окружения")

    await init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Пользовательские хендлеры
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(cb_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_category, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(cb_user_close, pattern=r"^uclose:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, pm_user_message))

    # Группа модерации
    app.add_handler(CallbackQueryHandler(cb_ticket_actions, pattern=r"^t:"))
    app.add_handler(MessageHandler(filters.Chat(MOD_GROUP_ID) & ~filters.COMMAND, mod_group_message))
    app.add_handler(CommandHandler("panel", cmd_panel, filters.Chat(MOD_GROUP_ID)))
    app.add_handler(CallbackQueryHandler(cb_panel, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(cb_autores, pattern=r"^ar:"))
    app.add_handler(MessageHandler(filters.Chat(MOD_GROUP_ID) & filters.TEXT, mod_group_text))
    app.add_handler(CommandHandler("history", cmd_history, filters.Chat(MOD_GROUP_ID)))
    app.add_handler(CommandHandler("stats", cmd_stats, filters.Chat(MOD_GROUP_ID)))

    print("🚀 Бот запущен")
    await app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())
