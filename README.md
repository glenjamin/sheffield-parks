# Sheffield Playparks

A personal tracker for visiting every playpark in Sheffield and ticking them
off as I go.

The [Sheffield City Council play-area map][council-map] only shows the ~118
playgrounds the council *manages* — it misses estate, informal and commercial
ones (the playground on the Hanover estate, for example). This repo merges the
council's open data with [OpenStreetMap][osm] to get a fuller list:

| Source | Count | Notes |
| --- | --- | --- |
| Council | 118 | Authoritative names, council-managed sites |
| OSM only | 13 | Named in OSM but not a council site (estate/commercial parks) |
| OSM unnamed | 70 | Mapped in OSM without a name; labelled by nearest street & area |
| **Total** | **201** | Within 10 miles of the city centre |

## Files

- **`sheffield_playgrounds.kml`** — import into [CoMaps][comaps] / Organic Maps
  as a bookmark list. Works offline; tick off by recolouring a pin or moving it
  to a "Visited" list.
- **`sheffield_playgrounds.geojson`** — import into [uMap][umap]. Pins start red
  (`visited=no`); add a conditional style `visited=yes` → green so ticking one
  off recolours it.
- **`sheffield_playgrounds.csv`** — the master list / spreadsheet. Columns:
  `name, lat, lon, source, visited, date_visited, notes`.

## Rebuilding

```sh
python3 build_playgrounds.py
```

Pure Python stdlib, no dependencies — but needs network access, so run it
outside any sandbox. The reverse-geocoding step is rate-limited to ~1 req/sec
(Nominatim policy), so a full run takes ~90s. Data sources and the merge logic
are documented in the script's header.

Results drift over time as OSM is edited — e.g. unnamed playgrounds gain names.

## Adding playground names back to OSM

Many of the 70 "unnamed" ones genuinely have a name on a sign that OSM doesn't
know yet. When visiting, add it with [Every Door][everydoor] (iOS/Android) or
the in-app OSM editor in CoMaps. Only add what's actually on the ground — don't
copy from Google Maps.

[council-map]: https://sheffieldcc.maps.arcgis.com/apps/instant/sidebar/index.html?appid=5dbfc04cd9564cb3a10a2af4d4c81796
[osm]: https://www.openstreetmap.org
[comaps]: https://comaps.app
[umap]: https://umap.openstreetmap.fr
[everydoor]: https://everydoor.app
