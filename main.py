import os
import sys
import time
import base64
import math
import urllib.parse
import re
import json
import html as html_lib
import threading
from collections import Counter

from datetime import datetime, timedelta, timezone

# Non-blocking key detection: msvcrt on Windows, select on Mac/Linux.
try:
    import msvcrt
except ImportError:
    msvcrt = None

if msvcrt is None:
    import select

from core.browser import launch_authenticated_browser
from core.sheets import connect_sheets, append_violation_row
from core.config_loader import load_daily_state, save_daily_state
from core.docs import send_report_to_docs

# Private settings (sheet links, document names) live in .env, which is not committed.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env is optional: real environment variables still work


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

WIB = timezone(timedelta(hours=7))

def _env_text(name, default=""):
    """Read a text setting from .env. A literal \\n in the value becomes a line break."""
    return os.getenv(name, default).replace("\\n", "\n").strip()


# Private settings, read from .env (see .env.example).
STAGING_SHEET_URL = os.getenv("STAGING_SHEET_URL", "")
OFFICIAL_SHEET_URL = os.getenv("OFFICIAL_SHEET_URL", "")
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "")   # spreadsheet that receives the fines
DOC_NAME = os.getenv("DOC_NAME", "")                   # Google Doc that receives the group report

# Group report wording, also from .env. The payment terms hold bank details, so keep them out of the code.
REPORT_TITLE = _env_text("REPORT_TITLE", "Denda Paid Promote")
REPORT_GREETING = _env_text("REPORT_GREETING", "Selamat pagi!")
REPORT_INTRO = _env_text("REPORT_INTRO", "Berikut daftar nama yang belum memenuhi ketentuan penugasan.")
REPORT_PAYMENT_TERMS = _env_text("REPORT_PAYMENT_TERMS")   # one line per rule; empty = section left out

# Task window (WIB): only stories/posts published inside it count.
STORY_START_HOUR = 11
STORY_START_MINUTE = 0
STORY_END_HOUR = 22
STORY_END_MINUTE = 15

# "YYYY-MM-DD" when stories are checked the day after (stories live 24h). None = today.
STORY_TARGET_DATE = None

# Accounts confirmed Correct are cached here so re-runs skip them.
CHECKED_ACCOUNTS_FILENAME = "checked_accounts.json"

# A story/post is a task item only if it carries ALL of these hashtags.
# Set GATEKEEPER_KEYWORDS in .env as comma-separated tags, e.g. GATEKEEPER_KEYWORDS=tag1,tag2
GATEKEEPER_KEYWORDS = [
    tag.strip().lstrip("#")
    for tag in os.getenv("GATEKEEPER_KEYWORDS", "").split(",")
    if tag.strip().lstrip("#")
]

MAX_SLIDES = 50
MAX_FEED_POSTS_SCAN = 12          # hard cap of recent feed posts opened per account
FEED_POSTS_PER_TASK_SCAN = 3      # recent posts opened per Feed task (capped by the value above)
FEED_STOP_AFTER_OLD_POSTS = 2     # stop scanning after this many consecutive posts older than the window

ALLOW_TEXT_FALLBACK = True        # also match hashtags typed as plain text
FEED_MAX_LINKED_HASHTAGS = 5      # Instagram only turns the first 5 caption hashtags into links; later ones lose their '#'
REQUIRE_VISIBLE_TAG_ANCHOR = True # ignore off-screen neighbour slides rendered in the DOM
STICKER_WAIT = 1.2                # max seconds to wait for stickers to render on a slide

USE_PARALLEL_TABS = True          # Story + Feed tasks: Story and Feed are checked at the same time in two browser sessions
PARALLEL_FEED_LAUNCH_TIMEOUT = 90 # max seconds the Story session waits to learn whether the Feed session started

USE_STORY_API_CAPTURE = True      # read hashtag/mention/link data from Instagram's own JSON responses
USE_OCR_FALLBACK = True           # last resort for text drawn directly on the image

HIRES_SCALE = 4.0                 # render scale used to capture the story card
STICKER_PAD_X = 6                 # crop padding (CSS px) around sticker hitboxes
STICKER_PAD_Y = 1
MIN_STICKER_HITBOX_FOR_LINK_OCR = 4  # fewer hitboxes -> not the sticker-link slide, skip OCR

# Internal status values. They are also written to Google Sheets, so keep them unchanged.
STATUS_BENAR = "Benar"
STATUS_SALAH = "Kesalahan"
STATUS_TDK_POST = "Tdk Post"
STATUS_REVIEW = "Manual Review"

# Console labels for the statuses above.
STATUS_LABEL = {
    STATUS_BENAR: "Correct",
    STATUS_SALAH: "Mistake",
    STATUS_TDK_POST: "Not done",
    STATUS_REVIEW: "Manual review",
}

# Fine per task (IDR).
NOMINAL = {
    STATUS_TDK_POST: 25000,
    STATUS_SALAH: 10000,
    STATUS_REVIEW: 0,
}

_NEXT_LABELS = [
    "Next",
    "Berikutnya",
    "Selanjutnya",
    "Lanjut",
]

_PREV_LABELS = [
    "Previous",
    "Go back",
    "Kembali",
    "Sebelumnya",
]

# Instagram UI text (both languages) used to detect page states.
_UNAVAILABLE_MARKERS = [
    "sorry, this page isn't available",
    "sorry, this page isn’t available",
    "halaman ini tidak tersedia",
    "halaman tidak tersedia",
]

_PRIVATE_MARKERS = [
    "this account is private",
    "akun ini bersifat pribadi",
    "akun ini privat",
]


# ---------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------

RULE = "=" * 64
THIN = "-" * 64
_LOG_TAGS = {"ok": " OK ", "warn": "WARN", "skip": "SKIP", "fail": "FAIL", "info": "INFO"}


_PRINT_LOCK = threading.Lock()
_log_ctx = threading.local()      # per-thread label (e.g. "(Story) ") shown in front of every log message


def log(kind, message, level=0):
    """Print one aligned log line: `[TAG] message`, indented by `level`."""
    label = getattr(_log_ctx, "label", "")

    with _PRINT_LOCK:
        print(f"{'    ' * level}[{_LOG_TAGS[kind]}] {label}{message}")


def log_item(kind, label, elapsed, detail, level=2):
    """Print a per-slide/per-post line with its duration in seconds."""
    log(kind, f"{label:<8}{elapsed:>6.1f}s | {detail}", level)


def section(title):
    """Print a boxed section title."""
    print(f"\n{RULE}\n{title}\n{RULE}")


def secs(start_time):
    """Seconds elapsed since `start_time` (a time.time() value)."""
    return time.time() - start_time


# ---------------------------------------------------------------------
# Errors and time helpers
# ---------------------------------------------------------------------

class InstagramSessionError(Exception):
    """Raised when the Instagram session is invalid (login or checkpoint page)."""


def now_wib():
    """Current time in WIB (UTC+7)."""
    return datetime.now(WIB)


_REL_TIME_RE = re.compile(
    r"^\s*(\d+)\s*"
    r"(days|day|d|hari|"
    r"hours|hour|hrs|hr|h|jam|j|"
    r"minutes|minute|mins|min|menit|m|"
    r"seconds|second|secs|sec|detik|s)"
    r"\s*$",
    re.IGNORECASE,
)


def parse_relative_ig_time(text):
    """Convert an Instagram relative time ('5h', '2 jam', '30m') to a WIB datetime, or None."""
    if not text:
        return None

    t = str(text).strip().lower()

    if t in (
        "just now",
        "baru saja",
        "now",
        "sekarang",
    ):
        return now_wib()

    m = _REL_TIME_RE.match(t)

    if not m:
        return None

    n = int(m.group(1))
    unit = m.group(2).lower()

    if unit in (
        "days",
        "day",
        "d",
        "hari",
    ):
        return now_wib() - timedelta(days=n)

    if unit in (
        "hours",
        "hour",
        "hrs",
        "hr",
        "h",
        "jam",
        "j",
    ):
        return now_wib() - timedelta(hours=n)

    if unit in (
        "seconds",
        "second",
        "secs",
        "sec",
        "detik",
        "s",
    ):
        return now_wib() - timedelta(seconds=n)

    return now_wib() - timedelta(minutes=n)


def story_window():
    """Return the (start, end) task window as WIB datetimes for the target day."""
    ref = now_wib()

    if STORY_TARGET_DATE:
        d = datetime.strptime(STORY_TARGET_DATE, "%Y-%m-%d")
        ref = ref.replace(year=d.year, month=d.month, day=d.day)

    start = ref.replace(
        hour=STORY_START_HOUR,
        minute=STORY_START_MINUTE,
        second=0,
        microsecond=0,
    )

    end = ref.replace(
        hour=STORY_END_HOUR,
        minute=STORY_END_MINUTE,
        second=0,
        microsecond=0,
    )

    return start, end


# ---------------------------------------------------------------------
# Hashtag parsing
# ---------------------------------------------------------------------

def _clean_tag(tag):
    """Normalise a hashtag: no '#', trimmed, lowercase."""
    return str(tag).strip().lstrip("#").strip().lower()


def _tag_in_text(tag, text_lower):
    """True if `#tag` appears in the text as a whole hashtag."""
    return re.search(r"#" + re.escape(tag) + r"(?!\w)", text_lower) is not None


def _tag_in_text_loose(tag, text_lower):
    """True if `tag` appears as a whole word, with or without '#'."""
    return re.search(r"(?<!\w)" + re.escape(tag) + r"(?!\w)", text_lower) is not None


def _collect_tags_from_url(url, tags, depth=0):
    """Collect hashtags from a URL path/query, following nested URLs (max depth 3)."""
    if not url or depth > 3:
        return
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return

    path = urllib.parse.unquote(parsed.path)
    m = re.search(r"/explore/tags/([^/?#]+)", path)
    if m:
        tags.add(_clean_tag(m.group(1)))

    for _key, values in urllib.parse.parse_qs(parsed.query).items():
        for value in values:
            value = value.strip()
            if "%" in value:
                value = urllib.parse.unquote(value)
            if value.startswith("#"):
                tag = _clean_tag(value)
                if tag:
                    tags.add(tag)
            elif value.lower().startswith(("http://", "https://", "/")):
                _collect_tags_from_url(value, tags, depth + 1)


def extract_hashtags_from_hrefs(hrefs):
    """Collect the hashtags referenced by a list of hrefs."""
    tags = set()
    for href in hrefs:
        _collect_tags_from_url(href, tags)
    return tags


def _href_is_tag(href, tag):
    """True if `href` points to the hashtag `tag`."""
    found = set()
    _collect_tags_from_url(href, found)
    return tag in found


# ---------------------------------------------------------------------
# Browser-side snapshot of the story slide
# ---------------------------------------------------------------------

_SNAPSHOT_JS = r"""
(tags) => {
  const timeEl = document.querySelector('header time') || document.querySelector('time');
  const grab = (sel) => Array.from(document.querySelectorAll(sel));
  const esc = (s) => String(s).replace(/["\\]/g, '\\$&');
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    return r.right > 0 && r.bottom > 0 && r.left < window.innerWidth && r.top < window.innerHeight;
  };

  const tagHits = {};
  for (const tag of (tags || [])) {
    const t = esc(String(tag).toLowerCase());
    const found = [];
    for (const sel of [`a[href*="/explore/tags/${t}" i]`, `a[href*="q=%23${t}" i]`]) {
      for (const el of document.querySelectorAll(sel)) {
        found.push({href: el.getAttribute('href') || '', visible: isVisible(el)});
      }
    }
    tagHits[tag] = found;
  }
  const tagAnchors = grab('a[href*="/explore/tags/" i], a[href*="q=%23" i]')
    .map(a => ({href: a.getAttribute('href') || '', visible: isVisible(a)}));

  const rectOf = (r) => ({x: r.left, y: r.top, w: r.width, h: r.height});
  const cardLike = (r) => r.height > 400 && r.width > 200 &&
                          (r.width / r.height) > 0.5 && (r.width / r.height) < 0.65;
  let card = null;
  for (let el = timeEl; el && el !== document.body && !card; el = el.parentElement) {
    const r = el.getBoundingClientRect();
    if (cardLike(r)) card = rectOf(r);
  }
  if (!card) {
    let best = null;
    for (const el of grab('video, img')) {
      const r = el.getBoundingClientRect();
      if (cardLike(r) && isVisible(el) && (!best || r.width * r.height > best.w * best.h)) best = rectOf(r);
    }
    card = best;
  }

  let media = null;
  if (card) {
    for (const el of grab('video, img')) {
      const r = el.getBoundingClientRect();
      const area = r.width * r.height;
      if (area < 0.5 * card.w * card.h) continue;
      const cx = r.left + r.width / 2;
      if (cx < card.x || cx > card.x + card.w) continue;   // slide tetangga (off-screen)
      if (!media || area > media.area) {
        media = {kind: el.tagName.toLowerCase(), src: el.currentSrc || el.src || '',
                 srcset: el.getAttribute('srcset') || '', area: area, rect: rectOf(r)};
      }
    }
  }

  const stickers = [];
  if (card) {
    for (const el of grab('[role="button"]')) {
      if (el.getAttribute('aria-label')) continue;
      if ((el.innerText || '').trim()) continue;
      if (el.querySelector('svg, img, canvas, video')) continue;   // tombol UI (pause, menu, kirim...)
      if (!isVisible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 6) continue;
      if (r.width > 0.9 * card.w && r.height > 0.5 * card.h) continue;   // area tap besar
      if (r.left < card.x - 2 || r.right > card.x + card.w + 2) continue;
      if (r.top < card.y - 2 || r.bottom > card.y + card.h + 2) continue;
      stickers.push({x: r.left - card.x, y: r.top - card.y, w: r.width, h: r.height});
    }
  }

  let progress = null;
  if (card) {
    const cands = [];
    for (const el of grab('div, span')) {
      const r = el.getBoundingClientRect();
      if (r.height < 1 || r.height > 5 || r.width < 8) continue;
      if (r.top < card.y - 2 || r.top > card.y + 34) continue;
      if (r.left < card.x - 2 || r.right > card.x + card.w + 2) continue;
      cands.push({el: el, r: r});
    }
    const set = new Set(cands.map(c => c.el));
    const roots = cands.filter(c => !set.has(c.el.parentElement));
    const tracks = [];
    for (const root of roots) {
      const kids = cands.filter(c => c.el.parentElement === root.el);
      if (kids.length >= 2) { for (const k of kids) tracks.push(k); } else { tracks.push(root); }
    }
    tracks.sort((a, b) => a.r.left - b.r.left);
    if (tracks.length >= 1 && tracks.length <= 60) {
      const ws = tracks.map(t => t.r.width);
      if (Math.max(...ws) / Math.min(...ws) <= 1.3) {
        const fills = tracks.map(t => {
          let best = 0;
          for (const c of cands) if (c.el !== t.el && t.el.contains(c.el)) best = Math.max(best, c.r.width / t.r.width);
          return Math.round(best * 100) / 100;
        });
        const cur = fills.findIndex(f => f < 0.98);
        progress = {total: tracks.length, current: cur === -1 ? tracks.length : cur + 1, fills: fills};
      }
    }
  }

  return {
    url: location.href,
    text: document.body ? (document.body.innerText || '') : '',
    datetime: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
    time_text: timeEl ? ((timeEl.innerText || '').trim()) : '',
    header_texts: grab('header span[dir="auto"]').map(e => (e.innerText || '').trim()),
    hrefs: grab('a[href]').map(a => a.getAttribute('href')).filter(Boolean),
    has_media: !!document.querySelector('video, img'),
    tag_hits: tagHits,
    tag_anchors: tagAnchors,
    card: card,
    media: media,
    stickers: stickers,
    progress: progress,
  };
}
"""


_QUICK_SIG_JS = """
() => {
  const t = document.querySelector('header time') || document.querySelector('time');
  return [location.href, t ? (t.getAttribute('datetime') || (t.innerText || '').trim()) : ''];
}
"""


def _take_snapshot(page, tags):
    """Run the snapshot script on the current story slide. Returns None on failure."""
    try:
        return page.evaluate(_SNAPSHOT_JS, list(tags))
    except Exception:
        return None


def _norm_sig(url, stamp):
    """Build a slide signature from its URL and timestamp."""
    stamp = (stamp or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}T", stamp):
        return ("dt", stamp)
    return (url or "", stamp)


def _quick_signature(page):
    """Cheap signature of the slide on screen, used to detect slide changes."""
    try:
        url, stamp = page.evaluate(_QUICK_SIG_JS)
        return _norm_sig(url, stamp)
    except Exception:
        return None


def _sig_from_snapshot(snap):
    """Signature of a snapshot (same format as _quick_signature)."""
    return _norm_sig(
        snap.get("url", ""),
        snap.get("datetime") or snap.get("time_text") or "",
    )


def _snapshot_ready(snap):
    """True when the snapshot shows a loaded story slide with media and a timestamp."""
    if not snap or not snap.get("has_media"):
        return False
    return "/stories/" in snap.get("url", "") and bool(snap.get("datetime") or snap.get("time_text"))


def _settled_snapshot(page, tags, timeout=6.0, sticker_wait=None):
    """Wait until the slide and its stickers have rendered, then return the snapshot (None on timeout)."""
    sticker_wait = STICKER_WAIT if sticker_wait is None else sticker_wait
    want = {_clean_tag(t) for t in tags if _clean_tag(t)}

    deadline = time.time() + timeout
    snap = None
    while time.time() < deadline:
        snap = _take_snapshot(page, tags)
        if _snapshot_ready(snap):
            break
        time.sleep(0.25)
    else:
        return None

    sig0 = _sig_from_snapshot(snap)
    end, last_n = time.time() + sticker_wait, None
    while time.time() < end:
        if want and want <= build_slide(0, snap)["hashtags"]:
            break
        time.sleep(0.3)
        nxt = _take_snapshot(page, tags)
        if not _snapshot_ready(nxt):
            break
        snap = nxt
        if _sig_from_snapshot(snap) != sig0:
            break
        n = (
            sum(1 for a in (snap.get("tag_anchors") or []) if a.get("visible"))
            + len(snap.get("stickers") or [])
        )
        if n > 0 and n == last_n:
            break
        last_n = n
    return snap


def _parse_snapshot_time(snap):
    """Return (posting time in WIB, raw text) from a snapshot, or (None, '')."""
    dt_attr = (snap.get("datetime") or "").strip()
    if dt_attr:
        try:
            dt = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(WIB), dt_attr
        except ValueError:
            pass

    candidates = [snap.get("time_text", "")] + list(snap.get("header_texts", []))
    for txt in candidates:
        parsed = parse_relative_ig_time(txt)
        if parsed:
            return parsed, txt
    return None, ""


# ---------------------------------------------------------------------
# Story API capture
# ---------------------------------------------------------------------

_STORY_HASHTAG_KEYS = {
    "story_hashtags", "story_hashtag", "hashtags", "hashtag", "tag",
}
_STORY_MENTION_KEYS = {
    "reel_mentions", "story_mentions", "mentions", "mention",
}
_STORY_LINK_KEYS = {
    "story_link_stickers", "story_cta", "link_stickers", "story_links",
}
_STORY_KEY_HINTS = ("hashtag", "mention", "link_sticker")


class StoryApiCapture:
    """Collects hashtag/mention/link data from the JSON responses Instagram fetches for a story.

    Official stickers reach the browser as data, separate from the pixels, so they can be
    read even when a sticker is styled beyond what OCR can handle. Text typed with the "Aa"
    tool is pixels only and never arrives as data; OCR covers that case.
    """

    def __init__(self, page):
        self._page = page
        self._tags = set()
        self._link_fragments = set()
        self._handler = None

    def attach(self):
        """Start listening to responses. Must run before the story viewer opens."""

        def _on_response(response):
            try:
                if response.status >= 400:
                    return
                url = response.url or ""
                ctype = ((response.headers or {}).get("content-type", "") or "").lower()
                looks_api = ("/graphql" in url) or ("/api/v1/" in url)
                if not (looks_api or "json" in ctype):
                    return
                raw = (response.text() or "").lstrip()
                if raw.startswith("for (;;);"):
                    raw = raw[len("for (;;);"):]
                body = json.loads(raw)
            except Exception:
                return
            self._extract(body)

        self._handler = _on_response
        try:
            self._page.on("response", self._handler)
        except Exception as e:
            log("warn", f"Story API capture unavailable: {e}", 1)
            self._handler = None

    def detach(self):
        """Stop listening to responses."""
        if self._handler is None:
            return
        try:
            self._page.remove_listener("response", self._handler)
        except Exception:
            pass
        self._handler = None

    def _extract(self, node):
        """Walk the JSON and collect values under hashtag/mention/link keys."""
        if isinstance(node, dict):
            for key, value in node.items():
                lk = str(key).lower()
                if (
                    lk in _STORY_HASHTAG_KEYS
                    or lk in _STORY_MENTION_KEYS
                    or lk in _STORY_LINK_KEYS
                    or any(hint in lk for hint in _STORY_KEY_HINTS)
                ):
                    self._collect_tag_values(value)
                self._extract(value)
        elif isinstance(node, list):
            for item in node:
                self._extract(item)

    def _collect_tag_values(self, value):
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            candidates = []
            if isinstance(entry, dict):
                for k in ("name", "tag_name", "text", "username", "link_url", "url"):
                    if entry.get(k):
                        candidates.append(entry[k])
                if isinstance(entry.get("hashtag"), dict) and entry["hashtag"].get("name"):
                    candidates.append(entry["hashtag"]["name"])
            elif isinstance(entry, str):
                candidates.append(entry)

            for raw in candidates:
                stripped = str(raw).strip()
                if not stripped:
                    continue
                if stripped.lower().startswith(("http://", "https://")):
                    self._link_fragments.add(stripped.lower())
                    found = set()
                    _collect_tags_from_url(stripped, found)
                    self._tags |= found
                else:
                    clean = _clean_tag(stripped)
                    if clean:
                        self._tags.add(clean)

    def tags(self):
        return set(self._tags)

    def link_fragments(self):
        return set(self._link_fragments)


# ---------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------

def _normalize_ocr_token(text):
    """Normalise OCR text for comparison: lowercase, no spaces, common look-alike characters fixed."""
    text = _clean_tag(text)
    text = re.sub(r"\s+", "", text)
    return text.translate(str.maketrans({"0": "o", "1": "l", "|": "i", "ı": "i"}))


def _edit_ratio(a, b):
    """Similarity ratio between two strings (0-1)."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def _ocr_tag_match(tag, candidate):
    """Fuzzy-match a hashtag against one OCR candidate string."""
    tag_n = _normalize_ocr_token(tag)
    cand_n = _normalize_ocr_token(candidate)
    if not tag_n or not cand_n:
        return False

    tag_c = tag_n.replace("_", "")
    cand_c = cand_n.replace("_", "")

    if len(cand_c) < max(4, int(len(tag_c) * 0.6)):
        return False

    if "_" in tag_n:
        base, suffix = tag_n.rsplit("_", 1)
        base = base.replace("_", "")
        if len(cand_c) < len(suffix) + 3:
            return False
        tail = cand_c[-len(suffix):]
        head = cand_c[:-len(suffix)]
        suffix_ok = tail == suffix or (len(suffix) >= 5 and _edit_ratio(tail, suffix) >= 0.79)
        if not suffix_ok:
            return False
        return base in head or _edit_ratio(head, base) >= 0.85

    if tag_c in cand_c:
        return len(cand_c) - len(tag_c) <= 2
    return _edit_ratio(cand_c, tag_c) >= 0.80


def _ocr_match_watch_tags(ocr_text, watch_tags):
    """Return the watch tags found in the OCR text, including tokens split across words."""
    tokens = re.findall(r"[a-zA-Z0-9_|ı]+", ocr_text or "")
    cands = list(tokens)
    for n in range(2, 7):
        for i in range(len(tokens) - n + 1):
            cands.append("".join(tokens[i:i + n]))
    return {t for t in watch_tags if any(_ocr_tag_match(t, c) for c in cands)}


def _decode_image(data):
    """Decode image bytes with OpenCV. Returns None on failure."""
    try:
        import cv2
        import numpy as np
        if not data:
            return None
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _cdp_capture_card(page, card, scale):
    """Capture the story card at high resolution through the Chrome DevTools Protocol."""
    try:
        cdp = page.context.new_cdp_session(page)
        try:
            res = cdp.send("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": False,
                "clip": {
                    "x": card["x"], "y": card["y"],
                    "width": card["w"], "height": card["h"],
                    "scale": scale,
                },
            })
        finally:
            try:
                cdp.detach()
            except Exception:
                pass
        return base64.b64decode(res["data"])
    except Exception as e:
        log("warn", f"Hi-res capture failed ({e}), falling back to a 1x screenshot", 2)
        return None


def _pick_source_url(media):
    """Pick the largest image URL from a media element's srcset/src."""
    srcset = media.get("srcset") or ""
    best, best_w = None, 0
    for url, w in re.findall(r"(\S+)\s+(\d+)w", srcset):
        if int(w) > best_w:
            best, best_w = url, int(w)
    url = best or media.get("src") or ""
    return url if url.lower().startswith(("http://", "https://")) else None


def _fetch_media_bytes(page, media):
    """Download the original story image. Returns None for videos or on failure."""
    try:
        if not media or media.get("kind") != "img":
            return None
        url = _pick_source_url(media)
        if not url:
            return None
        resp = page.context.request.get(url, timeout=5000)
        return resp.body() if resp.ok else None
    except Exception:
        return None


def _grab_slide_media(page, snap):
    """Capture a slide as a hi-res card image plus one crop per sticker hitbox, for OCR."""
    import cv2

    card = snap.get("card")
    cap = {
        "card": card,
        "stickers": list(snap.get("stickers") or []),
        "crops": [],
        "card_jpg": None,
        "screen_png": None,
    }

    if not card:
        try:
            cap["screen_png"] = page.screenshot(type="png")
        except Exception:
            pass

    sources = []
    if card:
        raw = _cdp_capture_card(page, card, HIRES_SCALE)
        if raw is None:
            try:
                raw = page.screenshot(
                    type="png",
                    clip={"x": card["x"], "y": card["y"], "width": card["w"], "height": card["h"]},
                )
            except Exception:
                raw = None
        img = _decode_image(raw)
        if img is not None:
            sources.append(("hires", img, img.shape[1] / card["w"], img.shape[0] / card["h"], 0.0, 0.0))

        media = snap.get("media")
        img2 = _decode_image(_fetch_media_bytes(page, media))
        if img2 is not None and media:
            mr = media["rect"]
            sources.append((
                "source", img2,
                img2.shape[1] / mr["w"], img2.shape[0] / mr["h"],
                card["x"] - mr["x"], card["y"] - mr["y"],
            ))
    elif cap["screen_png"]:
        img = _decode_image(cap["screen_png"])
        if img is not None:
            sources.append(("screen", img, 1.0, 1.0, 0.0, 0.0))

    padx, pady = STICKER_PAD_X, STICKER_PAD_Y
    for k, st in enumerate(cap["stickers"]):
        for label, im, sx, sy, ox, oy in sources:
            H, W = im.shape[:2]
            x0 = max(0, int(round((st["x"] - padx + ox) * sx)))
            x1 = min(W, int(round((st["x"] + st["w"] + padx + ox) * sx)))
            y0 = max(0, int(round((st["y"] - pady + oy) * sy)))
            y1 = min(H, int(round((st["y"] + st["h"] + pady + oy) * sy)))
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            ok, buf = cv2.imencode(".png", im[y0:y1, x0:x1])
            if ok:
                cap["crops"].append({
                    "k": k, "src": label, "png": buf.tobytes(),
                    "size": [x1 - x0, y1 - y0],
                })

    if sources:
        ok, buf = cv2.imencode(".jpg", sources[0][1], [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            cap["card_jpg"] = buf.tobytes()

    return cap


# Lazily created EasyOCR reader, shared by all accounts.
_OCR_READER = None

_OCR_READ_KWARGS = dict(
    detail=0,
    paragraph=False,
    text_threshold=0.40,
    low_text=0.15,
    link_threshold=0.25,
    contrast_ths=0.1,
    adjust_contrast=0.70,
    mag_ratio=1.5,
    canvas_size=2560,
)

_OCR_ALLOWLIST = "#_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _get_ocr_reader():
    """Load the EasyOCR reader once per run."""
    global _OCR_READER
    if _OCR_READER is None:
        import easyocr
        log("info", "Loading OCR model (once per run)...", 2)
        try:
            _OCR_READER = easyocr.Reader(["en"], gpu=True, verbose=False)
        except Exception as e:
            log("fail", f"OCR model failed to load: {e}", 2)
            raise
    return _OCR_READER


def _ocr_crop_texts(reader, png_bytes):
    """OCR one sticker crop and return its text lines."""
    import cv2

    img = _decode_image(png_bytes)
    if img is None:
        return []

    h = img.shape[0]
    if h < 96:
        img = cv2.resize(img, None, fx=96.0 / h, fy=96.0 / h, interpolation=cv2.INTER_CUBIC)
    elif h > 240:
        img = cv2.resize(img, None, fx=160.0 / h, fy=160.0 / h, interpolation=cv2.INTER_AREA)
    img = cv2.copyMakeBorder(img, 12, 12, 12, 12, cv2.BORDER_REPLICATE)

    texts = []
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        texts += [str(t) for t in reader.recognize(gray, allowlist=_OCR_ALLOWLIST, detail=0)]
    except Exception:
        pass
    try:
        texts += [str(t) for t in reader.readtext(img, **_OCR_READ_KWARGS)]
    except Exception:
        pass
    return texts


def _run_ocr_fallback(image_bytes, unresolved_tags):
    """OCR the whole slide and return (matched watch tags, raw text)."""
    try:
        import cv2

        img = _decode_image(image_bytes)
        if img is None:
            return set(), ""

        if img.shape[1] < 1000:
            img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        reader = _get_ocr_reader()
        results = reader.readtext(img, **_OCR_READ_KWARGS)
        ocr_text = " ".join(results)
    except Exception as e:
        log("warn", f"OCR failed: {e}", 2)
        return set(), ""

    return _ocr_match_watch_tags(ocr_text, unresolved_tags), ocr_text


def _ocr_full_slide_blocks(image_bytes):
    """OCR the whole slide and return every text block."""
    try:
        import cv2

        img = _decode_image(image_bytes)
        if img is None:
            return []

        if img.shape[1] < 1000:
            img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        reader = _get_ocr_reader()
        return [str(t) for t in reader.readtext(img, **_OCR_READ_KWARGS)]
    except Exception as e:
        log("warn", f"OCR failed: {e}", 2)
        return []


def _ocr_sticker_block(cap, extra_lines=4):
    """OCR the text block around the sticker hitboxes at full capture resolution.

    Hashtags and mentions typed in a story's text box are clickable, but the lines above
    them (e.g. a greeting or keyword) are pixels only. They sit right above the hitboxes,
    so the area is enlarged upwards. Returns the OCR text blocks.
    """
    try:
        import cv2

        card = cap.get("card")
        stickers = cap.get("stickers") or []
        if not card or not stickers:
            return []

        img = _decode_image(cap.get("card_jpg"))
        if img is None:
            return []

        sx = img.shape[1] / card["w"]
        sy = img.shape[0] / card["h"]

        heights = sorted(st["h"] for st in stickers)
        line_h = max(heights[len(heights) // 2], 8)

        x0 = max(0.0, min(st["x"] for st in stickers) - 40)
        x1 = min(float(card["w"]), max(st["x"] + st["w"] for st in stickers) + 40)
        y0 = max(0.0, min(st["y"] for st in stickers) - line_h * extra_lines)
        y1 = min(float(card["h"]), max(st["y"] + st["h"] for st in stickers) + line_h)

        crop = img[int(y0 * sy):int(y1 * sy), int(x0 * sx):int(x1 * sx)]
        if crop.size == 0:
            return []

        reader = _get_ocr_reader()
        variants = [crop]

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        variants.append(cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray))

        texts = []
        for variant in variants:
            try:
                texts += [str(t) for t in reader.readtext(variant, **_OCR_READ_KWARGS)]
            except Exception:
                pass

        return texts

    except Exception:
        return []


def _slide_free_text_blocks(ocr_blocks, known_tokens):
    """Return the OCR blocks that are not one of the known hashtags or labels."""
    leftover = []

    for block in ocr_blocks:
        norm = _normalize_ocr_token(block).replace("_", "")

        if not norm:
            continue

        if any(_ocr_tag_match(t, block) for t in known_tokens if t):
            continue

        leftover.append(block.strip())

    return [b for b in leftover if b]


def slide_has_ajakan(slide, task=None):
    """True if the slide has free text beyond the known hashtags and sticker link (content is not validated)."""
    blocks = slide.get(
        "ajakan_blocks",
        []
    )

    if not blocks:
        return False

    known_tokens = set(GATEKEEPER_KEYWORDS)

    if task:
        known_tokens |= {
            _clean_tag(h)
            for h in task.get("hashtags", [])
            if _clean_tag(h)
        }

        sticker_link = task.get("sticker_link")
        if sticker_link:
            known_tokens.add(sticker_link)

    leftover = _slide_free_text_blocks(
        blocks,
        known_tokens
    )

    total_len = sum(
        len(_normalize_ocr_token(b))
        for b in leftover
    )

    return len(leftover) >= 2 or total_len >= 4


def _ocr_slide(cap, unresolved):
    """OCR sticker crops first, then the whole slide for tags still missing. Returns (found tags, text)."""
    found, texts = set(), []

    crops = cap.get("crops") or []
    if crops:
        reader = _get_ocr_reader()
        for crop in crops:
            if found >= unresolved:
                break
            t = _ocr_crop_texts(reader, crop["png"])
            texts.extend(t)
            found |= _ocr_match_watch_tags(" ".join(t), unresolved - found)

    remaining = unresolved - found
    if remaining:
        image = cap.get("card_jpg") or cap.get("screen_png") or b""
        if image:
            f2, raw = _run_ocr_fallback(image, remaining)
            found |= f2
            texts.append(raw)

    return found, " ".join(texts)


# ---------------------------------------------------------------------
# Slide model and requirement checks
# ---------------------------------------------------------------------

def build_slide(index, snap, api_tags=None, api_link_fragments=None, ocr_tags=None):
    """Build the slide record from a DOM snapshot: time, text, hashtags per source, stickers."""
    s_time, time_raw = _parse_snapshot_time(snap)
    raw_hrefs = snap.get("hrefs", [])

    tags, hidden = set(), set()
    for tag, hits in (snap.get("tag_hits") or {}).items():
        ct = _clean_tag(tag)
        for hit in hits:
            if _href_is_tag(hit.get("href", ""), ct):
                if hit.get("visible") or not REQUIRE_VISIBLE_TAG_ANCHOR:
                    tags.add(ct)
                else:
                    hidden.add(ct)
    hidden -= tags

    other = set()
    for a in (snap.get("tag_anchors") or []):
        if a.get("visible") or not REQUIRE_VISIBLE_TAG_ANCHOR:
            _collect_tags_from_url(a.get("href", ""), other)
    other -= tags

    return {
        "index": index,
        "url": snap.get("url", ""),
        "time": s_time,
        "time_raw": time_raw,
        "text": (snap.get("text") or "").lower(),
        "hrefs": [urllib.parse.unquote(h).lower() for h in raw_hrefs if h],
        "hashtags": tags,
        "hidden_tags": hidden,
        "other_tags": other,
        "api_tags": set(api_tags) if api_tags else set(),
        "api_link_fragments": set(api_link_fragments) if api_link_fragments else set(),
        "ocr_tags": set(ocr_tags) if ocr_tags else set(),
        "ocr_text": "",
        "ajakan_blocks": [],
        "stickers": list(snap.get("stickers") or []),
        "progress": snap.get("progress"),
        "unstable": False,
    }


def slide_gatekeeper_hits(slide, task=None):
    """Gatekeeper hashtags found on the slide (href, API, text or OCR)."""
    text = slide.get("text", "")

    hits = []

    for keyword in GATEKEEPER_KEYWORDS:
        ct = _clean_tag(keyword)

        matched = (
            ct in slide.get("hashtags", set())
            or ct in slide.get("api_tags", set())
            or (ALLOW_TEXT_FALLBACK and _tag_in_text(ct, text))
            or ct in slide.get("ocr_tags", set())
            # Feed only: once the caption hit Instagram's hashtag limit, the extra
            # hashtags are plain words without '#', so match them without it.
            or (
                ALLOW_TEXT_FALLBACK
                and slide.get("hashtag_limit_reached")
                and _tag_in_text_loose(ct, text)
            )
        )

        if matched:
            hits.append(ct)

    return sorted(set(hits))


def is_story_task_candidate(slide):
    """True if the slide carries every gatekeeper hashtag."""
    hits = slide_gatekeeper_hits(
        slide
    )

    required = {
        _clean_tag(keyword)
        for keyword in GATEKEEPER_KEYWORDS
    }

    return required.issubset(
        set(hits)
    )


# Feed posts use the same gatekeeper rule as story slides.
is_feed_task_candidate = is_story_task_candidate


def slide_tag_source(
    slide,
    tag,
    allow_bare_tag=False
):
    """Where a hashtag was found on the slide: href, api, text, text_bare, ocr, or None."""
    tag = _clean_tag(tag)

    if not tag:
        return None

    if tag in slide.get(
        "hashtags",
        set()
    ):
        return "href"

    if tag in slide.get(
        "api_tags",
        set()
    ):
        return "api"

    if ALLOW_TEXT_FALLBACK and _tag_in_text(
        tag,
        slide.get("text", "")
    ):
        return "text"

    if allow_bare_tag and ALLOW_TEXT_FALLBACK and _tag_in_text_loose(
        tag,
        slide.get("text", "")
    ):
        return "text_bare"

    if tag in slide.get(
        "ocr_tags",
        set()
    ):
        return "ocr"

    return None


def slide_link_source(
    slide,
    req_link
):
    """Where a sticker link was found on the slide: href, api, text, ocr, or None."""
    req = (req_link or "").strip().lower()

    if not req:
        return None

    if any(req in h for h in slide.get("hrefs", [])):
        return "href"

    if any(req in frag for frag in slide.get("api_link_fragments", set())):
        return "api"

    if ALLOW_TEXT_FALLBACK and req in slide.get("text", "").lower():
        return "text"

    if req in slide.get("ocr_text", "").lower() or req in slide.get("ocr_tags", set()):
        return "ocr"

    return None


def slide_has_sticker_link(
    slide,
    req_link
):
    """True if the required sticker link is present (or none is required)."""
    req = (req_link or "").strip().lower()

    if not req:
        return True

    return slide_link_source(slide, req_link) is not None


def normalize_keywords(raw):
    """Turn a task's `keyword` value into a clean list of lowercase strings.

    Accepts None, a single string, or a list/tuple/set of strings (as in daily_state.json).
    Empty items and "0" are dropped, duplicates are removed, order is kept.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        raw = [raw]

    keywords = []

    for item in raw:
        if item is None:
            continue

        text = str(item).strip().lower()

        if not text or text == "0" or text in keywords:
            continue

        keywords.append(text)

    return keywords


def _keyword_norm(text):
    """Keep only letters/digits (lowercase). Drops emoji, spaces, punctuation and '@'/'#'.

    OCR cannot read emoji and often changes spacing/punctuation, so keywords are compared
    in this reduced form. Common OCR look-alikes (0/o, 1/l, |/i) are folded together.
    """
    text = "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    return text.translate(str.maketrans({"0": "o", "1": "l"}))


KEYWORD_FUZZY_MIN_LEN = 6      # shorter keywords must match exactly
KEYWORD_FUZZY_MAX_LEN = 60     # longer keywords (captions) are matched exactly only
KEYWORD_FUZZY_RATIO = 0.80     # similarity needed for a fuzzy (OCR-tolerant) match (~1 wrong char in 6-10)


def _keyword_fuzzy_in(needle, haystack):
    """True if `needle` appears in `haystack`, allowing a few OCR character errors."""
    if not needle:
        return True

    if needle in haystack:
        return True

    if not (KEYWORD_FUZZY_MIN_LEN <= len(needle) <= KEYWORD_FUZZY_MAX_LEN):
        return False

    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None)
    matcher.set_seq2(needle)

    for size in (len(needle) - 1, len(needle), len(needle) + 1):
        if size < 1 or size > len(haystack):
            continue

        for i in range(len(haystack) - size + 1):
            matcher.set_seq1(haystack[i:i + size])

            if (
                matcher.real_quick_ratio() >= KEYWORD_FUZZY_RATIO
                and matcher.quick_ratio() >= KEYWORD_FUZZY_RATIO
                and matcher.ratio() >= KEYWORD_FUZZY_RATIO
            ):
                return True

    return False


def keyword_in_slide(slide, keyword):
    """True if a required keyword is on the slide/post (DOM text, OCR text or, for @user, a profile link)."""
    kw_norm = _keyword_norm(keyword)

    # Emoji-only keyword: nothing that can be verified, so it does not block the task.
    if not kw_norm:
        return True

    # A mention such as "@josw_jy" also counts when the slide links to that profile.
    kw = str(keyword).strip().lower()
    if kw.startswith("@") and len(kw) > 1:
        handle = kw[1:]
        if any(f"/{handle}" in h for h in slide.get("hrefs", [])):
            return True

        if _clean_tag(handle) in slide.get("api_tags", set()):
            return True

    for source in (slide.get("text", ""), slide.get("ocr_text", "")):
        if _keyword_fuzzy_in(kw_norm, _keyword_norm(source)):
            return True

    return False


def check_slide_requirements(
    slide,
    task,
    allow_bare_tag=False
):
    """Return what a slide is missing for a task: hashtags, keyword, sticker link, call to action."""
    missing = []


    for h in task.get(
        "hashtags",
        []
    ):
        tag = _clean_tag(h)

        if (
            tag
            and slide_tag_source(
                slide,
                tag,
                allow_bare_tag=allow_bare_tag
            ) is None
        ):
            missing.append(
                f"#{tag}"
            )


    # "keyword" may be a single string, a list of strings, or None.
    # Every keyword in the list must appear on the slide/post.
    for keyword in normalize_keywords(task.get("keyword")):
        if not keyword_in_slide(slide, keyword):
            missing.append(
                f"keyword '{keyword}'"
            )


    sticker_link = (
        task.get(
            "sticker_link"
        )
        or ""
    ).strip()

    if sticker_link:
        if not slide_has_sticker_link(
            slide,
            sticker_link
        ):
            missing.append(f"sticker link '{sticker_link.lower()}'")


    if task.get("ajakan_required"):

        if not slide_has_ajakan(
            slide,
            task
        ):
            missing.append("free-text call to action")

    return missing


def check_feed_post_requirements(
    post,
    task
):
    """Same as check_slide_requirements, plus bare hashtags and the account-tag rule."""
    missing = check_slide_requirements(
        post,
        task,
        allow_bare_tag=True
    )

    tag_required = task.get(
        "tag_required"
    )

    if tag_required is not None:

        actual_tagged = bool(
            post.get("tagged")
        )

        if actual_tagged != bool(tag_required):

            missing.append(
                f"account tag (required={bool(tag_required)}, actual={actual_tagged})"
            )

    return missing


# ---------------------------------------------------------------------
# Task evaluation
# ---------------------------------------------------------------------

def evaluate_story_task(slides, task, complete=True, used_slides=None):
    """Fallback evaluation of one Story task against every collected slide.

    Returns Correct when a task slide meets all requirements. Otherwise returns the
    status that explains why: incomplete read, unreadable time, wrong content, or nothing posted.
    """
    if used_slides is None:
        used_slides = set()

    start, end = story_window()
    task_id = task.get("task_id", "?")

    gatekeeper_slides = []
    unknown_time = []
    partial_slides = []

    for slide in slides:
        if slide["index"] in used_slides:
            continue

        if not is_story_task_candidate(slide):
            if (
                slide.get("hashtags")
                or slide.get("api_tags")
                or slide.get("ocr_tags")
                or slide_gatekeeper_hits(slide)
            ):
                partial_slides.append(slide["index"])
            continue

        if slide["time"] is None:
            unknown_time.append(slide)
            continue

        if start <= slide["time"] <= end:
            gatekeeper_slides.append(slide)

    for slide in gatekeeper_slides:
        missing = check_slide_requirements(slide, task)

        if not missing:
            used_slides.add(slide["index"])
            return STATUS_BENAR

        log("info", f"Task {task_id}: slide {slide['index']} is missing {', '.join(missing)}", 2)

    if not complete:
        log("warn", f"Task {task_id}: story was not fully read", 2)
        return STATUS_REVIEW

    if unknown_time:
        indexes = [s["index"] for s in unknown_time]
        log("warn", f"Task {task_id}: slides {indexes} have task hashtags but unreadable time", 2)
        return STATUS_REVIEW

    if gatekeeper_slides:
        return STATUS_SALAH

    if partial_slides:
        log("warn", f"Task {task_id}: slides {partial_slides} have only some of the task hashtags", 2)
        return STATUS_REVIEW

    return STATUS_TDK_POST


def evaluate_feed_task(
    gatekeeper_posts,
    task,
    unknown_time_posts=None,
    partial_posts=None,
    complete=True,
    used_posts=None
):
    """Fallback evaluation of one Feed task against the collected task posts.

    Same decision order as evaluate_story_task().
    """
    unknown_time_posts = unknown_time_posts or []
    partial_posts = partial_posts or []
    used_posts = set() if used_posts is None else used_posts
    task_id = task.get("task_id", "?")

    for post in gatekeeper_posts:
        if post.get("index") in used_posts:
            continue

        if not check_feed_post_requirements(post, task):
            used_posts.add(post.get("index"))
            return STATUS_BENAR

    if not complete:
        log("warn", f"Task {task_id}: feed was not fully read", 2)
        return STATUS_REVIEW

    if unknown_time_posts:
        indexes = [p.get("index") for p in unknown_time_posts]
        log("warn", f"Task {task_id}: posts {indexes} have task hashtags but unreadable time", 2)
        return STATUS_REVIEW

    if gatekeeper_posts:
        return STATUS_SALAH

    if partial_posts:
        indexes = [p.get("index") for p in partial_posts]
        log("warn", f"Task {task_id}: posts {indexes} have only some of the task hashtags", 2)
        return STATUS_REVIEW

    return STATUS_TDK_POST


# ---------------------------------------------------------------------
# Story viewer navigation
# ---------------------------------------------------------------------

def _rect_distance(px, py, rect):
    """Distance from a point to a rectangle (0 when inside)."""
    dx = max(rect["x"] - px, 0, px - (rect["x"] + rect["w"]))
    dy = max(rect["y"] - py, 0, py - (rect["y"] + rect["h"]))
    return math.hypot(dx, dy)


def _safe_hold_point(snap):
    """Pick a point on the story card away from stickers, so holding the mouse cannot trigger one."""
    card = (snap or {}).get("card")
    if not card:
        return None

    absolute = [
        {"x": card["x"] + s["x"], "y": card["y"] + s["y"], "w": s["w"], "h": s["h"]}
        for s in (snap.get("stickers") or [])
    ]

    best, best_d = None, -1.0
    for fx, fy in (
        (0.50, 0.45), (0.50, 0.35), (0.50, 0.55), (0.25, 0.45), (0.75, 0.45),
        (0.25, 0.35), (0.75, 0.35), (0.25, 0.55), (0.75, 0.55), (0.50, 0.30),
    ):
        px, py = card["x"] + card["w"] * fx, card["y"] + card["h"] * fy
        d = min([_rect_distance(px, py, r) for r in absolute] or [1e9])
        if d >= 30:
            return (px, py)
        if d > best_d:
            best, best_d = (px, py), d
    return best


def _hold_pause(page, snap=None):
    """Press and hold on the story to pause autoplay. Returns True if the hold started."""
    try:
        pt = _safe_hold_point(snap)
        if pt is None:
            vp = page.viewport_size or {"width": 1280, "height": 720}
            pt = (vp["width"] // 2, vp["height"] // 2)
        page.mouse.move(pt[0], pt[1])
        page.mouse.down()
        return True
    except Exception:
        return False


def _release_pause(page):
    """Release the mouse hold."""
    try:
        page.mouse.up()
    except Exception:
        pass


def _click_nav(
    page,
    direction
):
    """Click the visible Next/Previous button. Returns True if a button was clicked."""
    labels = (
        _NEXT_LABELS
        if direction == "next"
        else _PREV_LABELS
    )

    for label in labels:
        try:
            loc = page.locator(
                f"[aria-label='{label}']"
            ).first

            if (
                loc.count() > 0
                and loc.is_visible()
            ):
                loc.click(
                    timeout=2000
                )

                return True

        except Exception:
            continue

    return False


def _advance(
    page,
    prev_sig,
    timeout=4.0
):
    """Go to the next slide. Returns 'advanced', 'stuck' (button did not work) or 'end' (no button)."""
    url0 = page.url

    clicked = _click_nav(
        page,
        "next"
    )

    if not clicked:
        try:
            page.keyboard.press(
                "ArrowRight"
            )
        except Exception:
            pass

        timeout = min(
            timeout,
            3.0
        )

    deadline = time.time() + timeout

    while time.time() < deadline:

        sig = _quick_signature(page)

        if sig is not None and sig != prev_sig:
            return "advanced"

        if _story_owner_from_url(page.url) != _story_owner_from_url(url0):
            return "advanced"

        time.sleep(0.2)

    return (
        "stuck"
        if clicked
        else "end"
    )


def _rewind_to_first_slide(
    page,
    username
):
    """Click Previous until the first slide of the user's story is shown."""
    uname = username.lower()

    time.sleep(1.0)

    for _ in range(MAX_SLIDES):

        before_url = page.url

        if not _click_nav(
            page,
            "prev"
        ):
            return

        deadline = time.time() + 2.5

        while time.time() < deadline:

            time.sleep(0.2)

            current_url = page.url

            if current_url != before_url:
                break

        else:
            return

        if (
            _story_owner_from_url(
                current_url
            ) != uname
        ):
            return


def _open_story_viewer(
    page,
    username
):
    """Open the story viewer from the profile avatar. Returns True on success."""
    selectors = [
        "header div[role='button']:has(canvas)",
        "header a:has(canvas)",
        "header canvas",
    ]

    for selector in selectors:

        try:
            loc = page.locator(
                selector
            ).first

            if loc.count() == 0:
                continue

            loc.click(
                timeout=3000,
                force=(
                    selector
                    == "header canvas"
                )
            )

        except Exception:
            continue

        deadline = time.time() + 6.0

        while time.time() < deadline:

            if "/stories/" in page.url:
                return True

            time.sleep(0.25)

    return False


def _story_owner_from_url(url):
    """Username in a /stories/<user>/ URL, lowercase, or None."""
    match = re.search(
        r"/stories/([^/?#]+)",
        url or ""
    )

    return (
        match.group(1).lower()
        if match
        else None
    )


def _ensure_first_slide(page, watch_tags, uname):
    """Use the progress bar to make sure the viewer is on the first slide."""
    for _ in range(MAX_SLIDES):
        snap = _take_snapshot(page, watch_tags)
        prog = (snap or {}).get("progress")

        if not prog or prog.get("current", 1) <= 1:
            return

        before = _quick_signature(page)

        if not _click_nav(page, "prev"):
            return

        deadline = time.time() + 2.5

        while time.time() < deadline:
            time.sleep(0.2)
            if _quick_signature(page) != before:
                break
        else:
            return

        if _story_owner_from_url(page.url) != uname:
            return


def collect_story_slides(
    page,
    username,
    watch_tags,
    max_slides=MAX_SLIDES,
    api_capture=None,
    sticker_link_quota=None,
    ajakan_check=False,
    story_tasks=None,
    used_slides=None
):
    """Read every story slide of `username` in two phases.

    Phase 1 (fast): pause each slide, snapshot the DOM, capture the image, move on.
    Phase 2 (slow): OCR and match the slides. It runs after the viewer is finished,
    because slow work between two slides makes the story auto-advance and skip slides.

    Phase 2 shortcuts, to save OCR time:
      - slides outside the task window are never OCR'd;
      - sticker links are only searched on task slides, and stop once each required
        label has been found as many times as tasks need it (`sticker_link_quota`);
      - reading stops as soon as every Story task is matched.

    Returns (slides, complete, task_results). `task_results` follows `story_tasks`;
    None means the task still needs evaluate_story_task().
    """
    uname = username.lower()

    slides = []
    seen = set()
    complete = True
    progress_total = 0

    sticker_link_quota = dict(sticker_link_quota or {})
    sticker_link_found = {tag: 0 for tag in sticker_link_quota}
    hashtag_watch_tags = [t for t in watch_tags if t not in sticker_link_quota]

    story_tasks = list(story_tasks or [])
    used_slides = set() if used_slides is None else used_slides
    task_results = [None] * len(story_tasks)
    win_start, win_end = story_window()

    _rewind_to_first_slide(page, username)
    _ensure_first_slide(page, watch_tags, uname)

    # ------------------------------ Phase 1 ------------------------------
    log("info", "Story: reading slides...", 1)
    t_phase1 = time.time()

    for index in range(1, max_slides + 1):

        if _story_owner_from_url(page.url) != uname:
            complete = bool(slides)
            break

        t_read = time.time()
        paused = False

        try:
            # Freeze the slide before doing any work on it.
            geom = _take_snapshot(page, watch_tags)
            paused = _hold_pause(page, geom)

            snap = _settled_snapshot(page, watch_tags)

            if snap is None:
                log("warn", f"Slide {index} was not ready, stopped reading", 2)
                complete = False
                break

            sig = _sig_from_snapshot(snap)

            if sig in seen:
                break
            seen.add(sig)

            slide = build_slide(
                index,
                snap,
                api_tags=(api_capture.tags() if api_capture else None),
                api_link_fragments=(api_capture.link_fragments() if api_capture else None),
            )

            slide["_cap"] = _grab_slide_media(page, snap)

            # Did the story auto-advance while the image was being captured?
            sig_after = _quick_signature(page)
            if sig_after is not None and sig_after != sig:
                slide["unstable"] = True
                complete = False

            slide["t_read"] = secs(t_read)
            slides.append(slide)

            prog = snap.get("progress")
            if prog:
                progress_total = max(progress_total, prog.get("total", 0))

        finally:
            if paused:
                _release_pause(page)

        # Autoplay already moved on: clicking Next now would skip a slide.
        now_sig = _quick_signature(page)
        if now_sig is not None and now_sig != sig:
            if _story_owner_from_url(page.url) != uname:
                break
            continue

        state = _advance(page, sig)

        if state == "end":
            break

        if state == "stuck":
            log("warn", f"Slide {index}: Next button did not move the story", 2)
            complete = False
            break

        if _story_owner_from_url(page.url) != uname:
            break

        time.sleep(0.4)

    else:
        log("warn", f"Reached the {max_slides}-slide limit", 2)
        complete = False

    if progress_total and len(slides) < progress_total:
        log("warn", f"Progress bar shows {progress_total} slides, only {len(slides)} were read", 2)
        complete = False

    log("info", f"Story: {len(slides)} slides read in {secs(t_phase1):.1f}s", 1)

    # ------------------------------ Phase 2 ------------------------------
    for position, slide in enumerate(slides):

        if story_tasks and all(r is not None for r in task_results):
            rest = slides[position:]
            log("skip", f"Slides {rest[0]['index']}-{rest[-1]['index']} not checked, all Story tasks matched", 2)
            break

        t_ocr = time.time()
        cap = slide.pop("_cap", None) or {}

        time_ok = (
            slide.get("time") is not None
            and win_start <= slide["time"] <= win_end
        )

        # Hashtags: an outside-window slide can never be a task slide, so skip OCR.
        unresolved = set()

        if USE_OCR_FALLBACK and (time_ok or not story_tasks):
            unresolved = {
                t for t in hashtag_watch_tags
                if slide_tag_source(slide, t) is None
            }

        if unresolved and cap:
            ocr_matched, ocr_raw_text = _ocr_slide(cap, unresolved)
            slide["ocr_tags"] = set(slide["ocr_tags"]) | ocr_matched
            slide["ocr_text"] = ocr_raw_text.lower()

        # Sticker links: only searched on task slides (window + gatekeeper hashtags).
        sticker_unresolved = set()

        if USE_OCR_FALLBACK and sticker_link_quota:
            is_candidate = is_story_task_candidate(slide)
            hitbox_ok = len(slide.get("stickers") or []) >= MIN_STICKER_HITBOX_FOR_LINK_OCR

            if time_ok and is_candidate:
                for tag, required in sticker_link_quota.items():
                    if sticker_link_found.get(tag, 0) >= required:
                        continue
                    if slide_link_source(slide, tag) is not None:
                        continue
                    if hitbox_ok:
                        sticker_unresolved.add(tag)

        if sticker_unresolved and cap:
            ocr_matched2, ocr_raw_text2 = _ocr_slide(cap, sticker_unresolved)
            slide["ocr_tags"] = set(slide["ocr_tags"]) | ocr_matched2
            slide["ocr_text"] = (
                (slide["ocr_text"] + " " + ocr_raw_text2)
                if slide["ocr_text"]
                else ocr_raw_text2
            ).lower()

        for tag, required in sticker_link_quota.items():
            if sticker_link_found.get(tag, 0) >= required:
                continue
            if slide_link_source(slide, tag) is not None:
                sticker_link_found[tag] = sticker_link_found.get(tag, 0) + 1

        # Keywords: text stickers are drawn on the image and are not in the DOM,
        # so OCR the whole slide when a keyword of an open task is still missing.
        full_blocks = None

        if USE_OCR_FALLBACK and story_tasks and time_ok and cap:
            open_keywords = {
                k
                for i, t in enumerate(story_tasks)
                if task_results[i] is None
                for k in normalize_keywords(t.get("keyword"))
            }

            if (
                open_keywords
                and is_story_task_candidate(slide)
                and any(not keyword_in_slide(slide, k) for k in open_keywords)
            ):
                def _add_ocr_text(parts):
                    extra = " ".join(parts)
                    slide["ocr_text"] = (
                        (slide["ocr_text"] + " " + extra) if slide["ocr_text"] else extra
                    ).lower()

                image = cap.get("card_jpg") or cap.get("screen_png") or b""
                if image:
                    full_blocks = _ocr_full_slide_blocks(image)
                    _add_ocr_text(full_blocks)

                if any(not keyword_in_slide(slide, k) for k in open_keywords):
                    _add_ocr_text(_ocr_sticker_block(cap))

        # Free-text call to action: OCR the whole slide, task slides only.
        if USE_OCR_FALLBACK and ajakan_check:
            if time_ok and is_story_task_candidate(slide) and cap:
                image = cap.get("card_jpg") or cap.get("screen_png") or b""
                if image:
                    slide["ajakan_blocks"] = (
                        full_blocks
                        if full_blocks is not None
                        else _ocr_full_slide_blocks(image)
                    )

        # Match the slide to the first Story task that is still open.
        matched_task = None

        if story_tasks and any(r is None for r in task_results):
            eligible = (
                slide["index"] not in used_slides
                and is_story_task_candidate(slide)
                and time_ok
            )

            if eligible:
                for i, task in enumerate(story_tasks):
                    if task_results[i] is not None:
                        continue

                    if not check_slide_requirements(slide, task):
                        used_slides.add(slide["index"])
                        task_results[i] = STATUS_BENAR
                        matched_task = task
                        break

        # One summary line per slide.
        t_read = slide.get("t_read", 0.0)
        t_ocr = secs(t_ocr)
        posted = slide["time"].strftime("%H:%M") if slide.get("time") else "--:--"

        if not time_ok:
            kind, verdict = "skip", "outside window" if slide.get("time") else "posting time unreadable"
        elif not is_story_task_candidate(slide):
            kind, verdict = "skip", "not a task slide"
        elif matched_task:
            kind, verdict = "ok", f"task slide | matches task {matched_task.get('task_id', '?')}"
        else:
            kind, verdict = "info", "task slide | no task matched"

        if slide.get("unstable"):
            verdict += " | unstable (autoplay moved during capture)"

        log_item(
            kind,
            f"Slide {slide['index']}",
            t_read + t_ocr,
            f"read {t_read:.1f}s + ocr {t_ocr:.1f}s | posted {posted} | {verdict}",
        )

    return slides, complete, task_results


# ---------------------------------------------------------------------
# Profile and feed reading
# ---------------------------------------------------------------------

def _page_visible_text(page):
    """Visible page text, used only to detect private/unavailable pages."""
    try:
        return page.locator(
            "body"
        ).inner_text()

    except Exception:
        return ""


def _load_profile(
    page,
    username
):
    """Open a profile. Returns {status, note, has_story, post_count}; `status` is set when the account cannot be checked."""
    page.goto(
        f"https://www.instagram.com/{username}/",
        wait_until="domcontentloaded",
        timeout=25000
    )

    current_url = page.url.lower()

    if (
        "/accounts/login"
        in current_url
        or "/challenge"
        in current_url
    ):
        raise InstagramSessionError(
            f"Instagram redirected to login/checkpoint ({page.url})."
        )

    try:
        page.wait_for_selector(
            "header, main",
            timeout=12000
        )

    except Exception:
        return {
            "status": STATUS_REVIEW,
            "note": "profile page did not finish loading",
        }

    time.sleep(1.0)

    visible = _page_visible_text(
        page
    ).lower()

    if any(
        marker in visible
        for marker in _UNAVAILABLE_MARKERS
    ):
        return {
            "status": STATUS_REVIEW,
            "note": "page unavailable (wrong username or deactivated account?)",
        }

    if any(
        marker in visible
        for marker in _PRIVATE_MARKERS
    ):
        return {
            "status": STATUS_REVIEW,
            "note": "private account",
        }

    has_story = False

    try:
        page.wait_for_function(
            """() => {
                const canvases = Array.from(
                    document.querySelectorAll('header canvas')
                );
                return canvases.some(
                    c => !c.closest('a[href*="/stories/highlights/"]')
                );
            }""",
            timeout=3000
        )

        has_story = True

    except Exception:
        pass

    post_count = len(
        page.query_selector_all(
            "main a[href*='/p/'], "
            "main a[href*='/reel/']"
        )
    )

    return {
        "status": None,
        "note": "",
        "has_story": has_story,
        "post_count": post_count,
    }


_TAG_PEOPLE_ARIA_HINTS = (
    "tag",
    "tags",
    "tagged",
    "ditandai",
    "tag people",
    "orang yang ditandai",
    "in this photo",
    "in this video",
    "foto ini",
    "video ini",
)

_TAG_PEOPLE_ARIA_EXCLUDE = (
    "hashtag",
)


def _post_has_tagged_people(page, page_html=None):
    """True if the open post has tagged people."""
    if page_html:
        try:
            m = re.search(
                r'"usertags"\s*:\s*\{\s*"in"\s*:\s*(\[.*?\])',
                page_html,
                flags=re.DOTALL,
            )

            if m and m.group(1).strip() not in ("[]", ""):
                return True

        except Exception:
            pass

    try:
        labels = page.eval_on_selector_all(
            "article [aria-label]",
            "els => els.map("
            "e => (e.getAttribute('aria-label') || '').toLowerCase()"
            ")"
        )

    except Exception:
        return False

    for label in labels:

        if any(exclude in label for exclude in _TAG_PEOPLE_ARIA_EXCLUDE):
            continue

        if any(hint in label for hint in _TAG_PEOPLE_ARIA_HINTS):
            return True

    return False


def _og_description(page_html):
    """Caption from the og:description meta tag, or None."""
    for tag in re.findall(r"<meta[^>]+>", page_html or ""):

        if (
            'property="og:description"' in tag
            or "property='og:description'" in tag
        ):
            m = (
                re.search(r'content="([^"]*)"', tag)
                or re.search(r"content='([^']*)'", tag)
            )

            if m:
                return html_lib.unescape(m.group(1))

            break

    return None


def _extract_feed_post_data(page):
    """Read caption text, hashtags, posting time and tagged flag from the open post. None if unreadable."""
    current_url = page.url.lower()

    if (
        "/accounts/login" in current_url
        or "/challenge" in current_url
    ):
        raise InstagramSessionError(
            f"Instagram redirected to login/checkpoint while opening a post ({page.url})."
        )

    def _settle():
        try:
            page.wait_for_load_state(
                "networkidle",
                timeout=6000
            )
        except Exception:
            pass
        time.sleep(0.6)

    def _try_extract():

        try:
            page_html = page.content()
        except Exception:
            page_html = ""

        og_caption = _og_description(page_html)

        article_text = ""
        try:
            article_loc = page.locator("article")
            if article_loc.count() > 0:
                article_text = article_loc.first.inner_text()
        except Exception:
            article_text = ""

        return page_html, og_caption, article_text

    _settle()
    page_html, og_caption, article_text = _try_extract()

    if not og_caption and not article_text:

        try:
            page.reload(
                wait_until="domcontentloaded",
                timeout=15000
            )
        except Exception:
            pass

        _settle()

        current_url = page.url.lower()

        if (
            "/accounts/login" in current_url
            or "/challenge" in current_url
        ):
            raise InstagramSessionError(
                f"Instagram redirected to login/checkpoint while opening a post ({page.url})."
            )

        page_html, og_caption, article_text = _try_extract()

        if not og_caption and not article_text:
            return None

    caption = og_caption or article_text or ""

    full_text = caption.lower()

    hashtag_text = f"{article_text}\n{og_caption or ''}"

    hashtags = {
        _clean_tag(m).rstrip("\"'.,;:!?)")
        for m in re.findall(r"#([^\s#]+)", hashtag_text)
    }

    try:
        hrefs = [
            a.get_attribute("href") or ""
            for a in page.query_selector_all(
                "article a[href*='/explore/tags/']"
            )
        ]
        hashtags |= extract_hashtags_from_hrefs(hrefs)
    except Exception:
        pass

    post_time = None

    try:
        time_el = page.query_selector("article time")

        if time_el:
            raw_dt = time_el.get_attribute("datetime")

            if raw_dt:
                post_time = (
                    datetime.fromisoformat(
                        raw_dt.replace("Z", "+00:00")
                    ).astimezone(WIB)
                )

    except Exception:
        post_time = None

    if post_time is None and page_html:

        m = re.search(
            r'<time\b[^>]*\bdatetime="([^"]+)"',
            page_html,
            flags=re.IGNORECASE
        )

        if m:

            try:
                post_time = (
                    datetime.fromisoformat(
                        html_lib.unescape(m.group(1)).replace(
                            "Z", "+00:00"
                        )
                    ).astimezone(WIB)
                )

            except ValueError:
                post_time = None

    try:
        tagged = _post_has_tagged_people(page, page_html=page_html)
    except Exception:
        tagged = False

    # Instagram links at most FEED_MAX_LINKED_HASHTAGS hashtags per caption; the rest turn
    # into plain text. When the limit is reached, later hashtags may be missing their '#'.
    hashtag_limit_reached = len({h for h in hashtags if h}) >= FEED_MAX_LINKED_HASHTAGS

    return {
        "text": full_text,
        "hashtags": hashtags,
        "hashtag_limit_reached": hashtag_limit_reached,
        "time": post_time,
        "tagged": tagged,
    }


def _is_pinned_grid_anchor(anchor):
    """True if a grid tile is pinned (pinned posts are old, so never task posts)."""
    try:
        return bool(
            anchor.evaluate(
                """
                (el) => {
                    let node = el;
                    for (let i = 0; i < 4 && node; i++) {
                        const tileLinks = node.querySelectorAll(
                            "a[href*='/p/'], a[href*='/reel/']"
                        );
                        if (tileLinks.length > 1) {
                            return false;
                        }
                        const hit = node.querySelector(
                            '[aria-label*="pin" i]'
                        );
                        if (hit) return true;
                        node = node.parentElement;
                    }
                    return false;
                }
                """
            )
        )
    except Exception:
        return False


def _collect_feed_post_urls(
    page,
    max_posts=MAX_FEED_POSTS_SCAN
):
    """Collect up to `max_posts` newest post URLs from the profile grid, skipping pinned ones."""
    urls = []
    seen = set()

    anchors = page.query_selector_all(
        "main a[href*='/p/'], "
        "main a[href*='/reel/']"
    )

    for a in anchors:

        href = a.get_attribute("href") or ""

        if not href:
            continue

        full = (
            href
            if href.startswith("http")
            else f"https://www.instagram.com{href}"
        )

        full = full.split("?")[0]

        if full in seen:
            continue

        if _is_pinned_grid_anchor(a):
            seen.add(full)
            continue

        seen.add(full)
        urls.append(full)

        if len(urls) >= max_posts:
            break

    return urls


# ---------------------------------------------------------------------
# Feed scanning
# ---------------------------------------------------------------------

def collect_and_evaluate_feed_tasks(page, username, feed_tasks, max_posts=MAX_FEED_POSTS_SCAN):
    """Open the newest feed posts one by one and match them against the Feed tasks.

    Cheap checks run first (posting time, then hashtags). Scanning stops early when
    every task is matched, or when FEED_STOP_AFTER_OLD_POSTS posts in a row were
    published before the window opens (the grid is newest-first, so the rest are older too).
    Posts published after the window closes do not trigger the stop, because older
    posts behind them can still be inside the window.
    Unresolved tasks fall back to evaluate_feed_task().
    Returns one status per task, in task order.
    """
    if not feed_tasks:
        return []

    start, end = story_window()
    urls = _collect_feed_post_urls(page, max_posts=max_posts)

    log("info", f"Feed: scanning up to {len(urls)} recent posts", 1)

    gatekeeper_posts = []
    unknown_time_posts = []
    partial_posts = []
    complete = True

    used_posts = set()
    results = [None] * len(feed_tasks)
    old_streak = 0

    for idx, url in enumerate(urls, start=1):

        if all(r is not None for r in results):
            log("skip", f"Posts {idx}-{len(urls)} not opened, all Feed tasks matched", 2)
            break

        if old_streak >= FEED_STOP_AFTER_OLD_POSTS:
            rest = f"{idx}-{len(urls)}" if idx < len(urls) else f"{idx}"
            log("skip", f"Post {rest} not opened, {old_streak} posts in a row are older than the window", 2)
            break

        t_post = time.time()
        label = f"Post {idx}"

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            log_item("warn", label, secs(t_post), f"failed to open: {e}")
            complete = False
            continue

        post = _extract_feed_post_data(page)

        if post is None:
            log_item("warn", label, secs(t_post), "could not be read")
            complete = False
            continue

        post["index"] = idx
        post["url"] = url

        if post["time"] is not None and not (start <= post["time"] <= end):
            too_old = post["time"] < start
            old_streak = old_streak + 1 if too_old else 0
            reason = "before window" if too_old else "after window"
            log_item("skip", label, secs(t_post), f"posted {post['time']:%H:%M} | {reason}")
            continue

        old_streak = 0

        if not is_feed_task_candidate(post):
            log_item("skip", label, secs(t_post), "not a task post")

            if slide_gatekeeper_hits(post):
                partial_posts.append(post)

            continue

        if post["time"] is None:
            log_item("warn", label, secs(t_post), "task post, but posting time is unreadable")
            unknown_time_posts.append(post)
            continue

        gatekeeper_posts.append(post)

        posted = f"posted {post['time']:%H:%M} | task post"

        if post.get("hashtag_limit_reached"):
            posted += f" | {FEED_MAX_LINKED_HASHTAGS}+ hashtags, bare tags allowed"
        missing_notes = []
        matched_task = None

        for i, task in enumerate(feed_tasks):
            if results[i] is not None:
                continue

            missing = check_feed_post_requirements(post, task)

            if not missing:
                used_posts.add(post["index"])
                results[i] = STATUS_BENAR
                matched_task = task
                break

            missing_notes.append(f"task {task.get('task_id', '?')} missing {', '.join(missing)}")

        if matched_task:
            log_item("ok", label, secs(t_post), f"{posted} | matches task {matched_task.get('task_id', '?')}")
        else:
            log_item("info", label, secs(t_post), f"{posted} | {'; '.join(missing_notes)}")

    # Tasks not matched inline are decided from everything collected above.
    for i, task in enumerate(feed_tasks):
        if results[i] is not None:
            continue

        results[i] = evaluate_feed_task(
            gatekeeper_posts,
            task,
            unknown_time_posts=unknown_time_posts,
            partial_posts=partial_posts,
            complete=complete,
            used_posts=used_posts,
        )

    return results


# Order used to list flagged statuses, most urgent first.
_SEVERITY_ORDER = [
    STATUS_REVIEW,
    STATUS_TDK_POST,
    STATUS_SALAH,
    STATUS_BENAR,
]


# ---------------------------------------------------------------------
# Per-account check
# ---------------------------------------------------------------------

def _task_type(task):
    """Task type in lowercase ('story' or 'feed')."""
    return str(
        task.get(
            "tipe",
            "Story"
        )
    ).strip().lower()


def _split_tasks(tasks_criteria):
    """Split tasks into (story_tasks, feed_tasks, other_tasks)."""
    story_tasks = [t for t in tasks_criteria if _task_type(t) == "story"]
    feed_tasks = [t for t in tasks_criteria if _task_type(t) == "feed"]
    other_tasks = [t for t in tasks_criteria if _task_type(t) not in ("story", "feed")]
    return story_tasks, feed_tasks, other_tasks


def _check_story_tasks(page, clean_username, story_tasks, profile):
    """Read the account's story and return one status per Story task."""
    results = []
    slides = []
    complete = True
    story_error = False
    story_task_results = [None] * len(story_tasks)

    # Shared by all Story tasks: a slide that satisfied one task is not reused for the next.
    used_story_slides = set()

    # Must be attached before the viewer opens: the story data is fetched at that moment.
    api_capture = StoryApiCapture(page) if USE_STORY_API_CAPTURE else None
    if api_capture:
        api_capture.attach()

    if not profile["has_story"]:
        log("info", "No active story", 1)

    elif not _open_story_viewer(page, clean_username):
        log("warn", "Could not open the story viewer", 1)
        story_error = True

    else:
        # Tags searched on every slide: gatekeepers, task hashtags and sticker-link labels.
        watch_tags = sorted(
            {_clean_tag(tag) for tag in GATEKEEPER_KEYWORDS}
            | {
                _clean_tag(h)
                for task in story_tasks
                for h in task.get("hashtags", [])
                if _clean_tag(h)
            }
            | {
                _clean_tag(task.get("sticker_link"))
                for task in story_tasks
                if task.get("sticker_link") and _clean_tag(task.get("sticker_link"))
            }
        )

        # How many times each sticker-link label must be found (one per task requiring it).
        sticker_link_quota = {}
        for task in story_tasks:
            clean_link = _clean_tag(task.get("sticker_link")) if task.get("sticker_link") else ""
            if clean_link:
                sticker_link_quota[clean_link] = sticker_link_quota.get(clean_link, 0) + 1

        ajakan_check = any(task.get("ajakan_required") for task in story_tasks)

        slides, complete, story_task_results = collect_story_slides(
            page,
            clean_username,
            watch_tags,
            api_capture=api_capture,
            sticker_link_quota=sticker_link_quota,
            ajakan_check=ajakan_check,
            story_tasks=story_tasks,
            used_slides=used_story_slides
        )

    if api_capture:
        api_capture.detach()

    for i, task in enumerate(story_tasks):
        if story_error:
            result = STATUS_REVIEW
        elif story_task_results[i] is not None:
            result = story_task_results[i]
        else:
            result = evaluate_story_task(
                slides,
                task,
                complete=complete,
                used_slides=used_story_slides
            )

        log("ok" if result == STATUS_BENAR else "fail",
            f"Task {task.get('task_id', '?')} (Story): {STATUS_LABEL[result]}", 1)
        results.append(result)

    return results


def _check_feed_tasks(page, clean_username, feed_tasks, profile):
    """Read the account's recent posts and return one status per Feed task."""
    results = []

    if profile["post_count"] == 0:
        log("info", "No feed posts", 1)
        return [STATUS_TDK_POST] * len(feed_tasks)

    feed_scan_limit = min(
        MAX_FEED_POSTS_SCAN,
        max(FEED_POSTS_PER_TASK_SCAN, len(feed_tasks) * FEED_POSTS_PER_TASK_SCAN)
    )

    feed_results = collect_and_evaluate_feed_tasks(
        page,
        clean_username,
        feed_tasks,
        max_posts=feed_scan_limit
    )

    for task, result in zip(feed_tasks, feed_results):
        log("ok" if result == STATUS_BENAR else "fail",
            f"Task {task.get('task_id', '?')} (Feed): {STATUS_LABEL[result]}", 1)
        results.append(result)

    return results


def check_instagram_post(page, target_username, tasks_criteria=None):
    """Check one account against today's tasks, Story first and then Feed, in one tab.

    Returns one status per task (not one per account), so fines are only counted
    for the tasks that actually failed.
    """
    clean_username = target_username.strip().replace("@", "")

    if not tasks_criteria:
        log("warn", "No task criteria configured", 1)
        return [STATUS_REVIEW]

    try:
        profile = _load_profile(page, clean_username)

        if profile["status"]:
            log("warn", f"{profile['note']} -> {STATUS_LABEL[profile['status']]}", 1)
            return [profile["status"]] * len(tasks_criteria)

        story_tasks, feed_tasks, other_tasks = _split_tasks(tasks_criteria)

        if other_tasks:
            log("warn", f"Unknown task type: {[t.get('tipe') for t in other_tasks]}", 1)
            return [STATUS_REVIEW] * len(tasks_criteria)

        results = []

        if story_tasks:
            results.extend(_check_story_tasks(page, clean_username, story_tasks, profile))

            # The story viewer left the profile page, so reload it before reading the feed.
            if feed_tasks:
                profile = _load_profile(page, clean_username)

                if profile["status"]:
                    return results + [profile["status"]] * len(feed_tasks)

        if feed_tasks:
            results.extend(_check_feed_tasks(page, clean_username, feed_tasks, profile))

        return results

    except InstagramSessionError:
        raise

    except Exception as e:
        log("fail", f"Error while checking the account: {e} -> {STATUS_LABEL[STATUS_REVIEW]}", 1)
        return [STATUS_REVIEW] * len(tasks_criteria)


def check_tasks_of_one_type(page, target_username, tasks_criteria, kind):
    """Check only the Story or only the Feed tasks of one account (used by the parallel tabs).

    `kind` is 'story' or 'feed'. Returns one status per task of that type.
    """
    clean_username = target_username.strip().replace("@", "")
    tasks = [t for t in tasks_criteria if _task_type(t) == kind]

    if not tasks:
        return []

    try:
        profile = _load_profile(page, clean_username)

        if profile["status"]:
            log("warn", f"{profile['note']} -> {STATUS_LABEL[profile['status']]}", 1)
            return [profile["status"]] * len(tasks)

        if kind == "story":
            return _check_story_tasks(page, clean_username, tasks, profile)

        return _check_feed_tasks(page, clean_username, tasks, profile)

    except InstagramSessionError:
        raise

    except Exception as e:
        log("fail", f"Error while checking the account: {e} -> {STATUS_LABEL[STATUS_REVIEW]}", 1)
        return [STATUS_REVIEW] * len(tasks)


# ---------------------------------------------------------------------
# Phase 1: daily setup
# ---------------------------------------------------------------------

def _read_multiline_input(end_marker="END"):
    """Read multi-line input (e.g. a caption with blank lines) until a line equals `end_marker`.

    Returns None when the text is empty or "0".
    """
    lines = []

    while True:
        try:
            line = input()
        except EOFError:
            break

        if line.strip().upper() == end_marker:
            break

        lines.append(line)

    text = "\n".join(lines).strip()

    if not text or text == "0":
        return None

    return text


def phase1_dynamic_setup():
    """Ask the operator for today's task requirements and save them to the daily state file."""
    jenis_penugasan = "Paid Promod"

    section("DAILY SETUP")

    try:
        total_tugas = int(input("Total tasks today (ENTER = 1): ").strip() or "1")
    except ValueError:
        total_tugas = 1

    tasks_criteria = []

    for i in range(1, total_tugas + 1):
        print(f"\n--- Task {i} of {total_tugas} ---")

        raw_type = input("Type: 1 = Story, 2 = Feed (ENTER = 1): ").strip() or "1"
        tipe_tugas = "Feed" if raw_type == "2" else "Story"

        print("Required hashtags, one per line (empty line to finish, 0 = none):")
        lines = []
        while True:
            line = input().strip()
            if not line:
                break
            lines.append(line)
        hashtags = [] if lines == ["0"] else lines

        # Sticker links only exist on Stories.
        sticker_link = None
        if tipe_tugas == "Story":
            raw_link = input("Sticker link label (0 = none): ").strip()
            sticker_link = None if raw_link in ("", "0") else raw_link

        label = "caption" if tipe_tugas == "Feed" else "text/keyword"
        print(
            f"Required {label}: paste multi-line text, then type END on its own line "
            "(0 then END = none):"
        )
        keyword = _read_multiline_input()

        # Feed tasks check account tags; Story tasks check a free-text call to action.
        tag_required = None
        ajakan_required = False

        if tipe_tugas == "Feed":
            raw_tag = input(
                "Account tag on photo/video: 1 = required, 2 = must not have, "
                "0 = don't check (ENTER = 0): "
            ).strip() or "0"
            tag_required = {"1": True, "2": False}.get(raw_tag)
        else:
            raw_cta = input(
                "Free-text call to action on the story (presence only): "
                "1 = required, 0 = don't check (ENTER = 0): "
            ).strip() or "0"
            ajakan_required = raw_cta == "1"

        tasks_criteria.append({
            "task_id": i,
            "tipe": tipe_tugas,
            "hashtags": hashtags,
            "sticker_link": sticker_link,
            "keyword": keyword,
            "tag_required": tag_required,
            "ajakan_required": ajakan_required,
        })

    return save_daily_state(jenis_penugasan, tasks_criteria)


# ---------------------------------------------------------------------
# Targets and confirmed-account cache
# ---------------------------------------------------------------------

def load_targets(
    json_filename="targets.json"
):
    """Read accounts from the targets JSON as a flat list of {divisi, nama, username}."""
    project_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    json_path = os.path.join(
        project_dir,
        json_filename
    )

    if not os.path.exists(
        json_path
    ):
        raise FileNotFoundError(f"{json_filename} not found at {json_path}")

    with open(
        json_path,
        "r",
        encoding="utf-8"
    ) as f:

        data_divisi = json.load(f)

    targets = []

    for item in data_divisi:

        nama_divisi = item.get(
            "divisi",
            "UNKNOWN"
        )

        for anggota in item.get(
            "anggota",
            []
        ):

            targets.append({
                "divisi": nama_divisi,
                "nama": anggota.get(
                    "nama",
                    ""
                ),
                "username": str(
                    anggota.get(
                        "username",
                        ""
                    )
                )
                .strip()
                .replace(
                    "@",
                    ""
                ),
            })

    return targets


def _checked_accounts_path():
    """Path of the checked-accounts cache file."""
    project_dir = os.path.dirname(
        os.path.abspath(__file__)
    )
    return os.path.join(
        project_dir,
        CHECKED_ACCOUNTS_FILENAME
    )


def load_checked_accounts():
    """Load today's cache of accounts confirmed Correct. Empty if missing, unreadable or from another day."""
    path = _checked_accounts_path()
    today = now_wib().strftime("%Y-%m-%d")
    empty = {"date": today, "accounts": []}

    if not os.path.exists(path):
        return empty

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log("warn", f"Could not read {CHECKED_ACCOUNTS_FILENAME}: {e}. Starting with an empty cache.")
        return empty

    if not isinstance(data, dict) or data.get("date") != today:
        return empty

    if not isinstance(data.get("accounts"), list):
        data["accounts"] = []

    return data


def save_checked_accounts(cache):
    """Write the cache atomically (temp file, then replace) so a crash cannot corrupt it."""
    path = _checked_accounts_path()
    tmp_path = path + ".tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        log("fail", f"Could not save {CHECKED_ACCOUNTS_FILENAME}: {e}")


def add_checked_account(cache, username, nama, divisi):
    """Add one confirmed-Correct account and save the cache immediately."""
    cache["accounts"].append({
        "username": username,
        "nama": nama,
        "divisi": divisi,
    })
    save_checked_accounts(cache)


def resolve_hashtags_for_division(
    hashtags_template,
    divisi
):
    """Replace the `_DIVISI` placeholder in hashtags with the account's division."""
    divisi_clean = (
        divisi.upper()
        .replace(" ", "")
    )

    resolved = []

    for h in hashtags_template:

        resolved.append(
            h.replace(
                "_DIVISI",
                f"_{divisi_clean}"
            )
        )

    return resolved


def fmt_idr(amount):
    """Format an amount with dot thousands separators (1.000.000)."""
    return f"{amount:,}".replace(
        ",",
        "."
    )


# ---------------------------------------------------------------------
# Group report
# ---------------------------------------------------------------------

def generate_group_report(
    jenis_penugasan,
    total_checked,
    passed_count,
    confirmed_violations,
    division_type
):
    """Build the group report text for one division. Returns '' when there are no violations."""
    if not confirmed_violations:
        return ""

    today_date = datetime.now().strftime(
        "%d %B %Y"
    )

    report_lines = [
        f"[{REPORT_TITLE} – {today_date}]\n",
        REPORT_GREETING,
        f"{REPORT_INTRO}\n",
        "Keterangan denda:",
    ]

    for idx, v in enumerate(
        confirmed_violations,
        start=1
    ):
        status_text = (
            "Tidak mengerjakan penugasan"
            if v["status"] == STATUS_TDK_POST
            else ""
        )

        jumlah_tugas = v.get("jumlah_tugas", 1)
        subtotal = jumlah_tugas * v["nominal"]

        keterangan_line = (
            f"{v['keterangan_kesalahan']}"
            if v.get("keterangan_kesalahan")
            else ""
        )

        report_lines.append(
            f"{idx}. {v['name']}\n"
            f"- {status_text}{keterangan_line}\n"
            f"Denda : {jumlah_tugas} x "
            f"Rp{fmt_idr(v['nominal'])} "
            f"= Rp{fmt_idr(subtotal)}\n"
        )

    if REPORT_PAYMENT_TERMS:
        report_lines.append("[Ketentuan Pembayaran Denda]")
        report_lines.extend(REPORT_PAYMENT_TERMS.splitlines())

    return "\n".join(report_lines)


# ---------------------------------------------------------------------
# Waiting for the start signal
# ---------------------------------------------------------------------

def _enter_pressed_nonblocking():
    """Non-Windows check for ENTER without blocking."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except Exception:
        return False

    if not ready:
        return False

    try:
        sys.stdin.readline()
    except Exception:
        pass

    return True


_CLOCK_DIGIT_GLYPHS = {
    "0": (" _ ", "| |", "|_|"),
    "1": ("   ", "  |", "  |"),
    "2": (" _ ", " _|", "|_ "),
    "3": (" _ ", " _|", " _|"),
    "4": ("   ", "|_|", "  |"),
    "5": (" _ ", "|_ ", " _|"),
    "6": (" _ ", "|_ ", "|_|"),
    "7": (" _ ", "  |", "  |"),
    "8": (" _ ", "|_|", "|_|"),
    "9": (" _ ", "|_|", " _|"),
    ":": ("   ", " . ", " . "),
}


def _render_ascii_clock(time_str):
    """Render 'HH:MM:SS' as three rows of small ASCII digits."""
    rows = ["", "", ""]

    for ch in time_str:

        glyph = _CLOCK_DIGIT_GLYPHS.get(
            ch,
            ("   ", "   ", "   ")
        )

        for i in range(3):
            rows[i] += glyph[i] + " "

    return rows


def _enable_ansi_on_windows():
    """Enable ANSI escape codes on the Windows console (needed to redraw the clock in place)."""
    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)

        mode = ctypes.c_uint32()

        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle,
                mode.value | 0x0004
            )

    except Exception:
        pass


def wait_for_space(auto_deadline=None):
    """Show a live clock until SPACE/ENTER is pressed or `auto_deadline` passes."""
    log("info", "Daily setup loaded. Press SPACE or ENTER to start.")

    if auto_deadline:
        log("info", f"Auto-start at {auto_deadline:%H:%M:%S} WIB.")

    print("\nCurrent time (WIB):")

    _enable_ansi_on_windows()

    clock_drawn = False
    last_time_str = None

    while True:
        now = now_wib()
        time_str = now.strftime("%H:%M:%S")

        if time_str != last_time_str:
            last_time_str = time_str

            if clock_drawn:
                # Move up over the previous clock (3 rows + 1 spacer).
                sys.stdout.write("\033[4A")

            for row in _render_ascii_clock(time_str):
                sys.stdout.write(f"\033[K{row}\n")

            sys.stdout.write("\033[K\n")
            sys.stdout.flush()
            clock_drawn = True

        if msvcrt:
            if msvcrt.kbhit() and msvcrt.getch() in (b" ", b"\r"):
                return
        elif _enter_pressed_nonblocking():
            return

        if auto_deadline and now >= auto_deadline:
            log("info", f"{now:%H:%M:%S} WIB reached, starting automatically.")
            return

        time.sleep(0.1)


# ---------------------------------------------------------------------
# Phase 4: review
# ---------------------------------------------------------------------

def _prompt_keterangan_kesalahan(item):
    """Ask for an optional note on a confirmed Mistake (group report only, never sent to Sheets)."""
    if item["status"] != STATUS_SALAH:
        return item

    note = input("    Note for report (ENTER = skip): ").strip()

    if not note:
        return item

    return {**item, "keterangan_kesalahan": note}


def review_flagged_item(item):
    """Let the operator review one flagged item.

    ENTER = confirm detected status | p = pass | s = mistake | t = not done.
    Manual-review items have no detected status, so ENTER is not accepted for them.
    Returns the confirmed item, or None when passed.
    """
    is_manual = item["status"] == STATUS_REVIEW

    options = "p = pass | s = mistake | t = not done"
    prompt = f"    {options}: " if is_manual else f"    ENTER = confirm | {options}: "

    while True:
        choice = input(prompt).strip().lower()

        if choice in ("p", "pass"):
            return None

        if choice == "" and not is_manual:
            return _prompt_keterangan_kesalahan(item)

        if choice in ("s", "t"):
            status = STATUS_SALAH if choice == "s" else STATUS_TDK_POST
            confirmed = {**item, "status": status, "nominal": NOMINAL[status]}
            return _prompt_keterangan_kesalahan(confirmed)

        log("warn", "Invalid input.", 1)


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------

def _fine_text(count, nominal):
    """Format a fine as `count x RpX = RpY`."""
    return f"{count} x Rp{fmt_idr(nominal)} = Rp{fmt_idr(count * nominal)}"


def _personalize_tasks(raw_tasks_criteria, divisi):
    """Copy the tasks for one account, with the division placeholder in hashtags resolved."""
    personalized = []

    for task in raw_tasks_criteria:
        cloned = task.copy()
        cloned["hashtags"] = resolve_hashtags_for_division(task.get("hashtags", []), divisi)
        personalized.append(cloned)

    return personalized


def _print_account_header(idx, total, username, divisi):
    """Print the separator line that starts an account's log block."""
    with _PRINT_LOCK:
        print(f"\n{THIN}\n[{idx}/{total}] https://www.instagram.com/{username}/ ({divisi})")


def _score_account(
    target, personalized_criteria, task_results, took,
    checked_cache, pre_cutoff_run, report_cutoff, flagged_list
):
    """Turn one account's task results into a cached pass or flagged entries. Returns 1 if passed, else 0."""
    divisi = target["divisi"]
    username = target["username"]
    jumlah_tugas = len(personalized_criteria)

    # Fines are counted per failed task, not per task assigned.
    missing_eval = jumlah_tugas - len(task_results)
    status_counts = Counter(r for r in task_results if r != STATUS_BENAR)

    # A task that was never evaluated counts as Manual Review, never silently ignored.
    if missing_eval > 0:
        status_counts[STATUS_REVIEW] += missing_eval

    all_passed = not status_counts and len(task_results) == jumlah_tugas

    if all_passed:
        # Correct is final: persist immediately so a restart never re-checks this account.
        add_checked_account(checked_cache, username, target.get("nama", ""), divisi)
        log("ok", f"https://www.instagram.com/{username}/ passed ({took:.1f}s), saved to {CHECKED_ACCOUNTS_FILENAME}", 1)
        return 1

    if pre_cutoff_run:
        log("skip", f"https://www.instagram.com/{username}/ not passed yet ({took:.1f}s), will be re-checked after {report_cutoff:%H:%M} WIB", 1)
        return 0

    # One flagged entry per status, so each carries the right rate and task count.
    for status in _SEVERITY_ORDER:
        count = status_counts.get(status, 0)

        if count == 0:
            continue

        nominal = NOMINAL.get(status, 0)

        flagged_list.append({
            "divisi": divisi,
            "name": target.get("nama", ""),
            "username": username,
            "status": status,
            "nominal": nominal,
            "jumlah_tugas": count,
        })

        log("fail", f"https://www.instagram.com/{username}/ {STATUS_LABEL[status]}: {_fine_text(count, nominal)} ({took:.1f}s)", 1)

    return 0


def _use_parallel_tabs(raw_tasks_criteria):
    """True when today's tasks include both Story and Feed (and nothing else), so two tabs can split the work."""
    if not USE_PARALLEL_TABS:
        return False

    story_tasks, feed_tasks, other_tasks = _split_tasks(raw_tasks_criteria)
    return bool(story_tasks and feed_tasks and not other_tasks)


def _check_accounts(pending_targets, raw_tasks_criteria, jenis_penugasan, checked_cache, pre_cutoff_run, report_cutoff):
    """Phase 2-3: open each pending account and score it.

    With both Story and Feed tasks, two tabs work at the same time (see _check_accounts_parallel).
    Returns (flagged_list, newly_passed_count).
    """
    if _use_parallel_tabs(raw_tasks_criteria):
        return _check_accounts_parallel(
            pending_targets, raw_tasks_criteria, checked_cache, pre_cutoff_run, report_cutoff
        )

    flagged_list = []
    passed_count = 0
    total = len(pending_targets)

    p, browser, context, page = launch_authenticated_browser(headless=False)

    try:
        for idx, target in enumerate(pending_targets, start=1):
            t_account = time.time()

            _print_account_header(idx, total, target["username"], target["divisi"])

            personalized_criteria = _personalize_tasks(raw_tasks_criteria, target["divisi"])

            task_results = check_instagram_post(
                page,
                target["username"],
                tasks_criteria=personalized_criteria
            )

            passed_count += _score_account(
                target, personalized_criteria, task_results, secs(t_account),
                checked_cache, pre_cutoff_run, report_cutoff, flagged_list
            )

            time.sleep(1.5)

    except InstagramSessionError as e:
        log("fail", f"Stopped: {e}")
        print("      Open the Chrome profile, complete the login/checkpoint, then run again.")

    except Exception as e:
        log("fail", f"Unexpected error: {e}")

    finally:
        context.close()
        browser.close()
        p.stop()

    return flagged_list, passed_count


def _close_session(session):
    """Close a (p, browser, context, page) session, ignoring errors."""
    if not session:
        return

    p, browser, context, _page = session

    for closer in (context.close, browser.close, p.stop):
        try:
            closer()
        except Exception:
            pass


def _check_accounts_parallel(pending_targets, raw_tasks_criteria, checked_cache, pre_cutoff_run, report_cutoff):
    """Check Story and Feed tasks at the same time, one browser tab per task type.

    Playwright's sync API cannot share one page or browser between threads, so each worker
    starts its own authenticated session inside its own thread: the Story worker reads
    stories, the Feed worker reads posts. The main thread only waits for both halves of an
    account, then scores it, so cache and report writes stay single-threaded and in order.

    If the Feed session cannot be started (for example the Chrome profile is locked by the
    Story session), the Story worker does the Feed part itself, one account at a time.
    """
    total = len(pending_targets)
    flagged_list = []

    slots = [
        {
            "story": None,
            "feed": None,
            "story_done": threading.Event(),
            "feed_done": threading.Event(),
            "t0": None,
            "scored": False,
        }
        for _ in pending_targets
    ]

    stop = threading.Event()               # set on a session error: workers stop picking up accounts
    story_launched = threading.Event()     # Feed worker starts its session only after the Story session is up
    feed_launch_done = threading.Event()   # set once we know whether the Feed session started
    feed_session_ok = [False]
    errors = []
    header_lock = threading.Lock()
    score_lock = threading.Lock()
    passed_total = [0]

    def start_account(idx):
        """Print the account header once, from whichever worker reaches the account first."""
        with header_lock:
            slot = slots[idx]
            if slot["t0"] is None:
                slot["t0"] = time.time()
                target = pending_targets[idx]
                _print_account_header(idx + 1, total, target["username"], target["divisi"])

    def run_part(page, idx, kind):
        target = pending_targets[idx]
        criteria = _personalize_tasks(raw_tasks_criteria, target["divisi"])
        start_account(idx)
        return check_tasks_of_one_type(page, target["username"], criteria, kind)

    def fail(exc):
        errors.append(exc)
        stop.set()

    def score_slot(idx):
        """Score account `idx` once both tabs delivered. Safe to call from any thread, any number of times."""
        with score_lock:
            slot = slots[idx]

            if slot["scored"] or slot["story"] is None or slot["feed"] is None:
                return

            slot["scored"] = True
            target = pending_targets[idx]
            personalized_criteria = _personalize_tasks(raw_tasks_criteria, target["divisi"])

            passed_total[0] += _score_account(
                target, personalized_criteria, slot["story"] + slot["feed"],
                secs(slot["t0"] or time.time()),
                checked_cache, pre_cutoff_run, report_cutoff, flagged_list
            )

    def wait_previous(idx, part):
        """Barrier: do not start account `idx` until BOTH tabs finished account `idx - 1`.

        Without it, the faster tab (usually Feed) races ahead to the next account while the
        other tab is still reading the previous one. Returns False when the run must stop.
        """
        if idx == 0:
            return True

        prev = slots[idx - 1]
        announced = False

        while not (prev["story_done"].is_set() and prev["feed_done"].is_set()):
            if stop.is_set():
                return False

            # The other tab died without setting `stop`: nothing left to wait for.
            if not all(t.is_alive() for t in threads):
                return False

            if not announced:
                other = "Story" if part == "feed" else "Feed"
                log("info", f"Waiting for the {other} tab to finish https://www.instagram.com/{pending_targets[idx - 1]['username']}/", 1)
                announced = True

            time.sleep(0.1)

        # Print the previous account's result before the next account's header appears.
        score_slot(idx - 1)
        return True

    def story_worker():
        _log_ctx.label = "(Story) "
        session = None

        try:
            session = launch_authenticated_browser(headless=False)
            page = session[3]
            story_launched.set()

            feed_launch_done.wait(timeout=PARALLEL_FEED_LAUNCH_TIMEOUT)
            also_feed = not feed_session_ok[0]

            if also_feed:
                log("warn", "Feed tab not available, Story tab will also read the Feed (sequential)")

            for idx in range(total):
                if stop.is_set() or not wait_previous(idx, "story"):
                    break

                slots[idx]["story"] = run_part(page, idx, "story")
                slots[idx]["story_done"].set()

                if also_feed:
                    _log_ctx.label = "(Feed) "
                    slots[idx]["feed"] = run_part(page, idx, "feed")
                    slots[idx]["feed_done"].set()
                    _log_ctx.label = "(Story) "

                time.sleep(1.5)

        except Exception as e:
            fail(e)

        finally:
            story_launched.set()
            feed_launch_done.set()
            _close_session(session)

    def feed_worker():
        _log_ctx.label = "(Feed) "
        session = None

        try:
            story_launched.wait()

            if stop.is_set():
                return

            try:
                session = launch_authenticated_browser(headless=False)
                feed_session_ok[0] = True
            except Exception as e:
                log("warn", f"Could not start a second browser session: {e}")
                return
            finally:
                feed_launch_done.set()

            page = session[3]

            for idx in range(total):
                if stop.is_set() or not wait_previous(idx, "feed"):
                    break

                slots[idx]["feed"] = run_part(page, idx, "feed")
                slots[idx]["feed_done"].set()
                time.sleep(1.5)

        except Exception as e:
            fail(e)

        finally:
            feed_launch_done.set()
            _close_session(session)

    threads = [
        threading.Thread(target=story_worker, name="story-tab", daemon=True),
        threading.Thread(target=feed_worker, name="feed-tab", daemon=True),
    ]

    log("info", "Parallel mode: tab 1 reads Stories, tab 2 reads Feeds")

    for t in threads:
        t.start()

    try:
        for idx, target in enumerate(pending_targets):
            slot = slots[idx]

            # Wait for both halves of this account (or for a worker to stop early).
            while not (slot["story_done"].is_set() and slot["feed_done"].is_set()):
                if not any(t.is_alive() for t in threads):
                    break

                time.sleep(0.2)

            if slot["story"] is None or slot["feed"] is None:
                break

            score_slot(idx)

    finally:
        stop.set()

        for t in threads:
            t.join(timeout=60)

    seen = set()

    for e in errors:
        if str(e) in seen:
            continue

        seen.add(str(e))

        if isinstance(e, InstagramSessionError):
            log("fail", f"Stopped: {e}")
            print("      Open the Chrome profile, complete the login/checkpoint, then run again.")
        else:
            log("fail", f"Unexpected error: {e}")

    return flagged_list, passed_total[0]


def _export_to_sheets(confirmed_violations, jenis_penugasan):
    """Append every confirmed violation to the matching Sheets tab (VAULT or ARENA)."""
    log("info", "Exporting to Google Sheets (staging)...")

    try:
        if not SPREADSHEET_NAME:
            raise ValueError("SPREADSHEET_NAME is not set in .env")

        sheet_groups = {"ARENA": [], "VAULT": []}

        for item in confirmed_violations:
            tab = "VAULT" if "VAULT" in item.get("divisi", "").upper() else "ARENA"
            sheet_groups[tab].append(item)

        total_exported = 0

        for tab_name, items in sheet_groups.items():
            if not items:
                continue

            worksheet = connect_sheets(SPREADSHEET_NAME, worksheet_name=tab_name)

            for item in items:
                append_violation_row(
                    worksheet=worksheet,
                    name=item.get("name") or item.get("username", ""),
                    username=item["username"],
                    jenis_pelanggaran=item["status"],
                    nominal_denda=item.get("jumlah_tugas", 1) * item["nominal"],
                    jenis_penugasan=jenis_penugasan,
                )
                total_exported += 1

        log("ok", f"Exported {total_exported} fines to Google Sheets")

    except Exception as e:
        log("fail", f"Google Sheets export failed: {e}")


def _publish_reports(confirmed_violations, jenis_penugasan, total_targets, passed_count):
    """Build the group report per division, save it to a .txt file and send it to Google Docs."""
    section("GROUP REPORT")

    reports = {
        "arena": [v for v in confirmed_violations if "VAULT" not in v.get("divisi", "").upper()],
        "vault": [v for v in confirmed_violations if "VAULT" in v.get("divisi", "").upper()],
    }

    any_report = False
    os.makedirs("report", exist_ok=True)   # git does not ship this folder

    for division_type, violations in reports.items():
        name = division_type.upper()
        report_filename = f"report/report_{division_type}.txt"

        report_text = generate_group_report(
            jenis_penugasan,
            total_targets,
            passed_count,
            violations,
            division_type
        )

        if not report_text:
            log("info", f"{name}: no confirmed violations")

            if os.path.exists(report_filename):
                os.remove(report_filename)

            continue

        any_report = True
        print(f"\n--- {name} ---\n{report_text}\n")

        with open(report_filename, "w", encoding="utf-8") as f:
            f.write(report_text)

        log("ok", f"{name}: saved to {report_filename}")

        try:
            docs_kwargs = {"doc_title_or_url": DOC_NAME} if DOC_NAME else {}
            send_report_to_docs(report_text, division_type, **docs_kwargs)
        except Exception as e:
            log("fail", f"{name}: could not send to Google Docs: {e}")

    if any_report:
        if STAGING_SHEET_URL:
            print(f"\nSheets (staging):  {STAGING_SHEET_URL}")
        if OFFICIAL_SHEET_URL:
            print(f"Sheets (official): {OFFICIAL_SHEET_URL}")


def run_paid_promote_checker(state):
    """Run the full flow: check accounts, review flagged ones, export and report.

    Before the task window closes the run is a pre-check: accounts that are Correct
    are cached, everyone else is left for a later run, and nothing is fined.
    """
    jenis_penugasan = state.get("jenis_penugasan") or state.get("jenis", "Paid Promod")
    raw_tasks_criteria = state.get("tasks_criteria", [])

    try:
        targets = load_targets("targets.json")
    except Exception as e:
        log("fail", f"Could not read targets.json: {e}")
        return

    # Accounts already confirmed Correct in an earlier run are not checked again.
    checked_cache = load_checked_accounts()
    checked_usernames = {acc.get("username", "").lower() for acc in checked_cache["accounts"]}

    pending_targets = [t for t in targets if t["username"].lower() not in checked_usernames]
    already_cached = len(targets) - len(pending_targets)

    # Decided once per run, so an account is never half-finalised when the clock passes the cutoff.
    window_start, report_cutoff = story_window()
    pre_cutoff_run = now_wib() < report_cutoff

    section(f"PAID PROMOTE CHECKER | {jenis_penugasan}")
    print(f"Accounts : {len(targets)} total | {already_cached} cached as correct | {len(pending_targets)} to check")
    print(f"Window   : {window_start:%H:%M}-{report_cutoff:%H:%M} WIB")
    print(
        "Mode     : "
        + (
            f"PRE-CHECK (window still open, nothing is fined before {report_cutoff:%H:%M} WIB)"
            if pre_cutoff_run
            else "FINAL"
        )
    )

    flagged_list = []
    passed_count = already_cached

    if not pending_targets:
        log("ok", "All accounts are already confirmed correct")

    else:
        section("CHECKING ACCOUNTS")

        flagged_list, newly_passed = _check_accounts(
            pending_targets,
            raw_tasks_criteria,
            jenis_penugasan,
            checked_cache,
            pre_cutoff_run,
            report_cutoff
        )
        passed_count += newly_passed

    if pre_cutoff_run:
        section("PRE-CHECK FINISHED")
        print(f"Correct so far: {passed_count}/{len(targets)}")
        print(
            f"Run again at any time (also after {report_cutoff:%H:%M} WIB) to continue. "
            "Correct accounts are not re-checked."
        )
        return

    # ------------------------------ Review ------------------------------
    section("REVIEW")

    confirmed_violations = []

    if not flagged_list:
        log("ok", "All accounts passed, no fines")

    else:
        print(f"{len(flagged_list)} flagged items need review.\n")

        for i, item in enumerate(flagged_list, start=1):
            print(
                f"[{i}/{len(flagged_list)}] https://instagram.com/{item['username']} "
                f"| {item['divisi']} | {STATUS_LABEL[item['status']]} "
                f"| {_fine_text(item.get('jumlah_tugas', 1), item['nominal'])}"
            )

            confirmed = review_flagged_item(item)

            if confirmed:
                confirmed_violations.append(confirmed)

            print()

        if confirmed_violations:
            _export_to_sheets(confirmed_violations, jenis_penugasan)
        else:
            log("info", "Every flagged item was passed by the operator")

    _publish_reports(confirmed_violations, jenis_penugasan, len(targets), passed_count)


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------

BANNER = r"""
 @@@@@@@%*++*++++=+=#@%*%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 @%**%%#*=*****+====*#**%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 @@#++**+==+++*+==+++*++%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 @@@#**+****++*+=*%*****%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 @@@@%**#%%#**+#%%%@%*+*@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 %@%*+=--++=---+%@@@+-=%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 --:::::::::::=@@@@+:-*#%@@@@@@*#@%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 :::::::::::--=@@@@-:=*#%@@@@%%%@@@##@@@@@@@@@@%#%%%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 :::::::::::==-#@@@=:++%%@%%@%#*==#@#*%@@@@@@@@@@%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 :::::::::::+--+@@@@--*#%%%@%%#%%@@@@#%@@@@@@@@@%%##%%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 ::::::::::::+=-#@@@@*=#%%%@@@@@@@@@@#@@@@@@@%+*@=-=@@-+#%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
 :::::----::==*+*@@@@@*-#%%@@@@@@@@@##@@@@@@@@@#*#@%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@       **                         *******            **      **       *******                                         **                   ******  **                      **
 :::::::::---=*==#@@@@@+**%@@@@@@@@#*@@@@@@@@@@@@@@@@@@@@@@@@@%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@      /**           **   **      /**////**          //      /**      /**////**                                       /**                  **////**/**                     /**
 ------------+=++=#@@@@@*+@@@@@@@@#*%@@@@@@@@@@@@@@@@@@@@@@@@**=#@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@      /** **    ** //** **       /**   /**  ******   **     /**      /**   /** ******  ******  **********   ******  ******  *****        **    // /**       *****   ***** /**  **  *****  ******
 ------------+++#%+%@@@@@#%@@@@@@#+*@@@@@@@@@@@@@@@@@@@@@@@@%===@**@@@@@@@@@@@@@@@@@@@@@@@@%@@@@@@@      /**/**   /**  //***        /*******  //////** /**  ******      /******* //**//* **////**//**//**//** **////**///**/  **///**      /**       /******  **///** **///**/** **  **///**//**//*
 --------------+-==@@@@@@@%%@@@@%+*%@@@@@@@@@@@@@@@@@@@@@@@@@-+===--%@%@@@@@@@@@@@@@@@@%::::+@@@@@@      /**//** /**    /**         /**////    ******* /** **///**      /**////   /** / /**   /** /** /** /**/**   /**  /**  /*******      /**       /**///**/*******/**  // /****  /******* /** /
 ------------==-=*==#@@@@@@%%@@@@#==@@@@@@@@@@@@@@@@@@@@@@@@@**++-=+*@@@@@@@@@@@@@@@@+=-::::-@@@@@@      /** //****     **          /**       **////** /**/**  /**      /**       /**   /**   /** /** /** /**/**   /**  /**  /**////       //**    **/**  /**/**//// /**   **/**/** /**////  /**
 -------------==+===*@@@@@@@#%@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@%*#*#@*%*@%@@@@@@@@@@@@+=====+-:%@@@@@      /**  //**     **           /**      //********/**//******      /**      /***   //******  *** /** /**//******   //** //******       //****** /**  /**//******//***** /**//**//******/***
 -------------=#=**+++@@@@@@@*%%%%@@@@@@@@@@@@@@@@@@@@@@@@%@@*+*@@%@@#%%@@@@@@@@@@@@+=--=---:-@@@@@      //    //     //            //        //////// //  //////       //       ///     //////  ///  //  //  //////     //   //////         //////  //   //  //////  /////  //  //  ////// ///
 ===----====--#====#%@@@@@@@@#+**=--=#%%@@@@@@@@@@@@@@@@@@+@%*+@@%%@@%@%%@@@@@@+@@@*+=++-::::#@@@@@
 ===============--+***#%%@@@@%+++=+****#*%#%%@@@@@@@@@@@+:+=#*@@%*@@@@@%@@@@@@@##@%-==-:::::::=+*%@
 ============-:==-+**%%%**@@@@+-=+==+*####%@@@@@@@@@@#=:::*-+@@%*%@@@@@@@@@@@@@@%*%+:-:::...::::--:
 ===========-.:-+=**%@@@%**%*%==--*@@@@@@@@@@@@@@@@=::::::--#@%*#@@@@@@@@@@@@@@+--=:::::::::::::-::
 ==========-:-@*=+=*#**####**-====++#@@@@@@%==++=:::::::::--%%%%@@@@@@@@@@@@@@#+=-:::::::::::::::--
 ==========::%=++*=*#+#+**=*---=++*#**::...:::.:::::::::::--@%%@@@#@@@@@@@@*@*=::--:::::::.::::::--
 =========:.:::-#@@@@@@@@@--====+*#%#+-::::..:..:::::.::::-=%%@@@%%%@@@@@@-*%=:::-:::::::::::::::::
 ========:.::::::-+@@@@@+-===+*+*#%%%*-::::::....::.:.::::-=+@@@@%#%@@@@@*=@=:::--::::::..:::::::::
 +======:...:::::::::::::--+**%@%*%%%@@@@@#=.:::::::::::::=+=@@@%%@@@@@@@=*@=--::.:::::::::::::::::
"""


def main():
    """Load or create today's setup, wait for the start signal, then run the checker."""
    print(BANNER)

    # With no gatekeeper tags every story/post would count as a task item, so stop early.
    if not GATEKEEPER_KEYWORDS:
        log("fail", "GATEKEEPER_KEYWORDS is not set in .env (comma-separated hashtags, without '#')")
        return

    if not REPORT_PAYMENT_TERMS:
        log("warn", "REPORT_PAYMENT_TERMS is not set in .env, so the group report will have no payment terms")

    state = load_daily_state()
    today = datetime.now().strftime("%Y-%m-%d")

    if state.get("status") != "LOADED" or state.get("date") != today:
        log("info", "Daily setup is not loaded for today. Enter today's requirements.")
        state = phase1_dynamic_setup()

    # Once the task window closes, the run starts by itself (SPACE/ENTER can still start it earlier).
    auto_deadline = story_window()[1] + timedelta(seconds=1)

    wait_for_space(auto_deadline=auto_deadline)

    run_paid_promote_checker(state)


if __name__ == "__main__":
    main()