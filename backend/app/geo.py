"""Where is the user? -> the BPS province domain (e.g. Lampung -> 1800), used as the default search region.

Two sources, best first:
  1. browser coordinates (navigator.geolocation, only with the user's permission), reverse geocoded with
     OpenStreetMap Nominatim -> ISO 3166-2 province code (ID-LA);
  2. the public IP address (ip-api.com). Coarse: mobile / ISP networks often resolve to Jakarta.
"""

from __future__ import annotations

import ipaddress
import time

import httpx

UA = {"User-Agent": "tabel-dari-pdfs/1.0 (+localhost)"}

# ISO 3166-2:ID province code -> BPS domain. New Papua provinces map to the domain they were split from
# (the BPS Web API lists only 9100 Papua Barat and 9400 Papua).
ISO_TO_BPS = {
    "AC": "1100", "SU": "1200", "SB": "1300", "RI": "1400", "JA": "1500", "SS": "1600", "BE": "1700", "LA": "1800",
    "BB": "1900", "KR": "2100", "JK": "3100", "JB": "3200", "JT": "3300", "YO": "3400", "JI": "3500", "BT": "3600",
    "BA": "5100", "NB": "5200", "NT": "5300", "KB": "6100", "KT": "6200", "KS": "6300", "KI": "6400", "KU": "6500",
    "SA": "7100", "ST": "7200", "SN": "7300", "SG": "7400", "GO": "7500", "SR": "7600", "MA": "8100", "MU": "8200",
    "PB": "9100", "PD": "9100", "PA": "9400", "PE": "9400", "PS": "9400", "PT": "9400",
}

_cache: dict[str, tuple[float, dict]] = {}
TTL = 6 * 3600


def _client() -> httpx.Client:
    return httpx.Client(timeout=8, headers=UA, follow_redirects=True)


def _result(domain: str | None, region: str, source: str, country: str = "ID") -> dict:
    return {"domain": domain, "region": region, "country": country, "source": source}


def from_coords(lat: float, lon: float) -> dict:
    key = f"c:{lat:.2f},{lon:.2f}"
    if key in _cache and time.time() - _cache[key][0] < TTL:
        return _cache[key][1]
    with _client() as c:
        r = c.get("https://nominatim.openstreetmap.org/reverse",
                  params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 5, "accept-language": "id"})
    r.raise_for_status()
    addr = (r.json() or {}).get("address") or {}
    iso = (addr.get("ISO3166-2-lvl4") or "").removeprefix("ID-")
    country = (addr.get("country_code") or "").upper()
    res = _result(ISO_TO_BPS.get(iso) if country == "ID" else None, addr.get("state") or "", "location", country)
    _cache[key] = (time.time(), res)
    return res


def _public(ip: str | None) -> bool:
    try:
        return bool(ip) and ipaddress.ip_address(ip).is_global  # type: ignore[arg-type]
    except ValueError:
        return False


def from_ip(client_ip: str | None) -> dict:
    """The client's public IP; a local / LAN client is on this machine's network, so its public IP is ours."""
    ip = client_ip if _public(client_ip) else ""
    key = f"ip:{ip}"
    if key in _cache and time.time() - _cache[key][0] < TTL:
        return _cache[key][1]
    with _client() as c:
        r = c.get(f"http://ip-api.com/json/{ip}", params={"fields": "status,countryCode,region,regionName"})
    r.raise_for_status()
    j = r.json() or {}
    if j.get("status") != "success":
        raise RuntimeError("IP location not available")
    country = j.get("countryCode") or ""
    res = _result(ISO_TO_BPS.get(j.get("region") or "") if country == "ID" else None, j.get("regionName") or "",
                  "ip", country)
    _cache[key] = (time.time(), res)
    return res
