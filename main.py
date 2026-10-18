import asyncio
import html
import hmac
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    BotCommand,
    MenuButtonCommands,
)
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn


# ============================================================
# НАСТРОЙКИ
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1003983603104"))

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

CONTACT = os.getenv(
    "TELEGRAM_CONTACT",
    "viktoriia_assistent",
).lstrip("@")

FILES_DIR = BASE_DIR / "files"

PDF_PATH = FILES_DIR / "Инструкция по списанию долгов.pdf"
DOCX_PATH = FILES_DIR / "Шаблоны всех заявлений.docx"

DB_PATH = (
    BASE_DIR / os.getenv("DATABASE_PATH", "clients.db")
).resolve()

HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8001"))

if not BOT_TOKEN or BOT_TOKEN == "PASTE_TELEGRAM_BOT_TOKEN_HERE":
    raise RuntimeError("Заполните BOT_TOKEN в файле .env")

if (
    not ADMIN_PASSWORD
    or ADMIN_PASSWORD == "CHANGE_THIS_TO_A_LONG_RANDOM_PASSWORD"
):
    raise RuntimeError(
        "Замените ADMIN_PASSWORD в файле .env на надёжный пароль"
    )

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
security = HTTPBasic()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row

    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                city TEXT DEFAULT '',
                debt TEXT DEFAULT '',
                contact_method TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES clients(user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_user
            ON messages(user_id, id);

            CREATE INDEX IF NOT EXISTS idx_clients_last_seen
            ON clients(last_seen);
        """)

        # Миграция для уже существующей базы данных:
        # добавляем способ связи, если столбца ещё нет.
        columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(clients)"
            ).fetchall()
        }

        if "contact_method" not in columns:
            con.execute(
                "ALTER TABLE clients "
                "ADD COLUMN contact_method TEXT DEFAULT ''"
            )


def ensure_client(user_id, username=""):
    stamp = now()

    with db() as con:
        con.execute("""
            INSERT INTO clients (
                user_id, username, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = CASE
                    WHEN excluded.username != ''
                    THEN excluded.username
                    ELSE clients.username
                END,
                last_seen = excluded.last_seen
        """, (user_id, username or "", stamp, stamp))


def set_client(user_id, **fields):
    allowed = {
        "username",
        "name",
        "phone",
        "city",
        "debt",
        "contact_method",
        "status",
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not fields:
        return

    sql = ", ".join(f"{key}=?" for key in fields)

    with db() as con:
        con.execute(
            f"UPDATE clients SET {sql} WHERE user_id=?",
            (*fields.values(), user_id),
        )


def log_message(user_id, direction, text):
    if not text:
        return

    with db() as con:
        con.execute(
            """
            INSERT INTO messages (
                user_id, direction, text, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (user_id, direction, str(text)[:10000], now()),
        )


async def say(message: Message, user_id: int, text: str, **kwargs):
    log_message(user_id, "bot", text)
    return await message.answer(text, **kwargs)


async def callback_say(
    callback: CallbackQuery,
    text: str,
    **kwargs,
):
    log_message(callback.from_user.id, "bot", text)
    await callback.message.answer(text, **kwargs)
    await callback.answer()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Получить бесплатную консультацию",
                    callback_data="consult",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Получить инструкции",
                    callback_data="instructions",
                )
            ],
        ]
    )


def debt_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Долги больше 500 т.р",
                    callback_data="debt_more",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Долги меньше 500 т.р",
                    callback_data="debt_less",
                )
            ],
        ]
    )


def contact_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="По телефону",
                    callback_data="contact_phone",
                )
            ],
            [
                InlineKeyboardButton(
                    text="В Telegram",
                    callback_data="contact_telegram",
                )
            ],
            [
                InlineKeyboardButton(
                    text="В WhatsApp",
                    callback_data="contact_whatsapp",
                )
            ],
        ]
    )


def consultation_button():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Получить бесплатную консультацию",
                    callback_data="consult_direct",
                )
            ]
        ]
    )


def telegram_url():
    text = (
        "Здравствуйте👋🏻 Хочу получить бесплатную "
        "консультацию по списанию кредитов и долгов."
    )

    return (
        f"https://t.me/{CONTACT}"
        f"?text={quote(text)}"
    )


# ============================================================
# СОСТОЯНИЯ ОПРОСА
# ============================================================

class LeadForm(StatesGroup):
    phone = State()
    name = State()
    city = State()


# ============================================================
# УВЕДОМЛЕНИЯ В СУПЕРГРУППУ
# ============================================================

async def send_lead_to_group(user_id: int):
    """
    Отправляет сведения о заявке в супергруппу.
    """

    with db() as con:
        client = con.execute(
            "SELECT * FROM clients WHERE user_id=?",
            (user_id,),
        ).fetchone()

    if not client:
        return

    username = client["username"] or ""
    if username:
        username = "@" + username.lstrip("@")
    else:
        username = "не указан"

    notification = (
        "Клиент оставил заявку на консультацию!\n"
        f"Имя: {client['name'] or 'Не указано'}\n"
        f"Имя пользователя: {username}\n"
        f"Номер: {client['phone'] or 'Не указан'}\n"
        f"Город: {client['city'] or 'Не указан'}\n"
        f"Способ связи: "
        f"{client['contact_method'] or 'Не указан'}\n"
        f"Сумма долга: {client['debt'] or 'Не указана'}"
    )

    try:
        await bot.send_message(
            chat_id=GROUP_ID,
            text=notification,
        )
    except Exception as exc:
        print(
            "Не удалось отправить заявку в группу: "
            f"{exc}"
        )


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    user = message.from_user

    ensure_client(user.id, user.username)
    log_message(user.id, "user", "/start")

    text = (
        "Добрый день! Я Надежда, ассистент практикующего юриста.\n\n"
        "В этом боте вы можете оперативно получить инструкции, "
        "шаблоны писем и записаться на консультацию бесплатно.\n"
        "— нажмите «Получить инструкции» "
        "(иски, претензии, заявления).\n\n"
        "Если ситуация нестандартная и нужен живой разбор — "
        "нажмите «Получить консультацию».\n\n"
        "Выбирайте пункт сами, отвечу сразу.\n\n"
        "Отвечу в том же чате."
    )

    await say(
        message,
        user.id,
        text,
        reply_markup=main_keyboard(),
    )


# ============================================================
# НАЧАЛО КОНСУЛЬТАЦИИ: ВОПРОС О ДОЛГЕ
# ============================================================

@dp.callback_query(F.data == "consult")
async def consult(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user

    ensure_client(user.id, user.username)
    log_message(
        user.id,
        "user",
        "Нажал: Получить бесплатную консультацию",
    )

    set_client(
        user.id,
        status="consult_started",
        debt="",
        phone="",
        name="",
        city="",
        contact_method="",
    )

    await state.clear()

    text = (
        "Отлично. Я задам несколько коротких вопросов — "
        "и скажу, подходит ли вам банкротство.\n\n"
        "Вопрос 1: Укажите общую сумму кредитов — "
        "и просроченных, и тех, что вы пока платите по графику?"
    )

    await callback_say(
        callback,
        text,
        reply_markup=debt_keyboard(),
    )


# ============================================================
# КНОПКА КОНСУЛЬТАЦИИ ПОСЛЕ ОТПРАВКИ ИНСТРУКЦИЙ
# ============================================================

@dp.callback_query(F.data == "consult_direct")
async def consult_direct(
    callback: CallbackQuery,
    state: FSMContext,
):
    user = callback.from_user

    ensure_client(user.id, user.username)
    log_message(
        user.id,
        "user",
        "Нажал консультацию после получения инструкций",
    )

    set_client(
        user.id,
        status="choosing_contact",
        phone="",
        name="",
        city="",
        contact_method="",
    )

    await state.clear()

    text = (
        "Подскажите, как вам будет удобнее получить "
        "бесплатную консультацию, по телефону, в WhatsApp "
        "или Telegram?\n\n"
        "На консультации я разберу вашу ситуацию "
        "и подскажу варианты решения."
    )

    await callback_say(
        callback,
        text,
        reply_markup=contact_keyboard(),
    )


# ============================================================
# ОТВЕТ НА ВОПРОС О СУММЕ ДОЛГА
# ============================================================

@dp.callback_query(
    F.data.in_({"debt_more", "debt_less"})
)
async def debt_answer(callback: CallbackQuery):
    user = callback.from_user

    ensure_client(user.id, user.username)

    if callback.data == "debt_more":
        debt = "Больше 500 000 ₽"
    else:
        debt = "Меньше 500 000 ₽"

    log_message(user.id, "user", f"Ответ: {debt}")

    set_client(
        user.id,
        debt=debt,
        status="debt_answered",
    )

    text = (
        "Спасибо за ответ. Возможность законного списания "
        "зависит не только от суммы долга, но и от обстоятельств "
        "вашей ситуации.\n\n"
        "Подскажите, как вам будет удобнее получить бесплатную "
        "консультацию — по телефону, в WhatsApp или Telegram?\n\n"
        "На консультации я разберу вашу ситуацию и подберу "
        "возможные варианты решения."
    )

    await callback_say(
        callback,
        text,
        reply_markup=contact_keyboard(),
    )


# ============================================================
# ОТПРАВКА ИНСТРУКЦИЙ: PDF + DOCX
# ============================================================

@dp.callback_query(F.data == "instructions")
async def instructions(callback: CallbackQuery):
    user = callback.from_user

    ensure_client(user.id, user.username)
    log_message(
        user.id,
        "user",
        "Нажал: Получить инструкции",
    )

    # Отправляем PDF.
    if PDF_PATH.is_file():
        log_message(
            user.id,
            "bot",
            "Отправлена инструкция PDF",
        )

        await callback.message.answer_document(
            FSInputFile(
                str(PDF_PATH),
                filename="Инструкция по списанию долгов.pdf",
            ),
            caption="Инструкция по списанию долгов",
        )
    else:
        await callback.message.answer(
            "Не найден файл «Инструкция по списанию долгов.pdf». "
            "Пожалуйста, сообщите администратору."
        )

    # Отправляем DOCX с шаблонами.
    if DOCX_PATH.is_file():
        log_message(
            user.id,
            "bot",
            "Отправлены шаблоны заявлений DOCX",
        )

        await callback.message.answer_document(
            FSInputFile(
                str(DOCX_PATH),
                filename="Шаблоны всех заявлений.docx",
            ),
            caption="Шаблоны всех заявлений",
        )
    else:
        await callback.message.answer(
            "Не найден файл «Шаблоны всех заявлений.docx». "
            "Пожалуйста, сообщите администратору."
        )

    text = (
        "Остались вопросы? Запишись на бесплатную консультацию "
        "к профильному юристу!"
    )

    log_message(user.id, "bot", text)

    await callback.message.answer(
        text,
        reply_markup=consultation_button(),
    )

    await callback.answer()


# ============================================================
# ВЫБОР СПОСОБА СВЯЗИ
# ============================================================

async def begin_contact_collection(
    callback: CallbackQuery,
    state: FSMContext,
    method: str,
    phone_prompt: str,
):
    user = callback.from_user

    ensure_client(user.id, user.username)

    log_message(
        user.id,
        "user",
        f"Выбран способ связи: {method}",
    )

    set_client(
        user.id,
        contact_method=method,
        status="collecting_contact",
    )

    await state.update_data(contact_method=method)
    await state.set_state(LeadForm.phone)

    await callback_say(
        callback,
        phone_prompt,
    )


@dp.callback_query(F.data == "contact_phone")
async def phone_choice(
    callback: CallbackQuery,
    state: FSMContext,
):
    await begin_contact_collection(
        callback,
        state,
        method="По телефону",
        phone_prompt=(
            "Напишите, пожалуйста, свой контактный номер "
            "для связи, чтобы мы могли созвониться"
        ),
    )


@dp.callback_query(F.data == "contact_whatsapp")
async def whatsapp_choice(
    callback: CallbackQuery,
    state: FSMContext,
):
    await begin_contact_collection(
        callback,
        state,
        method="WhatsApp",
        phone_prompt=(
            "Напишите, пожалуйста, свой контактный номер "
            "для связи, чтобы мы могли написать вам в WhatsApp"
        ),
    )


@dp.callback_query(F.data == "contact_telegram")
async def telegram_choice(callback: CallbackQuery):
    user = callback.from_user

    ensure_client(user.id, user.username)

    log_message(
        user.id,
        "user",
        "Выбран способ связи: Telegram",
    )

    set_client(
        user.id,
        contact_method="Telegram",
        status="telegram_clicked",
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Написать в Telegram",
                    url=telegram_url(),
                )
            ]
        ]
    )

    await callback_say(
        callback,
        (
            "Нажмите кнопку ниже, чтобы перейти в личные сообщения "
            "и отправить подготовленное сообщение."
        ),
        reply_markup=keyboard,
    )

    # Отдельно сообщаем в группу о выборе Telegram.
    # Контакты и город будут пустыми, поскольку бот их не получил.
    await send_lead_to_group(user.id)


# ============================================================
# СБОР НОМЕРА ТЕЛЕФОНА
# ============================================================

@dp.message(LeadForm.phone)
async def collect_phone(
    message: Message,
    state: FSMContext,
):
    user = message.from_user

    ensure_client(user.id, user.username)

    phone = (message.text or "").strip()
    log_message(user.id, "user", phone)

    if len(phone) < 5:
        await say(
            message,
            user.id,
            "Пожалуйста, проверьте номер и отправьте его ещё раз.",
        )
        return

    data = await state.get_data()
    method = data.get("contact_method", "")

    set_client(
        user.id,
        phone=phone,
        contact_method=method,
    )

    await state.set_state(LeadForm.name)

    await say(
        message,
        user.id,
        "Напишите свое имя и фамилию",
    )


# ============================================================
# СБОР ИМЕНИ И ФАМИЛИИ
# ============================================================

@dp.message(LeadForm.name)
async def collect_name(
    message: Message,
    state: FSMContext,
):
    user = message.from_user

    ensure_client(user.id, user.username)

    name = (message.text or "").strip()
    log_message(user.id, "user", name)

    if len(name) < 2:
        await say(
            message,
            user.id,
            "Пожалуйста, напишите свое имя и фамилию.",
        )
        return

    set_client(user.id, name=name)

    await state.set_state(LeadForm.city)

    await say(
        message,
        user.id,
        "Напишите свой город проживания",
    )


# ============================================================
# СБОР ГОРОДА И ЗАВЕРШЕНИЕ ЗАЯВКИ
# ============================================================

@dp.message(LeadForm.city)
async def collect_city(
    message: Message,
    state: FSMContext,
):
    user = message.from_user

    ensure_client(user.id, user.username)

    city = (message.text or "").strip()
    log_message(user.id, "user", city)

    if len(city) < 2:
        await say(
            message,
            user.id,
            "Пожалуйста, укажите город проживания ещё раз.",
        )
        return

    set_client(
        user.id,
        city=city,
        status="consultation_lead",
    )

    # Отправляем полную заявку в супергруппу.
    await send_lead_to_group(user.id)

    await state.clear()

    await say(
        message,
        user.id,
        "Благодарим за обращение я свяжусь с вами в ближайшее время!",
    )


# ============================================================
# СОХРАНЕНИЕ ПРОЧИХ СООБЩЕНИЙ В ИСТОРИЮ CRM
# ============================================================

@dp.message()
async def log_other_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return

    user = message.from_user

    ensure_client(user.id, user.username)

    content = (
        message.text
        or message.caption
        or "[Сообщение без текста]"
    )

    log_message(user.id, "user", content)


# ============================================================
# CRM API — ДАШБОРД НЕ МЕНЯЕМ
# ============================================================

app = FastAPI(title="Nadezhda CRM")


def require_admin(
    credentials: HTTPBasicCredentials = Depends(HTTPBasic()),
):
    user_ok = hmac.compare_digest(
        credentials.username.encode(),
        ADMIN_USER.encode(),
    )

    pass_ok = hmac.compare_digest(
        credentials.password.encode(),
        ADMIN_PASSWORD.encode(),
    )

    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Требуется вход",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(require_admin)):
    path = BASE_DIR / "dashboard" / "index.html"

    if not path.is_file():
        raise HTTPException(
            500,
            "Не найден dashboard/index.html",
        )

    return path.read_text(encoding="utf-8")


@app.get("/api/stats")
def stats(_: str = Depends(require_admin)):
    month_ago = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()

    with db() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM clients"
        ).fetchone()[0]

        active = con.execute(
            "SELECT COUNT(*) FROM clients WHERE last_seen >= ?",
            (month_ago,),
        ).fetchone()[0]

        leads = con.execute(
            "SELECT COUNT(*) FROM clients "
            "WHERE status='consultation_lead'"
        ).fetchone()[0]

        telegram = con.execute(
            "SELECT COUNT(*) FROM clients "
            "WHERE status='telegram_clicked'"
        ).fetchone()[0]

    return {
        "total_clients": total,
        "active_month": active,
        "consultation_leads": leads,
        "telegram_clicks": telegram,
        "conversion_percent": (
            round(leads / active * 100, 1)
            if active
            else 0
        ),
    }


@app.get("/api/clients")
def clients(
    q: str = "",
    status: str = "",
    _: str = Depends(require_admin),
):
    query = """
        SELECT
            user_id,
            username,
            name,
            phone,
            city,
            debt,
            status,
            first_seen,
            last_seen
        FROM clients
        WHERE (
            CAST(user_id AS TEXT) LIKE ?
            OR name LIKE ?
            OR username LIKE ?
            OR phone LIKE ?
            OR city LIKE ?
        )
    """

    term = f"%{q}%"
    params = [term] * 5

    if status:
        query += " AND status=?"
        params.append(status)

    query += " ORDER BY last_seen DESC LIMIT 500"

    with db() as con:
        rows = con.execute(query, params).fetchall()

    return [dict(row) for row in rows]


@app.get("/api/clients/{user_id}/messages")
def client_messages(
    user_id: int,
    _: str = Depends(require_admin),
):
    with db() as con:
        exists = con.execute(
            "SELECT user_id FROM clients WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if not exists:
            raise HTTPException(
                404,
                "Клиент не найден",
            )

        rows = con.execute(
            """
            SELECT direction, text, created_at
            FROM messages
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT 200
            """,
            (user_id,),
        ).fetchall()

    return [dict(row) for row in reversed(rows)]


# ============================================================
# ЗАПУСК БОТА И CRM
# ============================================================

async def setup_menu():
    """
    Добавляет кнопку «Меню» слева от поля ввода
    с командой /start - Начать.
    """
    await bot.set_my_commands(
        [BotCommand(command="start", description="Начать")]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def run_bot():
    await setup_menu()
    await dp.start_polling(bot)


async def run_server():
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )

    try:
        await uvicorn.Server(config).serve()
    except (SystemExit, OSError) as error:
        # Если порт занят (например, хостингом), дашборд просто
        # не запускается, а бот продолжает работать.
        print(
            f"CRM-дашборд не запущен на {HOST}:{PORT}: {error!r}. "
            "Бот продолжает работу."
        )


async def main():
    init_db()

    await asyncio.gather(
        run_bot(),
        run_server(),
    )


if __name__ == "__main__":
    asyncio.run(main())
