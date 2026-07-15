# Dynamic Media Card Tool

**Link:** https://dynamic-media-card.onrender.com/

## User Guide

Dynamic Media Card Tool lets you schedule an update to an existing X website card or app card — swapping its media, changing its title, and changing its destination URL — on a post you've already published. At the exact date and time you choose, the tool automatically applies the new creative to the card through the X Ads API.

You'll need an active X Ads account at [ads.x.com](https://ads.x.com) (required to read and update card creatives via the Ads API).

## Signing In

- Open the app and click **Sign in with X**.
- A window will open on X asking you to authorize the app (the tool uses X OAuth 1.0a, which is required for the Ads API).
- Grant permission — you'll be redirected back to the tool and the scheduler interface will load.

Before you sign in, open x.com in the same browser and make sure you're logged into the exact X account that owns the post you want to change. The card can only be updated by an account with access to it.

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

## Choosing the New Media

In the **Schedule Update** form, click **Select new media** to open the media picker. It has two tabs:

- **Browse library** — browse your X Ads media library, filtered to match the original card's media type (the header shows "Showing image/video media — matches original"). Type in the **Search media…** box and click **Search** to filter by name/file name, and use **Load more** to page through additional results.
- **Upload** — pick a local file to upload. A progress bar shows upload and processing status; videos and GIFs are transcoded on X's side, so the tool polls until processing finishes before the media is usable.

Selecting or uploading media fills in the **New Media Key**, and its type, aspect ratio, and preview load automatically.

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

If you leave a value the same as the current card, the tool simply won't change it — only the fields that differ are updated.

## Scheduling the Update

- Pick a date and a 24-hour time (HH:MM). The scheduled time must be in the future — a warning appears and saving is blocked if it's in the past.
- Choose the matching time zone offset (the dropdown lists GMT/UTC offsets).
- Click **Save Schedule**.

The update runs server-side at the exact time you set (the tool uses APScheduler's date trigger). Schedules are stored so that pending updates still run even if the server restarts before their time. In the schedules list, times are displayed in your browser's local time.

## Managing Scheduled Updates

Your saved updates appear under **Your Scheduled Updates**. Click **Refresh** to reload the list. Each row shows:

- The Ads Account ID and card ID, with a colored status badge.
- **Scheduled** (local time) and, once it has run, **Executed** time.
- A **Will change:** (pending) / **Changed:** (executed) summary.
- The new Title, Media key (shown as old → new when the media changes), and URL.
- Media previews — small old → new thumbnails when the media is being swapped.
- A result message on completed, failed, or cancelled rows.

### Statuses

| Status | Meaning |
| --- | --- |
| Pending | Waiting for its scheduled time. |
| Running | Currently applying the update. |
| Completed | The card was updated successfully. |
| Failed | The update did not complete (see the row's error message). |
| Cancelled | You cancelled it before it ran; the record is kept for reference. |

### Row actions

- **Run now** — execute the update immediately instead of waiting (intended for testing). Available on any row that isn't cancelled or currently running.
- **Cancel** — available on pending rows; it stops the scheduled run and marks the row cancelled but keeps the record.
- **Refresh** — reloads the list with the latest statuses.

## Tips

- **Only website cards and app cards are supported.** The post you load must have one of these attached, or validation will fail.
- **Match the media type.** New media must be the same type (image or video) as the card's original media. GIFs are handled as video.
- **Use your media library to avoid re-uploading.** If the creative already exists in your Ads account, pick it from Browse library instead of uploading again.
- **Keep videos reasonable.** Videos and GIFs are capped at 512 MB and must transcode before they're usable; smaller files upload and process faster. Images are capped at 5 MB.
- **Make the post public and resolvable.** The tool has to read the live post and card, so the post must exist and be accessible to the signed-in account.
- **Test with Run now.** Before relying on a schedule, you can trigger it immediately to confirm the new creative applies as expected.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| Sign-in window is blocked | Allow pop-ups for this site, then click Sign in with X again. Make sure you're already logged into the correct X account in the same browser. |
| Required badge won't turn green | Enter a valid X Ads Account ID in the field, or click a detected chip. Confirm your X account has an active Ads account at ads.x.com → Account settings, and that you're signed in as that user. |
| "No card found" / validation fails | The post must contain a website card or app card. Check the URL is a full x.com/twitter.com `/status/` link, the post is public, and your selected Ads account actually has access to that card. |
| Can't access the Ads account during validation | Sign in with an X account that has access to that Ads account, verify the Account ID is exact, and ensure your app has the Ads API product enabled. |
| Media type mismatch when saving | The new media must be the same type as the card's original (image vs video/GIF). Choose media of the matching type. |
| Upload rejected as too large | Images are limited to 5 MB and videos/GIFs to 512 MB. Compress or shorten the file and try again. |
| Upload fails or won't finish processing | Video/GIF uploads transcode on X's side; wait for processing to complete. If it still fails, try a different file or format (MP4, MOV, M4V, WebM, GIF). |
| Media library is empty | Your Ads account has no media of the required type. Upload assets at ads.x.com first, or use the Upload tab to add a new file. |
| Schedule shows failed | Open the row to read the error message. Common causes: expired/revoked X authorization (sign in again), the Ads account losing access to the card, or an Ads API error. Fix the cause and create a new schedule (or use Run now to retry). |
| Schedule stuck on running | If a run is interrupted (e.g. a server restart), the tool auto-recovers the row to failed after a short window so you can retry. Refresh the list to see the updated status. |
