"""
Telegram-бот: ищет в OpenStreetMap компании без сайта и присылает их по одной.
Без API-ключей. Всё в одном файле. Запуск: python bot.py

Настройка: впиши BOT_TOKEN и OWNER_ID прямо в раздел КОНФИГ ниже
(или задай одноимённые переменные окружения; DB_PATH необязателен).

Зависимости: python-telegram-bot>=21, httpx>=0.27, python-dotenv (необязательно)
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlparse

import httpx
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import LinkPreviewOptions, Update
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, TypeHandler, filters)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)

# ============================== КОНФИГ ==============================
# ===== ВПИШИ СВОИ ЗНАЧЕНИЯ СЮДА (замени текст в кавычках и 0) =====
BOT_TOKEN = "8877577931:AAEH2AjO2nLBfysUUprUoZ3G31ytfGAmJmo"   # токен от @BotFather
OWNER_ID = 1527444391                       # твой Telegram ID, число (например 123456789)
# ===================================================================
TOKEN_PLACEHOLDER = "ВСТАВЬ_ТОКЕН_СЮДА"
BOT_TOKEN = os.getenv("BOT_TOKEN") or BOT_TOKEN          # переменные окружения, если заданы, главнее
OWNER_ID = int(os.getenv("OWNER_ID") or 0) or OWNER_ID
DB_PATH = os.getenv("DB_PATH", "leads.db")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = [  # публичные серверы Overpass, пробуем по очереди
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "leadbot/1.0 (personal lead search bot)"}  # Nominatim требует User-Agent
MAX_RESULTS = 400

# Категории: (слова-основы, [условия OSM]). Первое совпадение выигрывает, порядок важен.
CATS = [
    (("кофейн", "кафе", "кофе"), ['["amenity"="cafe"]']),
    (("пицц",), ['["cuisine"="pizza"]["amenity"~"restaurant|fast_food"]']),
    (("ресторан",), ['["amenity"="restaurant"]']),
    (("фастфуд", "фаст-фуд", "быстрое питание", "шаурм", "бургер"), ['["amenity"="fast_food"]']),
    (("барбер", "парикмахер", "стрижк"), ['["shop"="hairdresser"]']),
    (("бар", "паб"), ['["amenity"="bar"]', '["amenity"="pub"]']),
    (("пекарн", "булочн"), ['["shop"="bakery"]']),
    (("салон красоты", "красот", "маникюр", "ногт", "косметолог"), ['["shop"="beauty"]']),
    (("стоматолог", "зубн"), ['["amenity"="dentist"]', '["healthcare"="dentist"]']),
    (("клиник", "медцентр", "медицинск"), ['["amenity"="clinic"]', '["amenity"="doctors"]']),
    (("аптек",), ['["amenity"="pharmacy"]']),
    (("автосервис", "автомастерск", "шиномонтаж", "ремонт авто"), ['["shop"="car_repair"]']),
    (("автомойк",), ['["amenity"="car_wash"]']),
    (("одежд", "бутик"), ['["shop"="clothes"]']),
    (("обув",), ['["shop"="shoes"]']),
    (("цвет", "флорист"), ['["shop"="florist"]']),
    (("продукт", "минимаркет"), ['["shop"="convenience"]', '["shop"="supermarket"]']),
    (("фитнес", "спортзал", "тренажер", "тренажёр"), ['["leisure"="fitness_centre"]']),
    (("гостиниц", "отел", "хостел"), ['["tourism"="hotel"]', '["tourism"="hostel"]', '["tourism"="guest_house"]']),
    (("химчистк", "прачечн"), ['["shop"="dry_cleaning"]', '["shop"="laundry"]']),
    (("ветеринар", "ветклиник"), ['["amenity"="veterinary"]']),
    (("зоомагазин",), ['["shop"="pet"]']),
    (("ювелир",), ['["shop"="jewelry"]']),
    (("стройматериал", "строительн"), ['["shop"="doityourself"]', '["shop"="hardware"]']),
    (("фотостуд",), ['["shop"="photo"]', '["craft"="photographer"]']),
    (("юрист", "адвокат", "нотариус"), ['["office"="lawyer"]', '["office"="notary"]']),
    (("массаж", "спа"), ['["shop"="massage"]', '["leisure"="spa"]']),
    (("мебель", "мебельн"), ['["shop"="furniture"]']),
    (("тату", "татуиров"), ['["shop"="tattoo"]']),
    (("детский сад",), ['["amenity"="kindergarten"]']),
]

# ============================== БАЗА ==============================


def _c():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    with closing(_c()) as c, c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS leads(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL,
              uid TEXT NOT NULL UNIQUE,      -- ключ против дублей
              title TEXT DEFAULT '', text TEXT DEFAULT '', contact TEXT DEFAULT '',
              link TEXT DEFAULT '', meta TEXT DEFAULT '', query TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'new',   -- new | skipped | taken
              created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_leads ON leads(kind, status, query);
            """
        )


def db_add(uid, title="", text="", contact="", link="", meta="", query="") -> bool:
    with closing(_c()) as c, c:
        cur = c.execute(
            "INSERT OR IGNORE INTO leads (kind,uid,title,text,contact,link,meta,query,created)"
            " VALUES ('place',?,?,?,?,?,?,?,?)",
            (uid, title, text, contact, link, meta, query, time.time()),
        )
        return cur.rowcount > 0


def db_next(query=None):
    sql, args = "SELECT * FROM leads WHERE kind='place' AND status='new'", []
    if query:
        sql += " AND query=?"
        args.append(query)
    with closing(_c()) as c:
        return c.execute(sql + " ORDER BY id LIMIT 1", args).fetchone()


def db_get(lead_id):
    with closing(_c()) as c:
        return c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def db_set_status(lead_id, status):
    with closing(_c()) as c, c:
        c.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))


# ============================== ПОИСК В OSM ==============================


def selectors_for(category: str) -> tuple[list[str], bool]:
    """Возвращает (условия OSM, найдена_ли_категория). Если нет, ищем по части названия."""
    cat = category.lower().strip()
    words = cat.split()
    for stems, sels in CATS:
        for s in stems:
            if (s in cat) if " " in s else any(w.startswith(s) for w in words):
                return sels, True
    term = max((re.sub(r"[^\w\-]", "", w, flags=re.U) for w in words), default="", key=len)
    term = term[:-2] if len(term) > 5 else term
    return [f'["name"~"{term}",i]'], False


def build_query(prefix: str, scope: str, sels: list[str]) -> str:
    nosite = '[!"website"][!"contact:website"][!"url"]'
    lines = "\n".join(f'  nwr{s}["name"]{nosite}{scope};' for s in sels)
    return f"[out:json][timeout:60];\n{prefix}\n(\n{lines}\n);\nout center tags {MAX_RESULTS};"


async def geocode(http: httpx.AsyncClient, city: str) -> dict | None:
    for extra in ({"featureType": "settlement"}, {}):
        r = await http.get(NOMINATIM, params={"q": city, "format": "jsonv2", "limit": 1,
                                              "accept-language": "ru", **extra}, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        if data:
            return data[0]
    return None


async def overpass(http: httpx.AsyncClient, query: str) -> list[dict]:
    """Пробует серверы по очереди, при неудаче делает вторую попытку после паузы."""
    errors: list[str] = []
    t0 = time.time()
    for attempt in range(2):
        for url in OVERPASS:
            host = urlparse(url).netloc
            try:
                r = await http.post(url, data={"data": query}, headers=HEADERS)
                if r.status_code != 200:
                    errors.append(f"{host}: HTTP {r.status_code}")
                    continue
                data = r.json()
                elements = data.get("elements", [])
                if not elements and "error" in str(data.get("remark", "")).lower():
                    errors.append(f"{host}: {data.get('remark')}")
                    continue
                return elements
            except Exception as e:
                errors.append(f"{host}: {type(e).__name__}")
        if attempt == 0 and time.time() - t0 < 120:
            await asyncio.sleep(5)
        else:
            break
    raise RuntimeError("серверы OSM сейчас перегружены или недоступны (" + "; ".join(errors[-3:]) + ")")


def to_lead(el: dict) -> dict | None:
    t = el.get("tags", {})
    name = t.get("name", "").strip()
    if not name:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")

    street = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")]))
    address = ", ".join(filter(None, [t.get("addr:city"), street]))
    phone = t.get("phone") or t.get("contact:phone") or t.get("mobile") or t.get("contact:mobile") or ""
    email = t.get("email") or t.get("contact:email") or ""
    socials = [f"{label}: {t[k]}" for label, k in (("Instagram", "contact:instagram"), ("VK", "contact:vk"),
                                                    ("Telegram", "contact:telegram"), ("WhatsApp", "contact:whatsapp"),
                                                    ("Facebook", "contact:facebook")) if t.get(k)]
    meta = "OSM · сайт в базе не указан (проверь вручную)"
    if socials:
        meta += "\n🔗 " + "; ".join(socials)
    link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}" if lat and lon else ""
    score = (2 if phone else 0) + (1 if (email or socials) else 0)
    return {"uid": f"osm:{el['type']}:{el['id']}", "title": name, "text": address,
            "contact": " · ".join(filter(None, [phone, email])), "link": link, "meta": meta, "_score": score}


async def find_places(category: str, city: str) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    sels, known = selectors_for(category)
    if not known:
        notes.append("Категория не распознана, ищу по части названия.")
    async with httpx.AsyncClient(timeout=90) as http:
        place = await geocode(http, city)
        if not place:
            return [], [f"Город «{city}» не найден."]
        notes.insert(0, "📍 " + place.get("display_name", city))

        scopes = []
        if place.get("osm_type") == "relation":
            scopes.append((f"area({3600000000 + int(place['osm_id'])})->.a;", "(area.a)"))
        s, n, w, e = place["boundingbox"]
        scopes.append(("", f"({s},{w},{n},{e})"))

        elements: list[dict] = []
        for prefix, scope in scopes:
            elements = await overpass(http, build_query(prefix, scope, sels))
            if elements:
                break

    leads = [l for l in (to_lead(el) for el in elements) if l]
    leads.sort(key=lambda l: -l["_score"])  # сначала те, у кого есть контакты
    return leads, notes


# ============================== БОТ ==============================
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
NEW_SEARCH = Btn("🔍 Новый поиск", callback_data="search")
HINT = "Введи запрос: <b>категория, город</b>\nНапример: Кофейни, Казань"


def card(lead, taken: bool = False):
    e = html.escape
    text = (f"🏪 <b>{e(lead['title'])}</b>\n📍 {e(lead['text'] or 'адрес не указан')}\n"
            f"📞 {e(lead['contact'] or 'контактов в базе нет')}\nℹ️ {e(lead['meta'])}")
    links = []
    if lead["link"]:
        links.append(Btn("🗺 На карте", url=lead["link"]))
    m = re.match(r"osm:(node|way|relation):(\d+)", lead["uid"])
    if m:
        links.append(Btn("OSM", url=f"https://www.openstreetmap.org/{m.group(1)}/{m.group(2)}"))
    if taken:
        text = "✅ <b>В работе</b>\n\n" + text
        actions = [Btn("⏭ Дальше", callback_data="next")]
    else:
        actions = [Btn("⏭ Пропустить", callback_data=f"skip:{lead['id']}"),
                   Btn("✅ Беру", callback_data=f"take:{lead['id']}")]
    return text, Markup(([links] if links else []) + [actions, [NEW_SEARCH]])


async def show(update: Update, text: str, markup: Markup | None = None):
    kw = dict(reply_markup=markup, parse_mode="HTML", link_preview_options=NO_PREVIEW)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, **kw)
    else:
        await update.effective_message.reply_text(text, **kw)


async def show_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lead = db_next(ctx.user_data.get("query"))
    if lead:
        await show(update, *card(lead))
    else:
        await show(update, "Больше нет компаний без сайта по этому запросу.", Markup([[NEW_SEARCH]]))


async def deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != OWNER_ID:
        raise ApplicationHandlerStop  # отвечаем только владельцу


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show(update, "Привет! " + HINT)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, arg = q.data.partition(":")

    if action == "search":
        await q.edit_message_text(HINT, parse_mode="HTML")
    elif action == "next":
        await show_next(update, ctx)
    elif action in ("skip", "take"):
        lead = db_get(int(arg))
        if not lead:
            return
        if action == "skip":
            db_set_status(lead["id"], "skipped")
            await show_next(update, ctx)
        else:
            db_set_status(lead["id"], "taken")
            await show(update, *card(lead, taken=True))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "," not in text:
        await show(update, "Нужен формат: <b>категория, город</b>\nНапример: Кофейни, Казань")
        return
    category, city = (p.strip() for p in text.rsplit(",", 1))
    if not category or not city:
        await show(update, HINT)
        return

    wait = await update.message.reply_text("Ищу, это может занять до минуты…")
    try:
        leads, notes = await find_places(category, city)
    except Exception as e:
        logging.exception("search failed")
        await wait.edit_text(f"⚠️ Не получилось выполнить поиск: {e}\nПопробуй ещё раз чуть позже.")
        return
    new = sum(db_add(query=text, **{k: v for k, v in l.items() if k != "_score"}) for l in leads)
    ctx.user_data["query"] = text

    summary = "\n".join(notes + [f"Без сайта в базе OSM: {len(leads)}, новых: {new}."])
    if not leads:
        summary += "\nПопробуй другую категорию или проверь написание города. В OSM данные есть не везде."
    await wait.edit_text(summary)
    if leads:
        await show_next(update, ctx)


def main():
    if BOT_TOKEN == TOKEN_PLACEHOLDER or not OWNER_ID:
        raise SystemExit("Впиши BOT_TOKEN и OWNER_ID в начале bot.py (раздел КОНФИГ).")
    db_init()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(TypeHandler(Update, deny), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()