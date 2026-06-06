function addDataset(innerText, id, index) {
  const div = document.createElement("div");
  div.className = "dataset";
  div.dataset.id = id;
  div.dataset.label = innerText;
  div.dataset.index = index;

  const p = document.createElement("p");
  p.textContent = innerText;
  div.appendChild(p);

  div.addEventListener("click", () => {
    div.classList.toggle("selected");

    window.dispatchEvent(new CustomEvent("dataset:selected", {
      detail: { index: div.dataset.index }
    }));

  });

  const container = document.getElementById("dataset-list");
  container.appendChild(div);
}

function renderLidarLayer(geojson, map, state) {
  // Remove old layer if it exists
  if (state.lidarLayer) {
    map.removeLayer(state.lidarLayer);
  }

  // Reset UI + index
  state.featureIndex.clear();

  const container = document.getElementById("dataset-list");
  if (container) container.innerHTML = "";

  // Create new layer
  state.lidarLayer = L.geoJSON(geojson, {
    pane: 'lidarPane',
    style: {
      color: '#0066cc',
      weight: 1.5,
      opacity: 1,
      fillColor: '#66b2ff',
      fillOpacity: 0.2
    },
    onEachFeature: (feature, lyr) => {
      const p = feature.properties || {};
      const objectId = p.OBJECTID;

      // Index by OBJECTID for selection highlighting
      if (objectId != null) state.featureIndex.set(String(objectId), lyr);

      const category = p.Category || 'Not Listed';
      const description = p.Description || 'Not Listed';
      const hacc = p.Horizontal_Accuracy || 'Not Listed';
      const vacc = p.Vertical_Accuracy || 'Not Listed';
      const year = p.Year_Collected || 'Not Listed';
      const tIndex = p.Tile_Index || 'Not Listed';
      const metadata = (p.FTP_Path && p.METADATA) ? (p.FTP_Path + p.METADATA) : null;

      lyr.bindPopup(`
        <div style="min-width:200px; width:auto;">
          <strong>${category}</strong><br/>
          ${description ? `Description: ${description}<br/>` : ''}
          ${tIndex ? `Tile Index: ${tIndex}<br/>` : ''}
          ${year ? `Year: ${year}<br/>` : ''}
          ${hacc ? `Horizontal Accuracy: ${hacc}<br/>` : ''}
          ${vacc ? `Vertical Accuracy: ${vacc}<br/>` : ''}
          ${metadata ? `<a href="${metadata}" target="_blank">Metadata</a>` : ''}
        </div>
      `);

      addDataset(`${category.match(/\{([^}]*)\}/)?.[1]} - ${description}`, objectId, tIndex);
    }
  }).addTo(map);
}

function filterLidarByUploadedAOI(uploadedGeojson, lidarAllGeojson, map, state) {
  if (!lidarAllGeojson || !lidarAllGeojson.features) return;

  const aoiFeatures =
    uploadedGeojson?.type === "FeatureCollection"
      ? uploadedGeojson.features
      : uploadedGeojson?.type === "Feature"
        ? [uploadedGeojson]
        : [];

  // Nothing usable => show everything
  if (aoiFeatures.length === 0) {
    renderLidarLayer(lidarAllGeojson, map, state);
    return;
  }

  // Turf must exist
  if (!window.turf?.booleanIntersects) {
    console.error("Turf.js is not loaded. Add turf.min.js before your module scripts.");
    renderLidarLayer(lidarAllGeojson, map, state);
    return;
  }

  const filtered = {
    ...lidarAllGeojson,
    features: lidarAllGeojson.features.filter(lidarFeat => {
      if (!lidarFeat?.geometry) return false;

      return aoiFeatures.some(aoiFeat => {
        if (!aoiFeat?.geometry) return false;
        try {
          return turf.booleanIntersects(lidarFeat, aoiFeat);
        } catch {
          return false;
        }
      });
    })
  };

  renderLidarLayer(filtered, map, state);

  // Optional zoom to filtered results
  try {
    const tmp = L.geoJSON(filtered);
    const b = tmp.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding: [20, 20] });
  } catch { }
}

export { addDataset, renderLidarLayer, filterLidarByUploadedAOI };