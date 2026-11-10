"""Read-only .clip catalog. No operation writes to an artwork file."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path


class Cancelled(Exception):
    pass


def path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fingerprint(path, cancel=None):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        while True:
            if cancel and cancel.is_set():
                raise Cancelled()
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = Path(path).stat()
    signature = lambda s: (s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise OSError("保存中にファイルが変化しました。保存終了後に再スキャンしてください。")
    return digest.hexdigest(), current


def extract_preview(path, cache_dir, digest):
    """Seek past large layer chunks; read only the embedded SQLite chunk."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / (digest + ".png")
    if target.is_file():
        return str(target), ""
    temporary = None
    try:
        with open(path, "rb") as stream:
            total = os.fstat(stream.fileno()).st_size
            if stream.read(8) != b"CSFCHUNK":
                raise ValueError("対応するCLIP形式のヘッダーではありません。")
            stream.seek(24)
            found = False
            while stream.tell() + 16 <= total:
                kind = stream.read(8)
                length = struct.unpack(">Q", stream.read(8))[0]
                if length > total - stream.tell():
                    raise ValueError("ファイルの保存が完了していないか、チャンクが不正です。")
                if kind != b"CHNKSQLi":
                    stream.seek(length, os.SEEK_CUR)
                    continue
                if length > 256 * 1024 * 1024:
                    raise ValueError("埋め込みデータが大きいため、プレビュー取得を省略しました。")
                with tempfile.NamedTemporaryFile(suffix=".sqlite", dir=cache_dir, delete=False) as out:
                    temporary = Path(out.name)
                    remaining = length
                    while remaining:
                        part = stream.read(min(1024 * 1024, remaining))
                        if not part:
                            raise ValueError("埋め込みデータが途中で終了しています。")
                        out.write(part)
                        remaining -= len(part)
                found = True
                break
        if not found:
            raise ValueError("このファイルからプレビューデータを検出できませんでした。")
        with closing(sqlite3.connect(temporary.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT ImageData FROM CanvasPreview WHERE length(ImageData) > 0 "
                "ORDER BY length(ImageData) DESC LIMIT 1"
            ).fetchone()
        if not row or not isinstance(row[0], bytes):
            raise ValueError("保存されたプレビュー画像がありません。")
        image = row[0]
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("PNG以外のプレビューにはまだ対応していません。")
        if len(image) > 32 * 1024 * 1024:
            raise ValueError("プレビュー画像が大きすぎます。")
        pending = target.with_suffix(".pending")
        pending.write_bytes(image)
        os.replace(pending, target)
        return str(target), ""
    except (OSError, ValueError, sqlite3.Error, struct.error) as exc:
        return "", str(exc)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


class Catalog:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "previews"
        self.cache_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "catalog.sqlite"
        self.scan_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS roots (
                    path TEXT PRIMARY KEY, added TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS works (
                    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, root TEXT NOT NULL,
                    digest TEXT NOT NULL, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, thumb TEXT NOT NULL DEFAULT '',
                    preview_error TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'present',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
                    added TEXT NOT NULL, updated TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS works_digest ON works(digest);
                CREATE TABLE IF NOT EXISTS history (
                    work_id TEXT NOT NULL, old_path TEXT NOT NULL,
                    new_path TEXT NOT NULL, changed TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def roots(self):
        with closing(self.connect()) as db:
            return [r[0] for r in db.execute("SELECT path FROM roots ORDER BY added, path")]

    def add_root(self, path):
        path = path_key(path)
        if not Path(path).is_dir():
            raise ValueError("読み取り可能なフォルダを選択してください。")
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO roots VALUES (?, ?)", (path, timestamp()))
        return path

    def remove_root(self, path):
        # Keep all catalog entries, tags, notes, and previews.
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM roots WHERE path=?", (path_key(path),))

    def works(self):
        with closing(self.connect()) as db:
            result = [dict(r) for r in db.execute("SELECT * FROM works")]
        for item in result:
            item["tags"] = json.loads(item["tags"])
        return result

    def metadata(self, work_id, tags, notes, favorite):
        tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        with closing(self.connect()) as db, db:
            db.execute("UPDATE works SET tags=?, notes=?, favorite=? WHERE id=?",
                       (json.dumps(tags, ensure_ascii=False), notes, int(favorite), work_id))

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination.suffix.lower() != ".sqlite":
            raise ValueError("バックアップの拡張子は .sqlite にしてください。")
        if destination == self.db_path or (destination.exists() and os.path.samefile(destination, self.db_path)):
            raise ValueError("管理データ本体とは別の場所を選択してください。")
        with closing(self.connect()) as source, closing(sqlite3.connect(destination)) as out:
            source.backup(out)

    def scan(self, notify=None, cancel=None):
        if not self.scan_lock.acquire(blocking=False):
            raise ValueError("スキャンはすでに実行中です。")
        try:
            return self._scan(notify or (lambda *args: None), cancel or threading.Event())
        finally:
            self.scan_lock.release()

    def _scan(self, notify, cancel):
        old = self.works()
        by_path = {r["path"]: r for r in old}
        roots = self.roots()
        discovered = {}
        root_state = {}
        errors = []
        for root in roots:
            if cancel.is_set():
                raise Cancelled()
            root_state[root] = "complete"
            try:
                Path(root).stat()
            except (FileNotFoundError, NotADirectoryError):
                # A removed folder on a connected volume differs from an unplugged drive.
                try:
                    root_state[root] = "missing" if Path(Path(root).anchor).is_dir() else "offline"
                except OSError:
                    root_state[root] = "offline"
                continue
            except OSError as exc:
                root_state[root] = "error"
                errors.append(f"{root}: {exc}")
                continue

            def walk_error(exc):
                root_state[root] = "error"
                errors.append(str(exc))

            for folder, dirs, names in os.walk(root, onerror=walk_error, followlinks=False):
                if cancel.is_set():
                    raise Cancelled()
                # Avoid symlinks, junctions and cloud reparse directories.
                safe_dirs = []
                for directory in dirs:
                    directory_path = Path(folder) / directory
                    try:
                        st = directory_path.lstat()
                        if directory_path.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                            continue
                        if path_key(directory_path) == path_key(self.data_dir):
                            continue
                        safe_dirs.append(directory)
                    except OSError as exc:
                        walk_error(exc)
                dirs[:] = safe_dirs
                for name in names:
                    if name.lower().endswith(".clip"):
                        candidate = Path(folder) / name
                        if not candidate.is_symlink():
                            discovered.setdefault(path_key(candidate), (root, candidate.stem))
            notify("progress", f"フォルダを確認中 · {len(discovered):,} 件")

        # Only definitely absent paths may be reconnected automatically.
        absent = set()
        availability = {}
        for record in old:
            if record["path"] in discovered:
                continue
            if root_state.get(record["root"]) == "offline":
                availability[record["id"]] = "offline"
                continue
            if root_state.get(record["root"]) == "error":
                availability[record["id"]] = "unreadable"
                continue
            try:
                Path(record["path"]).stat()
                availability[record["id"]] = "present"
            except (FileNotFoundError, NotADirectoryError):
                availability[record["id"]] = "missing"
                absent.add(record["id"])
            except OSError:
                availability[record["id"]] = "unreadable"

        scanned = []
        for index, (path, (root, name)) in enumerate(discovered.items(), 1):
            if cancel.is_set():
                raise Cancelled()
            notify("progress", f"作品を確認中 · {index:,} / {len(discovered):,} · {Path(path).name}")
            prior = by_path.get(path)
            try:
                stat = Path(path).stat()
                if (prior and prior["size"] == stat.st_size and prior["mtime_ns"] == stat.st_mtime_ns
                        and prior["thumb"] and Path(prior["thumb"]).is_file()):
                    scanned.append({**prior, "root": root, "reuse": True})
                    continue
                digest, stat = fingerprint(path, cancel)
                thumb, preview_error = extract_preview(path, self.cache_dir, digest)
                after = Path(path).stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise OSError("プレビュー取得中に保存が行われました。再スキャンしてください。")
                scanned.append(dict(path=path, root=root, name=name, digest=digest,
                                    size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                    thumb=thumb, preview_error=preview_error))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                if prior:
                    availability[prior["id"]] = "unreadable"

        candidates = defaultdict(list)
        for record in old:
            if record["id"] in absent:
                candidates[record["digest"]].append(record)
        new_counts = Counter(r["digest"] for r in scanned if r["path"] not in by_path)
        stats = dict(found=len(scanned), added=0, moved=0, updated=0, ambiguous=0,
                     errors=errors, offline=sum(s == "offline" for s in root_state.values()))
        now = timestamp()
        if cancel.is_set():
            raise Cancelled()
        with closing(self.connect()) as db, db:
            for work_id, status in availability.items():
                db.execute("UPDATE works SET status=? WHERE id=?", (status, work_id))
            for item in scanned:
                prior = by_path.get(item["path"])
                if not prior:
                    matches = candidates.get(item["digest"], [])
                    if len(matches) == 1 and new_counts[item["digest"]] == 1:
                        prior = matches.pop()
                        stats["moved"] += 1
                        db.execute("INSERT INTO history VALUES (?,?,?,?)",
                                   (prior["id"], prior["path"], item["path"], now))
                    elif matches:
                        stats["ambiguous"] += 1
                if prior:
                    if prior["digest"] != item["digest"]:
                        stats["updated"] += 1
                    db.execute("""UPDATE works SET path=?, name=?, root=?, digest=?, size=?,
                        mtime_ns=?, thumb=?, preview_error=?, status='present', updated=? WHERE id=?""",
                        (item["path"], item["name"], item["root"], item["digest"], item["size"],
                         item["mtime_ns"], item["thumb"], item["preview_error"], now, prior["id"]))
                else:
                    stats["added"] += 1
                    db.execute("""INSERT INTO works
                        (id,path,name,root,digest,size,mtime_ns,thumb,preview_error,added,updated)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (uuid.uuid4().hex, item["path"], item["name"], item["root"], item["digest"],
                         item["size"], item["mtime_ns"], item["thumb"], item["preview_error"], now, now))
        return stats

    def relink(self, work_id, destination):
        if not self.scan_lock.acquire(blocking=False):
            raise ValueError("スキャン終了後に再接続してください。")
        try:
            name = Path(destination).stem
            destination = path_key(destination)
            if Path(destination).suffix.lower() != ".clip":
                raise ValueError(".clip ファイルを選択してください。")
            digest, stat = fingerprint(destination)
            thumb, error = extract_preview(destination, self.cache_dir, digest)
            roots = sorted(self.roots(), key=len, reverse=True)
            root = next((r for r in roots if destination.startswith(r.rstrip(os.sep) + os.sep)),
                        str(Path(destination).parent))
            with closing(self.connect()) as db, db:
                old = db.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
                if not old:
                    raise ValueError("作品が見つかりません。")
                duplicate = db.execute("SELECT id FROM works WHERE path=? AND id<>?",
                                       (destination, work_id)).fetchone()
                if duplicate:
                    # Permit explicitly reconnecting to an auto-imported entry only when it has no annotations.
                    other = db.execute("SELECT * FROM works WHERE id=?", (duplicate[0],)).fetchone()
                    if other["favorite"] or json.loads(other["tags"]) or other["notes"]:
                        raise ValueError("指定先はタグやメモのある別作品として登録済みです。")
                    db.execute("DELETE FROM works WHERE id=?", (duplicate[0],))
                db.execute("""UPDATE works SET path=?,name=?,root=?,digest=?,size=?,mtime_ns=?,
                    thumb=?,preview_error=?,status='present',updated=? WHERE id=?""",
                    (destination, name, root, digest, stat.st_size,
                     stat.st_mtime_ns, thumb, error, timestamp(), work_id))
                db.execute("INSERT INTO history VALUES (?,?,?,?)",
                           (work_id, old["path"], destination, timestamp()))
        finally:
            self.scan_lock.release()
