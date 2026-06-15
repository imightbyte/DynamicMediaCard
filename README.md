# Dynamic Media Card Tool

Schedule updates to your X (Twitter) Website Cards and App Download Cards at a future time.

This app has the **exact same theme, styling, and footer** as [Post Xploder](https://promoted-threads.onrender.com/).

**Top-left branding:** "Dynamic Media Card Tool"  
**Sign-in:** Pure X OAuth only. Clicking "Sign in with X" goes directly to X's authorization page. No email/password or other providers.

**No database** — tokens and schedules are kept in-memory only. Restarting the server clears them (you just sign in again). This makes local setup trivial and works great on platforms like Render where disks are ephemeral unless you pay for persistence.

## Features

- Sign in with X (OAuth 2.0 PKCE)
- Select / enter your X Ads Account ID (auto-detects accounts you have access to via the Ads API)
- Paste a post link containing a **Website Card** or **App Card** (validated via `https://api.x.com/2/tweets` + `card_uri`)
- Loads the existing card details (title, media_id, URL) — shown read-only
- Edit the three values and pick a future time to apply the change
- On media_id change: automatically validates that the new media has **identical dimensions** (width × height) as the original card's media
- URL validation
- Save the schedule — at the chosen time the app calls:

  `PUT https://ads-api.x.com/12/accounts/{account_id}/cards/{card_id}`

- View, cancel, and manually trigger scheduled updates
- Background scheduler (in-memory only — schedules are lost on restart; just sign in again and re-create them)

## Exact Footer

The footer is copied verbatim from the reference (X links, X Ads links, "How your data flows" copy, and the final takedown notice).

## Quick Start (Local)

1. Clone and install:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy env and fill values:

```bash
cp .env.example .env
```

Edit `.env` (see the big comments inside `.env.example` for the exact steps):

- `X_CLIENT_ID` and `X_CLIENT_SECRET` — from your X App (OAuth 2.0 section in the developer portal)
- `X_REDIRECT_URI` — must be **identical** to the Callback URI you registered in the X App
  - Local: `http://127.0.0.1:8000/callback`
  - Later on Render: `https://your-app.onrender.com/callback`
- `SECRET_KEY` — any long random string (use `openssl rand -hex 32` or the Python one-liner in .env.example)

3. In the X Developer Portal:
   - Your App must have **OAuth 2.0** enabled (User authentication settings)
   - Callback URI(s) added
   - The Project/App must have the **Ads API** product enabled
   - Scopes: at least `tweet.read`, `users.read`, `offline.access`

4. Run:

```bash
uvicorn main:app --reload --port 8000
```

5. Open http://127.0.0.1:8000

6. Click **Sign in with X**.
   - You will be redirected to X (this is normal for OAuth).
   - After you approve, X will automatically redirect you **back** to this tool (the registered Callback URI).
   - If you stay on X's site instead of coming back, the most common cause is that the Callback URI registered in your X App does not exactly match the `X_REDIRECT_URI` value in your `.env`.

The tool only uses X OAuth — there are no other sign-in options.

## Deploy (Render, Railway, Fly, etc.)

- Use the same environment variables (see .env.example for details)
- No database: schedules and tokens are kept in memory only. They are lost when the dyno/process restarts (this is normal on free Render and actually simplifies local + deploy).
- The scheduler runs inside the web process. On Render free tier the service sleeps; for reliable timing you will eventually want a cheap always-on instance.
- When you later push to GitHub + deploy on Render, just set the four variables (X_CLIENT_ID, X_CLIENT_SECRET, X_REDIRECT_URI with your production callback, and a strong SECRET_KEY).

Example uvicorn start command (no reload in prod):

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Add a `Procfile`:

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

## How the Flow Works (per spec)

1. User clicks **Sign in with X** → goes directly to X OAuth (no other sign-in options exist).
2. User enters (or selects detected) X Ads Account ID.
3. User pastes an X post link that uses a website/app card.
4. App calls `GET https://api.x.com/2/tweets/{id}?tweet.fields=card_uri,...`
   - If `card_uri` is absent or not a website/app card → error + block next step.
5. Parse `card_uri` → card_id, then fetch full card via Ads API (now also returns whether the creative is image or video).
6. Display current values **uneditable** (including media type: image vs video).
7. User provides new title / media_id / URL + a future `scheduled_at`.
8. When entering a new media_id the app checks **both**:
   - Exact same dimensions (width × height)
   - Same media type (image or video) as the original card
9. URL must be a valid http(s) URL.
10. Save → the in-memory schedule is created. When the time arrives the background poller executes the PUT to the Ads API.

## Security Notes

- Access + refresh tokens are stored encrypted at rest using the `SECRET_KEY` (Fernet).
- Never commit your real `.env`.
- In production, rotate keys, use HTTPS only, and consider a proper secrets manager + Postgres.

## Tech

- FastAPI + Jinja2 + Tailwind (CDN) + Alpine.js (to closely match the reference implementation style)
- APScheduler for reliable in-process scheduling
- httpx for all X / Ads API calls
- SQLite by default

## Troubleshooting

- "This post does not contain a website card or app card": the tweet must have `card_uri` in the v2 response. Make sure the post is actually using a card created in ads.x.com (website or app download card) and is attached to the tweet.
- Dimension check fails: the new media_id must come from the same Ads account's media library and have identical pixel width/height.
- 403/401 on Ads calls: ensure the signed-in X user has access to the Ads account, the App has Ads API enabled, and tokens are valid (try sign out + sign in again).
- Token refresh: the app auto-refreshes using the stored refresh_token before executing a scheduled update.

## Credits

Theme, nav, colors, buttons, and **footer** are reproduced exactly from https://promoted-threads.onrender.com/ (Post Xploder) per the request.

---

Made for scheduling dynamic media card (website / app card) updates on X.
