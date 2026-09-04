"""Google Places API (New) — Text Search client + business normalization."""

import re
import time
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any
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

# ── API field masks ────────────────────────────────────────────────────

SEARCH_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.location", "places.rating", "places.userRatingCount",
    "places.websiteUri", "places.businessStatus", "places.types",
    "places.primaryType", "nextPageToken",
])

DETAIL_FIELDS = ",".join([
    "id", "displayName", "formattedAddress", "location", "rating",
    "userRatingCount", "websiteUri", "businessStatus", "types",
    "primaryType", "internationalPhoneNumber", "nationalPhoneNumber",
    "regularOpeningHours", "editorialSummary",
])

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


# ── Google Maps client ────────────────────────────────────────────────

class MapsClient:
    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    DETAIL_URL = "https://places.googleapis.com/v1/places"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        retry = Retry(total=4, backoff_factor=1.2,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["POST", "GET"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._last = 0.0

    def _throttle(self):
        gap = time.monotonic() - self._last
        if gap < 0.12:
            time.sleep(0.12 - gap)
        self._last = time.monotonic()

    def search(self, query: str, page_token: str | None = None,
               page_size: int = 20) -> dict[str, Any]:
        self._throttle()
        body: dict[str, Any] = {
            "textQuery": query,
            "pageSize": page_size,
            "languageCode": "pt-BR",
        }
        if page_token:
            body["pageToken"] = page_token
        r = self.session.post(
            self.SEARCH_URL, json=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": SEARCH_FIELDS,
            },
            timeout=25,
        )
        r.raise_for_status()
        return r.json()

    def details(self, place_id: str) -> dict[str, Any] | None:
        self._throttle()
        try:
            r = self.session.get(
                f"{self.DETAIL_URL}/{place_id}",
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": DETAIL_FIELDS,
                },
                timeout=20,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None


# ── Raw → Business conversion ─────────────────────────────────────────

def to_business(raw: dict, query: str, details: dict | None = None) -> Business:
    addr = raw.get("formattedAddress", "")
    p = parse_address(addr)
    loc = raw.get("location", {})
    disp = raw.get("displayName", {})
    name = disp.get("text", "") if isinstance(disp, dict) else str(disp)
    website = raw.get("websiteUri", "")
    types = raw.get("types", [])
    primary = raw.get("primaryType", "")

    phone, hours, desc = "", "", ""
    if details:
        phone = details.get("internationalPhoneNumber", "") or details.get("nationalPhoneNumber", "")
        oh = details.get("regularOpeningHours")
        if oh and isinstance(oh, dict):
            hours = "; ".join(oh.get("weekdayDescriptions", [])[:7])
        es = details.get("editorialSummary")
        if es and isinstance(es, dict):
            desc = es.get("overview", "")

    pid = raw.get("id", "")
    return Business(
        name=name, address=addr, city=p["city"], state=p["state"],
        country=p["country"], phone=phone, website=website or "",
        maps_url=f"https://maps.google.com/?cid={pid}" if pid else "",
        place_id=pid,
        latitude=loc.get("latitude", 0.0), longitude=loc.get("longitude", 0.0),
        rating=raw.get("rating", 0.0) or 0.0,
        reviews=raw.get("userRatingCount", 0) or 0,
        hours=hours, categories=", ".join(types) if types else primary,
        description=desc, has_website=has_own_website(website),
        source_query=query,
        collected_at=datetime.now(timezone.utc).isoformat(),
    )
