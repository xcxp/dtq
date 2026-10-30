"""บอทส่งคำคมให้กำลังใจ 06:00 / 12:00 / 18:00 (เวลาไทย) ไม่ซ้ำตลอดอายุ"""
from __future__ import annotations

import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_deps() -> None:
    """กันเหนียว: ถ้าแพ็กเกจยังไม่ถูกติดตั้ง ให้ลองติดตั้งจาก requirements.txt เอง"""
    try:
        import aiohttp, discord, dotenv  # noqa: F401
        return
    except ImportError:
        pass
    req = os.path.join(BASE_DIR, "requirements.txt")
    for extra in ([], ["--user"]):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req, *extra])
            return
        except subprocess.CalledProcessError:
            continue
    print("[boot] ติดตั้งแพ็กเกจไม่สำเร็จ ตรวจ requirements.txt / หน้า Startup ในแผงควบคุม")


ensure_deps()

import asyncio
import logging
from datetime import datetime, time as dtime, timezone
from time import monotonic

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from providers import HEADERS, TIMEOUT, translate_th
from quote_store import TH, QuoteStore, harvest

load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("quotebot")


# ---------- ตั้งค่าจาก .env ----------
def parse_targets(raw: str) -> dict:
    targets = {}
    for part in raw.replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        gid, sep, cid = part.partition(":")
        if not sep or not gid.strip().isdigit() or not cid.strip().isdigit():
            raise SystemExit(f"TARGETS ผิดรูปแบบที่ '{part}' (ต้องเป็น server_id:channel_id)")
        targets[int(gid)] = int(cid)  # 1 server = 1 ช่อง
    return targets


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
TARGETS = parse_targets(os.getenv("TARGETS", ""))
TEST_ON_START = os.getenv("TEST_ON_START", "0").strip().lower() in ("1", "true", "yes", "on")
CATCHUP_MINUTES = int(os.getenv("CATCHUP_MINUTES", "90") or 0)

SLOTS = {
    6:  {"emoji": "🌅", "title": "อรุณสวัสดิ์", "sub": "พลังงานเช้านี้", "color": 0xFFB347},
    12: {"emoji": "☀️", "title": "เที่ยงวันแล้ว", "sub": "เติมไฟกลางวัน", "color": 0x4FC3F7},
    18: {"emoji": "🌇", "title": "ตะวันรอนแล้ว", "sub": "ส่งท้ายวัน", "color": 0x9575CD},
}


def nearest_slot(now: datetime) -> int:
    return min(SLOTS, key=lambda h: abs(now.hour * 60 + now.minute - h * 60))


def build_embed(quote: dict, thai, hour: int, remaining: int) -> discord.Embed:
    cfg = SLOTS[hour]
    esc = discord.utils.escape_markdown
    embed = discord.Embed(
        title=f"{cfg['emoji']}  {cfg['title']}  ·  {cfg['sub']}",
        description=f"> ❝ *{esc(quote['text'])}* ❞\n> \n> ✍️ **{esc(quote['author'])}**",
        color=cfg["color"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="DAILY SPARK  •  คำคมให้กำลังใจประจำวัน")
    if thai:
        value = f"**{esc(thai.replace(chr(10), ' ')[:900])}**"
    else:
        value = "*(ระบบแปลขัดข้องชั่วคราว อ่านต้นฉบับไปก่อนนะ)*"
    embed.add_field(name="🇹🇭  คำแปลไทย", value=value, inline=False)
    embed.add_field(
        name="💭  ตีความตามใจ",
        value="อ่านจบแล้วลองถามตัวเองว่า *“ประโยคนี้กำลังให้กำลังใจเรายังไงนะ?”* ✨",
        inline=False,
    )
    if quote.get("source") == "test":
        footer = "🧪 ข้อความทดสอบ ยังไม่นับเป็นคำคมจริง"
    else:
        footer = f"คำคมใบที่ {quote['number']:,}  •  เหลือในคลัง {remaining:,} ใบ"
        if quote.get("source") == "zenquotes":
            footer += "  •  via zenquotes.io"
    embed.set_footer(text=footer)
    return embed


class QuoteBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())  # ไม่ใช้ privileged intents
        self.store = QuoteStore()
        self.web = None
        self.post_lock = None
        self.harvest_lock = None
        self._ready_once = False
        self._test_last = {}
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name="test", description="ส่งข้อความทดสอบไปที่ช่องคำคม (ไม่กินโควตาคำคมจริง)")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        async def test_cmd(interaction: discord.Interaction) -> None:
            await self.run_test(interaction)

    async def setup_hook(self) -> None:
        self.web = aiohttp.ClientSession(headers=HEADERS, timeout=TIMEOUT)
        self.post_lock = asyncio.Lock()      # สร้างตรงนี้ (ใน event loop จริง) กันบัก Python 3.8/3.9
        self.harvest_lock = asyncio.Lock()
        for gid in TARGETS:                  # ลงทะเบียน /test เฉพาะ server ที่ระบุ (ขึ้นเร็ว)
            try:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("ลงทะเบียนคำสั่ง /test ใน server %s แล้ว", gid)
            except Exception as e:
                log.warning("ลงทะเบียนคำสั่งใน server %s ไม่สำเร็จ: %s "
                            "(ต้องเชิญบอทด้วย scope applications.commands และบอทต้องอยู่ใน server นั้น)", gid, e)
        self.harvest_loop.start()
        self.daily_slots.start()

    async def close(self) -> None:
        if self.web and not self.web.closed:
            await self.web.close()
        await super().close()

    # ---------- สถิติคลัง ----------
    def report(self, prefix: str, warn: bool = False) -> None:
        total, used, remaining = self.store.stats()
        years = remaining / 3 / 365.25
        log.info("%s | คลัง %s ใบ | ใช้แล้ว %s | เหลือ %s ใบ (พอใช้ ≈ %.1f ปี)",
                 prefix, f"{total:,}", f"{used:,}", f"{remaining:,}", years)
        if warn and years < 10:
            log.warning("คลังตอนนี้พอใช้ ≈ %.1f ปี (ยังไม่ครบ 10 ปี) บอทจะเติมให้ทุกวัน "
                        "หรือเสริมด้วย data/extra_quotes.json", years)

    async def run_harvest(self) -> None:
        if self.harvest_lock.locked():          # มีรอบที่กำลังรันอยู่ ให้รอจนจบแล้วใช้ผลนั้น
            async with self.harvest_lock:
                return
        async with self.harvest_lock:
            added = await harvest(self.store, self.web)
            self.report(f"เติมคลังเพิ่ม {added} ใบ", warn=True)

    # ---------- ส่งข้อความ ----------
    async def send_one(self, gid: int, cid: int, embed: discord.Embed) -> str:
        """ส่งไป 1 ช่อง คืน '' ถ้าสำเร็จ หรือข้อความ error"""
        try:
            channel = self.get_channel(cid) or await self.fetch_channel(cid)
            if getattr(channel, "guild", None) is None or channel.guild.id != gid:
                msg = f"channel {cid} ไม่ได้อยู่ใน server {gid}"
                log.error(msg)
                return msg
            await channel.send(embed=embed)
            log.info("ส่งแล้ว -> server %s / channel %s", gid, cid)
            return ""
        except discord.Forbidden:
            msg = f"บอทไม่มีสิทธิ์ในช่อง {cid} (ต้องมี View Channel, Send Messages, Embed Links)"
            log.error(msg)
            return msg
        except Exception as e:
            log.exception("ส่งไป server %s ไม่สำเร็จ", gid)
            return f"ส่งไม่สำเร็จ: {e}"

    async def broadcast(self, embed: discord.Embed) -> None:
        for gid, cid in TARGETS.items():
            await self.send_one(gid, cid, embed)

    async def run_test(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        if gid not in TARGETS:
            await interaction.response.send_message("server นี้ไม่ได้อยู่ใน TARGETS ของบอท", ephemeral=True)
            return
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message("ต้องมีสิทธิ์ Manage Server ถึงจะใช้คำสั่งนี้ได้", ephemeral=True)
            return
        wait = 20 - (monotonic() - self._test_last.get(gid, -1000.0))
        if wait > 0:
            await interaction.response.send_message(f"รออีก {wait:.0f} วินาทีค่อยทดสอบใหม่นะ", ephemeral=True)
            return
        self._test_last[gid] = monotonic()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = self.store.peek()
            if row is None:
                await interaction.followup.send(
                    "คลังคำคมยังว่างอยู่ รอเติมคลังเสร็จก่อน (ดู Console) แล้วลองใหม่", ephemeral=True)
                return
            sample = dict(row, source="test", number=0)
            thai = await translate_th(self.web, sample["text"])
            _, _, remaining = self.store.stats()
            embed = build_embed(sample, thai, nearest_slot(datetime.now(TH)), remaining)
            err = await self.send_one(gid, TARGETS[gid], embed)
            lines = ["✅ ส่งข้อความทดสอบไปที่ช่องคำคมแล้ว" if not err else f"❌ {err}",
                     "🇹🇭 แปลไทย: ได้" if thai else "🇹🇭 แปลไทย: ไม่ได้ (ดู Console ว่า Google/MyMemory ติดอะไร)"]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception:
            log.exception("/test error")
            await interaction.followup.send("เกิดข้อผิดพลาดภายใน ดูรายละเอียดใน Console", ephemeral=True)

    async def post_slot(self, date_str: str, hour: int) -> None:
        slot_key = f"{date_str}-{hour:02d}"
        async with self.post_lock:
            status, quote = self.store.claim(slot_key)
            if status == "done":                 # รอบนี้ส่งไปแล้ว (เช่น บอทรีสตาร์ท)
                return
            if status == "empty":
                log.warning("คลังคำคมหมด ลองเติมเพิ่ม")
                await self.run_harvest()
                status, quote = self.store.claim(slot_key)
            if status != "ok":
                log.error("ไม่มีคำคมใหม่เหลือ ข้ามรอบ %s (ไม่ส่งซ้ำของเก่า)", slot_key)
                return
            thai = await translate_th(self.web, quote["text"])
            _, _, remaining = self.store.stats()
            await self.broadcast(build_embed(quote, thai, hour, remaining))

    # ---------- ตัวตั้งเวลา ----------
    @tasks.loop(time=[dtime(hour=h, tzinfo=TH) for h in SLOTS])
    async def daily_slots(self) -> None:
        try:
            now = datetime.now(TH)
            await self.post_slot(now.strftime("%Y-%m-%d"), nearest_slot(now))
        except Exception:
            log.exception("daily_slots error (ลูปยังทำงานต่อ)")

    @daily_slots.before_loop
    async def _before_daily(self) -> None:
        await self.wait_until_ready()
        await self.catch_up()

    @tasks.loop(hours=6)
    async def harvest_loop(self) -> None:
        try:
            await self.run_harvest()
        except Exception:
            log.exception("harvest error (ลูปยังทำงานต่อ)")

    async def catch_up(self) -> None:
        """บอทเพิ่งเปิดหลังเวลาส่งไม่นาน ให้ส่งรอบที่พลาดไป (ส่งซ้ำไม่ได้ เพราะมี slot_key กัน)"""
        now = datetime.now(TH)
        for hour in sorted(SLOTS):
            slot_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            late_min = (now - slot_time).total_seconds() / 60
            if 0 < late_min <= CATCHUP_MINUTES:
                log.info("ส่งรอบที่พลาด %02d:00 (ช้าไป %.0f นาที)", hour, late_min)
                await self.post_slot(now.strftime("%Y-%m-%d"), hour)

    # ---------- events ----------
    async def on_ready(self) -> None:
        log.info("เข้าสู่ระบบเป็น %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        if self._ready_once:
            return
        self._ready_once = True
        for gid, cid in TARGETS.items():
            guild = self.get_guild(gid)
            if guild is None:
                log.warning("บอทยังไม่ได้อยู่ใน server %s (ต้องเชิญบอทก่อน)", gid)
            elif guild.get_channel(cid) is None:
                log.warning("ไม่พบ channel %s ใน server %s (เช็ก ID / สิทธิ์ View Channel)", cid, gid)
        self.report("เริ่มระบบ")
        if TEST_ON_START:
            sample = {
                "text": "This is a test. Your daily quotes will arrive at 06:00, 12:00 and 18:00.",
                "author": "Quote Bot", "source": "test", "number": 0,
            }
            thai = "นี่คือข้อความทดสอบ คำคมประจำวันจะมาถึงตอน 06:00, 12:00 และ 18:00 (เวลาไทย)"
            _, _, remaining = self.store.stats()
            await self.broadcast(build_embed(sample, thai, nearest_slot(datetime.now(TH)), remaining))


def main() -> None:
    if not TOKEN or TOKEN.startswith("PASTE_"):
        raise SystemExit("ไม่พบ DISCORD_TOKEN ในไฟล์ .env")
    if not TARGETS:
        raise SystemExit("ไม่พบ TARGETS ในไฟล์ .env (รูปแบบ server_id:channel_id)")
    QuoteBot().run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()