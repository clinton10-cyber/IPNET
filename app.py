import os
import sqlite3
import secrets
import json as json_lib
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, g, render_template, request, jsonify, redirect, url_for, session, flash, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "ipnet.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "ico"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

# Paystack config — get these from https://dashboard.paystack.com/#/settings/developer
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
NGN_PER_USD = float(os.environ.get("NGN_PER_USD", "1600"))  # used to show wallet in USD equiv; adjust as needed

# ---------------------------------------------------------------------------
# Location-based currency display
# ---------------------------------------------------------------------------
# Prices are stored and charged in USD internally (wallet, checkout, orders).
# What changes per-visitor is only the DISPLAY currency, detected from their IP
# on first visit and overridable any time via the currency picker in the header.

CURRENCIES = {
    "USD": {"symbol": "$", "name": "US Dollar"},
    "NGN": {"symbol": "₦", "name": "Nigerian Naira"},
    "GBP": {"symbol": "£", "name": "British Pound"},
    "EUR": {"symbol": "€", "name": "Euro"},
    "GHS": {"symbol": "GH₵", "name": "Ghanaian Cedi"},
    "KES": {"symbol": "KSh", "name": "Kenyan Shilling"},
    "ZAR": {"symbol": "R", "name": "South African Rand"},
    "INR": {"symbol": "₹", "name": "Indian Rupee"},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar"},
    "AUD": {"symbol": "A$", "name": "Australian Dollar"},
    "EGP": {"symbol": "E£", "name": "Egyptian Pound"},
    "PKR": {"symbol": "₨", "name": "Pakistani Rupee"},
}

# Fallback rates (USD -> currency), used if the live rate lookup fails or hasn't
# run yet. The live lookup (refreshed every few hours via a free, keyless API)
# overrides these whenever it succeeds.
FALLBACK_RATES = {
    "USD": 1.0, "NGN": NGN_PER_USD, "GBP": 0.78, "EUR": 0.92, "GHS": 15.5,
    "KES": 129.0, "ZAR": 18.2, "INR": 84.0, "CAD": 1.37, "AUD": 1.52,
    "EGP": 48.5, "PKR": 279.0,
}

FX_CACHE_SECONDS = 6 * 3600


def get_client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def get_fx_rates():
    """Dict of USD->currency rates, cached in the settings table and refreshed
    at most once every FX_CACHE_SECONDS. Falls back to FALLBACK_RATES on error."""
    cached_at = get_setting("fx_rates_at", "")
    cached_json = get_setting("fx_rates_json", "")
    if cached_at and cached_json:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(cached_at)).total_seconds()
            if age < FX_CACHE_SECONDS:
                return json_lib.loads(cached_json)
        except Exception:
            pass
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=6)
        payload = resp.json()
        rates = payload.get("rates", {})
        if rates:
            merged = dict(FALLBACK_RATES)
            for code in CURRENCIES:
                if code in rates:
                    merged[code] = rates[code]
            set_setting("fx_rates_json", json_lib.dumps(merged))
            set_setting("fx_rates_at", datetime.utcnow().isoformat())
            return merged
    except Exception:
        pass
    return dict(FALLBACK_RATES)


def detect_currency_from_ip(ip):
    """Best-effort IP geolocation to a currency code. Returns 'USD' on failure."""
    if not ip or ip.startswith(("127.", "10.", "192.168.")):
        return "USD"
    try:
        resp = requests.get(f"https://ipapi.co/{ip}/json/", timeout=4)
        payload = resp.json()
        code = (payload.get("currency") or "").upper()
        if code in CURRENCIES:
            return code
    except Exception:
        pass
    return "USD"


def get_display_currency():
    """Session-cached display currency: explicit user choice wins, otherwise the
    IP-detected currency (looked up once per session), otherwise USD."""
    override = session.get("currency_override")
    if override in CURRENCIES:
        return override
    if "detected_currency" in session:
        return session["detected_currency"]
    code = detect_currency_from_ip(get_client_ip())
    session["detected_currency"] = code
    return code


def format_money(usd_amount, currency=None, rates=None):
    currency = currency or get_display_currency()
    rates = rates if rates is not None else get_fx_rates()
    rate = rates.get(currency, 1.0)
    symbol = CURRENCIES.get(currency, {}).get("symbol", "$")
    converted = float(usd_amount or 0) * rate
    if converted >= 1000:
        return f"{symbol}{converted:,.0f}"
    return f"{symbol}{converted:,.2f}"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    section TEXT NOT NULL,           -- 'gaming_accounts' | 'game_boost' | 'social_media' | 'websites' | 'other'
    icon TEXT DEFAULT '',
    icon_image TEXT DEFAULT '',      -- uploaded logo image path, takes priority over emoji icon if set
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    stock INTEGER DEFAULT 999,
    is_active INTEGER DEFAULT 1,
    image_url TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref TEXT UNIQUE NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    buyer_name TEXT,
    buyer_contact TEXT,
    buyer_country TEXT,
    items_json TEXT NOT NULL,
    total REAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    status TEXT DEFAULT 'pending',   -- pending | paid | delivered | cancelled
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT DEFAULT '',
    country TEXT DEFAULT '',
    wallet_balance REAL NOT NULL DEFAULT 0,
    paystack_customer_code TEXT DEFAULT '',
    dva_account_number TEXT DEFAULT '',
    dva_bank_name TEXT DEFAULT '',
    dva_account_name TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tx_ref TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,              -- 'funding' | 'purchase' | 'refund' | 'adjustment'
    amount REAL NOT NULL,            -- positive for credit, negative for debit
    currency TEXT DEFAULT 'NGN',
    status TEXT DEFAULT 'pending',   -- pending | success | failed
    provider TEXT DEFAULT 'paystack',
    provider_ref TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_title TEXT NOT NULL,
    username_or_email TEXT DEFAULT '',
    password TEXT DEFAULT '',
    extra_info TEXT DEFAULT '',
    delivered_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    image_url TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    image_url TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS listing_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    image_url TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);
"""


def migrate_schema(db):
    """Idempotent, safe-to-run-every-startup migrations for installs created before
    the games/screenshots feature existed."""
    try:
        db.execute("ALTER TABLE listings ADD COLUMN game_id INTEGER REFERENCES games(id)")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE listings ADD COLUMN platform_id INTEGER REFERENCES platforms(id)")
    except sqlite3.OperationalError:
        pass  # column already exists
    db.commit()


def seed_games_if_empty(db):
    count = db.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    if count > 0:
        return
    for i, game in enumerate(GAMES_50):
        db.execute(
            "INSERT INTO games (name, slug, sort_order) VALUES (?,?,?)",
            (game, slugify(game), i),
        )
    db.commit()


def seed_platforms_if_empty(db):
    count = db.execute("SELECT COUNT(*) c FROM platforms").fetchone()["c"]
    if count > 0:
        return
    for i, platform in enumerate(SOCIAL_PLATFORMS.keys()):
        db.execute(
            "INSERT INTO platforms (name, slug, sort_order) VALUES (?,?,?)",
            (platform, slugify(platform), i),
        )
    db.commit()


def migrate_legacy_social_categories_to_platform(db):
    """Categories were originally seeded per platform+service (e.g. 'TikTok
    Followers', 'TikTok Likes'). This tags any listing sitting in one of those
    categories with the matching row in the new `platforms` table, so the
    'Browse by Platform' hub (TikTok / Instagram / Facebook...) can group every
    account, service, and listing for that platform in one place — the same
    way `games` groups gaming accounts. Categories themselves are left alone;
    only the platform tag is backfilled."""
    for platform in SOCIAL_PLATFORMS.keys():
        platform_row = db.execute("SELECT id FROM platforms WHERE name=?", (platform,)).fetchone()
        if not platform_row:
            continue
        db.execute(
            "UPDATE listings SET platform_id=? WHERE platform_id IS NULL AND category_id IN "
            "(SELECT id FROM categories WHERE section='social_media' AND name LIKE ?)",
            (platform_row["id"], f"{platform} %"),
        )
    db.commit()


def migrate_legacy_per_game_categories(db):
    """Older installs seeded one category PER GAME (e.g. 'PUBG Mobile Accounts').
    Newer installs use a single dropdown of games (the `games` table) attached to
    a listing instead. This folds any leftover per-game categories into that
    dropdown so nothing is lost, then removes the now-redundant categories."""
    fallback_gaming = db.execute(
        "SELECT id FROM categories WHERE section='gaming_accounts' AND name=? ",
        (GAMING_ACCOUNT_CATEGORIES[0],),
    ).fetchone()
    fallback_boost = db.execute(
        "SELECT id FROM categories WHERE section='game_boost' AND name=?",
        (BOOST_CATEGORIES[0],),
    ).fetchone()

    for game in GAMES_50:
        game_row = db.execute("SELECT id FROM games WHERE name=?", (game,)).fetchone()
        if not game_row:
            continue
        game_id = game_row["id"]

        legacy_gaming = db.execute(
            "SELECT id FROM categories WHERE section='gaming_accounts' AND name=?",
            (f"{game} Accounts",),
        ).fetchone()
        if legacy_gaming and fallback_gaming:
            db.execute(
                "UPDATE listings SET category_id=?, game_id=? WHERE category_id=?",
                (fallback_gaming["id"], game_id, legacy_gaming["id"]),
            )
            db.execute("DELETE FROM categories WHERE id=?", (legacy_gaming["id"],))

        legacy_boost = db.execute(
            "SELECT id FROM categories WHERE section='game_boost' AND name=?",
            (f"{game} Boost & Credits",),
        ).fetchone()
        if legacy_boost and fallback_boost:
            db.execute(
                "UPDATE listings SET category_id=?, game_id=? WHERE category_id=?",
                (fallback_boost["id"], game_id, legacy_boost["id"]),
            )
            db.execute("DELETE FROM categories WHERE id=?", (legacy_boost["id"],))
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    migrate_schema(db)
    seed_if_empty(db)
    seed_games_if_empty(db)
    seed_platforms_if_empty(db)
    migrate_legacy_per_game_categories(db)
    migrate_legacy_social_categories_to_platform(db)
    db.close()


# ---------------------------------------------------------------------------
# Seed data — default categories across all sections
# ---------------------------------------------------------------------------

GAMES_50 = [
    "PUBG Mobile", "Call of Duty: Mobile", "Call of Duty: Warzone", "Free Fire",
    "Fortnite", "Valorant", "Counter-Strike 2 (CS:GO)", "Apex Legends",
    "Mobile Legends: Bang Bang", "League of Legends", "Wild Rift",
    "Overwatch 2", "Rainbow Six Siege", "Genshin Impact", "Honkai: Star Rail",
    "Roblox", "Minecraft", "GTA V / GTA Online", "FIFA / EA FC",
    "eFootball / PES", "Clash of Clans", "Clash Royale", "Brawl Stars",
    "Rules of Survival", "Standoff 2", "Critical Ops", "Arena of Valor",
    "Dota 2", "World of Warcraft", "Diablo 4", "Rocket League",
    "Among Us", "Garena AOV", "Blood Strike", "Delta Force",
    "Point Blank", "Special Forces Group 2", "Modern Combat 5",
  "Shadow Fight 4", "Asphalt 9: Legends", "8 Ball Pool",
    "Ludo King", "Subway Surfers", "Candy Crush Saga", "Teamfight Tactics",
    "Fall Guys", "Halo Infinite", "Battlefield 2042", "Destiny 2",
    "Sea of Thieves"
]

SOCIAL_PLATFORMS = {
    "TikTok": ["Followers", "Likes", "Views", "Shares", "Comments", "Live Stream Views", "Verified Account"],
    "YouTube": ["Subscribers", "Views", "Likes", "Comments", "Watch Time Hours", "Monetized Channel"],
    "Instagram": ["Followers", "Likes", "Views", "Comments", "Story Views", "Verified Account"],
    "Facebook": ["Page Likes", "Followers", "Post Likes", "Video Views", "Comments", "Aged Account"],
    "Twitter / X": ["Followers", "Likes", "Retweets", "Views", "Verified Account"],
    "Telegram": ["Channel Members", "Post Views", "Reactions"],
    "Snapchat": ["Followers", "Views", "Account"],
    "WhatsApp": ["Channel Followers", "Business Account"],
    "Twitch": ["Followers", "Live Viewers", "Subscribers"],
    "LinkedIn": ["Connections", "Followers", "Post Likes"],
}

GAMING_ACCOUNT_CATEGORIES = [
    "Full Access Accounts", "Rare Skins Accounts", "High Rank / Elite Accounts",
    "Starter Accounts", "Bundled Accounts (Multi-game)"
]

BOOST_CATEGORIES = [
    "Rank Boost", "Level Boost", "In-Game Currency / Credits",
    "Battle Pass Completion", "Win Boost (Placement Matches)", "Coaching Sessions"
]

WEBSITE_CATEGORIES = [
    "Business Websites", "E-commerce Stores", "Landing Pages",
    "Blogs / Content Sites", "Portfolio Websites", "Website Templates",
    "Domain + Hosting Bundles"
]

OTHER_CATEGORIES = [
    "Gift Cards", "VPN Subscriptions", "Streaming Accounts (Netflix, Spotify etc.)",
    "Software Licenses", "Email Accounts"
]


def slugify(text):
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-").replace("---", "-").replace("--", "-")


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def save_uploaded_image(file_storage, subfolder):
    """Save an uploaded image under static/uploads/<subfolder>/ and return its public URL path,
    or None if no valid file was provided."""
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_image(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = f"{secrets.token_hex(8)}.{ext}"
    folder = os.path.join(UPLOAD_DIR, subfolder)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, fname)
    file_storage.save(path)
    return f"/static/uploads/{subfolder}/{fname}"


def get_gallery_map(listing_ids):
    """Returns {listing_id: [image_url, ...]} for screenshot galleries."""
    if not listing_ids:
        return {}
    db = get_db()
    placeholders = ",".join("?" for _ in listing_ids)
    rows = db.execute(
        f"SELECT * FROM listing_images WHERE listing_id IN ({placeholders}) ORDER BY sort_order, id",
        listing_ids,
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["listing_id"], []).append(r["image_url"])
    return out


def get_setting(key, default=""):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def seed_if_empty(db):
    count = db.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if count > 0:
        return

    order = 0

    def add_cat(name, section, icon):
        nonlocal order
        order += 1
        slug = slugify(f"{section}-{name}")
        db.execute(
            "INSERT INTO categories (name, slug, section, icon, sort_order) VALUES (?,?,?,?,?)",
            (name, slug, section, icon, order),
        )

    # Gaming accounts categories (games are selected via a dropdown on the
    # listing itself now — see the `games` table — not as separate categories)
    for c in GAMING_ACCOUNT_CATEGORIES:
        add_cat(c, "gaming_accounts", "")

    # Game boosting / leveling / credits categories
    for c in BOOST_CATEGORIES:
        add_cat(c, "game_boost", "")

    # Social media, per platform per service
    for platform, services in SOCIAL_PLATFORMS.items():
        for service in services:
            add_cat(f"{platform} {service}", "social_media", "")

    # Websites
    for c in WEBSITE_CATEGORIES:
        add_cat(c, "websites", "")

    # Other digital goods
    for c in OTHER_CATEGORIES:
        add_cat(c, "other", "")

    db.commit()

    # Add a few sample listings so the storefront isn't empty
    sample_cats = db.execute("SELECT id, name FROM categories LIMIT 6").fetchall()
    for cat in sample_cats:
        db.execute(
            "INSERT INTO listings (category_id, title, description, price, currency, stock) VALUES (?,?,?,?,?,?)",
            (cat["id"], f"{cat['name']} - Starter Package", "Sample listing. Edit price and details in admin.", 5.0, "USD", 50),
        )
    db.commit()


SECTION_LABELS = {
    "gaming_accounts": "Gaming Accounts",
    "game_boost": "Game Boosting & Credits",
    "social_media": "Social Media Services",
    "websites": "Websites",
    "other": "Other Digital Goods",
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


@app.context_processor
def inject_user():
    currency = get_display_currency()
    rates = get_fx_rates()
    return {
        "current_user": current_user(),
        "site_logo": get_setting("site_logo", ""),
        "site_favicon": get_setting("site_favicon", ""),
        "display_currency": currency,
        "currency_options": CURRENCIES,
        "currency_symbol": CURRENCIES.get(currency, {}).get("symbol", "$"),
        "fx_rate": rates.get(currency, 1.0),
        "fmt_price": lambda usd: format_money(usd, currency=currency, rates=rates),
    }


@app.route("/set-currency", methods=["POST"])
def set_currency():
    code = request.form.get("currency", "").upper()
    if code in CURRENCIES:
        session["currency_override"] = code
    dest = request.form.get("next") or request.referrer or url_for("index")
    return redirect(dest)


# ---------------------------------------------------------------------------
# SEO: sitemap.xml + robots.txt
# ---------------------------------------------------------------------------

@app.route("/sitemap.xml")
def sitemap_xml():
    db = get_db()
    urls = [
        (url_for("index", _external=True), "1.0", "daily"),
        (url_for("games_hub", _external=True), "0.8", "daily"),
        (url_for("platforms_hub", _external=True), "0.8", "daily"),
    ]
    for section in SECTION_LABELS:
        urls.append((url_for("section_view", section=section, _external=True), "0.7", "weekly"))
    for cat in db.execute("SELECT id FROM categories ORDER BY id").fetchall():
        urls.append((url_for("category_view", cat_id=cat["id"], _external=True), "0.6", "weekly"))
    for game in db.execute("SELECT id FROM games ORDER BY id").fetchall():
        urls.append((url_for("game_view", game_id=game["id"], _external=True), "0.6", "weekly"))
    for platform in db.execute("SELECT id FROM platforms ORDER BY id").fetchall():
        urls.append((url_for("platform_view", platform_id=platform["id"], _external=True), "0.6", "weekly"))

    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, priority, changefreq in urls:
        xml_parts.append(
            f"<url><loc>{loc}</loc><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>"
        )
    xml_parts.append("</urlset>")
    return Response("".join(xml_parts), mimetype="application/xml")


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /dashboard",
        "Disallow: /checkout",
        "Disallow: /order/",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /wallet/",
        "",
        f"Sitemap: {url_for('sitemap_xml', _external=True)}",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sw.js")
def service_worker():
    """Served from the root path (not /static/) so its default scope covers
    the whole site — a service worker can only control paths at or below
    the URL it's served from."""
    response = send_from_directory(app.static_folder, "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Public storefront routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    sections = []
    for section, label in SECTION_LABELS.items():
        cats = db.execute(
            "SELECT * FROM categories WHERE section=? ORDER BY sort_order LIMIT 12", (section,)
        ).fetchall()
        total = db.execute("SELECT COUNT(*) c FROM categories WHERE section=?", (section,)).fetchone()["c"]
        sections.append({"key": section, "label": label, "categories": cats, "total": total})
    featured = db.execute(
        "SELECT l.*, c.name as cat_name, c.section as section, g.name as game_name, g.image_url as game_image, "
        "p.name as platform_name, p.image_url as platform_image "
        "FROM listings l JOIN categories c ON c.id = l.category_id "
        "LEFT JOIN games g ON g.id = l.game_id "
        "LEFT JOIN platforms p ON p.id = l.platform_id "
        "WHERE l.is_active=1 ORDER BY l.created_at DESC LIMIT 8"
    ).fetchall()
    games = db.execute("SELECT * FROM games ORDER BY sort_order LIMIT 16").fetchall()
    platforms = db.execute("SELECT * FROM platforms ORDER BY sort_order LIMIT 16").fetchall()
    return render_template("index.html", sections=sections, featured=featured, section_labels=SECTION_LABELS, games=games, platforms=platforms)


@app.route("/section/<section>")
def section_view(section):
    db = get_db()
    if section not in SECTION_LABELS:
        return "Not found", 404
    q = request.args.get("q", "").strip()
    if q:
        cats = db.execute(
            "SELECT * FROM categories WHERE section=? AND name LIKE ? ORDER BY sort_order",
            (section, f"%{q}%"),
        ).fetchall()
    else:
        cats = db.execute("SELECT * FROM categories WHERE section=? ORDER BY sort_order", (section,)).fetchall()
    return render_template("section.html", section=section, label=SECTION_LABELS[section], categories=cats, q=q)


@app.route("/category/<int:cat_id>")
def category_view(cat_id):
    db = get_db()
    cat = db.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()
    if not cat:
        return "Not found", 404

    game_id = request.args.get("game_id", "").strip()
    platform_id = request.args.get("platform_id", "").strip()
    is_game_section = cat["section"] in ("gaming_accounts", "game_boost")
    is_social_section = cat["section"] == "social_media"

    base_query = (
        "SELECT l.*, g.name as game_name, g.image_url as game_image, "
        "p.name as platform_name, p.image_url as platform_image FROM listings l "
        "LEFT JOIN games g ON g.id = l.game_id "
        "LEFT JOIN platforms p ON p.id = l.platform_id "
        "WHERE l.category_id=? AND l.is_active=1"
    )
    params = [cat_id]
    if is_game_section and game_id:
        base_query += " AND l.game_id=?"
        params.append(game_id)
    if is_social_section and platform_id:
        base_query += " AND l.platform_id=?"
        params.append(platform_id)
    listings = db.execute(base_query + " ORDER BY l.price ASC", params).fetchall()

    gallery_map = get_gallery_map([l["id"] for l in listings])

    games_in_category = []
    if is_game_section:
        games_in_category = db.execute(
            "SELECT DISTINCT g.id, g.name, g.image_url FROM listings l "
            "JOIN games g ON g.id = l.game_id WHERE l.category_id=? AND l.is_active=1 ORDER BY g.name",
            (cat_id,),
        ).fetchall()

    platforms_in_category = []
    if is_social_section:
        platforms_in_category = db.execute(
            "SELECT DISTINCT p.id, p.name, p.image_url FROM listings l "
            "JOIN platforms p ON p.id = l.platform_id WHERE l.category_id=? AND l.is_active=1 ORDER BY p.name",
            (cat_id,),
        ).fetchall()

    return render_template(
        "category.html", category=cat, listings=listings, label=SECTION_LABELS.get(cat["section"], ""),
        gallery_map=gallery_map, games_in_category=games_in_category, selected_game=game_id,
        platforms_in_category=platforms_in_category, selected_platform=platform_id,
    )


@app.route("/games")
def games_hub():
    db = get_db()
    games = db.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM listings l WHERE l.game_id=g.id AND l.is_active=1) as listing_count "
        "FROM games g ORDER BY g.sort_order"
    ).fetchall()
    return render_template("games_hub.html", games=games)


@app.route("/games/<int:game_id>")
def game_view(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if not game:
        return "Not found", 404
    listings = db.execute(
        "SELECT l.*, c.name as cat_name, c.section as section FROM listings l "
        "JOIN categories c ON c.id = l.category_id "
        "WHERE l.game_id=? AND l.is_active=1 ORDER BY c.section, l.price ASC",
        (game_id,),
    ).fetchall()
    gallery_map = get_gallery_map([l["id"] for l in listings])
    return render_template("game_view.html", game=game, listings=listings, gallery_map=gallery_map, section_labels=SECTION_LABELS)


@app.route("/platforms")
def platforms_hub():
    db = get_db()
    platforms = db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM listings l WHERE l.platform_id=p.id AND l.is_active=1) as listing_count "
        "FROM platforms p ORDER BY p.sort_order"
    ).fetchall()
    return render_template("platforms_hub.html", platforms=platforms)


@app.route("/platforms/<int:platform_id>")
def platform_view(platform_id):
    db = get_db()
    platform = db.execute("SELECT * FROM platforms WHERE id=?", (platform_id,)).fetchone()
    if not platform:
        return "Not found", 404
    listings = db.execute(
        "SELECT l.*, c.name as cat_name, c.section as section FROM listings l "
        "JOIN categories c ON c.id = l.category_id "
        "WHERE l.platform_id=? AND l.is_active=1 ORDER BY c.name, l.price ASC",
        (platform_id,),
    ).fetchall()
    gallery_map = get_gallery_map([l["id"] for l in listings])
    return render_template("platform_view.html", platform=platform, listings=listings, gallery_map=gallery_map, section_labels=SECTION_LABELS)


@app.route("/search")
def search():
    db = get_db()
    q = request.args.get("q", "").strip()
    results = []
    if q:
        results = db.execute(
            "SELECT l.*, c.name as cat_name, c.section as section, g.name as game_name, p.name as platform_name "
            "FROM listings l JOIN categories c ON c.id=l.category_id "
            "LEFT JOIN games g ON g.id = l.game_id "
            "LEFT JOIN platforms p ON p.id = l.platform_id "
            "WHERE l.is_active=1 AND (l.title LIKE ? OR c.name LIKE ? OR g.name LIKE ? OR p.name LIKE ?) LIMIT 60",
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    return render_template("search.html", q=q, results=results)


@app.route("/checkout", methods=["POST"])
def checkout():
    db = get_db()
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "Cart is empty"}), 400

    total = 0.0
    validated_items = []
    for it in items:
        listing = db.execute("SELECT * FROM listings WHERE id=? AND is_active=1", (it.get("id"),)).fetchone()
        if not listing:
            continue
        qty = max(1, int(it.get("qty", 1)))
        line_total = listing["price"] * qty
        total += line_total
        validated_items.append({
            "id": listing["id"], "title": listing["title"], "price": listing["price"],
            "qty": qty, "line_total": line_total,
        })

    if not validated_items:
        return jsonify({"error": "No valid items"}), 400

    user = current_user()
    order_status_val = "pending"

    if user:
        if user["wallet_balance"] < total:
            return jsonify({
                "error": f"Insufficient wallet balance. You have ${user['wallet_balance']:.2f}, need ${total:.2f}.",
                "insufficient_balance": True,
            }), 400
        db.execute("UPDATE users SET wallet_balance = wallet_balance - ? WHERE id=?", (total, user["id"]))
        tx_ref = "IPNP-" + secrets.token_hex(8).upper()
        db.execute(
            "INSERT INTO wallet_transactions (user_id, tx_ref, kind, amount, currency, status, provider, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], tx_ref, "purchase", -total, "USD", "success", "wallet", "Order payment"),
        )
        order_status_val = "paid"

    order_ref = "IPN-" + secrets.token_hex(4).upper()
    db.execute(
        "INSERT INTO orders (order_ref, user_id, buyer_name, buyer_contact, buyer_country, items_json, total, currency, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            order_ref,
            user["id"] if user else None,
            data.get("name", user["full_name"] if user else ""),
            data.get("contact", user["email"] if user else ""),
            data.get("country", user["country"] if user else ""),
            json_lib.dumps(validated_items),
            total,
            "USD",
            order_status_val,
        ),
    )
    db.commit()
    return jsonify({"order_ref": order_ref, "total": total, "items": validated_items, "paid_from_wallet": bool(user)})


@app.route("/order/<order_ref>")
def order_status(order_ref):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref,)).fetchone()
    if not order:
        return "Order not found", 404
    items = json_lib.loads(order["items_json"])
    return render_template("order.html", order=order, items=items)


# ---------------------------------------------------------------------------
# User auth
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Paystack Dedicated Virtual Account (DVA) helpers
# ---------------------------------------------------------------------------

def create_paystack_customer_and_dva(user_id, email, full_name):
    """Create a Paystack customer + a permanent dedicated virtual account for them.
    Returns dict with account_number/bank_name/account_name on success, or None on failure.
    Requires PAYSTACK_SECRET_KEY and your Paystack account to be approved for
    Dedicated Virtual Accounts (Settings -> Preferences on their dashboard)."""
    if not PAYSTACK_SECRET_KEY:
        return None

    name_parts = (full_name or "").strip().split(" ", 1)
    first_name = name_parts[0] if name_parts and name_parts[0] else "IPNET"
    last_name = name_parts[1] if len(name_parts) > 1 else "User"

    try:
        cust_resp = requests.post(
            "https://api.paystack.co/customer",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={"email": email, "first_name": first_name, "last_name": last_name},
            timeout=15,
        )
        cust_payload = cust_resp.json()
        if not cust_payload.get("status"):
            return None
        customer_code = cust_payload["data"]["customer_code"]

        dva_resp = requests.post(
            "https://api.paystack.co/dedicated_account",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={"customer": customer_code, "preferred_bank": "wema-bank"},
            timeout=20,
        )
        dva_payload = dva_resp.json()
        if not dva_payload.get("status"):
            return {"customer_code": customer_code, "account_number": "", "bank_name": "", "account_name": ""}

        d = dva_payload["data"]
        return {
            "customer_code": customer_code,
            "account_number": d.get("account_number", ""),
            "bank_name": d.get("bank", {}).get("name", ""),
            "account_name": d.get("account_name", ""),
        }
    except requests.RequestException:
        return None


def ensure_user_has_dva(user):
    """Lazily create a DVA for a user who doesn't have one yet (e.g. signed up before
    Paystack keys were configured, or creation failed at signup time). Safe to call often."""
    if user["dva_account_number"] or not PAYSTACK_SECRET_KEY:
        return user
    result = create_paystack_customer_and_dva(user["id"], user["email"], user["full_name"])
    if result and result.get("account_number"):
        db = get_db()
        db.execute(
            "UPDATE users SET paystack_customer_code=?, dva_account_number=?, dva_bank_name=?, dva_account_name=? WHERE id=?",
            (result["customer_code"], result["account_number"], result["bank_name"], result["account_name"], user["id"]),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return user


# ---------------------------------------------------------------------------
# User auth
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        country = request.form.get("country", "").strip()
        if not email or not password:
            flash("Email and password are required", "error")
            return render_template("register.html")
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            flash("An account with that email already exists", "error")
            return render_template("register.html")
        pw_hash = generate_password_hash(password)
        cur = db.execute(
            "INSERT INTO users (email, password_hash, full_name, country) VALUES (?,?,?,?)",
            (email, pw_hash, full_name, country),
        )
        db.commit()
        user_id = cur.lastrowid
        session["user_id"] = user_id

        # Try to provision a dedicated virtual account right away; harmless if it fails
        # (dashboard will retry next visit via ensure_user_has_dva).
        if PAYSTACK_SECRET_KEY:
            result = create_paystack_customer_and_dva(user_id, email, full_name)
            if result and result.get("account_number"):
                db.execute(
                    "UPDATE users SET paystack_customer_code=?, dva_account_number=?, dva_bank_name=?, dva_account_name=? WHERE id=?",
                    (result["customer_code"], result["account_number"], result["bank_name"], result["account_name"], user_id),
                )
                db.commit()

        flash("Welcome to IPNET!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        flash("Invalid email or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# User dashboard & wallet
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    user = ensure_user_has_dva(user)

    orders = db.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()

    orders_with_creds = []
    for o in orders:
        creds = db.execute("SELECT * FROM order_credentials WHERE order_id=?", (o["id"],)).fetchall()
        orders_with_creds.append({"order": o, "items": json_lib.loads(o["items_json"]), "credentials": creds})

    tx = db.execute(
        "SELECT * FROM wallet_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
        (user["id"],),
    ).fetchall()

    return render_template(
        "dashboard.html", user=user, orders_with_creds=orders_with_creds, transactions=tx,
        ngn_rate=NGN_PER_USD, paystack_configured=bool(PAYSTACK_SECRET_KEY),
    )


@app.route("/wallet/webhook", methods=["POST"])
def wallet_webhook():
    """Paystack server-to-server webhook. Handles two kinds of charge.success events:
    1. A transfer straight into a user's Dedicated Virtual Account (the normal flow
       here) — matched by the receiving account number, credited in USD-equivalent.
    2. A one-off hosted-checkout transaction (if you ever re-enable that flow) —
       matched by tx_ref against wallet_transactions.
    Configure this URL in the Paystack dashboard: Settings -> API Keys & Webhooks."""
    import hashlib
    import hmac

    raw_body = request.get_data()
    if PAYSTACK_SECRET_KEY:
        signature = request.headers.get("x-paystack-signature", "")
        computed = hmac.new(PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(signature, computed):
            return jsonify({"error": "invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    if event.get("event") != "charge.success":
        return jsonify({"received": True})

    data = event.get("data", {})
    db = get_db()

    # Case 1: inbound transfer to a Dedicated Virtual Account
    authorization = data.get("authorization", {}) or {}
    receiving_account = authorization.get("receiver_bank_account_number") or data.get("metadata", {}).get("receiver_account_number")
    customer_email = (data.get("customer", {}) or {}).get("email", "")
    paystack_ref = str(data.get("id", data.get("reference", "")))
    ngn_amount = (data.get("amount", 0) or 0) / 100.0  # kobo -> naira

    # Idempotency guard: skip if we've already recorded this Paystack transaction id
    already = db.execute("SELECT id FROM wallet_transactions WHERE provider_ref=?", (paystack_ref,)).fetchone()
    if already:
        return jsonify({"received": True, "note": "already processed"})

    user = None
    if receiving_account:
        user = db.execute("SELECT * FROM users WHERE dva_account_number=?", (receiving_account,)).fetchone()
    if not user and customer_email:
        user = db.execute("SELECT * FROM users WHERE email=?", (customer_email.lower(),)).fetchone()

    if user and ngn_amount > 0:
        usd_amount = round(ngn_amount / NGN_PER_USD, 2)
        tx_ref = "IPNW-" + secrets.token_hex(8).upper()
        db.execute(
            "INSERT INTO wallet_transactions (user_id, tx_ref, kind, amount, currency, status, provider, provider_ref, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user["id"], tx_ref, "funding", usd_amount, "NGN", "success", "paystack", paystack_ref,
             f"Bank transfer received: ₦{ngn_amount:,.2f}"),
        )
        db.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?", (usd_amount, user["id"]))
        db.commit()

    return jsonify({"received": True})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["is_admin"] = True
            nxt = request.args.get("next") or url_for("admin_dashboard")
            return redirect(nxt)
        flash("Invalid credentials", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    stats = {
        "categories": db.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"],
        "listings": db.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"],
        "orders": db.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
        "pending_orders": db.execute("SELECT COUNT(*) c FROM orders WHERE status='pending'").fetchone()["c"],
    }
    recent_orders = db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10").fetchall()
    return render_template(
        "admin_dashboard.html", stats=stats, recent_orders=recent_orders,
        current_logo=get_setting("site_logo", ""), current_favicon=get_setting("site_favicon", ""),
    )


@app.route("/admin/site-logo", methods=["POST"])
@admin_required
def admin_update_site_logo():
    logo_path = save_uploaded_image(request.files.get("logo_file"), "site")
    if logo_path:
        set_setting("site_logo", logo_path)
        flash("Site logo updated", "success")
    else:
        flash("Please choose a valid image file (png, jpg, jpeg, gif, webp)", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/site-favicon", methods=["POST"])
@admin_required
def admin_update_site_favicon():
    favicon_path = save_uploaded_image(request.files.get("favicon_file"), "site")
    if favicon_path:
        set_setting("site_favicon", favicon_path)
        flash("Favicon updated", "success")
    else:
        flash("Please choose a valid image file (ico, png)", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/categories")
@admin_required
def admin_categories():
    db = get_db()
    section = request.args.get("section", "")
    if section:
        cats = db.execute("SELECT * FROM categories WHERE section=? ORDER BY sort_order", (section,)).fetchall()
    else:
        cats = db.execute("SELECT * FROM categories ORDER BY section, sort_order").fetchall()
    return render_template("admin_categories.html", categories=cats, section_labels=SECTION_LABELS, current_section=section)


@app.route("/admin/categories/add", methods=["POST"])
@admin_required
def admin_add_category():
    db = get_db()
    name = request.form.get("name", "").strip()
    section = request.form.get("section", "other")
    icon = ""
    logo_path = save_uploaded_image(request.files.get("logo_file"), "categories")
    if name:
        slug = slugify(f"{section}-{name}-{secrets.token_hex(2)}")
        db.execute(
            "INSERT INTO categories (name, slug, section, icon, icon_image) VALUES (?,?,?,?,?)",
            (name, slug, section, icon, logo_path or ""),
        )
        db.commit()
        flash(f"Category '{name}' added", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/<int:cat_id>/logo", methods=["POST"])
@admin_required
def admin_update_category_logo(cat_id):
    db = get_db()
    logo_path = save_uploaded_image(request.files.get("logo_file"), "categories")
    if logo_path:
        db.execute("UPDATE categories SET icon_image=? WHERE id=?", (logo_path, cat_id))
        db.commit()
        flash("Logo updated", "success")
    else:
        flash("Please choose a valid image file (png, jpg, jpeg, gif, webp)", "error")
    return redirect(url_for("admin_categories"))


@app.route("/admin/categories/<int:cat_id>/delete", methods=["POST"])
@admin_required
def admin_delete_category(cat_id):
    db = get_db()
    db.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    db.commit()
    flash("Category deleted", "success")
    return redirect(url_for("admin_categories"))


@app.route("/admin/listings")
@admin_required
def admin_listings():
    db = get_db()
    cat_id = request.args.get("category_id")
    base = (
        "SELECT l.*, c.name as cat_name, c.section as section, g.name as game_name, p.name as platform_name "
        "FROM listings l JOIN categories c ON c.id=l.category_id LEFT JOIN games g ON g.id=l.game_id "
        "LEFT JOIN platforms p ON p.id=l.platform_id "
    )
    if cat_id:
        listings = db.execute(base + "WHERE l.category_id=? ORDER BY l.id DESC", (cat_id,)).fetchall()
    else:
        listings = db.execute(base + "ORDER BY l.id DESC LIMIT 200").fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY section, sort_order").fetchall()
    games = db.execute("SELECT * FROM games ORDER BY name").fetchall()
    platforms = db.execute("SELECT * FROM platforms ORDER BY name").fetchall()
    gallery_counts = {}
    if listings:
        placeholders = ",".join("?" for _ in listings)
        for row in db.execute(
            f"SELECT listing_id, COUNT(*) c FROM listing_images WHERE listing_id IN ({placeholders}) GROUP BY listing_id",
            [l["id"] for l in listings],
        ).fetchall():
            gallery_counts[row["listing_id"]] = row["c"]
    return render_template(
        "admin_listings.html", listings=listings, categories=categories, selected_cat=cat_id,
        games=games, platforms=platforms, gallery_counts=gallery_counts, section_labels=SECTION_LABELS,
    )


@app.route("/admin/listings/add", methods=["POST"])
@admin_required
def admin_add_listing():
    db = get_db()
    category_id = request.form.get("category_id")
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    price = request.form.get("price", "0").strip()
    stock = request.form.get("stock", "999").strip()
    image_url = request.form.get("image_url", "").strip()
    game_id = request.form.get("game_id") or None
    platform_id = request.form.get("platform_id") or None
    uploaded_path = save_uploaded_image(request.files.get("image_file"), "listings")
    final_image = uploaded_path or image_url
    try:
        price_f = float(price)
        stock_i = int(stock)
    except ValueError:
        flash("Invalid price or stock", "error")
        return redirect(url_for("admin_listings"))

    if title and category_id:
        cur = db.execute(
            "INSERT INTO listings (category_id, title, description, price, stock, image_url, game_id, platform_id) VALUES (?,?,?,?,?,?,?,?)",
            (category_id, title, description, price_f, stock_i, final_image, game_id, platform_id),
        )
        listing_id = cur.lastrowid
        for i, f in enumerate(request.files.getlist("gallery_files")):
            path = save_uploaded_image(f, "listings")
            if path:
                db.execute(
                    "INSERT INTO listing_images (listing_id, image_url, sort_order) VALUES (?,?,?)",
                    (listing_id, path, i),
                )
        db.commit()
        flash(f"Listing '{title}' added", "success")
    return redirect(url_for("admin_listings", category_id=category_id))


@app.route("/admin/listings/<int:listing_id>/edit", methods=["POST"])
@admin_required
def admin_edit_listing(listing_id):
    db = get_db()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    price = request.form.get("price", "0").strip()
    stock = request.form.get("stock", "0").strip()
    is_active = 1 if request.form.get("is_active") == "on" else 0
    image_url = request.form.get("image_url", "").strip()
    game_id = request.form.get("game_id") or None
    platform_id = request.form.get("platform_id") or None
    uploaded_path = save_uploaded_image(request.files.get("image_file"), "listings")
    try:
        price_f = float(price)
        stock_i = int(stock)
    except ValueError:
        flash("Invalid price or stock", "error")
        return redirect(url_for("admin_listings"))

    final_image = uploaded_path or image_url
    db.execute(
        "UPDATE listings SET title=?, description=?, price=?, stock=?, is_active=?, image_url=?, game_id=?, platform_id=? WHERE id=?",
        (title, description, price_f, stock_i, is_active, final_image, game_id, platform_id, listing_id),
    )
    for i, f in enumerate(request.files.getlist("gallery_files")):
        path = save_uploaded_image(f, "listings")
        if path:
            db.execute(
                "INSERT INTO listing_images (listing_id, image_url, sort_order) VALUES (?,?,?)",
                (listing_id, path, i),
            )
    db.commit()
    flash("Listing updated", "success")
    return redirect(url_for("admin_listings"))


@app.route("/admin/listings/<int:listing_id>/delete", methods=["POST"])
@admin_required
def admin_delete_listing(listing_id):
    db = get_db()
    db.execute("DELETE FROM listings WHERE id=?", (listing_id,))
    db.commit()
    flash("Listing deleted", "success")
    return redirect(url_for("admin_listings"))


@app.route("/admin/listings/<int:listing_id>/images", methods=["GET"])
@admin_required
def admin_listing_images(listing_id):
    db = get_db()
    listing = db.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if not listing:
        return "Not found", 404
    images = db.execute(
        "SELECT * FROM listing_images WHERE listing_id=? ORDER BY sort_order, id", (listing_id,)
    ).fetchall()
    return render_template("admin_listing_images.html", listing=listing, images=images)


@app.route("/admin/listings/<int:listing_id>/images/add", methods=["POST"])
@admin_required
def admin_add_listing_images(listing_id):
    db = get_db()
    files = request.files.getlist("gallery_files")
    added = 0
    for f in files:
        path = save_uploaded_image(f, "listings")
        if path:
            db.execute(
                "INSERT INTO listing_images (listing_id, image_url) VALUES (?,?)", (listing_id, path)
            )
            added += 1
    db.commit()
    flash(f"Added {added} screenshot(s)" if added else "Choose at least one valid image", "success" if added else "error")
    return redirect(url_for("admin_listing_images", listing_id=listing_id))


@app.route("/admin/listings/images/<int:image_id>/delete", methods=["POST"])
@admin_required
def admin_delete_listing_image(image_id):
    db = get_db()
    img = db.execute("SELECT * FROM listing_images WHERE id=?", (image_id,)).fetchone()
    db.execute("DELETE FROM listing_images WHERE id=?", (image_id,))
    db.commit()
    if img:
        return redirect(url_for("admin_listing_images", listing_id=img["listing_id"]))
    return redirect(url_for("admin_listings"))


@app.route("/admin/games")
@admin_required
def admin_games():
    db = get_db()
    games = db.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM listings l WHERE l.game_id=g.id) as listing_count "
        "FROM games g ORDER BY g.sort_order"
    ).fetchall()
    return render_template("admin_games.html", games=games)


@app.route("/admin/games/add", methods=["POST"])
@admin_required
def admin_add_game():
    db = get_db()
    name = request.form.get("name", "").strip()
    logo_path = save_uploaded_image(request.files.get("image_file"), "games")
    if name:
        max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) m FROM games").fetchone()["m"]
        db.execute(
            "INSERT INTO games (name, slug, image_url, sort_order) VALUES (?,?,?,?)",
            (name, slugify(f"{name}-{secrets.token_hex(2)}"), logo_path or "", max_order + 1),
        )
        db.commit()
        flash(f"Game '{name}' added", "success")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/<int:game_id>/image", methods=["POST"])
@admin_required
def admin_update_game_image(game_id):
    db = get_db()
    logo_path = save_uploaded_image(request.files.get("image_file"), "games")
    if logo_path:
        db.execute("UPDATE games SET image_url=? WHERE id=?", (logo_path, game_id))
        db.commit()
        flash("Game image updated", "success")
    else:
        flash("Please choose a valid image file", "error")
    return redirect(url_for("admin_games"))


@app.route("/admin/games/<int:game_id>/delete", methods=["POST"])
@admin_required
def admin_delete_game(game_id):
    db = get_db()
    db.execute("UPDATE listings SET game_id=NULL WHERE game_id=?", (game_id,))
    db.execute("DELETE FROM games WHERE id=?", (game_id,))
    db.commit()
    flash("Game removed", "success")
    return redirect(url_for("admin_games"))


@app.route("/admin/platforms")
@admin_required
def admin_platforms():
    db = get_db()
    platforms = db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM listings l WHERE l.platform_id=p.id) as listing_count "
        "FROM platforms p ORDER BY p.sort_order"
    ).fetchall()
    return render_template("admin_platforms.html", platforms=platforms)


@app.route("/admin/platforms/add", methods=["POST"])
@admin_required
def admin_add_platform():
    db = get_db()
    name = request.form.get("name", "").strip()
    logo_path = save_uploaded_image(request.files.get("image_file"), "platforms")
    if name:
        max_order = db.execute("SELECT COALESCE(MAX(sort_order),0) m FROM platforms").fetchone()["m"]
        db.execute(
            "INSERT INTO platforms (name, slug, image_url, sort_order) VALUES (?,?,?,?)",
            (name, slugify(f"{name}-{secrets.token_hex(2)}"), logo_path or "", max_order + 1),
        )
        db.commit()
        flash(f"Platform '{name}' added", "success")
    return redirect(url_for("admin_platforms"))


@app.route("/admin/platforms/<int:platform_id>/image", methods=["POST"])
@admin_required
def admin_update_platform_image(platform_id):
    db = get_db()
    logo_path = save_uploaded_image(request.files.get("image_file"), "platforms")
    if logo_path:
        db.execute("UPDATE platforms SET image_url=? WHERE id=?", (logo_path, platform_id))
        db.commit()
        flash("Platform image updated", "success")
    else:
        flash("Please choose a valid image file", "error")
    return redirect(url_for("admin_platforms"))


@app.route("/admin/platforms/<int:platform_id>/delete", methods=["POST"])
@admin_required
def admin_delete_platform(platform_id):
    db = get_db()
    db.execute("UPDATE listings SET platform_id=NULL WHERE platform_id=?", (platform_id,))
    db.execute("DELETE FROM platforms WHERE id=?", (platform_id,))
    db.commit()
    flash("Platform removed", "success")
    return redirect(url_for("admin_platforms"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    db = get_db()
    orders = db.execute(
        "SELECT o.*, u.email as user_email FROM orders o LEFT JOIN users u ON u.id=o.user_id "
        "ORDER BY o.created_at DESC LIMIT 300"
    ).fetchall()
    return render_template("admin_orders.html", orders=orders)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_update_order_status(order_id):
    db = get_db()
    status = request.form.get("status", "pending")
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    db.commit()
    flash("Order status updated", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/orders/<int:order_id>")
@admin_required
def admin_order_detail(order_id):
    db = get_db()
    order = db.execute(
        "SELECT o.*, u.email as user_email FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?",
        (order_id,),
    ).fetchone()
    if not order:
        return "Not found", 404
    items = json_lib.loads(order["items_json"])
    creds = db.execute("SELECT * FROM order_credentials WHERE order_id=?", (order_id,)).fetchall()
    return render_template("admin_order_detail.html", order=order, items=items, credentials=creds)


@app.route("/admin/orders/<int:order_id>/deliver", methods=["POST"])
@admin_required
def admin_deliver_credentials(order_id):
    db = get_db()
    item_title = request.form.get("item_title", "").strip()
    username_or_email = request.form.get("username_or_email", "").strip()
    password = request.form.get("password", "").strip()
    extra_info = request.form.get("extra_info", "").strip()
    if item_title and (username_or_email or password):
        db.execute(
            "INSERT INTO order_credentials (order_id, item_title, username_or_email, password, extra_info) "
            "VALUES (?,?,?,?,?)",
            (order_id, item_title, username_or_email, password, extra_info),
        )
        db.execute("UPDATE orders SET status='delivered' WHERE id=? AND status IN ('paid','pending')", (order_id,))
        db.commit()
        flash("Credentials delivered to buyer's dashboard", "success")
    else:
        flash("Enter at least a username/email or password", "error")
    return redirect(url_for("admin_order_detail", order_id=order_id))


@app.route("/admin/orders/credentials/<int:cred_id>/delete", methods=["POST"])
@admin_required
def admin_delete_credentials(cred_id):
    db = get_db()
    cred = db.execute("SELECT * FROM order_credentials WHERE id=?", (cred_id,)).fetchone()
    db.execute("DELETE FROM order_credentials WHERE id=?", (cred_id,))
    db.commit()
    flash("Credential entry removed", "success")
    if cred:
        return redirect(url_for("admin_order_detail", order_id=cred["order_id"]))
    return redirect(url_for("admin_orders"))


@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 300").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/adjust", methods=["POST"])
@admin_required
def admin_adjust_wallet(user_id):
    db = get_db()
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        flash("Invalid amount", "error")
        return redirect(url_for("admin_users"))
    note = request.form.get("note", "Manual adjustment by admin").strip()
    tx_ref = "IPNA-" + secrets.token_hex(8).upper()
    db.execute(
        "INSERT INTO wallet_transactions (user_id, tx_ref, kind, amount, currency, status, provider, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, tx_ref, "adjustment", amount, "USD", "success", "manual", note),
    )
    db.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?", (amount, user_id))
    db.commit()
    flash(f"Wallet adjusted by ${amount:.2f}", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/retry-dva", methods=["POST"])
@admin_required
def admin_retry_dva(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash("User not found", "error")
        return redirect(url_for("admin_users"))
    if not PAYSTACK_SECRET_KEY:
        flash("Set PAYSTACK_SECRET_KEY before creating funding accounts", "error")
        return redirect(url_for("admin_users"))
    result = create_paystack_customer_and_dva(user["id"], user["email"], user["full_name"])
    if result and result.get("account_number"):
        db.execute(
            "UPDATE users SET paystack_customer_code=?, dva_account_number=?, dva_bank_name=?, dva_account_name=? WHERE id=?",
            (result["customer_code"], result["account_number"], result["bank_name"], result["account_name"], user_id),
        )
        db.commit()
        flash(f"Funding account created: {result['account_number']} ({result['bank_name']})", "success")
    else:
        flash("Could not create a funding account. Check your Paystack account is approved for Dedicated Virtual Accounts.", "error")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
