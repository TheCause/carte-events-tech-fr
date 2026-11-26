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
from collections import defaultdict
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
    "FR": ("France", "Paris|Lyon|Nantes|Bordeaux|Toulouse|Lille|Rennes|Montpellier|Grenoble|Strasbourg|Marseille|Aix-en-Provence|Clermont-Ferrand|Tours|Caen|Brest|Nice|Valbonne|Rouen|Dijon|Metz|Nancy|Pau|Poitiers|Angers|Orléans|Annecy|Avignon|Reims|Limoges"),
    "BE": ("Belgique", "Bruxelles|Liège|Namur|Louvain-la-Neuve|Mons|Charleroi"),
    "CH": ("Suisse", "Genève|Lausanne|Neuchâtel|Fribourg|Sion|Yverdon-les-Bains"),
    "LU": ("Luxembourg", "Luxembourg|Esch-sur-Alzette"),
    "CA": ("Canada", "Montréal|Québec|Sherbrooke|Gatineau|Trois-Rivières"),
    "SN": ("Sénégal", "Dakar"),
    "CI": ("Côte d’Ivoire", "Abidjan|Yamoussoukro"),
    "MA": ("Maroc", "Casablanca|Rabat|Marrakech|Tanger|Fès"),
    "TN": ("Tunisie", "Tunis|Sfax|Sousse"),
    "CM": ("Cameroun", "Yaoundé|Douala"),
    "MG": ("Madagascar", "Antananarivo"),
}
TECH = re.compile(
    r"\b(?:dev|tech|code|coding|software|logiciel|informatique|programmation|ia|ai|llm|"
    r"data|cloud|devops|linux|open[ -]?source|rust|python|php|symfony|laravel|javascript|"
    r"typescript|react|angular|java|kotlin|golang|cyber\w*|infosec|agile|api|kubernetes|"
    r"docker|web|ux|hackathon|afup|devfest|gdg|postgres\w*|sql)\b", re.I,
)


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)).strip()


CITIES = {norm(city): (city, code) for code, (_, names) in COUNTRIES.items() for city in names.split("|")}
CITIES.update({"brussels": CITIES["bruxelles"], "geneva": CITIES["geneve"], "sophia antipolis": CITIES["valbonne"]})
CITY_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(CITIES, key=len, reverse=True))) + r")(?!\w)")
COUNTRY_CODES = {norm(name): code for code, (name, _) in COUNTRIES.items()}
COUNTRY_CODES.update({norm(code): code for code in COUNTRIES})
COUNTRY_CODES.update({"cote d'ivoire": "CI", "ivory coast": "CI", "belgium": "BE", "switzerland": "CH", "morocco": "MA", "tunisia": "TN", "senegal": "SN", "cameroon": "CM"})
ONLINE = {"online", "en ligne", "virtual"}
AGENDA = re.compile(r"\b(?:agenda|calendrier|calendar|event(?:s)?|evenement(?:s)?|meetup(?:s)?|conference(?:s)?|prochain(?:e|es|s)?)\b")


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


def parse_schema(node, base, source, city="", code="", *, tech_and_french=False):
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
    online = ("OnlineEventAttendanceMode" in mode or (
        not physical and any(isinstance(p, dict) and "VirtualLocation" in types(p) for p in places)
    )) and "MixedEventAttendanceMode" not in mode
    address = next((p["address"] for p in physical if isinstance(p.get("address"), dict)), {})
    city = string(address.get("addressLocality")) or city
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("identifier") or country.get("name")
    code = COUNTRY_CODES.get(norm(string(country)), code if not country else "")
    city, inferred_code = CITIES.get(norm(city), (city, ""))
    code = code or inferred_code
    start = date(node.get("startDate"))
    end = date(node.get("endDate")) or start
    if not title or not start or end < TODAY or string(node.get("eventStatus")).endswith(("EventCancelled", "EventPostponed")):
        return None
    if tech_and_french and not (TECH.search(text) and french(text, language)):
        return None
    if source != AFUP and not in_scope(city, code, text, online, language):
        return None
    url = url_for(node.get("url") or node.get("@id"), base) or base
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
        online = country in ONLINE or norm(city) in ONLINE
        canonical, inferred = CITIES.get(norm(city), (city, ""))
        code = COUNTRY_CODES.get(country, inferred if not country else "")
        languages = [string(tag.get("value")) for tag in item.get("tags", [])
                     if isinstance(tag, dict) and tag.get("key") == "language"]
        language = "fr" if any(french("", lang) for lang in languages if lang) else next(iter(languages), "")
        if item.get("status") in {"cancelled", "canceled", "postponed"}:
            continue
        if in_scope(city, code, f'{title} {string(item.get("description"))}', online, language):
            yield Event(title, url_for(item.get("hyperlink"), url) or url, dates[0], dates[-1],
                        "En ligne" if online else canonical, "" if online else code, online, ["developers.events"])


def afup_agenda_links(soup, base):
    origin = (urlsplit(base).hostname or "").removeprefix("www.")
    seen = {urlsplit(base).path.rstrip("/")}
    for link in soup.select("a[href]"):
        url = url_for(link["href"], base)
        target = urlsplit(url)
        path = target.path.rstrip("/")
        if not path or path in seen or (target.hostname or "").removeprefix("www.") != origin:
            continue
        label = norm(link.get_text(" ", strip=True))
        if AGENDA.search(label) or AGENDA.search(norm(path).replace("_", "-")):
            seen.add(path)
            yield url


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
    seen = set()
    for link in links:
        url = url_for(link.get("href"), base)
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        if not host or url in seen or any(host == domain or host.endswith("." + domain) for domain in excluded):
            continue
        seen.add(url)
        title = link.get_text(" ", strip=True) or host
        page = get(client, url)
        metadata = [title]
        upcoming = []
        city, code = "", ""
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
                        city, code = CITIES.get(norm(locality), (locality, COUNTRY_CODES.get(norm(string(address_country)), "")))
                        break
        inferred = CITY_PATTERN.search(norm(" ".join(metadata)))
        if not city and inferred:
            city, code = CITIES[inferred.group()]
        if upcoming:
            for event in upcoming:
                if not event.online and not event.city:
                    event.city, event.code = city, code
                if event.url != url:
                    event.extra_urls.append(url)
                yield event
        else:
            yield Event(title, url, None, None, city, code, False, [AFUP])


def regional(client):
    for region, code in (("EU", "FR"), ("EU", "BE"), ("EU", "CH"), ("EU", "LU"),
                         ("NA", "CA"), ("AF", "SN"), ("AF", "CI"), ("AF", "MA"), ("AF", "TN")):
        url = f"https://dev.events/{region}/{code}"
        response = get(client, url)
        if response is not None:
            for node in schema_events(BeautifulSoup(response.content, "lxml")):
                event = parse_schema(node, str(response.url), "dev.events", code=code)
                if event:
                    yield event


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
    for slug, city, code in (
        ("fr--Paris", "Paris", "FR"), ("fr--Lyon", "Lyon", "FR"), ("fr--Nantes", "Nantes", "FR"),
        ("fr--Toulouse", "Toulouse", "FR"), ("fr--Bordeaux", "Bordeaux", "FR"), ("fr--Lille", "Lille", "FR"),
        ("be--Brussels", "Bruxelles", "BE"), ("ch--Geneva", "Genève", "CH"), ("ca--qc--Montreal", "Montréal", "CA"),
    ):
        response = get(client, "https://www.meetup.com/fr-FR/find/", params={
            "location": slug, "source": "EVENTS", "categoryId": "546",
        })
        if response is not None:
            for node in schema_events(BeautifulSoup(response.content, "lxml")):
                event = parse_schema(node, str(response.url), "meetup", city, code)
                if event:
                    yield event


def meetup_group(url):
    parsed = urlsplit(url)
    if parsed.hostname not in {"meetup.com", "www.meetup.com"}:
        return None
    parts = parsed.path.lower().strip("/").split("/")
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0]):
        parts = parts[1:]
    if parts and parts[0] and (len(parts) == 1 or parts[1] == "events"):
        return parts[0]
    return None


def tag_afup_meetups(events):
    groups = {}
    for event in events:
        if AFUP in event.sources:
            for url in [*event.extra_urls, event.url]:
                if group := meetup_group(url):
                    groups.setdefault(group, url)
    for event in events:
        if AFUP not in event.sources and (group := meetup_group(event.url)) in groups:
            event.sources.append(AFUP)
            if groups[group] != event.url and groups[group] not in event.extra_urls:
                event.extra_urls.append(groups[group])


def deduplicate(events):
    buckets = defaultdict(list)
    result = []
    for event in events:
        matches = buckets[event.start]
        for existing in matches:
            a, b = urlsplit(event.url), urlsplit(existing.url)
            same_url = (a.hostname, a.path.rstrip("/")) == (b.hostname, b.path.rstrip("/")) and bool(a.path.strip("/"))
            same_place = event.online == existing.online and (event.code == existing.code or not event.code or not existing.code) and (
                not event.city or not existing.city or norm(event.city) == norm(existing.city))
            similarity = fuzz.ratio(norm(event.title), norm(existing.title))
            if (same_place and similarity >= 87) or (same_url and similarity >= 75):
                existing.sources = sorted(set(existing.sources + event.sources))
                for url in [event.url, *event.extra_urls]:
                    if url != existing.url and url not in existing.extra_urls:
                        existing.extra_urls.append(url)
                if not existing.city:
                    existing.city, existing.code, existing.online = event.city, event.code, event.online
                existing.end = max(filter(None, [existing.end, event.end]), default=None)
                break
        else:
            matches.append(event)
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
        key = f"{norm(city)}|{code}"
        if key in self.unresolved:
            return None
        row = self.connection.execute("SELECT lat, lon FROM geocache WHERE query = ?", (key,)).fetchone()
        if row and row[0] is not None:
            return row
        coords = None
        if code == "FR":
            response = get(client, "https://data.geopf.fr/geocodage/search/", params={"q": city, "index": "address", "type": "municipality", "limit": 1})
            if response is not None:
                try:
                    feature = next(iter(response.json()["features"]), None)
                    if feature and float(feature["properties"].get("score", 0)) >= .65:
                        lon, lat = feature["geometry"]["coordinates"]
                        coords = float(lat), float(lon)
                except (ValueError, KeyError, TypeError):
                    logging.warning("Réponse de la BAN illisible pour %s", city)
        if coords is None:
            if self.last_request is not None:
                time.sleep(max(0, 15.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            endpoint = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
            response = get(client, endpoint, params={"city": city, "countrycodes": code.lower(), "format": "jsonv2", "limit": 1})
            if response is not None:
                try:
                    item = next(iter(response.json()), None)
                    if item:
                        coords = float(item["lat"]), float(item["lon"])
                except (ValueError, KeyError, TypeError):
                    logging.warning("Réponse Nominatim illisible pour %s", city)
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
    headers = {"User-Agent": f"CarteEventsTechFrancophones/1.0 ({contact or 'collecteur open source'})",
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
