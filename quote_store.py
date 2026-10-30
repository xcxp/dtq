"""ฐานข้อมูลคำคม (SQLite): หัวใจของระบบ 'ห้ามซ้ำ'"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from providers import SOURCES, clean

log = logging.getLogger("store")

TH = timezone(timedelta(hours=7))  # เวลาไทย (ไม่มี DST)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "quotes.db")
REFETCH_DAYS = 14  # แหล่งแบบดึงทั้งก้อน จะดึงซ้ำอย่างมากทุกกี่วัน

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    hash    TEXT NOT NULL UNIQUE,      -- กันซ้ำระดับฐานข้อมูล
    text    TEXT NOT NULL,
    author  TEXT NOT NULL,
    source  TEXT NOT NULL,
    used_at TEXT                       -- NULL = ยังไม่เคยใช้
);
CREATE TABLE IF NOT EXISTS slots(
    slot_key   TEXT PRIMARY KEY,       -- เช่น 2026-10-31-06
    quote_id   INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def norm_hash(text: str) -> str:
    """ตัดเครื่องหมาย/ช่องว่าง/ตัวพิมพ์ทิ้งก่อนทำ hash คำคมเดียวกันที่ต่างแค่เครื่องหมายจะนับเป็นใบเดียว"""
    key = re.sub(r"[\W_]+", "", text.lower())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(TH).isoformat(timespec="seconds")


class QuoteStore:
    def __init__(self, path: str = DB_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- meta ---
    def get_meta(self, key: str):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
        self.db.commit()

    # --- คลัง ---
    def add_many(self, items) -> int:
        """เพิ่มคำคม ใบที่ซ้ำจะถูกข้าม คืนจำนวนที่เพิ่มใหม่จริง"""
        before = self.db.total_changes
        self.db.executemany(
            "INSERT OR IGNORE INTO quotes(hash,text,author,source) VALUES (?,?,?,?)",
            [(norm_hash(t), t, a, s) for (t, a, s) in items],
        )
        self.db.commit()
        return self.db.total_changes - before

    def stats(self):
        r = self.db.execute(
            "SELECT COUNT(*) AS total, SUM(used_at IS NOT NULL) AS used FROM quotes"
        ).fetchone()
        total, used = r["total"] or 0, r["used"] or 0
        return total, used, total - used

    def peek(self):
        """สุ่มดูคำคมที่ยังไม่เคยใช้ 1 ใบ โดยไม่ทำเครื่องหมายว่าใช้แล้ว (สำหรับ /test)"""
        row = self.db.execute(
            "SELECT id,text,author,source FROM quotes WHERE used_at IS NULL ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def claim(self, slot_key: str):
        """จองคำคมใบใหม่ให้รอบเวลานี้ คืน ('ok', dict) | ('done', None) | ('empty', None)"""
        if self.db.execute("SELECT 1 FROM slots WHERE slot_key=?", (slot_key,)).fetchone():
            return "done", None
        row = self.db.execute(
            "SELECT id,text,author,source FROM quotes WHERE used_at IS NULL ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        if row is None:
            return "empty", None
        now = _now()
        cur = self.db.execute(
            "UPDATE quotes SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"])
        )
        if cur.rowcount != 1:
            self.db.rollback()
            return "empty", None
        self.db.execute(
            "INSERT INTO slots(slot_key,quote_id,created_at) VALUES (?,?,?)", (slot_key, row["id"], now)
        )
        self.db.commit()
        number = self.db.execute("SELECT COUNT(*) FROM slots WHERE quote_id IS NOT NULL").fetchone()[0]
        return "ok", dict(row, number=number)


# ---------- เติมคลัง ----------
def import_extra(store: QuoteStore) -> int:
    """นำเข้า data/extra_quotes.json (ถ้ามี) รูปแบบ [{"text": "...", "author": "..."}] นำเข้าใหม่เมื่อไฟล์ถูกแก้"""
    path = os.path.join(DATA_DIR, "extra_quotes.json")
    if not os.path.exists(path):
        return 0
    stamp = str(os.path.getmtime(path))
    if store.get_meta("extra_stamp") == stamp:
        return 0
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    items = []
    for r in rows:
        c = clean(r.get("text") or r.get("quote") or r.get("content") or r.get("q"),
                  r.get("author") or r.get("a"), "extra")
        if c:
            items.append(c)
    n = store.add_many(items)
    store.set_meta("extra_stamp", stamp)
    log.info("extra_quotes.json: เพิ่มใหม่ %d ใบ", n)
    return n


def _fetched_recently(store: QuoteStore, name: str) -> bool:
    last = store.get_meta(f"last:{name}")
    if not last:
        return False
    try:
        return (datetime.now(TH).date() - datetime.fromisoformat(last).date()).days < REFETCH_DAYS
    except ValueError:
        return False


async def harvest(store: QuoteStore, session) -> int:
    """ดึงคำคมจากทุกแหล่งเข้าคลัง ข้ามแหล่งที่ล่มได้ คืนจำนวนที่เพิ่มใหม่"""
    added = 0
    try:
        added += import_extra(store)
    except Exception as e:
        log.warning("นำเข้า extra_quotes.json ไม่สำเร็จ: %s", e)
    for name, fetch in SOURCES.items():
        static = name != "zenquotes"  # zenquotes สุ่มใหม่ได้เรื่อย ๆ จึงดึงทุกวัน
        if static and _fetched_recently(store, name):
            continue
        got, ok = 0, True
        try:
            async for batch in fetch(session):
                got += store.add_many(batch)
        except Exception as e:
            ok = False
            log.warning("ดึงจาก %s ไม่สำเร็จ: %s", name, e)
        if ok and static:
            store.set_meta(f"last:{name}", _now())
        log.info("%s: เพิ่มใหม่ %d ใบ%s", name, got, "" if ok else " (ไม่ครบ จะลองใหม่รอบหน้า)")
        added += got
    return added