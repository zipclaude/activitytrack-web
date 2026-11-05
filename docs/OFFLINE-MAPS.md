# Offline maps (issue #155 of the app)

The app's track map draws the movements over the OpenStreetMap tiles. With an
**offline map** it draws them on a basic map kept on the phone (roads, rivers, lakes,
borders, place names; a few MB for 100 km around a town), with no network: the
tiles of `tile.openstreetmap.org` may not be downloaded for offline use, so the
phones get their own files, which the server prepares.

## What an administrator does

Page **Mappe** (admins only):

1. **Rendi disponibili le mappe offline ai telefoni**: the switch. Off, the phones
   receive nothing and remove the maps they got from here; the areas stay prepared.
2. **Aree**: an area is a centre (latitude, longitude), a **radius** (5 to 250 km) and a
   **zoom level**. *Scegli un'area e uno zoom* opens the form, which tells as you type
   how many tiles and how many MB the map will be; or start from one of 23 areas
   (a town and 100 km around it) and change it. Saving an area starts its preparation.
3. **Zoom level**: 8 (about 1:1 600 000) to `[maps] max_zoom` (12, about 1:100 000). Level 10
   (about 1:400 000, 2-3 MB for 100 km) is enough for roads and towns; 11 (about 1:200 000, 5 MB)
   adds the railway and more detail. A map larger than `[maps] max_mb` (48) is refused.
4. The row shows the state (*in preparazione* with its progress, *pronta*, *non riuscita*
   with the reason), the weight and when it was built. **Aggiorna** prepares it again;
   if a build fails the previous file stays on offer. **Elimina** removes the area and its file.

The phones take the maps with their next sync (within 15 minutes, at once if the app is
open), on Wi-Fi; a map you delete or switch off goes away from them at the next sync.

## How a map is made

`maps/pmtiles.py`, standard library only (the server runs on Debian packages):

1. The Protomaps basemap (OpenStreetMap data, daily builds; PMTiles) is read **by byte
   ranges** from `[maps] source`: the 100 GB of the planet are not downloaded; the
   tiles of the area, from zoom 0 to the chosen one, are, with the requests of
   neighbouring tiles merged.
2. The heavy layers are dropped (`landuse`, `pois`, `buildings`, `natural`, `landcover`):
   the green areas alone are 85 % of the bytes. A vector tile is a sequence of
   independent layer blocks, so dropping a layer is copying the others.
3. A new PMTiles file is written (gzip, identical tiles stored once) with the layers of
   what is left in its metadata and the attribution; it is read back, its size checked
   against `max_mb` and its SHA-256 computed, and only then it replaces the previous
   file, `files/<org>/maps/<area>.pmtiles`.

Measured on 100 km around Trento: z0-10 is 12.9 MB as extracted and **2.3 MB** after step 2;
z0-11 is 27 MB and 4.9 MB.

The work runs in a process of its own (`manage.py build_maps --area N`) started by the
page, one at a time (a lock file in the data folder); an area is *in coda* until its turn.
A build that does not end in 45 minutes (the server restarted) is shown as failed and can
be started again. The log is `maps-build.log` in the data folder.

```sh
activitytrack-web-manage build_maps --area 3            # one area
activitytrack-web-manage build_maps --older-than 30     # the ones built more than 30 days ago, or never
activitytrack-web-manage build_maps --all --source /srv/planet.pmtiles
```

`--older-than` is for a timer or cron job: Protomaps builds every day, a map changes slowly.

## What the phones are told

`head` carries `mapsRev`, the fingerprint of the catalog; `GET maps` lists the maps
(`id`, name, file, bytes, sha256, bbox, maxZoom, updated) and `GET maps/<id>` serves a
file. See `docs/SYNC.md` ("Maps"). The files live under the files folder, so the
`[files] offload` setting (nginx `X-Accel-Redirect`, Apache `X-Sendfile`) applies to them too.

## Without the server

A phone not paired with a server takes the same `.pmtiles` files from the `maps` folder
of its data folder (Settings → Location → Offline maps → *Import from the data folder*).
`scripts/build-offline-maps.py` in the app's repository makes such files from the same
Protomaps data, with the same layers dropped.

## Licences

Map data © OpenStreetMap contributors (ODbL); basemap schema and daily builds:
[Protomaps](https://protomaps.com/). Both are in the attribution of each file and of the map.
Be gentle with the source: an area is a few hundred requests, once; do not build in a loop.
