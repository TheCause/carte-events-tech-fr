import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup

import collect


class CollectTestCase(unittest.TestCase):
    def test_clean_afup_label_strips_new_badge_without_corrupting_names(self):
        link_badge = BeautifulSoup(
            '<a class="sig__inner" href="https://ncrafts.io/"><span class="sig__badge">new</span> NewCrafts</a>',
            "lxml",
        ).select_one("a")
        self.assertEqual(collect.clean_afup_label(link_badge), "NewCrafts")

        link_inline = BeautifulSoup(
            '<a class="sig__inner" href="https://www.lacantine-brest.net/">new La Cantine Brest</a>',
            "lxml",
        ).select_one("a")
        self.assertEqual(collect.clean_afup_label(link_inline), "La Cantine Brest")

        link_preserve = BeautifulSoup(
            '<a class="sig__inner" href="https://example.org/">New Wave Tech</a>',
            "lxml",
        ).select_one("a")
        self.assertEqual(collect.clean_afup_label(link_preserve), "New Wave Tech")

    def test_parse_schema_detects_visio_prefix_and_excludes_foreign_non_francophone_countries(self):
        future = "2099-05-10"
        visio = collect.parse_schema(
            {
                "@type": "Event",
                "name": "[VISIO] Café Yeeso - Spécial Networking",
                "description": "Rencontre en ligne de la communauté tech.",
                "startDate": future,
                "url": "https://www.meetup.com/yeeso/events/1/",
                "location": {"@type": "Place", "address": {"addressLocality": "Lyon", "addressCountry": "FR"}},
            },
            "https://www.meetup.com/",
            "meetup",
            require_tech=True,
            multilingual_french=True,
        )
        self.assertIsNotNone(visio)
        self.assertTrue(visio.online)
        self.assertEqual(visio.city, "En ligne")
        self.assertEqual(visio.code, "")

        foreign = collect.parse_schema(
            {
                "@type": "Event",
                "name": "SymfonyCon Warsaw 2099",
                "startDate": future,
                "url": "https://live.symfony.com/2099-warsaw/",
                "location": {"@type": "Place", "address": {"addressLocality": "Warsaw", "addressCountry": "PL"}},
            },
            "https://symfony.com/",
            collect.AFUP,
        )
        self.assertIsNone(foreign)

    def test_meetup_filters_non_tech_and_english_in_multilingual_cities(self):
        future = "2099-06-15"
        non_tech = collect.parse_schema(
            {
                "@type": "Event",
                "name": "Vernissage de l’exposition photo « Portraits »",
                "description": "Venez découvrir les photographies exposées dans notre galerie.",
                "startDate": future,
                "url": "https://www.meetup.com/coworking-toulouse/events/42/",
            },
            "https://www.meetup.com/",
            "meetup",
            city="Toulouse",
            code="FR",
            require_tech=True,
            multilingual_french=True,
        )
        self.assertIsNone(non_tech)

        english_montreal = collect.parse_schema(
            {
                "@type": "Event",
                "name": "Building AI Voice Agents: DevPro Workshop",
                "description": "Join us in Montreal to build real-time voice agents with Python and LLMs.",
                "startDate": future,
                "inLanguage": "en",
                "url": "https://www.meetup.com/gdg-montreal/events/99/",
            },
            "https://www.meetup.com/",
            "meetup",
            city="Montréal",
            code="CA",
            require_tech=True,
            multilingual_french=True,
        )
        self.assertIsNone(english_montreal)

        french_montreal = collect.parse_schema(
            {
                "@type": "Event",
                "name": "Atelier Python et IA générative à Montréal",
                "description": "Venez coder ensemble un projet d'intelligence artificielle en Python.",
                "startDate": future,
                "url": "https://www.meetup.com/python-montreal/events/100/",
            },
            "https://www.meetup.com/",
            "meetup",
            city="Montréal",
            code="CA",
            require_tech=True,
            multilingual_french=True,
        )
        self.assertIsNotNone(french_montreal)
        self.assertEqual(french_montreal.city, "Montréal")

    def test_tag_afup_links_meetup_domains_and_absorbs_redundant_undated_communities(self):
        events = [
            collect.Event("SnowCamp", "https://snowcamp.io/", None, None, "", "", False, [collect.AFUP]),
            collect.Event("SnowCamp 2027", "https://snowcamp.io/", "2027-01-14", "2027-01-16", "Grenoble", "FR", False, ["developers.events"]),
            collect.Event("Mixit", "https://mixitconf.org/", None, None, "", "", False, [collect.AFUP]),
            collect.Event("MiXit", "https://dev.events/conferences/mixit", "2027-04-22", "2027-04-23", "Lyon", "FR", False, ["dev.events"]),
            collect.Event("Software Crafters Lyon", "https://www.meetup.com/software-craftsmanship-lyon/", None, None, "Lyon", "FR", False, [collect.AFUP]),
            collect.Event("Coding Dojo", "https://www.meetup.com/fr-FR/software-craftsmanship-lyon/events/123/", "2027-02-10", "2027-02-10", "Villeurbanne", "FR", False, ["meetup"]),
            collect.Event("Human Talks", "https://humantalks.com/", None, None, "", "FR", False, [collect.AFUP]),
        ]
        collect.tag_afup_meetups(events)

        by_title = {e.title: e for e in events}
        self.assertNotIn("SnowCamp", by_title)
        self.assertIn(collect.AFUP, by_title["SnowCamp 2027"].sources)

        self.assertNotIn("Mixit", by_title)
        self.assertIn(collect.AFUP, by_title["MiXit"].sources)
        self.assertIn("https://mixitconf.org/", by_title["MiXit"].extra_urls)

        self.assertIn(collect.AFUP, by_title["Coding Dojo"].sources)
        self.assertIn("Human Talks", by_title)

    def test_deduplicate_merges_metro_cities_years_overlapping_dates_and_promotes_official_url(self):
        raw = [
            collect.Event(
                "DevFest Paris 2026",
                "https://dev.events/conferences/dev-fest-paris-2026-qko0tfdl",
                "2026-11-27",
                "2026-11-27",
                "Nanterre",
                "FR",
                False,
                ["dev.events"],
            ),
            collect.Event(
                "DevFest Paris 2026",
                "https://devfest.gdgparis.fr/",
                "2026-11-27",
                "2026-11-27",
                "Paris",
                "FR",
                False,
                ["developers.events"],
            ),
            collect.Event(
                "DevFest Paris 2026 [External Ticket]",
                "https://www.meetup.com/fr-FR/gdg-paris/events/316006107/",
                "2026-11-27",
                "2026-11-27",
                "Nanterre",
                "FR",
                False,
                [collect.AFUP],
            ),
            collect.Event(
                "apidays Paris",
                "https://dev.events/conferences/apidays-paris",
                "2026-11-30",
                "2026-12-03",
                "Paris",
                "FR",
                False,
                ["dev.events"],
            ),
            collect.Event(
                "Apidays Paris",
                "https://www.apidays.global/paris",
                "2026-12-01",
                "2026-12-03",
                "Paris",
                "FR",
                False,
                ["developers.events"],
            ),
            collect.Event(
                "Devoxx France",
                "https://dev.events/conferences/devoxx-france",
                "2027-04-07",
                "2027-04-09",
                "Paris",
                "FR",
                False,
                ["dev.events"],
            ),
            collect.Event(
                "Devoxx France 2027",
                "https://www.devoxx.fr/",
                "2027-04-07",
                "2027-04-09",
                "Paris",
                "FR",
                False,
                ["developers.events"],
            ),
        ]

        deduped = collect.deduplicate(raw)
        self.assertEqual(len(deduped), 3)

        devfest = deduped[0]
        self.assertEqual(devfest.url, "https://devfest.gdgparis.fr/")
        self.assertEqual(devfest.title, "DevFest Paris 2026")
        self.assertEqual(devfest.sources, [collect.AFUP, "dev.events", "developers.events"])

        apidays = deduped[1]
        self.assertEqual(apidays.url, "https://www.apidays.global/paris")
        self.assertEqual(apidays.start, "2026-11-30")
        self.assertEqual(apidays.end, "2026-12-03")

        devoxx = deduped[2]
        self.assertEqual(devoxx.url, "https://www.devoxx.fr/")
        self.assertEqual(devoxx.title, "Devoxx France 2027")

    def test_infer_afup_location_distinguishes_local_and_national_communities(self):
        self.assertEqual(collect.infer_afup_location("DevLille", "https://devlille.fr/", ["DevLille"]), ("Lille", "FR"))
        body = BeautifulSoup("<html><body><footer>Association loi 1901 basée à Rennes</footer></body></html>", "lxml")
        self.assertEqual(collect.infer_afup_location("BreizhCamp", "https://www.breizhcamp.org/", ["BreizhCamp"], body), ("Rennes", "FR"))

        multi_city = BeautifulSoup("<html><body><footer>Antennes à Paris, Lyon, Nantes et Bordeaux</footer></body></html>", "lxml")
        self.assertEqual(collect.infer_afup_location("Human Talks", "https://humantalks.com/", ["Human Talks"], multi_city), ("", ""))
        self.assertEqual(collect.infer_afup_location("Drupal France", "https://www.drupal.fr/", ["Drupal France"], multi_city), ("", "FR"))
        self.assertEqual(collect.infer_afup_location("PHP Morocco", "https://phpmorocco.ma/", ["PHP Morocco"]), ("", "MA"))

    def test_geocoder_uses_cache_and_exports_rfc7946_geojson(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "data.geopf.fr" in str(request.url):
                return httpx.Response(
                    200,
                    json={"features": [{"geometry": {"coordinates": [2.3522, 48.8566]}, "properties": {"score": 0.95}}]},
                )
            raise AssertionError(f"Unexpected network call: {request.url}")

        with closing(sqlite3.connect(":memory:")) as db, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            geocoder = collect.Geocoder(db)
            coords = geocoder.coordinates(client, "Paris", "FR")
            self.assertEqual(coords, (48.8566, 2.3522))
            # Second call hits SQLite cache without network
            cached = geocoder.coordinates(None, "Paris", "FR")
            self.assertEqual(cached, (48.8566, 2.3522))

            with tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "events.geojson"
                events = [
                    collect.Event("Paris Web", "https://www.paris-web.fr/", "2027-10-01", "2027-10-02", "Paris", "FR", False, [collect.AFUP]),
                    collect.Event("Conf En Ligne", "https://example.fr/live", "2027-10-05", "2027-10-05", "En ligne", "", True, ["luma"]),
                ]
                with patch.object(collect, "Geocoder", return_value=geocoder):
                    collect.export(client, events, geocoder, out)
                payload = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(payload["type"], "FeatureCollection")
                self.assertEqual(payload["metadata"]["total"], 2)
                # RFC 7946: [longitude, latitude]
                self.assertEqual(payload["features"][0]["geometry"]["coordinates"], [2.3522, 48.8566])
                self.assertIsNone(payload["features"][1]["geometry"])


if __name__ == "__main__":
    unittest.main()
