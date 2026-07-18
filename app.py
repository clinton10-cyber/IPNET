import os
import sqlite3
import secrets
import json as json_lib
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, g, render_template, request, jsonify, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "ipnet.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

# Paystack config — get these from https://dashboard.paystack.com/#/settings/developer
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
NGN_PER_USD = float(os.environ.get("NGN_PER_USD", "1600"))  # used to show wallet in USD equiv; adjust as needed

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
    icon TEXT DEFAULT '🎮',
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
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.commit()
    seed_if_empty(db)
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

    # Gaming accounts categories
    for c in GAMING_ACCOUNT_CATEGORIES:
        add_cat(c, "gaming_accounts", "🕹️")

    # Per-game gaming account categories (50 games)
    for game in GAMES_50:
        add_cat(f"{game} Accounts", "gaming_accounts", "🎮")

    # Game boosting / leveling / credits (50 games)
    for game in GAMES_50:
        add_cat(f"{game} Boost & Credits", "game_boost", "⚡")

    for c in BOOST_CATEGORIES:
        add_cat(c, "game_boost", "🚀")

    # Social media, per platform per service
    for platform, services in SOCIAL_PLATFORMS.items():
        for service in services:
            add_cat(f"{platform} {service}", "social_media", "📱")

    # Websites
    for c in WEBSITE_CATEGORIES:
        add_cat(c, "websites", "🌐")

    # Other digital goods
    for c in OTHER_CATEGORIES:
        add_cat(c, "other", "💳")

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
    return {"current_user": current_user()}


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
        "SELECT l.*, c.name as cat_name, c.section as section FROM listings l "
        "JOIN categories c ON c.id = l.category_id WHERE l.is_active=1 ORDER BY l.created_at DESC LIMIT 8"
    ).fetchall()
    return render_template("index.html", sections=sections, featured=featured, section_labels=SECTION_LABELS)


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
    listings = db.execute(
        "SELECT * FROM listings WHERE category_id=? AND is_active=1 ORDER BY price ASC", (cat_id,)
    ).fetchall()
    return render_template("category.html", category=cat, listings=listings, label=SECTION_LABELS.get(cat["section"], ""))


@app.route("/search")
def search():
    db = get_db()
    q = request.args.get("q", "").strip()
    results = []
    if q:
        results = db.execute(
            "SELECT l.*, c.name as cat_name, c.section as section FROM listings l "
            "JOIN categories c ON c.id=l.category_id "
            "WHERE l.is_active=1 AND (l.title LIKE ? OR c.name LIKE ?) LIMIT 60",
            (f"%{q}%", f"%{q}%"),
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
        session["user_id"] = cur.lastrowid
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
        ngn_rate=NGN_PER_USD, paystack_public_key=PAYSTACK_PUBLIC_KEY,
    )


@app.route("/wallet/fund/init", methods=["POST"])
@login_required
def wallet_fund_init():
    """Initialize a Paystack transaction for wallet funding (amount in USD, converted to NGN)."""
    user = current_user()
    data = request.get_json(force=True)
    try:
        usd_amount = float(data.get("amount_usd", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    if usd_amount < 1:
        return jsonify({"error": "Minimum funding amount is $1"}), 400

    ngn_amount = round(usd_amount * NGN_PER_USD, 2)
    tx_ref = "IPNW-" + secrets.token_hex(8).upper()

    db = get_db()
    db.execute(
        "INSERT INTO wallet_transactions (user_id, tx_ref, kind, amount, currency, status, provider) "
        "VALUES (?,?,?,?,?,?,?)",
        (user["id"], tx_ref, "funding", usd_amount, "USD", "pending", "paystack"),
    )
    db.commit()

    if not PAYSTACK_SECRET_KEY:
        return jsonify({"error": "Payments are not configured yet. Set PAYSTACK_SECRET_KEY on the server."}), 503

    try:
        resp = requests.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={
                "email": user["email"],
                "amount": int(round(ngn_amount * 100)),  # kobo
                "reference": tx_ref,
                "callback_url": url_for("wallet_fund_callback", _external=True),
                "metadata": {"user_id": user["id"], "usd_amount": usd_amount},
            },
            timeout=15,
        )
        payload = resp.json()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not reach Paystack: {e}"}), 502

    if not payload.get("status"):
        return jsonify({"error": payload.get("message", "Could not start payment")}), 502

    return jsonify({
        "authorization_url": payload["data"]["authorization_url"],
        "tx_ref": tx_ref,
    })


def _credit_wallet_if_needed(tx_ref):
    """Verify a Paystack transaction and credit the wallet exactly once. Returns (ok, message)."""
    db = get_db()
    tx = db.execute("SELECT * FROM wallet_transactions WHERE tx_ref=?", (tx_ref,)).fetchone()
    if not tx:
        return False, "Unknown transaction reference"
    if tx["status"] == "success":
        return True, "Already credited"

    if not PAYSTACK_SECRET_KEY:
        return False, "Payments not configured"

    try:
        resp = requests.get(
            f"https://api.paystack.co/transaction/verify/{tx_ref}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            timeout=15,
        )
        payload = resp.json()
    except requests.RequestException as e:
        return False, f"Verification request failed: {e}"

    data = payload.get("data", {})
    if payload.get("status") and data.get("status") == "success":
        db.execute(
            "UPDATE wallet_transactions SET status='success', provider_ref=? WHERE tx_ref=?",
            (str(data.get("id", "")), tx_ref),
        )
        db.execute(
            "UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?",
            (tx["amount"], tx["user_id"]),
        )
        db.commit()
        return True, "Wallet credited"
    else:
        db.execute("UPDATE wallet_transactions SET status='failed' WHERE tx_ref=?", (tx_ref,))
        db.commit()
        return False, "Payment not successful"


@app.route("/wallet/fund/callback")
def wallet_fund_callback():
    """User is redirected here by Paystack after paying. We verify as a fallback to the webhook."""
    tx_ref = request.args.get("reference") or request.args.get("trxref")
    if tx_ref:
        ok, message = _credit_wallet_if_needed(tx_ref)
        flash(message if ok else f"Payment issue: {message}", "success" if ok else "error")
    return redirect(url_for("dashboard"))


@app.route("/wallet/webhook", methods=["POST"])
def wallet_webhook():
    """Paystack server-to-server webhook — this is what actually auto-credits reliably,
    even if the user closes their browser before the callback redirect fires.
    Configure this URL in the Paystack dashboard: Settings -> API Keys & Webhooks."""
    import hashlib
    import hmac

    if PAYSTACK_SECRET_KEY:
        signature = request.headers.get("x-paystack-signature", "")
        computed = hmac.new(
            PAYSTACK_SECRET_KEY.encode("utf-8"), request.get_data(), hashlib.sha512
        ).hexdigest()
        if not hmac.compare_digest(signature, computed):
            return jsonify({"error": "invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        tx_ref = event.get("data", {}).get("reference")
        if tx_ref:
            _credit_wallet_if_needed(tx_ref)

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
    return render_template("admin_dashboard.html", stats=stats, recent_orders=recent_orders)


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
    icon = request.form.get("icon", "🎮").strip() or "🎮"
    if name:
        slug = slugify(f"{section}-{name}-{secrets.token_hex(2)}")
        db.execute("INSERT INTO categories (name, slug, section, icon) VALUES (?,?,?,?)", (name, slug, section, icon))
        db.commit()
        flash(f"Category '{name}' added", "success")
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
    if cat_id:
        listings = db.execute(
            "SELECT l.*, c.name as cat_name FROM listings l JOIN categories c ON c.id=l.category_id "
            "WHERE l.category_id=? ORDER BY l.id DESC", (cat_id,)
        ).fetchall()
    else:
        listings = db.execute(
            "SELECT l.*, c.name as cat_name FROM listings l JOIN categories c ON c.id=l.category_id ORDER BY l.id DESC LIMIT 200"
        ).fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY section, sort_order").fetchall()
    return render_template("admin_listings.html", listings=listings, categories=categories, selected_cat=cat_id)


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
    try:
        price_f = float(price)
        stock_i = int(stock)
    except ValueError:
        flash("Invalid price or stock", "error")
        return redirect(url_for("admin_listings"))

    if title and category_id:
        db.execute(
            "INSERT INTO listings (category_id, title, description, price, stock, image_url) VALUES (?,?,?,?,?,?)",
            (category_id, title, description, price_f, stock_i, image_url),
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
    try:
        price_f = float(price)
        stock_i = int(stock)
    except ValueError:
        flash("Invalid price or stock", "error")
        return redirect(url_for("admin_listings"))

    db.execute(
        "UPDATE listings SET title=?, description=?, price=?, stock=?, is_active=?, image_url=? WHERE id=?",
        (title, description, price_f, stock_i, is_active, image_url, listing_id),
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


# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
