#!/usr/bin/env python3
"""Collecte les événements tech francophones et exporte un GeoJSON RFC 7946."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from langdetect import DetectorFactory, LangDetectException, detect
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "public/events.geojson"
AFUP = "afup_lettre_ouverte"
TODAY = dt.datetime.now(dt.timezone.utc).date().isoformat()
DetectorFactory.seed = 0

COUNTRIES = {
    "FR": (
        "France",
        "Paris|Lyon|Nantes|Bordeaux|Toulouse|Lille|Rennes|Montpellier|Grenoble|Strasbourg|"
        "Marseille|Aix-en-Provence|Clermont-Ferrand|Tours|Caen|Brest|Nice|Valbonne|Rouen|"
        "Dijon|Metz|Nancy|Pau|Poitiers|Angers|Orléans|Annecy|Avignon|Reims|Limoges|Niort|"
        "La Rochelle|Vannes|Lorient|Quimper|Saint-Étienne|Chambéry|Valence|Besançon|Mulhouse|"
        "Amiens|Le Mans|Le Havre|Nîmes|Perpignan|Toulon|Bayonne|Biarritz|Bidart|Laval|Blois|"
        "Bourges|Troyes|Arras|Valenciennes|Roubaix|Villeurbanne|Nanterre|Châtillon|Balma|Échirolles",
    ),
    "BE": ("Belgique", "Bruxelles|Liège|Namur|Louvain-la-Neuve|Mons|Charleroi"),
    "CH": ("Suisse", "Genève|Lausanne|Neuchâtel|Fribourg|Sion|Yverdon-les-Bains"),
    "LU": ("Luxembourg", "Luxembourg|Esch-sur-Alzette|Mondorf-les-Bains"),
    "CA": ("Canada", "Montréal|Québec|Sherbrooke|Gatineau|Trois-Rivières"),
    "SN": ("Sénégal", "Dakar"),
    "CI": ("Côte d’Ivoire", "Abidjan|Yamoussoukro"),
    "MA": ("Maroc", "Casablanca|Rabat|Marrakech|Tanger|Fès|Kénitra|El Jadida|Agadir"),
    "TN": ("Tunisie", "Tunis|Sfax|Sousse"),
    "CM": ("Cameroun", "Yaoundé|Douala"),
    "MG": ("Madagascar", "Antananarivo|Antsirabe"),
}

TECH = re.compile(
    r"\b(?:dev|tech|code|coding|software|logiciel\w*|informatique|programmation|num[eé]rique|"
    r"ia|ai|llm|rag|ml|agent\w*|data|cloud|devops|linux|open[ -]?source|rust|python|php|"
    r"symfony|laravel|drupal|wordpress|prestashop|magento|sylius|odoo|javascript|typescript|"
    r"react|vue|svelte|angular|node|java|kotlin|golang|swift|ios|android|flutter|mobile|"
    r"cyber\w*|infosec|s[eé]curit[eé]|pentest|hacking|agile|scrum|craft\w*|api|kubernetes|"
    r"docker|serverless|aws|azure|gcp|snowflake|dbt|airflow|web|ux|ui|a11y|accessibilit[eé]|"
    r"produit|product|qa|test\w*|hackathon|afup|devfest|gdg|jug|mug|postgres\w*|mysql|sql|"
    r"iot|embarqu[eé]|embedded|[eé]lectronique|electronics|hardware|xr|vr)\b",
    re.I,
)


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)).strip()


CITIES = {norm(city): (city, code) for code, (_, names) in COUNTRIES.items() for city in names.split("|")}
CITY_ALIASES = {
    "brussels": CITIES["bruxelles"],
    "geneva": CITIES["geneve"],
    "sophia antipolis": CITIES["valbonne"],
    "sophia-antipolis": CITIES["valbonne"],
    "la defense": CITIES["nanterre"],
    "paris la defense": CITIES["nanterre"],
    "loire valley": CITIES["blois"],
}
CITIES.update(CITY_ALIASES)

METRO_AREAS = {
    "nanterre": "paris", "chatillon": "paris", "issy-les-moulineaux": "paris",
    "boulogne-billancourt": "paris", "saint-denis": "paris", "montreuil": "paris",
    "courbevoie": "paris", "puteaux": "paris", "levallois-perret": "paris",
    "villeurbanne": "lyon",
    "roubaix": "lille", "villeneuve-d'ascq": "lille", "marcq-en-baroeul": "lille",
    "balma": "toulouse", "labege": "toulouse", "blagnac": "toulouse",
    "merignac": "bordeaux", "pessac": "bordeaux", "talence": "bordeaux",
    "echirolles": "grenoble", "meylan": "grenoble", "saint-martin-d'heres": "grenoble",
    "saint-herblain": "nantes", "reze": "nantes",
    "cesson-sevigne": "rennes",
    "valbonne": "nice",
}

CITY_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(CITIES, key=len, reverse=True))) + r")(?!\w)")
COUNTRY_CODES = {norm(name): code for code, (name, _) in COUNTRIES.items()}
COUNTRY_CODES.update({norm(code): code for code in COUNTRIES})
COUNTRY_CODES.update({
    "cote d'ivoire": "CI", "ivory coast": "CI", "belgium": "BE", "switzerland": "CH",
    "morocco": "MA", "tunisia": "TN", "senegal": "SN", "cameroon": "CM",
})
TLD_COUNTRIES = {"fr": "FR", "be": "BE", "ch": "CH", "lu": "LU", "ca": "CA", "sn": "SN", "ci": "CI", "ma": "MA", "tn": "TN", "cm": "CM", "mg": "MG"}
ONLINE = {"online", "en ligne", "virtual", "remote", "distanciel"}
ONLINE_TITLE = re.compile(r"(?:\[\s*(?:visio|en ligne|online|distanciel|remote|webinaire|webinar)\s*\]|\b(?:en visio|webinaire)\b)", re.I)
AGENDA = re.compile(r"\b(?:agenda|calendrier|calendar|event(?:s)?|evenement(?:s)?|meetup(?:s)?|conference(?:s)?|prochain(?:e|es|s)?)\b")
NATIONAL_HINT = re.compile(r"\b(?:france|francophone|francophonie|national\w*)\b", re.I)

AGGREGATOR_HOSTS = {"dev.events", "developers.events"}
SHARED_HOSTS = AGGREGATOR_HOSTS | {
    "meetup.com", "luma.com", "lu.ma", "eventbrite.fr", "eventbrite.com", "billetweb.fr",
    "helloasso.com", "gdg.community.dev", "sessionize.com", "pretalx.com", "kommunity.com", "mobilizon.fr",
}


@dataclass
class Event:
    title: str
    url: str
    start: str | None
    end: str | None
    city: str
    code: str
    online: bool = False
    sources: list[str] = field(default_factory=list)
    extra_urls: list[str] = field(default_factory=list)


def string(value):
    return value.strip() if isinstance(value, str) else ""


def french(text, language=""):
    if language:
        return language.lower().replace("_", "-").split("-")[0] in {"fr", "fra", "fre", "french", "français"}
    cleaned = re.sub(r"https?://\S+", "", BeautifulSoup(text, "lxml").get_text(" ", strip=True))
    try:
        return len(cleaned) >= 12 and detect(cleaned) == "fr"
    except LangDetectException:
        return False


def url_for(value, base):
    try:
        url = urljoin(base, string(value))
        parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return parsed._replace(fragment="").geturl()
    except ValueError:
        pass
    return ""


def host_of(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def dedicated_host(url):
    host = host_of(url)
    if not host or any(host == domain or host.endswith("." + domain) for domain in SHARED_HOSTS):
        return ""
    return host


def url_rank(url):
    host = host_of(url)
    if not host:
        return 0
    if any(host == domain or host.endswith("." + domain) for domain in AGGREGATOR_HOSTS):
        return 1
    if any(host == domain or host.endswith("." + domain) for domain in SHARED_HOSTS):
        return 2
    return 3


def metro_city(city, code=""):
    normalized = norm(city)
    if not normalized:
        return ""
    canonical_norm = norm(CITIES.get(normalized, (city, code))[0])
    return METRO_AREAS.get(canonical_norm, METRO_AREAS.get(normalized, canonical_norm))


def clean_title(title, strip_parens=False):
    cleaned = re.sub(r"\[[^\]]*\]", " ", title)
    if strip_parens:
        cleaned = re.sub(r"\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"(?<!\w)(?:19|20)\d{2}(?!\w)|'\d{2}(?!\w)", " ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", norm(cleaned))
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_city_tokens(cleaned_title, city):
    if not city:
        return cleaned_title
    tokens = {norm(city), metro_city(city)}
    for alias, (canonical, _) in CITY_ALIASES.items():
        if norm(canonical) in tokens:
            tokens.add(alias)
    pattern = r"\b(?:" + "|".join(map(re.escape, sorted(filter(None, tokens), key=len, reverse=True))) + r")\b"
    stripped = re.sub(r"\s+", " ", re.sub(pattern, " ", cleaned_title)).strip()
    return stripped if len(stripped) >= 5 else cleaned_title


def clean_afup_label(link):
    clone = BeautifulSoup(str(link), "lxml")
    for badge in clone.select(".sig__badge, .sig__new, .badge, sup"):
        if norm(badge.get_text(" ", strip=True)) in {"new", "nouveau"}:
            badge.decompose()
    raw = clone.get_text(" ", strip=True)
    return re.sub(r"^(?:new|nouveau)\s+", "", raw).strip()


def date(value):
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
        return dt.date.fromisoformat(string(value)[:10]).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def scripts(soup, **attrs):
    for tag in soup.find_all("script", attrs=attrs):
        try:
            yield json.loads(tag.get_text())
        except ValueError:
            continue


def types(node):
    value = node.get("@type", [])
    return {string(item).rsplit("/", 1)[-1] for item in (value if isinstance(value, list) else [value])}


def schema_events(soup):
    for payload in scripts(soup, type="application/ld+json"):
        for node in nodes(payload):
            if any(name.endswith("Event") for name in types(node)) and node.get("name"):
                yield node


def in_scope(city, code, text, online=False, language=""):
    if online:
        return french(text, language)
    return code in COUNTRIES and (code not in {"BE", "CH", "CA"} or CITIES.get(norm(city), ("", ""))[1] == code or french(text, language))


def parse_schema(node, base, source, city="", code="", *, tech_and_french=False, require_tech=False, multilingual_french=False):
    title = string(node.get("name"))
    description = string(node.get("description"))
    text = f"{title} {description}"
    language = node.get("inLanguage")
    if isinstance(language, dict):
        language = language.get("alternateName") or language.get("name")
    language = string(language)
    mode = string(node.get("eventAttendanceMode"))
    places = node.get("location") or []
    places = places if isinstance(places, list) else [places]
    physical = [p for p in places if isinstance(p, dict) and "VirtualLocation" not in types(p)]
    explicit_online = bool(ONLINE_TITLE.search(title))
    online = (explicit_online or "OnlineEventAttendanceMode" in mode or (
        not physical and any(isinstance(p, dict) and "VirtualLocation" in types(p) for p in places)
    )) and "MixedEventAttendanceMode" not in mode
    address = next((p["address"] for p in physical if isinstance(p.get("address"), dict)), {})
    city = string(address.get("addressLocality")) or city
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("identifier") or country.get("name")
    country_str = string(country)
    code = COUNTRY_CODES.get(norm(country_str), code if not country_str else "")
    city, inferred_code = CITIES.get(norm(city), (city, ""))
    code = code or inferred_code
    start = date(node.get("startDate"))
    end = date(node.get("endDate")) or start
    if not title or not start or end < TODAY or string(node.get("eventStatus")).endswith(("EventCancelled", "EventPostponed")):
        return None
    if not online and country_str and not code:
        return None
    url = url_for(node.get("url") or node.get("@id"), base) or base
    if require_tech and not TECH.search(f"{text} {url}"):
        return None
    if tech_and_french and not (TECH.search(text) and french(text, language)):
        return None
    if multilingual_french and (online or code in {"BE", "CH", "CA"}) and not french(text, "" if online else language):
        return None
    if source != AFUP and not in_scope(city, code, text, online, "" if online and source == "meetup" else language):
        return None
    return Event(title, url, start, end, "En ligne" if online else city, "" if online else code, online, [source])


def get(client, url, **kwargs):
    try:
        response = client.get(url, **kwargs)
        response.raise_for_status()
        return response
    except httpx.HTTPError as exc:
        logging.warning("%s · %s", url, exc)
        return None


def developers(client):
    url = "https://developers.events/all-events.json"
    response = get(client, url)
    if response is None:
        return
    for item in response.json():
        if not isinstance(item, dict):
            continue
        title = string(item.get("name"))
        dates = sorted(day for value in item.get("date", []) or [] if (day := date(value)))
        if not title or not dates or dates[-1] < TODAY:
            continue
        city = string(item.get("city"))
        country = norm(string(item.get("country")))
        online = country in ONLINE or norm(city) in ONLINE or bool(ONLINE_TITLE.search(title))
        canonical, inferred = CITIES.get(norm(city), (city, ""))
        code = COUNTRY_CODES.get(country, inferred if not country else "")
        languages = [string(tag.get("value")) for tag in item.get("tags", [])
                     if isinstance(tag, dict) and tag.get("key") == "language"]
        language = "fr" if any(french("", lang) for lang in languages if lang) else next(iter(languages), "")
        if item.get("status") in {"cancelled", "canceled", "postponed"}:
            continue
        if in_scope(canonical, code, f'{title} {string(item.get("description"))}', online, language):
            yield Event(title, url_for(item.get("hyperlink"), url) or url, dates[0], dates[-1],
                        "En ligne" if online else canonical, "" if online else code, online, ["developers.events"])


def afup_agenda_links(soup, base):
    origin = host_of(base)
    seen = {urlsplit(base).path.rstrip("/")}
    for link in soup.select("a[href]"):
        url = url_for(link["href"], base)
        target = urlsplit(url)
        path = target.path.rstrip("/")
        if not path or path in seen or host_of(url) != origin:
            continue
        label = norm(link.get_text(" ", strip=True))
        if AGENDA.search(label) or AGENDA.search(norm(path).replace("_", "-")):
            seen.add(path)
            yield url


def infer_afup_location(title, url, head_metadata, html=None):
    inferred = CITY_PATTERN.search(norm(" ".join(head_metadata)))
    if inferred:
        return CITIES[inferred.group()]
    host_stem = host_of(url).split(".")[0]
    compact = norm(f"{title} {host_stem}")
    for city_norm in sorted(CITIES, key=len, reverse=True):
        if len(city_norm) >= 4 and city_norm in compact and city_norm not in {"mans", "pau", "nice"}:
            return CITIES[city_norm]
    if html is not None and not NATIONAL_HINT.search(title):
        snippets = [tag.get_text(" ", strip=True) for tag in html.select("h1, h2, h3, address, footer, [class*='lieu'], [class*='location'], [class*='venue']")]
        found = {CITIES[m.group()][0]: CITIES[m.group()] for m in CITY_PATTERN.finditer(norm(" ".join(snippets)))}
        if len(found) == 1:
            return next(iter(found.values()))
    for name_norm, country_code in COUNTRY_CODES.items():
        if len(name_norm) >= 4 and re.search(rf"(?<!\w){re.escape(name_norm)}(?!\w)", norm(title)):
            return "", country_code
    tld = host_of(url).rsplit(".", 1)[-1]
    if tld in TLD_COUNTRIES and (tld != "fr" or NATIONAL_HINT.search(title)):
        return "", TLD_COUNTRIES[tld]
    return "", ""


def _fetch_afup_entry(client, title, url):
    page = get(client, url)
    metadata = [title]
    upcoming = []
    city, code = "", ""
    html = None
    if page is not None:
        html = BeautifulSoup(page.content, "lxml")
        for selector in ('meta[property="og:title"]', 'meta[property="og:description"]', 'meta[name="description"]', 'meta[property="og:locality"]'):
            tag = html.select_one(selector)
            metadata.append(string(tag.get("content")) if tag else "")
        if html.title:
            metadata.append(html.title.get_text(" ", strip=True))
        home_url = str(page.url)
        home_events = list(schema_events(html))
        upcoming.extend(event for node in home_events if (event := parse_schema(node, home_url, AFUP)))
        if not upcoming:
            for agenda_url in list(afup_agenda_links(html, home_url))[:2]:
                agenda = get(client, agenda_url, timeout=8)
                if agenda is not None:
                    agenda_html = BeautifulSoup(agenda.content, "lxml")
                    upcoming.extend(event for node in schema_events(agenda_html)
                                    if (event := parse_schema(node, str(agenda.url), AFUP)))
        for node in home_events:
            for place in nodes(node.get("location")):
                locality = string(place.get("addressLocality"))
                if locality:
                    address_country = place.get("addressCountry")
                    if isinstance(address_country, dict):
                        address_country = address_country.get("identifier") or address_country.get("name")
                    inferred_code = COUNTRY_CODES.get(norm(string(address_country)), "")
                    if not string(address_country) or inferred_code:
                        city, code = CITIES.get(norm(locality), (locality, inferred_code))
                        break
    if not city:
        city, inferred_code = infer_afup_location(title, url, metadata, html)
        code = code or inferred_code
    if upcoming:
        results = []
        for event in upcoming:
            if not event.online and not event.city and city:
                event.city, event.code = city, code
            if event.url != url and url not in event.extra_urls:
                event.extra_urls.append(url)
            results.append(event)
        return results
    return [Event(title, url, None, None, city, code, False, [AFUP])]


def afup(client):
    base = "https://lettreouverte.afup.org/"
    response = get(client, base)
    if response is None:
        return
    soup = BeautifulSoup(response.content, "lxml")
    excluded = {"afup.org", "github.com", "twitter.com", "x.com", "linkedin.com", "bsky.app", "mastodon.social", "pouet.chapril.org", "youtube.com"}
    links = soup.select("#signataires a.sig__inner[href]")
    if not links:
        logging.warning("Section des signataires introuvable ou vide sur %s", base)
        return
    targets = []
    seen = set()
    for link in links:
        url = url_for(link.get("href"), base)
        host = host_of(url)
        if not host or url in seen or any(host == domain or host.endswith("." + domain) for domain in excluded):
            continue
        seen.add(url)
        title = clean_afup_label(link) or host
        targets.append((title, url))
    with ThreadPoolExecutor(max_workers=6) as pool:
        for entries in pool.map(lambda item: _fetch_afup_entry(client, *item), targets):
            yield from entries


def regional(client):
    pairs = (("EU", "FR"), ("EU", "BE"), ("EU", "CH"), ("EU", "LU"),
             ("NA", "CA"), ("AF", "SN"), ("AF", "CI"), ("AF", "MA"), ("AF", "TN"))

    def fetch_region(pair):
        region, code = pair
        url = f"https://dev.events/{region}/{code}"
        response = get(client, url)
        if response is None:
            return []
        return [
            event
            for node in schema_events(BeautifulSoup(response.content, "lxml"))
            if (event := parse_schema(node, str(response.url), "dev.events", code=code))
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for entries in pool.map(fetch_region, pairs):
            yield from entries


def luma(client):
    for slug, city, code in (("paris", "Paris", "FR"), ("lyon", "Lyon", "FR"), ("bordeaux", "Bordeaux", "FR"),
                             ("lille", "Lille", "FR"), ("brussels", "Bruxelles", "BE"),
                             ("geneva", "Genève", "CH"), ("montreal", "Montréal", "CA")):
        response = get(client, f"https://luma.com/{slug}")
        if response is None:
            continue
        soup = BeautifulSoup(response.content, "lxml")
        base = str(response.url)
        structured_urls = set()
        for node in schema_events(soup):
            title = string(node.get("name"))
            event_url = url_for(node.get("url"), base)
            if event_url != base and urlsplit(event_url).hostname in {"luma.com", "lu.ma"} and TECH.search(title) and not node.get("description"):
                detail = get(client, event_url)
                if detail is not None:
                    detail_soup = BeautifulSoup(detail.content, "lxml")
                    matching = next((item for item in schema_events(detail_soup) if item.get("name") == title), {})
                    node = {**node, **matching}
                    if not node.get("description"):
                        meta = detail_soup.select_one('meta[property="og:description"]')
                        if meta:
                            node["description"] = meta.get("content")
            event = parse_schema(node, base, "luma", city, code, tech_and_french=True)
            if event:
                structured_urls.add(event.url.rstrip("/"))
                yield event
        for payload in scripts(soup, id="__NEXT_DATA__"):
            for node in nodes(payload):
                if not (node.get("name") and node.get("start_at")):
                    continue
                event_url = url_for(node.get("url"), "https://luma.com/") if node.get("url") else base
                if event_url.rstrip("/") in structured_urls:
                    continue
                address = node.get("geo_address_json") or {}
                event = parse_schema({
                    "name": node["name"], "description": node.get("description") or node.get("description_md") or node.get("description_text"),
                    "startDate": node["start_at"], "endDate": node.get("end_at"),
                    "url": event_url,
                    "inLanguage": node.get("language"),
                    "eventAttendanceMode": "OnlineEventAttendanceMode" if node.get("location_type") == "online" else "",
                    "location": {"address": {"addressLocality": address.get("city"), "addressCountry": address.get("country")}} if isinstance(address, dict) else {},
                }, base, "luma", city, code, tech_and_french=True)
                if event:
                    yield event


def meetup(client):
    targets = (
        ("fr--Paris", "Paris", "FR"), ("fr--Lyon", "Lyon", "FR"), ("fr--Nantes", "Nantes", "FR"),
        ("fr--Toulouse", "Toulouse", "FR"), ("fr--Bordeaux", "Bordeaux", "FR"), ("fr--Lille", "Lille", "FR"),
        ("be--Brussels", "Bruxelles", "BE"), ("ch--Geneva", "Genève", "CH"), ("ca--qc--Montreal", "Montréal", "CA"),
    )

    def fetch_city(target):
        slug, city, code = target
        response = get(client, "https://www.meetup.com/fr-FR/find/", params={
            "location": slug, "source": "EVENTS", "categoryId": "546",
        })
        if response is None:
            return []
        return [
            event
            for node in schema_events(BeautifulSoup(response.content, "lxml"))
            if (event := parse_schema(
                node, str(response.url), "meetup", city, code,
                require_tech=True, multilingual_french=True,
            ))
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        for entries in pool.map(fetch_city, targets):
            yield from entries


def meetup_group(url):
    parsed = urlsplit(url)
    if parsed.hostname not in {"meetup.com", "www.meetup.com"}:
        return None
    parts = parsed.path.lower().strip("/").split("/")
    if parts and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0]):
        parts = parts[1:]
    if parts and parts[0] and (len(parts) == 1 or parts[1] == "events"):
        return parts[0]
    return None


def tag_afup_meetups(events):
    """Rattache aux signataires AFUP les événements partageant un groupe Meetup, un domaine dédié ou un nom de communauté."""
    groups = {}
    domains = {}
    communities_by_name = {}

    for event in events:
        if AFUP not in event.sources:
            continue
        for url in [*event.extra_urls, event.url]:
            if group := meetup_group(url):
                groups.setdefault(group, url)
            if event.start is None and (domain := dedicated_host(url)):
                domains.setdefault(domain, event)
        if event.start is None:
            key = clean_title(event.title, strip_parens=True)
            if len(key) >= 4:
                communities_by_name.setdefault(key, event)

    absorbed = set()
    for event in events:
        if event.start is None:
            continue
        matched_community = None
        if AFUP not in event.sources and (group := meetup_group(event.url)) in groups:
            event.sources = sorted(set([*event.sources, AFUP]))
            if groups[group] != event.url and groups[group] not in event.extra_urls:
                event.extra_urls.append(groups[group])
        for url in [event.url, *event.extra_urls]:
            if (domain := dedicated_host(url)) and domain in domains:
                matched_community = domains[domain]
                break
        if matched_community is None:
            key = clean_title(event.title, strip_parens=True)
            candidate = communities_by_name.get(key)
            if candidate and (
                not candidate.city or not event.city
                or metro_city(candidate.city, candidate.code) == metro_city(event.city, event.code)
            ):
                matched_community = candidate
        if matched_community is not None:
            absorbed.add(id(matched_community))
            event.sources = sorted(set([*event.sources, AFUP]))
            if not event.city and matched_community.city and not event.online:
                event.city, event.code = matched_community.city, matched_community.code
            for url in [matched_community.url, *matched_community.extra_urls]:
                if url and url != event.url and url not in event.extra_urls:
                    event.extra_urls.append(url)

    if absorbed:
        events[:] = [event for event in events if id(event) not in absorbed]


def _same_url(url_a, url_b):
    a, b = urlsplit(url_a), urlsplit(url_b)
    host_a, host_b = host_of(url_a), host_of(url_b)
    if not host_a or host_a != host_b or host_a in AGGREGATOR_HOSTS:
        return False
    path_a, path_b = a.path.rstrip("/"), b.path.rstrip("/")
    if host_a in SHARED_HOSTS:
        return bool(path_a) and path_a == path_b
    return path_a == path_b


def _dates_compatible(a, b, same_url=False):
    if (a.start is None) != (b.start is None):
        return False
    if a.start is None:
        return True
    a_start, a_end = a.start, a.end or a.start
    b_start, b_end = b.start, b.end or b.start
    if a_start <= b_end and b_start <= a_end:
        return True
    if same_url:
        delta = abs((dt.date.fromisoformat(a_start) - dt.date.fromisoformat(b_start)).days)
        return delta <= 2
    return False


def _titles_match(a, b, same_place, same_url):
    raw_sim = fuzz.ratio(norm(a.title), norm(b.title))
    if (same_place and raw_sim >= 87) or (same_url and raw_sim >= 70):
        return True
    clean_a, clean_b = clean_title(a.title), clean_title(b.title)
    if not clean_a or not clean_b:
        return False
    clean_sim = fuzz.ratio(clean_a, clean_b)
    if same_url and clean_sim >= 75:
        return True
    if not same_place:
        return False
    if len(min(clean_a, clean_b, key=len)) >= 5 and clean_sim >= 88:
        return True
    city_ref = a.city or b.city
    core_a, core_b = strip_city_tokens(clean_a, city_ref), strip_city_tokens(clean_b, city_ref)
    if len(min(core_a, core_b, key=len)) >= 5 and fuzz.ratio(core_a, core_b) >= 90:
        return True
    if a.start and a.start == b.start:
        base_a = clean_title(re.split(r"\s+[-–—]\s+", a.title, maxsplit=1)[0])
        base_b = clean_title(re.split(r"\s+[-–—]\s+", b.title, maxsplit=1)[0])
        if len(min(base_a, base_b, key=len)) >= 6 and base_a == base_b:
            return True
    return False


def _merge_event(existing, incoming):
    existing.sources = sorted(set(existing.sources + incoming.sources))
    all_urls = [existing.url, *existing.extra_urls, incoming.url, *incoming.extra_urls]
    best_url = max(all_urls, key=lambda u: (url_rank(u), -all_urls.index(u)))
    existing.url = best_url
    existing.extra_urls = []
    for url in all_urls:
        if url and url != existing.url and url not in existing.extra_urls:
            existing.extra_urls.append(url)
    if "[" in existing.title and "[" not in incoming.title:
        existing.title = incoming.title
    elif "[" not in incoming.title and re.search(r"\b20\d{2}\b", incoming.title) and not re.search(r"\b20\d{2}\b", existing.title):
        existing.title = incoming.title
    if not existing.city and incoming.city:
        existing.city, existing.code, existing.online = incoming.city, incoming.code, incoming.online
    elif (
        not existing.online and not incoming.online
        and existing.city and incoming.city
        and norm(existing.city) in METRO_AREAS
        and norm(incoming.city) == METRO_AREAS[norm(existing.city)]
        and url_rank(incoming.url) >= url_rank(existing.url)
    ):
        pass
    if existing.start and incoming.start:
        existing.start = min(existing.start, incoming.start)
    existing.end = max(filter(None, [existing.end, incoming.end]), default=existing.start)


def deduplicate(events):
    dated = []
    undated = []
    result = []
    for event in sorted(events, key=lambda item: (item.start is None, item.start or "", -url_rank(item.url), norm(item.title))):
        pool = undated if event.start is None else dated
        for existing in pool:
            same_url = any(
                _same_url(u1, u2)
                for u1 in [event.url, *event.extra_urls]
                for u2 in [existing.url, *existing.extra_urls]
            )
            if not _dates_compatible(event, existing, same_url=same_url):
                continue
            same_place = (
                event.online == existing.online
                and (event.code == existing.code or not event.code or not existing.code)
                and (not event.city or not existing.city or metro_city(event.city, event.code) == metro_city(existing.city, existing.code))
            )
            if _titles_match(event, existing, same_place, same_url):
                _merge_event(existing, event)
                break
        else:
            pool.append(event)
            result.append(event)
    return sorted(result, key=lambda event: (event.start or "9999", norm(event.title), event.url))


class Geocoder:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS geocache (query TEXT PRIMARY KEY, lat REAL, lon REAL)")
        self.last_request = None
        self.unresolved = set()

    def coordinates(self, client, city, code):
        if not city or not code:
            return None
        canonical_city, _ = CITIES.get(norm(city), (city, code))
        key = f"{norm(canonical_city)}|{code}"
        if key in self.unresolved:
            return None
        row = self.connection.execute("SELECT lat, lon FROM geocache WHERE query = ?", (key,)).fetchone()
        if row and row[0] is not None:
            return row
        coords = None
        if code == "FR":
            response = get(client, "https://data.geopf.fr/geocodage/search/", params={"q": canonical_city, "index": "address", "type": "municipality", "limit": 1})
            if response is not None:
                try:
                    feature = next(iter(response.json()["features"]), None)
                    if feature and float(feature["properties"].get("score", 0)) >= .65:
                        lon, lat = feature["geometry"]["coordinates"]
                        coords = float(lat), float(lon)
                except (ValueError, KeyError, TypeError):
                    logging.warning("Réponse de la BAN illisible pour %s", canonical_city)
        if coords is None:
            if self.last_request is not None:
                time.sleep(max(0, 15.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            endpoint = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
            response = get(client, endpoint, params={"city": canonical_city, "countrycodes": code.lower(), "format": "jsonv2", "limit": 1})
            if response is not None:
                try:
                    item = next(iter(response.json()), None)
                    if item:
                        coords = float(item["lat"]), float(item["lon"])
                except (ValueError, KeyError, TypeError):
                    logging.warning("Réponse Nominatim illisible pour %s", canonical_city)
        if coords and -90 <= coords[0] <= 90 and -180 <= coords[1] <= 180:
            with self.connection:
                self.connection.execute("INSERT OR REPLACE INTO geocache VALUES (?, ?, ?)", (key, *coords))
            return coords
        self.unresolved.add(key)
        return None


def export(client, events, geocoder, output):
    features = []
    for event in events:
        coords = None if event.online else geocoder.coordinates(client, event.city, event.code)
        geometry = {"type": "Point", "coordinates": [round(coords[1], 5), round(coords[0], 5)]} if coords else None
        features.append({"type": "Feature", "geometry": geometry, "properties": {
            "name": event.title, "url": event.url, "extra_urls": event.extra_urls,
            "start_date": event.start, "end_date": event.end, "city": event.city,
            "country": COUNTRIES.get(event.code, ("", ""))[0], "is_online": event.online,
            "is_afup_signatory": AFUP in event.sources, "sources": event.sources,
        }})
    payload = {"type": "FeatureCollection", "metadata": {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "total": len(features),
    }, "features": features}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as temp:
        try:
            json.dump(payload, temp, ensure_ascii=False, indent=2, allow_nan=False)
            temp.write("\n")
            temp.close()
            Path(temp.name).replace(output)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s · %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    contact = quote(os.environ.get("GEOCODER_CONTACT", "").strip(), safe=":/@.+-")
    headers = {"User-Agent": f"CarteEventsTechFrancophones/1.1 ({contact or 'collecteur open source'})",
               "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"}
    with httpx.Client(headers=headers, timeout=15, follow_redirects=True) as client, closing(sqlite3.connect(ROOT / ".geocache.sqlite")) as db:
        raw_events = []
        for source in (afup, developers, regional, luma, meetup):
            try:
                entries = list(source(client))
                raw_events.extend(entries)
                logging.info("%s · %s entrées", source.__name__, len(entries))
            except (ValueError, TypeError, AttributeError) as exc:
                logging.warning("%s · données illisibles (%s)", source.__name__, exc)
        if not raw_events:
            raise SystemExit("Aucune entrée collectée. Le GeoJSON existant est conservé.")
        tag_afup_meetups(raw_events)
        events = deduplicate(raw_events)
        export(client, events, Geocoder(db), OUTPUT)
        logging.info("%s · %s entrées distinctes", OUTPUT, len(events))


if __name__ == "__main__":
    main()
