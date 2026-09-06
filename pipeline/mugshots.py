"""Find a booking photo for each story.

For every story not yet checked, fetch the article page, collect candidate
images (og:image, twitter:image, and <img> tags whose src, alt, or nearby
caption mentions a mugshot, booking, jail, sheriff, police, or the
offender's last name), and ask Claude Haiku (vision) whether each is a
booking photograph of one person. The best URL is stored in mugshot_url;
the page hot-links it. Nothing is copied.

    python pipeline/mugshots.py [--limit N] [--recheck]

Runs on new rows at the end of the daily and backfill jobs, and over the
whole database once via the "Mug shot sweep" workflow.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import unescape
from urllib.parse import urljoin

import anthropic
import requests

from config import BACKFILL_STORIES_CSV, CLASSIFY_MODEL, STORIES_CSV
from fetch import BROWSER_UA, FETCH_TIMEOUT
from store import load_stories, save_stories

WORKERS = 6
MAX_CANDIDATES = 4
MAX_IMAGE_BYTES = 4_000_000

_KEYWORDS = re.compile(r"mug ?shot|booking|jail|sheriff|police|arrest|inmate|custody|"
                       r"correction|detention|department|charged|suspect", re.I)
_SKIP = re.compile(r"logo|icon|avatar|sprite|placeholder|banner|advert|\.svg|\.gif|"
                   r"pixel|badge|weather|share|social|facebook|twitter|author|byline|"
                   r"headshot-staff|reporter|anchor", re.I)
_META_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)', re.I)
_META_RE2 = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](og:image|twitter:image)', re.I)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z-]+)\s*=\s*["\']([^"\']*)["\']')
_FIGCAP_RE = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.I | re.S)


def candidate_images(html: str, page_url: str, last_name: str = "") -> list[str]:
    """Image URLs worth asking about, most promising first."""
    scored: dict[str, int] = {}

    def add(url: str, score: int):
        url = unescape(url.strip())
        if not url or url.startswith("data:"):
            return
        url = urljoin(page_url, url)
        if _SKIP.search(url):
            return
        scored[url] = max(scored.get(url, 0), score)

    for m in _META_RE.finditer(html):
        add(m.group(2), 3)
    for m in _META_RE2.finditer(html):
        add(m.group(1), 3)

    captions = " ".join(re.sub(r"<[^>]+>", " ", c) for c in _FIGCAP_RE.findall(html))
    for tag in _IMG_RE.findall(html):
        attrs = dict((k.lower(), v) for k, v in _ATTR_RE.findall(tag))
        src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src") or ""
        if not src and attrs.get("srcset"):
            src = attrs["srcset"].split(",")[0].split()[0]
        if not src:
            continue
        text = " ".join([attrs.get("alt", ""), attrs.get("title", ""), src])
        score = 1
        if _KEYWORDS.search(text):
            score += 3
        if last_name and re.search(re.escape(last_name), text, re.I):
            score += 3
        try:
            w = int(re.sub(r"\D", "", attrs.get("width", "") or "0") or 0)
            if 0 < w < 120:
                continue
        except ValueError:
            pass
        add(src, score)
    if last_name and re.search(re.escape(last_name), captions, re.I):
        # Captioned images are usually the ones in the article body; bump them.
        for url in list(scored):
            if scored[url] < 3:
                scored[url] += 1
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:MAX_CANDIDATES]]


def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": BROWSER_UA})
        if r.ok and "html" in r.headers.get("content-type", ""):
            return r.text
    except requests.RequestException:
        pass
    return None


_QUESTION = ("Is this image a booking photograph (a mugshot) of a single person, as taken by "
             "police or a jail? Answer with only a JSON object: "
             '{"is_mugshot": true|false, "confidence": "high"|"medium"|"low"}. '
             "A news photo of a person in court, a crime scene, a police badge, a building, "
             "a reporter, or a group of people is not a mugshot.")


def _ask(client: anthropic.Anthropic, image_block: dict) -> tuple[bool, str]:
    resp = client.messages.create(
        model=CLASSIFY_MODEL, max_tokens=100,
        messages=[{"role": "user", "content": [image_block, {"type": "text", "text": _QUESTION}]}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return False, "low"
    d = json.loads(m.group(0))
    return bool(d.get("is_mugshot")), str(d.get("confidence", "low"))


def is_mugshot(client: anthropic.Anthropic, image_url: str) -> bool:
    """Ask by URL first (the API fetches it); if the host blocks that,
    download and send the bytes."""
    try:
        ok, conf = _ask(client, {"type": "image", "source": {"type": "url", "url": image_url}})
        return ok and conf != "low"
    except anthropic.BadRequestError:
        pass
    try:
        r = requests.get(image_url, timeout=FETCH_TIMEOUT, headers={"User-Agent": BROWSER_UA},
                         stream=True)
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        if not r.ok or ctype not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            return False
        data = r.raw.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return False
        ok, conf = _ask(client, {"type": "image", "source": {
            "type": "base64", "media_type": ctype,
            "data": base64.standard_b64encode(data).decode()}})
        return ok and conf != "low"
    except (requests.RequestException, anthropic.APIError, ValueError):
        return False


def find_mugshot(client: anthropic.Anthropic, story: dict) -> str:
    html = fetch_html(story["source_url"])
    if not html:
        return ""
    last = (story.get("offender_name") or "").split()[-1] if story.get("offender_name") else ""
    for url in candidate_images(html, story["source_url"], last):
        if is_mugshot(client, url):
            return url
    return ""


def sweep(client: anthropic.Anthropic, path, limit: int = 0, recheck: bool = False) -> dict:
    stories = load_stories(path)
    todo = [s for s in stories if recheck or not s.get("mugshot_checked")]
    if limit:
        todo = todo[:limit]
    counts = {"checked": 0, "found": 0}
    if not todo:
        return counts
    print(f"{path.name}: checking {len(todo)} of {len(stories)} stories")

    def work(s):
        try:
            return find_mugshot(client, s)
        except Exception as e:  # noqa: BLE001
            print(f"  error on id {s['id']}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for s, url in zip(todo, pool.map(work, todo)):
            if url is None:
                continue  # error: leave unchecked for next time
            s["mugshot_url"] = url
            s["mugshot_checked"] = date.today().isoformat()
            counts["checked"] += 1
            if url:
                counts["found"] += 1
                print(f"  id {s['id']}: {url[:100]}")
    save_stories(stories, path)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="stories per file (0 = all)")
    parser.add_argument("--recheck", action="store_true", help="re-examine already checked stories")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")
    client = anthropic.Anthropic()
    t0 = time.monotonic()
    total = {"checked": 0, "found": 0}
    for path in (STORIES_CSV, BACKFILL_STORIES_CSV):
        if path.exists():
            c = sweep(client, path, args.limit, args.recheck)
            total["checked"] += c["checked"]
            total["found"] += c["found"]
    print(f"Mug shots: found {total['found']} of {total['checked']} checked "
          f"in {int(time.monotonic() - t0)}s")


if __name__ == "__main__":
    main()
