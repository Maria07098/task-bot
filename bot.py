#!/usr/bin/env python3
"""Task Tracker — Telegram Bot"""

import os
import sqlite3
import logging
from datetime import datetime, time as dtime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TOKEN    = os.environ["BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_PATH  = DATA_DIR / "tasks.db"

# ── Constants ─────────────────────────────────────────────────────────────────

PIPELINE = [
    "user-spec", "planning", "in-progress",
    "mini-review", "code-review", "deploy", "done",
]

STAGE_LABEL = {
    "user-spec":   "📝 User Spec",
    "planning":    "🗺 Планирование",
    "in-progress": "⚙️ В работе",
    "mini-review": "🔍 Мини-ревью",
    "code-review": "👁 Код-ревью",
    "deploy":      "🚀 Деплой",
    "done":        "✅ Готово",
}

TYPE_LABEL     = {"feature": "✨ Фича", "bug": "🐛 Баг", "refactoring": "🔧 Рефакторинг"}
SIZE_LABEL     = {"S": "S — до 1ч", "M": "M — 2–4ч", "L": "L — полдня"}
PRIORITY_LABEL = {"high": "🔴 Высокий", "medium": "🟡 Средний", "low": "🟢 Низкий"}
PRIORITY_ICON  = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Conversation states
TITLE, TASK_TYPE, SIZE, PRIORITY, WHY, DEADLINE = range(6)

# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id       TEXT PRIMARY KEY,
                title    TEXT NOT NULL,
                type     TEXT NOT NULL,
                size     TEXT NOT NULL,
                priority TEXT NOT NULL,
                stage    TEXT NOT NULL DEFAULT 'user-spec',
                why      TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                created  TEXT NOT NULL,
                updated  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counter (
                id    INTEGER PRIMARY KEY DEFAULT 1,
                value INTEGER DEFAULT 0
            );
            INSERT OR IGNORE INTO counter (id, value) VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS users (
                chat_id  INTEGER PRIMARY KEY,
                username TEXT DEFAULT ''
            );
        """)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def next_task_id() -> str:
    with get_conn() as conn:
        conn.execute("UPDATE counter SET value = value + 1 WHERE id = 1")
        row = conn.execute("SELECT value FROM counter WHERE id = 1").fetchone()
        return f"TASK-{row['value']:03d}"


def db_add_task(task: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (:id,:title,:type,:size,:priority,:stage,:why,:deadline,:created,:updated)",
            task,
        )


def db_get_tasks(stage: str = None) -> list:
    with get_conn() as conn:
        if stage:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE stage=? ORDER BY created", (stage,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created"
            ).fetchall()
        return [dict(r) for r in rows]


def db_get_task(task_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def db_update_task(task_id: str, **fields):
    fields["updated"] = now()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [task_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)


def db_delete_task(task_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def db_register_user(chat_id: int, username: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (chat_id, username) VALUES (?, ?)",
            (chat_id, username or ""),
        )


def db_get_users() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id FROM users").fetchall()
        return [r["chat_id"] for r in rows]

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(raw: str) -> str | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    # dd.mm without year — assume current year
    try:
        dt = datetime.strptime(raw.strip(), "%d.%m").replace(year=datetime.now().year)
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return None


def deadline_status(deadline_str: str) -> str:
    if not deadline_str:
        return ""
    try:
        dl    = datetime.strptime(deadline_str, "%d.%m.%Y").date()
        days  = (dl - datetime.now().date()).days
        if days < 0:
            return f"\n⏰ Дедлайн: {deadline_str} — *просрочено на {-days} дн.*"
        elif days == 0:
            return f"\n⏰ Дедлайн: {deadline_str} — *сегодня!*"
        elif days == 1:
            return f"\n⏰ Дедлайн: {deadline_str} — *завтра*"
        else:
            return f"\n⏰ Дедлайн: {deadline_str} (через {days} дн.)"
    except ValueError:
        return f"\n⏰ Дедлайн: {deadline_str}"


def format_card(task: dict) -> str:
    why_str = f"\n💬 *Зачем:* {task['why']}" if task.get("why") else ""
    return (
        f"*{task['id']}* — {task['title']}\n"
        f"{STAGE_LABEL[task['stage']]}\n"
        f"{TYPE_LABEL[task['type']]}  •  {SIZE_LABEL[task['size']]}  •  "
        f"{PRIORITY_LABEL[task['priority']]}"
        f"{why_str}"
        f"{deadline_status(task.get('deadline', ''))}"
    )


def task_buttons(task: dict) -> InlineKeyboardMarkup:
    tid     = task["id"]
    cur_idx = PIPELINE.index(task["stage"])
    rows    = []

    if cur_idx < len(PIPELINE) - 1:
        next_stage = PIPELINE[cur_idx + 1]
        rows.append([InlineKeyboardButton(
            f"➡️ {STAGE_LABEL[next_stage]}", callback_data=f"move:{tid}"
        )])

    bottom = []
    if task["stage"] != "done":
        bottom.append(InlineKeyboardButton("✅ Готово", callback_data=f"done:{tid}"))
    bottom.append(InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{tid}"))
    rows.append(bottom)

    return InlineKeyboardMarkup(rows)

# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    await update.message.reply_text(
        "👋 *Task Tracker Bot*\n\n"
        "/add — добавить задачу\n"
        "/list — активные задачи\n"
        "/board — доска по этапам\n"
        "/upcoming — задачи с дедлайном (7 дней)\n"
        "/help — справка",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Команды:*\n\n"
        "/add — добавить задачу\n"
        "/list — активные задачи\n"
        "/board — канбан-доска\n"
        "/upcoming — ближайшие дедлайны\n"
        "/cancel — отменить добавление задачи\n\n"
        "*Этапы:*\n"
        "📝 → 🗺 → ⚙️ → 🔍 → 👁 → 🚀 → ✅\n\n"
        "*Дедлайн:* формат `25.05.2026`\n"
        "Напоминание придёт в 9:00 за день до дедлайна и в день дедлайна.",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    tasks = [t for t in db_get_tasks() if t["stage"] != "done"]

    if not tasks:
        await update.message.reply_text("Активных задач нет.\n/add — добавить первую.")
        return

    await update.message.reply_text(f"*Активных задач: {len(tasks)}*", parse_mode="Markdown")
    for task in tasks:
        await update.message.reply_text(
            format_card(task),
            parse_mode="Markdown",
            reply_markup=task_buttons(task),
        )


async def cmd_board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    tasks = [t for t in db_get_tasks() if t["stage"] != "done"]

    lines = ["*Доска задач*\n"]
    for stage in PIPELINE[:-1]:
        stage_tasks = [t for t in tasks if t["stage"] == stage]
        lines.append(f"{STAGE_LABEL[stage]} *({len(stage_tasks)})*")
        if stage_tasks:
            for t in stage_tasks:
                dl = f" ⏰{t['deadline']}" if t.get("deadline") else ""
                lines.append(f"  {PRIORITY_ICON[t['priority']]} `{t['id']}` {t['title'][:35]}{dl}")
        else:
            lines.append("  —")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    today   = datetime.now().date()
    tasks   = db_get_tasks()
    hitting = []

    for task in tasks:
        if task["stage"] == "done" or not task.get("deadline"):
            continue
        parsed = parse_date(task["deadline"])
        if not parsed:
            continue
        days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
        if days <= 7:
            hitting.append((days, task))

    if not hitting:
        await update.message.reply_text("Нет задач с дедлайном в ближайшие 7 дней. 🎉")
        return

    hitting.sort(key=lambda x: x[0])
    await update.message.reply_text(
        f"*Задачи с дедлайном (7 дней): {len(hitting)}*", parse_mode="Markdown"
    )
    for _, task in hitting:
        await update.message.reply_text(
            format_card(task),
            parse_mode="Markdown",
            reply_markup=task_buttons(task),
        )

# ── Add task — conversation ───────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Новая задача*\n\nКак называется задача? Напиши кратко.",
        parse_mode="Markdown",
    )
    return TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text.strip()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✨ Фича",       callback_data="type:feature"),
        InlineKeyboardButton("🐛 Баг",        callback_data="type:bug"),
        InlineKeyboardButton("🔧 Рефакторинг", callback_data="type:refactoring"),
    ]])
    await update.message.reply_text("Тип задачи?", reply_markup=kb)
    return TASK_TYPE


async def add_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["type"] = query.data.split(":")[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("S — до 1ч",  callback_data="size:S"),
        InlineKeyboardButton("M — 2–4ч",   callback_data="size:M"),
        InlineKeyboardButton("L — полдня", callback_data="size:L"),
    ]])
    await query.edit_message_text("Размер задачи?", reply_markup=kb)
    return SIZE


async def add_size(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["size"] = query.data.split(":")[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔴 Высокий", callback_data="priority:high"),
        InlineKeyboardButton("🟡 Средний", callback_data="priority:medium"),
        InlineKeyboardButton("🟢 Низкий",  callback_data="priority:low"),
    ]])
    await query.edit_message_text("Приоритет?", reply_markup=kb)
    return PRIORITY


async def add_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["priority"] = query.data.split(":")[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Пропустить", callback_data="skip:why")
    ]])
    await query.edit_message_text(
        "Зачем эта задача? Напиши кратко.\n_(или нажми Пропустить)_",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WHY


async def add_why_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["why"] = update.message.text.strip()
    await _ask_deadline(update.message, edit=False)
    return DEADLINE


async def add_why_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["why"] = ""
    await _ask_deadline(query.message, edit=True)
    return DEADLINE


async def _ask_deadline(msg, edit: bool):
    kb   = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="skip:deadline")]])
    text = "Дедлайн? Напиши дату в формате `25.05.2026`\n_(или нажми Пропустить)_"
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def add_deadline_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = parse_date(update.message.text)
    if not parsed:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="skip:deadline")]])
        await update.message.reply_text(
            "Не понял дату. Попробуй формат `25.05.2026`\n_(или нажми Пропустить)_",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return DEADLINE
    context.user_data["deadline"] = parsed
    await _finish_add(update.message, context, edit=False)
    return ConversationHandler.END


async def add_deadline_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["deadline"] = ""
    await _finish_add(query.message, context, edit=True)
    return ConversationHandler.END


async def _finish_add(msg, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    ud      = context.user_data
    task_id = next_task_id()
    task    = {
        "id":       task_id,
        "title":    ud["title"],
        "type":     ud["type"],
        "size":     ud["size"],
        "priority": ud["priority"],
        "stage":    "user-spec",
        "why":      ud.get("why", ""),
        "deadline": ud.get("deadline", ""),
        "created":  now(),
        "updated":  now(),
    }
    db_add_task(task)

    text = f"✅ *Задача создана!*\n\n{format_card(task)}"
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=task_buttons(task))
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=task_buttons(task))

    context.user_data.clear()


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ── Inline button callbacks ───────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()

    action, task_id = query.data.split(":", 1)
    task = db_get_task(task_id)

    if not task:
        await query.edit_message_text("Задача не найдена.")
        return

    if action == "move":
        cur_idx = PIPELINE.index(task["stage"])
        if cur_idx < len(PIPELINE) - 1:
            new_stage = PIPELINE[cur_idx + 1]
            db_update_task(task_id, stage=new_stage)
            task = db_get_task(task_id)
            await query.edit_message_text(
                format_card(task),
                parse_mode="Markdown",
                reply_markup=task_buttons(task),
            )

    elif action == "done":
        db_update_task(task_id, stage="done")
        await query.edit_message_text(
            f"✅ *{task_id} завершена!*\n_{task['title']}_",
            parse_mode="Markdown",
        )

    elif action == "del":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"delok:{task_id}"),
            InlineKeyboardButton("Нет",         callback_data=f"delno:{task_id}"),
        ]])
        await query.edit_message_text(
            f"Удалить задачу *{task_id}*?\n_{task['title']}_",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif action == "delok":
        db_delete_task(task_id)
        await query.edit_message_text(f"🗑 Задача *{task_id}* удалена.", parse_mode="Markdown")

    elif action == "delno":
        await query.edit_message_text(
            format_card(task),
            parse_mode="Markdown",
            reply_markup=task_buttons(task),
        )

# ── Deadline reminder job ─────────────────────────────────────────────────────

async def check_deadlines(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().date()
    users = db_get_users()
    if not users:
        return

    for task in db_get_tasks():
        if task["stage"] == "done" or not task.get("deadline"):
            continue
        parsed = parse_date(task["deadline"])
        if not parsed:
            continue
        days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
        if days not in (0, 1):
            continue

        label = "завтра" if days == 1 else "сегодня"
        text  = (
            f"⏰ *Напоминание о дедлайне*\n\n"
            f"*{task['id']}* — {task['title']}\n"
            f"Дедлайн *{label}* ({task['deadline']})\n"
            f"Этап: {STAGE_LABEL[task['stage']]}"
        )
        for chat_id in users:
            try:
                await context.bot.send_message(chat_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Не удалось отправить напоминание {chat_id}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            TITLE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            TASK_TYPE: [CallbackQueryHandler(add_type,         pattern="^type:")],
            SIZE:      [CallbackQueryHandler(add_size,         pattern="^size:")],
            PRIORITY:  [CallbackQueryHandler(add_priority,     pattern="^priority:")],
            WHY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_why_text),
                CallbackQueryHandler(add_why_skip, pattern="^skip:why$"),
            ],
            DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_deadline_text),
                CallbackQueryHandler(add_deadline_skip, pattern="^skip:deadline$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("board",    cmd_board))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(
        callback_handler, pattern="^(move|done|del|delok|delno):"
    ))

    # Напоминания каждый день в 9:00
    app.job_queue.run_daily(check_deadlines, time=dtime(9, 0))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

