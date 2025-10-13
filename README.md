# Planet_image_extraction

# Planet Imagery Extraction (AOI search → preview footprints → clip order → download)

Search Planet scenes over your AOI, preview **footprints** on an interactive map, and place **Orders API** jobs to **clip** and **download** imagery bundles—reliably and repeatably.

> **Design constraint:** The script **does not call Planet GET endpoints** except those required by the **Orders** workflow (polling order status and downloading results). Preview uses **AOI + scene footprints only** (no Planet tiles).

---

## Features

- 🔍 **Quick Search** using robust filters (date range, cloud cover, view angle, instruments, item types)
- 🗺️ **Preview Map** (Folium): AOI + **scene footprints** (no Planet tiles). Optional auto-open in your browser.
- 👆 **Interactive or non-interactive**:
  - `--prompt` → pick a scene from a terminal list
  - default (no `--prompt`) → pick **first** result or a specific `--item-id`
- ✂️ **Orders API**: Places a **clip** order to your AOI with chosen product bundle (`visual`, `analytic`, `analytic_sr`)
- ⏱️ **Robust polling** with retries/backoff; downloads all order results to a single ZIP
- 🧾 **Order logs saved** to:
  - Human-readable TXT (`--orders-out`)
  - **CSV** (`--orders-csv`)
  - **JSONL** (`--orders-jsonl`)
- 🧭 **CRS safety**: AOIs auto-reprojected to **EPSG:4326** if needed
- 🧰 **Production-ready**: logging, structured CLI, retries, clear errors, PEP‑8, type hints

---

## Requirements

- **Python**: 3.9+
- **Packages**:
  - `geopandas`, `shapely`, `folium`, `requests`
  - (these pull in `fiona`, `pyproj`, `urllib3`, etc.)

> **Windows tip:** Installing GeoPandas/Shapely is often easiest via **conda**:
> ```bash
> conda create -n planet python=3.11 -y
> conda activate planet
> conda install -c conda-forge geopandas folium requests -y
> ```
> Or with pip (ensure GDAL/GEOS are present on your system):
> ```bash
> python -m venv .venv
> .venv\Scripts\activate      # Windows
> source .venv/bin/activate   # macOS/Linux
> pip install geopandas folium requests
> ```

---

## Configuration

### 1) Planet API Key
Set the environment variable **`PL_API_KEY`** before running:

**Windows PowerShell**

$env:PL_API_KEY="YOUR_PLANET_API_KEY"
