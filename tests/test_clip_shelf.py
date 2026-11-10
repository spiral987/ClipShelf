import hashlib
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from clip_core import Cancelled, Catalog, extract_preview, path_key


def png(color=(137, 174, 126), width=240, height=180):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            factor = .62 + .38 * ((x / width + y / height) / 2)
            row.extend(int(c * factor) for c in color)
        rows.append(b"\x00" + bytes(row))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def make_clip(path, color=(137, 174, 126)):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = png(color)
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
        db_path = Path(temp) / "preview.sqlite"
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("CREATE TABLE CanvasPreview (ImageData BLOB)")
            db.execute("INSERT INTO CanvasPreview VALUES (?)", (image,))
        sql = db_path.read_bytes()
    def chunk(kind, data):
        return kind + struct.pack(">Q", len(data)) + data
    data = b"CSFCHUNK" + bytes(16)
    data += chunk(b"CHNKHead", b"synthetic test fixture")
    data += chunk(b"CHNKExta", bytes(1024))
    data += chunk(b"CHNKSQLi", sql)
    data += chunk(b"CHNKFoot", b"")
    path.write_bytes(data)
    return image


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.home = Path(self.temp.name)
        self.root = self.home / "Illustrations"
        self.root.mkdir()
        self.catalog = Catalog(self.home / "data")
        self.catalog.add_root(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name="Artwork.clip", color=(137, 174, 126)):
        path = self.root / name
        make_clip(path, color)
        self.catalog.scan()
        return path, next(w for w in self.catalog.works() if w["path"] == path_key(path))

    def annotate(self, work):
        self.catalog.metadata(work["id"], ["創作", "完成"], "引き継ぐメモ", True)

    def test_import_and_preview_does_not_touch_source(self):
        path = self.root / "Nested" / "Artwork.CLIP"
        image = make_clip(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        stat_before = path.stat()
        result = self.catalog.scan()
        work = self.catalog.works()[0]
        self.assertEqual(result["added"], 1)
        self.assertEqual(work["name"], "Artwork")
        self.assertEqual(Path(work["thumb"]).read_bytes(), image)
        self.annotate(work)
        self.catalog.scan()
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(stat_before.st_mtime_ns, path.stat().st_mtime_ns)

    def test_unchanged_files_reuse_fingerprint(self):
        self.add()
        with patch("clip_core.fingerprint", side_effect=AssertionError("should reuse")):
            self.assertEqual(self.catalog.scan()["added"], 0)

    def test_move_and_rename_preserve_annotations(self):
        path, work = self.add()
        self.annotate(work)
        moved = self.root / "Archive" / "Renamed.clip"
        moved.parent.mkdir()
        path.rename(moved)
        stats = self.catalog.scan()
        updated = self.catalog.works()[0]
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(len(self.catalog.works()), 1)
        self.assertEqual(updated["id"], work["id"])
        self.assertEqual(updated["tags"], ["創作", "完成"])
        self.assertEqual(updated["notes"], "引き継ぐメモ")
        self.assertTrue(updated["favorite"])

    def test_moved_root_is_reconnected_when_new_folder_is_registered(self):
        path, work = self.add()
        self.annotate(work)
        new_root = self.home / "Elsewhere"
        self.root.rename(new_root)
        self.catalog.add_root(new_root)
        self.assertEqual(self.catalog.scan()["moved"], 1)
        self.assertEqual(self.catalog.works()[0]["id"], work["id"])

    def test_existing_identical_copy_remains_separate(self):
        path, work = self.add()
        self.annotate(work)
        shutil.copyfile(path, self.root / "Copy.clip")
        stats = self.catalog.scan()
        self.assertEqual(stats["moved"], 0)
        self.assertEqual(len(self.catalog.works()), 2)
        self.assertEqual(sum(bool(w["tags"]) for w in self.catalog.works()), 1)

    def test_ambiguous_copies_are_not_auto_merged(self):
        first, work = self.add()
        shutil.copyfile(first, self.root / "Copy.clip")
        self.catalog.scan()
        self.annotate(work)
        first.rename(self.root / "MovedA.clip")
        (self.root / "Copy.clip").rename(self.root / "MovedB.clip")
        stats = self.catalog.scan()
        self.assertEqual(stats["moved"], 0)
        self.assertEqual(stats["ambiguous"], 2)
        self.assertEqual(len(self.catalog.works()), 4)

    def test_edit_in_place_preserves_metadata(self):
        path, work = self.add()
        self.annotate(work)
        make_clip(path, (180, 80, 100))
        result = self.catalog.scan()
        updated = self.catalog.works()[0]
        self.assertEqual(result["updated"], 1)
        self.assertNotEqual(updated["digest"], work["digest"])
        self.assertEqual(updated["tags"], ["創作", "完成"])

    def test_edited_move_can_be_manually_relinked_after_scan(self):
        path, work = self.add()
        self.annotate(work)
        moved = self.root / "Edited.clip"
        path.rename(moved)
        make_clip(moved, (80, 100, 180))
        self.assertEqual(self.catalog.scan()["moved"], 0)
        self.assertEqual(len(self.catalog.works()), 2)
        self.catalog.relink(work["id"], moved)
        works = self.catalog.works()
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]["id"], work["id"])
        self.assertEqual(works[0]["notes"], "引き継ぐメモ")
        self.assertEqual(works[0]["status"], "present")

    def test_manual_relink_does_not_drop_other_annotations(self):
        path, original = self.add()
        other_path, other = self.add("Other.clip", (160, 90, 110))
        self.annotate(other)
        with self.assertRaises(ValueError):
            self.catalog.relink(original["id"], other_path)
        self.assertEqual(len(self.catalog.works()), 2)
        self.assertTrue(next(w for w in self.catalog.works() if w["id"] == other["id"])["tags"])

    def test_cancel_leaves_database_unchanged(self):
        _, work = self.add()
        self.annotate(work)
        make_clip(self.root / "New.clip", (200, 170, 100))
        before = self.catalog.works()
        event = threading.Event()
        def callback(kind, text):
            if "作品を確認中" in text:
                event.set()
        with self.assertRaises(Cancelled):
            self.catalog.scan(callback, event)
        self.assertEqual(before, self.catalog.works())

    def test_unreadable_root_does_not_merge_into_copy(self):
        path, work = self.add()
        self.annotate(work)
        other = self.home / "Other"
        other.mkdir()
        shutil.copyfile(path, other / "Copy.clip")
        self.catalog.add_root(other)
        real_stat = Path.stat
        def guarded_stat(value, *args, **kwargs):
            if path_key(value) == path_key(self.root):
                raise PermissionError("test unreadable root")
            return real_stat(value, *args, **kwargs)
        with patch.object(Path, "stat", guarded_stat):
            result = self.catalog.scan()
        self.assertEqual(result["moved"], 0)
        works = self.catalog.works()
        self.assertEqual(len(works), 2)
        self.assertEqual(next(w for w in works if w["id"] == work["id"])["status"], "unreadable")

    def test_offline_drive_does_not_merge_into_copy(self):
        path, work = self.add()
        other = self.home / "Other"
        other.mkdir()
        shutil.copyfile(path, other / "Copy.clip")
        self.catalog.add_root(other)
        real_stat = Path.stat
        real_is_dir = Path.is_dir
        def guarded_stat(value, *args, **kwargs):
            if path_key(value) == path_key(self.root):
                raise FileNotFoundError("disconnected drive")
            return real_stat(value, *args, **kwargs)
        def guarded_dir(value):
            if str(value) == Path(path_key(self.root)).anchor:
                return False
            return real_is_dir(value)
        with patch.object(Path, "stat", guarded_stat), patch.object(Path, "is_dir", guarded_dir):
            result = self.catalog.scan()
        self.assertEqual(result["moved"], 0)
        self.assertEqual(next(w for w in self.catalog.works() if w["id"] == work["id"])["status"], "offline")

    def test_backup_contains_metadata_and_original_remains_intact(self):
        _, work = self.add()
        self.annotate(work)
        destination = self.home / "backup.sqlite"
        self.catalog.backup(destination)
        with closing(sqlite3.connect(destination)) as db:
            self.assertEqual(db.execute("SELECT notes FROM works").fetchone()[0], "引き継ぐメモ")

    def test_backup_refuses_artwork_extension(self):
        path, _ = self.add()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.catalog.backup(path)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_clip_is_still_listed_with_preview_error(self):
        (self.root / "Unsupported.clip").write_bytes(b"not a clip file")
        self.catalog.scan()
        work = self.catalog.works()[0]
        self.assertEqual(work["status"], "present")
        self.assertTrue(work["preview_error"])
        self.assertEqual(work["thumb"], "")

    def test_overlapping_roots_are_deduplicated(self):
        nested = self.root / "Nested"
        make_clip(nested / "One.clip")
        self.catalog.add_root(nested)
        self.catalog.scan()
        self.assertEqual(len(self.catalog.works()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
