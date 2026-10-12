# Dinero social scraper worker

Dinero's backend runs on an edge runtime: no Python, no subprocesses, no
browser. The uploaded scraper projects that require those things run here and
are called by Dinero over authenticated HTTP — exactly like the existing
Google Maps (GoSOM) scraper on Render.

| Dinero source            | Project used                       | Extra config |
| ------------------------ | ---------------------------------- | ------------ |
| Instagram Profile Search | `instaloader`                      | `INSTAGRAM_USER`, `INSTAGRAM_PASSWORD` (optional, avoids rate limits) |
| TikTok Creator Search    | `TikTok-Api` (Playwright)          | `TIKTOK_MS_TOKEN` (optional) |
| LinkedIn People Search   | `linkedin_scraper` (Selenium)      | `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` (required) |
| X (Twitter) Post Search  | `snscrape.modules.twitter`         | — |
| Facebook Page Search     | `snscrape.modules.facebook`        | — |
| Funded Startup Search    | startup-scraper TechCrunch source  | — |

Sources that do **not** need this worker (they run inside Dinero already):

- **YouTube** — the `yt-meta` project is a YouTube Data API v3 client, so its
  requests are made directly by Dinero. Needs `YOUTUBE_API_KEY`.
- **Google Maps** — the existing GoSOM integration, untouched.

**Reddit is not handled by this worker** — there is no `/scrape/reddit` route
here. Reddit post search is built into Dinero's own backend at
`src/lib/sources/social.server.ts`, which calls Reddit's REST API directly over
plain HTTPS with real pagination. It works keyless against Reddit's public
endpoints, and runs with proper OAuth plus higher rate limits when
`REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` are set in Dinero's project
settings (create a free "script" app at reddit.com/prefs/apps).

## Deploy

1. Push this folder to a repo (or point Render at this subdirectory).
2. Create a Render **Web Service** from the Dockerfile (`render.yaml` included).
3. Set `SCRAPER_TOKEN` to a long random string, plus any per-source
   credentials from the table above.
4. In Dinero, set:
   - `SOCIAL_SCRAPER_URL` = `https://<your-service>.onrender.com`
   - `SOCIAL_SCRAPER_TOKEN` = the same `SCRAPER_TOKEN`

Until those two variables exist, the worker-backed sources return a clear
"deploy the social scraper service" message instead of failing silently.

## API

```
GET  /                       -> health + source list
POST /scrape/{source}        -> { "source": "...", "items": [...] }
     Authorization: Bearer $SCRAPER_TOKEN
     Body: { "query": "...", "location": "...", "limit": 25 }
```

Every item is normalized to `{ name, username, handle, followers, bio, email,
website, location, title, company, text, url }`; Dinero maps that to its lead
schema, tags each row with its source, and runs its existing duplicate check
before saving.

## Limits

- One request per source at a time (`MAX_CONCURRENCY`, default 2) — the
  browser-based scrapers are memory hungry.
- Requests over ~55s are abandoned by Dinero; keep `limit` modest.
- Scraping Instagram, TikTok, LinkedIn, X and Facebook may violate those
  platforms' terms of service. Use accounts and volumes you are comfortable
  with; credentials stay on this service and are never sent to the browser.
