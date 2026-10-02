import os
import json
import time
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, request, send_from_directory, abort, jsonify
import telebot
from telebot import types
import cloudinary
import cloudinary.uploader

try:
    from flask_compress import Compress
except ImportError:  # сжатие необязательно, но сильно ускоряет загрузку
    Compress = None

import db  # Neon (PostgreSQL): товары, заказы, вопросы, промокоды

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("makeup_and_care")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

# Render автоматически прокидывает RENDER_EXTERNAL_URL для web-сервисов.
# Можно переопределить вручную через WEBHOOK_URL, если нужно.
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Пароль для входа в админ-панель (/admin). Задаётся в переменных окружения.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Telegram ID администратора(ов). Только этим пользователям бот присылает
# ссылку на админ-панель по команде /start. Можно указать несколько ID
# через запятую: "111111,222222".
ADMIN_CHAT_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_CHAT_ID", "").split(",")
    if x.strip().lstrip("-").isdigit()
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Cloudinary: фото товаров. Достаточно переменной CLOUDINARY_URL
# (cloudinary://API_KEY:API_SECRET@CLOUD_NAME) — SDK читает её сам.
# Либо три отдельные: CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET.
if os.environ.get("CLOUDINARY_CLOUD_NAME"):
    cloudinary.config(
        cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
        api_key=os.environ.get("CLOUDINARY_API_KEY"),
        api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
        secure=True,
    )
else:
    cloudinary.config(secure=True)
if not cloudinary.config().cloud_name:
    raise RuntimeError("Cloudinary не настроен: задайте CLOUDINARY_URL или CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET")
CLOUDINARY_FOLDER = os.environ.get("CLOUDINARY_FOLDER", "makeup-care/products")

db.init_db()

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_BADGES = {"hit", "trend", "sale", "instock"}
ALLOWED_PROMO_TYPES = {"percent", "fixed"}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 МБ на файл
app.config["COMPRESS_MIMETYPES"] = ["text/html", "text/css", "application/json", "application/javascript"]
app.config["COMPRESS_MIN_SIZE"] = 500
if Compress:
    Compress(app)

# Отправка сообщений в Telegram — в фоне, чтобы заказ/вопрос не ждали ответа Telegram API
_bg = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tg-send")


def _safe_send(chat_id, text, what="сообщение"):
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        log.warning("Не удалось отправить %s (%s): %s", what, chat_id, e)


def send_async(chat_id, text, what="сообщение"):
    _bg.submit(_safe_send, chat_id, text, what)


# Кэш каталога в памяти: products.json читается из БД не чаще раза в CATALOG_TTL секунд
CATALOG_TTL = int(os.environ.get("CATALOG_TTL", "30"))
_catalog = {"body": None, "etag": None, "at": 0.0}
_catalog_lock = threading.Lock()


def invalidate_catalog():
    with _catalog_lock:
        _catalog["body"] = None
        _catalog["at"] = 0.0


def get_catalog():
    now = time.time()
    if _catalog["body"] is not None and now - _catalog["at"] < CATALOG_TTL:
        return _catalog["body"], _catalog["etag"]
    with _catalog_lock:
        if _catalog["body"] is not None and time.time() - _catalog["at"] < CATALOG_TTL:
            return _catalog["body"], _catalog["etag"]
        body = json.dumps(db.fetch("products"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _catalog["body"] = body
        _catalog["etag"] = '"' + hashlib.md5(body).hexdigest() + '"'
        _catalog["at"] = time.time()
        return body, _catalog["etag"]


# --- Вспомогательные функции для товаров и авторизации админа -----------

def require_admin():
    if not ADMIN_PASSWORD:
        abort(403, description="ADMIN_PASSWORD не задан на сервере")
    supplied = request.headers.get("X-Admin-Password", "")
    if supplied != ADMIN_PASSWORD:
        abort(401, description="Неверный пароль")


def fmt_price(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def sanitize_images(raw):
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if isinstance(u, (str,)) and str(u).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def sanitize_badge(raw):
    b = str(raw or "").strip().lower()
    return b if b in ALLOWED_BADGES else None


def sanitize_old_price(raw):
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# --- Промокоды -----------------------------------------------------------

def sanitize_positive_int(raw):
    """Возвращает положительное целое либо None, если значение не задано/некорректно."""
    if raw in (None, ""):
        return None
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def find_promo_by_code(code):
    code_norm = (code or "").strip().upper()
    if not code_norm:
        return None
    return db.promo_by_code(code_norm)


def validate_promo_for_use(promo, subtotal, customer_chat_id=None):
    """Проверяет, что промокод можно применить к заказу на сумму subtotal.
    Возвращает текст ошибки либо None, если всё в порядке."""
    if not promo:
        return "Промокод не найден"
    if not promo.get("active", True):
        return "Промокод больше не активен"
    max_uses = promo.get("max_uses")
    if max_uses is not None and int(promo.get("used_count") or 0) >= int(max_uses):
        return "Лимит использований промокода исчерпан"
    min_total = promo.get("min_total")
    if min_total is not None and subtotal < int(min_total):
        return f"Промокод действует от суммы заказа {fmt_price(min_total)}"
    if customer_chat_id and has_customer_used_promo(promo.get("code"), customer_chat_id):
        return "Вы уже использовали этот промокод"
    return None


def has_customer_used_promo(promo_code, customer_chat_id):
    """Проверяет, применял ли этот покупатель данный промокод ранее
    (отменённые заказы не считаются использованием)."""
    if not promo_code or not customer_chat_id:
        return False
    return db.customer_used_promo(promo_code, customer_chat_id)


def compute_promo_discount(promo, subtotal):
    if promo.get("type") == "percent":
        discount = subtotal * float(promo.get("value") or 0) / 100.0
    else:
        discount = float(promo.get("value") or 0)
    discount = max(0.0, min(discount, subtotal))
    return int(round(discount))


# --- Telegram handlers -------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message):
    if not BASE_URL:
        bot.send_message(
            message.chat.id,
            "Makeup&Care: мини-апп ещё не сконфигурирован (не задан WEBHOOK_URL)."
        )
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            text="💄 Открыть Makeup&Care",
            web_app=types.WebAppInfo(url=BASE_URL + "/")
        )
    )

    is_admin = bool(message.from_user) and message.from_user.id in ADMIN_CHAT_IDS
    if is_admin:
        keyboard.add(
            types.InlineKeyboardButton(
                text="Панель администратора",
                web_app=types.WebAppInfo(url=BASE_URL + "/admin")
            )
        )

    bot.send_message(
        message.chat.id,
        "💄 *Makeup&Care — Beauty Store*\n\n"
        "Добро пожаловать в Makeup&Care! Оригинальная косметика и уход из США "
        "и Европы: редкие позиции, новинки и возможность индивидуального заказа.\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог, проверить наличие и оформить заказ.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )



@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message: types.Message):
    cmd_start(message)


# --- Flask routes --------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    resp = send_from_directory(APP_DIR, "index.html", max_age=0, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"  # браузер проверяет ETag и получает 304 без загрузки файла
    return resp


@app.route("/products.json", methods=["GET"])
def products():
    body, etag = get_catalog()
    if request.headers.get("If-None-Match") == etag:
        resp = app.response_class(status=304)
    else:
        resp = app.response_class(body, mimetype="application/json")
    resp.headers["ETag"] = etag
    # короткий кэш + показ устаревшего, пока обновляется в фоне
    resp.headers["Cache-Control"] = "public, max-age=20, stale-while-revalidate=300"
    return resp


@app.route("/api/order", methods=["POST"])
def create_order():
    data = request.get_json(force=True, silent=True) or {}

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    comment = (data.get("comment") or "").strip()
    items = data.get("items") or []

    if not name or not phone:
        abort(400, description="Укажите имя и телефон")
    if not isinstance(items, list) or not items:
        abort(400, description="Корзина пуста")

    subtotal = sum(int(i.get("price") or 0) * int(i.get("qty") or 1) for i in items)

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    promo_code_raw = (data.get("promo_code") or "").strip()
    promo = None
    discount = 0
    if promo_code_raw:
        promo = find_promo_by_code(promo_code_raw)
        promo_err = validate_promo_for_use(promo, subtotal, customer_chat_id)
        if promo_err:
            abort(400, description=promo_err)
        discount = compute_promo_discount(promo, subtotal)

    total = subtotal - discount

    order = db.insert("orders", {
        "name": name,
        "phone": phone,
        "comment": comment,
        "items": items,
        "subtotal": subtotal,
        "discount": discount,
        "promo_code": promo.get("code") if promo else None,
        "total": total,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
    })
    new_id = order["id"]

    if promo:
        db.promo_increment(promo["id"])

    items_lines = "\n".join(
        f"· {i.get('name','—')} (размер {i.get('size') or '—'}) × {i.get('qty', 1)}"
        for i in items
    )

    discount_line = ""
    if discount > 0:
        discount_line = (
            f"Сумма товаров: {fmt_price(subtotal)}\n"
            f"Промокод: {promo.get('code')} (−{fmt_price(discount)})\n"
        )

    if customer_chat_id:
        send_async(
            customer_chat_id,
            "✅ Заказ оформлен!\n\n"
            f"Номер заказа: MC-{new_id}\n"
            f"{items_lines}\n\n"
            f"{discount_line}"
            f"Итого: {fmt_price(total)}\n\n"
            "Мы свяжемся с вами в этом чате, чтобы подтвердить заказ и "
            "обсудить доставку.",
            "подтверждение заказа",
        )

    admin_text = (
        f"🛒 Новый заказ MC-{new_id}\n\n"
        f"Клиент: {name}\n"
        f"Телефон: {phone}\n"
        + (f"Telegram: @{customer_username}\n" if customer_username else "")
        + f"\n{items_lines}\n\n"
        f"{discount_line}"
        f"Итого: {fmt_price(total)}\n"
        + (f"\nКомментарий: {comment}\n" if comment else "")
        + "\nОткройте панель администратора (вкладка «Заказы»), чтобы "
          "написать клиенту и обсудить доставку."
    )
    for admin_id in ADMIN_CHAT_IDS:
        send_async(admin_id, admin_text, "уведомление админу")

    return jsonify({"ok": True, "order_id": new_id}), 201


@app.route("/api/promo/check", methods=["POST"])
def check_promo():
    """Проверка промокода из корзины (до оформления заказа)."""
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    try:
        subtotal = int(float(data.get("subtotal") or 0))
    except (TypeError, ValueError):
        subtotal = 0
    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    if not code:
        abort(400, description="Введите промокод")

    promo = find_promo_by_code(code)
    err = validate_promo_for_use(promo, subtotal, customer_chat_id)
    if err:
        abort(400, description=err)

    discount = compute_promo_discount(promo, subtotal)
    return jsonify({
        "ok": True,
        "code": promo.get("code"),
        "type": promo.get("type"),
        "value": promo.get("value"),
        "discount": discount,
        "new_total": subtotal - discount,
    })


@app.route("/api/orders", methods=["GET"])
def customer_orders():
    """История заказов покупателя (по его Telegram ID)."""
    try:
        tg_id = int(request.args.get("tg_id", ""))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")
    mine = db.fetch("orders", "WHERE customer_chat_id = %s", (tg_id,), order="id DESC")
    return jsonify(mine)


@app.route("/api/orders/<int:oid>/cancel", methods=["POST"])
def customer_cancel_order(oid):
    """Покупатель отменяет свой заказ, если он ещё не взят в обработку."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        tg_id = int(data.get("tg_id"))
    except (TypeError, ValueError):
        abort(400, description="Некорректный tg_id")

    order = db.get("orders", oid)
    if not order:
        abort(404, description="Заказ не найден")
    if order.get("customer_chat_id") != tg_id:
        abort(403, description="Это не ваш заказ")
    if order.get("status") != "new":
        abort(400, description="Заказ уже в обработке — отмена недоступна")

    order = db.update("orders", oid, {"status": "cancelled"})

    for admin_id in ADMIN_CHAT_IDS:
        send_async(admin_id, f"❌ Клиент отменил заказ MC-{oid}", "уведомление об отмене")

    return jsonify(order)


@app.route("/api/question", methods=["POST"])
def create_question():
    data = request.get_json(force=True, silent=True) or {}

    question = (data.get("question") or "").strip()
    product_name = (data.get("product_name") or "").strip()
    try:
        product_id = int(data.get("product_id"))
    except (TypeError, ValueError):
        product_id = None

    if not question:
        abort(400, description="Введите вопрос")

    tg_user = data.get("tg_user") or {}
    try:
        customer_chat_id = int(tg_user.get("id")) if tg_user.get("id") else None
    except (TypeError, ValueError):
        customer_chat_id = None
    customer_username = (tg_user.get("username") or "").strip() or None

    entry = db.insert("questions", {
        "product_id": product_id,
        "product_name": product_name,
        "question": question,
        "customer_chat_id": customer_chat_id,
        "customer_username": customer_username,
        "status": "new",
    })
    new_id = entry["id"]

    if customer_chat_id:
        send_async(
            customer_chat_id,
            "✅ Ваш вопрос отправлен администратору.\n\n"
            f"Товар: {product_name}\n"
            f"Вопрос: {question}\n\n"
            "Мы ответим вам в этом чате.",
            "подтверждение вопроса",
        )

    admin_text = (
        f"❓ Новый вопрос по товару «{product_name}»\n\n"
        + (f"От: @{customer_username}\n\n" if customer_username else "\n")
        + f"{question}\n\n"
        "Откройте панель администратора (вкладка «Вопросы»), чтобы ответить клиенту."
    )
    for admin_id in ADMIN_CHAT_IDS:
        send_async(admin_id, admin_text, "уведомление админу")

    return jsonify({"ok": True, "question_id": new_id}), 201


@app.route("/admin", methods=["GET"])
def admin_page():
    return send_from_directory(APP_DIR, "admin.html")


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}


# --- Admin API -------------------------------------------------------

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    require_admin()
    return jsonify({"ok": True})


@app.route("/api/admin/products", methods=["GET"])
def admin_get_products():
    require_admin()
    return jsonify(db.fetch("products"))


@app.route("/api/admin/products", methods=["POST"])
def admin_create_product():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    sizes_raw = data.get("sizes", "")
    sizes = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or [])
    try:
        price = int(float(data.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    images = sanitize_images(data.get("images") if "images" in data else data.get("image"))
    product = db.insert("products", {
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "price": price,
        "old_price": sanitize_old_price(data.get("old_price")),
        "badge": sanitize_badge(data.get("badge")),
        "sizes": sizes or ["One size"],
        "swatch": int(data.get("swatch") or 0) % 6,
        "desc": (data.get("desc") or "").strip(),
        "images": images,
        "image": images[0] if images else None,
    })
    invalidate_catalog()
    return jsonify(product), 201


@app.route("/api/admin/products/<int:pid>", methods=["PUT"])
def admin_update_product(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    p = db.get("products", pid)
    if not p:
        abort(404, description="Товар не найден")

    upd = {}
    if "name" in data:
        upd["name"] = (data.get("name") or "").strip()
    if "category" in data:
        upd["category"] = (data.get("category") or "").strip()
    if "price" in data:
        try:
            upd["price"] = int(float(data.get("price") or 0))
        except (TypeError, ValueError):
            pass
    if "old_price" in data:
        upd["old_price"] = sanitize_old_price(data.get("old_price"))
    if "badge" in data:
        upd["badge"] = sanitize_badge(data.get("badge"))
    if "sizes" in data:
        sizes_raw = data.get("sizes", "")
        upd["sizes"] = [s.strip() for s in sizes_raw.split(",") if s.strip()] if isinstance(sizes_raw, str) else (sizes_raw or p["sizes"])
    if "desc" in data:
        upd["desc"] = (data.get("desc") or "").strip()
    if "swatch" in data:
        upd["swatch"] = int(data.get("swatch") or 0) % 6
    if "images" in data:
        images = sanitize_images(data.get("images"))
        upd["images"] = images
        upd["image"] = images[0] if images else None
    elif "image" in data:
        images = sanitize_images(data.get("image"))
        upd["images"] = images
        upd["image"] = images[0] if images else None
    result = db.update("products", pid, upd)
    invalidate_catalog()
    return jsonify(result)


@app.route("/api/admin/products/<int:pid>", methods=["DELETE"])
def admin_delete_product(pid):
    require_admin()
    if not db.delete("products", pid):
        abort(404, description="Товар не найден")
    invalidate_catalog()
    return jsonify({"ok": True})


@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    """Загружает фото в Cloudinary и возвращает постоянную https-ссылку."""
    require_admin()
    file = request.files.get("file")
    if not file or not file.filename:
        abort(400, description="Файл не передан")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        abort(400, description="Недопустимый формат файла")
    try:
        result = cloudinary.uploader.upload(
            file.stream,
            folder=CLOUDINARY_FOLDER,
            resource_type="image",
        )
    except Exception as e:
        log.exception("Cloudinary upload failed")
        abort(400, description=f"Не удалось загрузить фото в Cloudinary: {e}")
    url = result["secure_url"]
    # Автоформат и автокачество — фото грузятся быстрее (для gif не применяем)
    if result.get("format") != "gif":
        url = url.replace("/upload/", "/upload/f_auto,q_auto/", 1)
    return jsonify({"url": url}), 201


@app.route("/api/admin/promos", methods=["GET"])
def admin_get_promos():
    require_admin()
    return jsonify(db.fetch("promos", order="id DESC"))


@app.route("/api/admin/promos", methods=["POST"])
def admin_create_promo():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}

    code = (data.get("code") or "").strip().upper()
    if not code:
        abort(400, description="Укажите код промокода")

    promo_type = (data.get("type") or "").strip().lower()
    if promo_type not in ALLOWED_PROMO_TYPES:
        abort(400, description="Некорректный тип скидки")

    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        abort(400, description="Укажите значение скидки")
    if value <= 0:
        abort(400, description="Значение скидки должно быть больше нуля")
    if promo_type == "percent" and value > 100:
        abort(400, description="Скидка в процентах не может быть больше 100")

    if db.promo_by_code(code):
        abort(400, description="Такой промокод уже существует")

    promo = db.insert("promos", {
        "code": code,
        "type": promo_type,
        "value": value,
        "active": bool(data.get("active", True)),
        "max_uses": sanitize_positive_int(data.get("max_uses")),
        "used_count": 0,
        "min_total": sanitize_positive_int(data.get("min_total")),
    })
    return jsonify(promo), 201


@app.route("/api/admin/promos/<int:pid>", methods=["PUT"])
def admin_update_promo(pid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    p = db.get("promos", pid)
    if not p:
        abort(404, description="Промокод не найден")

    upd = {}
    if "code" in data:
        code = (data.get("code") or "").strip().upper()
        if not code:
            abort(400, description="Укажите код промокода")
        other = db.promo_by_code(code)
        if other and other.get("id") != pid:
            abort(400, description="Такой промокод уже существует")
        upd["code"] = code
    if "type" in data:
        promo_type = (data.get("type") or "").strip().lower()
        if promo_type not in ALLOWED_PROMO_TYPES:
            abort(400, description="Некорректный тип скидки")
        upd["type"] = promo_type
    if "value" in data:
        try:
            value = float(data.get("value"))
        except (TypeError, ValueError):
            abort(400, description="Укажите значение скидки")
        if value <= 0:
            abort(400, description="Значение скидки должно быть больше нуля")
        if upd.get("type", p.get("type")) == "percent" and value > 100:
            abort(400, description="Скидка в процентах не может быть больше 100")
        upd["value"] = value
    if "active" in data:
        upd["active"] = bool(data.get("active"))
    if "max_uses" in data:
        upd["max_uses"] = sanitize_positive_int(data.get("max_uses"))
    if "min_total" in data:
        upd["min_total"] = sanitize_positive_int(data.get("min_total"))
    return jsonify(db.update("promos", pid, upd))


@app.route("/api/admin/promos/<int:pid>", methods=["DELETE"])
def admin_delete_promo(pid):
    require_admin()
    if not db.delete("promos", pid):
        abort(404, description="Промокод не найден")
    return jsonify({"ok": True})


@app.route("/api/admin/orders", methods=["GET"])
def admin_get_orders():
    require_admin()
    return jsonify(db.fetch("orders", order="id DESC"))


@app.route("/api/admin/orders/<int:oid>/status", methods=["PUT"])
def admin_update_order_status(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in {"new", "contacted", "done", "cancelled"}:
        abort(400, description="Некорректный статус")
    order = db.update("orders", oid, {"status": status})
    if not order:
        abort(404, description="Заказ не найден")
    return jsonify(order)


@app.route("/api/admin/orders/<int:oid>/message", methods=["POST"])
def admin_message_customer(oid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст сообщения")

    order = db.get("orders", oid)
    if not order:
        abort(404, description="Заказ не найден")

    chat_id = order.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого заказа нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Сообщение от Makeup&Care по заказу MC-{oid}:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить сообщение: {e}")

    order = db.add_message(
        "orders", oid,
        {"text": text, "at": datetime.now(timezone.utc).isoformat()},
        mark_status=("new", "contacted"),
    )
    return jsonify(order)


@app.route("/api/admin/questions", methods=["GET"])
def admin_get_questions():
    require_admin()
    return jsonify(db.fetch("questions", order="id DESC"))


@app.route("/api/admin/questions/<int:qid>/message", methods=["POST"])
def admin_message_question(qid):
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        abort(400, description="Введите текст ответа")

    q = db.get("questions", qid)
    if not q:
        abort(404, description="Вопрос не найден")

    chat_id = q.get("customer_chat_id")
    if not chat_id:
        abort(400, description="У этого вопроса нет чата с покупателем в Telegram")

    try:
        bot.send_message(
            chat_id,
            f"💬 Ответ от Makeup&Care по товару «{q.get('product_name','')}»:\n\n{text}"
        )
    except Exception as e:
        abort(400, description=f"Не удалось отправить ответ: {e}")

    q = db.add_message(
        "questions", qid,
        {"text": text, "at": datetime.now(timezone.utc).isoformat()},
        mark_status="answered",
    )
    return jsonify(q)


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "OK", 200


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
def handle_api_errors(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(err, "description", str(err))}), err.code
    return err


def setup_webhook():
    if not BASE_URL:
        log.warning("WEBHOOK_URL / RENDER_EXTERNAL_URL не заданы — вебхук не установлен.")
        return
    url = f"{BASE_URL}/webhook/{BOT_TOKEN}"
    try:
        if bot.get_webhook_info().url == url:
            log.info("Webhook уже установлен: %s", url)
            return
    except Exception as e:
        log.warning("Не удалось проверить вебхук: %s", e)
    bot.set_webhook(url=url, max_connections=20, drop_pending_updates=False)
    log.info("Webhook установлен: %s", url)


setup_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
