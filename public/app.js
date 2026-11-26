'use strict';

const $ = id => document.getElementById(id);
const status = $('status');
const controls = ['query', 'source', 'place', 'afup'].map($);
const sources = { afup_lettre_ouverte: 'Signataire AFUP', luma: 'Luma', meetup: 'Meetup' };
const dateFormat = new Intl.DateTimeFormat('fr', { dateStyle: 'long', timeZone: 'UTC' });
const normalize = value => value.toLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, '');
const featurePlace = ({ properties: p }) => p.is_online ? 'online' : p.city ? 'physical' : 'unknown';
let features = null;
let map;
let cluster;
let opener;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function link(url, label) {
  try {
    const parsed = new URL(url);
    if (['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password) {
      const node = element('a', label);
      node.href = parsed.href;
      node.rel = 'noreferrer';
      return node;
    }
  } catch {}
  return element('span', label);
}

function locationText(p) {
  return p.is_online ? 'En ligne' : [p.city, p.country].filter(Boolean).join(', ') || 'Lieu non précisé';
}

function details(container, p) {
  container.append(element('p', locationText(p)));
  if (p.start_date) {
    const start = dateFormat.format(new Date(p.start_date));
    const end = p.end_date && p.end_date !== p.start_date ? dateFormat.format(new Date(p.end_date)) : '';
    container.append(element('p', end ? `${start} – ${end}` : start));
  }
  container.append(element('p', (p.sources || []).map(source => sources[source] || source).join(' · '), 'sources'));
}

function coordinates(feature) {
  if (feature.geometry?.type !== 'Point') return null;
  const [lon, lat] = feature.geometry.coordinates || [];
  return Number.isFinite(lat) && Number.isFinite(lon) ? [lat, lon] : null;
}

function markerFor(feature) {
  const p = feature.properties;
  const popup = element('div');
  popup.append(link(p.url, p.name));
  details(popup, p);
  return L.marker(coordinates(feature), {
    title: p.name, alt: p.name,
    icon: L.divIcon({
      className: p.is_afup_signatory ? 'event-pin afup' : 'event-pin',
      iconSize: [18, 18],
    }),
  }).bindPopup(popup);
}

function render() {
  if (!features) return;
  const [query, source, place, afup] = controls;
  const term = normalize(query.value.trim());
  const selected = features.filter(feature => {
    const p = feature.properties;
    return (!afup.checked || p.is_afup_signatory)
      && (!source.value || p.sources?.includes(source.value))
      && (!place.value || place.value === featurePlace(feature))
      && (!term || normalize([p.name, p.city, p.country, locationText(p), ...(p.sources || [])].join(' ')).includes(term));
  });

  const cards = document.createDocumentFragment();
  const markers = [];
  for (const feature of selected) {
    const p = feature.properties;
    const card = element('li', '', 'card');
    const title = element('h3');
    title.append(link(p.url, p.name));
    card.append(title);
    details(card, p);
    if (cluster && coordinates(feature)) {
      const marker = markerFor(feature);
      markers.push(marker);
      const button = element('button', 'Voir sur la carte');
      button.type = 'button';
      button.setAttribute('aria-label', `Voir sur la carte · ${p.name}`);
      button.addEventListener('click', () => {
        $('map-heading').focus();
        cluster.zoomToShowLayer(marker, () => {
          marker.openPopup();
          opener = button;
          marker.getPopup().getElement()?.querySelector('a')?.focus();
        });
      });
      card.append(button);
    }
    cards.append(card);
  }
  $('list').replaceChildren(cards);
  if (cluster) {
    opener = null;
    cluster.clearLayers();
    cluster.addLayers(markers);
    if (markers.length === 1) map.setView(markers[0].getLatLng(), 9);
    if (markers.length > 1) map.fitBounds(cluster.getBounds(), { padding: [30, 30], maxZoom: 9 });
  }

  const online = selected.filter(feature => feature.properties.is_online).length;
  const mapped = selected.filter(coordinates).length;
  const unmapped = selected.length - online - mapped;
  status.textContent = selected.length
    ? `${selected.length} résultat(s) · ${mapped} géolocalisé(s) · ${online} en ligne · ${unmapped} sans point sur la carte`
    : 'Aucun résultat.';
}

if (window.L?.markerClusterGroup) {
  map = L.map('map', { scrollWheelZoom: false, attributionControl: false }).setView([46.6, 2.4], 5);
  L.control.attribution({ prefix: false }).addTo(map);
  L.tileLayer('https://{s}.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png', {
    maxZoom: 20,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · Tuiles <a href="https://www.openstreetmap.fr/">OSM France</a>',
  }).on('tileerror', () => {
    $('map-message').textContent = 'Certaines tuiles du fond de carte sont indisponibles.';
  }).addTo(map);
  cluster = L.markerClusterGroup().addTo(map);
  $('map-link').hidden = false;
  map.on('popupclose', () => {
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
} else {
  $('map').hidden = true;
  $('map-message').textContent = 'La carte est indisponible. La liste reste consultable.';
}

for (const control of controls) control.addEventListener('input', render);
fetch('./events.geojson')
  .then(response => {
    if (!response.ok) throw new Error(response.status);
    return response.json();
  })
  .then(data => {
    if (data.type !== 'FeatureCollection' || !Array.isArray(data.features)) throw new Error('GeoJSON invalide');
    features = data.features;
    render();
  })
  .catch(() => { status.textContent = 'Impossible de charger les données des événements.'; });
