import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape
from io import BytesIO
from pathlib import Path
from textwrap import wrap
from zoneinfo import ZoneInfo

import aiosqlite
import arabic_reshaper
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.fsm.strategy import FSMStrategy
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bidi.algorithm import get_display
from dotenv import load_dotenv
from openai import AsyncOpenAI
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
OWNER_ID = os.getenv("OWNER_ID", "").strip() or (
    str(next(iter(ADMIN_IDS))) if len(ADMIN_IDS) == 1 else ""
)
DB_PATH = os.getenv("DB_PATH", "amlak.db")
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "").strip() or None
VOICE_MODEL = os.getenv("VOICE_MODEL", "gpt-4o-mini-transcribe").strip()
VOICE_LIMIT_BYTES = 5_000_000
VOICE_LIMIT_SECONDS = 60
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini").strip()
TZ = ZoneInfo("Asia/Tehran")

dp = Dispatcher(fsm_strategy=FSMStrategy.USER_IN_CHAT, events_isolation=SimpleEventIsolation())
dp.message.filter(
    (F.chat.type == "private")
    | (F.chat.type.in_({"group", "supergroup"})
       & F.text.regexp(r"^/sldgroup(?:@[A-Za-z0-9_]+)?$"))
)
publication_lock = asyncio.Lock()

CATALOG = "🏘 ملک‌های موجود"
CONSULT = "📩 درخواست مشاور"
AGENCY = "🏢 پنل املاک همکار"
AGENCY_OFFER = "ثبت ملک دفتر همکار"
AGENCY_WANT = "ثبت تقاضای مشتری دفتر"
BUYER_PDF = "📄 PDF ملک‌های مناسب من"
FIND = "🔎 پیدا کردن ملک"
ADD = "🏠 فروشنده‌ام"
LEAD = "🔎 خریدارم"
MINE = "📋 ثبت‌های من"
MY_REQUESTS = "📨 پیگیری درخواست‌ها"
PARTNERSHIP = "🤝 مشارکت در ساخت"
REVIEW = "✅ بررسی آگهی‌ها"
INBOX = "📥 درخواست مشتریان"
OFFICE_VISIT = "📝 ثبت مراجعه"
DAILY_REPORT = "📄 گزارش مشتریان امروز"
VISIT_TYPES = ("خریدار", "فروشنده")
VISIT_SOURCES = ("حضوری", "تلفنی")
VISIT_STATUSES = ("در انتظار پیگیری", "پیگیری شد")
MENU = "↩️ منوی اصلی"
ANY = "همه موارد"
CITIES = ("شیراز", "صدرا")
SADRA_PHASES = ("صدرا فاز ۱", "صدرا فاز ۲")
SKIP_AREA = "بدون انتخاب محله"
DEALS = ("فروش", "رهن و اجاره", "پیش‌فروش")
TYPES = ("آپارتمان", "زمین", "مغازه", "ویلا", "اداری", "سایر")
STATUS = {"draft": "در انتظار بررسی", "scheduled": "زمان‌بندی‌شده",
          "published": "تأییدشده", "reviewed": "پاسخ دفتر ارسال شد",
          "rejected": "ردشده", "closed": "بسته‌شده"}


def keyboard(*rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
    )


def main_menu(user_id):
    rows = [(LEAD, ADD), (FIND, CATALOG), (BUYER_PDF,), (CONSULT, PARTNERSHIP), (AGENCY,), (MINE, MY_REQUESTS)]
    if is_admin(user_id):
        rows.extend([(REVIEW, INBOX), (OFFICE_VISIT, DAILY_REPORT)])
    return keyboard(*rows)


class SearchProperty(StatesGroup):
    deal = State()
    kind = State()
    city = State()
    area = State()
    budget = State()


class BuyerRequest(StatesGroup):
    deal = State()
    property_type = State()
    city = State()
    area = State()
    size = State()
    budget = State()
    description = State()
    viewing = State()
    phone = State()


class OwnerReply(StatesGroup):
    text = State()


class OfficeVisit(StatesGroup):
    kind = State()
    source = State()
    details = State()
    phone = State()
    status = State()


scheduler = AsyncIOScheduler(timezone=TZ)


class AddProperty(StatesGroup):
    deal_type = State()
    property_type = State()
    city = State()
    area = State()
    size = State()
    price = State()
    details = State()
    phone = State()


LOCATION_STATES = {
    form.city.state: form for form in (BuyerRequest, AddProperty, SearchProperty)
}


async def ask_location(message: Message, state: FSMContext, form):
    await state.set_state(form.city)
    extra = (CITIES[1], ANY) if form is SearchProperty else ()
    rows = [(CITIES[0], *SADRA_PHASES)]
    if extra:
        rows.append(extra)
    await message.answer("محدودهٔ ملک را انتخاب کنید.", reply_markup=keyboard(*rows, (MENU,)))


async def ask_neighborhood(message: Message, state: FSMContext, form):
    await state.set_state(form.area)
    await message.answer(
        with_voice_option("نام محله را وارد کنید، یا دکمهٔ پایین را بزنید."),
        reply_markup=keyboard((SKIP_AREA, MENU)),
    )


@dp.message(StateFilter(*LOCATION_STATES), F.text, ~F.text.startswith("/"))
async def select_location(message: Message, state: FSMContext):
    form = LOCATION_STATES[await state.get_state()]
    choice = message.text
    if choice in SADRA_PHASES:
        await state.update_data(city=CITIES[1], phase=SADRA_PHASES.index(choice) + 1)
    elif choice == CITIES[0] or (form is SearchProperty and choice in (CITIES[1], ANY)):
        await state.update_data(city=None if choice == ANY else choice, phase=None)
    else:
        await message.answer("محدوده را از دکمه‌ها انتخاب کنید.")
        return
    await ask_neighborhood(message, state, form)


def location_label(data: dict, neighborhood: str) -> str:
    phase = data.get("phase")
    city = SADRA_PHASES[phase - 1] if phase else data.get("city")
    return "، ".join(part for part in (city, neighborhood) if part)


def is_admin(user_id: int) -> bool:
    return str(user_id) == OWNER_ID


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            deal_type TEXT NOT NULL,
            property_type TEXT NOT NULL,
            area TEXT NOT NULL,
            size_m2 INTEGER,
            price_toman INTEGER,
            details TEXT,
            phone TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            scheduled_at TEXT,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            request_text TEXT NOT NULL,
            phone TEXT,
            replied_at TEXT,
            created_at TEXT NOT NULL
        )
        """)
        async with db.execute("PRAGMA table_info(leads)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "phone" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN phone TEXT")
        if "replied_at" not in columns:
            await db.execute("ALTER TABLE leads ADD COLUMN replied_at TEXT")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS office_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            details TEXT NOT NULL,
            phone TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS listing_group (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            chat_id INTEGER NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS listing_group_posts (
            property_id INTEGER PRIMARY KEY,
            posted_at TEXT
        )
        """)
        # Older releases approved listings for internal use only. Queue them
        # once for explicit approval before making the catalog public.
        async with db.execute("PRAGMA table_info(properties)") as cursor:
            property_columns = {row[1] for row in await cursor.fetchall()}
        if "catalog_version" not in property_columns:
            await db.commit()
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("PRAGMA table_info(properties)") as cursor:
                current = {row[1] for row in await cursor.fetchall()}
            if "catalog_version" not in current:
                await db.execute("ALTER TABLE properties ADD COLUMN catalog_version INTEGER DEFAULT 1")
                await db.execute("UPDATE properties SET status='draft', scheduled_at=NULL WHERE status IN ('published','scheduled')")
        await db.commit()


def toman_number(text: str):
    value = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:[,٬،][0-9]{3})+)", value.strip()):
        return None
    digits = re.sub(r"[,٬،]", "", value)
    return int(digits) if len(digits.strip()) <= 15 else None


def format_price(price):
    if not price:
        return "توافقی / ثبت نشده"
    return f"{price:,} تومان"


def mobile_number(text):
    digits = text.strip().translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )
    return digits if re.fullmatch(r"(?:0|\+98)9[0-9]{9}", digits) else None


def lead_phone_number(message: Message):
    contact = message.contact
    if contact and contact.user_id == message.from_user.id:
        number = contact.phone_number
        return mobile_number("+" + number if number.startswith("98") else number)
    return mobile_number(message.text or "")


def phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ارسال شمارهٔ خودم", request_contact=True)],
            [KeyboardButton(text=MENU)],
        ],
        resize_keyboard=True,
    )


def with_voice_option(prompt: str) -> str:
    if AI_API_KEY:
        return prompt + "\nبرای تبدیل ویس فارسی به متن، همین‌جا ویس بفرستید. صدا برای تبدیل به سرویس هوش مصنوعی ارسال می‌شود."
    return prompt


def reply_button(kind, record_id):
    buttons = [[InlineKeyboardButton(text="✉️ ارسال پاسخ", callback_data=f"ownerreply:{kind}:{record_id}")]]
    if kind == "seller":
        buttons.append([InlineKeyboardButton(text="تأیید برای فهرست خریداران", callback_data=f"approve:{record_id}"),
                        InlineKeyboardButton(text="رد", callback_data=f"reject:{record_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def notify_owner(bot, text, markup):
    try:
        await bot.send_message(int(OWNER_ID), text, reply_markup=markup)
    except TelegramAPIError:
        logging.warning("Could not notify the owner; submission is available in the bot inbox")


def property_text(row, *, admin=False):
    phone = row["phone"] if admin else None
    return (
        f"🏠 <b>آگهی شماره {row['id']}</b>\n"
        f"معامله: {escape(row['deal_type'])}\n"
        f"ملک: {escape(row['property_type'])}\n"
        f"منطقه: {escape(row['area'])}\n"
        f"متراژ: {row['size_m2'] or 'ثبت نشده'} متر\n"
        f"{'رهن / مبلغ ثبت‌شده' if row['deal_type'] == 'رهن و اجاره' else 'قیمت'}: {format_price(row['price_toman'])}\n"
        f"توضیحات: {escape(row['details'] or 'ندارد')}\n"
        f"تماس: {escape(phone or 'از طریق مشاور')}"
    )


async def get_property(property_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM properties WHERE id=?", (property_id,))
        return await cur.fetchone()


async def publish_property(bot: Bot, property_id: int, *, due_at=None):
    async with publication_lock:
        row = await get_property(property_id)
        if not row or row["status"] not in ("draft", "reviewed", "scheduled"):
            return False
        if due_at and (row["status"] != "scheduled" or row["scheduled_at"] > due_at):
            return False
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE properties SET status='published', scheduled_at=NULL WHERE id=?",
                (property_id,),
            )
            await db.commit()
        return True


async def publish_due_jobs(bot: Bot):
    now = datetime.now(TZ).isoformat(timespec="minutes")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM properties WHERE status='scheduled' AND scheduled_at<=?",
            (now,)
        )
        rows = await cur.fetchall()
    for row in rows:
        await publish_property(bot, row["id"], due_at=now)


@dp.message(Command("sldgroup"), F.chat.type.in_({"group", "supergroup"}))
async def connect_listing_group(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO listing_group (singleton, chat_id) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET chat_id=excluded.chat_id",
            (message.chat.id,),
        )
        await db.commit()
    await message.answer("گروه آگهی‌های SLD متصل شد. آگهی‌ها با کد ملک منتشر می‌شوند.")


async def send_listing_group_posts(bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        destination = await db.execute_fetchall("SELECT chat_id FROM listing_group")
        if not destination:
            return
        rows = await db.execute_fetchall("""
            SELECT p.id, p.deal_type, p.property_type, p.area, p.size_m2, p.price_toman
            FROM listing_group_posts AS posts
            JOIN properties AS p ON p.id = posts.property_id
            WHERE posts.posted_at IS NULL AND p.status NOT IN ('closed', 'rejected')
            ORDER BY p.id LIMIT 20
        """)
        if not rows:
            return
        try:
            username = (await bot.me()).username
            for row in rows:
                code = row["id"]
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="پیگیری از دفتر",
                        url=f"https://t.me/{username}?start=listing_{code}",
                    )
                ]])
                await bot.send_message(
                    destination[0]["chat_id"],
                    "🏠 " + "\n".join(public_lines(row))
                    + "\n\nبرای اطلاعات بیشتر یا هماهنگی بازدید، کد ملک را به شهر املاک پناهی اعلام کنید."
                    + "\nنشانی دفتر: صدرا، فاز ۲، بلوار پارسه، ساختمان افرا",
                    reply_markup=markup,
                )
                await db.execute(
                    "UPDATE listing_group_posts SET posted_at=? WHERE property_id=?",
                    (datetime.now(TZ).isoformat(timespec="seconds"), code),
                )
                await db.commit()
        except TelegramAPIError as exc:
            logging.warning("SLD group post deferred: %s", type(exc).__name__)


@dp.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^listing_[0-9]{1,15}$")))
async def inquire_group_listing(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    property_id = int(command.args.removeprefix("listing_"))
    async with aiosqlite.connect(DB_PATH) as db:
        available = await db.execute_fetchall("""
            SELECT 1 FROM listing_group_posts AS posts
            JOIN properties AS p ON p.id = posts.property_id
            WHERE p.id = ? AND posts.posted_at IS NOT NULL AND p.status NOT IN ('closed', 'rejected')
        """, (property_id,))
    if not available:
        await message.answer("این ملک در دسترس نیست.", reply_markup=main_menu(message.from_user.id))
        return
    await state.set_state(BuyerRequest.description)
    await state.update_data(category=f"پیگیری گروه SLD، کد ملک {property_id}")
    await message.answer(
        f"برای پیگیری ملک با کد {property_id}، درخواست خود را برای دفتر بنویسید.",
        reply_markup=keyboard((MENU,)),
    )


@dp.message(CommandStart())
@dp.message(F.text == MENU)
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "به شهر املاک پناهی خوش آمدید 🏠\nخدمات خرید، فروش و اجاره ملک در صدرا و شیراز",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("id"))
async def user_id(message: Message):
    await message.answer(f"شناسه تلگرام شما: {message.from_user.id}")


def public_lines(row):
    # Only structured fields are public. Legacy locations containing arbitrary
    # contact information are replaced with a general location label.
    area = row["area"].strip()
    prefix, separator, neighborhood = area.partition("، ")
    area_to_check = neighborhood if separator and prefix in (*CITIES, *SADRA_PHASES) else area
    if not re.fullmatch(r"[آ-غف-یۀءئؤأإ\u200c ]{1,70}(?: [۰-۹0-9]{1,2})?", area_to_check):
        area = "محدوده با هماهنگی دفتر"
    kind = row["property_type"] if row["property_type"] in TYPES else "ملک"
    deal = row["deal_type"] if row["deal_type"] in DEALS else "معامله"
    return [f"کد ملک {row['id']}  |  {kind}  |  {deal}", area,
            f"{row['size_m2'] or 'نامشخص'} متر مربع  |  {format_price(row['price_toman'])}"]


LOCATION_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩يك", "01234567890123456789یک"
)
PHASE_PATTERN = re.compile(r"فاز[\s‌]*(1|2|یک|دو|اول|دوم)(?![\w‌])")
PHASE_NUMBERS = {"1": 1, "2": 2, "یک": 1, "دو": 2, "اول": 1, "دوم": 2}


def location_matches(area, city, phase, neighborhood):
    area = area.translate(LOCATION_TRANSLATION).replace("\u200c", " ")
    place_words = re.findall(r"[^\s،,]+", area)
    if city and city not in place_words:
        return False
    if phase and not any(PHASE_NUMBERS[label] == phase for label in PHASE_PATTERN.findall(area)):
        return False
    terms = re.findall(r"[^\s،,]+", (neighborhood or "").translate(LOCATION_TRANSLATION).replace("\u200c", " "))
    return all(term in area for term in terms)


async def inventory(filters, before=None, limit=None):
    sql = """SELECT id, deal_type, property_type, area, size_m2, price_toman
             FROM properties WHERE status='published'"""
    args = []
    for field in ("deal_type", "property_type"):
        if filters.get(field):
            sql += f" AND instr({field}, ?) > 0"
            args.append(filters[field])
    if any(filters.get(key) for key in ("city", "phase", "area")):
        sql += " AND location_matches(area, ?, ?, ?)"
        args.extend((filters.get("city"), filters.get("phase"), str(filters.get("area") or "")))
    max_price = filters.get("max_price_toman")
    if type(max_price) is int and 0 < max_price <= 999_999_999_999_999:
        sql += " AND price_toman > 0 AND price_toman <= ?"
        args.append(max_price)
    min_size = filters.get("min_size_m2")
    if type(min_size) is int and 0 < min_size <= 100_000:
        sql += " AND size_m2 >= ?"
        args.append(min_size)
    keywords = filters.get("keywords")
    if isinstance(keywords, list) and not args:
        words = [str(word).strip() for word in keywords if len(str(word).strip()) >= 2][:6]
        if words:
            sql += " AND (" + " OR ".join(
                "(area LIKE ? OR property_type LIKE ? OR deal_type LIKE ? OR details LIKE ?)"
                for _ in words
            ) + ")"
            for word in words:
                args.extend([f"%{word}%"] * 4)
    if before:
        sql += " AND id < ?"
        args.append(before)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.create_function("location_matches", 4, location_matches, deterministic=True)
        return await db.execute_fetchall(sql, args)


async def catalog_page(message, before=None):
    rows = await inventory({}, before, 6)
    if not rows:
        await message.answer("فعلاً ملک تأییدشده‌ای در این صفحه موجود نیست.")
        return
    cards = ["\n".join(public_lines(row)) for row in rows[:5]]
    buttons = [[InlineKeyboardButton(text=f"بازدید ملک {r['id']}", callback_data=f"visit:{r['id']}")] for r in rows[:5]]
    if len(rows) > 5:
        buttons.append([InlineKeyboardButton(text="ملک‌های بعدی", callback_data=f"catalog:{rows[4]['id']}")])
    await message.answer("\n\n".join(cards), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.message(F.text == CATALOG)
async def open_catalog(message: Message, state: FSMContext):
    await state.clear()
    await catalog_page(message)


@dp.callback_query(F.data.regexp(r"^catalog:[0-9]{1,19}$"), F.message.chat.type == "private")
async def next_catalog(query: CallbackQuery):
    await query.answer()
    await catalog_page(query.message, int(query.data.split(":")[1]))


@dp.message(F.text == AGENCY)
async def agency_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("پنل همکاری دفاتر املاک | فایل یا تقاضای مشتری را ثبت کنید. دفتر پناهی بررسی می‌کند.",
                         reply_markup=keyboard((AGENCY_OFFER, AGENCY_WANT), (MINE, MENU)))


@dp.message(F.text == AGENCY_OFFER)
async def agency_offer(message: Message, state: FSMContext):
    await add_start(message, state)
    await state.update_data(agency=True)


@dp.message(F.text.in_({CONSULT, AGENCY_WANT, PARTNERSHIP}))
async def contact_office(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BuyerRequest.description)
    category, prompt = {
        CONSULT: ("مشاوره", "موضوع مشاوره را بنویسید."),
        AGENCY_WANT: ("تقاضای دفتر همکار", "نام دفتر و مشخصات ملک درخواستی مشتری را بنویسید."),
        PARTNERSHIP: ("مشارکت در ساخت", "شهر، محله، متراژ زمین و شرایط مدنظر برای مشارکت را بنویسید."),
    }[message.text]
    await state.update_data(category=category)
    await message.answer(with_voice_option(prompt),
                         reply_markup=keyboard((MENU,)))


def buyer_pdf(rows):
    output = BytesIO()
    font = "BuyerReport"
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    pdf = Canvas(output, pagesize=A4)
    for index, row in enumerate(rows):
        if index % 8 == 0:
            if index:
                pdf.showPage()
            pdf.setFont(font, 15)
            pdf.drawRightString(550, 795, pdf_rtl("شهر املاک پناهی | ملک‌های مطابق درخواست شما"))
            pdf.setFont(font, 10)
            pdf.drawRightString(550, 770, pdf_rtl(f"تعداد ملک: {len(rows)}   |   صفحه {index // 8 + 1}"))
        y = 725 - (index % 8) * 85
        for line in public_lines(row):
            pdf.setFont(font, 11)
            while pdf.stringWidth(pdf_rtl(line), font, 11) > 500 and len(line) > 1:
                line = line[:-2] + "…"
            pdf.drawRightString(550, y, pdf_rtl(line))
            y -= 18
    pdf.save()
    return output.getvalue()


@dp.message(F.text.in_({FIND, BUYER_PDF}))
async def start_find(message: Message, state: FSMContext):
    await state.clear()
    await state.update_data(wants_pdf=message.text == BUYER_PDF)
    await state.set_state(SearchProperty.deal)
    await message.answer("نوع معامله را انتخاب کنید.", reply_markup=keyboard(DEALS, (ANY, MENU)))


BUYER_DEAL_TO_LISTING = {"خرید": "فروش", "رهن و اجاره": "رهن و اجاره", "پیش‌خرید": "پیش‌فروش"}
BUYER_DEALS = tuple(BUYER_DEAL_TO_LISTING)


@dp.message(F.text == LEAD)
async def start_lead(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(BuyerRequest.deal)
    await message.answer("نوع معامله را انتخاب کنید.", reply_markup=keyboard(BUYER_DEALS, (MENU,)))


@dp.message(BuyerRequest.deal, F.text, ~F.text.startswith("/"))
async def lead_deal(message: Message, state: FSMContext):
    if message.text not in BUYER_DEALS:
        await message.answer("یکی از دکمه‌های نوع معامله را انتخاب کنید.")
        return
    await state.update_data(deal=message.text)
    await state.set_state(BuyerRequest.property_type)
    await message.answer("دنبال چه نوع ملکی هستید؟", reply_markup=keyboard(TYPES[:3], TYPES[3:], (MENU,)))


@dp.message(BuyerRequest.property_type, F.text, ~F.text.startswith("/"))
async def lead_property_type(message: Message, state: FSMContext):
    if message.text not in TYPES:
        await message.answer("یکی از دکمه‌های نوع ملک را انتخاب کنید.")
        return
    await state.update_data(property_type=message.text)
    await ask_location(message, state, BuyerRequest)


@dp.message(BuyerRequest.area, F.text, ~F.text.startswith("/"))
async def lead_area(message: Message, state: FSMContext):
    area = message.text.strip()
    if not 0 < len(area) <= 120:
        await message.answer("شهر و محله را در حداکثر ۱۲۰ نویسه بنویسید.")
        return
    await state.update_data(area="" if area == SKIP_AREA else area)
    await state.set_state(BuyerRequest.size)
    await message.answer("متراژ دلخواه را به متر مربع وارد کنید. اگر فرقی ندارد، صفر بفرستید.")


@dp.message(BuyerRequest.size, F.text, ~F.text.startswith("/"))
async def lead_size(message: Message, state: FSMContext):
    size = toman_number(message.text)
    if size is None or size > 100000:
        await message.answer("متراژ را به عدد بین صفر و ۱۰۰٬۰۰۰ وارد کنید.")
        return
    await state.update_data(size=size)
    await state.set_state(BuyerRequest.budget)
    data = await state.get_data()
    amount = "سقف مبلغ رهن" if data["deal"] == "رهن و اجاره" else "سقف بودجهٔ خرید"
    await message.answer(f"{amount} را به تومان وارد کنید. اگر مشخص نیست، صفر بفرستید.")


@dp.message(BuyerRequest.budget, F.text, ~F.text.startswith("/"))
async def lead_budget(message: Message, state: FSMContext):
    budget = toman_number(message.text)
    if budget is None:
        await message.answer("مبلغ کامل را به تومان وارد کنید. اگر مشخص نیست، صفر بفرستید.")
        return
    data = await state.get_data()
    size = f"{data['size']:,} متر مربع" if data["size"] else "فرقی ندارد"
    amount = "سقف رهن" if data["deal"] == "رهن و اجاره" else "سقف بودجه"
    budget_text = f"{budget:,} تومان" if budget else "مشخص نشده"
    location = location_label(data, data['area'])
    request = (
        f"{data['deal']} | {data['property_type']}\n"
        f"محدوده: {location}\n"
        f"متراژ: {size}\n"
        f"{amount}: {budget_text}"
    )
    await state.update_data(budget=budget)
    await message.answer("خلاصهٔ درخواست شما\n" + request)
    await ask_buyer_phone(message, state, request)


@dp.message(F.text.in_({MINE, REVIEW, INBOX}))
async def menu_action(message: Message, state: FSMContext):
    await state.clear()
    if message.text == MINE:
        await my_ads(message)
    elif message.text == REVIEW:
        await pending(message)
    else:
        await admin_leads(message)


@dp.message(F.text == OFFICE_VISIT)
async def start_office_visit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(OfficeVisit.kind)
    await message.answer("مراجعه مربوط به خریدار است یا فروشنده؟", reply_markup=keyboard(VISIT_TYPES, (MENU,)))


@dp.message(F.text == DAILY_REPORT)
async def send_daily_report(message: Message):
    if not is_admin(message.from_user.id):
        return
    today = datetime.now(TZ).date()
    records = await daily_records(today)
    pdf = await asyncio.to_thread(build_daily_report, today, records)
    await message.answer_document(
        BufferedInputFile(pdf, filename=f"shahr-amlak-{today.isoformat()}.pdf"),
        caption="گزارش مشتریان امروز به وقت تهران",
        protect_content=True,
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "فرمان‌ها:\n"
        "/add - ثبت آگهی\n"
        "/lead متن درخواست - ثبت درخواست مشتری\n"
        "/myads - آگهی‌های ثبت‌شده توسط شما\n"
        "/close شماره - بستن آگهی خودتان در ربات\n"
        "/leads - درخواست‌های مشتری (مدیر)\n"
        "فرمان‌های مدیر:\n"
        "/pending - آگهی‌های پیش‌نویس\n"
        "از «بررسی آگهی‌ها» و «درخواست مشتریان» می‌توانید به ثبت‌کننده پاسخ دهید.\n"
        "ثبت حضوری و تلفنی و دریافت PDF امروز از منوی مدیریت انجام می‌شود.\n"
        "/cancel - لغو فرم فعلی"
    )


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("فرم لغو شد.", reply_markup=main_menu(message.from_user.id))


@dp.message(OfficeVisit.kind, F.text, ~F.text.startswith("/"))
async def office_visit_kind(message: Message, state: FSMContext):
    if message.text not in VISIT_TYPES:
        await message.answer("«خریدار» یا «فروشنده» را انتخاب کنید.")
        return
    await state.update_data(kind=message.text)
    await state.set_state(OfficeVisit.source)
    await message.answer("روش مراجعه را انتخاب کنید.", reply_markup=keyboard(VISIT_SOURCES, (MENU,)))


@dp.message(OfficeVisit.source, F.text, ~F.text.startswith("/"))
async def office_visit_source(message: Message, state: FSMContext):
    if message.text not in VISIT_SOURCES:
        await message.answer("«حضوری» یا «تلفنی» را انتخاب کنید.")
        return
    await state.update_data(source=message.text)
    await state.set_state(OfficeVisit.details)
    await message.answer(
        "مشخصات ملک یا نیاز مشتری را در یک پیام بنویسید. نام مشتری، محله، متراژ و قیمت یا بودجه را در صورت دسترسی اضافه کنید.",
        reply_markup=keyboard((MENU,)),
    )


@dp.message(OfficeVisit.details, F.text, ~F.text.startswith("/"))
async def office_visit_details(message: Message, state: FSMContext):
    details = message.text.strip()
    if not 0 < len(details) <= 1400:
        await message.answer("مشخصات را در یک پیام حداکثر ۱۴۰۰ نویسه بفرستید.")
        return
    await state.update_data(details=details)
    await state.set_state(OfficeVisit.phone)
    await message.answer("شماره موبایل مشتری را با 09 یا +98 وارد کنید.")


@dp.message(OfficeVisit.phone, F.text, ~F.text.startswith("/"))
async def office_visit_phone(message: Message, state: FSMContext):
    phone = mobile_number(message.text)
    if not phone:
        await message.answer("شماره موبایل را با 09 یا +98 وارد کنید.")
        return
    await state.update_data(phone=phone)
    await state.set_state(OfficeVisit.status)
    await message.answer("وضعیت پیگیری را انتخاب کنید.", reply_markup=keyboard(VISIT_STATUSES, (MENU,)))


@dp.message(OfficeVisit.status, F.text, ~F.text.startswith("/"))
async def save_office_visit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text not in VISIT_STATUSES:
        await message.answer("یکی از وضعیت‌های پیگیری را انتخاب کنید.")
        return
    visit = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO office_visits (kind, source, details, phone, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (visit["kind"], visit["source"], visit["details"], visit["phone"], message.text,
             datetime.now(TZ).isoformat(timespec="minutes")),
        )
        visit_id = cursor.lastrowid
        await db.commit()
    await state.clear()
    await message.answer(
        f"مراجعهٔ شماره {visit_id} ثبت شد و در گزارش امروز می‌آید.",
        reply_markup=main_menu(message.from_user.id),
    )


async def daily_records(today):
    bounds = (today.isoformat(), (today + timedelta(days=1)).isoformat())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        async with db.execute("""
            SELECT 'خریدار' AS kind, 'تلگرام' AS source, id, created_at, phone,
                   request_text AS details, replied_at IS NULL AS pending,
                   CASE WHEN replied_at IS NULL THEN 'در انتظار بررسی' ELSE 'پاسخ ارسال شد' END AS status
            FROM leads WHERE created_at >= ? AND created_at < ?
            UNION ALL
            SELECT kind, source, id, created_at, phone, details, status = 'در انتظار پیگیری', status
            FROM office_visits WHERE created_at >= ? AND created_at < ?
        """, bounds * 2) as cursor:
            records = [dict(row) async for row in cursor]
        async with db.execute(
            "SELECT * FROM properties WHERE created_at >= ? AND created_at < ?", bounds
        ) as cursor:
            async for row in cursor:
                rent = "مبلغ رهن" if row["deal_type"] == "رهن و اجاره" else "قیمت"
                size = f"{row['size_m2']:,} متر" if row["size_m2"] else "ثبت نشده"
                details = (
                    f"{row['deal_type']} {row['property_type']}، {row['area']}، متراژ {size}، "
                    f"{rent}: {format_price(row['price_toman'])}\n{row['details'] or ''}"
                )
                records.append(dict(
                    kind="فروشنده", source="تلگرام", id=row["id"], created_at=row["created_at"],
                    phone=row["phone"], details=details, pending=row["status"] in ("draft", "scheduled"),
                    status=STATUS.get(row["status"], row["status"]),
                ))
    return sorted(records, key=lambda row: (row["created_at"], row["id"], row["source"]))


def pdf_rtl(text):
    text = "".join(char for char in text if char.isprintable() or char in "\u200c\u200d\u200e")
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def build_daily_report(today, records):
    font_dir = Path("/usr/share/fonts/truetype/dejavu")
    for name, filename in (("Report", "DejaVuSans.ttf"), ("ReportBold", "DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))

    buffer = BytesIO()
    pdf = Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Shahr Amlak - {today.isoformat()}")
    width, height = A4
    right, left = width - 42, 42
    buyers = [row for row in records if row["kind"] == "خریدار"]
    sellers = [row for row in records if row["kind"] == "فروشنده"]
    pending_count = sum(row["pending"] for row in records)
    page = 0

    def draw(text, y, *, size=11, bold=False, color="#183334"):
        pdf.setFont("ReportBold" if bold else "Report", size)
        pdf.setFillColor(HexColor(color))
        pdf.drawRightString(right, y, pdf_rtl(text))

    def new_page():
        nonlocal page
        if page:
            pdf.showPage()
        page += 1
        draw("شهر املاک پناهی", height - 43, bold=True, color="#08786D")
        draw("گزارش روزانهٔ مشتریان", height - 75, size=20, bold=True)
        draw(f"تاریخ گزارش: \u200e{today.isoformat()}\u200e   |   به وقت تهران", height - 98)
        pdf.setStrokeColor(HexColor("#B7D7D2"))
        pdf.line(left, height - 113, right, height - 113)
        draw(f"خریداران: {len(buyers)}   |   فروشندگان: {len(sellers)}   |   منتظر پیگیری: {pending_count}",
             height - 136, bold=True)
        draw(f"گزارش داخلی دفتر   |   صفحه {page}", 34, size=9, color="#536C6D")
        return height - 174

    y = new_page()
    for title, rows in (("خریداران", buyers), ("فروشندگان", sellers)):
        if y < 185:
            y = new_page()
        draw(f"{title}  ({len(rows)})", y, size=15, bold=True, color="#08786D")
        y -= 24
        if not rows:
            draw("امروز موردی ثبت نشده است.", y, color="#536C6D")
            y -= 32
        for row in rows:
            # Break long unspaced input before measuring the displayed text.
            lines = []
            for paragraph in row["details"].splitlines():
                line = ""
                for word in (part for token in paragraph.split() for part in wrap(token, 30)):
                    candidate = f"{line} {word}".strip()
                    if pdfmetrics.stringWidth(pdf_rtl(candidate), "Report", 11) > right - left:
                        lines.append(line)
                        line = word
                    else:
                        line = candidate
                if line:
                    lines.append(line)

            if y - min(len(lines), 3) * 17 < 108:
                y = new_page()
                draw(f"{title} (ادامه)", y, size=15, bold=True, color="#08786D")
                y -= 24
            clock = row["created_at"][11:16]
            draw(f"{clock}   |   {row['source']}   |   ثبت {row['id']}   |   {row['status']}", y, bold=True)
            phone = row["phone"] or "ثبت نشده"
            if phone.startswith("+"):
                # Force the plus sign to stay on the left of international numbers.
                phone = "\u200e" + phone
            draw(f"شماره تماس: {phone}", y - 19)
            y -= 37
            for line in lines:
                if y < 60:
                    y = new_page()
                    draw(f"{title}   |   ادامهٔ ثبت {row['id']}", y, bold=True, color="#08786D")
                    y -= 22
                draw(line, y)
                y -= 17
            pdf.setStrokeColor(HexColor("#DFEAE8"))
            pdf.line(left, y + 3, right, y + 3)
            y -= 19
        y -= 8
    pdf.save()
    return buffer.getvalue()


@dp.message(Command("add"))
@dp.message(F.text.in_({ADD, "🏠 ثبت ملک"}))
async def add_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddProperty.deal_type)
    await message.answer("نوع معامله را انتخاب کنید.", reply_markup=keyboard(DEALS, (MENU,)))


@dp.message(AddProperty.deal_type, ~F.text.startswith("/"))
async def add_deal(message: Message, state: FSMContext):
    if message.text not in DEALS:
        await message.answer("یکی از گزینه‌های معامله را انتخاب کنید.")
        return
    await state.update_data(deal_type=message.text)
    await state.set_state(AddProperty.property_type)
    await message.answer("نوع ملک را انتخاب کنید.", reply_markup=keyboard(TYPES[:3], TYPES[3:], (MENU,)))


@dp.message(AddProperty.property_type, ~F.text.startswith("/"))
async def add_type(message: Message, state: FSMContext):
    if message.text not in TYPES:
        await message.answer("یکی از گزینه‌های نوع ملک را انتخاب کنید.")
        return
    await state.update_data(property_type=message.text)
    await ask_location(message, state, AddProperty)


@dp.message(AddProperty.area, F.text, ~F.text.startswith("/"))
async def add_area(message: Message, state: FSMContext):
    if not (message.text or "").strip():
        await message.answer("نام محله و شهر را به صورت متن بفرستید.")
        return
    neighborhood = "" if message.text == SKIP_AREA else message.text.strip()[:95]
    await state.update_data(area=location_label(await state.get_data(), neighborhood))
    await state.set_state(AddProperty.size)
    await message.answer("متراژ را به عدد وارد کن. اگر مشخص نیست، 0 بنویس.")


@dp.message(AddProperty.size, ~F.text.startswith("/"))
async def add_size(message: Message, state: FSMContext):
    value = toman_number(message.text or "")
    if value is None or value > 100000:
        await message.answer("متراژ را به صورت عدد بین صفر و ۱۰۰٬۰۰۰ وارد کنید.")
        return
    await state.update_data(size_m2=value)
    await state.set_state(AddProperty.price)
    data = await state.get_data()
    amount = "مبلغ رهن" if data["deal_type"] == "رهن و اجاره" else "قیمت کل"
    await message.answer(f"{amount} را به تومان وارد کنید. برای مبلغ توافقی، صفر بفرستید.")


@dp.message(AddProperty.price, ~F.text.startswith("/"))
async def add_price(message: Message, state: FSMContext):
    value = toman_number(message.text or "")
    if value is None:
        await message.answer("مبلغ کامل را به تومان وارد کنید. برای قیمت توافقی، صفر بفرستید.")
        return
    await state.update_data(price_toman=value)
    await state.set_state(AddProperty.details)
    data = await state.get_data()
    extra = " مبلغ اجاره ماهانه را هم ذکر کنید." if data["deal_type"] == "رهن و اجاره" else ""
    await message.answer(with_voice_option("ویژگی‌های ملک و شرایط پرداخت را بنویسید. شماره تماس را در توضیحات نگذارید." + extra))


@dp.message(AddProperty.details, F.text, ~F.text.startswith("/"))
async def add_details(message: Message, state: FSMContext):
    if not (message.text or "").strip():
        await message.answer("توضیحات را به صورت متن بفرستید.")
        return
    await state.update_data(details=message.text.strip()[:1500])
    await state.set_state(AddProperty.phone)
    await message.answer("شماره موبایل را با 09 یا +98 وارد کنید یا دکمهٔ ارسال شماره را بزنید.", reply_markup=phone_keyboard())


@dp.message(AddProperty.phone, F.contact)
@dp.message(AddProperty.phone, F.text, ~F.text.startswith("/"))
async def add_phone(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = lead_phone_number(message)
    if not phone:
        await message.answer("شماره موبایل را با 09 یا +98 وارد کنید.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO properties
            (owner_user_id, deal_type, property_type, area, size_m2, price_toman,
             details, phone, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (
                message.from_user.id, data["deal_type"], data["property_type"],
                data["area"], data["size_m2"] or None, data["price_toman"] or None,
                (("دفتر همکار | " if data.get("agency") else "") + data["details"]), phone,
                datetime.now(TZ).isoformat(timespec="minutes")
            )
        )
        property_id = cur.lastrowid
        await db.execute("INSERT INTO listing_group_posts (property_id) VALUES (?)", (property_id,))
        await db.commit()
    await state.clear()
    await message.answer(
        f"ملک شماره {property_id} ثبت شد و در انتظار بررسی دفتر است.",
        reply_markup=main_menu(message.from_user.id)
    )
    await notify_owner(
        message.bot,
        ("🏢 دفتر همکار\n" if data.get("agency") else "") + f"🏠 ملک جدید شماره {property_id}\n"
        f"{data['deal_type']} | {data['property_type']} | {data['area']}\n"
        f"متراژ: {data['size_m2'] or 'ثبت نشده'}\n"
        f"مبلغ: {format_price(data['price_toman'])}\n"
        f"توضیحات: {data['details']}\nشماره فروشنده: {phone}",
        reply_button("seller", property_id),
    )


async def extract_search_filters(query: str) -> dict:
    """Optional AI parser. Falls back to keyword search if no API key or parsing fails."""
    if not AI_API_KEY:
        return {"keywords": [w for w in re.split(r"\s+", query) if len(w) >= 2 and not w.isdigit()]}
    try:
        client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": (
                    "You extract real-estate search filters from Persian user requests. "
                    "Return only valid JSON with keys keywords (array of short Persian search terms), "
                    "area (string or empty), property_type (string or empty), deal_type (string or empty), "
                    "max_price_toman (integer or null), min_size_m2 (integer or null). "
                    "Never invent values; use null/empty when not explicit. Convert Persian digits to numbers. "
                    "Amounts such as 8 billion toman mean 8000000000 toman."
                )},
                {"role": "user", "content": query}
            ],
            temperature=0
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        return data if isinstance(data, dict) else {"keywords": []}
    except Exception:
        logging.exception("AI search parsing failed; using keyword search")
        return {"keywords": [w for w in re.split(r"\s+", query) if len(w) >= 2 and not w.isdigit()]}


@dp.message(Command("search"))
async def search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await start_lead(message, state)
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await message.answer("عبارت جست‌وجو را بعد از فرمان بنویس.\nمثال: /search آپارتمان در شهرک گلستان تا ۸ میلیارد")
        return

    await state.clear()
    filters = await extract_search_filters(query)
    await show_results(message, filters)


async def show_results(message: Message, filters: dict):
    rows = await inventory(filters, limit=8)
    if not rows:
        await message.answer("ملکی با این مشخصات پیدا نشد. می‌توانید درخواست مشاور ثبت کنید.", reply_markup=main_menu(message.from_user.id))
        return
    await message.answer("جدیدترین ملک‌های مطابق جست‌وجو", reply_markup=main_menu(message.from_user.id))
    for row in rows:
        button = InlineKeyboardButton(text="درخواست بازدید", callback_data=f"visit:{row['id']}")
        await message.answer(
            "\n".join(public_lines(row)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
        )


@dp.message(Command("lead"))
async def lead(message: Message, state: FSMContext):
    request_text = (message.text or "").partition(" ")[2].strip()
    if not request_text:
        await start_lead(message, state)
        return
    await state.clear()
    await ask_buyer_phone(message, state, request_text)


async def ask_buyer_phone(message, state, request_text):
    await state.update_data(request_text=request_text[:1500])
    await state.set_state(BuyerRequest.phone)
    await message.answer(
        "شماره موبایل را با 09 یا +98 وارد کنید یا دکمهٔ ارسال شماره را بزنید.",
        reply_markup=phone_keyboard(),
    )


@dp.message(BuyerRequest.phone, F.contact)
@dp.message(BuyerRequest.phone, F.text, ~F.text.startswith("/"))
async def buyer_phone(message: Message, state: FSMContext):
    phone = lead_phone_number(message)
    if not phone:
        await message.answer("شماره موبایل را با 09 یا +98 وارد کنید.")
        return
    data = await state.get_data()
    request_text = data["request_text"]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO leads (user_id, username, request_text, phone, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                message.from_user.id, message.from_user.username, request_text, phone,
                datetime.now(TZ).isoformat(timespec="minutes")
            )
        )
        lead_id = cursor.lastrowid
        await db.commit()
    await state.clear()
    await message.answer(
        f"درخواست شماره {lead_id} ثبت شد. پاسخ دفتر در همین چت به شما می‌رسد.",
        reply_markup=main_menu(message.from_user.id),
    )
    category = data.get("category", "درخواست خریدار")
    await notify_owner(
        message.bot, f"📩 {category} #{lead_id}\n{request_text}\nشماره تماس: {phone}",
        reply_button("buyer", lead_id),
    )
    if data.get("deal"):
        await show_results(message, {
            "deal_type": BUYER_DEAL_TO_LISTING[data["deal"]],
            "property_type": data["property_type"],
            "area": data["area"],
            "city": data.get("city"),
            "phase": data.get("phase"),
            "min_size_m2": data["size"],
            "max_price_toman": data["budget"],
        })


@dp.message(F.text == MY_REQUESTS)
async def my_requests(message: Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT id, request_text, replied_at FROM leads WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        )
    lines = []
    for code, request, replied_at in rows:
        subject = request.splitlines()[0][:100]
        status = "پاسخ دفتر ارسال شد" if replied_at else "در انتظار پاسخ دفتر"
        lines.append(f"درخواست {code} | {subject} | {status}")
    await message.answer(
        "\n".join(lines) or "هنوز درخواستی ثبت نکرده‌اید.",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("myads"))
async def my_ads(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, property_type, area, status FROM properties WHERE owner_user_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,)
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer("هنوز آگهی‌ای ثبت نکرده‌ای.")
        return
    lines = ["آگهی‌های شما:"]
    for r in rows:
        lines.append(f"#{r['id']} | {r['property_type']} | {r['area']} | {STATUS.get(r['status'], r['status'])}")
    lines.append("برای بستن آگهی خودتان، شماره را بعد از /close بفرستید.")
    await message.answer("\n".join(lines), reply_markup=main_menu(message.from_user.id))


@dp.message(Command("pending"))
async def pending(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("این فرمان فقط برای مدیر فعال است.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM properties WHERE status IN ('draft','reviewed') "
            "ORDER BY (status='draft') DESC, id DESC LIMIT 20"
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer("آگهی پیش‌نویس نداریم.")
        return
    for row in rows:
        await message.answer(
            property_text(row, admin=True) + f"\nوضعیت: {STATUS[row['status']]}",
            parse_mode="HTML", reply_markup=reply_button("seller", row["id"]),
        )


@dp.callback_query(F.data.regexp(r"^(?:approve|reject):[0-9]{1,15}$"), F.message.chat.type == "private")
async def moderate_property(query: CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("این گزینه فقط برای مدیر فعال است.", show_alert=True)
        return
    action, raw_id = query.data.split(":")
    property_id = int(raw_id)
    if action == "approve":
        changed = await publish_property(query.bot, property_id)
    else:
        changed = await reject_property(property_id)
    await query.answer("وضعیت آگهی به‌روزرسانی شد." if changed else "وضعیت آگهی تغییر نکرد.", show_alert=True)
    if changed:
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ارسال پاسخ", callback_data=f"ownerreply:seller:{property_id}")]]))


@dp.message(Command("publish"))
async def publish_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("این فرمان فقط برای مدیر فعال است.")
        return
    arg = (message.text or "").partition(" ")[2].strip()
    if not (arg.isdecimal() and len(arg) <= 15):
        await message.answer("استفاده: /publish شناسه")
        return
    success = await publish_property(message.bot, int(arg))
    await message.answer("ملک به فهرست خریداران اضافه شد." if success else "آگهی قابل تأیید نیست.")


@dp.message(Command("schedule"))
async def schedule_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("این فرمان فقط برای مدیر فعال است.")
        return
    arg = (message.text or "").partition(" ")[2].strip()
    match = re.match(r"^(\d{1,15})\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$", arg)
    if not match:
        await message.answer("استفاده:\n/schedule 12 2026-11-01 18:30")
        return
    property_id, date_part, time_part = match.groups()
    try:
        scheduled = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await message.answer("تاریخ یا ساعت معتبر نیست.")
        return
    if scheduled <= datetime.now(TZ):
        await message.answer("زمان انتشار باید در آینده باشد.")
        return
    row = await get_property(int(property_id))
    if not row or row["status"] not in ("draft", "reviewed", "scheduled"):
        await message.answer("آگهی قابل زمان‌بندی نیست.")
        return
    async with publication_lock, aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE properties SET status='scheduled', scheduled_at=? WHERE id=? AND status IN ('draft','reviewed','scheduled')",
            (scheduled.isoformat(timespec="minutes"), int(property_id))
        )
        await db.commit()
    if not cursor.rowcount:
        await message.answer("آگهی قابل زمان‌بندی نیست.")
        return
    await message.answer(f"زمان‌بندی شد: آگهی {property_id} در {scheduled.strftime('%Y-%m-%d %H:%M')} به وقت تهران.")


@dp.message(SearchProperty.deal, ~F.text.startswith("/"))
async def find_deal(message: Message, state: FSMContext):
    if message.text not in (*DEALS, ANY):
        await message.answer("نوع معامله را از دکمه‌ها انتخاب کنید.")
        return
    await state.update_data(deal_type=None if message.text == ANY else message.text)
    await state.set_state(SearchProperty.kind)
    await message.answer("نوع ملک را انتخاب کنید.", reply_markup=keyboard(TYPES[:3], TYPES[3:], (ANY, MENU)))


@dp.message(SearchProperty.kind, ~F.text.startswith("/"))
async def find_kind(message: Message, state: FSMContext):
    if message.text not in (*TYPES, ANY):
        await message.answer("نوع ملک را از دکمه‌ها انتخاب کنید.")
        return
    await state.update_data(property_type=None if message.text == ANY else message.text)
    await ask_location(message, state, SearchProperty)


@dp.message(SearchProperty.area, F.text, ~F.text.startswith("/"))
async def find_area(message: Message, state: FSMContext):
    area = (message.text or "").strip()
    if not 0 < len(area) <= 120:
        await message.answer("نام شهر یا محله را بنویسید.")
        return
    await state.update_data(area=None if area == SKIP_AREA else area)
    await state.set_state(SearchProperty.budget)
    data = await state.get_data()
    if data["deal_type"] == "رهن و اجاره":
        prompt = "سقف مبلغ رهن را به تومان وارد کنید."
    elif data["deal_type"]:
        prompt = "سقف بودجه را به تومان وارد کنید."
    else:
        prompt = "برای مشاهده همه معاملات، دکمه «همه موارد» را بزنید. برای جست‌وجو با بودجه ابتدا نوع معامله را مشخص کنید."
    await message.answer(prompt, reply_markup=keyboard((ANY, MENU)))


@dp.message(SearchProperty.budget, ~F.text.startswith("/"))
async def find_budget(message: Message, state: FSMContext):
    filters = await state.get_data()
    if message.text != ANY:
        budget = toman_number(message.text or "")
        if not filters["deal_type"] or not budget:
            await message.answer("سقف بودجه را به تومان وارد کنید یا «همه موارد» را بزنید. بودجه فقط با انتخاب نوع معامله قابل اعمال است.")
            return
        filters["max_price_toman"] = budget
    await state.clear()
    if filters.get("wants_pdf"):
        rows = await inventory(filters)
        if not rows:
            await message.answer("ملک مطابق این مشخصات موجود نیست. می‌توانید درخواست مشاور ثبت کنید.", reply_markup=main_menu(message.from_user.id))
            return
        document = BufferedInputFile(await asyncio.to_thread(buyer_pdf, rows), filename="matching-properties.pdf")
        await message.answer_document(document, caption="ملک‌های مطابق انتخاب شما؛ برای هماهنگی، کد ملک را به دفتر بفرستید.",
                                      reply_markup=main_menu(message.from_user.id))
    else:
        await show_results(message, filters)


@dp.callback_query(F.data.regexp(r"^visit:[0-9]{1,15}$"), F.message.chat.type == "private")
async def request_visit(query: CallbackQuery, state: FSMContext):
    property_id = int(query.data.split(":")[1])
    row = await get_property(property_id)
    if not row or row["status"] != "published":
        await query.answer("این ملک در دسترس نیست.", show_alert=True)
        return
    await query.answer()
    await state.set_data({"property_id": property_id})
    await state.set_state(BuyerRequest.viewing)
    await query.message.answer(
        with_voice_option("زمان پیشنهادی بازدید را بفرستید. شماره تماس در مرحلهٔ بعد دریافت می‌شود."),
        reply_markup=keyboard((MENU,)),
    )


@dp.message(BuyerRequest.viewing, F.text, ~F.text.startswith("/"))
@dp.message(BuyerRequest.description, F.text, ~F.text.startswith("/"))
async def lead_reply(message: Message, state: FSMContext):
    description = (message.text or "").strip()
    if not 0 < len(description) <= 1400:
        await message.answer("درخواست را در یک پیام متنی حداکثر ۱۴۰۰ نویسه بفرستید.")
        return
    property_id = (await state.get_data()).get("property_id")
    if property_id:
        row = await get_property(property_id)
        if not row or row["status"] != "published":
            await state.clear()
            await message.answer("این ملک در دسترس نیست.", reply_markup=main_menu(message.from_user.id))
            return
        description = f"درخواست بازدید از ملک شماره {property_id}\n{description}"
    category = (await state.get_data()).get("category")
    if category:
        description = f"{category} | {description}"
    await ask_buyer_phone(message, state, description)


@dp.message(Command("leads"))
async def admin_leads(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("این فرمان فقط برای مدیر فعال است.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leads ORDER BY (replied_at IS NULL) DESC, id DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
    if not rows:
        await message.answer("درخواستی ثبت نشده است.")
        return
    for row in rows:
        status = "پاسخ ارسال شد" if row["replied_at"] else "در انتظار بررسی"
        await message.answer(
            f"درخواست {row['id']} | {status}\n{row['request_text']}\n"
            f"شماره خریدار: {row['phone'] or 'در فیلد جداگانه ثبت نشده'}",
            reply_markup=reply_button("buyer", row["id"]),
        )


@dp.callback_query(F.data.regexp(r"^ownerreply:(buyer|seller):[0-9]{1,19}$"), F.message.chat.type == "private")
async def prepare_owner_reply(query: CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("این گزینه فقط برای مدیر فعال است.", show_alert=True)
        return
    _, kind, raw_id = query.data.split(":")
    await query.answer()
    await state.set_data({"kind": kind, "record_id": int(raw_id)})
    await state.set_state(OwnerReply.text)
    label = "درخواست خریدار" if kind == "buyer" else "ملک فروشنده"
    await query.message.answer(
        f"پاسخ خود را برای {label} شماره {raw_id} بنویسید. "
        "با فرستادن متن، پیام به ثبت‌کننده ارسال می‌شود.",
        reply_markup=keyboard((MENU,)),
    )


@dp.message(OwnerReply.text, F.text, ~F.text.startswith("/"))
async def send_owner_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    answer = message.text.strip()
    if not 0 < len(answer) <= 3500:
        await message.answer("پاسخ را در یک پیام حداکثر ۳۵۰۰ نویسه بنویسید.")
        return
    data = await state.get_data()
    record_id = data["record_id"]
    is_buyer = data["kind"] == "buyer"
    select = "SELECT user_id FROM leads WHERE id=?" if is_buyer else "SELECT owner_user_id FROM properties WHERE id=?"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(select, (record_id,)) as cursor:
            record = await cursor.fetchone()
        if not record:
            await state.clear()
            await message.answer("این ثبت پیدا نشد.", reply_markup=main_menu(message.from_user.id))
            return
        label = "درخواست" if is_buyer else "ملک"
        try:
            await message.bot.send_message(record[0], f"پیام دفتر دربارهٔ {label} شماره {record_id}\n\n{answer}")
        except TelegramAPIError:
            await message.answer("پیام نرسید. می‌توانید دوباره تلاش کنید یا به منوی اصلی برگردید.")
            return
        if is_buyer:
            await db.execute(
                "UPDATE leads SET replied_at=? WHERE id=?",
                (datetime.now(TZ).isoformat(timespec="minutes"), record_id),
            )
        else:
            await db.execute("UPDATE properties SET status='reviewed' WHERE id=? AND status='draft'", (record_id,))
        await db.commit()
    await state.clear()
    await message.answer("پاسخ ارسال شد.", reply_markup=main_menu(message.from_user.id))


@dp.message(Command("reject", "close"))
async def close_property(message: Message):
    command, _, value = message.text.partition(" ")
    value = value.strip()
    rejecting = command.split("@", 1)[0] == "/reject"
    if rejecting and not is_admin(message.from_user.id):
        await message.answer("این فرمان فقط برای مدیر فعال است.")
        return
    if not (value.isdecimal() and len(value) <= 15):
        await message.answer(f"شماره آگهی را کنار فرمان بفرستید. نمونه {command} 12")
        return
    if rejecting:
        changed = await reject_property(int(value))
    else:
        async with publication_lock, aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "UPDATE properties SET status='closed', scheduled_at=NULL "
                "WHERE id=? AND owner_user_id=? AND status IN ('draft','reviewed','scheduled','published')",
                (int(value), message.from_user.id),
            )
            await db.commit()
            changed = bool(cursor.rowcount)
    await message.answer("وضعیت آگهی به‌روزرسانی شد." if changed else "آگهی قابل تغییر نیست.")


async def reject_property(property_id: int):
    async with publication_lock, aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE properties SET status='rejected', scheduled_at=NULL "
            "WHERE id=? AND status IN ('draft','reviewed','scheduled')",
            (property_id,),
        )
        await db.commit()
        return bool(cursor.rowcount)


VOICE_STEPS = {
    BuyerRequest.area.state: lead_area,
    BuyerRequest.description.state: lead_reply,
    BuyerRequest.viewing.state: lead_reply,
    AddProperty.area.state: add_area,
    SearchProperty.area.state: find_area,
    AddProperty.details.state: add_details,
}


@dp.message(F.voice)
async def receive_voice(message: Message, state: FSMContext):
    step = VOICE_STEPS.get(await state.get_state())
    if not AI_API_KEY or step is None:
        await message.answer("در این مرحله از دکمه‌ها یا ورودی متنی استفاده کنید.")
        return
    voice = message.voice
    if voice.duration > VOICE_LIMIT_SECONDS or not voice.file_size or voice.file_size > VOICE_LIMIT_BYTES:
        await message.answer("ویس باید حداکثر یک دقیقه و ۵ مگابایت باشد.")
        return
    try:
        async with asyncio.timeout(30):
            recording = await message.bot.download(voice)
            async with AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL, max_retries=0) as client:
                result = await client.audio.transcriptions.create(
                    file=("voice.ogg", recording.getvalue(), "audio/ogg"),
                    model=VOICE_MODEL,
                    language="fa",
                )
    except Exception as exc:
        logging.warning("Voice transcription unavailable: %s", type(exc).__name__)
        await message.answer("تبدیل ویس انجام نشد. دوباره ویس بفرستید یا پاسخ را تایپ کنید.")
        return
    transcript = result.text.strip()
    if not transcript:
        await message.answer("متنی از ویس تشخیص داده نشد. دوباره امتحان کنید یا پاسخ را تایپ کنید.")
        return
    await message.answer("متن تشخیص‌داده‌شده:\n" + transcript[:1500])
    await step(message.model_copy(update={"text": transcript}), state)


@dp.message()
async def unknown_message(message: Message):
    await message.answer("برای ادامه یکی از گزینه‌ها را انتخاب کنید.", reply_markup=main_menu(message.from_user.id))


async def main():
    if not OWNER_ID.isascii() or not OWNER_ID.isdecimal() or int(OWNER_ID) <= 0:
        raise RuntimeError("OWNER_ID یا ADMIN_IDS تک‌نفره را با شناسه عددی مدیر تنظیم کنید.")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN در فایل .env تنظیم نشده است.")
    await init_db()
    bot = Bot(BOT_TOKEN)
    await bot.set_my_commands([
        BotCommand(command="start", description="منوی اصلی"),
        BotCommand(command="help", description="راهنما"),
        BotCommand(command="id", description="شناسه تلگرام من"),
    ])
    scheduler.add_job(publish_due_jobs, "interval", seconds=30, args=[bot], id="publish_due")
    scheduler.add_job(send_listing_group_posts, "interval", seconds=15, args=[bot],
                      id="sld_group_posts", max_instances=1)
    scheduler.start()
    logging.info("Shahr Amlak bot is running. Commit: %s", os.getenv("RAILWAY_GIT_COMMIT_SHA", "local"))
    try:
        await dp.start_polling(bot, handle_as_tasks=True)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
