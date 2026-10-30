"""แหล่งคำคม (ไม่ต้องมี API key) + ตัวแปลไทย (ไม่ต้องสมัคร)"""
from __future__ import annotations

import os
import asyncio
import html
import logging
import re
from typing import AsyncIterator, List, Optional, Tuple

import aiohttp

log = logging.getLogger("providers")

Quote = Tuple[str, str, str]  # (text, author, source)

HEADERS = {"User-Agent": "daily-quote-discord-bot/1.0"}
TIMEOUT = aiohttp.ClientTimeout(total=25)

MIN_LEN, MAX_LEN = 8, 260
BAD_AUTHORS = {"", "unknown", "anonymous", "anon", "various", "unknown author", "zenquotes.io"}


def clean(text, author, source: str) -> Optional[Quote]:
    """ทำความสะอาด 1 ใบ คืน None ถ้าใช้ไม่ได้ (สั้น/ยาวไป/ไม่มีชื่อคนพูด)"""
    text = re.sub(r"\s+", " ", str(text or "")).strip().strip('"“”').strip()
    author = re.sub(r"\s+", " ", str(author or "")).strip()
    author = re.sub(r",?\s*type\.fit$", "", author, flags=re.I).strip()
    if not (MIN_LEN <= len(text) <= MAX_LEN):
        return None
    if author.lower() in BAD_AUTHORS:
        return None
    return text, author, source


async def _get_json(session: aiohttp.ClientSession, url: str, params=None):
    async with session.get(url, params=params) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} จาก {url}")
        return await resp.json(content_type=None)


def _batch(items) -> List[Quote]:
    return [c for c in items if c]


# ---------- แหล่งคำคม (async generator: ส่งทีละชุด ถ้าล่มกลางทางของที่ได้แล้วไม่หาย) ----------
async def fetch_dummyjson(session) -> AsyncIterator[List[Quote]]:
    skip, total = 0, None
    while total is None or skip < total:
        data = await _get_json(session, "https://dummyjson.com/quotes", {"limit": 100, "skip": skip})
        total = int(data.get("total") or 0)
        rows = data.get("quotes") or []
        if not rows:
            return
        yield _batch(clean(r.get("quote"), r.get("author"), "dummyjson") for r in rows)
        skip += 100
        await asyncio.sleep(0.3)


async def fetch_quotable(session) -> AsyncIterator[List[Quote]]:
    page, pages = 1, 1
    while page <= pages and page <= 40:
        data = await _get_json(session, "https://api.quotable.io/quotes", {"limit": 150, "page": page})
        pages = int(data.get("totalPages") or 1)
        yield _batch(clean(r.get("content"), r.get("author"), "quotable") for r in data.get("results") or [])
        page += 1
        await asyncio.sleep(0.4)


async def fetch_typefit(session) -> AsyncIterator[List[Quote]]:
    data = await _get_json(session, "https://type.fit/api/quotes")
    yield _batch(clean(r.get("text"), r.get("author"), "type.fit") for r in data)


async def fetch_zenquotes(session, rounds: int = 6) -> AsyncIterator[List[Quote]]:
    """สุ่มทีละ 50 ใบ จำกัด 5 ครั้ง/30 วินาที จึงเว้น 7 วินาทีต่อครั้ง"""
    for i in range(rounds):
        data = await _get_json(session, "https://zenquotes.io/api/quotes")
        if not isinstance(data, list) or not data or data[0].get("a") == "zenquotes.io":
            log.warning("zenquotes: โดนจำกัดความถี่หรือข้อมูลไม่ตรงรูปแบบ หยุดรอบนี้")
            return
        yield _batch(clean(r.get("q"), r.get("a"), "zenquotes") for r in data)
        if i < rounds - 1:
            await asyncio.sleep(7)


SOURCES = {
    "dummyjson": fetch_dummyjson,
    "quotable": fetch_quotable,
    "type.fit": fetch_typefit,
    "zenquotes": fetch_zenquotes,
}


# ---------- แปลไทย ----------

async def translate_th(session, text: str) -> Optional[str]:
    """EN -> TH คืน None ถ้าแปลไม่ได้ทุกเจ้า"""
    try:  # 1) Google (ไม่เป็นทางการ ไม่ต้องมี key)
        params = {"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": text}
        data = await _get_json(session, "https://translate.googleapis.com/translate_a/single", params)
        th = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        if th:
            log.info("แปลสำเร็จด้วย Google")
            return th
        log.warning("Google ตอบกลับมาแต่ไม่มีคำแปล")
    except Exception as e:
        log.warning("แปลด้วย Google ไม่สำเร็จ: %s", e)
    try:  # 2) MyMemory (สำรอง)
        params = {"q": text, "langpair": "en|th"}
        email = os.getenv("MYMEMORY_EMAIL", "").strip()  # ใส่อีเมลเพื่อเพิ่มโควตา ไม่ต้องสมัคร
        if email:
            params["de"] = email
        data = await _get_json(session, "https://api.mymemory.translated.net/get", params)
        th = html.unescape((data.get("responseData") or {}).get("translatedText") or "").strip()
        status = str(data.get("responseStatus"))
        if th and "MYMEMORY WARNING" not in th.upper() and status == "200":
            log.info("แปลสำเร็จด้วย MyMemory")
            return th
        log.warning("MyMemory ตอบกลับแต่ใช้ไม่ได้ (status=%s): %s", status, th[:80])
    except Exception as e:
        log.warning("แปลด้วย MyMemory ไม่สำเร็จ: %s", e)
    return None