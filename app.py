"""
Dinero social scraper worker.

Dinero's own backend runs on an edge runtime that cannot execute Python, run a
browser, or hold a logged-in session. The uploaded scraper projects that need
those things run here instead, unchanged, behind one small HTTP API:

    POST /scrape/instagram   -> instaloader
    POST /scrape/tiktok      -> TikTokApi
    POST /scrape/linkedin    -> linkedin_scraper (Selenium)
    POST /scrape/x           -> snscrape.modules.twitter
    POST /scrape/facebook    -> snscrape.modules.facebook
    POST /scrape/startups    -> startup-scraper style funding sources

Every handler returns the same envelope so Dinero can normalize once:

    { "source": "...", "items": [ {name, handle, email, website, ...} ] }

Auth: send `Authorization: Bearer $SCRAPER_TOKEN`. No credential is ever
returned to the caller.

Deploy with the included Dockerfile / render.yaml, then set SOCIAL_SCRAPER_URL
and SOCIAL_SCRAPER_TOKEN in Dinero.
"""
from __future__ import annotations

import gc
import os
import re
import threading
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Dinero social scraper", version="1.0.0")

SCRAPER_TOKEN = os.environ.get("SCRAPER_TOKEN", "")
# One scrape at a time per source keeps memory and upstream rate limits sane.
_LOCKS: Dict[str, threading.Semaphore] = {}
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "1"))

# Render Free has 512MB. One Chromium alone can take ~250MB, so only ONE
# browser-backed scrape may run at a time across the whole process. Handlers
# are sync and run in FastAPI's threadpool, so a threading lock is the right
# primitive (an asyncio lock would not guard threadpool workers).
BROWSER_SOURCES = {"tiktok", "linkedin"}
_BROWSER_LOCK = threading.Semaphore(1)

# Lean Chromium flags shared by Selenium and Playwright.
LEAN_CHROMIUM_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,BackForwardCache,MediaRouter,OptimizationHints",
    "--disable-sync",
    "--disable-default-apps",
    "--mute-audio",
    "--no-first-run",
    "--no-zygote",
    "--renderer-process-limit=1",
    "--blink-settings=imagesEnabled=false",
    "--js-flags=--max-old-space-size=256",
    "--window-size=1280,900",
]
# Heavy assets never needed for scraping text/links.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_PATTERNS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.avif",
    "*.mp4", "*.webm", "*.mp3", "*.m4a", "*.woff", "*.woff2", "*.ttf", "*.otf", "*.css",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def _lock(source: str) -> threading.Semaphore:
    return _LOCKS.setdefault(source, threading.Semaphore(MAX_CONCURRENCY))


def require_token(authorization: str = Header(default="")) -> None:
    if not SCRAPER_TOKEN:
        return  # unset token = open instance (local dev only)
    if authorization != f"Bearer {SCRAPER_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def first_email(*texts: Any) -> str | None:
    for t in texts:
        if not t:
            continue
        m = EMAIL_RE.search(str(t))
        if m:
            return m.group(0).lower()
    return None


def first_url(*texts: Any) -> str | None:
    for t in texts:
        if not t:
            continue
        m = URL_RE.search(str(t))
        if m:
            return m.group(0).rstrip(".,);")
    return None


class ScrapeRequest(BaseModel):
    query: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=120)
    limit: int = Field(default=25, ge=1, le=200)


@app.get("/")
def health() -> Dict[str, Any]:
    return {"ok": True, "sources": ["instagram", "tiktok", "linkedin", "x", "facebook", "startups", "jobspy", "linkedin_api", "site_crawl"]}


# ------------------------------- Instagram --------------------------------
def scrape_instagram(req: ScrapeRequest) -> List[Dict[str, Any]]:
    import instaloader

    loader = instaloader.Instaloader(
        download_pictures=False, download_videos=False, download_comments=False,
        save_metadata=False, quiet=True,
    )
    user = os.environ.get("INSTAGRAM_USER")
    password = os.environ.get("INSTAGRAM_PASSWORD")
    if user and password:
        try:
            loader.load_session_from_file(user)
        except FileNotFoundError:
            loader.login(user, password)
            loader.save_session_to_file()

    tag = req.query.lstrip("#").strip()
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    hashtag = instaloader.Hashtag.from_name(loader.context, tag)
    for post in hashtag.get_posts():
        owner = post.owner_username
        if owner in seen:
            continue
        seen.add(owner)
        try:
            profile = instaloader.Profile.from_username(loader.context, owner)
            bio = profile.biography or ""
            items.append({
                "name": profile.full_name or owner,
                "username": owner,
                "handle": f"@{owner}",
                "followers": profile.followers,
                "bio": bio,
                "email": profile.business_email or first_email(bio),
                "website": profile.external_url or first_url(bio),
                "category": profile.business_category_name or "Instagram profile",
                "url": f"https://www.instagram.com/{owner}/",
            })
        except Exception:  # a private/removed profile must not kill the run
            continue
        if len(items) >= req.limit:
            break
    return items


# --------------------------------- TikTok ----------------------------------
def scrape_tiktok(req: ScrapeRequest) -> List[Dict[str, Any]]:
    import asyncio

    from TikTokApi import TikTokApi

    ms_token = os.environ.get("TIKTOK_MS_TOKEN")

    async def run() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        async with TikTokApi() as api:
            await api.create_sessions(
                ms_tokens=[ms_token] if ms_token else None,
                num_sessions=1,
                sleep_after=3,
                headless=True,
                override_browser_args=[a for a in LEAN_CHROMIUM_ARGS if a != "--headless=new"],
            )
            # Block images/media/fonts/CSS on every session page.
            for sess in getattr(api, "sessions", []) or []:
                page = getattr(sess, "page", None)
                if page is None:
                    continue
                try:
                    async def _block(route):
                        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", _block)
                except Exception:
                    pass
            tag = api.hashtag(name=req.query.lstrip("#").strip())
            async for video in tag.videos(count=req.limit * 2):
                info = video.as_dict or {}
                author = (info.get("author") or {}).get("uniqueId")
                if not author or author in seen:
                    continue
                seen.add(author)
                sig = (info.get("author") or {}).get("signature", "")
                out.append({
                    "name": (info.get("author") or {}).get("nickname") or author,
                    "username": author,
                    "handle": f"@{author}",
                    "followers": (info.get("authorStats") or {}).get("followerCount"),
                    "bio": sig,
                    "email": first_email(sig),
                    "website": first_url(sig),
                    "text": info.get("desc"),
                    "url": f"https://www.tiktok.com/@{author}",
                })
                if len(out) >= req.limit:
                    break
        return out

    try:
        return asyncio.run(run())
    finally:
        gc.collect()


# -------------------------------- LinkedIn ---------------------------------
def scrape_linkedin(req: ScrapeRequest) -> List[Dict[str, Any]]:
    from linkedin_scraper import Person, actions
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not email or not password:
        raise HTTPException(status_code=400, detail="LINKEDIN_EMAIL / LINKEDIN_PASSWORD not configured")

    opts = Options()
    for flag in LEAN_CHROMIUM_ARGS:
        opts.add_argument(flag)
    opts.page_load_strategy = "eager"
    opts.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.fonts": 2,
        "profile.managed_default_content_settings.media_stream": 2,
        "profile.managed_default_content_settings.plugins": 2,
    })
    driver = webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS})
    except Exception:
        pass
    try:
        actions.login(driver, email, password)
        terms = " ".join(x for x in [req.query, req.location] if x)
        driver.get("https://www.linkedin.com/search/results/people/?keywords=" + terms.replace(" ", "%20"))
        links: List[str] = []
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/in/']"):
            href = (a.get_attribute("href") or "").split("?")[0]
            if "/in/" in href and href not in links:
                links.append(href)
            if len(links) >= req.limit:
                break

        items: List[Dict[str, Any]] = []
        for url in links:
            try:
                person = Person(url, driver=driver, scrape=True, close_on_complete=False)
                items.append({
                    "name": person.name,
                    "title": person.job_title,
                    "company": person.company,
                    "location": getattr(person, "location", None) or req.location or None,
                    "email": first_email(getattr(person, "about", None)),
                    "website": None,
                    "url": url,
                })
            except Exception:
                continue
        return items
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        del driver
        gc.collect()


# ------------------------------ X and Facebook ------------------------------
def scrape_x(req: ScrapeRequest) -> List[Dict[str, Any]]:
    import snscrape.modules.twitter as sntwitter

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for i, tweet in enumerate(sntwitter.TwitterSearchScraper(req.query).get_items()):
        if i >= req.limit * 3:
            break
        user = tweet.user
        if not user or user.username in seen:
            continue
        seen.add(user.username)
        bio = getattr(user, "renderedDescription", None) or getattr(user, "description", "") or ""
        items.append({
            "name": user.displayname or user.username,
            "username": user.username,
            "handle": f"@{user.username}",
            "followers": getattr(user, "followersCount", None),
            "location": getattr(user, "location", None),
            "bio": bio,
            "email": first_email(bio),
            "website": getattr(user, "linkUrl", None) or first_url(bio),
            "text": tweet.rawContent,
            "url": f"https://twitter.com/{user.username}",
        })
        if len(items) >= req.limit:
            break
    return items


def scrape_facebook(req: ScrapeRequest) -> List[Dict[str, Any]]:
    import snscrape.modules.facebook as snfacebook

    items: List[Dict[str, Any]] = []
    scraper = snfacebook.FacebookUserScraper(req.query.strip().replace(" ", ""))
    entity = scraper.entity
    if entity is not None:
        items.append({
            "name": getattr(entity, "displayname", None) or req.query,
            "username": getattr(entity, "username", None),
            "handle": getattr(entity, "username", None),
            "followers": getattr(entity, "followers", None),
            "bio": getattr(entity, "description", None),
            "email": first_email(getattr(entity, "description", None)),
            "website": getattr(entity, "websites", None) or first_url(getattr(entity, "description", None)),
            "url": getattr(entity, "url", None),
        })
    for i, post in enumerate(scraper.get_items()):
        if i >= req.limit:
            break
        content = getattr(post, "content", "") or ""
        email = first_email(content)
        if not email:
            continue
        items.append({
            "name": req.query,
            "email": email,
            "website": first_url(content),
            "text": content,
            "url": getattr(post, "url", None),
        })
    return items


# -------------------------------- Startups ---------------------------------
def scrape_startups(req: ScrapeRequest) -> List[Dict[str, Any]]:
    """TechCrunch funding feed — the reliable, key-free source of the
    startup-scraper project (Crunchbase/Dealroom need paid access)."""
    import feedparser
    import requests
    from bs4 import BeautifulSoup

    feed = feedparser.parse("https://techcrunch.com/category/venture/feed/")
    items: List[Dict[str, Any]] = []
    needle = req.query.lower().strip()
    for entry in feed.entries[: req.limit * 2]:
        title = entry.get("title", "")
        summary = BeautifulSoup(entry.get("summary", ""), "html.parser").get_text(" ", strip=True)
        if needle and needle not in f"{title} {summary}".lower():
            continue
        company = re.split(r"\s+(?:raises|lands|secures|nabs|closes|bags)\s+", title, flags=re.I)[0].strip()
        website = None
        try:
            page = requests.get(entry.get("link", ""), timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 dinero-leadgen"})
            soup = BeautifulSoup(page.text, "html.parser")
            for a in soup.select("article a[href^='http']"):
                href = a["href"]
                if "techcrunch.com" not in href and "twitter.com" not in href:
                    website = href
                    break
        except Exception:
            pass
        items.append({
            "name": company[:120] or title[:120],
            "company": company[:120],
            "category": "Funded startup",
            "website": website,
            "email": None,
            "text": summary[:280],
            "url": entry.get("link"),
        })
        if len(items) >= req.limit:
            break
    return items


# -------------------------------- JobSpy ----------------------------------
def scrape_jobspy(req: ScrapeRequest) -> List[Dict[str, Any]]:
    """job-spy-enhanced-script: multi-board job search (Indeed, LinkedIn,
    ZipRecruiter, Glassdoor) rolled up into hiring-company leads."""
    from jobspy import scrape_jobs

    df = scrape_jobs(
        site_name=["indeed", "linkedin", "zip_recruiter", "glassdoor"],
        search_term=req.query,
        location=req.location or "United States",
        results_wanted=req.limit,
        hours_old=720,
        country_indeed="USA",
    )
    items: List[Dict[str, Any]] = []
    if df is None or len(df) == 0:
        return items
    for _, row in df.head(req.limit).iterrows():
        def g(k: str) -> Any:
            v = row.get(k)
            return None if v is None or str(v) == "nan" else v

        company = str(g("company") or "").strip()
        if not company:
            continue
        items.append({
            "name": company[:120],
            "company": company[:120],
            "title": str(g("title") or "")[:140] or None,
            "location": str(g("location") or "")[:120] or None,
            "website": g("company_url") or g("company_url_direct"),
            "email": first_email(g("description")),
            "category": "Hiring company",
            "text": str(g("description") or "")[:280] or None,
            "url": g("job_url"),
        })
    return items


# ---------------------------- LinkedIn (Voyager) ----------------------------
def scrape_linkedin_api(req: ScrapeRequest) -> List[Dict[str, Any]]:
    """linkedin-api project: authenticated Voyager API people search.
    Faster and lighter than the Selenium scraper, same credentials."""
    from linkedin_api import Linkedin

    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")
    if not (email and password):
        raise HTTPException(status_code=400,
                            detail="Set LINKEDIN_EMAIL and LINKEDIN_PASSWORD on the scraper service.")
    api = Linkedin(email, password)
    people = api.search_people(keywords=req.query or None,
                               regions=None,
                               limit=req.limit) or []
    items: List[Dict[str, Any]] = []
    for p in people[: req.limit]:
        name = " ".join(filter(None, [p.get("name")])) or p.get("public_id") or ""
        if not name:
            continue
        urn = p.get("public_id") or p.get("urn_id")
        items.append({
            "name": str(name)[:120],
            "handle": urn,
            "title": (p.get("jobtitle") or "")[:140] or None,
            "company": (p.get("location") and None) or None,
            "location": (p.get("location") or "")[:120] or None,
            "category": "LinkedIn profile",
            "url": f"https://www.linkedin.com/in/{urn}" if urn else None,
        })
    return items


# --------------------------------- Scrapy ----------------------------------
def scrape_site_crawl(req: ScrapeRequest) -> List[Dict[str, Any]]:
    """Scrapy project: breadth-first crawl of one domain, collecting the
    contact emails and page titles found along the way."""
    import subprocess
    import json as _json
    import tempfile
    import textwrap

    start = req.query.strip()
    if not start.startswith("http"):
        start = f"https://{start}"
    depth = 2
    out = tempfile.NamedTemporaryFile(suffix=".jl", delete=False)
    out.close()
    spider = textwrap.dedent(f"""
        import re, scrapy
        from scrapy.crawler import CrawlerProcess
        from urllib.parse import urlparse
        EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{{2,}}")
        HOST = urlparse({start!r}).netloc
        class S(scrapy.Spider):
            name = "dinero"
            start_urls = [{start!r}]
            custom_settings = {{
                "DEPTH_LIMIT": {depth}, "ROBOTSTXT_OBEY": True, "LOG_LEVEL": "ERROR",
                "CLOSESPIDER_ITEMCOUNT": {req.limit}, "CLOSESPIDER_TIMEOUT": 40,
                "CONCURRENT_REQUESTS": 8, "USER_AGENT": "Mozilla/5.0 dinero-leadgen",
                "FEEDS": {{{out.name!r}: {{"format": "jsonlines"}}}},
            }}
            def parse(self, response):
                text = response.text
                for e in set(EMAIL.findall(text)):
                    yield {{"email": e.lower(), "url": response.url,
                            "title": (response.css("title::text").get() or "").strip()}}
                for href in response.css("a::attr(href)").getall():
                    u = response.urljoin(href)
                    if urlparse(u).netloc == HOST:
                        yield scrapy.Request(u, callback=self.parse)
        CrawlerProcess().crawl  # keep import used
        p = CrawlerProcess()
        p.crawl(S)
        p.start()
    """)
    script = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    script.write(spider)
    script.close()
    subprocess.run(["python", script.name], timeout=50, capture_output=True)

    seen: set[str] = set()
    items: List[Dict[str, Any]] = []
    try:
        with open(out.name) as fh:
            for line in fh:
                try:
                    rec = _json.loads(line)
                except Exception:
                    continue
                mail = rec.get("email")
                if not mail or mail in seen:
                    continue
                seen.add(mail)
                items.append({
                    "name": (rec.get("title") or mail.split("@")[1])[:120],
                    "email": mail,
                    "website": start,
                    "category": "Website contact",
                    "url": rec.get("url"),
                })
                if len(items) >= req.limit:
                    break
    except FileNotFoundError:
        pass
    return items


HANDLERS = {
    "instagram": scrape_instagram,
    "tiktok": scrape_tiktok,
    "linkedin": scrape_linkedin,
    "x": scrape_x,
    "facebook": scrape_facebook,
    "startups": scrape_startups,
    "jobspy": scrape_jobspy,
    "linkedin_api": scrape_linkedin_api,
    "site_crawl": scrape_site_crawl,
}


@app.post("/scrape/{source}", dependencies=[Depends(require_token)])
def scrape(source: str, req: ScrapeRequest) -> Dict[str, Any]:
    handler = HANDLERS.get(source)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"Unknown source '{source}'")
    sem = _lock(source)
    if not sem.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=f"{source} scraper busy, retry shortly")
    is_browser = source in BROWSER_SOURCES
    # HTTP-only sources never touch Chromium and never wait on the browser lock.
    if is_browser and not _BROWSER_LOCK.acquire(timeout=5):
        sem.release()
        raise HTTPException(status_code=429, detail="A browser scrape is already running, retry shortly")
    try:
        return {"source": source, "items": handler(req)}
    except HTTPException:
        raise
    except Exception as exc:  # surface the real cause to Dinero, no secrets
        raise HTTPException(status_code=502, detail=f"{source} scrape failed: {exc}"[:500])
    finally:
        if is_browser:
            _BROWSER_LOCK.release()
        sem.release()
        gc.collect()
