# Carte des événements tech francophones

La carte recense les prochaines dates et les communautés signataires de la lettre ouverte de l’AFUP. La liste filtrable est utilisable au clavier et affiche aussi les événements en ligne et les entrées sans point sur la carte.

## Démarrage local

Python 3.11 ou supérieur est requis. Le collecteur interroge des pages et services publics sans clé API.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
GEOCODER_CONTACT="https://adresse-de-votre-projet.example" python collect.py
python -m http.server 8000 --directory public
```

Ouvrez [la carte locale](http://localhost:8000). La variable `GEOCODER_CONTACT` permet d’indiquer votre URL de projet ou une adresse de contact dans le User-Agent. Le workflow renseigne l’URL du dépôt automatiquement.

## Collecte et publication

- Le collecteur lit le flux JSON de developers.events, les sites et agendas des signataires AFUP et les données structurées de dev.events, Luma et Meetup. Les événements de groupes Meetup signataires sont aussi rattachés à l’AFUP. Sur Luma, un indice tech et un signal de langue française sont tous deux requis. Les annulations et les dates périmées sont filtrées selon les données disponibles. Une source indisponible est signalée dans les journaux.
- La [Géoplateforme IGN](https://geoservices.ign.fr/geocodage) interroge la Base Adresse Nationale, puis Nominatim sert de repli et couvre les autres pays. Le cache SQLite conserve les géocodages réussis. Le service public Nominatim est interrogé en série, avec au plus quatre requêtes par minute pour la collecte récurrente. Définissez `NOMINATIM_URL` dans l’environnement local ou dans les variables GitHub Actions pour utiliser une instance de votre choix. [Politique d’utilisation de Nominatim](https://operations.osmfoundation.org/policies/nominatim/)
- Dans GitHub, ouvrez **Settings → Pages**, puis sélectionnez **GitHub Actions** comme source. Le workflow publie le dossier `public` à chaque push sur `main`, lors d’un déclenchement manuel et chaque jour à 05 h UTC, selon la disponibilité de GitHub Actions.
- Une collecte intégralement vide interrompt le workflow avant publication. Un refus HTTP d’une source n’empêche pas les autres d’alimenter le fichier. Luma ou Meetup peuvent limiter l’accès depuis un environnement CI.

## Import dans uMap

Téléchargez `events.geojson` depuis la carte et importez-le au format GeoJSON dans [uMap France](https://umap.openstreetmap.fr/). Pour lire les mises à jour du fichier hébergé, configurez l’URL publique de `events.geojson` dans les données distantes, sélectionnez le format GeoJSON et activez **Dynamique**. Seules les entités géolocalisées peuvent être représentées sur la carte uMap. Les entrées sans point restent dans le fichier et dans la liste web.

## Licences et confidentialité

Le code du projet est sous licence MIT. Leaflet et Leaflet.markercluster sont servis localement avec leurs licences respectives dans `public/vendor`. Les données issues des sources conservent leurs conditions d’utilisation. Le fond de carte charge les tuiles auprès d’OpenStreetMap France et affiche les attributions. L’interface n’inclut ni outil de mesure d’audience, ni cookie applicatif, ni script tiers chargé à l’exécution.
