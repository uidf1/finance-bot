import asyncio
import logging
import os
from datetime import datetime
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN!")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

DB_PATH = "/app/data/finance.db"

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                trans_type TEXT,
                description TEXT,
                date TEXT
            )
        """
        )
        await db.commit()


class RecordState(StatesGroup):
    waiting_for_amount = State()
    waiting_for_date_stat = State()


def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➖ Расход"), KeyboardButton(text="➕ Доход")],
            [
                KeyboardButton(text="📊 Сверка за сегодня"),
                KeyboardButton(text="📅 Сверка за дату"),
            ],
        ],
        resize_keyboard=True,
    )


def cancel_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я помогу вести учёт доходов и расходов.\nВыбери действие на клавиатуре снизу 👇",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "cancel")
async def cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.")
    await callback.answer()


@dp.message(F.text.in_(["➖ Расход", "➕ Доход"]))
async def start_record(message: types.Message, state: FSMContext):
    trans_type = "expense" if message.text == "➖ Расход" else "income"
    await state.update_data(trans_type=trans_type)

    label = "расхода" if trans_type == "expense" else "дохода"
    await message.answer(
        f"Введи сумму {label} (можно добавить комментарий через пробел, например: `250 такси`):",
        reply_markup=cancel_inline_kb(),
        parse_mode="Markdown",
    )
    await state.set_state(RecordState.waiting_for_amount)


@dp.message(RecordState.waiting_for_amount)
async def process_amount(message: types.Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    raw_amount = parts[0].replace(",", ".")

    try:
        amount = float(raw_amount)
    except ValueError:
        await message.answer("Введи число! Например: `500` или `500.50 кофе`")
        return

    description = parts[1] if len(parts) > 1 else ""
    data = await state.get_data()
    trans_type = data["trans_type"]
    today_str = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, amount, trans_type, description, date) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, amount, trans_type, description, today_str),
        )
        await db.commit()

    icon = "🔴" if trans_type == "expense" else "🟢"
    title = "Расход" if trans_type == "expense" else "Доход"
    desc_str = f" ({description})" if description else ""

    await message.answer(
        f"{icon} **Записано!**\n{title}: {amount:.2f} руб.{desc_str}\nДата: {today_str}",
        reply_markup=main_kb(),
        parse_mode="Markdown",
    )
    await state.clear()


@dp.message(F.text == "📊 Сверка за сегодня")
async def stat_today(message: types.Message):
    today_str = datetime.now().strftime("%Y-%m-%d")
    await send_stats_for_date(message.from_user.id, today_str, message)


@dp.message(F.text == "📅 Сверка за дату")
async def ask_custom_date(message: types.Message, state: FSMContext):
    await message.answer(
        "Введи дату в формате `ГГГГ-ММ-ДД` (например, `2026-11-09`):",
        reply_markup=cancel_inline_kb(),
        parse_mode="Markdown",
    )
    await state.set_state(RecordState.waiting_for_date_stat)


@dp.message(RecordState.waiting_for_date_stat)
async def process_custom_date(message: types.Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await message.answer(
            "Неверный формат даты! Используй формат `ГГГГ-ММ-ДД`, например: `2026-11-09`"
        )
        return

    await send_stats_for_date(message.from_user.id, date_str, message)
    await state.clear()


async def send_stats_for_date(user_id: int, date_str: str, message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT amount, trans_type, description FROM transactions WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer(
            f"За дату **{date_str}** записей нет.", parse_mode="Markdown"
        )
        return

    total_income = sum(r[0] for r in rows if r[1] == "income")
    total_expense = sum(r[0] for r in rows if r[1] == "expense")
    net_result = total_income - total_expense

    history_lines = []
    for amount, trans_type, desc in rows:
        icon = "🟢 +" if trans_type == "income" else "🔴 -"
        comment = f" — _{desc}_" if desc else ""
        history_lines.append(f"{icon}{amount:.2f} руб.{comment}")

    history_str = "\n".join(history_lines)

    if net_result > 0:
        status = f"🟩 **В плюсе на {net_result:.2f} руб.**"
    elif net_result < 0:
        status = f"🟥 **В минусе на {abs(net_result):.2f} руб.**"
    else:
        status = "🟦 **В нуле (0.00 руб.)**"

    text = (
        f"📊 **Сверка за {date_str}:**\n\n"
        f"➕ **Доходы:** {total_income:.2f} руб.\n"
        f"➖ **Расходы:** {total_expense:.2f} руб.\n"
        f"──────────────────\n"
        f"Итог: {status}\n\n"
        f"📜 **История за день:**\n{history_str}"
    )

    await message.answer(text, parse_mode="Markdown", reply_markup=main_kb())


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
