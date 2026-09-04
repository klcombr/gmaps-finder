"""SerpAPI Google Maps client + business normalization."""

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Social domains (not real websites) ──────────────────────────────────

SOCIAL = frozenset({
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "instagram.com", "www.instagram.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "linkedin.com", "www.linkedin.com",
    "wa.me", "api.whatsapp.com", "chat.whatsapp.com",
    "maps.google.com", "goo.gl",
    "tiktok.com", "www.tiktok.com",
    "youtube.com", "www.youtube.com",
    "pinterest.com", "www.pinterest.com",
    "threads.net", "www.threads.net",
})

# ── Regex helpers ──────────────────────────────────────────────────────

_SUFFIXES = re.compile(r"\b(ltda|mei?|eireli|s\.?a\.?|epp|ss)\b", re.I)
_MULTI_SP = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_STATE_RE = re.compile(r"[A-Z]{2}")


# ── Business dataclass ────────────────────────────────────────────────

@dataclass
class Business:
    name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    phone: str = ""
    website: str = ""
    maps_url: str = ""
    place_id: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    rating: float = 0.0
    reviews: int = 0
    hours: str = ""
    categories: str = ""
    description: str = ""
    has_website: str = "unknown"
    source_query: str = ""
    collected_at: str = ""


# ── Normalization ──────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name)
    n = n.encode("ascii", "ignore").decode("ascii")
    n = _SUFFIXES.sub("", n)
    n = _NON_ALNUM.sub(" ", n.lower())
    n = _MULTI_SP.sub(" ", n).strip()
    return n if len(n) >= 3 else name.strip().lower()


def parse_address(formatted: str) -> dict[str, str]:
    parts = [p.strip() for p in formatted.split(",")]
    r: dict[str, str] = {"address": formatted, "city": "", "state": "", "country": ""}
    if not parts:
        return r
    r["country"] = parts[-1].strip()
    if len(parts) < 2:
        return r
    sl = parts[-2].strip()
    m = re.match(r"^(.+?)\s*[-–]\s*([A-Z]{2})$", sl)
    if m:
        r["city"], r["state"] = m.group(1).strip(), m.group(2).strip()
        return r
    if _STATE_RE.fullmatch(sl):
        r["state"] = sl
        if len(parts) >= 3:
            r["city"] = parts[-3].strip()
    else:
        tokens = sl.split()
        if len(tokens) >= 2 and _STATE_RE.fullmatch(tokens[-1]):
            r["city"], r["state"] = " ".join(tokens[:-1]), tokens[-1]
        else:
            r["city"] = sl
    return r


def build_fingerprint(name: str, address: str, phone: str) -> str:
    parts = [normalize_name(name)]
    if address:
        na = re.sub(r"[^a-z0-9\s]", " ", address.lower())
        parts.append(_MULTI_SP.sub(" ", na).strip())
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 8:
            parts.append(digits[-8:])
    return "|".join(parts)


def has_own_website(url: str | None) -> str:
    if not url or not url.strip():
        return "false"
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return "unknown"
    for d in SOCIAL:
        if host == d or host.endswith("." + d):
            return "false"
    return "true"


# ── SerpAPI client ────────────────────────────────────────────────────

class SerpClient:
    """SerpAPI Google Maps search — no Google billing needed."""

    ENDPOINT = "https://serpapi.com/search.json"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        retry = Retry(total=4, backoff_factor=1.2,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._last = 0.0

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < 1.0:
            time.sleep(1.0 - gap)
        self._last = time.monotonic()

    def search(self, query: str, start: int = 0) -> dict:
        self._throttle()
        params = {
            "engine": "google_maps",
            "q": query,
            "api_key": self.api_key,
            "start": start,
            "hl": "pt-br",
        }
        r = self.session.get(self.ENDPOINT, params=params, timeout=30)
        r.raise_for_status()
        return r.json()


# ── SerpAPI response → Business ───────────────────────────────────────

def to_business(raw: dict, query: str) -> Business:
    addr = raw.get("address", "")
    p = parse_address(addr)
    gps = raw.get("gps_coordinates", {})
    types = raw.get("types", [])
    primary = raw.get("type", "")

    # Operating hours → string
    oh = raw.get("operating_hours", {})
    hours = ""
    if oh and isinstance(oh, dict):
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        parts = []
        for d in days:
            if d in oh:
                pt = {"monday": "Seg", "tuesday": "Ter", "wednesday": "Qua",
                      "thursday": "Qui", "friday": "Sex", "saturday": "Sab", "sunday": "Dom"}
                parts.append(f"{pt.get(d, d[:3])}: {oh[d]}")
        hours = "; ".join(parts)

    website = raw.get("website", "") or ""
    pid = raw.get("place_id", "")

    return Business(
        name=raw.get("title", ""),
        address=addr,
        city=p["city"],
        state=p["state"],
        country=p.get("country", "") or raw.get("country", ""),
        phone=raw.get("phone", "") or "",
        website=website,
        maps_url=f"https://maps.google.com/?cid={raw.get('data_cid', pid)}" if pid else "",
        place_id=pid,
        latitude=gps.get("latitude", 0.0) or 0.0,
        longitude=gps.get("longitude", 0.0) or 0.0,
        rating=raw.get("rating", 0.0) or 0.0,
        reviews=raw.get("reviews", 0) or 0,
        hours=hours,
        categories=", ".join(types) if types else primary,
        description=raw.get("description", "") or "",
        has_website=has_own_website(website),
        source_query=query,
        collected_at=datetime.now(timezone.utc).isoformat(),
    )
