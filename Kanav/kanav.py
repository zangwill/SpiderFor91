#!/usr/bin/env python3
"""
KanAV (kanav.ad) Video Crawler
===============================
Scrapes video direct URLs (m3u8), titles, cover images, and unique IDs from
https://kanav.ad without browser automation (pure requests + BeautifulSoup).

Site notes:
    - MacCMS v10 based site.
    - Listing pages: /index.php/vod/show/id/{cat}.html, paginated via
      /index.php/vod/show/id/{cat}/page/{N}.html (24 videos per page).
    - The video URL is hidden in the detail page inside a
      `var player_... = {...}` script with an encrypted "url" field:
          decode(url) = unquote( base64decode( unquote(url) ) )
      e.g. "JTY4JTc0JTc0..." -> "https://cdnNN.11yun.space/....m3u8"

Usage:
    python3 kanav.py --job /path/to/job.json

    # For manual testing:
    python3 kanav.py --url "https://kanav.ad/index.php/vod/play/id/120952/sid/1/nid/1.html"
"""

import requests
from bs4 import BeautifulSoup
import json
import re
import time
import os
import sys
import argparse
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, unquote

CRAWLER_NAME = "kanav"
CRAWLER_PROTOCOL = "crawler.v2"

# ── Configuration ──────────────────────────────────────────────────────────

BASE_URL = "https://kanav.ad"
DEFAULT_CATEGORY_ID = 22  # "流出自拍" listing (default crawl target)
VIDEOS_PER_PAGE = 24
DEFAULT_WORKERS = 5
REQUEST_TIMEOUT = 30
DELAY_BETWEEN_PAGES = 0.5
DELAY_BETWEEN_VIDEOS = 0.3
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def log(msg):
    """Write a log message to stderr."""
    print(f"[{CRAWLER_NAME}] {msg}", file=sys.stderr, flush=True)


def emit(obj):
    """Write a JSON Lines object to stdout and flush."""
    try:
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        sys.exit(0)


def positive_int(value, default=10):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def deadline_reached(limits, start_mono, last_item_mono, emitted):
    limits = limits or {}
    max_runtime = limits.get("max_runtime_seconds")
    if max_runtime:
        try:
            if time.monotonic() - start_mono >= float(max_runtime):
                return True
        except (TypeError, ValueError):
            pass
    deadline_at = limits.get("deadline_at")
    if deadline_at:
        try:
            text = str(deadline_at).replace("Z", "+00:00")
            deadline = datetime.fromisoformat(text)
            if deadline.tzinfo is None:
                return datetime.utcnow() >= deadline
            return datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc)
        except Exception:
            pass
    idle = limits.get("candidate_idle_timeout_seconds")
    if idle:
        try:
            anchor = last_item_mono if emitted > 0 else start_mono
            if time.monotonic() - anchor >= float(idle):
                return True
        except (TypeError, ValueError):
            pass
    return False


# ── HTTP helper ────────────────────────────────────────────────────────────

def create_session(proxies=None):
    """Create a requests session with headers and optional proxy."""
    session = requests.Session()
    session.headers.update(HEADERS)
    if proxies:
        session.proxies.update(proxies)
    return session


def fetch_page(session, url, max_retries=MAX_RETRIES):
    """Fetch a page with retries and exponential backoff."""
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 404:
                # video may have been removed; not worth retrying
                return resp
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                time.sleep(wait)
            else:
                raise e
    return None


# ── URL decoding (MacCMS player "url" field) ──────────────────────────────

def decode_media_url(enc):
    """
    Decode the MacCMS-encrypted video URL.

    The stored value is: percent-encode( base64( percent-encode(url) ) )
    So decoding is: unquote -> base64decode -> unquote.
    Multiple sources may be joined by "$$$"; the first one wins.
    """
    if not enc:
        return ""
    try:
        step1 = unquote(enc)
        step2 = base64.b64decode(step1).decode("utf-8")
        step3 = unquote(step2)
    except Exception:
        return ""
    # keep the first non-empty source
    for part in re.split(r"\$\$\$", step3):
        part = part.strip()
        if re.match(r"^https?://", part):
            return part
    return step3.strip()


# ── Pagination helper ──────────────────────────────────────────────────────

HOT_LABEL_URL = "/index.php/label/hot.html"


def get_total_pages(session, category_id, source="category"):
    """Get the total number of listing pages from the first page."""
    if source == "hot":
        # 热门影片标签页: single page, no real pagination
        return 1
    url = f"{BASE_URL}/index.php/vod/show/id/{category_id}.html"
    resp = fetch_page(session, url)
    if resp is None:
        return 1
    soup = BeautifulSoup(resp.text, "html.parser")

    # Last-page link: <li><a class="extend" href=".../page/767.html" title="尾页">
    pagination_links = soup.select(".pagination a")
    for link in pagination_links:
        if link.get("title") == "尾页" or link.text.strip() == "尾页":
            href = link.get("href", "")
            match = re.search(r"/page/(\d+)", href)
            if match:
                return int(match.group(1))

    # Fallback 1: total videos count hidden in <div class="total">
    total_div = soup.select_one("div.total")
    if total_div:
        try:
            total = int(total_div.text.strip())
            return max(1, (total + VIDEOS_PER_PAGE - 1) // VIDEOS_PER_PAGE)
        except (TypeError, ValueError):
            pass

    # Fallback 2: largest page number in pagination links
    max_page = 1
    for link in pagination_links:
        href = link.get("href", "")
        match = re.search(r"/page/(\d+)", href)
        if match:
            max_page = max(max_page, int(match.group(1)))

    return max_page


# ── Listing page scraper ───────────────────────────────────────────────────

DURATION_RE = re.compile(
    r"(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分钟)?\s*(?:(\d+)\s*秒)?"
)


def parse_duration(text):
    """Parse '22分钟 6秒' / '1小时 17分钟 1秒' into seconds."""
    text = text or ""
    match = DURATION_RE.search(text)
    if not match:
        return None
    h, m, s = match.groups()
    if h is None and m is None and s is None:
        return None
    return (int(h or 0) * 3600) + (int(m or 0) * 60) + int(s or 0)


def scrape_listing_page(session, category_id, page_num, source="category"):
    """
    Scrape a single listing page for basic video metadata.

    Returns a list of dicts with keys:
        vod_id, title, cover_image, duration_seconds, category, page_url
    """
    if source == "hot":
        url = f"{BASE_URL}{HOT_LABEL_URL}"
    elif page_num == 1:
        url = f"{BASE_URL}/index.php/vod/show/id/{category_id}.html"
    else:
        url = f"{BASE_URL}/index.php/vod/show/id/{category_id}/page/{page_num}.html"
    resp = fetch_page(session, url)
    if resp is None:
        raise RuntimeError(f"failed to fetch listing page {page_num}")
    soup = BeautifulSoup(resp.text, "html.parser")

    videos = []
    for item in soup.select("div.video-item"):
        try:
            # Detail page URL -> extract the native video id
            link = item.select_one("a[href*='/index.php/vod/play/']")
            if not link:
                link = item.find("a")
            page_url = urljoin(BASE_URL, link.get("href", "")) if link else ""
            match = re.search(r"/vod/play/id/(\d+)", page_url)
            vod_id = match.group(1) if match else ""

            # Cover image (data-original, falling back to src)
            img = item.select_one("img")
            cover_image = ""
            if img:
                cover_image = (
                    img.get("data-original") or img.get("src") or ""
                ).strip()

            # Title (img alt first, then .entry-title link text)
            title = ""
            if img:
                title = (img.get("alt") or "").strip()
            if not title:
                title_el = item.select_one(".entry-title a")
                if title_el:
                    title = title_el.text.strip()

            # Category badge (sometimes holds a view-count like "1229 Views"
            # when the video has no category)
            cat_el = item.select_one(".model-view-left")
            category = cat_el.text.strip() if cat_el else ""
            if re.match(r"^\d+\s*Views?$", category):
                category = ""

            # Duration badge
            dur_el = item.select_one(".model-view")
            duration_seconds = parse_duration(
                dur_el.text.strip() if dur_el else ""
            )

            videos.append({
                "vod_id": vod_id,
                "title": title,
                "cover_image": cover_image,
                "duration_seconds": duration_seconds,
                "category": category,
                "page_url": page_url,
            })
        except Exception as e:
            log(f"WARN: Failed to parse video item on page {page_num}: {e}")
            continue

    return videos


# ── Detail page scraper ────────────────────────────────────────────────────

def scrape_detail_page(session, page_url):
    """
    Scrape a single video detail page for the direct m3u8 URL and extras.

    Returns a dict with keys:
        media_url, actor, published_at, tags
    """
    result = {
        "media_url": "",
        "actor": "",
        "published_at": "",
        "tags": [],
    }

    try:
        resp = fetch_page(session, page_url)
        if resp is None or resp.status_code == 404:
            log(f"WARN: detail page unavailable (404): {page_url}")
            return result
        soup = BeautifulSoup(resp.text, "html.parser")

        # ── Player script: var player_aaaa = {...} ─────────────────────
        script = soup.find(
            "script", string=re.compile(r"var\s+player_\w+\s*=")
        )
        if script and script.string:
            match = re.search(
                r"var\s+player_\w+\s*=\s*(\{.*\})\s*;?\s*$",
                script.string, re.S
            )
            if match:
                try:
                    data = json.loads(match.group(1))
                except (json.JSONDecodeError, TypeError):
                    data = {}
                result["media_url"] = decode_media_url(data.get("url", ""))
                vod_data = data.get("vod_data") or {}
                if isinstance(vod_data, dict):
                    result["actor"] = (vod_data.get("vod_actor") or "").strip()
                elif isinstance(vod_data, str) and vod_data:
                    result["actor"] = vod_data.strip()

        # ── Published date: <a class="btn btn-info btn-md">上映日期：2026-08-05</a>
        for a in soup.select("div.video-countext-categories a.btn-info"):
            text = a.text.strip()
            dm = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", text)
            if dm:
                result["published_at"] = dm.group(1)
                break

        # ── Tags: plain links to /index.php/vod/search.html?wd=...
        for a in soup.select("div.video-countext-tags a"):
            href = a.get("href", "")
            if "vod/search.html" in href:
                tag = a.text.strip()
                # skip the view-count pseudo-tag like "1229 Views"
                if tag and not re.match(r"^\d+\s*Views?$", tag):
                    result["tags"].append(tag)

    except Exception as e:
        log(f"WARN: Failed to scrape detail page {page_url}: {e}")

    return result


# ── Item builder ───────────────────────────────────────────────────────────

def sanitize_source_id(raw):
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "", str(raw or ""))
    if not re.search(r"[A-Za-z0-9]", sanitized):
        return ""
    return sanitized[:160]


VIEW_COUNT_RE = re.compile(r"^\d+\s*Views?$")


def build_item(video):
    """
    Build a crawler item from listing + detail data.

    Args:
        video: dict with keys from listing + detail scraping

    Returns:
        dict suitable for stdout JSON Lines output, or None if invalid
    """
    source_id = sanitize_source_id(video.get("vod_id"))
    title = (video.get("title") or "").strip()
    media_url = (video.get("media_url") or "").strip()
    if not source_id or not title or not media_url:
        return None

    item = {
        "type": "item",
        "source_id": source_id,
        "title": title,
        "media_url": media_url,
        "thumbnail_url": (video.get("cover_image") or "").strip(),
        "detail_url": video.get("page_url", ""),
        "headers": {
            "Referer": "https://kanav.ad/",
            "User-Agent": HEADERS["User-Agent"],
        },
    }

    if video.get("actor"):
        item["author"] = video["actor"]

    if video.get("category"):
        item["tags"] = [video["category"]]
    if video.get("tags"):
        extra = item.get("tags", [])
        for t in video["tags"]:
            if t not in extra:
                extra.append(t)
        item["tags"] = extra
    # drop any stray view-count pseudo-tags
    if item.get("tags"):
        item["tags"] = [t for t in item["tags"] if not VIEW_COUNT_RE.match(t)]
        if not item["tags"]:
            del item["tags"]

    if video.get("duration_seconds"):
        item["duration_seconds"] = video["duration_seconds"]

    if video.get("published_at"):
        item["published_at"] = video["published_at"]

    return item


# ── Read seen file ─────────────────────────────────────────────────────────

def load_seen_ids(seen_file_path):
    """
    Load seen source IDs from a text file (one ID per line).

    Returns a set of source_id strings.
    """
    seen = set()
    if not seen_file_path:
        return seen

    if not os.path.exists(seen_file_path):
        log(f"Seen file does not exist yet: {seen_file_path}")
        return seen

    try:
        with open(seen_file_path, "r", encoding="utf-8") as f:
            for line in f:
                sid = line.strip()
                if sid:
                    seen.add(sid)
        log(f"Loaded {len(seen)} seen IDs from {seen_file_path}")
    except Exception as e:
        log(f"WARN: Failed to read seen file: {e}")

    return seen


# ── Main job runner ────────────────────────────────────────────────────────

def run_job(job_path):
    """Run the crawler job from a job.json file."""

    # ── Parse job config ─────────────────────────────────────────────────
    if not os.path.exists(job_path):
        log(f"ERROR: job file not found: {job_path}")
        sys.exit(1)

    try:
        with open(job_path, "r", encoding="utf-8") as f:
            job = json.load(f)
    except Exception as e:
        log(f"ERROR: failed to read job file: {e}")
        sys.exit(1)

    if job.get("protocol") != CRAWLER_PROTOCOL:
        log(f"ERROR: unsupported protocol: {job.get('protocol')!r} "
            f"(need {CRAWLER_PROTOCOL!r})")
        sys.exit(1)
    if job.get("mode") not in ("", None, "crawl"):
        log(f"ERROR: unsupported mode: {job.get('mode')!r}")
        sys.exit(1)

    candidate_budget = positive_int(
        job.get("candidate_budget") or job.get("target_new"),
        default=10,
    )

    seen_file = job.get("seen_source_ids_file", "")
    proxy_url = (job.get("network") or {}).get("proxy_url", "")
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
        log(f"Using proxy: {proxy_url}")

    # Admin config: optional "source" ("category" or "hot") and
    # "category_id" (site listing category, default 22 = 流出自拍)
    config = job.get("config") or {}
    source = "category"
    if isinstance(config, dict):
        source = str(config.get("source", "category") or "category").strip().lower()
        if source not in ("category", "hot"):
            log(f"WARN: unknown source {source!r}, falling back to 'category'")
            source = "category"
        category_id = positive_int(config.get("category_id"), DEFAULT_CATEGORY_ID)
    else:
        category_id = DEFAULT_CATEGORY_ID
    workers = positive_int(config.get("workers"), DEFAULT_WORKERS) \
        if isinstance(config, dict) else DEFAULT_WORKERS

    limits = job.get("limits") if isinstance(job.get("limits"), dict) else {}
    progress_interval = positive_int(
        limits.get("progress_interval_seconds"), default=60
    )

    log(f"Job started: source={source}, category_id={category_id}, "
        f"candidate_budget={candidate_budget}, seen_file={seen_file}, "
        f"proxy={'yes' if proxy_url else 'no'}")

    # ── Load seen IDs ────────────────────────────────────────────────────
    seen = load_seen_ids(seen_file)

    # ── Setup ────────────────────────────────────────────────────────────
    session = create_session(proxies)

    # Auto-detect total pages
    try:
        max_pages = get_total_pages(session, category_id, source)
        log(f"Found {max_pages} listing pages "
            f"({max_pages * VIDEOS_PER_PAGE} videos estimated)")
    except Exception as e:
        log(f"ERROR: Failed to detect total pages: {e}")
        sys.exit(1)

    # ── Crawl ────────────────────────────────────────────────────────────
    emitted = 0
    checked = 0
    page_num = 1
    stopped_early = False
    start_mono = time.monotonic()
    last_item_mono = start_mono
    last_progress_mono = start_mono

    detail_lock = threading.Lock()

    def maybe_progress(message=""):
        nonlocal last_progress_mono
        now = time.monotonic()
        if not message and now - last_progress_mono < progress_interval:
            return
        emit({
            "type": "progress",
            "checked": checked,
            "emitted": emitted,
            "message": message or f"checked={checked} emitted={emitted}",
        })
        last_progress_mono = now

    for page_num in range(1, max_pages + 1):
        if emitted >= candidate_budget:
            stopped_early = True
            break
        if deadline_reached(limits, start_mono, last_item_mono, emitted):
            log("Reached job deadline/limits, stopping")
            break

        # ── Fetch listing page ─────────────────────────────────────────
        try:
            page_videos = scrape_listing_page(session, category_id, page_num, source)
        except Exception as e:
            log(f"ERROR: Failed to scrape listing page {page_num}: {e}")
            maybe_progress(f"Failed listing page {page_num}")
            if page_num < max_pages:
                time.sleep(DELAY_BETWEEN_PAGES)
            continue

        if not page_videos:
            log(f"Page {page_num}/{max_pages}: empty page, stopping")
            break

        checked += len(page_videos)

        # ── Filter already-seen videos (by ID from listing URL) ────────
        unseen = []
        for v in page_videos:
            sid = sanitize_source_id(v.get("vod_id"))
            if sid and sid not in seen:
                v["vod_id"] = sid
                unseen.append(v)

        if not unseen:
            log(f"Page {page_num}/{max_pages}: {len(page_videos)} videos, "
                f"0 new (all already seen)")
            maybe_progress(f"Scanned page {page_num}/{max_pages}")
            if page_num < max_pages:
                time.sleep(DELAY_BETWEEN_PAGES)
            continue

        remaining = candidate_budget - emitted
        if len(unseen) > remaining:
            unseen = unseen[:remaining]

        log(f"Page {page_num}/{max_pages}: {len(page_videos)} videos, "
            f"{len(unseen)} new → need detail pages")

        def fetch_and_build(video):
            detail = scrape_detail_page(session, video["page_url"])
            video["media_url"] = detail["media_url"]
            video["actor"] = detail["actor"]
            video["published_at"] = detail["published_at"]
            video["tags"] = detail["tags"]
            time.sleep(DELAY_BETWEEN_VIDEOS)
            return build_item(video)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_video = {
                executor.submit(fetch_and_build, v): v
                for v in unseen
            }

            for future in as_completed(future_to_video):
                if emitted >= candidate_budget:
                    stopped_early = True
                    for f in future_to_video:
                        f.cancel()
                    break
                if deadline_reached(limits, start_mono, last_item_mono, emitted):
                    for f in future_to_video:
                        f.cancel()
                    break

                v = future_to_video[future]
                try:
                    item = future.result()
                except Exception as e:
                    log(f"WARN: Failed detail page for "
                        f"video {v['vod_id']}: {e}")
                    continue

                if not item:
                    log(f"WARN: Invalid item for video {v['vod_id']}, skip")
                    continue

                with detail_lock:
                    if item["source_id"] in seen:
                        continue
                    seen.add(item["source_id"])
                    emit(item)
                    emitted += 1
                    last_item_mono = time.monotonic()
                    last_progress_mono = last_item_mono

        maybe_progress(f"Scanned page {page_num}/{max_pages}")

        if emitted >= candidate_budget:
            stopped_early = True
            break

        if page_num < max_pages:
            time.sleep(DELAY_BETWEEN_PAGES)

    if not stopped_early:
        page_num = min(page_num, max_pages)

    emit({
        "type": "done",
        "stats": {
            "checked": checked,
            "emitted": emitted,
        },
    })

    log(f"Job complete: checked={checked}, emitted={emitted}, "
        f"pages={page_num}/{max_pages}")


# ── Single-video test mode ─────────────────────────────────────────────────

def scrape_single_video(url):
    """
    Scrape a single video by its detail page URL (for testing).

    Outputs one item JSON to stdout.
    """
    session = create_session()

    resp = fetch_page(session, url)
    if resp is None or resp.status_code != 200:
        log(f"ERROR: failed to fetch {url}")
        sys.exit(1)
    soup = BeautifulSoup(resp.text, "html.parser")

    match = re.search(r"/vod/play/id/(\d+)", url)
    vod_id = match.group(1) if match else ""

    # Title
    title = ""
    h1 = soup.select_one("h1")
    if h1:
        title = h1.text.strip()

    # Cover
    cover = ""
    pic_script = soup.find(
        "script", string=re.compile(r"MacPlayer\.Pic")
    )
    if pic_script and pic_script.string:
        pm = re.search(r'MacPlayer\.Pic="([^"]*)"', pic_script.string)
        if pm:
            cover = pm.group(1)
    if not cover:
        img = soup.select_one("img.countext-img")
        if img:
            cover = img.get("src", "")

    # Player url + actor
    media_url = ""
    actor = ""
    player_script = soup.find(
        "script", string=re.compile(r"var\s+player_\w+\s*=")
    )
    if player_script and player_script.string:
        pm = re.search(r"var\s+player_\w+\s*=\s*(\{.*\})\s*;?\s*$",
                       player_script.string, re.S)
        if pm:
            try:
                data = json.loads(pm.group(1))
                media_url = decode_media_url(data.get("url", ""))
                vod_data = data.get("vod_data") or {}
                if isinstance(vod_data, dict):
                    actor = (vod_data.get("vod_actor") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass

    # Published date
    published_at = ""
    for a in soup.select("div.video-countext-categories a.btn-info"):
        dm = re.search(r"(\d{4}-\d{1,2}-\d{1,2})", a.text)
        if dm:
            published_at = dm.group(1)
            break

    video = {
        "vod_id": vod_id,
        "title": title,
        "media_url": media_url,
        "cover_image": cover,
        "actor": actor,
        "published_at": published_at,
        "tags": [],
        "category": "",
        "duration_seconds": None,
        "page_url": url,
    }

    item = build_item(video)
    if not item:
        log("ERROR: could not build valid item from page")
        sys.exit(1)
    emit(item)
    return item


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"{CRAWLER_NAME} - KanAV (kanav.ad) Video Crawler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--job", "-j",
        type=str,
        default=None,
        help="Path to job.json for crawler orchestration",
    )
    parser.add_argument(
        "--url", "-u",
        type=str,
        default=None,
        help="Scrape a single video by its detail page URL (test mode)",
    )

    args = parser.parse_args()

    if args.job:
        run_job(args.job)
    elif args.url:
        log(f"Test mode: scraping single video {args.url}")
        scrape_single_video(args.url)
    else:
        parser.print_help()
        print(
            "\n[ERROR] Specify --job (job.json) or --url (test mode)",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user")
        sys.exit(0)
    except BrokenPipeError:
        # stdout closed by reader - exit silently
        sys.exit(0)
