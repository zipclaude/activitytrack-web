# SPDX-License-Identifier: GPL-3.0-or-later
import gzip
import http.server
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import Organization, Profile, User
from sync import tests as sync_tests

from . import build, catalog, pmtiles, tiles
from .models import MapArea


def layer(name, payload=b""):
    body = b"\x0a" + pmtiles.write_varint(len(name)) + name.encode() + payload + b"\x78\x02"
    return b"\x1a" + pmtiles.write_varint(len(body)) + body


def mvt(*names, filler=100):
    return b"".join(layer(n, bytes(filler)) for n in names)


def make_archive(path, box=(9.8, 45.1, 12.5, 47.0), max_zoom=4, layers=("earth", "landuse", "roads", "pois")):
    blobs = {tiles.tile_id(z, x, y): mvt(*layers) for z, x, y in tiles.tiles_of(list(box), max_zoom)}
    pmtiles.write(str(path), blobs, list(box), 0, max_zoom, {
        "name": "test", "vector_layers": [{"id": n} for n in layers], "tilestats": {"x": 1},
    })
    return path


class TilesTest(SimpleTestCase):
    def test_tile_ids_follow_the_spec(self):
        # PMTiles: z0 is 0, then the Hilbert order of each zoom
        self.assertEqual([tiles.tile_id(0, 0, 0), tiles.tile_id(1, 0, 0), tiles.tile_id(1, 0, 1), tiles.tile_id(1, 1, 1), tiles.tile_id(1, 1, 0)], [0, 1, 2, 3, 4])
        self.assertEqual(tiles.tile_id(2, 0, 0), 5)

    def test_the_bbox_is_wider_in_longitude_away_from_the_equator(self):
        b = tiles.bbox(46.0, 11.0, 100)
        self.assertAlmostEqual(b[3] - b[1], 2 * 100 / 111.32, places=3)
        self.assertGreater(b[2] - b[0], b[3] - b[1])

    def test_counts_match_a_known_area(self):
        # 100 km around Trento: measured with the build script (109 tiles to z10, 365 to z11)
        box = tiles.bbox(46.07, 11.12, 100)
        self.assertEqual(tiles.count(box, 10), 109)
        self.assertEqual(tiles.count(box, 11), 365)
        self.assertEqual(len(list(tiles.tiles_of(box, 10))), 109)

    def test_estimate_is_near_the_measured_sizes(self):
        _, size10 = tiles.estimate(46.07, 11.12, 100, 10)
        _, size11 = tiles.estimate(46.07, 11.12, 100, 11)
        self.assertTrue(1.7e6 < size10 < 2.9e6, size10)
        self.assertTrue(4.0e6 < size11 < 5.8e6, size11)

    def test_poles_and_the_date_line_do_not_break_the_range(self):
        for lat, lon in ((84.9, 0), (-84.9, 179.9), (0, -179.9)):
            self.assertGreater(tiles.count(tiles.bbox(lat, lon, 200), 8), 0)


class PMTilesTest(SimpleTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_written_archives_read_back(self):
        path = make_archive(self.dir / "a.pmtiles")
        a = pmtiles.Archive(pmtiles.FileSource(str(path)))
        self.assertEqual((a.min_zoom, a.max_zoom), (0, 4))
        self.assertEqual(pmtiles.layer_names(a.tile(tiles.tile_id(0, 0, 0))), ["earth", "landuse", "roads", "pois"])
        self.assertEqual(pmtiles.verify(str(path))["roads"] > 0, True)

    def test_a_tile_that_is_not_there_is_none(self):
        a = pmtiles.Archive(pmtiles.FileSource(str(make_archive(self.dir / "a.pmtiles", box=(10, 45, 11, 46)))))
        self.assertIsNone(a.locate(tiles.tile_id(4, 0, 0)))
        self.assertIsNone(a.tile(tiles.tile_id(4, 15, 15)))

    def test_extract_cuts_the_area_and_drops_the_layers(self):
        src = make_archive(self.dir / "world.pmtiles", box=(-180, -85, 180, 85), max_zoom=4)
        out = self.dir / "area.pmtiles"
        box = tiles.bbox(46.07, 11.12, 100)
        n = pmtiles.extract(pmtiles.Archive(pmtiles.FileSource(str(src))), box, 4, str(out))
        self.assertEqual(n, tiles.count(box, 4))
        a = pmtiles.Archive(pmtiles.FileSource(str(out)))
        self.assertEqual(pmtiles.layer_names(a.tile(tiles.tile_id(0, 0, 0))), ["earth", "roads"])
        self.assertEqual(sorted(pmtiles.verify(str(out))), ["earth", "roads"])
        meta = a.metadata()
        self.assertEqual([v["id"] for v in meta["vector_layers"]], ["earth", "roads"])
        self.assertEqual(meta["attribution"], pmtiles.ATTRIBUTION)
        self.assertNotIn("tilestats", meta)
        self.assertLess(out.stat().st_size, src.stat().st_size)
        self.assertAlmostEqual(a.bounds[0], box[0], places=4)

    def test_extract_is_repeatable(self):
        src = make_archive(self.dir / "world.pmtiles", box=(-180, -85, 180, 85), max_zoom=3)
        box = tiles.bbox(46.07, 11.12, 100)
        for name in ("a", "b"):
            pmtiles.extract(pmtiles.Archive(pmtiles.FileSource(str(src))), box, 3, str(self.dir / f"{name}.pmtiles"))
        self.assertEqual(pmtiles.sha256_file(str(self.dir / "a.pmtiles")), pmtiles.sha256_file(str(self.dir / "b.pmtiles")))

    def test_an_area_the_archive_does_not_cover_is_refused(self):
        # tiles of zoom 6 around Trento only: nothing of the Andes, and no low zoom to stand in for it
        box = tiles.bbox(46.07, 11.12, 100)
        blobs = {tiles.tile_id(z, x, y): mvt("earth") for z, x, y in tiles.tiles_of(box, 6) if z == 6}
        pmtiles.write(str(self.dir / "a.pmtiles"), blobs, box, 6, 6, {})
        src = pmtiles.Archive(pmtiles.FileSource(str(self.dir / "a.pmtiles")))
        with self.assertRaises(pmtiles.PMTilesError):
            pmtiles.extract(src, [-100, -60, -90, -50], 6, str(self.dir / "x.pmtiles"))
        self.assertFalse((self.dir / "x.pmtiles").exists())

    def test_identical_tiles_are_stored_once(self):
        blobs = {tiles.tile_id(2, x, y): mvt("earth") for x in range(4) for y in range(4)}
        pmtiles.write(str(self.dir / "d.pmtiles"), blobs, [0, 0, 1, 1], 0, 2, {})
        a = pmtiles.Archive(pmtiles.FileSource(str(self.dir / "d.pmtiles")))
        self.assertEqual(a.data_len, len(pmtiles.gz(mvt("earth"))))

    def test_refuses_what_is_not_a_vector_pmtiles(self):
        p = self.dir / "bad"
        p.write_bytes(b"not an archive" + bytes(200))
        with self.assertRaises(pmtiles.PMTilesError):
            pmtiles.Archive(pmtiles.FileSource(str(p)))
        good = make_archive(self.dir / "g.pmtiles", max_zoom=1).read_bytes()
        raster = bytearray(good)
        raster[99] = 2  # tile type PNG
        p.write_bytes(bytes(raster))
        with self.assertRaises(pmtiles.PMTilesError):
            pmtiles.Archive(pmtiles.FileSource(str(p)))
        zstd = bytearray(good)
        zstd[98] = 4
        p.write_bytes(bytes(zstd))
        with self.assertRaises(pmtiles.PMTilesError):
            pmtiles.Archive(pmtiles.FileSource(str(p)))

    def test_a_truncated_archive_is_an_error_not_garbage(self):
        good = make_archive(self.dir / "g.pmtiles", max_zoom=3).read_bytes()
        (self.dir / "t.pmtiles").write_bytes(good[:len(good) - 50])
        a = pmtiles.Archive(pmtiles.FileSource(str(self.dir / "t.pmtiles")))
        with self.assertRaises(pmtiles.PMTilesError):
            for tid in a.root[0]:
                a.tile(tid)

    def test_strip_keeps_unknown_fields(self):
        tile = mvt("a", "b") + b"\x08\x05"  # an extra varint field
        self.assertEqual(pmtiles.strip_layers(tile, {"a"}), mvt("b") + b"\x08\x05")


class RangeHandler(http.server.BaseHTTPRequestHandler):
    """A static file server that answers byte ranges (the Protomaps builds are served so)."""

    path_on_disk = None
    ranges = 0

    def do_GET(self):
        data = Path(self.path_on_disk).read_bytes()
        rng = self.headers.get("Range")
        if not rng:
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        a, b = rng.split("=")[1].split("-")
        a, b = int(a), min(int(b), len(data) - 1)
        type(self).ranges += 1
        self.send_response(206)
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
        self.end_headers()
        self.wfile.write(data[a:b + 1])

    def log_message(self, *args):
        pass


class NoRangeHandler(RangeHandler):
    def do_GET(self):
        data = Path(self.path_on_disk).read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class RemoteTest(SimpleTestCase):
    def serve(self, handler, path):
        handler = type("H", (handler,), {"path_on_disk": str(path), "ranges": 0})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_address[1]}/world.pmtiles", handler

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.world = make_archive(self.dir / "world.pmtiles", box=(-180, -85, 180, 85), max_zoom=5)

    def test_extract_reads_only_ranges_of_a_remote_archive(self):
        url, handler = self.serve(RangeHandler, self.world)
        box = tiles.bbox(46.07, 11.12, 100)
        n = pmtiles.extract(pmtiles.Archive(pmtiles.source_for(url)), box, 5, str(self.dir / "out.pmtiles"))
        self.assertEqual(n, tiles.count(box, 5))
        local = self.dir / "local.pmtiles"
        pmtiles.extract(pmtiles.Archive(pmtiles.FileSource(str(self.world))), box, 5, str(local))
        self.assertEqual(pmtiles.sha256_file(str(self.dir / "out.pmtiles")), pmtiles.sha256_file(str(local)))
        # neighbouring tiles are read together: far fewer requests than tiles
        self.assertLess(handler.ranges, n)

    def test_a_server_without_ranges_is_refused_not_downloaded_whole(self):
        url, _ = self.serve(NoRangeHandler, self.world)
        with self.assertRaisesRegex(pmtiles.PMTilesError, "byte ranges"):
            pmtiles.Archive(pmtiles.source_for(url))


class CatalogTestCase(sync_tests.SyncTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        patcher = override_settings(FILES_DIR=self.tmp / "files", DATA_DIR=self.tmp)
        patcher.enable()
        self.addCleanup(patcher.disable)

    def area(self, slug="trento", ready=True, org=None, max_zoom=4, **over):
        """An area; ready: with a (small) file built for it, as if the build had run."""
        org = org or self.org
        a = MapArea.objects.create(org=org, slug=slug, name=slug.title(), lat=46.07, lon=11.12, radius_km=100, max_zoom=max_zoom, **over)
        if ready:
            make_archive(a.path, max_zoom=4)
            MapArea.objects.filter(pk=a.pk).update(
                state=MapArea.READY, size=a.path.stat().st_size, sha256=pmtiles.sha256_file(str(a.path)),
                built_bbox=a.bbox, built_zoom=max_zoom, built=sync_tests.timezone.now())
        a.refresh_from_db()
        return a

    def offer(self, on=True):
        self.org.offline_maps = on
        self.org.save()


class PhoneApiTest(CatalogTestCase):
    def auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def test_nothing_is_offered_unless_the_organisation_turns_it_on(self):
        self.area()
        token = self.pair(self.alice)
        r = self.client.get("/api/v1/maps", **self.auth(token))
        self.assertEqual(r.json(), {"schema": 1, "rev": "", "maps": []})
        self.assertEqual(self.client.get("/api/v1/sync/head", **self.auth(token)).json()["mapsRev"], "")
        self.assertEqual(self.client.get("/api/v1/maps/trento", **self.auth(token)).status_code, 404)

    def test_the_catalog_lists_the_good_areas(self):
        a = self.area()
        self.area("rotta", ready=False)
        self.offer()
        token = self.pair(self.alice)
        doc = self.client.get("/api/v1/maps", **self.auth(token)).json()
        self.assertEqual(len(doc["maps"]), 1)
        m = doc["maps"][0]
        self.assertEqual((m["id"], m["file"], m["bytes"], m["sha256"], m["maxZoom"]), ("trento", "trento.pmtiles", a.size, a.sha256, 4))
        self.assertEqual(m["bbox"], a.built_bbox)
        self.assertEqual(self.client.get("/api/v1/sync/head", **self.auth(token)).json()["mapsRev"], doc["rev"])

    def test_the_revision_changes_with_the_files(self):
        self.area()
        self.offer()
        before = catalog.revision(self.org)
        a = MapArea.objects.get(slug="trento")
        MapArea.objects.filter(pk=a.pk).update(sha256="f" * 64)
        self.assertNotEqual(catalog.revision(self.org), before)

    def test_the_file_is_served_with_its_bytes(self):
        a = self.area()
        self.offer()
        r = self.client.get("/api/v1/maps/trento", **self.auth(self.pair(self.alice)))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), a.path.read_bytes())
        self.assertIn("trento.pmtiles", r["Content-Disposition"])

    def test_the_file_goes_through_the_offload_when_it_is_on(self):
        self.area()
        self.offer()
        token = self.pair(self.alice)
        with self.settings(FILES_OFFLOAD="nginx", FILES_ACCEL_LOCATION="/_files/"):
            r = self.client.get("/api/v1/maps/trento", **self.auth(token))
        self.assertEqual(r["X-Accel-Redirect"], f"/_files/{self.org.pk}/maps/trento.pmtiles")
        with self.settings(FILES_OFFLOAD="apache"):
            r = self.client.get("/api/v1/maps/trento", **self.auth(token))
        self.assertTrue(r["X-Sendfile"].endswith("trento.pmtiles"))

    def test_an_organisation_sees_only_its_own_maps(self):
        other = Organization.objects.create(name="Altra")
        self.area("altrui", org=other)
        other.offline_maps = True
        other.save()
        self.offer()
        token = self.pair(self.alice)
        self.assertEqual(self.client.get("/api/v1/maps", **self.auth(token)).json()["maps"], [])
        self.assertEqual(self.client.get("/api/v1/maps/altrui", **self.auth(token)).status_code, 404)

    def test_a_missing_file_is_not_offered(self):
        a = self.area()
        self.offer()
        a.path.unlink()
        self.assertEqual(catalog.entries(self.org), [])

    def test_no_token_no_maps(self):
        self.assertEqual(self.client.get("/api/v1/maps").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/maps/trento").status_code, 401)

    def test_the_slug_cannot_leave_the_folder(self):
        self.area()
        self.offer()
        token = self.pair(self.alice)
        for bad in ("..%2Fx", "a.b", "%2e%2e"):
            self.assertEqual(self.client.get(f"/api/v1/maps/{bad}", **self.auth(token)).status_code, 404)


class PageTest(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def post_area(self, **over):
        data = {"name": "Trento", "lat": "46.07", "lon": "11.12", "radius_km": "100", "max_zoom": "10"}
        data.update(over)
        return self.client.post("/mappe/nuova/", data)

    def test_only_admins_see_the_page(self):
        self.client.force_login(self.alice)
        for url in ("/mappe/", "/mappe/nuova/", "/mappe/stato/", "/mappe/stima/?lat=1&lon=1&radius_km=10&max_zoom=8"):
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(self.client.post("/mappe/abilita/", {"offline_maps": "on"}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get("/mappe/").status_code, 302)

    def test_the_page_lists_areas_and_presets(self):
        self.area()
        r = self.client.get("/mappe/")
        self.assertContains(r, "Trento")
        self.assertContains(r, "Torino e Piemonte")
        self.assertNotContains(r, 'value="trento"')

    def test_the_switch_turns_the_offer_on_and_off(self):
        self.client.post("/mappe/abilita/", {"offline_maps": "on"})
        self.org.refresh_from_db()
        self.assertTrue(self.org.offline_maps)
        self.client.post("/mappe/abilita/", {})
        self.org.refresh_from_db()
        self.assertFalse(self.org.offline_maps)

    def test_a_new_area_is_saved_and_its_preparation_starts(self):
        with mock.patch.object(build, "start") as start:
            r = self.post_area()
        self.assertRedirects(r, "/mappe/")
        a = MapArea.objects.get()
        self.assertEqual((a.slug, a.org, a.max_zoom, a.radius_km), ("trento", self.org, 10, 100))
        start.assert_called_once_with(a)

    def test_two_areas_with_one_name_get_two_files(self):
        with mock.patch.object(build, "start"):
            self.post_area()
            self.post_area()
        self.assertEqual(sorted(MapArea.objects.values_list("slug", flat=True)), ["trento", "trento-2"])

    def test_a_preset_prefills_the_form(self):
        r = self.client.get("/mappe/nuova/?preset=roma")
        self.assertContains(r, "Roma e Lazio")
        self.assertContains(r, 'name="slug" value="roma"')

    def test_bad_values_are_refused_with_the_field(self):
        cases = {
            "lat": ("91", "latitudine"), "lon": ("181", "longitudine"), "radius_km": ("3", "raggio"),
            "max_zoom": ("20", "zoom"), "name": ("", "nome"),
        }
        with mock.patch.object(build, "start") as start:
            for field, (value, word) in cases.items():
                r = self.post_area(**{field: value})
                self.assertEqual(r.status_code, 200, field)
                self.assertIn(field, r.context["errors"], field)
        start.assert_not_called()
        self.assertEqual(MapArea.objects.count(), 0)

    def test_a_map_that_would_be_too_big_is_refused(self):
        with override_settings(MAPS_MAX_MB=1), mock.patch.object(build, "start") as start:
            r = self.post_area()
        self.assertIn("max_zoom", r.context["errors"])
        start.assert_not_called()

    def test_comma_decimals_are_accepted(self):
        with mock.patch.object(build, "start"):
            self.post_area(lat="46,07", lon="11,12")
        self.assertEqual(MapArea.objects.get().lat, 46.07)

    def test_renaming_does_not_rebuild_moving_does(self):
        a = self.area(max_zoom=10)
        with mock.patch.object(build, "start") as start:
            self.client.post(f"/mappe/{a.pk}/", {"name": "Altro nome", "lat": "46.07", "lon": "11.12", "radius_km": "100", "max_zoom": "10"})
            start.assert_not_called()
            self.client.post(f"/mappe/{a.pk}/", {"name": "Altro nome", "lat": "46.07", "lon": "11.12", "radius_km": "120", "max_zoom": "10"})
            start.assert_called_once()
        a.refresh_from_db()
        self.assertEqual((a.name, a.radius_km), ("Altro nome", 120))
        # the file on offer is the one built, whatever the form says now
        self.assertEqual(a.built_bbox, tiles.bbox(46.07, 11.12, 100))

    def test_an_area_of_another_organisation_is_not_reachable(self):
        other = Organization.objects.create(name="Altra")
        a = self.area("altrui", org=other)
        for url in (f"/mappe/{a.pk}/", ):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(f"/mappe/{a.pk}/elimina/").status_code, 404)
        self.assertEqual(self.client.post(f"/mappe/{a.pk}/prepara/").status_code, 404)

    def test_deleting_removes_the_file(self):
        a = self.area()
        path = a.path
        self.assertTrue(path.exists())
        self.client.post(f"/mappe/{a.pk}/elimina/")
        self.assertFalse(path.exists())
        self.assertEqual(MapArea.objects.count(), 0)

    def test_rebuilding_a_busy_area_is_not_started_twice(self):
        a = self.area(ready=False, state=MapArea.BUILDING, started=sync_tests.timezone.now())
        with mock.patch.object(build, "start") as start:
            self.client.post(f"/mappe/{a.pk}/prepara/")
        start.assert_not_called()

    def test_an_interrupted_build_is_shown_as_failed_and_can_be_started_again(self):
        old = sync_tests.timezone.now() - sync_tests.timedelta(hours=2)
        a = self.area(ready=False, state=MapArea.BUILDING, started=old)
        self.assertTrue(a.stale)
        self.assertFalse(a.busy)
        s = self.client.get("/mappe/stato/").json()
        self.assertEqual(s["areas"][0]["state"], "stale")
        self.assertFalse(s["busy"])
        with mock.patch.object(build, "start") as start:
            self.client.post(f"/mappe/{a.pk}/prepara/")
        start.assert_called_once()

    def test_the_estimate_is_json(self):
        r = self.client.get("/mappe/stima/?lat=46.07&lon=11.12&radius_km=100&max_zoom=10")
        d = r.json()
        self.assertEqual((d["tiles"], d["scale"], d["too_big"]), (109, 409000, False))
        self.assertEqual(self.client.get("/mappe/stima/?lat=x&lon=1&radius_km=1&max_zoom=1").status_code, 400)


class BuildTest(CatalogTestCase):
    def setUp(self):
        super().setUp()
        self.world = make_archive(self.tmp / "world.pmtiles", box=(-180, -85, 180, 85), max_zoom=5)

    def test_building_an_area_makes_a_ready_file(self):
        a = self.area(ready=False, max_zoom=5)
        with override_settings(MAPS_SOURCE=str(self.world)):
            done = build.build(a.pk)
        self.assertEqual(done.state, MapArea.READY)
        self.assertEqual(done.sha256, pmtiles.sha256_file(str(done.path)))
        self.assertEqual(done.size, done.path.stat().st_size)
        self.assertEqual((done.built_zoom, done.built_bbox), (5, done.bbox))
        self.assertEqual(done.source, "world.pmtiles")
        self.assertEqual(sorted(pmtiles.verify(str(done.path))), ["earth", "roads"])
        self.assertFalse(done.path.with_suffix(".new").exists())

    def test_a_failed_build_keeps_the_previous_file(self):
        a = self.area()
        good = a.sha256
        with override_settings(MAPS_SOURCE=str(self.tmp / "nowhere.pmtiles")), self.assertRaises(Exception):
            build.build(a.pk)
        a.refresh_from_db()
        self.assertEqual((a.state, a.sha256), (MapArea.FAILED, good))
        self.assertTrue(a.message)
        self.assertEqual(pmtiles.sha256_file(str(a.path)), good)
        self.assertTrue(a.usable)

    def test_a_map_over_the_limit_is_not_put_in_place(self):
        a = self.area(ready=False, max_zoom=5)
        with override_settings(MAPS_SOURCE=str(self.world), MAPS_MAX_MB=0), self.assertRaises(pmtiles.PMTilesError):
            build.build(a.pk)
        a.refresh_from_db()
        self.assertEqual(a.state, MapArea.FAILED)
        self.assertIn("limite", a.message)
        self.assertFalse(a.path.exists())
        self.assertFalse(a.path.with_suffix(".new").exists())

    def test_the_command_builds_one_area_and_the_old_ones(self):
        a = self.area(ready=False, max_zoom=5)
        self.offer()
        with override_settings(MAPS_SOURCE=str(self.world)):
            call_command("build_maps", "--area", str(a.pk))
            a.refresh_from_db()
            self.assertEqual(a.state, MapArea.READY)
            first = a.built
            call_command("build_maps", "--older-than", "1")  # built just now: left alone
            a.refresh_from_db()
            self.assertEqual(a.built, first)
            call_command("build_maps", "--older-than", "0")
            a.refresh_from_db()
            self.assertGreaterEqual(a.built, first)

    def test_the_label_of_a_daily_build_is_its_date(self):
        self.assertEqual(build.source_label("https://build.protomaps.com/20261104.pmtiles"), "2026-11-04")
        self.assertEqual(build.source_label("/srv/planet.pmtiles"), "planet.pmtiles")
