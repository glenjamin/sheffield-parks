#!/usr/bin/env python3
"""
build_playgrounds.py
====================
Rebuilds the Sheffield playpark tracker files from their original sources.

Outputs (written next to this script):
  - sheffield_playgrounds.csv      master list / spreadsheet
  - sheffield_playgrounds.geojson  for uMap (red "to-do" pins, visited=no)
  - sheffield_playgrounds.kml      for CoMaps / Organic Maps / Google Earth

WHY THIS EXISTS
---------------
Sheffield City Council's public map only shows council-MANAGED playgrounds
(~118). Estate / informal / commercial playgrounds are missing from it.
OpenStreetMap is more comprehensive geographically but names most playgrounds
poorly. This script merges both, so the final list is the union:

  Council (authoritative names, with coords)   118
  + OSM playgrounds with a name not near a       ~13
    council site ("OSM only")
  + OSM playgrounds with NO name, reverse-        ~70
    geocoded to "Unnamed playground nr <street>, <area>"
  ----------------------------------------------------
  ~201 playgrounds within 10 miles of the city centre

Requires network access (run OUTSIDE any sandbox). Pure stdlib, no pip deps.
Runtime is dominated by the Nominatim step (~1.1s/point, politeness limit).

DATA SOURCES (discovered 2026-07)
---------------------------------
  - Overpass API (OSM playgrounds)
  - Sheffield CC "Parks & Countryside Service Sites" open-data FeatureServer
  - Nominatim reverse geocoding (labels for unnamed OSM playgrounds)
See the constants below for exact endpoints.
"""

import json
import math
import csv
import time
import os
import urllib.request
import urllib.parse

# --- Config -----------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))

# Sheffield city centre + radius used to exclude non-Sheffield "Sheffield"s.
# The Overpass area query below matches EVERY admin area named "Sheffield",
# which includes Sheffield AL/OH/MA in the USA. Filtering by distance from the
# UK city centre drops those cleanly (they are 200+ miles away).
CENTRE = (53.3811, -1.4701)
RADIUS_M = 16093.4  # 10 miles

# Overpass: all playgrounds inside any admin_level=8 area named "Sheffield".
# Several public mirrors — tried in order, since any one may be busy (504).
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_QUERY = """
[out:json][timeout:60];
area["name"="Sheffield"]["admin_level"="8"]->.a;
(
  node["leisure"="playground"](area.a);
  way["leisure"="playground"](area.a);
  relation["leisure"="playground"](area.a);
);
out center tags;
"""

# Sheffield CC open data — "Parks & Countryside Service Sites" (layer 12).
# We only want rows where site_type == 'Playgrounds'. Public, no token needed
# (the map's utility.arcgis.com proxy URLs ARE token-gated — don't use those).
COUNCIL_URL = ("https://sheffieldcitycouncil.cloud.esriuk.com/server/rest/"
               "services/AGOL/INSPIRE/FeatureServer/12/query")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim usage policy: <=1 req/sec and a real User-Agent. Be polite.
USER_AGENT = "sheffield-playpark-tracker/1.0 (glen@geckoboard.com)"
NOMINATIM_DELAY_S = 1.1

# Dedupe thresholds (metres).
SELF_DEDUPE_M = 40    # merge near-identical unnamed OSM points (way + stray node)
SAME_SITE_M = 150     # an OSM point this close to a named site == the same site
UNNAMED_SAME_M = 120  # unnamed OSM point this close to any named site == dup


def main():
    print("1/5 Fetching OSM playgrounds from Overpass ...")
    osm = fetch_overpass()
    osm_named = [e for e in osm if e["name"]]
    osm_unnamed = [e for e in osm if not e["name"]]
    print(f"     {len(osm)} OSM features ({len(osm_named)} named, "
          f"{len(osm_unnamed)} unnamed)")

    print("2/5 Fetching council playgrounds ...")
    council = fetch_council()
    print(f"     {len(council)} council playgrounds")

    # Named list = council backbone + OSM-named ones NOT co-located with a
    # council site (commercial soft-play, estate parks like Upper Hanover, ...).
    merged = list(council)
    for o in osm_named:
        if not _near(o, council, SAME_SITE_M):
            merged.append({**o, "source": "OSM only"})

    print("3/5 Deduping unnamed OSM playgrounds ...")
    # Collapse near-duplicate unnamed points, then drop any that sit on top of
    # an already-listed named site (those are the same playground, unnamed).
    deduped = _self_dedupe(osm_unnamed, SELF_DEDUPE_M)
    new_unnamed = [p for p in deduped if not _near(p, merged, UNNAMED_SAME_M)]
    print(f"     {len(osm_unnamed)} -> {len(deduped)} (self) -> "
          f"{len(new_unnamed)} genuinely new")

    print(f"4/5 Reverse-geocoding {len(new_unnamed)} unnamed points "
          f"(~{len(new_unnamed) * NOMINATIM_DELAY_S:.0f}s) ...")
    for i, p in enumerate(new_unnamed):
        p["name"] = _label_unnamed(p["lat"], p["lon"])
        p["source"] = "OSM unnamed"
        time.sleep(NOMINATIM_DELAY_S)
        if (i + 1) % 20 == 0:
            print(f"     {i + 1}/{len(new_unnamed)}")
    merged.extend(new_unnamed)

    # Keep only playgrounds within 10 miles of the city centre (drops US ones).
    merged = [r for r in merged
              if _haversine(CENTRE[0], CENTRE[1], r["lat"], r["lon"]) <= RADIUS_M]
    # Council first, then alphabetical — stable, readable ordering.
    merged.sort(key=lambda x: (x["source"] != "Council", x["name"]))

    print(f"5/5 Writing {len(merged)} playgrounds ...")
    write_csv(merged)
    write_geojson(merged)
    write_kml(merged)

    from collections import Counter
    print("     by source:", dict(Counter(r["source"] for r in merged)))
    print("Done.")


# --- Fetchers ---------------------------------------------------------------

def fetch_overpass():
    """Return [{name|None, lat, lon}] for every OSM playground in the query.

    Tries each mirror, retrying transient 429/504s with a short backoff.
    """
    data = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    last = None
    for url in OVERPASS_URLS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data,
                                             headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as r:
                    payload = json.load(r)
                return _parse_overpass(payload)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 504):      # busy/rate-limited -> back off
                    time.sleep(5 * (attempt + 1))
                    continue
                break                          # other errors: try next mirror
            except Exception as e:
                last = e
                time.sleep(3)
        print(f"     mirror failed ({url}): {last}")
    raise RuntimeError(f"all Overpass mirrors failed: {last}")


def _parse_overpass(payload):
    out = []
    for e in payload["elements"]:
        # ways/relations return a "center"; nodes return lat/lon directly.
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if lat is None:
            continue
        out.append({"name": e.get("tags", {}).get("name"),
                    "lat": lat, "lon": lon})
    return out


def fetch_council():
    """Return [{name, lat, lon, source='Council'}] for council playgrounds."""
    params = urllib.parse.urlencode({
        "where": "site_type='Playgrounds'",
        "outFields": "site_name,site_type",
        "returnGeometry": "true",
        "outSR": "4326",       # WGS84 lat/lon
        "f": "json",
    })
    req = urllib.request.Request(f"{COUNCIL_URL}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    out = []
    for f in payload["features"]:
        g = f.get("geometry") or {}
        lat, lon = g.get("y"), g.get("x")
        if lat is None and g.get("rings"):  # polygon -> ring centroid
            pts = g["rings"][0]
            lon = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
        name = f["attributes"].get("site_name")
        if lat and name:
            out.append({"name": name, "lat": lat, "lon": lon,
                        "source": "Council"})
    return out


def _label_unnamed(lat, lon):
    """Reverse-geocode to 'Unnamed playground nr <street>, <area>'."""
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "jsonv2",
        "zoom": "16", "addressdetails": "1",
    })
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            a = json.load(r).get("address", {})
    except Exception:
        a = {}
    road = a.get("road") or a.get("pedestrian") or a.get("footway")
    area = (a.get("suburb") or a.get("neighbourhood") or a.get("village")
            or a.get("residential") or a.get("quarter") or a.get("hamlet"))
    bits = [b for b in (road, area) if b]
    if bits:
        return "Unnamed playground nr " + ", ".join(bits)
    return f"Unnamed playground ({lat:.4f},{lon:.4f})"


# --- Writers ----------------------------------------------------------------

def write_csv(rows):
    with open(os.path.join(HERE, "sheffield_playgrounds.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lat", "lon", "source",
                    "visited", "date_visited", "notes"])
        for r in rows:
            w.writerow([r["name"], round(r["lat"], 6), round(r["lon"], 6),
                        r["source"], "", "", ""])


def write_geojson(rows):
    # _umap_options makes uMap draw every pin red (= to-do). In uMap, add a
    # conditional style visited=yes -> Green so ticking off recolours the pin.
    feats = [{
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(r["lon"], 6), round(r["lat"], 6)]},
        "properties": {"name": r["name"], "source": r["source"],
                       "visited": "no", "date_visited": "", "notes": "",
                       "_umap_options": {"color": "Red", "iconClass": "Drop"}},
    } for r in rows]
    with open(os.path.join(HERE, "sheffield_playgrounds.geojson"), "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, indent=1)


def write_kml(rows):
    placemarks = "\n".join(
        f"""    <Placemark>
      <name>{_xml(r['name'])}</name>
      <description>Source: {_xml(r['source'])} | visited: no</description>
      <styleUrl>#todo</styleUrl>
      <Point><coordinates>{round(r['lon'], 6)},{round(r['lat'], 6)},0</coordinates></Point>
    </Placemark>""" for r in rows)
    # color is KML aabbggrr (opaque red). Import into CoMaps as a bookmark list.
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Sheffield Playparks</name>
    <Style id="todo"><IconStyle><color>ff3643f4</color><Icon><href>http://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon></IconStyle></Style>
{placemarks}
  </Document>
</kml>"""
    with open(os.path.join(HERE, "sheffield_playgrounds.kml"), "w") as fh:
        fh.write(kml)


# --- Geometry / helpers (kept at the bottom) --------------------------------

def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    R = 6371000.0
    rad = math.radians
    dlat = rad(lat2 - lat1)
    dlon = rad(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _near(point, others, metres):
    """True if `point` is within `metres` of any item in `others`."""
    return any(_haversine(point["lat"], point["lon"], o["lat"], o["lon"]) < metres
               for o in others)


def _self_dedupe(points, metres):
    """Greedily drop points within `metres` of one already kept."""
    kept = []
    for p in points:
        if not _near(p, kept, metres):
            kept.append(p)
    return kept


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    main()
