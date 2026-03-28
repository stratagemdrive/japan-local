#!/usr/bin/env python3
"""
fetch_japan_news.py
────────────────────────────────────────────────────────────────────
Fetches RSS headlines from Japanese English-language news sources,
translates Japanese titles to English where needed, categorises each
story, and writes/merges them into docs/japan_news.json.

Target categories (20 stories each, Japan as primary subject):
  Diplomacy | Military | Energy | Economy | Local Events

Output: docs/japan_news.json
URL:    https://stratagemdrive.github.io/japan-local/japan_news.json
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import requests
from dateutil import parser as dateparser
from deep_translator import GoogleTranslator

# ──────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("docs")
OUTPUT_FILE = OUTPUT_DIR / "japan_news.json"
MAX_PER_CATEGORY = 20
MAX_AGE_DAYS = 7
COUNTRY = "japan"
REQUEST_TIMEOUT = 20  # seconds per feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────
# RSS SOURCES
# Notes:
#   • NHK World-Japan  – fully English; multiple topic feeds used.
#   • The Japan Times  – fully English.
#   • Japan Today      – fully English.
#   • Kyodo News       – fully English.
#   • Nippon.com       – fully English.
#   • The Japan News (Yomiuri) – fully English.
#   • The Mainichi     – fully English.
#   • Asahi Shimbun AJW – English; the old rss.asahi.com category
#       feeds are deprecated. We use the current ajw feed URL; the
#       script skips gracefully if the feed is unavailable.
#   • Foreign Press Center Japan – mostly English; occasional
#       Japanese titles are translated below.
#   • SoraNews24       – fully English.
# ──────────────────────────────────────────────────────────────────
RSS_SOURCES = [
    # ── NHK World-Japan (English) ──────────────────────────────────
    {
        "source": "NHK World Japan",
        "url": "https://www3.nhk.or.jp/rss/news/cat0.xml",  # all news
    },
    {
        "source": "NHK World Japan",
        "url": "https://www3.nhk.or.jp/rss/news/cat6.xml",  # politics
    },
    {
        "source": "NHK World Japan",
        "url": "https://www3.nhk.or.jp/rss/news/cat1.xml",  # science/culture
    },
    # ── The Japan Times ───────────────────────────────────────────
    {
        "source": "The Japan Times",
        "url": "https://www.japantimes.co.jp/feed/",
    },
    {
        "source": "The Japan Times",
        "url": "https://www.japantimes.co.jp/news/feed/",
    },
    # ── Japan Today ───────────────────────────────────────────────
    {
        "source": "Japan Today",
        "url": "https://japantoday.com/feed",
    },
    {
        "source": "Japan Today",
        "url": "https://japantoday.com/category/politics/feed",
    },
    {
        "source": "Japan Today",
        "url": "https://japantoday.com/category/business/feed",
    },
    # ── Kyodo News ────────────────────────────────────────────────
    {
        "source": "Kyodo News",
        "url": "https://english.kyodonews.net/rss/all.xml",
    },
    # ── Nippon.com ────────────────────────────────────────────────
    {
        "source": "Nippon.com",
        "url": "https://www.nippon.com/en/feed/rss2/",
    },
    {
        "source": "Nippon.com",
        "url": "https://www.nippon.com/en/news/feed/rss2/",
    },
    # ── The Japan News (Yomiuri Shimbun) ──────────────────────────
    {
        "source": "The Japan News",
        "url": "https://japannews.yomiuri.co.jp/feed/",
    },
    # ── The Mainichi ──────────────────────────────────────────────
    {
        "source": "The Mainichi",
        "url": "https://mainichi.jp/english/feed/rss",
    },
    # ── Asahi Shimbun (Asia & Japan Watch) ────────────────────────
    # Primary: current AJW feed; fallback skipped silently if 404.
    {
        "source": "Asahi Shimbun",
        "url": "https://www.asahi.com/ajw/rss.rdf",
    },
    {
        "source": "Asahi Shimbun",
        "url": "https://rss.asahi.com/rss/asahi/newsheadlines.rdf",
    },
    # ── Foreign Press Center Japan ────────────────────────────────
    {
        "source": "Foreign Press Center Japan",
        "url": "https://fpcj.jp/feed/",
    },
    # ── SoraNews24 ────────────────────────────────────────────────
    {
        "source": "SoraNews24",
        "url": "https://soranews24.com/feed/",
    },
]

# ──────────────────────────────────────────────────────────────────
# CATEGORY KEYWORD MAPS
# Keys are category names; values are lists of (pattern, weight).
# Scoring: sum weights of matched patterns; highest score wins.
# ──────────────────────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "Diplomacy": [
        (r"\b(diplomat|diplomacy|foreign\s+minister|foreign\s+ministry|mofa|ambassador|embassy|consulate)\b", 4),
        (r"\b(summit|bilateral|multilateral|treaty|agreement|alliance|pact)\b", 3),
        (r"\b(united\s+nations|un\s+security\s+council|g7|g20|quad|asean|nato)\b", 3),
        (r"\b(sanction|tariff\s+negotiation|trade\s+deal|trade\s+talk)\b", 3),
        (r"\b(prime\s+minister|foreign\s+policy|state\s+visit|official\s+visit)\b", 2),
        (r"\b(china|korea|us|usa|united\s+states|russia|india|taiwan)\b", 1),
        (r"\b(relations|talks|negotiation|dialogue)\b", 1),
    ],
    "Military": [
        (r"\b(military|defence|defense|jsdf|self[- ]defense\s+force|ground\s+self[- ]defense|maritime\s+self[- ]defense|air\s+self[- ]defense)\b", 5),
        (r"\b(missile|warship|fighter\s+jet|aircraft\s+carrier|destroyer|submarine|frigate)\b", 4),
        (r"\b(drill|exercise|wargame|combat|armed\s+forces|troops|soldier|sailor|airman)\b", 4),
        (r"\b(weapon|arms|ammunition|bomb|artillery|drone|uav)\b", 4),
        (r"\b(north\s+korea|dprk|pla|peoples?\s+liberation\s+army)\b", 3),
        (r"\b(security|deterrence|rearmament|defense\s+budget|defense\s+spending)\b", 3),
        (r"\b(pentagon|nato|alliance|collective\s+defense)\b", 2),
    ],
    "Energy": [
        (r"\b(nuclear|reactor|tepco|npp|power\s+plant)\b", 5),
        (r"\b(renewable|solar|wind\s+power|geothermal|hydrogen|fuel\s+cell)\b", 4),
        (r"\b(oil|gas|lng|petroleum|crude|refinery|pipeline)\b", 4),
        (r"\b(electricity|grid|power\s+outage|blackout|kilowatt|megawatt)\b", 3),
        (r"\b(carbon|emission|net.zero|climate\s+goal|greenhouse)\b", 3),
        (r"\b(energy\s+security|energy\s+policy|power\s+supply|utility)\b", 3),
        (r"\b(coal|fossil\s+fuel|decarboni[sz])\b", 3),
    ],
    "Economy": [
        (r"\b(gdp|economic\s+growth|recession|inflation|deflation|cpi)\b", 5),
        (r"\b(bank\s+of\s+japan|boj|interest\s+rate|monetary\s+policy|yield\s+curve)\b", 5),
        (r"\b(trade|export|import|current\s+account|trade\s+balance|trade\s+deficit|trade\s+surplus)\b", 4),
        (r"\b(stock|nikkei|topix|yen|currency|forex|exchange\s+rate)\b", 4),
        (r"\b(budget|fiscal|tax|subsidy|stimulus|spending\s+plan)\b", 4),
        (r"\b(company|corporate|merger|acquisition|bankruptcy|startup|ipo)\b", 3),
        (r"\b(wage|salary|labour|labor|employment|unemployment|job)\b", 3),
        (r"\b(semiconductor|chip|supply\s+chain|manufacturing|industry)\b", 2),
    ],
    "Local Events": [
        (r"\b(earthquake|tsunami|typhoon|flood|landslide|volcanic|eruption|disaster|evacuation)\b", 5),
        (r"\b(festival|matsuri|ceremony|celebration|fireworks|parade)\b", 4),
        (r"\b(prefecture|municipality|city\s+hall|governor|mayor|ward)\b", 4),
        (r"\b(crime|arrest|murder|robbery|fraud|scam|court|verdict|sentence)\b", 4),
        (r"\b(school|university|education|student|teacher|exam|entrance)\b", 3),
        (r"\b(hospital|medical|health\s+care|clinic|patient|disease|outbreak)\b", 3),
        (r"\b(sport|athletics|baseball|football|soccer|sumo|judo|karate|marathon|olympics)\b", 3),
        (r"\b(culture|art|museum|film|movie|anime|manga|traditional)\b", 2),
        (r"\b(local|regional|community|neighbourhood|neighborhood|village|town)\b", 2),
        (r"\b(weather|snow|rain|temperature|heatwave|cold\s+snap)\b", 2),
    ],
}

# ──────────────────────────────────────────────────────────────────
# JAPAN RELEVANCE KEYWORDS
# A story must reference Japan directly to be included.
# ──────────────────────────────────────────────────────────────────
JAPAN_KEYWORDS = re.compile(
    r"\b(japan(ese)?|tokyo|osaka|kyoto|hiroshima|nagasaki|sapporo|fukuoka|"
    r"nagoya|yokohama|kobe|sendai|okinawa|hokkaido|honshu|kyushu|shikoku|"
    r"nikkei|yen|jsdf|jfa|ldp|dpj|cdp|komeito|nippon|waseda|keio|"
    r"abe|kishida|ishiba|suga|koizumi)\b",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────

def _story_id(url: str) -> str:
    """Stable unique ID from story URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _needs_translation(text: str) -> bool:
    """Heuristic: >25% non-ASCII → assume Japanese."""
    if not text:
        return False
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii / len(text) > 0.25


def _translate(text: str) -> str:
    """Translate text to English using Google Translate (free tier)."""
    if not text or not _needs_translation(text):
        return text
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(text)
        return translated or text
    except Exception as exc:
        log.warning("Translation failed for '%s…': %s", text[:40], exc)
        return text


def _parse_date(entry) -> Optional[datetime]:
    """Extract and normalise a publication datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for attr in ("published", "updated", "created"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return dateparser.parse(val).astimezone(timezone.utc)
            except Exception:
                pass
    return None


def _score_category(text: str) -> str:
    """Return the best-matching category for the given text."""
    text_lower = text.lower()
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, rules in CATEGORY_KEYWORDS.items():
        for pattern, weight in rules:
            if re.search(pattern, text_lower):
                scores[cat] += weight
    best = max(scores, key=scores.get)
    # If no category scored > 0, default to Local Events
    return best if scores[best] > 0 else "Local Events"


def _is_japan_relevant(title: str, summary: str = "") -> bool:
    """Return True if the story is about Japan."""
    combined = f"{title} {summary}"
    return bool(JAPAN_KEYWORDS.search(combined))


def _fetch_feed(source_cfg: dict) -> list[dict]:
    """Fetch a single RSS feed and return a list of normalised story dicts."""
    source = source_cfg["source"]
    url = source_cfg["url"]
    stories = []

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; StratagemdrivBot/1.0; "
                "+https://stratagemdrive.github.io/japan-local/)"
            )
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            log.warning("Feed not found (404): %s — skipping.", url)
            return []
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        log.warning("Could not fetch feed %s: %s", url, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    for entry in feed.entries:
        try:
            raw_title = getattr(entry, "title", "") or ""
            raw_title = raw_title.strip()
            if not raw_title:
                continue

            story_url = getattr(entry, "link", "") or ""
            story_url = story_url.strip()

            pub_date = _parse_date(entry)
            if pub_date and pub_date < cutoff:
                continue  # too old

            summary = getattr(entry, "summary", "") or ""
            # Strip HTML tags from summary
            summary = re.sub(r"<[^>]+>", " ", summary).strip()

            # Translate if needed
            title = _translate(raw_title)
            summary_en = _translate(summary) if _needs_translation(summary) else summary

            # Japan relevance check
            if not _is_japan_relevant(title, summary_en):
                continue

            category = _score_category(f"{title} {summary_en}")

            pub_date_str = (
                pub_date.strftime("%Y-%m-%dT%H:%M:%SZ") if pub_date else ""
            )

            stories.append(
                {
                    "id": _story_id(story_url or raw_title),
                    "title": title,
                    "source": source,
                    "url": story_url,
                    "published_date": pub_date_str,
                    "category": category,
                }
            )
        except Exception as exc:
            log.debug("Skipping entry due to error: %s", exc)
            continue

    log.info("  %s  →  %d usable stories", url, len(stories))
    return stories


# ──────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────

def load_existing() -> dict[str, list[dict]]:
    """Load existing JSON grouped by category."""
    if OUTPUT_FILE.exists():
        try:
            with OUTPUT_FILE.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "stories" in data:
                grouped: dict[str, list[dict]] = {c: [] for c in CATEGORY_KEYWORDS}
                for story in data["stories"]:
                    cat = story.get("category", "Local Events")
                    if cat in grouped:
                        grouped[cat].append(story)
                return grouped
        except Exception as exc:
            log.warning("Could not parse existing JSON (%s); starting fresh.", exc)
    return {c: [] for c in CATEGORY_KEYWORDS}


def merge(
    existing: dict[str, list[dict]],
    incoming: list[dict],
) -> dict[str, list[dict]]:
    """
    Merge new stories into existing buckets.

    Rules:
      1. Deduplicate by story ID.
      2. Drop stories older than MAX_AGE_DAYS.
      3. Add new stories first (newest wins the slot).
      4. If a bucket exceeds MAX_PER_CATEGORY, drop oldest entries first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    def _age_ok(story: dict) -> bool:
        if not story.get("published_date"):
            return True  # keep if date unknown
        try:
            dt = datetime.fromisoformat(story["published_date"].replace("Z", "+00:00"))
            return dt >= cutoff
        except Exception:
            return True

    # Build a combined pool per category
    pools: dict[str, dict[str, dict]] = {c: {} for c in CATEGORY_KEYWORDS}

    # Seed with existing (filter stale)
    for cat, stories in existing.items():
        for s in stories:
            if _age_ok(s):
                pools[cat][s["id"]] = s

    # Add incoming (new stories override if same ID)
    for s in incoming:
        cat = s["category"]
        pools[cat][s["id"]] = s

    # Sort each bucket newest-first, trim to MAX_PER_CATEGORY
    def _sort_key(s: dict) -> datetime:
        try:
            return datetime.fromisoformat(s["published_date"].replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    result: dict[str, list[dict]] = {}
    for cat, pool in pools.items():
        sorted_stories = sorted(pool.values(), key=_sort_key, reverse=True)
        result[cat] = sorted_stories[:MAX_PER_CATEGORY]

    return result


def build_output(merged: dict[str, list[dict]]) -> dict:
    """Flatten merged dict into the final JSON structure."""
    all_stories = []
    for cat_stories in merged.values():
        for s in cat_stories:
            all_stories.append(
                {
                    "title": s["title"],
                    "source": s["source"],
                    "url": s["url"],
                    "published_date": s["published_date"],
                    "category": s["category"],
                }
            )
    return {
        "country": COUNTRY,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "story_count": len(all_stories),
        "stories": all_stories,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=== Japan News Aggregator starting ===")

    # 1. Fetch all feeds
    all_incoming: list[dict] = []
    seen_ids: set[str] = set()

    for cfg in RSS_SOURCES:
        log.info("Fetching: %s", cfg["url"])
        stories = _fetch_feed(cfg)
        for s in stories:
            if s["id"] not in seen_ids:
                all_incoming.append(s)
                seen_ids.add(s["id"])
        time.sleep(0.5)  # polite crawl delay

    log.info("Total unique incoming stories: %d", len(all_incoming))

    # 2. Load existing output
    existing = load_existing()

    # 3. Merge
    merged = merge(existing, all_incoming)

    # 4. Log category counts
    for cat, stories in merged.items():
        log.info("  %-16s : %d stories", cat, len(stories))

    # 5. Write output
    output = build_output(merged)
    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    log.info("Written %d stories to %s", output["story_count"], OUTPUT_FILE)
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
