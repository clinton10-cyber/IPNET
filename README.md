# IPNET — Digital Marketplace

Sell gaming accounts, game boosting/leveling/credits (50+ games preloaded), and social
media growth services (TikTok, YouTube, Instagram, Facebook, Twitter/X, Telegram,
Snapchat, WhatsApp, Twitch, LinkedIn), plus websites and other digital goods —
all from one Flask + SQLite app. Works for buyers in any country (USD pricing,
wallet funded via Paystack in NGN).

## Features
- Storefront with categories grouped into 5 sections: Gaming Accounts, Game Boost &
  Credits, Social Media Services, Websites, Other Digital Goods.
- 50 popular games pre-seeded as categories in both "Gaming Accounts" and
  "Game Boost & Credits" sections.
- Social media section pre-seeded with 10 platforms x multiple service types each
  (followers, likes, views, etc.)
- **User accounts** — register/login, each user has a wallet balance.
- **Wallet funding via Paystack** — user enters a USD amount, gets redirected to
  Paystack (charged in NGN), and on successful payment the wallet is **automatically
  credited** via a Paystack webhook (with a redirect-callback fallback in case the
  webhook doesn't fire in time).
- **Checkout pays from wallet balance** when logged in (guest checkout without an
  account still works too, using the old "we'll contact you" flow).
- **User dashboard** (`/dashboard`) — wallet balance + funding history, and "My
  Purchases" showing delivered login credentials for each order.
- **Admin delivers credentials per order** — admin opens an order, types in
  username/password/notes for a purchased item, and it appears instantly in the
  buyer's dashboard, marking the order as delivered.
- Admin panel (`/admin`) to add/edit/delete categories, add/edit/delete listings,
  set **prices**, stock, and toggle active/inactive. View/manage orders, users, and
  manually adjust any user's wallet balance (e.g. for refunds or off-platform payments).

## Local run

```bash
pip install -r requirements.txt
python app.py
```

Visit http://localhost:5000 — admin panel at http://localhost:5000/admin

Default admin login (change these before deploying!):
- username: `admin`
- password: `changeme123`

Set your own via environment variables `ADMIN_USERNAME` and `ADMIN_PASSWORD`.

## Setting up Paystack (for automatic wallet funding)

1. Create a free account at https://paystack.com and complete verification for a
   Nigerian business/individual account.
2. Go to Settings → API Keys & Webhooks. Copy your **Secret Key** and **Public Key**
   (use test keys first to try it out, live keys once you're ready to go live).
3. Set environment variables on your server (locally or on Render):
   - `PAYSTACK_SECRET_KEY` = your secret key
   - `PAYSTACK_PUBLIC_KEY` = your public key
   - `NGN_PER_USD` = the NGN-to-USD rate you want to charge at (e.g. `1600`) — update
     this periodically as exchange rates move, either as an env var or by editing it
     directly in `app.py`.
4. In the same Paystack dashboard page, set your **Webhook URL** to:
   `https://YOUR-DOMAIN/wallet/webhook`
   This is what makes crediting automatic — Paystack calls this URL the moment a
   payment succeeds, and the app verifies + credits the wallet server-side.
5. Test it: log in on the site, go to Dashboard → Fund Wallet, enter an amount, and
   complete a test payment. Your balance should update within a few seconds.

Without these keys set, wallet funding will show a friendly "payments not configured
yet" message — everything else (browsing, admin, manual wallet adjustments) still
works fine.

## Deploy to Render

1. Push this project to a GitHub repo.
2. On Render, choose "New +" → "Blueprint" and point it at the repo — it will pick
   up `render.yaml` automatically and create the service, generating a random
   admin password and secret key for you (check the Environment tab after deploy
   to see/set `ADMIN_PASSWORD`).
   - Alternatively, choose "New +" → "Web Service", connect the repo, and set:
     - Build command: `pip install -r requirements.txt`
     - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
     - Add environment variables `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`,
       `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY`, `NGN_PER_USD`.
3. After deploying, update your Paystack webhook URL to point at your live Render
   domain (e.g. `https://ipnet.onrender.com/wallet/webhook`).
4. Render's free tier uses an ephemeral filesystem — the SQLite file (`ipnet.db`)
   will reset on redeploy, which means users, wallets, and orders reset too. For
   persistent data across deploys, either:
   - Add a Render Disk (Paid) mounted at the project directory, or
   - Migrate to Render's managed PostgreSQL later (the code is plain SQL so this
     is a straightforward swap).

## Managing your store
- Go to `/admin`, log in.
- **Categories**: add new ones (e.g. a new game, a new social platform service)
  under any of the 5 sections. Delete ones you don't need.
- **Listings & Prices**: this is where you set what's actually for sale and its
  price — pick a category, add a listing (title, description, price, stock),
  and edit price/stock/active status inline anytime.
- **Orders**: see everything customers submitted at checkout, with their contact
  info and linked account (if logged in). Click "Manage" on any order to deliver
  login credentials for purchased accounts — this shows up instantly in the
  buyer's dashboard.
- **Users & Wallets**: see every registered user and their wallet balance, and
  manually credit/debit a wallet (positive or negative amount) — useful for
  refunds, bonuses, or crediting a payment that came in outside Paystack.

## Customizing
- Site name/branding: edit `templates/base.html` (`<title>`, header logo) and
  `static/css/style.css` (`--accent`, `--accent2` colors).
- Add more games/platforms: either add via the admin panel, or edit the seed
  lists (`GAMES_50`, `SOCIAL_PLATFORMS`, etc.) near the top of `app.py` before
  first run (seeding only happens once, when the categories table is empty).

