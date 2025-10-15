#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Planet image extraction: search, preview (tiles), and order clipped imagery.

- Reads one or many AOIs (GeoJSON/Shapefile/GeoPackage).
- Quick-searches Planet scenes using robust filters.
- (Optional) Builds an interactive Folium map with Planet tile overlays.
- Places an Orders API request with a server-side clip tool to the AOI.
- Polls order status and downloads results into a single ZIP per order.

Notes:
- Set your Planet API key in environment variable: PL_API_KEY
- Tested with PSScene items (configurable).

This version removes asset activation & clipped PNG preview utilities:
- Removed: activate_and_get_asset_location()
- Removed: save_clipped_preview_png()

Prompting:
- Use --prompt to enable interactive item selection per AOI.
- By default (no --prompt), the script is non-interactive and will
  use --item-id if provided, else the first search result.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import folium
import geopandas as gpd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from shapely.geometry import Polygon, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
import webbrowser


# ------------------------------- Logging ------------------------------------ #

def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


logger = logging.getLogger("planet-image-extraction")


# ------------------------------ Data Models --------------------------------- #

@dataclass(frozen=True)
class SearchParams:
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    max_cloud_cover: float = 0.1  # 0..1
    min_nadir: float = -1.0       # degrees
    max_nadir: float = 1.0        # degrees
    instruments: List[str] = field(default_factory=lambda: ["PSB.SD"])
    item_types: List[str] = field(default_factory=lambda: ["PSScene"])
    result_limit: int = 100       # cap features drawn on a map


@dataclass(frozen=True)
class Config:
    api_key: str
    data_url: str = "https://api.planet.com/data/v1"
    orders_url: str = "https://api.planet.com/compute/ops/orders/v2"
    poll_interval_s: int = 30
    download_dir: Path = Path(".")
    timeout_s: int = 30


# ------------------------------ HTTP Session -------------------------------- #

def requests_retry_session(
    retries: int = 5,
    backoff_factor: float = 0.5,
    status_forcelist: Tuple[int, ...] = (429, 500, 502, 503, 504),
    allowed_methods: Tuple[str, ...] = ("GET", "POST"),
    timeout_s: int = 30,
    api_key: Optional[str] = None,
) -> requests.Session:
    """Create a requests.Session with retry/backoff and optional auth."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=set(allowed_methods),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.request = _with_timeout(session.request, timeout_s)  # type: ignore
    if api_key:
        session.auth = (api_key, "")
    return session


def _with_timeout(request_func, timeout_s: int):
    def wrapper(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout_s)
        return request_func(method, url, **kwargs)
    return wrapper


# ------------------------------- Utilities ---------------------------------- #

def pretty(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def validate_geometry(geom: BaseGeometry) -> Polygon:
    if geom.is_empty:
        raise ValueError("AOI geometry is empty.")
    if geom.geom_type == "Polygon":
        poly = geom
    else:
        merged = unary_union(geom)
        if merged.geom_type == "Polygon":
            poly = merged
        elif merged.geom_type == "MultiPolygon":
            poly = max(merged.geoms, key=lambda p: p.area)
        else:
            raise ValueError(f"Unsupported geometry type for AOI: {geom.geom_type}")
    if not poly.is_valid:
        poly = poly.buffer(0)
        if not poly.is_valid:
            raise ValueError("AOI polygon is invalid and could not be fixed.")
    return poly  # type: ignore[return-value]


def write_text_list(rows: Iterable[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r}\n")


# ---------------------------- Planet API Helpers ---------------------------- #

def build_planet_filters(
    geometry: Polygon,
    params: SearchParams,
) -> Dict:
    """Construct combined filters for Planet quick-search."""
    geometry_filter = {
        "type": "GeometryFilter",
        "field_name": "geometry",
        "config": mapping(geometry),
    }
    date_filter = {
        "type": "DateRangeFilter",
        "field_name": "acquired",
        "config": {"gt": f"{params.start_date}T00:00:00Z", "lt": f"{params.end_date}T00:00:00Z"},
    }
    cloud_filter = {
        "type": "RangeFilter",
        "field_name": "cloud_cover",
        "config": {"lt": params.max_cloud_cover},
    }
    nadir_filter = {
        "type": "RangeFilter",
        "field_name": "view_angle",
        "config": {"gt": params.min_nadir, "lte": params.max_nadir},
    }
    instrument_filter = {
        "type": "StringInFilter",
        "field_name": "instrument",
        "config": params.instruments,
    }
    combined = {
        "type": "AndFilter",
        "config": [geometry_filter, date_filter, cloud_filter, nadir_filter, instrument_filter],
    }
    logger.debug("Combined filter: %s", pretty(combined))
    return combined


def quick_search(
    session: requests.Session,
    cfg: Config,
    geometry: Polygon,
    params: SearchParams,
) -> Dict:
    """Perform a Planet quick-search and return the GeoJSON."""
    request_payload = {"item_types": params.item_types, "filter": build_planet_filters(geometry, params)}
    quick_url = f"{cfg.data_url}/quick-search"
    logger.info("Searching items: %s between %s..%s", params.item_types, params.start_date, params.end_date)
    resp = session.post(quick_url, json=request_payload)
    if not resp.ok:
        raise RuntimeError(f"Quick-search failed: {resp.status_code} {resp.text}")
    data = resp.json()
    if "features" in data and isinstance(data["features"], list):
        if len(data["features"]) > params.result_limit:
            data["features"] = data["features"][: params.result_limit]
    logger.info("Found %d features.", len(data.get("features", [])))
    return data


def make_preview_map(
    geometry: Polygon,
    search_geojson: Dict,
    api_key: str,
    out_html: Path,
) -> None:
    """Build a Folium map with AOI and Planet tiles for each feature."""
    centroid = geometry.centroid
    fmap = folium.Map(location=[centroid.y, centroid.x], zoom_start=12)
    aoi_style = {"fillOpacity": 0, "color": "blue", "weight": 3, "dashArray": "5, 5"}
    folium.GeoJson(mapping(geometry), style_function=lambda _: aoi_style, name="AOI").add_to(fmap)

    tile_template = "https://tiles.planet.com/data/v1/{item_type}/{item_id}/{{z}}/{{x}}/{{y}}.png?api_key=" + api_key

    for feat in search_geojson.get("features", []):
        item_id = feat.get("id")
        item_type = feat.get("properties", {}).get("item_type")
        if not item_id or not item_type:
            continue
        tiles = tile_template.format(item_type=item_type, item_id=item_id)
        folium.TileLayer(
            tiles=tiles,
            attr="Planet Labs PBC",
            name=f"{item_type}:{item_id}",
            overlay=True,
            control=True,
        ).add_to(fmap)

    folium.LayerControl().add_to(fmap)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(out_html))
    logger.info("Saved preview map to: %s", out_html.resolve())


def place_order_and_download(
    session: requests.Session,
    cfg: Config,
    item_id: str,
    item_type: str,
    aoi_geometry: Polygon,
    product_bundle: str,
    order_name: str,
) -> Path:
    """
    Place a clip order for a single item and download results as a ZIP.
    """
    order_request = {
        "name": order_name,
        "products": [
            {"item_ids": [item_id], "item_type": item_type, "product_bundle": product_bundle}
        ],
        "tools": [{"clip": {"aoi": mapping(aoi_geometry)}}],
    }
    headers = {"Content-Type": "application/json"}
    resp = session.post(cfg.orders_url, headers=headers, json=order_request)
    if resp.status_code != 202:
        raise RuntimeError(f"Order failed: {resp.status_code} {resp.text}")

    order = resp.json()
    order_id = order["id"]
    order_url = order["_links"]["_self"]
    logger.info("Order placed: %s", order_id)

    # Poll for completion
    while True:
        stat = session.get(order_url)
        if not stat.ok:
            raise RuntimeError(f"Failed to fetch order status: {stat.status_code} {stat.text}")
        s = stat.json()
        state = s.get("state")
        logger.info("Order state: %s", state)
        if state == "success":
            break
        if state == "failed":
            raise RuntimeError(f"Order {order_id} failed. Response: {pretty(s)}")
        time.sleep(cfg.poll_interval_s)

    # Download all results into one ZIP
    results = s.get("_links", {}).get("results", [])
    if not results:
        raise RuntimeError("No results links present in completed order.")

    cfg.download_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cfg.download_dir / f"{order_name}_bundle.zip"
    with zip_path.open("wb") as fout:
        import zipfile
        with zipfile.ZipFile(fout, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for idx, result in enumerate(results, start=1):
                url = result.get("location")
                orig_name = result.get("name", f"result_{idx}")
                file_name = Path(orig_name).name
                logger.info("Downloading: %s", file_name)
                r = session.get(url, stream=True)
                r.raise_for_status()
                buffer = BytesIO()
                total = int(r.headers.get("Content-Length", "0"))
                chunk = 1024 * 1024
                downloaded = 0
                for b in r.iter_content(chunk_size=chunk):
                    if b:
                        buffer.write(b)
                        downloaded += len(b)
                        if total:
                            pct = 100.0 * downloaded / total
                            logger.info("... %s: %.1f%%", file_name, pct)
                zf.writestr(file_name, buffer.getvalue())
    logger.info("Saved ZIP: %s", zip_path.resolve())
    return zip_path


# --------------------------------- CLI -------------------------------------- #

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Search, preview, and order Planet imagery clipped to your AOI.",
    )
    p.add_argument("aoi", type=str, help="Path to AOI file (GeoJSON/Shapefile/GPKG).")
    p.add_argument("--start-date", default="2020-09-01", help="Start date YYYY-MM-DD.")
    p.add_argument("--end-date", default="2020-12-31", help="End date YYYY-MM-DD.")
    p.add_argument("--max-cloud", type=float, default=0.10, help="Max cloud cover (0..1).")
    p.add_argument("--min-nadir", type=float, default=-1.0, help="Min view angle (deg).")
    p.add_argument("--max-nadir", type=float, default=1.0, help="Max view angle (deg).")
    p.add_argument("--instrument", action="append", default=["PSB.SD"],
                   help="Instrument code(s), e.g., PSB.SD. Repeat for multiple.")
    p.add_argument("--item-type", action="append", default=["PSScene"],
                   help="Item type(s), e.g., PSScene.")
    p.add_argument("--bundle", default="visual",
                   choices=["visual", "analytic", "analytic_sr"],
                   help="Product bundle for the order.")
    p.add_argument("--download-dir", type=str, default="./downloads", help="Directory for ZIP results.")
    p.add_argument("--orders-out", type=str, default="./orders/list_of_orders_2020.txt",
                   help="Where to save a text list of (api_key_hidden,item_id,item_type,AOI_wkt).")
    p.add_argument("--map-out", type=str, default="./preview/planet_search_preview_map.html",
                   help="Where to save the preview map.")
    p.add_argument("--preview", action="store_true", help="Generate a Folium map with Planet tiles.")

    p.add_argument("--open-map", action="store_true",
               help="Open the preview HTML in your default browser after saving.")

    # Prompt control: opt-in interactive mode
    p.add_argument("--prompt", action="store_true",
                   help="Enable interactive prompts to pick an item per AOI.")

    # Item selection (non-interactive default)
    p.add_argument("--item-id", type=str, default="",
                   help="If provided, order this item_id; otherwise uses the first search result.")

    # Actions
    p.add_argument("--order", action="store_true", help="Place an order for the chosen (or first) feature.")

    # Verbosity
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv).")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    api_key = os.environ.get("PL_API_KEY", "").strip()
    if not api_key:
        logger.error("Environment variable PL_API_KEY is not set. Aborting.")
        return 2

    cfg = Config(api_key=api_key, download_dir=Path(args.download_dir))
    params = SearchParams(
        start_date=args.start_date,
        end_date=args.end_date,
        max_cloud_cover=args.max_cloud,
        min_nadir=args.min_nadir,
        max_nadir=args.max_nadir,
        instruments=args.instrument,
        item_types=args.item_type,
    )

    # Read AOI(s)
    gdf = gpd.read_file(args.aoi)
    if gdf.empty:
        logger.error("AOI file has no features.")
        return 2

    session = requests_retry_session(timeout_s=cfg.timeout_s, api_key=cfg.api_key)

    orders_list: List[str] = []
    order_idx = 0

    for idx, row in gdf.iterrows():
        geom = validate_geometry(row.geometry)
        logger.info("Processing AOI %d ...", idx + 1)

        # Search
        geojson = quick_search(session, cfg, geom, params)
        feats = geojson.get("features", [])
        if not feats:
            logger.warning("No features found for AOI %d.", idx + 1)
            continue

        # Optional map preview
        if args.preview:
            map_path = Path(args.map_out)
            if len(gdf) > 1:
                map_path = map_path.with_name(f"{map_path.stem}_aoi{idx+1}{map_path.suffix}")
            make_preview_map(geom, geojson, api_key, map_path)
            
            if args.open_map:
                webbrowser.open(str(map_path.resolve()))


        # Pick item id: prompt or non-interactive
        chosen_item_id = (args.item_id or "").strip()
        if args.prompt:
            print("\nAvailable features:")
            for i, f in enumerate(feats, start=1):
                props = f.get("properties", {})
                acquired = props.get("acquired", "n/a")
                cc = props.get("cloud_cover", "n/a")
                itype = props.get("item_type", "n/a")
                print(f"{i:2d}. {f.get('id')} | {acquired} | CC={cc} | {itype}")
            pick = input("Enter index to order (ENTER = first, 's' skip this AOI): ").strip().lower()
            if pick == "s":
                logger.info("Skipping AOI %d per user input.", idx + 1)
                continue
            if pick:
                try:
                    chosen_item_id = feats[int(pick) - 1]["id"]
                except Exception:
                    logger.warning("Invalid selection, defaulting to first.")
                    chosen_item_id = feats[0]["id"]
            else:
                chosen_item_id = feats[0]["id"]
        else:
            if not chosen_item_id:
                chosen_item_id = feats[0]["id"]
                logger.info("Non-interactive: selecting first item_id: %s", chosen_item_id)

        # Resolve item type for chosen id
        match = next((f for f in feats if f["id"] == chosen_item_id), feats[0])
        item_type = match["properties"]["item_type"]

        # Persist a reference list (mask API key)
        orders_list.append(f"(api_key=****, item_id={chosen_item_id}, item_type={item_type}, aoi_wkt={geom.wkt[:120]}...)")

        if args.order:
            order_idx += 1
            order_name = f"AOI_{idx+1:03d}_{order_idx:03d}"
            try:
                zip_path = place_order_and_download(
                    session, cfg, chosen_item_id, item_type, geom, args.bundle, order_name
                )
                logger.info("Downloaded order to: %s", zip_path)
            except Exception as e:
                logger.error("Order failed for %s: %s", chosen_item_id, e)

    if orders_list:
        out_txt = Path(args.orders_out)
        write_text_list(orders_list, out_txt)
        logger.info("Wrote orders reference to: %s", out_txt.resolve())
    else:
        logger.info("No orders recorded.")

    return 0


if __name__ == "__main__":

    sys.exit(main())
