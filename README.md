
# HAL UGRC Raster Downloader

A Flask web app for downloading and clipping LIDAR raster data from publicly hosted datasets. The app provides a browser UI for uploading polygon boundaries, selecting datasets, and requesting clipped GeoTIFF outputs.

## What it does
- Serves a web interface from `app.py` with a Leaflet map UI.
- Accepts boundary input as GeoJSON or shapefile parts (`.shp`, `.shx`, `.dbf`, `.prj`).
- Queries UGRC LIDAR dataset indexes for intersecting tiles.
- Downloads, mosaics, reprojects, clips, and zips the resulting raster files.
- Uses a background job system with endpoints for job status, cancel/delete, and download.

## Key endpoints
- `/` : main map UI
- `/upload_shapefile_parts` : upload shapefile multipart upload and convert to GeoJSON
- `/download_lidar` : submit a raster extraction job
- `/jobs` : list current jobs
- `/jobs/<job_id>` : job status
- `/jobs/<job_id>/cancel` : cancel/delete a job
- `/jobs/<job_id>/download` : download completed ZIP output

## Requirements
- Python 3.9+ recommended
- Install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run locally for development

```bash
python app.py
```

Then open:

```text
http://localhost:5000
```

## Run with Waitress / production-style server

```bash
python server.py
```

Then open:

```text
http://localhost:8080
```

## Repository structure

```text
app.py
server.py
requirements.txt
static/
  css/
  js/
templates/
  index.html
```

## Notes
- `app.py` is the Flask application entrypoint.
- `server.py` runs the same app via `waitress` on port 8080.
- The job system stores temporary output under the `LIDAR_WORKDIR` environment variable or the OS temp directory by default.
- For production deployment, run behind a proper WSGI server and secure the app before exposing it publicly.
