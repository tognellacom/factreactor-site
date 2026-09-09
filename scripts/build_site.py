#!/usr/bin/env python3
"""Build the FactReactor site from the public YouTube channel feed.

Reads the channel's Atom feed, merges it into data/videos.json (the feed only
ever carries the newest 15 entries, so the archive has to persist here), and
renders a static site into _site/.

Standard library only, no dependencies.

Environment:
  CHANNEL_ID  YouTube channel id (default: the FactReactor channel)
  BASE_URL    Absolute site root, e.g. https://factreactor.com. Falls back to
              the GitHub Pages URL derived from GITHUB_REPOSITORY.
  FEED_FILE   Read the feed from this file instead of the network (for tests).
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

SITE_NAME = "FactReactor"
SITE_TAGLINE = "One genuine aha moment in under a minute."
SITE_DESCRIPTION = (
    "Short science videos about things you were wrong about — your own body, "
    "the physics you live in, the systems running without your knowledge. "
    "Every claim checked against the original research."
)
CHANNEL_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
SOCIAL = [
    ("YouTube", CHANNEL_URL),
    ("TikTok", "https://www.tiktok.com/@factreactor_hq"),
    ("Instagram", "https://www.instagram.com/factreactor_hq"),
]

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
STATE_PATH = ROOT / "data" / "videos.json"
TOPICS_PATH = ROOT / "data" / "topics.json"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

URL_RE = re.compile(r"https?://[^\s<>\"']+")


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------

def base_url() -> str:
    explicit = os.environ.get("BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
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
    """video_id -> topic. Hand-maintained; keys starting with _ are comments."""
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
  display: inline-block; font-size: 13px; letter-spacing: .22em;
  text-transform: uppercase; color: var(--gold); text-decoration: none; font-weight: 700;
}
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
"""


def head(title: str, description: str, canonical: str, image: str, og_type: str) -> str:
    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
        f'<meta name="description" content="{html.escape(description, quote=True)}">',
        '<meta name="theme-color" content="#07070c">',
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
    tags.append(f"<style>{CSS}</style>")
    return "\n".join(tags)


def footer() -> str:
    links = " · ".join(
        f'<a href="{html.escape(url, quote=True)}" rel="noopener" target="_blank">{name}</a>'
        for name, url in SOCIAL
    )
    return (
        '<footer class="site"><div class="wrap">'
        f"{links}<br>© {datetime.now(timezone.utc).year} {SITE_NAME}"
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
        listing = '<p class="empty">Noch keine Videos erfasst.</p>'

    # Topics, most-used first, then alphabetical. Hidden until JS confirms the
    # filter works, so a no-JS visitor never sees dead buttons.
    counts: dict[str, int] = {}
    for v in videos:
        if v.get("topic"):
            counts[v["topic"]] = counts.get(v["topic"], 0) + 1
    buttons = [
        f'<button type="button" data-topic="" aria-pressed="true">Alle '
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
{head(f"{SITE_NAME} — {SITE_TAGLINE}", SITE_DESCRIPTION, f"{root}/" if root else "", "", "website")}
<script type="application/ld+json">
{ld}
</script>
</head>
<body>
  <header class="site"><div class="wrap">
    <span class="brand">{SITE_NAME}</span>
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
{head(f"{title} — {SITE_NAME}", summary(description) or SITE_TAGLINE, canonical, video["thumbnail"], "video.other")}
<script type="application/ld+json">
{json_ld(payload)}
</script>
</head>
<body>
  <header class="site"><div class="wrap">
    <a class="brand" href="../../">{SITE_NAME}</a>
  </div></header>
  <main class="video"><div class="wrap">
    <a class="back" href="../../">&larr; Alle Videos</a>
    <div class="player">
      <iframe src="https://www.youtube-nocookie.com/embed/{html.escape(vid, quote=True)}"
              title="{html.escape(title, quote=True)}"
              loading="lazy" allowfullscreen
              allow="accelerometer; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
              referrerpolicy="strict-origin-when-cross-origin"></iframe>
    </div>
    <h1>{html.escape(title)}</h1>
    <p class="meta">{f'{html.escape(video["topic"])} · ' if video.get("topic") else ''}Veröffentlicht am {pretty_date(video.get("published", ""))}</p>
    <a class="watch" href="{html.escape(watch, quote=True)}" rel="noopener" target="_blank">Auf YouTube ansehen</a>
    <div class="body-copy">
{paragraphs(description)}
    </div>
  </div></main>
{footer()}
</body>
</html>
"""


def render_sitemap(videos: list[dict], root: str) -> str:
    urls = [f"{root}/"] + [f"{root}/v/{v['slug']}/" for v in videos]
    body = "\n".join(f"  <url><loc>{html.escape(u, quote=True)}</loc></url>" for u in urls)
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

    videos = ordered(state, load_topics())

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    (OUT / "index.html").write_text(render_index(videos, root), encoding="utf-8")
    for video in videos:
        page = OUT / "v" / video["slug"]
        page.mkdir(parents=True, exist_ok=True)
        (page / "index.html").write_text(render_video(video, root), encoding="utf-8")

    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    if root:
        (OUT / "sitemap.xml").write_text(render_sitemap(videos, root), encoding="utf-8")
        (OUT / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: {root}/sitemap.xml\n", encoding="utf-8"
        )

    print(f"{len(videos)} Videos, {added} neu · Basis-URL: {root or '(nicht gesetzt)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
