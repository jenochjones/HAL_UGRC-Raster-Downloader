import { renderLidarLayer, filterLidarByUploadedAOI } from './helpers.js';

const DEFAULT_STYLE = {
  color: '#0066cc', weight: 1.5, opacity: 1, fillColor: '#66b2ff', fillOpacity: 0.2
};
const HIGHLIGHT_STYLE = {
  color: '#ff8800', weight: 4, opacity: 1, fillColor: '#ffd08a', fillOpacity: 0.55
};

const DRAW_AOI_STYLE = {
  color: '#22c55e', weight: 2, opacity: 1,
  fillColor: '#86efac', fillOpacity: 0.25
};


let lidarAllGeojson = null;
let uploadedShapefileGeoJSON = null;

// Shared state between modules
const state = { featureIndex: new Map(), lidarLayer: null };

let currentlyHighlighted = new Set();
function clearHighlight() {
  if (currentlyHighlighted.size === 0) return;
  for (const lyr of currentlyHighlighted) lyr.setStyle?.(DEFAULT_STYLE);
  currentlyHighlighted.clear();
}
function highlightSelected() {
  const datasets = document.getElementsByClassName("dataset selected");
  const highlightedIds = Array.from(datasets).map(d => String(d.dataset.id));
  if (highlightedIds.length === 0) { clearHighlight(); return; }

  clearHighlight();
  let combinedBounds = null;
  let lastLayer = null;

  for (const hid of highlightedIds) {
    const lyr = state.featureIndex.get(hid);
    if (!lyr) continue;
    lyr.setStyle?.(HIGHLIGHT_STYLE);
    lyr.bringToFront?.();
    currentlyHighlighted.add(lyr);
    lastLayer = lyr;
    const b = lyr.getBounds?.();
    if (b?.isValid?.() && b.isValid()) combinedBounds = combinedBounds ? combinedBounds.extend(b) : b;
  }

  if (combinedBounds?.isValid?.() && combinedBounds.isValid()) {
    map.fitBounds(combinedBounds, { padding: [30, 30], maxZoom: 14 });
    return;
  }
  const ll = lastLayer?.getLatLng?.();
  if (ll) map.setView(ll, Math.max(map.getZoom(), 12));
}

// Initialize map
const map = L.map('map', { zoomControl: false }).setView(MAP_CENTER, MAP_ZOOM);

// Panes
map.createPane('lidarPane'); map.getPane('lidarPane').style.zIndex = 400;
map.createPane('uploadPane'); map.getPane('uploadPane').style.zIndex = 650;

map.createPane('drawPane');
map.getPane('drawPane').style.zIndex = 700; // above upload

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
}).addTo(map);

L.control.zoom({ position: 'topright' }).addTo(map);

// Load LiDAR extents
const base = 'https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/LiDAR_Extents/FeatureServer/0/query';
const params = new URLSearchParams({ where: '1=1', outFields: '*', returnGeometry: 'true', f: 'geojson' });
fetch(`${base}?${params.toString()}`)
  .then(resp => { if (!resp.ok) throw new Error(`HTTP ${resp.status}`); return resp.json(); })
  .then(geojson => { lidarAllGeojson = geojson; renderLidarLayer(lidarAllGeojson, map, state); })
  .catch(err => { console.error('Failed to load GeoJSON:', err); alert('Could not load GeoJSON from the service (check CORS/format/token).'); });

// Shapefile upload group
const serverUploadedGroup = window.serverUploadedGroup || L.layerGroup().addTo(map);

const drawnAOIGroup = new L.FeatureGroup();
map.addLayer(drawnAOIGroup);

// ------------------------------
// NEW: Leaflet.draw AOI tools
// ------------------------------
function setAOIFromGeoJSON(geojson) {
  uploadedShapefileGeoJSON = geojson; // reuse your existing variable as "active AOI"
  if (lidarAllGeojson && geojson) {
    filterLidarByUploadedAOI(geojson, lidarAllGeojson, map, state);
  }
}

function clearAOI() {
  uploadedShapefileGeoJSON = null;
  drawnAOIGroup.clearLayers();
  serverUploadedGroup.clearLayers();

  // Best-effort reset of LiDAR list/layer (avoids "stuck filtered" state)
  try {
    state.featureIndex?.clear?.();
    const list = document.getElementById('dataset-list');
    if (list) list.innerHTML = '';
    if (state.lidarLayer) {
      map.removeLayer(state.lidarLayer);
      state.lidarLayer = null;
    }
    if (lidarAllGeojson) renderLidarLayer(lidarAllGeojson, map, state);
  } catch (e) {
    console.warn('Reset failed (non-fatal):', e);
  }
}

// Create the draw control (only polygon; editing enabled)
const drawControl = new L.Control.Draw({
  position: 'topright',
  draw: {
    polygon: {
      allowIntersection: false,
      showArea: true,
      shapeOptions: { ...DRAW_AOI_STYLE, pane: 'drawPane' }
    },
    polyline: false,
    rectangle: false,
    circle: false,
    circlemarker: false,
    marker: false
  },
  edit: {
    featureGroup: drawnAOIGroup,
    edit: true,
    remove: true
  }
});
map.addControl(drawControl);

// When a polygon is created, it becomes the AOI (and replaces uploaded shapefile display)
map.on(L.Draw.Event.CREATED, (e) => {
  const layer = e.layer;

  // keep exactly one AOI polygon
  drawnAOIGroup.clearLayers();
  serverUploadedGroup.clearLayers(); // user is now using drawn AOI instead of uploaded shapefile

  // ensure style/pane
  if (layer.setStyle) layer.setStyle({ ...DRAW_AOI_STYLE, pane: 'drawPane' });
  if (layer.options) layer.options.pane = 'drawPane';

  drawnAOIGroup.addLayer(layer);

  const aoi = drawnAOIGroup.toGeoJSON(); // FeatureCollection
  // Tag as user-drawn for backend if you want (optional)
  aoi.type = 'FeatureCollection';
  aoi.features = (aoi.features || []).map(f => ({
    ...f,
    properties: { ...(f.properties || {}), aoi_source: 'drawn' }
  }));

  setAOIFromGeoJSON(aoi);

  const b = layer.getBounds?.();
  if (b?.isValid?.() && b.isValid()) map.fitBounds(b, { padding: [20, 20] });
});

// When edited, update AOI and re-filter
map.on(L.Draw.Event.EDITED, () => {
  if (drawnAOIGroup.getLayers().length === 0) return;
  const aoi = drawnAOIGroup.toGeoJSON();
  aoi.features = (aoi.features || []).map(f => ({
    ...f,
    properties: { ...(f.properties || {}), aoi_source: 'drawn' }
  }));
  setAOIFromGeoJSON(aoi);
});

// When deleted, clear AOI and reset
map.on(L.Draw.Event.DELETED, () => {
  clearAOI();
});

// Sidebar buttons: start drawing / clear
const btnDrawAOI = document.getElementById('btn-draw-aoi');
const btnClearAOI = document.getElementById('btn-clear-aoi');

btnDrawAOI?.addEventListener('click', () => {
  // programmatically start polygon draw
  const handler = new L.Draw.Polygon(map, {
    allowIntersection: false,
    showArea: true,
    shapeOptions: { ...DRAW_AOI_STYLE, pane: 'drawPane' }
  });
  handler.enable();
});

btnClearAOI?.addEventListener('click', () => clearAOI());

function styleFor(feature) {
  const geom = feature?.geometry?.type || '';
  if (geom.includes('Line')) return { color: '#2563eb', weight: 3 };
  if (geom.includes('Polygon')) return { color: '#ef4444', weight: 2, fillColor: '#fca5a5', fillOpacity: 0.35 };
  return { color: '#10b981' };
}

const btnLoadShp4 = document.getElementById('btn-load-shp-4');
const shp4Input = document.getElementById('shp-4-input');

btnLoadShp4?.addEventListener('click', () => shp4Input?.click());

shp4Input?.addEventListener('change', async (e) => {
  const files = Array.from(e.target.files || []);
  if (files.length === 0) return;

  try {
    const parts = { shp: null, shx: null, dbf: null, prj: null };
    for (const f of files) {
      const ext = f.name.split('.').pop().toLowerCase();
      if (ext in parts && !parts[ext]) parts[ext] = f;
    }
    if (!parts.shp || !parts.shx || !parts.dbf || !parts.prj) {
      alert('Please select exactly one each: .shp, .shx, .dbf, and .prj (same basename).');
      e.target.value = '';
      return;
    }
    const stem = (name) => name.replace(/\.[^.]+$/, '').toLowerCase();
    const s = stem(parts.shp.name);
    if (![parts.shx, parts.dbf, parts.prj].every(f => stem(f.name) === s)) {
      alert('All four files must share the same name (e.g., parcels.shp/shx/dbf/prj).');
      e.target.value = '';
      return;
    }

    const form = new FormData();
    form.append('shp', parts.shp, parts.shp.name);
    form.append('shx', parts.shx, parts.shx.name);
    form.append('dbf', parts.dbf, parts.dbf.name);
    form.append('prj', parts.prj, parts.prj.name);

    const res = await fetch('/upload_shapefile_parts', { method: 'POST', body: form });
    if (!res.ok) throw new Error(await res.text() || `Upload failed: ${res.status}`);

    const payload = await res.json();
    const { layer, warnings } = payload || {};
    if (warnings?.length) console.warn('Upload warnings:', warnings);
    if (!layer?.geojson) {
      alert('No features returned from server.');
      e.target.value = '';
      return;
    }

    serverUploadedGroup.clearLayers();
    drawnAOIGroup?.clearLayers?.(); // if user previously drew an AOI, remove it
    const layerObj = L.geoJSON(layer.geojson, {
      pane: 'uploadPane',
      style: styleFor,
      pointToLayer: (feature, latlng) => L.circleMarker(latlng, { radius: 6, ...styleFor(feature) }),
      onEachFeature: (feature, lyr) => {
        const props = feature?.properties || {};
        const rows = Object.entries(props)
          .slice(0, 20)
          .map(([k, v]) => `<tr><th style="text-align:left;padding-right:8px;">${k}</th><td>${v}</td></tr>`)
          .join('');
        if (rows) lyr.bindPopup(`<div style="max-height:180px;overflow:auto;"><table>${rows}</table></div>`);
      }
    }).addTo(serverUploadedGroup);

    uploadedShapefileGeoJSON = layer.geojson;
    filterLidarByUploadedAOI(layer.geojson, lidarAllGeojson, map, state);

    const b = layerObj.getBounds?.();
    if (b?.isValid?.() && b.isValid()) map.fitBounds(b, { padding: [20, 20] });

  } catch (err) {
    console.error(err);
    alert(`Upload failed: ${err.message}`);
  } finally {
    e.target.value = '';
  }
});

document.getElementById('stitch-toggle')?.addEventListener('click', (e) => {
  e.currentTarget.classList.toggle('selected');
});

window.addEventListener("dataset:selected", () => highlightSelected());
window.addEventListener('resize', () => map.invalidateSize());

/* ------------------------------
   Helpers for download payload
------------------------------ */
function normalizeEpsg(input) {
  const raw = (input || '').trim();
  if (!raw) return null;
  const upper = raw.toUpperCase();
  return upper.startsWith('EPSG:') ? upper : `EPSG:${upper.replace(/[^0-9]/g, '') || upper}`;
}

function getSelectedDatasetObjects() {
  const selected = document.querySelectorAll('#dataset-list .dataset.selected');
  return Array.from(selected).map((el) => ({
    id: String(el.dataset.id),
    index: String(el.dataset.index ?? ''),
    label: (el.dataset.label || el.textContent || '').trim() || String(el.dataset.id)
  }));
}

function isStitchSelected() {
  return document.getElementById('stitch-toggle')?.classList.contains('selected') === true;
}

/* ------------------------------
   Ranking dialog (unchanged)
------------------------------ */
function buildRankListItems(listEl, datasetObjs) {
  listEl.innerHTML = '';
  datasetObjs.forEach((ds) => {
    const li = document.createElement('li');
    li.className = 'rank-item';
    li.draggable = true;
    li.dataset.index = ds.index;

    const handle = document.createElement('span');
    handle.className = 'rank-handle';
    handle.textContent = '⠿';
    handle.title = 'Drag to reorder';

    const label = document.createElement('span');
    label.className = 'rank-label';
    label.textContent = ds.label;

    const controls = document.createElement('span');
    controls.className = 'rank-controls';

    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'rank-arrow';
    up.textContent = '↑';
    up.title = 'Move up';
    up.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      const prev = li.previousElementSibling;
      if (prev) listEl.insertBefore(li, prev);
    });

    const down = document.createElement('button');
    down.type = 'button';
    down.className = 'rank-arrow';
    down.textContent = '↓';
    down.title = 'Move down';
    down.addEventListener('click', (e) => {
      e.preventDefault(); e.stopPropagation();
      const next = li.nextElementSibling;
      if (next) listEl.insertBefore(next, li);
    });

    controls.appendChild(up);
    controls.appendChild(down);

    li.appendChild(handle);
    li.appendChild(label);
    li.appendChild(controls);

    listEl.appendChild(li);
  });

  // drag & drop ordering
  let dragging = null;
  listEl.querySelectorAll('li').forEach((li) => {
    li.addEventListener('dragstart', (e) => {
      dragging = li;
      li.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/html', li.innerHTML);
    });
    li.addEventListener('dragend', () => {
      dragging = null;
      li.classList.remove('dragging');
    });
    li.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (!dragging || dragging === li) return;
      const rect = li.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      if (before) listEl.insertBefore(dragging, li);
      else listEl.insertBefore(dragging, li.nextSibling);
    });
    li.addEventListener('drop', (e) => { e.preventDefault(); e.stopPropagation(); });
  });
}

function openRankingDialog(datasetObjs) {
  const dialog = document.getElementById('rank-dialog');
  const listEl = document.getElementById('rank-list');
  const cancelBtn = document.getElementById('rank-cancel');
  const form = document.getElementById('rank-form');

  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(datasetObjs.map(d => d.id));
  }

  buildRankListItems(listEl, datasetObjs);

  return new Promise((resolve, reject) => {
    const cleanup = () => {
      cancelBtn?.removeEventListener('click', onCancel);
      dialog.removeEventListener('cancel', onCancel);
      form.removeEventListener('submit', onSubmit);
    };
    const onCancel = () => {
      cleanup();
      dialog.close('cancel');
      reject(new Error('Ranking cancelled'));
    };
    const onSubmit = (e) => {
      e.preventDefault();
      const ranked = Array.from(listEl.querySelectorAll('li')).map(li => li.dataset.index);
      cleanup();
      dialog.close('confirm');
      resolve(ranked);
    };

    cancelBtn?.addEventListener('click', onCancel);
    dialog.addEventListener('cancel', onCancel);
    form.addEventListener('submit', onSubmit);
    dialog.showModal();
  });
}

/* ------------------------------
   NEW: Jobs panel UI + polling
------------------------------ */
const jobsListEl = document.getElementById('jobs-list');
const jobCards = new Map(); // job_id -> element

function statusClass(status) {
  const s = String(status || '').toLowerCase();
  if (s === 'completed') return 'completed';
  if (s === 'failed') return 'failed';
  if (s === 'canceled') return 'canceled';
  return 'processing';
}

function formatJobTitle(job) {
  const name = (job?.job_name || '').trim();
  return name ? name : 'Untitled job';
}

function upsertJobCard(job) {
  if (!jobsListEl || !job?.job_id) return;

  const id = job.job_id;
  let card = jobCards.get(id);

  if (!card) {
    card = document.createElement('div');
    card.className = 'job-card';
    card.dataset.jobId = id;

    const top = document.createElement('div');
    top.className = 'job-top';

    const left = document.createElement('div');

    const title = document.createElement('p');
    title.className = 'job-title';
    title.textContent = formatJobTitle(job);

    const sub = document.createElement('p');
    sub.className = 'job-sub';
    sub.textContent = `Job ID: ${id}`;

    left.appendChild(title);
    left.appendChild(sub);

    const badge = document.createElement('span');
    badge.className = 'job-status processing';
    badge.textContent = 'processing';

    top.appendChild(left);
    top.appendChild(badge);

    const msg = document.createElement('p');
    msg.className = 'job-sub';
    msg.style.display = 'none';

    const actions = document.createElement('div');
    actions.className = 'job-actions';

    const btnCancel = document.createElement('button');
    btnCancel.className = 'menu-btn';
    btnCancel.textContent = 'Cancel';
    btnCancel.addEventListener('click', async () => {
      try {
        btnCancel.disabled = true;
        const res = await fetch(`/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
        if (!res.ok) throw new Error(await res.text() || `Cancel failed (${res.status})`);
        // Remove card after cancel/delete
        card.remove();
        jobCards.delete(id);
      } catch (e) {
        btnCancel.disabled = false;
        alert(e.message || 'Cancel/Delete failed.');
      }
    });

    const btnDownload = document.createElement('button');
    btnDownload.className = 'menu-btn';
    btnDownload.textContent = 'Download';
    btnDownload.style.display = 'none';
    btnDownload.addEventListener('click', () => {
      window.location.href = `/jobs/${encodeURIComponent(id)}/download`;
    });

    actions.appendChild(btnCancel);
    actions.appendChild(btnDownload);

    card.appendChild(top);
    card.appendChild(msg);
    card.appendChild(actions);

    jobsListEl.prepend(card); // newest first
    jobCards.set(id, card);
  }

  // Update content
  const badge = card.querySelector('.job-status');
  const title = card.querySelector('.job-title');
  const msg = card.querySelectorAll('.job-sub')[1]; // second job-sub is message (we set below)
  const btnCancel = card.querySelector('.job-actions .menu-btn:nth-child(1)');
  const btnDownload = card.querySelector('.job-actions .menu-btn:nth-child(2)');

  const st = String(job.status || 'processing').toLowerCase();

  if (title) title.textContent = formatJobTitle(job);

  if (badge) {
    badge.className = `job-status ${statusClass(st)}`;
    badge.textContent = st;
  }

  // show error message if failed
  const errorText = (job.error || '').trim();
  if (msg) {
    if (st === 'failed' && errorText) {
      msg.style.display = 'block';
      msg.textContent = `Error: ${errorText}`;
    } else {
      msg.style.display = 'none';
      msg.textContent = '';
    }
  }

  // buttons
  if (btnCancel) {
    btnCancel.textContent = (st === 'completed' || st === 'failed' || st === 'canceled') ? 'Delete' : 'Cancel';
    btnCancel.disabled = false;
  }

  if (btnDownload) {
    btnDownload.style.display = (st === 'completed') ? 'inline-block' : 'none';
    btnDownload.disabled = (st !== 'completed');
  }
}

async function fetchJobsAndRender() {
  try {
    const res = await fetch('/jobs', { method: 'GET' });
    if (!res.ok) return;
    const data = await res.json();
    const jobs = data?.jobs || [];
    jobs.forEach(upsertJobCard);
  } catch {
    // silent
  }
}

// start polling
fetchJobsAndRender();
setInterval(fetchJobsAndRender, 2000);

/* ------------------------------
   Download LiDAR: now submits a job
------------------------------ */
async function sendDownloadRequest(payloadArray, jobName) {
  const res = await fetch('/download_lidar', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ data: payloadArray, job_name: jobName })
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => '');
    throw new Error(txt || `Request failed (${res.status})`);
  }
  return res.json().catch(() => ({}));
}

document.getElementById('btn-download-lidar')?.addEventListener('click', async () => {
  try {
    if (!uploadedShapefileGeoJSON && !drawnAOIGroup) {
      alert('Please load a shapefile AOI or draw an AOI polygon first.');
      return;
    }

    const selected = getSelectedDatasetObjects();
    if (selected.length === 0) { alert('Please select at least one dataset.'); return; }

    const outCrs = normalizeEpsg(document.getElementById('out-crs')?.value);
    if (!outCrs || outCrs === 'EPSG:') { alert('Please enter an Output CRS (EPSG code).'); return; }

    const stitch = isStitchSelected();

    // Rank if multiple datasets
    let rankedIndices = selected.map(d => d.index);
    if (selected.length > 1) rankedIndices = await openRankingDialog(selected);

    const jobName = (document.getElementById('job-name')?.value || '').trim();

    // Build array: [uploaded geojson, selected datasets (ranked indices), output CRS, stitch toggle]
    const payload = [uploadedShapefileGeoJSON, rankedIndices, outCrs, stitch];

    const btn = document.getElementById('btn-download-lidar');
    if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }

    const result = await sendDownloadRequest(payload, jobName);
    console.log('Job submitted:', result);

    if (result?.job_id) {
      // show immediately in jobs panel
      upsertJobCard({ job_id: result.job_id, job_name: result.job_name || jobName, status: 'processing' });
    } else {
      alert(result?.message || 'Request submitted. Server is processing.');
    }

  } catch (err) {
    console.error(err);
    alert(err?.message || 'Download request failed.');
  } finally {
    const btn = document.getElementById('btn-download-lidar');
    if (btn) { btn.disabled = false; btn.textContent = 'Download LiDAR'; }
  }
});

const select = document.getElementById('out-crs-select');
const input = document.getElementById('out-crs');

// When user selects from dropdown → update input
select.addEventListener('change', () => {
  input.value = select.value.replace("EPSG:", "");
});

// When user types manually → clear dropdown selection
input.addEventListener('input', () => {
  select.selectedIndex = 0;
});