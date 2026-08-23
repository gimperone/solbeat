"""Keyless news collector: RSS/Atom feeds parsed namespace-agnostically,
filtered by Solana-related keywords, deduplicated via the store."""
import xml.etree.ElementTree as ET

from core.util import http_request, utcnow


def _parse_date(s: str | None) -> float:
    if not s:
        return 0
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []
    # find all <item> or <entry> regardless of namespaces
    for el in root.iter():
        ln = _localname(el.tag)
        if ln in ("item", "entry"):
            title = link = pub = summary = ""
            for ch in el.iter():
                cln = _localname(ch.tag)
                if cln == "title" and not title:
                    title = (ch.text or "").strip()
                elif cln == "link" and not link:
                    link = (ch.get("href") or (ch.text or "")).strip()
                elif cln in ("pubdate", "published", "updated", "date") and not pub:
                    pub = (ch.text or "").strip()
                elif cln in ("description", "summary", "content") and not summary:
                    summary = (ch.text or "").strip()[:300]
            if title and link:
                items.append({
                    "title": title,
                    "link": link.split("?")[0],
                    "published": _parse_date(pub),
                    "summary": summary,
                })
    return items


def collect_news(cfg: dict) -> dict:
    ncfg = cfg.get("news", {})
    feeds = ncfg.get("feeds", [])
    keywords = [k.lower() for k in ncfg.get("keyword_filter", ["solana"])]
    max_items = int(ncfg.get("max_items", 30))

    matched: dict[str, dict] = {}
    for feed in feeds:
        fname = feed.get("name", "?")
        try:
            xml_bytes = http_request(feed["url"], timeout=20)
            for it in parse_feed(xml_bytes):
                hay = (it["title"] + " " + it["summary"]).lower()
                if any(k in hay for k in keywords):
                    it["source"] = fname
                    matched.setdefault(it["link"], it)
        except Exception:
            continue  # per-feed isolation

    items = sorted(matched.values(), key=lambda x: x["published"], reverse=True)[:max_items]
    return {"items": items}


def attach_store(store):
    """Wrap collect to dedupe against store and persist."""
    def wrapped(cfg: dict) -> dict:
        res = collect_news(cfg)
        fresh = store.filter_new_links(res["items"])
        store.save_news(fresh)
        res["new_count"] = len(fresh)
        return res
    return wrapped
