'use strict';

const $ = id => document.getElementById(id);
const summary = $('summary');
const liveStatus = $('status');
const filterForm = $('filter-form');
const resetBtn = $('reset');
const controls = {
  query: $('query'),
  period: $('period'),
  country: $('country'),
  source: $('source'),
  place: $('place'),
  afup: $('afup'),
};

const SOURCE_LABELS = {
  afup_lettre_ouverte: 'Signataire AFUP',
  luma: 'Luma',
  meetup: 'Meetup',
};

const MONTHS_FR = [
  'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
  'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
];
const collator = new Intl.Collator('fr', { sensitivity: 'base' });
const normalize = value => String(value || '').toLowerCase().normalize('NFD').replace(/\p{Diacritic}/gu, '');
const featurePlace = ({ properties: p }) => p.is_online ? 'online' : p.city ? 'physical' : 'unknown';

let allFeatures = null;
let initialCountryParam = '';
let map;
let cluster;
let opener;
let activeCard = null;
let statusTimer = null;

function todayPlainISO() {
  if (typeof Temporal !== 'undefined') {
    return Temporal.Now.plainDateISO().toString();
  }
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function isLeapYear(year) {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year, month) {
  return [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

function addDaysISO(isoDate, days) {
  if (typeof Temporal !== 'undefined') {
    return Temporal.PlainDate.from(isoDate).add({ days }).toString();
  }
  let [y, m, d] = isoDate.split('-').map(Number);
  d += days;
  while (d > daysInMonth(y, m)) {
    d -= daysInMonth(y, m);
    m += 1;
    if (m > 12) {
      m = 1;
      y += 1;
    }
  }
  return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function formatCalendarDate(isoDate) {
  if (!isoDate) return '';
  if (typeof Temporal !== 'undefined') {
    try {
      return Temporal.PlainDate.from(isoDate).toLocaleString('fr-FR', { dateStyle: 'long' });
    } catch {}
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!match) return isoDate;
  const [, year, month, day] = match;
  const monthName = MONTHS_FR[Number(month) - 1];
  return monthName ? `${Number(day)} ${monthName} ${year}` : isoDate;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    if (['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password) {
      return parsed;
    }
  } catch {}
  return null;
}

function link(url, label, className) {
  const parsed = safeUrl(url);
  if (parsed) {
    const node = element('a', label, className);
    node.href = parsed.href;
    node.rel = 'noreferrer';
    return node;
  }
  return element('span', label, className);
}

function locationText(p) {
  return p.is_online ? 'En ligne' : [p.city, p.country].filter(Boolean).join(', ') || 'Lieu non précisé';
}

function timeElement(isoDate) {
  const node = element('time', formatCalendarDate(isoDate));
  node.dateTime = isoDate;
  return node;
}

function dateParagraph(p) {
  if (!p.start_date) return null;
  const line = element('p', '', 'meta-line');
  line.append(timeElement(p.start_date));
  if (p.end_date && p.end_date !== p.start_date) {
    line.append(' – ', timeElement(p.end_date));
  }
  return line;
}

function buildBadges(p) {
  const wrap = element('div', '', 'badges');
  if (p.is_afup_signatory) {
    wrap.append(element('span', 'Signataire AFUP', 'badge afup'));
  }
  if (p.is_online) {
    wrap.append(element('span', 'En ligne', 'badge'));
  } else if (!p.start_date) {
    wrap.append(element('span', 'Communauté', 'badge'));
  }
  return wrap.childNodes.length ? wrap : null;
}

function appendDetails(container, p, includeExtras = false) {
  const badges = buildBadges(p);
  if (badges) container.append(badges);
  container.append(element('p', locationText(p), 'meta-line'));
  const dates = dateParagraph(p);
  if (dates) container.append(dates);
  if (p.sources?.length) {
    const labels = p.sources.map(source => SOURCE_LABELS[source] || source).join(' · ');
    container.append(element('p', labels, 'sources'));
  }
  if (includeExtras && Array.isArray(p.extra_urls) && p.extra_urls.length) {
    const validExtras = p.extra_urls.map(safeUrl).filter(Boolean);
    if (validExtras.length) {
      const extraLine = element('p', 'Liens associés : ', 'extra-links');
      validExtras.slice(0, 2).forEach((parsed, index) => {
        if (index > 0) extraLine.append(' · ');
        const hostLabel = parsed.hostname.replace(/^www\./, '');
        extraLine.append(link(parsed.href, hostLabel));
      });
      container.append(extraLine);
    }
  }
}

function escapeIcs(value) {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/;/g, '\\;')
    .replace(/,/g, '\\,')
    .replace(/\r?\n/g, '\\n');
}

function downloadIcs(p) {
  if (!p.start_date) return;
  const dtStart = p.start_date.replace(/-/g, '');
  const dtEnd = addDaysISO(p.end_date || p.start_date, 1).replace(/-/g, '');
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  const uid = `${dtStart}-${normalize(p.name).replace(/[^a-z0-9]+/g, '-').slice(0, 40)}@carte-events-tech-fr`;
  const lines = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//Carte des evenements tech francophones//FR',
    'CALSCALE:GREGORIAN',
    'BEGIN:VEVENT',
    `UID:${uid}`,
    `DTSTAMP:${stamp}`,
    `DTSTART;VALUE=DATE:${dtStart}`,
    `DTEND;VALUE=DATE:${dtEnd}`,
    `SUMMARY:${escapeIcs(p.name)}`,
    `LOCATION:${escapeIcs(locationText(p))}`,
    p.url ? `URL:${escapeIcs(p.url)}` : '',
    p.url ? `DESCRIPTION:${escapeIcs(p.url)}` : '',
    'END:VEVENT',
    'END:VCALENDAR',
  ].filter(Boolean).join('\r\n');

  const blob = new Blob([lines], { type: 'text/calendar;charset=utf-8' });
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = blobUrl;
  a.download = `${normalize(p.name).replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'evenement'}.ics`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}

function coordinates(feature) {
  if (feature.geometry?.type !== 'Point') return null;
  const [lon, lat] = feature.geometry.coordinates || [];
  return Number.isFinite(lat) && Number.isFinite(lon) ? [lat, lon] : null;
}

function markerFor(feature, onOpen) {
  const p = feature.properties;
  const popup = element('div');
  popup.append(link(p.url, p.name));
  appendDetails(popup, p, false);
  const marker = L.marker(coordinates(feature), {
    title: p.name,
    alt: p.is_afup_signatory ? `${p.name} (Signataire AFUP)` : p.name,
    icon: L.divIcon({
      className: p.is_afup_signatory ? 'event-pin afup' : 'event-pin',
      iconSize: p.is_afup_signatory ? [16, 16] : [18, 18],
    }),
  }).bindPopup(popup);
  if (onOpen) marker.on('popupopen', onOpen);
  return marker;
}

function hasActiveFilters() {
  return Boolean(
    controls.query.value.trim()
    || controls.period.value
    || controls.country.value
    || controls.source.value
    || controls.place.value
    || controls.afup.checked,
  );
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  controls.query.value = params.get('q') || '';
  if (params.get('period')) controls.period.value = params.get('period');
  initialCountryParam = params.get('country') || '';
  if (params.get('source')) controls.source.value = params.get('source');
  if (params.get('place')) controls.place.value = params.get('place');
  controls.afup.checked = params.get('afup') === '1' || params.get('afup') === 'true';
}

function syncUrlState() {
  const url = new URL(window.location.href);
  const entries = [
    ['q', controls.query.value.trim()],
    ['period', controls.period.value],
    ['country', controls.country.value],
    ['source', controls.source.value],
    ['place', controls.place.value],
    ['afup', controls.afup.checked ? '1' : ''],
  ];
  for (const [key, val] of entries) {
    if (val) url.searchParams.set(key, val);
    else url.searchParams.delete(key);
  }
  window.history.replaceState(null, '', url);
}

function populateCountries(features) {
  const counts = new Map();
  for (const feature of features) {
    const country = feature.properties.country;
    if (country) counts.set(country, (counts.get(country) || 0) + 1);
  }
  const sorted = [...counts.keys()].toSorted((a, b) => collator.compare(a, b));
  const options = [element('option', 'Tous les pays')];
  options[0].value = '';
  for (const country of sorted) {
    const opt = element('option', `${country} (${counts.get(country)})`);
    opt.value = country;
    options.push(opt);
  }
  controls.country.replaceChildren(...options);
  if (initialCountryParam && counts.has(initialCountryParam)) {
    controls.country.value = initialCountryParam;
  }
}

function announcePolite(message) {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    liveStatus.textContent = message;
  }, 300);
}

function render(announce = false) {
  if (!allFeatures) return;
  resetBtn.hidden = !hasActiveFilters();
  syncUrlState();

  const today = todayPlainISO();
  const in30Days = addDaysISO(today, 30);
  const term = normalize(controls.query.value.trim());
  const period = controls.period.value;
  const selectedCountry = controls.country.value;
  const selectedSource = controls.source.value;
  const selectedPlace = controls.place.value;
  const onlyAfup = controls.afup.checked;

  const selected = allFeatures.filter(feature => {
    const p = feature.properties;
    const endDate = p.end_date || p.start_date;
    if (endDate && endDate < today) return false;
    if (onlyAfup && !p.is_afup_signatory) return false;
    if (selectedCountry && p.country !== selectedCountry) return false;
    if (selectedSource && !p.sources?.includes(selectedSource)) return false;
    if (selectedPlace && selectedPlace !== featurePlace(feature)) return false;
    if (period === 'upcoming' && !p.start_date) return false;
    if (period === 'month' && (!p.start_date || p.start_date > in30Days)) return false;
    if (period === 'communities' && p.start_date) return false;
    if (term) {
      const haystack = normalize([p.name, p.city, p.country, locationText(p), ...(p.sources || [])].join(' '));
      if (!haystack.includes(term)) return false;
    }
    return true;
  });

  const cards = document.createDocumentFragment();
  const markers = [];
  activeCard = null;

  for (const feature of selected) {
    const p = feature.properties;
    const card = element('li', '', 'card');
    const title = element('h3');
    title.append(link(p.url, p.name));
    card.append(title);
    appendDetails(card, p, true);

    const actions = element('div', '', 'card-actions');
    if (cluster && coordinates(feature)) {
      const marker = markerFor(feature, () => {
        if (activeCard) activeCard.classList.remove('is-active');
        activeCard = card;
        card.classList.add('is-active');
      });
      markers.push(marker);
      const mapBtn = element('button', 'Voir sur la carte');
      mapBtn.type = 'button';
      mapBtn.setAttribute('aria-label', `Voir sur la carte · ${p.name}`);
      mapBtn.addEventListener('click', () => {
        $('map-heading').focus();
        cluster.zoomToShowLayer(marker, () => {
          marker.openPopup();
          opener = mapBtn;
          marker.getPopup().getElement()?.querySelector('a')?.focus();
        });
      });
      actions.append(mapBtn);
    }

    if (p.start_date) {
      const icsBtn = element('button', 'Ajouter à l’agenda (.ics)');
      icsBtn.type = 'button';
      icsBtn.setAttribute('aria-label', `Ajouter à l’agenda (.ics) · ${p.name}`);
      icsBtn.addEventListener('click', () => downloadIcs(p));
      actions.append(icsBtn);
    }

    if (actions.childNodes.length) card.append(actions);
    cards.append(card);
  }

  $('list').replaceChildren(cards);

  if (cluster) {
    opener = null;
    cluster.clearLayers();
    cluster.addLayers(markers);
    if (markers.length === 1) map.setView(markers[0].getLatLng(), 9);
    else if (markers.length > 1) map.fitBounds(cluster.getBounds(), { padding: [30, 30], maxZoom: 9 });
  }

  const datedCount = selected.filter(f => f.properties.start_date).length;
  const communityCount = selected.length - datedCount;
  const onlineCount = selected.filter(f => f.properties.is_online).length;
  const mappedCount = selected.filter(coordinates).length;

  const text = selected.length
    ? `${selected.length} entrée(s) (${datedCount} événement(s), ${communityCount} communauté(s)) · ${mappedCount} sur la carte · ${onlineCount} en ligne`
    : 'Aucun résultat ne correspond aux filtres sélectionnés.';
  summary.textContent = text;
  if (announce) announcePolite(text);
}

if (window.L?.markerClusterGroup) {
  map = L.map('map', { scrollWheelZoom: false, attributionControl: false }).setView([46.6, 2.4], 5);
  L.control.attribution({ prefix: false }).addTo(map);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '© <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap</a>',
  }).on('tileerror', () => {
    $('map-message').textContent = 'Certaines tuiles du fond de carte sont indisponibles.';
  }).addTo(map);
  cluster = L.markerClusterGroup().addTo(map);
  $('map-link').hidden = false;
  map.on('popupclose', () => {
    if (activeCard) {
      activeCard.classList.remove('is-active');
      activeCard = null;
    }
    if (opener?.isConnected) opener.focus();
    opener = null;
  });
} else {
  $('map').hidden = true;
  $('map-message').textContent = 'La carte est indisponible. La liste reste consultable.';
}

readUrlState();

filterForm.addEventListener('submit', event => {
  event.preventDefault();
  render(true);
});

filterForm.addEventListener('reset', () => {
  setTimeout(() => {
    initialCountryParam = '';
    render(true);
    controls.query.focus();
  }, 0);
});

for (const control of Object.values(controls)) {
  control.addEventListener('input', () => render(true));
}

fetch('./events.geojson')
  .then(response => {
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  })
  .then(data => {
    if (data.type !== 'FeatureCollection' || !Array.isArray(data.features)) {
      throw new Error('GeoJSON invalide');
    }
    if (data.metadata?.generated_at) {
      const genDate = formatCalendarDate(String(data.metadata.generated_at).slice(0, 10));
      if (genDate) {
        $('updated-at').textContent = `Dernière actualisation : ${genDate} · Export GeoJSON RFC 7946 compatible uMap.`;
      }
    }
    const today = todayPlainISO();
    allFeatures = data.features.filter(f => {
      const end = f?.properties?.end_date || f?.properties?.start_date;
      return !end || end >= today;
    });
    populateCountries(allFeatures);
    render(false);
  })
  .catch(() => {
    summary.textContent = 'Impossible de charger les données des événements.';
    announcePolite('Impossible de charger les données des événements.');
  });
