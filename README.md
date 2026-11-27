# Carte des événements tech francophones

La carte recense les prochaines dates d’événements tech francophones et les communautés signataires de la lettre ouverte de l’AFUP. L’interface filtrable (recherche, période, pays, source, format, signataires AFUP) est utilisable au clavier, synchronise ses filtres dans l’URL, permet l’export calendrier `.ics`, s’adapte au thème clair/sombre du système et affiche aussi les événements en ligne ainsi que les communautés sans point sur la carte.

## Démarrage local

Python 3.11 ou supérieur est requis. Le collecteur interroge des pages et services publics sans clé API.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
GEOCODER_CONTACT="https://adresse-de-votre-projet.example" python collect.py
python -m http.server 8000 --directory public
```

Ouvrez [la carte locale](http://localhost:8000). La variable `GEOCODER_CONTACT` permet d’indiquer votre URL de projet ou une adresse de contact dans le User-Agent. Le workflow renseigne l’URL du dépôt automatiquement.

## Collecte, déduplication et publication

- Le collecteur lit le flux JSON de developers.events, les événements d’À la French (alafrench.org), les sites et agendas des signataires AFUP et les données structurées de dev.events, Luma et Meetup. Les événements partageant un groupe Meetup, un domaine web dédié ou un nom de communauté signataire sont rattachés à l’AFUP, et ceux mentionnant À la French lui sont également rattachés. Sur Luma et Meetup, le périmètre thématique tech et le signal francophone (en ligne et zones multilingues) filtrent le bruit hors sujet. Les annulations, pays hors périmètre francophone et dates périmées sont écartés, et la déduplication fusionne les doublons multi-sources (y compris par aire métropolitaine et plages de dates chevauchantes) en privilégiant l’URL officielle de l’événement.
- La [Géoplateforme IGN](https://geoservices.ign.fr/geocodage) interroge la Base Adresse Nationale, puis Nominatim sert de repli et couvre les autres pays. Le cache SQLite conserve les géocodages réussis. Le service public Nominatim est interrogé en série, avec au plus quatre requêtes par minute pour la collecte récurrente. Définissez `NOMINATIM_URL` dans l’environnement local ou dans les variables GitHub Actions pour utiliser une instance de votre choix. [Politique d’utilisation de Nominatim](https://operations.osmfoundation.org/policies/nominatim/)
- Dans GitHub, ouvrez **Settings → Pages**, puis sélectionnez **GitHub Actions** comme source. Le workflow exécute les tests unitaires puis publie le dossier `public` à chaque push sur `main`, lors d’un déclenchement manuel et chaque jour à 05 h UTC, selon la disponibilité de GitHub Actions.
- Une collecte intégralement vide interrompt le workflow avant publication. Un refus HTTP d’une source n’empêche pas les autres d’alimenter le fichier. Luma ou Meetup peuvent limiter l’accès depuis un environnement CI.

## Import dans uMap

Téléchargez `events.geojson` depuis la carte et importez-le au format GeoJSON dans [uMap France](https://umap.openstreetmap.fr/). Pour lire les mises à jour du fichier hébergé, configurez l’URL publique de `events.geojson` dans les données distantes, sélectionnez le format GeoJSON et activez **Dynamique**. Seules les entités géolocalisées peuvent être représentées sur la carte uMap. Les entrées sans point restent dans le fichier et dans la liste web.

## Licences et confidentialité

Le code du projet est sous licence MIT. Leaflet et Leaflet.markercluster sont servis localement avec leurs licences respectives dans `public/vendor`. Les données issues des sources conservent leurs conditions d’utilisation. Le fond de carte charge les tuiles auprès d’OpenStreetMap (`tile.openstreetmap.org`) et affiche les attributions. L’interface n’inclut ni outil de mesure d’audience, ni cookie applicatif, ni script tiers chargé à l’exécution.
