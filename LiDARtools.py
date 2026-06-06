import rasterio
import numpy as np
from shapely.geometry import Point
import geopandas as gpd
from scipy.ndimage import gaussian_gradient_magnitude, laplace
from scipy.spatial import Delaunay
import ezdxf

import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime


# =========================
# 1. LOAD DEM
# =========================
def load_dem(dem_path):
    with rasterio.open(dem_path) as src:
        dem = src.read(1)
        transform = src.transform
        nodata = src.nodata
        crs = src.crs
    return dem, transform, nodata, crs


# =========================
# 2. TERRAIN METRICS
# =========================
def compute_terrain_metrics(dem, nodata):
    dem = dem.astype(float)

    if nodata is not None:
        dem[dem == nodata] = np.nan
    else:
        # If nodata is undefined, keep as-is; user may already have a masked/clean raster.
        # Any NaNs will still be respected downstream.
        dem[~np.isfinite(dem)] = np.nan

    # Slope proxy (gradient magnitude)
    slope = gaussian_gradient_magnitude(dem, sigma=1)

    # Curvature (detects ridges/valleys)
    curvature = laplace(dem)

    return slope, curvature


# =========================
# 3. ADAPTIVE THINNING
# =========================
def adaptive_thinning(
    dem, slope, curvature, transform, nodata,
    slope_weight=0.7,
    curvature_weight=0.3,
    keep_percent=0.2,
    base_grid=5
):
    dem = dem.astype(float)
    if nodata is not None:
        dem[dem == nodata] = np.nan
    else:
        dem[~np.isfinite(dem)] = np.nan

    rows, cols = np.where(~np.isnan(dem))

    # Normalize metrics (guard against all-NaN or zero-range)
    slope_min, slope_max = np.nanmin(slope), np.nanmax(slope)
    curv_abs = np.abs(curvature)
    curv_min, curv_max = np.nanmin(curv_abs), np.nanmax(curv_abs)

    slope_range = slope_max - slope_min
    curv_range = curv_max - curv_min

    slope_norm = (slope - slope_min) / slope_range if slope_range != 0 else np.zeros_like(slope)
    curvature_norm = (curv_abs - curv_min) / curv_range if curv_range != 0 else np.zeros_like(curv_abs)

    # Combined importance score
    importance = slope_weight * slope_norm + curvature_weight * curvature_norm

    threshold = np.nanpercentile(importance, 100 * (1 - keep_percent))

    points = []
    elevations = []

    for row, col in zip(rows, cols):
        score = importance[row, col]

        if score >= threshold:
            keep = True
        else:
            # Grid thinning for low-importance areas
            keep = (row % base_grid == 0) and (col % base_grid == 0)

        if keep:
            x, y = rasterio.transform.xy(transform, row, col)
            z = dem[row, col]

            if np.isfinite(z):
                points.append((x, y))
                elevations.append(z)

    return np.array(points, dtype=float), np.array(elevations, dtype=float)


# =========================
# 4. BUILD TIN
# =========================
def build_tin(points):
    """
    Delaunay triangulation requires unique XY points.
    Returns (tri, unique_points, unique_index_map)
    """
    if len(points) < 3:
        raise ValueError("Need at least 3 points to build a TIN.")

    # Unique XY points (stable)
    pts_view = np.ascontiguousarray(points).view([('', points.dtype)] * 2)
    _, unique_idx = np.unique(pts_view, return_index=True)
    unique_idx = np.sort(unique_idx)

    unique_points = points[unique_idx]
    tri = Delaunay(unique_points)

    return tri, unique_points, unique_idx


# =========================
# 5. EXPORT DXF (3DFACE)
# =========================
def export_dxf_tin(points, elevations, tri, output_path):
    doc = ezdxf.new()
    msp = doc.modelspace()

    for simplex in tri.simplices:
        pts = []
        for idx in simplex:
            x, y = points[idx]
            z = elevations[idx]
            pts.append((x, y, z))

        # DXF 3DFACE (triangle)
        msp.add_3dface(pts)

    doc.saveas(output_path)


# =========================
# 6. EXPORT LANDXML (TIN Surface)
# =========================
def export_landxml_tin(
    points_xy,
    elevations,
    tri,
    output_path,
    surface_name="TerrainSurface",
    units="metric",
    crs=None,
    write_faces=True,
    landxml_version="1.2",
    write_yxz=True
):
    """
    Writes a minimal LandXML surface:
      <LandXML ...>
        <Units>...</Units>
        <CoordinateSystem .../>   (optional)
        <Surfaces>
          <Surface name="...">
            <Definition surfType="TIN">
              <Pnts>
                <P id="1"> N E Z </P>
              </Pnts>
              <Faces>
                <F> 1 2 3 </F>
              </Faces>
            </Definition>
          </Surface>
        </Surfaces>
      </LandXML>

    - Civil 3D treats coordinates as Northing, Easting, Elevation (Y,X,Z) [5](https://help.autodesk.com/cloudhelp/2026/ENU/Civil3D-UserGuide/files/GUID-4D10ABA5-5EA0-41A8-BB61-C3F446CE7C6B.htm)
      If write_yxz=True (default), we write y x z in each <P>.
      If False, we write x y z.
    - Faces reference <P> ids. [4](http://www.landxml.org/schema/landxml-1.2/documentation/LandXML-1.2Doc_F.html)[3](http://www.landxml.org/schema/landxml-1.2/documentation/LandXML-1.2Doc_Faces.html)
    """

    # --- Root ---
    ns = "http://www.landxml.org/schema/LandXML-1.2"
    ET.register_namespace("", ns)

    landxml = ET.Element(
        f"{{{ns}}}LandXML",
        {
            "xmlns": ns,
            "version": landxml_version,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "language": "English",
        }
    )

    # --- Units (minimal) ---
    units_el = ET.SubElement(landxml, f"{{{ns}}}Units")
    if str(units).lower().startswith("imp"):
        ET.SubElement(units_el, f"{{{ns}}}Imperial", {"linearUnit": "foot", "areaUnit": "squareFoot", "volumeUnit": "cubicFoot"})
    else:
        ET.SubElement(units_el, f"{{{ns}}}Metric", {"linearUnit": "meter", "areaUnit": "squareMeter", "volumeUnit": "cubicMeter"})

    # --- CoordinateSystem (optional; Civil 3D can read EPSG name/code) [6](http://landxml.org/schema/LandXML-1.2/documentation/LandXML-1.2Doc_CoordinateSystem.html)[5](https://help.autodesk.com/cloudhelp/2026/ENU/Civil3D-UserGuide/files/GUID-4D10ABA5-5EA0-41A8-BB61-C3F446CE7C6B.htm)
    if crs is not None:
        try:
            epsg = crs.to_epsg()
        except Exception:
            epsg = None

        cs_attrib = {}
        if epsg is not None:
            cs_attrib["epsgCode"] = str(epsg)

        # Use CRS name if available
        try:
            cs_name = crs.to_string()
        except Exception:
            cs_name = None

        if cs_name:
            cs_attrib["name"] = cs_name

        # ogcWktCode exists in schema, but WKT strings can be huge; keep optional/short [6](http://landxml.org/schema/LandXML-1.2/documentation/LandXML-1.2Doc_CoordinateSystem.html)
        try:
            wkt = crs.to_wkt()
            if wkt and len(wkt) <= 2000:
                cs_attrib["ogcWktCode"] = wkt
        except Exception:
            pass

        if cs_attrib:
            ET.SubElement(landxml, f"{{{ns}}}CoordinateSystem", cs_attrib)

    # --- Surfaces / Surface / Definition ---
    surfaces_el = ET.SubElement(landxml, f"{{{ns}}}Surfaces")
    surface_el = ET.SubElement(surfaces_el, f"{{{ns}}}Surface", {"name": surface_name})
    definition_el = ET.SubElement(surface_el, f"{{{ns}}}Definition", {"surfType": "TIN"})

    # --- Points ---
    pnts_el = ET.SubElement(definition_el, f"{{{ns}}}Pnts")

    # LandXML points must have IDs referenced by faces. [1](https://www.knickknackcivil.com/hacking-landxml.html)[4](http://www.landxml.org/schema/landxml-1.2/documentation/LandXML-1.2Doc_F.html)
    # We'll use 1-based IDs in the same order as points_xy/elevations arrays.
    for i, ((x, y), z) in enumerate(zip(points_xy, elevations), start=1):
        p_el = ET.SubElement(pnts_el, f"{{{ns}}}P", {"id": str(i)})

        if write_yxz:
            # N E Z (y x z) for Civil 3D convention [5](https://help.autodesk.com/cloudhelp/2026/ENU/Civil3D-UserGuide/files/GUID-4D10ABA5-5EA0-41A8-BB61-C3F446CE7C6B.htm)
            p_el.text = f"{y:.6f} {x:.6f} {z:.6f}"
        else:
            p_el.text = f"{x:.6f} {y:.6f} {z:.6f}"

    # --- Faces ---
    if write_faces:
        faces_el = ET.SubElement(definition_el, f"{{{ns}}}Faces")
        # Each <F> contains 3 ids (TIN) referencing P ids [4](http://www.landxml.org/schema/landxml-1.2/documentation/LandXML-1.2Doc_F.html)[3](http://www.landxml.org/schema/landxml-1.2/documentation/LandXML-1.2Doc_Faces.html)
        for simplex in tri.simplices:
            # tri.simplices indices are 0-based into points_xy; convert to 1-based IDs
            a, b, c = (int(simplex[0]) + 1, int(simplex[1]) + 1, int(simplex[2]) + 1)
            f_el = ET.SubElement(faces_el, f"{{{ns}}}F")
            f_el.text = f"{a} {b} {c}"

    # --- Pretty print and write ---
    xml_bytes = ET.tostring(landxml, encoding="utf-8")
    pretty = minidom.parseString(xml_bytes).toprettyxml(indent="  ", encoding="utf-8")
    with open(output_path, "wb") as f:
        f.write(pretty)


# =========================
# 7. OPTIONAL: EXPORT POINTS
# =========================
def export_points_gpkg(points, elevations, crs, output_path):
    gdf = gpd.GeoDataFrame(
        {"elevation": elevations},
        geometry=[Point(x, y) for x, y in points],
        crs=crs
    )
    gdf.to_file(output_path, driver="GPKG")


# =========================
# MAIN PIPELINE
# =========================
def process_dem_to_tin(
    dem_path,
    output_landxml,
    output_dxf=None,
    output_points=None,
    keep_percent=0.2,
    base_grid=5,
    surface_name="TerrainSurface",
    units="metric",
    landxml_write_faces=True
):
    print("Loading DEM...")
    dem, transform, nodata, crs = load_dem(dem_path)

    print("Computing terrain metrics...")
    slope, curvature = compute_terrain_metrics(dem, nodata)

    print("Adaptive thinning...")
    points, elevations = adaptive_thinning(
        dem,
        slope,
        curvature,
        transform,
        nodata,
        keep_percent=keep_percent,
        base_grid=base_grid
    )

    print(f"Points retained: {len(points)}")

    print("Building TIN...")
    tri, unique_points, unique_idx = build_tin(points)
    unique_elevations = elevations[unique_idx]

    print("Exporting LandXML...")
    export_landxml_tin(
        unique_points,
        unique_elevations,
        tri,
        output_landxml,
        surface_name=surface_name,
        units=units,
        crs=crs,
        write_faces=landxml_write_faces,
        landxml_version="1.2",
        write_yxz=True  # Northing, Easting, Elevation for Civil 3D [5](https://help.autodesk.com/cloudhelp/2026/ENU/Civil3D-UserGuide/files/GUID-4D10ABA5-5EA0-41A8-BB61-C3F446CE7C6B.htm)
    )

    if output_dxf:
        print("Exporting DXF...")
        export_dxf_tin(unique_points, unique_elevations, tri, output_dxf)

    if output_points:
        print("Exporting points...")
        export_points_gpkg(points, elevations, crs, output_points)

    print("Done.")


# =========================
# RUN
# =========================
if __name__ == "__main__":
    process_dem_to_tin(
        dem_path=r"C:\Users\ejones\Downloads\Test1_b68ef026-35d2-41fc-9d74-9293ee1d5380\Stitched_DEMs.tif",
        output_landxml=r"C:\Users\ejones\Downloads\terrain_tin.xml",
        output_dxf=r"C:\Users\ejones\Downloads\terrain_tin.dxf",
        output_points=r"C:\Users\ejones\Downloads\terrain_points.gpkg",
        keep_percent=0.25,   # adjust density
        base_grid=6,         # thinning aggressiveness
        surface_name="Stitched_DEM_TIN",
        units="Imperial",
        landxml_write_faces=True
    )