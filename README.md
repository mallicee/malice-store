# MALICE Store

Telegram Mini App / storefront for the MALICE demo shop. Includes a static HTML frontend and an aiogram bot that reads prices from SQLite, records orders, and sends notifications to the configured admin.

> **Current limitations:** online payments are not connected. Orders are accepted with status `Новый` and require manual follow-up. This is a portfolio/demo project, not a production-ready payment system.

## Features

- Responsive dark streetwear storefront
- Four available products (hoodie discontinued): T-shirt, longsleeve, cap, sticker pack
- Size selection for clothing
- Telegram Mini App open/menu button and order hand-off via Telegram start link (`m1_...`)
- SQLite order history and administrator notifications
- Server-side lookup of product ID and price; browser-provided prices are not trusted
- Existing database rows for discontinued products are hidden from the sellable catalogue rather than deleted

## Repository structure

```text
MALICE/
├── bot/
│   └── malice_bot.py
├── website/
│   └── index.html
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

The live website currently uses the existing Netlify image-optimization endpoint for product images. The source HTML is included, but the original `img/` image assets were not available in this export. Keep that endpoint available or copy the original image assets into the site and update image URLs before removing the old Netlify site.

## Configure the bot (Windows / Python 3.13)

1. Install Python 3.13.
2. In the repository root, copy `.env.example` to `.env`.
3. Edit `.env` and set:
   - `BOT_TOKEN`: token from BotFather. **Do not commit it.**
   - `ADMIN_ID`: your numeric Telegram user ID.
   - `WEBAPP_URL`: the deployed Mini App URL.
4. Install dependencies:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   py -m pip install -r requirements.txt
   ```

5. Run the bot from the repository root:

   ```powershell
   py -3.13 bot\malice_bot.py
   ```

If there is no local database, the bot creates `bot/malice_v3.db` and seeds a fresh catalogue. If migrating an existing private MALICE setup, **make a backup first**, then copy your existing `malice_v3.db` into `bot/` before running. The production database must remain private and must not be committed.

The bot will update Telegram's chat-menu Mini App URL on startup and also sends a reply-keyboard button with the same URL. The top-level `Open App` button is configured via Telegram's Bot API menu-button setting; verify it after changing `WEBAPP_URL`.

## Deploy the website

Deploy the contents of `website/` as static assets to Cloudflare Pages or another static host. The URL must match `WEBAPP_URL` in `.env`. The HTML currently references images from the existing Netlify image optimizer, so transfer the original images and update the URLs before removing that host.

## Product catalogue

| ID | Product | Price |
|---:|---|---:|
| 1 | Футболка MALICE | 1290 ₽ |
| 2 | Лонгслив MALICE | 1790 ₽ |
| 4 | Кепка Mono | 990 ₽ |
| 5 | Стикерпак | 790 ₽ |

ID 3 remains unused because the discontinued hoodie had that ID in the previous catalogue. It is intentionally not reused, to preserve old order history.

## GitHub safety checklist

- Upload the contents of this project folder, not the ZIP archive itself.
- Do not include `.env`, `malice_v3.db`, logs, backups, or Telegram tokens.
- `.gitignore` excludes these files, but verify the upload list before committing.
- If a token was ever committed publicly, revoke it with BotFather and replace it; deleting it from the latest file alone is not enough.
