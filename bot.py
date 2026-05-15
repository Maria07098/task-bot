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

SIZE_LABEL     = {"S": "S — до 1ч", "M": "M — 2–4ч", "L": "L — полдня"}
PRIORITY_LABEL = {"high": "🔴 Высокий", "medium": "🟡 Средний", "low": "🟢 Низкий"}
PRIORITY_ICON  = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Conversation states
TITLE, WHY, SIZE, PRIORITY, DEADLINE = range(5)
TASK_TYPE = 99  # не используется, оставлен для совместимости

# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
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
    conn.close()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def next_task_id() -> str:
    conn = get_conn()
    conn.execute("UPDATE counter SET value = value + 1 WHERE id = 1")
    row = conn.execute("SELECT value FROM counter WHERE id = 1").fetchone()
    val = row["value"]
    conn.close()
    return f"TASK-{val:03d}"


def db_add_task(task: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO tasks VALUES (:id,:title,:type,:size,:priority,:stage,:why,:deadline,:created,:updated)",
        task,
    )
    conn.close()


def db_get_tasks(stage: str = None) -> list:
    conn = get_conn()
    if stage:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE stage=? ORDER BY created", (stage,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created").fetchall()
    result = [dict(r) for r in rows]
    conn.close()
    return result


def db_get_task(task_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def db_update_task(task_id: str, **fields):
    fields["updated"] = now()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [task_id]
    conn = get_conn()
    conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", values)
    conn.close()


def db_delete_task(task_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.close()


def db_register_user(chat_id: int, username: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO users (chat_id, username) VALUES (?, ?)",
        (chat_id, username or ""),
    )
    conn.close()


def db_get_users() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT chat_id FROM users").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(raw: str) -> str | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    try:
        dt = datetime.strptime(raw.strip(), "%d.%m").replace(year=datetime.now().year)
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return None


def deadline_status(deadline_str: str) -> str:
    if not deadline_str:
        return ""
    try:
        dl   = datetime.strptime(deadline_str, "%d.%m.%Y").date()
        days = (dl - datetime.now().date()).days
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
    return (
        f"*{task['id']}* — {task['title']}\n"
        f"{STAGE_LABEL[task['stage']]}\n"
        f"{SIZE_LABEL[task['size']]}  •  {PRIORITY_LABEL[task['priority']]}\n"
        f"💬 {task['why']}"
        f"{deadline_status(task.get('deadline', ''))}"
    )


def task_buttons(task: dict) -> InlineKeyboardMarkup:
    """
    Кнопка ✅ Готово — только на этапе deploy (последнем перед done).
    Сразу после создания задачи кнопки завершения нет.
    """
    tid     = task["id"]
    cur_idx = PIPELINE.index(task["stage"])
    rows    = []

    # Кнопка перехода на следующий этап
    if cur_idx < len(PIPELINE) - 1:
        next_stage = PIPELINE[cur_idx + 1]
        label = "✅ Завершить" if next_stage == "done" else f"➡️ {STAGE_LABEL[next_stage]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"move:{tid}")])

    # Нижний ряд: удалить + меню
    bottom = [
        InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{tid}"),
        InlineKeyboardButton("🏠 Меню",   callback_data="menu:main"),
    ]
    rows.append(bottom)

    return InlineKeyboardMarkup(rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить задачу", callback_data="menu:add")],
        [
            InlineKeyboardButton("📋 Активные",      callback_data="menu:list"),
            InlineKeyboardButton("📊 Доска",          callback_data="menu:board"),
        ],
        [
            InlineKeyboardButton("✅ Выполненные",    callback_data="menu:done_list"),
            InlineKeyboardButton("⏰ Просроченные",   callback_data="menu:overdue"),
        ],
        [InlineKeyboardButton("🗓 Запланированные",  callback_data="menu:planned")],
    ])

# ── Menu text ─────────────────────────────────────────────────────────────────

MENU_TEXT = "👋 *Task Tracker*\nВыбери действие:"

# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown",
                                    reply_markup=main_menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Команды:*\n\n"
        "/start — главное меню\n"
        "/add — добавить задачу\n"
        "/cancel — отменить добавление\n\n"
        "*Этапы:*\n"
        "📝 → 🗺 → ⚙️ → 🔍 → 👁 → 🚀 → ✅\n\n"
        "Кнопка *Завершить* появляется только на этапе 🚀 Деплой.\n"
        "Напоминание о дедлайне приходит в 9:00 за день до и в день дедлайна.",
        parse_mode="Markdown",
    )

# ── List views ────────────────────────────────────────────────────────────────

async def show_active(target, edit: bool = False):
    tasks = [t for t in db_get_tasks() if t["stage"] != "done"]
    if not tasks:
        text = "Активных задач нет."
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:main")]])
        if edit:
            await target.edit_message_text(text, reply_markup=kb)
        else:
            await target.reply_text(text, reply_markup=kb)
        return

    header = f"*Активных задач: {len(tasks)}*"
    if edit:
        await target.edit_message_text(header, parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup([[
                                           InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                                       ]]))
    else:
        await target.reply_text(header, parse_mode="Markdown")

    for task in tasks:
        await target.message.reply_text(format_card(task), parse_mode="Markdown",
                                        reply_markup=task_buttons(task)) \
            if edit else \
            await target.reply_text(format_card(task), parse_mode="Markdown",
                                    reply_markup=task_buttons(task))


async def show_done_list(msg):
    tasks = [t for t in db_get_tasks() if t["stage"] == "done"]
    kb    = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:main")]])
    if not tasks:
        await msg.reply_text("Выполненных задач пока нет.", reply_markup=kb)
        return
    await msg.reply_text(f"*Выполненных задач: {len(tasks)}*", parse_mode="Markdown")
    for task in tasks:
        await msg.reply_text(format_card(task), parse_mode="Markdown", reply_markup=kb)


async def show_overdue(msg):
    today = datetime.now().date()
    tasks = []
    for t in db_get_tasks():
        if t["stage"] == "done" or not t.get("deadline"):
            continue
        parsed = parse_date(t["deadline"])
        if parsed:
            days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
            if days < 0:
                tasks.append((days, t))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:main")]])
    if not tasks:
        await msg.reply_text("Просроченных задач нет. 🎉", reply_markup=kb)
        return
    tasks.sort(key=lambda x: x[0])
    await msg.reply_text(f"*Просроченных задач: {len(tasks)}*", parse_mode="Markdown")
    for _, task in tasks:
        await msg.reply_text(format_card(task), parse_mode="Markdown",
                             reply_markup=task_buttons(task))


async def show_planned(msg):
    today = datetime.now().date()
    tasks = []
    for t in db_get_tasks():
        if t["stage"] == "done" or not t.get("deadline"):
            continue
        parsed = parse_date(t["deadline"])
        if parsed:
            days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
            if days >= 0:
                tasks.append((days, t))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:main")]])
    if not tasks:
        await msg.reply_text("Нет запланированных задач с дедлайном.", reply_markup=kb)
        return
    tasks.sort(key=lambda x: x[0])
    await msg.reply_text(f"*Запланированных задач: {len(tasks)}*", parse_mode="Markdown")
    for _, task in tasks:
        await msg.reply_text(format_card(task), parse_mode="Markdown",
                             reply_markup=task_buttons(task))


async def show_board(msg):
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
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:main")]])
    await msg.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)

# ── Add task — conversation ───────────────────────────────────────────────────

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db_register_user(update.effective_chat.id, update.effective_user.username or "")
    context.user_data.clear()
    await update.message.reply_text(
        "➕ *Новая задача*\n\nКак называется задача? Напиши кратко.",
        parse_mode="Markdown",
    )
    return TITLE


async def add_start_from_menu(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await query.edit_message_text(
        "➕ *Новая задача*\n\nКак называется задача? Напиши кратко.",
        parse_mode="Markdown",
    )
    return TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "Опиши задачу подробнее — что нужно сделать и зачем?"
    )
    return WHY


async def add_why_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Описание обязательно. Напиши хотя бы пару слов.")
        return WHY
    context.user_data["why"] = text
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("S — до 1ч",  callback_data="size:S"),
        InlineKeyboardButton("M — 2–4ч",   callback_data="size:M"),
        InlineKeyboardButton("L — полдня", callback_data="size:L"),
    ]])
    await update.message.reply_text("Сколько времени займёт?", reply_markup=kb)
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
            parse_mode="Markdown", reply_markup=kb,
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
        "type":     "task",
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
    kb   = task_buttons(task)
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    context.user_data.clear()


async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Отменено.", reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

# ── Callback handler ──────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    data   = query.data

    # ── Меню ──────────────────────────────────────────────────────────────────
    if data == "menu:main":
        await query.edit_message_text(MENU_TEXT, parse_mode="Markdown",
                                      reply_markup=main_menu_keyboard())
        return

    if data == "menu:add":
        # Запускаем диалог добавления через редактирование сообщения
        context.user_data.clear()
        context.user_data["_adding"] = True
        await query.edit_message_text(
            "➕ *Новая задача*\n\nКак называется задача? Напиши кратко.",
            parse_mode="Markdown",
        )
        # Устанавливаем состояние через user_data
        context.user_data["_conv_state"] = TITLE
        return

    if data == "menu:list":
        tasks = [t for t in db_get_tasks() if t["stage"] != "done"]
        if not tasks:
            await query.edit_message_text(
                "Активных задач нет.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Добавить", callback_data="menu:add"),
                    InlineKeyboardButton("🏠 Меню",    callback_data="menu:main"),
                ]])
            )
            return
        await query.edit_message_text(
            f"*Активных задач: {len(tasks)}*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        for task in tasks:
            await query.message.reply_text(format_card(task), parse_mode="Markdown",
                                           reply_markup=task_buttons(task))
        return

    if data == "menu:board":
        tasks = [t for t in db_get_tasks() if t["stage"] != "done"]
        lines = ["*Доска задач*\n"]
        for stage in PIPELINE[:-1]:
            stage_tasks = [t for t in tasks if t["stage"] == stage]
            lines.append(f"{STAGE_LABEL[stage]} *({len(stage_tasks)})*")
            for t in stage_tasks:
                dl = f" ⏰{t['deadline']}" if t.get("deadline") else ""
                lines.append(f"  {PRIORITY_ICON[t['priority']]} `{t['id']}` {t['title'][:35]}{dl}")
            if not stage_tasks:
                lines.append("  —")
            lines.append("")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        return

    if data == "menu:done_list":
        tasks = [t for t in db_get_tasks() if t["stage"] == "done"]
        if not tasks:
            await query.edit_message_text(
                "Выполненных задач пока нет.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                ]])
            )
            return
        await query.edit_message_text(
            f"*Выполненных задач: {len(tasks)}*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        for task in tasks:
            await query.message.reply_text(format_card(task), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                ]])
            )
        return

    if data == "menu:overdue":
        today = datetime.now().date()
        tasks = []
        for t in db_get_tasks():
            if t["stage"] == "done" or not t.get("deadline"):
                continue
            parsed = parse_date(t["deadline"])
            if parsed:
                days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
                if days < 0:
                    tasks.append((days, t))
        if not tasks:
            await query.edit_message_text(
                "Просроченных задач нет. 🎉",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                ]])
            )
            return
        tasks.sort(key=lambda x: x[0])
        await query.edit_message_text(
            f"*Просроченных задач: {len(tasks)}*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        for _, task in tasks:
            await query.message.reply_text(format_card(task), parse_mode="Markdown",
                                           reply_markup=task_buttons(task))
        return

    if data == "menu:planned":
        today = datetime.now().date()
        tasks = []
        for t in db_get_tasks():
            if t["stage"] == "done" or not t.get("deadline"):
                continue
            parsed = parse_date(t["deadline"])
            if parsed:
                days = (datetime.strptime(parsed, "%d.%m.%Y").date() - today).days
                if days >= 0:
                    tasks.append((days, t))
        if not tasks:
            await query.edit_message_text(
                "Нет задач с дедлайном.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                ]])
            )
            return
        tasks.sort(key=lambda x: x[0])
        await query.edit_message_text(
            f"*Запланированных задач: {len(tasks)}*", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        for _, task in tasks:
            await query.message.reply_text(format_card(task), parse_mode="Markdown",
                                           reply_markup=task_buttons(task))
        return

    # ── Действия с задачами ───────────────────────────────────────────────────
    if ":" not in data:
        return

    action, task_id = data.split(":", 1)
    task = db_get_task(task_id)

    if not task:
        await query.edit_message_text(
            "Задача не найдена.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )
        return

    if action == "move":
        cur_idx = PIPELINE.index(task["stage"])
        if cur_idx < len(PIPELINE) - 1:
            new_stage = PIPELINE[cur_idx + 1]
            db_update_task(task_id, stage=new_stage)
            task = db_get_task(task_id)
            if task["stage"] == "done":
                await query.edit_message_text(
                    f"✅ *{task_id} завершена!*\n_{task['title']}_",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
                    ]])
                )
            else:
                await query.edit_message_text(
                    format_card(task), parse_mode="Markdown",
                    reply_markup=task_buttons(task),
                )

    elif action == "del":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, удалить", callback_data=f"delok:{task_id}"),
            InlineKeyboardButton("Нет",         callback_data=f"delno:{task_id}"),
        ]])
        await query.edit_message_text(
            f"Удалить задачу *{task_id}*?\n_{task['title']}_",
            parse_mode="Markdown", reply_markup=kb,
        )

    elif action == "delok":
        db_delete_task(task_id)
        await query.edit_message_text(
            f"🗑 Задача *{task_id}* удалена.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu:main")
            ]])
        )

    elif action == "delno":
        await query.edit_message_text(
            format_card(task), parse_mode="Markdown",
            reply_markup=task_buttons(task),
        )

# ── Text handler (for add via menu button) ────────────────────────────────────

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст когда пользователь добавляет задачу через кнопку меню."""
    state = context.user_data.get("_conv_state")
    if state == TITLE:
        context.user_data["title"] = update.message.text.strip()
        context.user_data["_conv_state"] = WHY
        await update.message.reply_text(
            "Опиши задачу подробнее — что нужно сделать и зачем?"
        )
    elif state == WHY:
        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("Описание обязательно. Напиши хотя бы пару слов.")
            return
        context.user_data["why"] = text
        context.user_data["_conv_state"] = SIZE
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("S — до 1ч",  callback_data="msize:S"),
            InlineKeyboardButton("M — 2–4ч",   callback_data="msize:M"),
            InlineKeyboardButton("L — полдня", callback_data="msize:L"),
        ]])
        await update.message.reply_text("Сколько времени займёт?", reply_markup=kb)
    elif state == DEADLINE:
        parsed = parse_date(update.message.text)
        if not parsed:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="mskip:deadline")]])
            await update.message.reply_text(
                "Не понял дату. Попробуй `25.05.2026`\n_(или нажми Пропустить)_",
                parse_mode="Markdown", reply_markup=kb,
            )
            return
        context.user_data["deadline"] = parsed
        await _menu_finish_add(update.message, context)


async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки во время добавления задачи через меню."""
    query = update.callback_query
    await query.answer()
    data  = query.data
    state = context.user_data.get("_conv_state")

    if data.startswith("msize:") and state == SIZE:
        context.user_data["size"] = data.split(":")[1]
        context.user_data["_conv_state"] = PRIORITY
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Высокий", callback_data="mpriority:high"),
            InlineKeyboardButton("🟡 Средний", callback_data="mpriority:medium"),
            InlineKeyboardButton("🟢 Низкий",  callback_data="mpriority:low"),
        ]])
        await query.edit_message_text("Приоритет?", reply_markup=kb)

    elif data.startswith("mpriority:") and state == PRIORITY:
        context.user_data["priority"] = data.split(":")[1]
        context.user_data["_conv_state"] = WHY
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Пропустить", callback_data="mskip:why")]])
        await query.edit_message_text(
            "Зачем эта задача? Напиши кратко.\n_(или нажми Пропустить)_",
            parse_mode="Markdown", reply_markup=kb,
        )

    elif data == "mskip:deadline" and state == DEADLINE:
        context.user_data["deadline"] = ""
        await _menu_finish_add(query.message, context, edit=True)


async def _menu_finish_add(msg, context, edit=False):
    ud      = context.user_data
    task_id = next_task_id()
    task    = {
        "id":       task_id,
        "title":    ud["title"],
        "type":     "task",
        "size":     ud["size"],
        "priority": ud["priority"],
        "stage":    "user-spec",
        "why":      ud.get("why", ""),
        "deadline": ud.get("deadline", ""),
        "created":  now(),
        "updated":  now(),
    }
    db_add_task(task)
    context.user_data.clear()

    text = f"✅ *Задача создана!*\n\n{format_card(task)}"
    if edit:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=task_buttons(task))
    else:
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=task_buttons(task))

# ── Reminder job ──────────────────────────────────────────────────────────────

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

    # ConversationHandler для /add команды
    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            TITLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            WHY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, add_why_text)],
            SIZE:     [CallbackQueryHandler(add_size,     pattern="^size:")],
            PRIORITY: [CallbackQueryHandler(add_priority, pattern="^priority:")],
            DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_deadline_text),
                CallbackQueryHandler(add_deadline_skip, pattern="^skip:deadline$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_add)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(conv)

    # Кнопки меню во время добавления задачи через меню
    app.add_handler(CallbackQueryHandler(menu_callback_handler,
                    pattern="^(msize:|mpriority:|mskip:)"))

    # Основной обработчик кнопок
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Текст во время добавления задачи через меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Напоминания каждый день в 9:00
    app.job_queue.run_daily(check_deadlines, time=dtime(9, 0))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
