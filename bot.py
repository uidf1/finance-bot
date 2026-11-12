import asyncio
import csv
import io
import logging
import os
from datetime import datetime, timedelta
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
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
    # создаем директорию и таблицу
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
    # обновленная клавиатура с новыми фичами
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➖ Расход"), KeyboardButton(text="➕ Доход")],
            [
                KeyboardButton(text="📊 Сверка за сегодня"),
                KeyboardButton(text="📅 Сверка за дату"),
            ],
            [KeyboardButton(text="📈 Отчеты"), KeyboardButton(text="↩️ Отменить последнюю")],
            [KeyboardButton(text="📥 Скачать CSV")]
        ],
        resize_keyboard=True,
    )


def cancel_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]
    )


def reports_inline_kb():
    # клавиатура выбора периода отчета
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="За текущую неделю", callback_data="report_week")],
            [InlineKeyboardButton(text="За текущий месяц", callback_data="report_month")],
            [InlineKeyboardButton(text="За текущий год", callback_data="report_year")]
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


# --- новые функции ниже ---

@dp.message(F.text == "📈 Отчеты")
async def show_reports_menu(message: types.Message):
    await message.answer("Выбери период для отчета:", reply_markup=reports_inline_kb())


@dp.callback_query(F.data.startswith("report_"))
async def process_report_callback(callback: types.CallbackQuery):
    period = callback.data.split("_")[1]
    now = datetime.now()
    user_id = callback.from_user.id

    # вычисляем даты начала и конца периода
    if period == "week":
        start_date = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        period_label = "текущую неделю"
    elif period == "month":
        start_date = now.strftime("%Y-%m-01")
        period_label = "текущий месяц"
    elif period == "year":
        start_date = now.strftime("%Y-01-01")
        period_label = "текущий год"
    else:
        return

    end_date = now.strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT amount, trans_type, description FROM transactions WHERE user_id = ? AND date >= ? AND date <= ?",
            (user_id, start_date, end_date),
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await callback.message.edit_text(f"За {period_label} записей нет.")
        return

    total_income = sum(r[0] for r in rows if r[1] == "income")
    total_expense = sum(r[0] for r in rows if r[1] == "expense")
    net_result = total_income - total_expense

    # анализируем топ категорий расходов по описанию
    expenses_dict = {}
    for r in rows:
        if r[1] == "expense" and r[2]:
            desc = r[2].strip().capitalize()
            expenses_dict[desc] = expenses_dict.get(desc, 0) + r[0]

    top_expenses_text = ""
    if expenses_dict:
        sorted_exp = sorted(expenses_dict.items(), key=lambda x: x[1], reverse=True)[:3]
        top_expenses_text = "\n\n🔥 **Топ-3 категорий расходов:**\n"
        for i, (desc, amount) in enumerate(sorted_exp, 1):
            top_expenses_text += f"{i}. {desc}: {amount:.2f} руб.\n"

    status = "🟩 Плюс" if net_result > 0 else ("🟥 Минус" if net_result < 0 else "🟦 Ноль")

    text = (
        f"📅 **Отчет за {period_label}:**\n"
        f"С {start_date} по {end_date}\n\n"
        f"➕ **Всего доходов:** {total_income:.2f} руб.\n"
        f"➖ **Всего расходов:** {total_expense:.2f} руб.\n"
        f"──────────────────\n"
        f"Итог: {status} ({net_result:.2f} руб.){top_expenses_text}"
    )

    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()


@dp.message(F.text == "↩️ Отменить последнюю")
async def undo_last_transaction(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        # находим последнюю транзакцию
        async with db.execute(
            "SELECT id, amount, trans_type, description FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await message.answer("Твоя история операций пуста. Нечего удалять.")
            return

        trans_id, amount, trans_type, desc = row
        
        # удаляем запись
        await db.execute("DELETE FROM transactions WHERE id = ?", (trans_id,))
        await db.commit()

    type_label = "Расход" if trans_type == "expense" else "Доход"
    desc_str = f" ({desc})" if desc else ""
    await message.answer(f"🗑 **Удалена последняя запись:**\n{type_label} на {amount:.2f} руб.{desc_str}", parse_mode="Markdown")


@dp.message(F.text == "📥 Скачать CSV")
async def export_csv(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT date, trans_type, amount, description FROM transactions WHERE user_id = ? ORDER BY date ASC, id ASC",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("В базе нет записей для выгрузки.")
        return

    # генерируем csv в памяти
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Дата', 'Тип', 'Сумма', 'Комментарий'])
    
    for row in rows:
        date, t_type, amount, desc = row
        t_type_ru = "Доход" if t_type == "income" else "Расход"
        writer.writerow([date, t_type_ru, amount, desc])

    csv_bytes = output.getvalue().encode('utf-8-sig') # utf-8-sig для правильного отображения кириллицы в excel
    
    document = BufferedInputFile(csv_bytes, filename=f"finance_export_{datetime.now().strftime('%Y%m%d')}.csv")
    await message.answer_document(document, caption="📊 Твоя история транзакций.")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
