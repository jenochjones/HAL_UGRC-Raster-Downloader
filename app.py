import geopandas as gpd
import os
import re
import json
import uuid
import shutil
import zipfile
import tempfile
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from glob import glob
from urllib.parse import urljoin
import requests
from flask import Flask, request, render_template, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.mask import mask as rio_mask
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_geom
from rasterio.io import MemoryFile
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from pyproj import CRS
import numpy as np

# --- Configure logging ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("raster_downloader")
logger.setLevel(LOG_LEVEL)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# --- Create the app ONCE ---
app = Flask(__name__)
app.config.from_mapping(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,  # 20 MB
    JSON_SORT_KEYS=False,
)
USE_PROXYFIX = os.environ.get("USE_PROXYFIX", "false").lower() in ("1", "true", "yes")
if USE_PROXYFIX:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.logger.setLevel(LOG_LEVEL)

# ---- Config ----
LIDAR_EXTENTS_FS0 = "https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/LiDAR_Extents/FeatureServer/0"
TILE_INDEX_MAPSERVER = "https://services1.arcgis.com/99lidPhWCzftIe9K/arcgis/rest/services/Lidar/FeatureServer/0"

# Where to build temp jobs (use persistent path if you want to keep results)
BASE_WORK_DIR = os.environ.get("LIDAR_WORKDIR", tempfile.gettempdir())
os.makedirs(BASE_WORK_DIR, exist_ok=True)
MAX_WORKER_THREADS = int(os.environ.get("MAX_WORKER_THREADS", "2"))

# ----------------------------
# JOB SYSTEM
# ----------------------------
JOBS = {}          # job_id -> dict
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS)

class JobCancelled(Exception):
    pass

def now_epoch():
    return int(time.time())

def safe_name(text: str) -> str:
    """Filesystem-safe name."""
    text = str(text or "").strip()
    text = re.sub(r"[^A-Za-z0-9_\-]+", "_", text)
    return text[:120] or "dataset"

def _choose_output_nodata(dtype, preferred=-9999.0):
    """
    Choose a nodata value that is representable for dtype.
    - floats: preferred (default -9999.0)
    - signed ints: dtype min
    - unsigned ints: dtype max (common convention)
    """
    dt = np.dtype(dtype)

    if np.issubdtype(dt, np.floating):
        # keep preferred (must be finite)
        if preferred is None or (isinstance(preferred, float) and not np.isfinite(preferred)):
            return -9999.0
        return float(preferred)

    if np.issubdtype(dt, np.signedinteger):
        return int(np.iinfo(dt).min)

    if np.issubdtype(dt, np.unsignedinteger):
        return int(np.iinfo(dt).max)

    # fallback
    return -9999.0

def remove_all_files(folderpath: str) -> None:
    """Remove all files in a folder (not recursive)."""
    if os.path.isdir(folderpath):
        for f in glob(os.path.join(folderpath, "*")):
            if os.path.isfile(f):
                os.remove(f)

def download_and_extract_zip(url: str, save_folder: str, cancel_event: threading.Event = None) -> None:
    """
    Download a zip from url -> save_folder, extract contents, remove zip.
    Cancellation-aware: checks cancel_event while downloading.
    """
    os.makedirs(save_folder, exist_ok=True)
    file_name = os.path.basename(url)
    zip_path = os.path.join(save_folder, file_name)

    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if cancel_event and cancel_event.is_set():
                        raise JobCancelled("Job canceled during download.")
                    if chunk:
                        f.write(chunk)

        if cancel_event and cancel_event.is_set():
            raise JobCancelled("Job canceled before extraction.")

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(save_folder)

    finally:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception:
                pass

def get_layer_id(mapserv_url: str, layer_name: str) -> int | None:
    meta_url = f"{mapserv_url}?f=json"
    resp = requests.get(meta_url, timeout=60)
    if resp.status_code != 200:
        return None
    data = resp.json()
    for lyr in data.get("layers", []):
        if lyr.get("name") == layer_name:
            return lyr.get("id")
    return None

def parse_geojson_geometry(uploaded_geojson) -> tuple[list[dict], CRS]:
    """
    Convert GeoJSON to (list_of_geojson_geom_dicts, input_crs).
    Defaults to EPSG:4326 when CRS not present.
    """
    input_crs = CRS.from_epsg(4326)

    if isinstance(uploaded_geojson, dict):
        crs_obj = uploaded_geojson.get("crs")
        if crs_obj:
            try:
                name = crs_obj.get("properties", {}).get("name")
                if name:
                    input_crs = CRS.from_user_input(name)
            except Exception:
                pass

    geoms = []
    if isinstance(uploaded_geojson, dict) and uploaded_geojson.get("type") == "FeatureCollection":
        feats = uploaded_geojson.get("features", [])
        geoms = [f.get("geometry") for f in feats if f.get("geometry")]
    elif isinstance(uploaded_geojson, dict) and uploaded_geojson.get("type") == "Feature":
        if uploaded_geojson.get("geometry"):
            geoms = [uploaded_geojson["geometry"]]
    elif isinstance(uploaded_geojson, dict) and uploaded_geojson.get("type") in ("Polygon", "MultiPolygon"):
        geoms = [uploaded_geojson]
    elif isinstance(uploaded_geojson, list):
        for item in uploaded_geojson:
            if isinstance(item, dict) and item.get("type") == "Feature" and item.get("geometry"):
                geoms.append(item["geometry"])
            elif isinstance(item, dict) and item.get("type") in ("Polygon", "MultiPolygon"):
                geoms.append(item)
    else:
        geoms = []

    if not geoms:
        raise ValueError("No polygon geometry found in uploaded GeoJSON.")

    shapely_geoms = [shape(g) for g in geoms]
    unioned = unary_union(shapely_geoms)
    if unioned.is_empty:
        raise ValueError("Uploaded GeoJSON geometry is empty.")

    return [mapping(unioned)], input_crs

def get_bbox_from_geom(geom_geojson_list: list[dict]) -> dict:
    geom = shape(geom_geojson_list[0])
    minx, miny, maxx, maxy = geom.bounds
    return {"xmin": minx, "ymin": miny, "xmax": maxx, "ymax": maxy}

def get_dataset_ext(datasets: list[str], url: str) -> dict:
    query_url = f"{url}/query"
    where = " OR ".join([f"Tile_Index = '{d}'" for d in datasets])
    params = {
        "outFields": "Tile_Index,File_Extension",
        "returnGeometry": "false",
        "f": "json",
        "where": where
    }
    resp = requests.get(query_url, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    return {
        f["attributes"]["Tile_Index"]: f["attributes"]["File_Extension"]
        for f in features
        if f.get("attributes")
    }

def get_intersecting_tiles(
    geom_geojson_list: list[dict],
    tile_index_url: str,
    layer_id: str,
    in_wkid: int = 4326
):
    if not layer_id:
        return []
    if not geom_geojson_list:
        raise ValueError("geom_geojson_list is required to compute a bounding box")

    if isinstance(in_wkid, CRS):
        epsg = in_wkid.to_epsg()
        in_wkid = int(epsg) if epsg else 4326
    elif hasattr(in_wkid, "to_epsg"):
        epsg = in_wkid.to_epsg()
        in_wkid = int(epsg) if epsg else 4326
    else:
        in_wkid = int(in_wkid)

    bbox = get_bbox_from_geom(geom_geojson_list)
    if not all(k in bbox for k in ("xmin", "ymin", "xmax", "ymax")):
        raise ValueError("Invalid bounding box computed from geometry")
    bbox["spatialReference"] = {"wkid": in_wkid}

    query_url = urljoin(tile_index_url.rstrip("/") + "/", "query")
    params = {
        "f": "json",
        "where": f"TILE_INDEX = '{layer_id}'",
        "geometry": json.dumps(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PATH,TILE,EXT",
        "returnGeometry": "false",
        "inSR": in_wkid
    }

    resp = requests.get(query_url, params=params, timeout=60)
    if resp.status_code != 200:
        return []
    data = resp.json()

    tiles = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes") or feat.get("properties")
        if attrs and all(k in attrs for k in ("PATH", "TILE", "EXT")):
            tiles.append((attrs["PATH"], attrs["TILE"], attrs["EXT"]))
    return tiles

def mosaic_rasters_to_array(
    raster_paths: list[str],
    out_nodata=None,
    negative_to_nodata: bool = True,
    reproject_to_first: bool = True,
    resampling: Resampling = Resampling.bilinear,
):
    """
    Robust mosaic that:
      - normalizes varying source nodata values into one consistent out_nodata
      - sets any values < 0 to nodata BEFORE mosaicing (optional)
      - optionally reprojects inputs to match the first raster CRS
      - avoids rasterio.merge warnings about unsafe nodata values by pre-filling
    Returns: mosaic_array, transform, crs, meta
    """

    if not raster_paths:
        raise ValueError("mosaic_rasters_to_array: raster_paths is empty.")

    srcs = []
    memfiles = []        # keep MemoryFiles alive
    std_datasets = []    # standardized datasets for merge

    try:
        # Open all sources
        for p in raster_paths:
            srcs.append(rasterio.open(p))

        # Basic validation against first raster
        ref = srcs[0]
        if ref.crs is None:
            raise ValueError("Reference raster CRS is None; cannot mosaic reliably.")
        ref_crs = ref.crs
        ref_count = ref.count

        # Ensure consistent band counts (you can relax this if you want, but merge expects consistency)
        for s in srcs:
            if s.count != ref_count:
                raise ValueError(
                    f"Band count mismatch: {s.name} has {s.count} bands but first raster has {ref_count}."
                )

        # Determine a safe common dtype (promote if mixed)
        dtypes = [np.dtype(s.dtypes[0]) for s in srcs]
        if len(set(dtypes)) == 1:
            out_dtype = dtypes[0]
        else:
            # Promote to a safe type (DEM-like workflows usually want float32)
            out_dtype = np.float32

        # Pick final output nodata
        if out_nodata is None:
            out_nodata = _choose_output_nodata(out_dtype, preferred=-9999.0)
        else:
            # Ensure representable
            out_nodata = _choose_output_nodata(out_dtype, preferred=out_nodata)

        # Standardize each dataset into a MemoryFile with:
        #   - same CRS (optional)
        #   - same dtype
        #   - same nodata (out_nodata)
        #   - values < 0 forced to nodata (optional)
        for s in srcs:
            ds = s
            # Read as masked array so that existing nodata becomes mask
            arr = ds.read(masked=True)

            # If nodata is NaN for floats, explicitly mask NaNs
            if np.issubdtype(np.dtype(arr.dtype), np.floating):
                # mask invalid values (nan/inf)
                arr = np.ma.masked_invalid(arr)

            # Force <0 to nodata BEFORE mosaicing
            if negative_to_nodata:
                # masked_where preserves existing mask and adds new mask where condition true
                arr = np.ma.masked_where(arr < 1500, arr)

            # Fill all masked cells with out_nodata and cast to out_dtype
            filled = arr.filled(out_nodata).astype(out_dtype, copy=False)

            # Write standardized dataset to an in-memory GeoTIFF
            meta = ds.meta.copy()
            meta.update(
                driver="GTiff",
                dtype=str(np.dtype(out_dtype)),
                nodata=out_nodata,
                count=filled.shape[0],
            )

            mf = MemoryFile()
            memfiles.append(mf)

            with mf.open(**meta) as tmp:
                tmp.write(filled)

            # Re-open for merge consumption
            std_datasets.append(mf.open())

        # Perform merge using standardized datasets
        mosaic, transform = rio_merge(std_datasets, nodata=out_nodata)

        # Build output meta
        meta_out = ref.meta.copy()
        meta_out.update(
            {
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": transform,
                "count": mosaic.shape[0],
                "crs": ref_crs,
                "dtype": str(np.dtype(out_dtype)),
                "nodata": out_nodata,
            }
        )

        # IMPORTANT: enforce final consistency (paranoia pass)
        if negative_to_nodata:
            mosaic = mosaic.astype(out_dtype, copy=False)
            mosaic[mosaic < 0] = out_nodata

        return mosaic, transform, ref_crs, meta_out

    finally:
        # Close opened datasets
        for ds in std_datasets:
            try:
                ds.close()
            except Exception:
                pass
        for s in srcs:
            try:
                s.close()
            except Exception:
                pass
        for mf in memfiles:
            try:
                mf.close()
            except Exception:
                pass


def clip_array_with_geojson(mosaic_array, mosaic_transform, mosaic_meta, mosaic_crs, mask_geoms, mask_crs):
    """
    Clip mosaic by polygon mask using rasterio.mask.
    Ensures mask_geoms are in the same CRS as the raster.
    mask_crs: pyproj.CRS or rasterio CRS or EPSG string that describes mask_geoms CRS
    """
    # Normalize CRS to something rasterio understands
    if mosaic_crs is None:
        raise ValueError("Raster CRS is None. Cannot clip without a defined raster CRS.")

    # Convert mask_crs to a string for transform_geom
    if isinstance(mask_crs, CRS):
        src_crs_str = mask_crs.to_string()
    else:
        # allow already-string / rasterio CRS
        src_crs_str = str(mask_crs)

    dst_crs_str = mosaic_crs.to_string() if hasattr(mosaic_crs, "to_string") else str(mosaic_crs)

    # Reproject mask geometries ONLY if needed
    if src_crs_str != dst_crs_str:
        mask_geoms_proj = [
            transform_geom(src_crs_str, dst_crs_str, g, precision=6)
            for g in mask_geoms
        ]
    else:
        mask_geoms_proj = mask_geoms

    mem_meta = mosaic_meta.copy()
    mem_meta.update({"driver": "GTiff", "crs": mosaic_crs})

    with MemoryFile() as memfile:
        with memfile.open(**mem_meta) as ds:
            ds.write(mosaic_array)

            # Helpful debug if it still fails
            # print("DEBUG ds.crs:", ds.crs, "bounds:", ds.bounds)

            out_img, out_transform = rio_mask(
                ds,
                mask_geoms_proj,
                crop=True,
                nodata=ds.nodata
            )

            out_meta = ds.meta.copy()
            out_meta.update({
                "height": out_img.shape[1],
                "width": out_img.shape[2],
                "transform": out_transform
            })
            return out_img, out_transform, out_meta


def reproject_to_crs(src_array, src_meta, dst_crs_str: str, resampling=Resampling.bilinear):
    src_crs = src_meta["crs"]
    dst_crs = CRS.from_user_input(dst_crs_str)
    transform, width, height = calculate_default_transform(
        src_crs, dst_crs, src_meta["width"], src_meta["height"],
        *rasterio.transform.array_bounds(
            src_meta["height"], src_meta["width"], src_meta["transform"]
        )
    )

    dst_meta = src_meta.copy()
    dst_meta.update({
        "crs": dst_crs,
        "transform": transform,
        "width": width,
        "height": height
    })

    count = src_meta.get("count", 1)
    dtype = src_meta.get("dtype", "float32")
    import numpy as np
    dst_array = np.zeros((count, height, width), dtype=dtype)

    for band in range(1, count + 1):
        reproject(
            source=src_array[band - 1],
            destination=dst_array[band - 1],
            src_transform=src_meta["transform"],
            src_crs=src_crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=resampling
        )
    return dst_array, dst_meta

def write_geotiff(out_path: str, array, meta):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    meta2 = meta.copy()
    meta2.update({"driver": "GTiff"})
    with rasterio.open(out_path, "w", **meta2) as dst:
        dst.write(array)

def zip_outputs(output_paths: list[str], zip_path: str) -> str:
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in output_paths:
            arcname = os.path.basename(p)
            z.write(p, arcname=arcname)
    return zip_path

# -------- upload helpers --------
def _same_stem(*names: str) -> bool:
    stems = [os.path.splitext(n)[0].lower() for n in names]
    return len(set(stems)) == 1

def _has_ext(filename: str, ext: str) -> bool:
    return filename.lower().endswith(f'.{ext}')

# ----------------------------
# Job helper utilities (NEW)
# ----------------------------
def get_job_snapshot(job_id: str) -> dict | None:
    """Return a shallow copy of a job dict (or None)."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return dict(j) if j else None

def update_job_if_exists(job_id: str, **fields) -> bool:
    """Update job fields if the job still exists. Returns True if updated."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return False
        j.update(fields)
        return True

def pop_job(job_id: str) -> dict | None:
    """Remove and return job record (or None)."""
    with JOBS_LOCK:
        return JOBS.pop(job_id, None)

# -------- Error handlers --------
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return 'File too large. Max size is 20 MB.', 413

@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    if isinstance(e, HTTPException):
        app.logger.warning("HTTP exception: %s", e)
        return e

    app.logger.exception("Unhandled exception during request handling")
    return jsonify({"status": "error", "message": "Internal server error"}), 500

# -------- Routes --------
@app.route('/')
def index():
    return render_template('index.html', center_lat=39.5, center_lng=-98.35, zoom=4)

@app.post('/upload_shapefile_parts')
def upload_shapefile_parts():
    req = request.files
    missing = [k for k in ('shp', 'shx', 'dbf', 'prj') if k not in req or req[k].filename == '']
    if missing:
        return f"Missing required file(s): {', '.join(missing)}", 400

    shp_f = req['shp']; shx_f = req['shx']; dbf_f = req['dbf']; prj_f = req['prj']

    if not _has_ext(shp_f.filename, 'shp'):
        return 'Expected a .shp file for field "shp"', 400
    if not _has_ext(shx_f.filename, 'shx'):
        return 'Expected a .shx file for field "shx"', 400
    if not _has_ext(dbf_f.filename, 'dbf'):
        return 'Expected a .dbf file for field "dbf"', 400
    if not _has_ext(prj_f.filename, 'prj'):
        return 'Expected a .prj file for field "prj"', 400

    if not _same_stem(shp_f.filename, shx_f.filename, dbf_f.filename, prj_f.filename):
        return 'All four files must share the same basename (e.g., parcels.shp/shx/dbf/prj).', 400

    safe_stem = os.path.splitext(secure_filename(shp_f.filename))[0]
    temp_dir = tempfile.mkdtemp(prefix='shp_parts_')
    warnings = []
    try:
        shp_path = os.path.join(temp_dir, f'{safe_stem}.shp')
        shx_path = os.path.join(temp_dir, f'{safe_stem}.shx')
        dbf_path = os.path.join(temp_dir, f'{safe_stem}.dbf')
        prj_path = os.path.join(temp_dir, f'{safe_stem}.prj')
        shp_f.save(shp_path); shx_f.save(shx_path); dbf_f.save(dbf_path); prj_f.save(prj_path)

        try:
            gdf = gpd.read_file(shp_path)
        except Exception as read_err:
            return f'Failed to read shapefile: {read_err}', 400

        if gdf.crs is None:
            warnings.append('CRS not detected; proceeding as-is. Data may not align with basemap.')
        else:
            try:
                gdf = gdf.to_crs(epsg=4326)
            except Exception as crs_err:
                warnings.append(f'Failed to reproject to EPSG:4326: {crs_err}. Using original coordinates.')

        geojson_obj = json.loads(gdf.to_json())
        return jsonify({'layer': {'name': safe_stem, 'geojson': geojson_obj}, 'warnings': warnings})

    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

# ----------------------------
# JOB ENDPOINTS
# ----------------------------
@app.get("/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    jobs_sorted = sorted(jobs, key=lambda j: j.get("created_at", 0), reverse=True)
    return jsonify({
        "jobs": [{
            "job_id": j["job_id"],
            "job_name": j.get("job_name", ""),
            "status": j.get("status", "processing"),
            "error": j.get("error", "")
        } for j in jobs_sorted]
    })

@app.get("/jobs/<job_id>")
def get_job(job_id: str):
    j = get_job_snapshot(job_id)
    if not j:
        abort(404)
    return jsonify({
        "job_id": j["job_id"],
        "job_name": j.get("job_name", ""),
        "status": j.get("status", "processing"),
        "error": j.get("error", "")
    })

@app.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    """
    Cancel/Delete job:
    - If processing: signal cancel, attempt to cancel future
    - Delete workspace and outputs
    - Remove job record from JOBS so it disappears from /jobs immediately
    This supports BOTH "Cancel" and "Delete" UX using one endpoint.
    """
    j = get_job_snapshot(job_id)
    if not j:
        return jsonify({"status": "error", "message": "Job not found"}), 404

    # signal cancel (safe for completed/failed too)
    ev = j.get("cancel_event")
    if ev:
        ev.set()

    # attempt to stop future if not started
    fut = j.get("future")
    try:
        if fut:
            fut.cancel()
    except Exception:
        pass

    # delete workspace
    job_dir = j.get("job_dir")
    if job_dir and os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)

    # remove from registry so it disappears from /jobs immediately
    pop_job(job_id)

    return jsonify({"status": "ok", "job_id": job_id})

@app.get("/jobs/<job_id>/download")
def download_job(job_id: str):
    j = get_job_snapshot(job_id)
    if not j:
        return jsonify({"status": "error", "message": "Job not found"}), 404

    if j.get("status") != "completed":
        return jsonify({"status": "error", "message": "Job not completed"}), 409

    zip_path = j.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"status": "error", "message": "Output not found"}), 404

    job_name = safe_name(j.get("job_name") or "lidar")
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{job_name}_{job_id}.zip"
    )

# ----------------------------
# JOB PROCESSOR
# ----------------------------
def process_lidar_job(job_id: str, uploaded_geojson, ranked_datasets, output_crs: str, stitch: bool):
    """
    Runs in a background thread. Must not return Flask responses.
    Must tolerate job being deleted while processing (Cancel/Delete).
    """
    j = get_job_snapshot(job_id)
    if not j:
        return

    cancel_event: threading.Event = j["cancel_event"]
    job_dir = j["job_dir"]
    downloads_dir = os.path.join(job_dir, "downloads")
    outputs_dir = os.path.join(job_dir, "outputs")
    os.makedirs(downloads_dir, exist_ok=True)
    os.makedirs(outputs_dir, exist_ok=True)

    def check_cancel_or_deleted():
        # If user clicked Cancel/Delete, cancel_event is set.
        if cancel_event.is_set():
            raise JobCancelled("Job canceled by user.")
        # If job record is gone, treat as deleted and stop.
        if not get_job_snapshot(job_id):
            raise JobCancelled("Job deleted by user.")

    try:
        check_cancel_or_deleted()

        mask_geoms, input_crs = parse_geojson_geometry(uploaded_geojson)

        check_cancel_or_deleted()

        ext_map = get_dataset_ext(ranked_datasets, LIDAR_EXTENTS_FS0)
        produced = []

        for dataset in ranked_datasets:
            check_cancel_or_deleted()
            ds_name = safe_name(dataset)
            ds_download_dir = os.path.join(downloads_dir, ds_name)
            os.makedirs(ds_download_dir, exist_ok=True)

            file_ext = ext_map.get(dataset) or ".tif"

            # 1) query intersecting tiles
            tiles = get_intersecting_tiles(mask_geoms, TILE_INDEX_MAPSERVER, dataset, input_crs)
            if not tiles:
                raise RuntimeError(f"No intersecting tiles found for dataset '{dataset}'.")

            # 2) download/extract each tile zip
            for path, tile, ext in tiles:
                check_cancel_or_deleted()
                tile_url = os.path.join(path, f"{tile}{ext}")
                download_and_extract_zip(tile_url, ds_download_dir, cancel_event=cancel_event)

            check_cancel_or_deleted()

            # 3) find extracted rasters
            raster_paths = glob(os.path.join(ds_download_dir, f"*{file_ext}"))
            if not raster_paths:
                raster_paths = glob(os.path.join(ds_download_dir, "*.tif")) + glob(os.path.join(ds_download_dir, "*.tiff"))
            if not raster_paths:
                raise RuntimeError(f"No rasters found after download for dataset '{dataset}'.")

            check_cancel_or_deleted()

            # 4) mosaic
            
            mosaic_arr, mosaic_transform, mosaic_crs, mosaic_meta = mosaic_rasters_to_array(
                raster_paths,
                out_nodata=-9999.0,
                negative_to_nodata=True
            )

            check_cancel_or_deleted()

            # 6) reproject
            reproj_arr, reproj_meta = reproject_to_crs(mosaic_arr, mosaic_meta, output_crs)

            check_cancel_or_deleted()

            mask_geoms_out = [
                transform_geom(
                    input_crs.to_string(),
                    reproj_meta["crs"].to_string(),
                    g,
                    precision=6
                )
                for g in mask_geoms
            ]

            # 5) clip
            
            clipped_arr, clipped_transform, clipped_meta = clip_array_with_geojson(
                reproj_arr,
                reproj_meta["transform"],
                reproj_meta,
                reproj_meta["crs"],
                mask_geoms_out,
                reproj_meta["crs"]
            )

            # clipped_meta.update({"crs": mosaic_crs, "transform": clipped_transform})

            check_cancel_or_deleted()

            # 7) write tif
            out_tif = os.path.join(outputs_dir, f"{ds_name}.tif")
            write_geotiff(out_tif, clipped_arr, clipped_meta)
            produced.append(out_tif)

        check_cancel_or_deleted()

        final_outputs = produced
        if bool(stitch) and len(produced) > 1:
            
            stitched_arr, stitched_transform, stitched_crs, stitched_meta = mosaic_rasters_to_array(
                produced,
                out_nodata=-9999.0,
                negative_to_nodata=True
            )

            stitched_meta.update({"crs": stitched_crs, "transform": stitched_transform})
            stitched_path = os.path.join(outputs_dir, "Stitched_DEMs.tif")
            write_geotiff(stitched_path, stitched_arr, stitched_meta)
            final_outputs = [stitched_path]

        zip_path = os.path.join(job_dir, "lidar_outputs.zip")
        zip_outputs(final_outputs, zip_path)

        # Mark completed only if job still exists
        update_job_if_exists(job_id, status="completed", zip_path=zip_path, error="")

    except JobCancelled:
        # Job was canceled or deleted. Ensure files are removed.
        shutil.rmtree(job_dir, ignore_errors=True)
        # If job still exists (rare timing), remove it so it disappears from /jobs.
        pop_job(job_id)
        return

    except Exception as e:
        logger.exception("Job %s failed", job_id)
        # Mark failed only if job still exists; otherwise user already deleted it.
        update_job_if_exists(job_id, status="failed", zip_path=None, error=str(e))

# ----------------------------
# Updated: /download_lidar now submits a job
# ----------------------------
@app.post("/download_lidar")
def download_lidar():
    data = request.get_json(silent=True) or {}
    arr = data.get("data")
    job_name = data.get("job_name", "")

    if not isinstance(arr, list) or len(arr) != 4:
        return jsonify({
            "status": "error",
            "message": "Expected JSON {data: [geojson, datasets, output_crs, stitch], job_name: '...'}"
        }), 400

    uploaded_geojson, ranked_datasets, output_crs, stitch = arr

    if not uploaded_geojson or not isinstance(uploaded_geojson, (dict, list)):
        return jsonify({"status": "error", "message": "Invalid or missing uploaded GeoJSON."}), 400
    if not isinstance(ranked_datasets, list) or len(ranked_datasets) == 0:
        return jsonify({"status": "error", "message": "No datasets provided."}), 400
    if not isinstance(output_crs, str) or not output_crs.upper().startswith("EPSG:"):
        return jsonify({"status": "error", "message": "Output CRS must look like EPSG:####."}), 400

    # Create job
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(BASE_WORK_DIR, f"lidar_job_{job_id}")
    cancel_event = threading.Event()

    job_record = {
        "job_id": job_id,
        "job_name": str(job_name or "").strip(),
        "status": "processing",
        "created_at": now_epoch(),
        "job_dir": job_dir,
        "zip_path": None,
        "error": "",
        "cancel_event": cancel_event,
        "future": None
    }

    with JOBS_LOCK:
        JOBS[job_id] = job_record

    # Submit background work
    future = EXECUTOR.submit(process_lidar_job, job_id, uploaded_geojson, ranked_datasets, output_crs, bool(stitch))
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["future"] = future

    logger.info("Accepted job %s: %s datasets, output CRS %s", job_id, len(ranked_datasets), output_crs)

    return jsonify({
        "status": "accepted",
        "job_id": job_id,
        "job_name": job_record["job_name"],
        "message": "Job submitted and processing started."
    }), 202
