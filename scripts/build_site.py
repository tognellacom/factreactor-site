#!/usr/bin/env python3
"""Build the FactReactor site from the public YouTube channel feed.

Reads the channel's Atom feed, merges it into data/videos.json (the feed only
ever carries the newest 15 entries, so the archive has to persist here), reads
one Atom feed per YouTube playlist to learn each video's topic, and renders a
static site into _site/.

Standard library only, no dependencies.

Environment:
  CHANNEL_ID  YouTube channel id (default: the FactReactor channel)
  BASE_URL    Absolute site root, e.g. https://factreactor.com. Falls back to
              the GitHub Pages URL derived from GITHUB_REPOSITORY.
  FEED_FILE   Read the feed from this file instead of the network (for tests).
  PLAYLIST_FEED_DIR
              Read the playlist feeds from <dir>/<playlist_id>.xml instead of
              the network (for tests).
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

CHANNEL_ID = os.environ.get("CHANNEL_ID", "UCnWVqsL-mppKqIWIJypERiQ")
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
PLAYLIST_FEED_URL = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"

SITE_NAME = "FactReactor"
SITE_TAGLINE = "One genuine aha moment in under a minute."
SITE_DESCRIPTION = (
    "Short science videos about things you were wrong about — your own body, "
    "the physics you live in, the systems running without your knowledge. "
    "Every claim checked against the original research."
)
CHANNEL_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"

# Google AdSense. Empty string disables the tag everywhere.
ADSENSE_CLIENT = "ca-pub-5873583387305949"

# Google Analytics 4 measurement id, e.g. "G-XXXXXXXXXX". Empty disables it.
ANALYTICS_ID = "G-10JB28937H"

# Google Consent Mode v2. Everything that could store or share data starts
# DENIED and stays denied until a consent management platform grants it — the
# consent message configured in AdSense under Privacy & messaging is what
# updates this. Without the defaults below, the ad and analytics tags would
# store data on first paint, before anyone is asked.
CONSENT_DEFAULTS = """window.dataLayer=window.dataLayer||[];
function gtag(){dataLayer.push(arguments);}
gtag('consent','default',{
 'ad_storage':'denied',
 'ad_user_data':'denied',
 'ad_personalization':'denied',
 'analytics_storage':'denied',
 'functionality_storage':'granted',
 'security_storage':'granted',
 'wait_for_update':500
});
gtag('set','ads_data_redaction',true);"""
SOCIAL = [
    ("YouTube", CHANNEL_URL),
    ("TikTok", "https://www.tiktok.com/@factreactor_hq"),
    ("Instagram", "https://www.instagram.com/factreactor_hq"),
    ("Facebook", "https://www.facebook.com/1375562482297804"),
    ("X", "https://x.com/FactReactor_hq"),
]

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
STATE_PATH = ROOT / "data" / "videos.json"
TOPICS_PATH = ROOT / "data" / "topics.json"
PLAYLISTS_PATH = ROOT / "data" / "playlists.json"
IMPRINT_PATH = ROOT / "data" / "imprint.json"
CORRECTIONS_PATH = ROOT / "data" / "corrections.json"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

URL_RE = re.compile(r"https?://[^\s<>\"']+")


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------

def custom_domain() -> str:
    """The hostname from a root CNAME file, if the repo has one."""
    path = ROOT / "CNAME"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        host = line.strip()
        if host and not host.startswith("#"):
            return host
    return ""


def base_url() -> str:
    explicit = os.environ.get("BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    domain = custom_domain()
    if domain:
        return f"https://{domain}"
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return ""


def fetch_feed() -> str:
    override = os.environ.get("FEED_FILE")
    if override:
        return Path(override).read_text(encoding="utf-8")
    req = urllib.request.Request(
        FEED_URL, headers={"User-Agent": "factreactor-site-builder/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def parse_feed(raw: str) -> list[dict]:
    root = ET.fromstring(raw)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id = (entry.findtext("yt:videoId", "", NS) or "").strip()
        if not video_id:
            continue
        title = (entry.findtext("atom:title", "", NS) or "").strip()
        published = (entry.findtext("atom:published", "", NS) or "").strip()

        description, thumbnail = "", ""
        group = entry.find("media:group", NS)
        if group is not None:
            description = (group.findtext("media:description", "", NS) or "").strip()
            node = group.find("media:thumbnail", NS)
            if node is not None:
                thumbnail = (node.get("url") or "").strip()

        entries.append(
            {
                "video_id": video_id,
                "title": title,
                "published": published,
                "description": description,
                "thumbnail": thumbnail
                or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
            }
        )
    return entries


# --------------------------------------------------------------------------
# topics, read off the playlists
# --------------------------------------------------------------------------
#
# The channel sorts every Short into a YouTube playlist named after its field,
# and that assignment is made at publishing time, on YouTube, by the person who
# publishes. So the topic already exists in a place this build can read - which
# is why nothing here guesses from keywords and why nobody has to copy a line
# into data/topics.json per video any more.
#
# Playlist feeds carry the newest 15 entries, exactly like the channel feed, so
# the same rule applies: the feed is only the supplier, data/topics.json is the
# archive, and an entry once written is never removed by this build.


def load_playlists() -> list[tuple[str, str]]:
    """(playlist_id, topic) in file order; earlier entries win a conflict."""
    if not PLAYLISTS_PATH.exists():
        return []
    try:
        raw = json.loads(PLAYLISTS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read {PLAYLISTS_PATH}: {exc}", file=sys.stderr)
        return []
    return [
        (k, str(v).strip())
        for k, v in raw.items()
        if not k.startswith("_") and str(v).strip()
    ]


def fetch_playlist(playlist_id: str) -> str:
    override = os.environ.get("PLAYLIST_FEED_DIR")
    if override:
        path = Path(override) / f"{playlist_id}.xml"
        return path.read_text(encoding="utf-8") if path.exists() else ""
    req = urllib.request.Request(
        PLAYLIST_FEED_URL.format(playlist_id),
        headers={"User-Agent": "factreactor-site-builder/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def discover_topics() -> dict:
    """video_id -> topic, read off the public playlist feeds.

    Every failure here is survivable on purpose: a playlist that cannot be read
    is reported and skipped, and what data/topics.json already holds stays put.
    A build must never drop a topic because YouTube was briefly unreachable.
    """
    found: dict[str, str] = {}
    for playlist_id, topic in load_playlists():
        try:
            raw = fetch_playlist(playlist_id)
        except Exception as exc:
            print(
                f"warning: playlist {topic} ({playlist_id}) could not be read: "
                f"{exc} - existing assignments are kept",
                file=sys.stderr,
            )
            continue

        try:
            ids = [e["video_id"] for e in parse_feed(raw)] if raw.strip() else []
        except ET.ParseError as exc:
            print(
                f"warning: playlist {topic} ({playlist_id}) returned no usable "
                f"feed: {exc}",
                file=sys.stderr,
            )
            continue

        if not ids:
            # The two ways this happens are worth naming, because both are
            # silent otherwise: a wrong id (the part after 'list=' in the
            # playlist URL) and a playlist set to private.
            print(
                f"warning: playlist {topic} ({playlist_id}) returned no videos - "
                "check the id against the 'list=' part of the playlist URL, and "
                "that the playlist is public or unlisted rather than private",
                file=sys.stderr,
            )
            continue

        for vid in ids:
            if vid in found:
                if found[vid] != topic:
                    print(
                        f"warning: {vid} is in both {found[vid]} and {topic} - "
                        f"keeping {found[vid]}, which stands first in "
                        f"{PLAYLISTS_PATH.name}",
                        file=sys.stderr,
                    )
                continue
            found[vid] = topic
    return found


def sync_topics(discovered: dict) -> int:
    """Fold the playlist reading into data/topics.json, in place.

    Returns how many assignments changed. Comment keys and any hand-written
    entry for a video that is in no playlist survive untouched.
    """
    raw: dict = {}
    if TOPICS_PATH.exists():
        try:
            raw = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: could not read {TOPICS_PATH}: {exc}", file=sys.stderr)
            return 0

    changed = 0
    for vid, topic in discovered.items():
        if raw.get(vid) != topic:
            raw[vid] = topic
            changed += 1

    if changed:
        TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOPICS_PATH.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return changed


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def slugify(text: str, fallback: str) -> str:
    slug = text.lower().replace("&", " and ")
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug[:80].strip("-") or fallback


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("videos"), dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: could not read {STATE_PATH}: {exc}", file=sys.stderr)
    return {"videos": {}}


def merge(state: dict, entries: list[dict]) -> tuple[dict, int]:
    """Fold feed entries into the archive. Nothing is ever removed."""
    videos = state["videos"]
    taken = {v.get("slug") for v in videos.values()}
    added = 0

    for entry in entries:
        vid = entry["video_id"]
        record = videos.get(vid)
        if record is None:
            slug = slugify(entry["title"], vid)
            # A retitled video must not steal an existing slug.
            if slug in taken:
                slug = f"{slug}-{vid.lower()}"
            taken.add(slug)
            record = {"slug": slug, "first_seen": now_iso()}
            videos[vid] = record
            added += 1

        # Refresh from the feed; the slug stays put so old links keep working.
        record["title"] = entry["title"]
        record["description"] = entry["description"]
        record["thumbnail"] = entry["thumbnail"]
        record["published"] = entry["published"] or record.get("published", "")

    return state, added


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_topics() -> dict:
    """video_id -> topic. Written by sync_topics(); _ keys are comments."""
    if not TOPICS_PATH.exists():
        return {}
    try:
        raw = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read {TOPICS_PATH}: {exc}", file=sys.stderr)
        return {}
    return {
        k: str(v).strip()
        for k, v in raw.items()
        if not k.startswith("_") and str(v).strip()
    }


def load_corrections() -> dict:
    """video_id -> clarification text, the video's pinned comment. Hand-kept
    (the feed does not carry comments); _ keys are comments."""
    if not CORRECTIONS_PATH.exists():
        return {}
    try:
        raw = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read {CORRECTIONS_PATH}: {exc}", file=sys.stderr)
        return {}
    return {
        k: str(v).strip()
        for k, v in raw.items()
        if not k.startswith("_") and str(v).strip()
    }


def load_imprint() -> dict:
    if not IMPRINT_PATH.exists():
        return {}
    try:
        raw = json.loads(IMPRINT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"warning: could not read {IMPRINT_PATH}: {exc}", file=sys.stderr)
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def imprint_is_complete() -> bool:
    """A legal notice with blanks in it is worse than none, so the page is only
    rendered and linked once the operator and a contact are actually filled in."""
    data = load_imprint()
    lines = [l for l in data.get("address", []) if str(l).strip()]
    return bool(str(data.get("operator", "")).strip()) and bool(
        str(data.get("email", "")).strip() or lines
    )


def ordered(state: dict, topics: dict) -> list[dict]:
    items = []
    for vid, rec in state["videos"].items():
        item = dict(rec)
        item["video_id"] = vid
        item["topic"] = topics.get(vid, "")
        items.append(item)
    items.sort(key=lambda v: (v.get("published", ""), v["video_id"]), reverse=True)
    return items


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def linkify(text: str) -> str:
    """Escape text, turning bare URLs into links without double-escaping them."""
    out, pos = [], 0
    for match in URL_RE.finditer(text):
        out.append(html.escape(text[pos : match.start()]))
        raw = match.group(0)
        url = raw.rstrip(".,);:")
        trailing = raw[len(url) :]
        safe = html.escape(url, quote=True)
        out.append(
            f'<a href="{safe}" rel="nofollow noopener" target="_blank">{safe}</a>'
        )
        out.append(html.escape(trailing))
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def paragraphs(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "\n".join(
        "<p>" + linkify(b).replace("\n", "<br>\n") + "</p>" for b in blocks
    )


def pretty_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def summary(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rsplit(" ", 1)[0] + "…"


def json_ld(payload: dict) -> str:
    # "</script" inside a JSON string would close the block early.
    return json.dumps(payload, ensure_ascii=False, indent=2).replace("<", "\\u003c")


CSS = """
:root {
  --bg: #07070c;
  --surface: #101019;
  --line: #23233a;
  --text: #eceaf5;
  --muted: #9694ad;
  --gold: #f5b544;
  --cyan: #4fd6e8;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-image:
    radial-gradient(900px 500px at 15% -10%, rgba(79, 214, 232, .10), transparent 65%),
    radial-gradient(800px 460px at 88% 4%, rgba(245, 181, 68, .10), transparent 62%);
  background-repeat: no-repeat;
}
.wrap { max-width: 940px; margin: 0 auto; padding: 0 20px; }
a { color: var(--cyan); }
a:hover { color: var(--gold); }

header.site { padding: 56px 0 34px; }
.brand {
  display: inline-flex; align-items: center; gap: 11px;
  font-size: 13px; letter-spacing: .22em;
  text-transform: uppercase; color: var(--gold); text-decoration: none; font-weight: 700;
}
.brand img { width: 44px; height: 44px; display: block; border-radius: 7px; }
header.site h1 { margin: 14px 0 8px; font-size: clamp(28px, 5vw, 42px); line-height: 1.15; }
.tagline { margin: 0; color: var(--muted); font-size: 18px; }
.links { margin-top: 22px; display: flex; flex-wrap: wrap; gap: 10px; }
.links a {
  border: 1px solid var(--line); border-radius: 999px; padding: 7px 16px;
  text-decoration: none; font-size: 14px; color: var(--text); background: var(--surface);
}
.links a:hover { border-color: var(--gold); color: var(--gold); }

.filters { display: flex; flex-wrap: wrap; gap: 9px; padding: 4px 0 26px; }
.filters button {
  font: inherit; font-size: 14px; cursor: pointer;
  background: var(--surface); color: var(--muted);
  border: 1px solid var(--line); border-radius: 999px; padding: 7px 15px;
}
.filters button:hover { color: var(--text); border-color: var(--muted); }
.filters button[aria-pressed="true"] {
  background: var(--gold); border-color: var(--gold); color: #14100a; font-weight: 700;
}
.filters .count { opacity: .65; font-weight: 400; }

.grid {
  display: grid; gap: 20px; padding: 0 0 60px;
  grid-template-columns: repeat(auto-fill, minmax(172px, 1fr));
}
.card {
  background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
  overflow: hidden; text-decoration: none; color: inherit; display: flex; flex-direction: column;
  transition: border-color .15s ease, transform .15s ease;
}
.card:hover { border-color: var(--gold); transform: translateY(-2px); }
.card[hidden] { display: none; }
/* Shorts are 9:16. YouTube pillarboxes them into the 4:3 hqdefault, so a
   centre crop back to 9:16 lands exactly on the original frame — no bars.
   height:auto is required, or the <img> height attribute beats aspect-ratio. */
.card .thumb { position: relative; }
.card img { width: 100%; height: auto; aspect-ratio: 9 / 16; object-fit: cover; display: block; background: #000; }
.card .topic {
  position: absolute; left: 8px; bottom: 8px;
  background: rgba(7, 7, 12, .82); border: 1px solid var(--line);
  color: var(--cyan); font-size: 11px; letter-spacing: .08em; text-transform: uppercase;
  padding: 3px 9px; border-radius: 999px; backdrop-filter: blur(4px);
}
.card .body { padding: 13px 15px 16px; }
.card h2 { margin: 0 0 7px; font-size: 15px; line-height: 1.35; }
.card time { color: var(--muted); font-size: 13px; }

main.video { padding-bottom: 64px; }
.player {
  margin: 0 auto 30px; width: min(360px, 100%); aspect-ratio: 9 / 16;
  max-height: 76vh; border-radius: 16px; overflow: hidden;
  border: 1px solid var(--line); background: #000;
}
.player iframe { width: 100%; height: 100%; border: 0; display: block; }
main.video h1 { font-size: clamp(24px, 4vw, 34px); line-height: 1.2; margin: 0 0 10px; }
.meta { color: var(--muted); font-size: 14px; margin-bottom: 26px; }
.body-copy p { margin: 0 0 18px; overflow-wrap: anywhere; }
main.video h2 { font-size: 18px; margin: 32px 0 8px; color: var(--gold); }
main.video p { margin: 0 0 14px; overflow-wrap: anywhere; }
main.video code {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 5px; padding: 1px 5px; font-size: 13px;
}
.watch {
  display: inline-block; margin: 6px 0 34px; padding: 12px 22px; border-radius: 999px;
  background: var(--gold); color: #14100a; font-weight: 700; text-decoration: none;
}
.watch:hover { background: #ffc95e; color: #14100a; }
.back { display: inline-block; margin-bottom: 28px; font-size: 14px; text-decoration: none; }

footer.site {
  border-top: 1px solid var(--line); padding: 26px 0 46px;
  color: var(--muted); font-size: 14px;
}
.empty { color: var(--muted); padding: 30px 0 70px; }
blockquote.note {
  margin: 0 0 10px; padding: 14px 18px; border-left: 3px solid var(--gold);
  background: var(--surface); border-radius: 0 10px 10px 0; overflow-wrap: anywhere;
}
main.video h2 a { color: var(--text); text-decoration: none; }
main.video h2 a:hover { color: var(--gold); }
"""


def head(
    title: str,
    description: str,
    canonical: str,
    image: str,
    og_type: str,
    prefix: str = "",
) -> str:
    """prefix walks back up to the site root, so asset links resolve from any
    depth and the site keeps working under a github.io project path too."""
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        f'<meta name="description" content="{html.escape(description, quote=True)}">',
        '<meta name="theme-color" content="#07070c">',
        f'<link rel="icon" type="image/png" sizes="32x32" href="{prefix}assets/icon-32.png">',
        f'<link rel="apple-touch-icon" sizes="180x180" href="{prefix}assets/icon-180.png">',
        f'<meta property="og:title" content="{html.escape(title, quote=True)}">',
        f'<meta property="og:description" content="{html.escape(description, quote=True)}">',
        f'<meta property="og:type" content="{og_type}">',
        f'<meta property="og:site_name" content="{SITE_NAME}">',
        '<meta name="twitter:card" content="summary_large_image">',
    ]
    if image:
        tags.append(f'<meta property="og:image" content="{html.escape(image, quote=True)}">')
        tags.append(f'<meta name="twitter:image" content="{html.escape(image, quote=True)}">')
    if canonical:
        tags.append(f'<link rel="canonical" href="{html.escape(canonical, quote=True)}">')
        tags.append(f'<meta property="og:url" content="{html.escape(canonical, quote=True)}">')
    # Order matters and is the whole point: the consent defaults must run
    # synchronously before either Google tag loads, otherwise both start with
    # consent granted and the defaults arrive too late to matter.
    if ADSENSE_CLIENT or ANALYTICS_ID:
        tags.append(f"<script>{CONSENT_DEFAULTS}</script>")

    if ANALYTICS_ID:
        gid = html.escape(ANALYTICS_ID, quote=True)
        tags.append(
            f'<script async src="https://www.googletagmanager.com/gtag/js?id={gid}"></script>'
        )
        tags.append(
            f"<script>gtag('js',new Date());gtag('config','{gid}');</script>"
        )

    if ADSENSE_CLIENT:
        tags.append(
            '<script async src="https://pagead2.googlesyndication.com/pagead/js/'
            f'adsbygoogle.js?client={html.escape(ADSENSE_CLIENT, quote=True)}"'
            ' crossorigin="anonymous"></script>'
        )
    tags.append(f"<style>{CSS}</style>")
    return "\n".join(tags)


def footer(prefix: str = "") -> str:
    """prefix walks back up to the site root: "" at the root, "../../" one
    level down under /v/<slug>/."""
    links = " · ".join(
        f'<a href="{html.escape(url, quote=True)}" rel="noopener" target="_blank">{name}</a>'
        for name, url in SOCIAL
    )
    legal = (
        f'<a href="{prefix}impressum/">Impressum</a>' if imprint_is_complete() else ""
    )
    pages = (
        f'<a href="{prefix}how-we-work/">How we work</a> · '
        f'<a href="{prefix}corrections/">Corrections</a>'
    )
    return (
        '<footer class="site"><div class="wrap">'
        f"{links}<br>"
        f"{pages} · "
        f"{legal + ' · ' if legal else ''}© {datetime.now(timezone.utc).year} {SITE_NAME}"
        "</div></footer>"
    )


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

FILTER_JS = """
(function () {
  var bar = document.querySelector('.filters');
  if (!bar) return;
  bar.hidden = false;
  bar.addEventListener('click', function (event) {
    var button = event.target.closest('button');
    if (!button) return;
    var wanted = button.dataset.topic;
    bar.querySelectorAll('button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b === button));
    });
    document.querySelectorAll('.card').forEach(function (card) {
      card.hidden = wanted !== '' && card.dataset.topic !== wanted;
    });
  });
})();
"""


def render_index(videos: list[dict], root: str) -> str:
    if videos:
        cards = "\n".join(
            f'''      <a class="card" href="v/{v["slug"]}/" data-topic="{html.escape(v.get("topic", ""), quote=True)}">
        <div class="thumb">
          <img src="{html.escape(v["thumbnail"], quote=True)}" alt="" loading="lazy" width="480" height="854">
          {f'<span class="topic">{html.escape(v["topic"])}</span>' if v.get("topic") else ''}
        </div>
        <div class="body">
          <h2>{html.escape(v["title"])}</h2>
          <time datetime="{html.escape(v.get("published", ""), quote=True)}">{pretty_date(v.get("published", ""))}</time>
        </div>
      </a>'''
            for v in videos
        )
        listing = f'<div class="grid">\n{cards}\n    </div>'
    else:
        listing = '<p class="empty">No videos yet.</p>'

    # Topics, most-used first, then alphabetical. Hidden until JS confirms the
    # filter works, so a no-JS visitor never sees dead buttons.
    counts: dict[str, int] = {}
    for v in videos:
        if v.get("topic"):
            counts[v["topic"]] = counts.get(v["topic"], 0) + 1
    buttons = [
        f'<button type="button" data-topic="" aria-pressed="true">All '
        f'<span class="count">{len(videos)}</span></button>'
    ]
    for topic, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        buttons.append(
            f'<button type="button" data-topic="{html.escape(topic, quote=True)}" '
            f'aria-pressed="false">{html.escape(topic)} '
            f'<span class="count">{count}</span></button>'
        )
    filters = (
        '<div class="filters" hidden>\n      ' + "\n      ".join(buttons) + "\n    </div>"
        if counts
        else ""
    )

    social = "\n".join(
        f'      <a href="{html.escape(url, quote=True)}" rel="noopener" target="_blank">{name}</a>'
        for name, url in SOCIAL
    )

    ld = json_ld(
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": SITE_NAME,
            "description": SITE_DESCRIPTION,
            **({"url": root + "/"} if root else {}),
        }
    )

    return f"""<!doctype html>
<html lang="en">
<head>
{head(f"{SITE_NAME} — {SITE_TAGLINE}", SITE_DESCRIPTION, f"{root}/" if root else "", f"{root}/assets/logo.png" if root else "", "website")}
<script type="application/ld+json">
{ld}
</script>
</head>
<body>
  <header class="site"><div class="wrap">
    <span class="brand"><img src="assets/icon-64.png" alt="" width="64" height="64">{SITE_NAME}</span>
    <h1>{html.escape(SITE_TAGLINE)}</h1>
    <p class="tagline">{html.escape(SITE_DESCRIPTION)}</p>
    <div class="links">
{social}
    </div>
  </div></header>
  <main><div class="wrap">
    {filters}
    {listing}
  </div></main>
{footer()}
<script>{FILTER_JS}</script>
</body>
</html>
"""


def render_video(video: dict, root: str) -> str:
    vid = video["video_id"]
    title = video["title"]
    description = video.get("description", "")
    canonical = f"{root}/v/{video['slug']}/" if root else ""
    watch = f"https://www.youtube.com/watch?v={vid}"

    payload = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": title,
        "description": summary(description, 480) or title,
        "thumbnailUrl": [video["thumbnail"]],
        "uploadDate": video.get("published", ""),
        "embedUrl": f"https://www.youtube.com/embed/{vid}",
        "contentUrl": watch,
        "publisher": {"@type": "Organization", "name": SITE_NAME},
    }
    if canonical:
        payload["url"] = canonical
    if video.get("duration"):  # ISO 8601, e.g. PT51S — only when known
        payload["duration"] = video["duration"]
    if video.get("topic"):
        payload["genre"] = video["topic"]

    return f"""<!doctype html>
<html lang="en">
<head>
{head(f"{title} — {SITE_NAME}", summary(description) or SITE_TAGLINE, canonical, video["thumbnail"], "video.other", "../../")}
<script type="application/ld+json">
{json_ld(payload)}
</script>
</head>
<body>
  <header class="site"><div class="wrap">
    <a class="brand" href="../../"><img src="../../assets/icon-64.png" alt="" width="64" height="64">{SITE_NAME}</a>
  </div></header>
  <main class="video"><div class="wrap">
    <a class="back" href="../../">&larr; All videos</a>
    <div class="player">
      <iframe src="https://www.youtube-nocookie.com/embed/{html.escape(vid, quote=True)}"
              title="{html.escape(title, quote=True)}"
              loading="lazy" allowfullscreen
              allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              referrerpolicy="strict-origin-when-cross-origin"></iframe>
    </div>
    <h1>{html.escape(title)}</h1>
    <p class="meta">{f'{html.escape(video["topic"])} · ' if video.get("topic") else ''}Published {pretty_date(video.get("published", ""))}</p>
    <a class="watch" href="{html.escape(watch, quote=True)}" rel="noopener" target="_blank">Watch on YouTube</a>
    <div class="body-copy">
{paragraphs(description)}
    </div>
{f'''    <h2>Clarification</h2>
    <blockquote class="note">{linkify(video["correction"])}</blockquote>
    <p class="meta">Also pinned under the video. All clarifications: <a href="../../corrections/">Corrections</a>.</p>''' if video.get("correction") else ""}
  </div></main>
{footer("../../")}
</body>
</html>
"""


def simple_page(slug: str, title: str, description: str, body: str, root: str) -> str:
    """A text page one level below the root, in the imprint's layout."""
    canonical = f"{root}/{slug}/" if root else ""
    return f"""<!doctype html>
<html lang="en">
<head>
{head(f"{title} — {SITE_NAME}", description, canonical, f"{root}/assets/logo.png" if root else "", "website", "../")}
</head>
<body>
  <header class="site"><div class="wrap">
    <a class="brand" href="../"><img src="../assets/icon-64.png" alt="" width="64" height="64">{SITE_NAME}</a>
  </div></header>
  <main class="video"><div class="wrap">
    <a class="back" href="../">&larr; Back to the videos</a>
    <h1>{html.escape(title)}</h1>
{body}
  </div></main>
{footer("../")}
</body>
</html>
"""


def render_method(root: str) -> str:
    body = """    <p>FactReactor makes one-minute Shorts about the things your own body and your
    own senses have been getting wrong your whole life, and the physics behind them.</p>

    <h2>A named source first</h2>
    <p>Each topic starts from a study, a textbook or a measured constant. If a claim
    can't be traced to one, it doesn't go in.</p>

    <h2>Ranges, not drama</h2>
    <p>Numbers that are genuinely contested are given as ranges, not rounded up into
    something more impressive.</p>

    <h2>Sources are public</h2>
    <p>They are listed in every YouTube description and on each video's page here.</p>

    <h2>Corrections stay visible</h2>
    <p>When a video simplifies something or gets it wrong, the clarification goes in
    the pinned comment under the video and on the
    <a href="../corrections/">corrections page</a>.</p>

    <h2>Who does what</h2>
    <p>Every topic and its sources are checked and approved by a person before
    production. Narration and visuals are AI-generated, and every upload is
    labelled as altered or synthetic content.</p>

    <p>Found a better source, or an error? Say so in the comments under the video,
    or write to the address in the <a href="../impressum/">imprint</a>.</p>"""
    return simple_page(
        "how-we-work",
        "How we work",
        f"How {SITE_NAME} videos are researched, sourced and corrected.",
        body,
        root,
    )


def render_corrections(videos: list[dict], root: str) -> str:
    items = "\n".join(
        f'''    <h2><a href="../v/{v["slug"]}/">{html.escape(v["title"])}</a></h2>
    <p class="meta">{pretty_date(v.get("published", ""))}</p>
    <blockquote class="note">{linkify(v["correction"])}</blockquote>'''
        for v in videos
        if v.get("correction")
    )
    body = f"""    <p>A one-minute video has to leave things out. This is what each one simplified
    or had no room for, and where the popular version of a topic gets it wrong,
    newest first. Each one is also the pinned comment under its video.</p>
{items or '    <p class="empty">Nothing here yet.</p>'}"""
    return simple_page(
        "corrections",
        "Corrections and clarifications",
        f"What {SITE_NAME} videos simplified, left out or had to clarify.",
        body,
        root,
    )


def render_imprint(root: str) -> str:
    data = load_imprint()
    canonical = f"{root}/impressum/" if root else ""

    rows = [html.escape(str(data["operator"]).strip())]
    rows += [
        html.escape(str(line).strip())
        for line in data.get("address", [])
        if str(line).strip()
    ]
    for label, key in (("VAT / UID", "vat"), ("Commercial register", "register")):
        if str(data.get(key, "")).strip():
            rows.append(f"{label}: {html.escape(str(data[key]).strip())}")
    block = "<br>\n      ".join(rows)

    responsible = str(data.get("responsible", "")).strip()
    if responsible:
        block += (
            "<br>\n      <br>\n      Responsible for the content: "
            + html.escape(responsible)
        )

    email = str(data.get("email", "")).strip()
    contact = (
        f'<p>Contact: <a href="mailto:{html.escape(email, quote=True)}">'
        f"{html.escape(email)}</a></p>"
        if email
        else ""
    )

    ads = (
        """
    <h2>Advertising</h2>
    <p>This site uses Google AdSense, a service of Google Ireland Limited. Google
    and its partners may set cookies or read device identifiers to select and
    measure ads. You can review and change your ad settings at
    <a href="https://adssettings.google.com/" rel="nofollow noopener" target="_blank">adssettings.google.com</a>
    and read how Google handles the data at
    <a href="https://policies.google.com/technologies/partner-sites" rel="nofollow noopener" target="_blank">policies.google.com/technologies/partner-sites</a>.</p>
"""
        if ADSENSE_CLIENT
        else ""
    )

    analytics = (
        """
    <h2>Analytics</h2>
    <p>This site uses Google Analytics 4 to count visits and see which pages are
    read. Measurement only starts once you have consented; until then no
    analytics data is stored on your device. IP addresses are shortened by Google
    before they are processed.</p>
"""
        if ANALYTICS_ID
        else ""
    )

    consent = (
        """
    <h2>Consent</h2>
    <p>Advertising and analytics are switched off by default. Nothing is stored
    on your device and no identifiers are shared until you agree in the consent
    dialogue, and Google's tags on this site are configured to honour that
    default (Google Consent Mode). You can change or withdraw your decision at
    any time through the privacy settings link in the consent dialogue.</p>
"""
        if ADSENSE_CLIENT or ANALYTICS_ID
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
{head(f"Imprint &amp; Privacy — {SITE_NAME}", f"Legal notice and privacy information for {SITE_NAME}.", canonical, f"{root}/assets/logo.png" if root else "", "website", "../")}
</head>
<body>
  <header class="site"><div class="wrap">
    <a class="brand" href="../"><img src="../assets/icon-64.png" alt="" width="64" height="64">{SITE_NAME}</a>
  </div></header>
  <main class="video"><div class="wrap">
    <a class="back" href="../">&larr; Back to the videos</a>
    <h1>Imprint &amp; Privacy</h1>

    <h2>Operator</h2>
    <p>
      {block}
    </p>
    {contact}

    <h2>Content</h2>
    <p>The videos on this site are produced by {SITE_NAME} and hosted on YouTube.
    Every claim is checked against the cited primary sources, which are listed in
    full on each video page. Should something turn out to be wrong, write to us
    and it will be corrected.</p>

    <h2>Hosting</h2>
    <p>This site is served by GitHub Pages (GitHub, Inc.). GitHub records
    connection data such as your IP address in its server logs to deliver the
    pages and defend against abuse.</p>

    <h2>Embedded videos</h2>
    <p>Videos are embedded through <code>youtube-nocookie.com</code>, YouTube's
    privacy-enhanced mode: no profiling cookie is set before you start playback.
    Loading a page still contacts Google's servers, which receive your IP address,
    and starting a video causes YouTube to store data on your device. YouTube is
    operated by Google Ireland Limited; see
    <a href="https://policies.google.com/privacy" rel="nofollow noopener" target="_blank">policies.google.com/privacy</a>.</p>
{ads}{analytics}{consent}
    <h2>Your rights</h2>
    <p>You can ask what personal data concerning you is processed and request its
    correction or deletion. Use the contact address above.</p>

    <p class="meta">Last updated: {datetime.now(timezone.utc).strftime("%d.%m.%Y")}</p>
  </div></main>
{footer("../")}
</body>
</html>
"""


def render_sitemap(videos: list[dict], root: str) -> str:
    # (url, lastmod). The index carries the newest video's date, so it only
    # changes when something is actually published; the imprint has no
    # meaningful date and gets none rather than a build timestamp that would
    # churn every hour.
    newest = videos[0].get("published", "") if videos else ""
    urls = [(f"{root}/", newest)]
    urls += [(f"{root}/v/{v['slug']}/", v.get("published", "")) for v in videos]
    urls += [(f"{root}/how-we-work/", ""), (f"{root}/corrections/", "")]
    if imprint_is_complete():
        urls.append((f"{root}/impressum/", ""))

    rows = []
    for url, lastmod in urls:
        row = f"  <url><loc>{html.escape(url, quote=True)}</loc>"
        if lastmod:
            row += f"<lastmod>{html.escape(lastmod, quote=True)}</lastmod>"
        rows.append(row + "</url>")
    body = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n</urlset>\n"
    )


# --------------------------------------------------------------------------

def main() -> int:
    root = base_url()

    try:
        entries = parse_feed(fetch_feed())
    except Exception as exc:  # network hiccup must not wipe an existing site
        print(f"error: could not read the channel feed: {exc}", file=sys.stderr)
        state = load_state()
        if not state["videos"]:
            return 1
        print("falling back to the stored archive", file=sys.stderr)
        entries = []

    state = load_state()
    state, added = merge(state, entries)
    save_state(state)

    # Topics come from the playlists, not from a hand-kept list. Skipped only
    # in the offline test mode, where there is no network to read them from.
    if os.environ.get("FEED_FILE") and not os.environ.get("PLAYLIST_FEED_DIR"):
        retagged = 0
        print("offline mode: playlist feeds not read, topics left as they are",
              file=sys.stderr)
    else:
        retagged = sync_topics(discover_topics())

    videos = ordered(state, load_topics())
    corrections = load_corrections()
    for video in videos:
        if video["video_id"] in corrections:
            video["correction"] = corrections[video["video_id"]]

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    (OUT / "index.html").write_text(render_index(videos, root), encoding="utf-8")
    for video in videos:
        page = OUT / "v" / video["slug"]
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text(render_video(video, root), encoding="utf-8")

    for slug, rendered in (
        ("how-we-work", render_method(root)),
        ("corrections", render_corrections(videos, root)),
    ):
        page = OUT / slug
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text(rendered, encoding="utf-8")

    if imprint_is_complete():
        page = OUT / "impressum"
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text(render_imprint(root), encoding="utf-8")
    else:
        print(
            "warning: data/imprint.json is incomplete — no imprint page was built "
            "and the footer link is omitted. Fill in 'operator' plus 'email' or "
            "'address'.",
            file=sys.stderr,
        )

    assets = ROOT / "assets"
    if assets.is_dir():
        shutil.copytree(assets, OUT / "assets")
    else:
        print("warning: assets/ is missing — logo and favicon will 404", file=sys.stderr)

    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    # The custom domain must travel inside the artifact. With Actions-based
    # Pages deployment nothing else carries it, so a CNAME left only in the
    # repo root gets dropped and the domain falls back to github.io.
    domain = custom_domain()
    if domain:
        (OUT / "CNAME").write_text(domain + "\n", encoding="utf-8")

    # ads.txt has to be served from the site root or AdSense treats the
    # inventory as unauthorised and pays out less. Like CNAME it only reaches
    # the published site by riding along in the artifact.
    ads_txt = ROOT / "ads.txt"
    if ads_txt.exists():
        content = ads_txt.read_text(encoding="utf-8")
        (OUT / "ads.txt").write_text(content, encoding="utf-8")
        # A mismatch here is silent and costs revenue, so fail loudly instead.
        if ADSENSE_CLIENT:
            publisher = ADSENSE_CLIENT.removeprefix("ca-")
            if publisher not in content:
                print(
                    f"warning: ads.txt does not mention {publisher}, which is the "
                    f"publisher id in ADSENSE_CLIENT ({ADSENSE_CLIENT})",
                    file=sys.stderr,
                )
    if root:
        (OUT / "sitemap.xml").write_text(render_sitemap(videos, root), encoding="utf-8")
        (OUT / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: {root}/sitemap.xml\n", encoding="utf-8"
        )

    tagged = sum(1 for v in videos if v["topic"])
    print(
        f"{len(videos)} Videos, {added} neu · {tagged} mit Thema, {retagged} "
        f"neu zugeordnet · Basis-URL: {root or '(nicht gesetzt)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
