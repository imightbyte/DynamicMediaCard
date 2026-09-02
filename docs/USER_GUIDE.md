# Dynamic Media Card Tool

**Link:** https://dynamic-media-card.onrender.com/

## User Guide

Dynamic Media Card Tool lets you schedule an update to an existing X website card or app card — swapping its media, changing its title, and changing its destination URL — on a post you've already published. At the exact date and time you choose, the tool automatically applies the new creative to the card through the X Ads API.

You'll need an active X Ads account at [ads.x.com](https://ads.x.com) (required to read and update card creatives via the Ads API).

> **Note (X internal):** The X handle using this tool must have **DSO** as its service level.

## Signing In

- Open the app and click **Sign in with X**.
- A window will open on X asking you to authorize the app (the tool uses X OAuth 1.0a, which is required for the Ads API).
- Grant permission — you'll be redirected back to the tool and the scheduler interface will load.

Before you sign in, open x.com in the same browser and make sure you're logged into the exact X account that owns the post you want to change. The card can only be updated by an account with access to it.

**Your session expires for security.** You're signed out automatically after **60 minutes of inactivity**, and also whenever you **close the browser** — after that you'll need to **Sign in with X** again. This only affects access to the tool: any updates you've already scheduled will still run at their set time, even while you're signed out.

## Connecting Your X Ads Account

Once signed in, link an X Ads account at the top of the page. This must be set before you can validate a post.

- If the app detects Ads accounts for your X user, they appear as clickable **Detected accounts** chips — click one to select it. Use **reload** to re-fetch the list.
- You can also type an **Account ID** directly into the field (placeholder shows an example like `e.g. abc123def`, or the auto-detected ID while detecting).
- The status badge shows **Active** (green) when an Account ID is present, or **Required** (yellow) when one is still needed.
- You can find your Account ID at [ads.x.com](https://ads.x.com) → Account settings.

The Account ID field is what the tool actually uses for every action. Clicking a detected chip simply fills the field in; typing or editing the field overrides it. If no accounts are detected, you can always paste the ID manually.

## Loading a Post & Its Card

- Paste the post link into **X Post URL**. Full x.com or twitter.com `/status/` links are accepted (e.g. `https://x.com/username/status/1234567890123456789`).
- Set your **Ads Account ID** first — it's required before validation.
- Click **Validate Post & Load Card**.

The post must contain a website card or app card. If it does, the tool reads the live card and shows a read-only **Current Card** (uneditable) panel with the current Title, Destination URL, and a Media Preview. These are the values your scheduled update will replace.

> **Website cards vs. app cards.** On website cards you can update the media, title, and destination URL. App cards show only their media on X, so the tool hides **Title** and **Destination URL** in the Current Card panel (and in the Schedule Update form) — for app cards, an update changes the media only.

## Choosing the New Media

In the **Schedule Update** form, click **Select new media** to open the media picker. It has two tabs:

- **Browse library** — browse your X Ads media library, filtered to match the original card's media type (the header shows "Showing image/video media — matches original"). Type in the **Search media…** box and click **Search** to filter by name/file name, and use **Load more** to page through additional results.
- **Upload** — pick a local file to upload. A progress bar shows upload and processing status; videos and GIFs are transcoded on X's side, so the tool polls until processing finishes before the media is usable.

Selecting or uploading media fills in the **New Media Key**, and its type, aspect ratio, and preview load automatically.

When you load a card, the media field (along with New Title and New Destination URL) is **prefilled with the card's current values**, so you edit from the current creative rather than from blank fields. Leave a field as-is to keep it unchanged.

### Accepted formats and size limits

| Media type | Accepted formats | Max upload size |
| --- | --- | --- |
| Image | JPG, JPEG, PNG, WebP | 5 MB |
| Video | MP4, MOV, M4V, WebM | 512 MB |
| GIF | GIF | 512 MB (treated as video) |

The new media must match the original card's media type — you can't replace an image card's image with a video, or vice versa. If the types don't match, saving the schedule is rejected. (GIFs count as "video" for matching purposes.)

## Setting the New Title & Destination URL

- **New Title** — the card's headline/name (up to 70 characters).
- **New Destination URL** — where the card sends people who click it (placeholder `https://example.com/landing-page`). It must be a valid http(s) URL.

Both fields are **prefilled with the card's current values** when you load the post, so you're editing the current title/URL rather than starting from blank. If you leave a value the same as the current card, the tool simply won't change it — only the fields that differ are updated.

> **App cards:** because app cards expose only their media on X, the **New Title** and **New Destination URL** inputs are hidden for them (in the one-time form and in every series step). App-card updates change the media only; the title and URL are left unchanged and aren't required or validated.

## Scheduling the Update

The schedule form has a segmented control with two modes: **One-time update** and **Multiple updates**. A helper caption under the control explains the difference — *One-time update* is a single scheduled update, while *Multiple updates* is several scheduled updates to the same card, run in order as a series.

### One-time update

- Pick a date and a 24-hour time (HH:MM). The scheduled time must be in the future — a warning appears and saving is blocked if it's in the past.
- Choose the matching time zone offset (the dropdown lists GMT/UTC offsets).
- Click **Save Schedule**.

The update runs server-side at the exact time you set (the tool uses APScheduler's date trigger). Schedules are stored so that pending updates still run even if the server restarts before their time. In the schedules list, times are displayed in your browser's local time.

### Multiple updates

Use this mode to schedule several updates to the *same* card in one go — each step with its own media, title, destination URL, and time. This is ideal for building up to a moment: for example, a countdown that changes the card 3 hours out, then 2 hours out, then 1 hour out, then 5 minutes out.

- Give the series a **Series name (label)** (placeholder `e.g. Countdown 1`) and choose one shared time zone offset for all steps.
- The builder starts with a **single step — "Update #1"**. Its title, URL, and media are prefilled from the original loaded card.
- Click **+ Add step** to append the next update. Each newly added step is **prefilled from the step immediately before it** (its current, edited title, URL, and media) — so the form reads as a running sequence of edits. Adjust whatever you want to change for that step; anything you leave inherits from the previous step. You can **Remove** extra steps.
- A series needs **at least 2 steps** (a single step shows an inline error, *"A series needs at least 2 steps."*) and has **no upper limit**.
- Optionally turn on **Stop remaining updates if one fails** — if a step fails, the tool cancels the series' remaining pending steps. Leave it off to let each step run independently.
- Click **Create series** to create the whole series at once.

Every step must be **in the future**, in **chronological order**, and at **distinct times** — the tool validates this and won't save a series that breaks these rules. As with one-time updates, each step's media must match the card's original media type. (For app cards, the per-step Title and Destination URL are hidden and each step updates the media only.)

Each step runs on its own at its exact time, and (like one-time updates) a series keeps running even if you sign out or the server restarts. After the final step, the card simply stays on that step's creative — there's no automatic revert.

## Managing Scheduled Updates

Your saved updates appear under **Your Scheduled Updates**. Click **Refresh** to reload the list. One-time updates and series are shown differently:

- **One-time updates** appear as a single row (tagged *One-time*).
- **Multiple updates (series)** appear as one collapsible card tagged *Multiple updates*, showing the series label, progress (e.g. "2 of 4 done"), a status breakdown, and the next upcoming step's time/countdown. Expand it to see every step in order with its own time, media preview, and status.

The original **post appears as a clickable link** — on one-time rows and on the series header — that opens the X post in a new tab.

Each row (a one-time update, or a step within a series) shows:

- The Ads Account ID and card ID, with a colored status badge.
- **Scheduled** (local time) and, once it has run, **Executed** time.
- A **Will change:** (pending) / **Changed:** (executed) summary.
- The new Title, Media key (shown as *baseline → new* when the media changes), and URL.
- Media previews — small *baseline → new* thumbnails when the media is being swapped.
- A result message on completed, failed, or cancelled rows.

The **Will change / Changed** summary and the media-preview comparison are computed against a **baseline**: for a **one-time** row the baseline is the original post; for a **series step** the baseline is the **previous step** (and the first step is compared against the original post). So a series step is flagged as "changed" only when it differs from the step before it — for example, if an earlier step swapped the media and a later step reverts it to the original, that later step shows *previous → original*.

### Statuses

| Status | Meaning |
| --- | --- |
| Pending | Waiting for its scheduled time. |
| Running | Currently applying the update. |
| Completed | The card was updated successfully. |
| Failed | The update did not complete (see the row's error message). |
| Cancelled | You cancelled it before it ran; the record is kept for reference. |

### Row actions

Each of these actions first opens an **in-app confirmation dialog** (not the browser's native pop-up); dismissing it does nothing.

- **Run now** — execute the update immediately instead of waiting (intended for testing). Available on any row that isn't cancelled or currently running. The dialog offers **Run now** / **Not now**.
- **Cancel** — available on pending rows; it stops the scheduled run and marks the row cancelled but keeps the record. The dialog offers **Cancel update** / **Keep it**.
- **Cancel series** — on a series, cancels all of its remaining pending steps at once. You can still cancel individual pending steps from within the expanded series. The dialog offers **Cancel series** / **Keep them**.
- **Refresh** — reloads the list with the latest statuses.

## Tips

- **Only website cards and app cards are supported.** The post you load must have one of these attached, or validation will fail.
- **Match the media type.** New media must be the same type (image or video) as the card's original media. GIFs are handled as video.
- **Use your media library to avoid re-uploading.** If the creative already exists in your Ads account, pick it from Browse library instead of uploading again.
- **Keep videos reasonable.** Videos and GIFs are capped at 512 MB and must transcode before they're usable; smaller files upload and process faster. Images are capped at 5 MB.
- **Make the post public and resolvable.** The tool has to read the live post and card, so the post must exist and be accessible to the signed-in account.
- **Test with Run now.** Before relying on a schedule, you can trigger it immediately to confirm the new creative applies as expected.
- **Use a series for build-up moments.** For countdowns, launches, or live events, the Multiple updates tab lets you stage several timed changes on one card at once — and "Stop remaining updates if one fails" keeps a broken step from cascading.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| Sign-in window is blocked | Allow pop-ups for this site, then click Sign in with X again. Make sure you're already logged into the correct X account in the same browser. |
| You were signed out unexpectedly | Sessions end after 60 minutes of inactivity or when you close the browser. Click Sign in with X to continue — any already-scheduled updates keep running while you're signed out. |
| Required badge won't turn green | Enter a valid X Ads Account ID in the field, or click a detected chip. Confirm your X account has an active Ads account at ads.x.com → Account settings, and that you're signed in as that user. |
| "No card found" / validation fails | The post must contain a website card or app card. Check the URL is a full x.com/twitter.com `/status/` link, the post is public, and your selected Ads account actually has access to that card. |
| Can't access the Ads account during validation | Sign in with an X account that has access to that Ads account, verify the Account ID is exact, and ensure your app has the Ads API product enabled. |
| Media type mismatch when saving | The new media must be the same type as the card's original (image vs video/GIF). Choose media of the matching type. |
| Upload rejected as too large | Images are limited to 5 MB and videos/GIFs to 512 MB. Compress or shorten the file and try again. |
| Upload fails or won't finish processing | Video/GIF uploads transcode on X's side; wait for processing to complete. If it still fails, try a different file or format (MP4, MOV, M4V, WebM, GIF). |
| Media library is empty | Your Ads account has no media of the required type. Upload assets at ads.x.com first, or use the Upload tab to add a new file. |
| Schedule shows failed | Open the row to read the error message. Common causes: expired/revoked X authorization (sign in again), the Ads account losing access to the card, or an Ads API error. Fix the cause and create a new schedule (or use Run now to retry). |
| Schedule stuck on running | If a run is interrupted (e.g. a server restart), the tool auto-recovers the row to failed after a short window so you can retry. Refresh the list to see the updated status. |
