# MALICE Telegram shop — no hoodie edition
# Python 3.13 recommended | pip install -r requirements.txt
import asyncio
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, WebAppInfo, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, MenuButtonWebApp,
)

# Configuration is read from the project-root .env file. Never commit .env.
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
load_dotenv(PROJECT_DIR / ".env")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
except ValueError as exc:
    raise RuntimeError("ADMIN_ID в .env должен быть числовым Telegram user ID.") from exc
WEBAPP_URL = os.getenv(
    "WEBAPP_URL", "https://malice-store.hara13271.workers.dev/"
).strip()
_db_override = os.getenv("MALICE_DB_PATH", "").strip()
DB_PATH = Path(_db_override).expanduser() if _db_override else BASE_DIR / "malice_v3.db"
if not DB_PATH.is_absolute():
    DB_PATH = (PROJECT_DIR / DB_PATH).resolve()

SEED_PRODUCTS = [
    (1, "Футболка MALICE", 1290, "Базовая футболка с логотипом MALICE", "👕"),
    (2, "Лонгслив MALICE", 1790, "Лонгслив с принтом MALICE", "🖤"),
    (4, "Кепка Mono", 990, "Кепка Mono", "🧢"),
    (5, "Стикерпак", 790, "Набор фирменных стикеров MALICE", "✨"),
]

dp = Dispatcher()
bot_carts: dict[int, dict[int, int]] = defaultdict(dict)

# Худи скрыт из каталога и запрещён для новых заказов. Запись в БД не удаляем,
# чтобы не портить историю старых заказов и не менять ID остальных товаров.
HIDDEN_PRODUCT_IDS = {3}

def is_hidden_product(row) -> bool:
    if row is None:
        return True
    try:
        pid = int(row["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        pid = -1
    name = _normalise_name(row["name"]) if "name" in row.keys() else ""
    return pid in HIDDEN_PRODUCT_IDS or "худи" in name or "hoodie" in name


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '', emoji TEXT NOT NULL DEFAULT '🛍️'
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Новый',
            created_at TEXT NOT NULL,
            order_group INTEGER,
            size TEXT NOT NULL DEFAULT ''
        )""")
        # Non-destructive schema upgrades for older MALICE databases.
        product_cols = {row[1] for row in db.execute("PRAGMA table_info(products)")}
        if "description" not in product_cols:
            db.execute("ALTER TABLE products ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "emoji" not in product_cols:
            db.execute("ALTER TABLE products ADD COLUMN emoji TEXT NOT NULL DEFAULT '🛍️'")

        # Keep one row per item internally; group items in a single checkout.
        order_cols = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
        if "quantity" not in order_cols:
            db.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
        if "total" not in order_cols:
            db.execute("ALTER TABLE orders ADD COLUMN total INTEGER NOT NULL DEFAULT 0")
        if "order_group" not in order_cols:
            db.execute("ALTER TABLE orders ADD COLUMN order_group INTEGER")
        if "size" not in order_cols:
            db.execute("ALTER TABLE orders ADD COLUMN size TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE orders SET total = price * quantity WHERE total = 0")
        # Older MALICE versions inserted each item as a separate order row.
        # Rows from one checkout share user_id and the same second-level timestamp.
        # Link those legacy rows under the smallest row ID without deleting data.
        db.execute("""
            UPDATE orders
            SET order_group = (
                SELECT MIN(o2.id) FROM orders o2
                WHERE o2.user_id = orders.user_id AND o2.created_at = orders.created_at
            )
            WHERE order_group IS NULL
              AND (SELECT COUNT(*) FROM orders o3
                   WHERE o3.user_id = orders.user_id AND o3.created_at = orders.created_at) > 1
        """)

        count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            db.executemany(
                "INSERT INTO products(id,name,price,description,emoji) VALUES(?,?,?,?,?)",
                SEED_PRODUCTS,
            )



def get_products():
    """Return only currently sellable products. Hidden products remain in DB for order history."""
    with connect() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(products)")}
        emoji = "emoji" if "emoji" in cols else "'🛍️' AS emoji"
        description = "description" if "description" in cols else "'' AS description"
        rows = db.execute(f"SELECT id,name,price,{description},{emoji} FROM products ORDER BY id").fetchall()
    return [row for row in rows if not is_hidden_product(row)]


def get_product(pid: int):
    """Find a sellable product; never return the removed hoodie."""
    if int(pid) in HIDDEN_PRODUCT_IDS:
        return None
    with connect() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(products)")}
        emoji = "emoji" if "emoji" in cols else "'🛍️' AS emoji"
        description = "description" if "description" in cols else "'' AS description"
        row = db.execute(f"SELECT id,name,price,{description},{emoji} FROM products WHERE id=?", (pid,)).fetchone()
    return None if is_hidden_product(row) else row


def _normalise_name(value: str) -> str:
    return " ".join(str(value).strip().casefold().replace("ё", "е").split())


def find_product_by_name(name: str):
    """Resolve exact product names and a small explicit set of legacy Mini App names."""
    cleaned = _normalise_name(name)
    if not cleaned:
        return None

    # The previous website sent these names without IDs. Map only known aliases;
    # price and product identity are always read from the server-side database.
    aliases = {
        "футболка malice": "Футболка MALICE",
        "лонгслив malice": "Лонгслив MALICE",
        "стикерпак": "Стикерпак",
        "набор фирменных наклеек malice": "Стикерпак",
        "набор фирменных стикеров malice": "Стикерпак",
        "набор наклеек malice": "Стикерпак",
        "набор наклеек": "Стикерпак",
        "кепка mono": "Кепка Mono",
        "кепка malice": "Кепка Mono",
    }
    target_name = aliases.get(cleaned, str(name).strip())
    target_cleaned = _normalise_name(target_name)
    with connect() as db:
        rows = db.execute("SELECT id,name,price FROM products").fetchall()
    matches = [r for r in rows if not is_hidden_product(r) and _normalise_name(r["name"]) == target_cleaned]
    if len(matches) == 1:
        return matches[0]
    # No fuzzy/substring guessing: unknown names must be fixed explicitly.
    return None


def parse_items(raw: str) -> list[tuple[int, int, str]]:
    """Parse Mini App JSON, keeping optional size. Prices always come from the DB."""
    if not raw or not raw.strip():
        raise ValueError("Пустые данные корзины")
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Некорректный JSON: {exc.msg}") from exc
    for _ in range(4):
        if isinstance(data, str):
            data = json.loads(data)
        else:
            break
    if isinstance(data, dict):
        found = False
        for key in ("items", "products", "cart", "order_items", "orderItems", "basket", "order", "data", "payload"):
            if key in data:
                data, found = data[key], True
                break
        if not found and data and all(str(k).isdigit() for k in data):
            data = [{"id": k, "quantity": v} for k, v in data.items()]
        elif not found and any(k in data for k in ("id", "product_id", "productId", "name", "product_name", "title")):
            data = [data]
        elif not found:
            raise ValueError(f"Не найден список items/cart. Поля: {', '.join(data.keys())}")
    if not isinstance(data, list) or not data:
        raise ValueError("Корзина пуста или имеет неверный формат")

    parsed: dict[tuple[int, str], int] = {}
    for entry in data:
        if isinstance(entry, (int, str)):
            pid_raw, qty_raw, name_raw, size_raw = entry, 1, None, ""
        elif isinstance(entry, dict):
            pid_raw = next((entry[k] for k in ("id", "product_id", "productId", "pid", "item_id", "sku") if entry.get(k) is not None), None)
            qty_raw = next((entry[k] for k in ("quantity", "qty", "count", "amount", "num") if entry.get(k) is not None), 1)
            name_raw = next((entry[k] for k in ("name", "product_name", "productName", "title") if entry.get(k)), None)
            size_raw = next((entry[k] for k in ("size", "selectedSize", "selected_size", "variant", "option") if entry.get(k)), "")
            nested = entry.get("product")
            if isinstance(nested, dict):
                if pid_raw is None:
                    pid_raw = next((nested[k] for k in ("id", "product_id", "productId") if nested.get(k) is not None), None)
                if name_raw is None:
                    name_raw = next((nested[k] for k in ("name", "title") if nested.get(k)), None)
                if not size_raw:
                    size_raw = next((nested[k] for k in ("size", "selectedSize", "selected_size") if nested.get(k)), "")
        else:
            raise ValueError("Позиция корзины имеет неподдерживаемый формат")
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Некорректное количество: {qty_raw!r}") from exc
        if qty < 1 or qty > 50:
            raise ValueError("Количество каждого товара должно быть от 1 до 50")
        size = _normalise_size(size_raw)
        product = None
        if pid_raw is not None:
            try:
                numeric_pid = int(pid_raw)
                if numeric_pid in HIDDEN_PRODUCT_IDS:
                    raise ValueError("Худи больше нет в каталоге MALICE. Обнови магазин и выбери другой товар.")
                product = get_product(numeric_pid)
            except ValueError as exc:
                if "больше нет в каталоге" in str(exc):
                    raise
                product = None
            except (TypeError, OverflowError):
                product = None
        if product is None and name_raw:
            product = find_product_by_name(str(name_raw))
        if product is None:
            raise ValueError(f"Товар не найден по ID {pid_raw!r} или названию {name_raw!r}. Проверь /products и каталог сайта.")
        key = (int(product["id"]), size)
        parsed[key] = parsed.get(key, 0) + qty
        if parsed[key] > 50:
            raise ValueError("Общее количество одного товара/размера не может превышать 50")
    return [(pid, qty, size) for (pid, size), qty in parsed.items()]


def _normalise_size(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "NA", "N/A", "NONE", "NO SIZE", "БЕЗ РАЗМЕРА", "НЕ УКАЗАН"}:
        return ""
    compact = re.sub(r"[^A-ZА-Я0-9]", "", text).replace("Ё", "Е")
    if compact in {"ONESIZE", "FREESIZE", "UNISIZE", "UNIVERSAL", "УНИВЕРСАЛЬНЫЙ"}:
        return "OS"
    if len(compact) > 4:
        compact = compact[:4]
    return compact


def parse_start_order(payload: str) -> list[tuple[int, int, str]]:
    """Read a compact, price-free order payload from the bot's own deep link."""
    if not payload.startswith("m1_") or len(payload) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        raise ValueError("Неверный формат ссылки заказа")
    chunks = payload[3:].split("_")
    if not chunks or len(chunks) > 10:
        raise ValueError("В заказе слишком много позиций")
    result: list[tuple[int, int, str]] = []
    for chunk in chunks:
        parts = chunk.split("-")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("Не удалось прочитать позицию заказа")
        pid, qty = int(parts[0]), int(parts[1])
        if pid < 1 or qty < 1 or qty > 50:
            raise ValueError("Неправильный ID или количество в заказе")
        result.append((pid, qty, _normalise_size(parts[2])))
    return result


def save_order(user_id: int, items: list[tuple]) -> tuple[list[str], int, int]:
    """Save all cart lines under one order number, preserving optional sizes."""
    lines, total = [], 0
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    normalized: list[tuple[int, int, str]] = []
    for item in items:
        if len(item) == 2:
            pid, qty = item
            size = ""
        elif len(item) >= 3:
            pid, qty, size = item[0], item[1], item[2]
        else:
            raise ValueError("Неверный формат позиции заказа")
        normalized.append((int(pid), int(qty), _normalise_size(size)))

    with connect() as db:
        resolved = []
        for pid, qty, size in normalized:
            row = db.execute("SELECT id,name,price FROM products WHERE id=?", (pid,)).fetchone()
            if pid in HIDDEN_PRODUCT_IDS or (row is not None and is_hidden_product(row)):
                raise ValueError("Худи больше нет в каталоге MALICE. Обнови магазин и выбери другой товар.")
            if row is None:
                raise ValueError(f"Товар ID {pid} отсутствует в базе")
            if qty < 1 or qty > 50:
                raise ValueError("Недопустимое количество")
            resolved.append((row, qty, size))
        if not resolved:
            raise ValueError("Корзина пуста")
        order_id = None
        for row, qty, size in resolved:
            unit_price = int(row["price"])
            subtotal = unit_price * qty
            cur = db.execute("""INSERT INTO orders
                (user_id,product_id,product_name,price,quantity,total,status,created_at,order_group,size)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (user_id, int(row["id"]), row["name"], unit_price, qty, subtotal,
                 "Новый", now, order_id, size))
            if order_id is None:
                order_id = int(cur.lastrowid)
                db.execute("UPDATE orders SET order_group=? WHERE id=?", (order_id, order_id))
            else:
                db.execute("UPDATE orders SET order_group=? WHERE id=?", (order_id, int(cur.lastrowid)))
            size_text = f" · размер {size}" if size and size != "OS" else ""
            lines.append(f"{row['name']}{size_text} × {qty} — {subtotal} ₽")
            total += subtotal
    return lines, total, int(order_id)


async def sync_open_app_button(api_bot: Bot, chat_id: int | None = None) -> None:
    """Set Telegram's header/menu Open App button to the current Cloudflare site.

    This is separate from the reply-keyboard button sent by /start.
    """
    try:
        await api_bot.set_chat_menu_button(
            chat_id=chat_id,
            menu_button=MenuButtonWebApp(
                text="Open App",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        )
        scope = f"для чата {chat_id}" if chat_id is not None else "по умолчанию для всех чатов"
        print(f"✅ Кнопка меню Mini App настроена ({scope}): {WEBAPP_URL}")
    except Exception as exc:
        # Keep the bot online even if Telegram does not allow the menu change.
        print(f"⚠️ Не удалось обновить кнопку Open App: {type(exc).__name__}: {exc}")


def webapp_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛍 Открыть магазин MALICE", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True, is_persistent=True,
    )


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Каталог бота", callback_data="catalog")],
        [InlineKeyboardButton(text="🧺 Корзина бота", callback_data="cart")],
        [InlineKeyboardButton(text="ℹ️ О проекте", callback_data="about")],
    ])


@dp.message(CommandStart())
async def start(message: Message):
    # Keep the header Open App menu button in sync with the published Cloudflare site.
    await sync_open_app_button(message.bot, message.chat.id)
    # Orders from the site deep-link checkout return as /start m1_<items>; works for Open App and chat-button launches.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("m1_"):
        try:
            items = parse_start_order(parts[1].strip())
            lines, total, order_id = save_order(message.from_user.id, items)
        except Exception as exc:
            print(f"[DEEPLINK ORDER] ERROR {type(exc).__name__}: {exc}")
            await message.answer("❌ Не удалось оформить заказ.\nПричина: " + str(exc)[:350] + "\n\nПроверь товары командой /products.")
            return
        confirmation = "✅ ЗАКАЗ ПРИНЯТ!\n\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nЗаказ: #{order_id}\nСтатус: Новый. Оплата пока не подключена."
        await message.answer(confirmation, reply_markup=webapp_keyboard())
        if ADMIN_ID:
            try:
                await message.bot.send_message(ADMIN_ID, "🛍 НОВЫЙ ЗАКАЗ MALICE\nПокупатель: " + message.from_user.full_name + f" (ID {message.from_user.id})\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nНомер заказа: #{order_id}")
            except Exception as exc:
                print(f"[ADMIN NOTIFY] {type(exc).__name__}: {exc}")
        return

    await message.answer("Обновляю кнопку магазина MALICE…", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        "Добро пожаловать в MALICE!\n\n"
        "Для оформления заказа нажми нижнюю кнопку «🛍 Открыть магазин MALICE». "
        "Если магазин открыт через Open App, кнопка оформления вернёт заказ в чат.",
        reply_markup=webapp_keyboard(),
    )
    await message.answer("Меню MALICE:", reply_markup=main_menu())


@dp.message(Command("shop"))
async def shop_cmd(message: Message):
    # Refresh the keyboard button without changing the database or site design.
    await message.answer(
        "Открой магазин нижней кнопкой ниже. Для отправки заказа используй именно её.",
        reply_markup=webapp_keyboard(),
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer("Команды:\n/start — обновить меню и кнопку магазина\n/shop — заново показать кнопку магазина\n/products — список доступных товаров (админ)\n/orders — последние заказы (админ)\n/new Название | Цена | Описание — добавить товар\n/delete ID — удалить товар\n\nХуди отключён и скрыт из каталога; старые заказы сохранены.")


@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("MALICE — магазин внутри Telegram. Оплата пока не подключена.", reply_markup=main_menu())


@dp.callback_query(F.data == "catalog")
async def catalog(callback: CallbackQuery):
    await callback.answer()
    rows = get_products()
    if not rows:
        await callback.message.answer("Каталог пуст. Добавь товары через /new.")
        return
    keyboard = [[InlineKeyboardButton(text=f"{r['name']} · {int(r['price'])} ₽", callback_data=f"detail:{int(r['id'])}")] for r in rows]
    keyboard.append([InlineKeyboardButton(text="← Меню", callback_data="home")])
    await callback.message.answer("КАТАЛОГ MALICE\nВыбери товар:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Меню MALICE:", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("detail:"))
async def detail(callback: CallbackQuery):
    await callback.answer()
    try:
        pid = int(callback.data.split(":", 1)[1])
    except ValueError:
        return
    row = get_product(pid)
    if not row:
        await callback.message.answer("Товар не найден. Обнови /products.")
        return
    await callback.message.answer(f"{row['emoji']} {row['name']}\nЦена: {row['price']} ₽\n{row['description']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data=f"add:{pid}")],
        [InlineKeyboardButton(text="🧺 Корзина", callback_data="cart")],
        [InlineKeyboardButton(text="← Каталог", callback_data="catalog")],
    ]))


async def send_bot_cart(message: Message, user_id: int):
    cart = bot_carts[user_id]
    lines = ["🧺 КОРЗИНА MALICE\n"]
    buttons = []
    total = 0
    for pid, qty in list(cart.items()):
        product = get_product(pid)
        if not product:
            cart.pop(pid, None)
            continue
        subtotal = int(product["price"]) * qty
        total += subtotal
        lines.append(f"{product['name']} × {qty} — {subtotal} ₽")
        buttons.append([
            InlineKeyboardButton(text=f"− {product['name']}", callback_data=f"remove:{pid}"),
            InlineKeyboardButton(text="+", callback_data=f"add:{pid}"),
        ])
    if total:
        lines.append(f"\nИтого: {total} ₽")
        buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="botcheckout")])
    else:
        lines.append("Корзина пока пустая.")
    buttons.extend([
        [InlineKeyboardButton(text="📦 Каталог", callback_data="catalog")],
        [InlineKeyboardButton(text="← Меню", callback_data="home")],
    ])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("add:"))
async def add_to_cart(callback: CallbackQuery):
    try:
        pid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID товара", show_alert=True)
        return
    if not get_product(pid):
        await callback.answer("Товар не найден", show_alert=True)
        return
    bot_carts[callback.from_user.id][pid] = bot_carts[callback.from_user.id].get(pid, 0) + 1
    await callback.answer("Добавлено в корзину")
    await send_bot_cart(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("remove:"))
async def remove_from_cart(callback: CallbackQuery):
    try:
        pid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID товара", show_alert=True)
        return
    cart = bot_carts[callback.from_user.id]
    if pid in cart:
        cart[pid] -= 1
        if cart[pid] <= 0:
            cart.pop(pid, None)
    await callback.answer("Количество обновлено")
    await send_bot_cart(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "cart")
async def bot_cart(callback: CallbackQuery):
    await callback.answer()
    await send_bot_cart(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "botcheckout")
async def bot_checkout(callback: CallbackQuery):
    cart = bot_carts[callback.from_user.id]
    if not cart:
        await callback.answer("Корзина пуста", show_alert=True)
        return
    try:
        lines, total, order_ids = save_order(callback.from_user.id, list(cart.items()))
    except Exception as exc:
        print(f"[BOT CART] order failed: {type(exc).__name__}: {exc}")
        await callback.answer("Не удалось сохранить заказ", show_alert=True)
        return
    cart.clear()
    await callback.answer("Заказ оформлен")
    await callback.message.answer("✅ ЗАКАЗ ПРИНЯТ!\n\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nЗаказ: #{order_ids}\nОплата пока не подключена.", reply_markup=main_menu())
    try:
        await callback.bot.send_message(ADMIN_ID, "🛍 НОВЫЙ ЗАКАЗ MALICE\nПокупатель: " + callback.from_user.full_name + f" (ID {callback.from_user.id})\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nНомер заказа: #{order_ids}")
    except Exception as exc:
        print(f"[ADMIN NOTIFY] {type(exc).__name__}: {exc}")


@dp.message(F.web_app_data)
async def receive_webapp_order(message: Message):
    raw = message.web_app_data.data or ""
    print(f"[MINI APP] user={message.from_user.id} payload={raw[:2000]!r}")
    try:
        items = parse_items(raw)
        lines, total, order_ids = save_order(message.from_user.id, items)
    except Exception as exc:
        print(f"[MINI APP] ERROR {type(exc).__name__}: {exc}")
        await message.answer("❌ Заказ не оформлен.\nПричина: " + str(exc)[:500] + "\n\nКаталог: /products")
        return
    await message.answer("✅ ЗАКАЗ ПРИНЯТ!\n\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nЗаказ: #{order_ids}\nСтатус: Новый. Оплата пока не подключена.")
    if ADMIN_ID:
        try:
            await message.bot.send_message(ADMIN_ID, "🛍 НОВЫЙ ЗАКАЗ MALICE\nПокупатель: " + message.from_user.full_name + f" (ID {message.from_user.id})\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽\nНомер заказа: #{order_ids}")
        except Exception as exc:
            print(f"[ADMIN NOTIFY] {type(exc).__name__}: {exc}")


@dp.message(Command("products"))
async def products_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Команда доступна администратору.")
        return
    rows = get_products()
    await message.answer("ТОВАРЫ MALICE:\n" + ("\n".join(f"ID {r['id']} · {r['name']} · {r['price']} ₽" for r in rows) if rows else "Каталог пуст."))


@dp.message(Command("orders"))
async def orders_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Команда доступна администратору.")
        return
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    if not rows:
        await message.answer("Пока нет заказов.")
        return

    # Combine line-item rows by order_group. Legacy single-item orders use their own id.
    groups: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        group_id = int(row["order_group"] or row["id"])
        groups[group_id].append(row)

    latest_groups = sorted(
        groups.items(), key=lambda pair: max(int(r["id"]) for r in pair[1]), reverse=True
    )[:20]
    output = ["ПОСЛЕДНИЕ ЗАКАЗЫ MALICE"]
    for order_id, order_rows in latest_groups:
        order_rows.sort(key=lambda r: int(r["id"]))
        first = order_rows[0]
        amount = sum(int(r["total"]) for r in order_rows)
        status = first["status"]
        items_text = "\n".join(
            f"  • {r['product_name']}{(' · размер ' + r['size']) if r['size'] and r['size'] != 'OS' else ''} × {r['quantity']} — {r['total']} ₽"
            for r in order_rows
        )
        output.append(
            f"Заказ #{order_id} · {status} · Итого: {amount} ₽\n"
            f"Покупатель: {first['user_id']} · {first['created_at']}\n{items_text}"
        )
    await message.answer("\n\n".join(output))


@dp.message(Command("new"))
async def new_product(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return
    parts = [p.strip() for p in message.text.partition(" ")[2].split("|")]
    if len(parts) == 3 and any(word in _normalise_name(parts[0]) for word in ("худи", "hoodie")):
        await message.answer("Худи удалён из ассортимента MALICE и больше не добавляется.")
        return
    if len(parts) != 3 or not parts[0] or not parts[1]:
        await message.answer("Формат: /new Название | Цена | Описание")
        return
    try:
        price = int(parts[1])
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("Цена должна быть целым неотрицательным числом.")
        return
    with connect() as db:
        cur = db.execute("INSERT INTO products(name,price,description,emoji) VALUES(?,?,?,'🛍️')", (parts[0], price, parts[2]))
    await message.answer(f"✅ Товар добавлен: {parts[0]} · {price} ₽ · ID {cur.lastrowid}. Для Mini App добавь его в index.html.")


@dp.message(Command("delete"))
async def delete_product(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /delete ID")
        return
    with connect() as db:
        cur = db.execute("DELETE FROM products WHERE id=?", (int(parts[1]),))
    await message.answer("✅ Удалён." if cur.rowcount else "Товар с таким ID не найден.")


async def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируй .env.example в .env и укажи токен BotFather.")
    if ADMIN_ID <= 0:
        raise RuntimeError("Не задан ADMIN_ID. Укажи свой числовой Telegram user ID в .env.")
    if not WEBAPP_URL.startswith("https://"):
        raise RuntimeError("WEBAPP_URL должен начинаться с https://")

    if not DB_PATH.is_file():
        print(f"ℹ️ Локальная база не найдена, будет создана новая: {DB_PATH}")
    init_db()
    print(f"🗄️ База MALICE: {DB_PATH}")
    bot = Bot(TOKEN)
    try:
        # Telegram API иногда отвечает медленно. Retry startup rather than
        # crashing immediately on a single aiohttp TimeoutError.
        connected = False
        for attempt in range(1, 6):
            try:
                await bot.delete_webhook(
                    drop_pending_updates=False,
                    request_timeout=90,
                )
                connected = True
                break
            except TelegramNetworkError as exc:
                if attempt == 5:
                    print("\n❌ Не удалось подключиться к Telegram API после 5 попыток.")
                    print("Это сетевой тайм-аут, а не ошибка корзины или базы данных.")
                    print("Проверь интернет, VPN/прокси и блокировку соединения для Python в брандмауэре Windows.")
                    print(f"Последняя ошибка: {exc}")
                    return
                delay = min(attempt * 5, 20)
                print(f"⚠️ Telegram API не ответил (попытка {attempt}/5). Повтор через {delay} сек.")
                await asyncio.sleep(delay)

        if not connected:
            return

        print(f"✅ MALICE подключился к Telegram. База данных: {DB_PATH}")
        print(f"Mini App URL (chat keyboard): {WEBAPP_URL}")
        await sync_open_app_button(bot)
        print("Худи скрыт из каталога; исторические записи сохранены.")
        print("Ожидание сообщений и заказов...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
