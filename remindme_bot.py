# pip install fluxer.py python-dateutil

import asyncio
import configparser
import datetime
import logging
import re
import sqlite3
from collections import deque

import fluxer
import dateutil.rrule
import dateutil.parser
import dateutil.tz

# -------------------------------------------------
CONFIG_FILE = "token.ini"
DB_FILE = "reminders.db"

SEND_INTERVAL = 1
BURST_WINDOW = 2

# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("RemindMeBot")

# ================= DATABASE =================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            user_id TEXT,
            message TEXT,
            remind_time TEXT,
            rrule_str TEXT,
            interval_hours INTEGER,
            original_tz TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS server_settings (
            channel_id TEXT PRIMARY KEY,
            timezone TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT PRIMARY KEY,
            timezone TEXT
        )
    """)

    conn.commit()
    return conn

# ================= CONFIG =================

def get_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return (
        cfg["auth"]["token"],
        cfg["auth"]["channel_id"],
        cfg["auth"].get("admin_role_id", "")
    )

# ================= HELPERS =================

def parse_simple_time(t):
    m = re.match(r"^(\d+)([dhms])$", t.lower())
    if not m:
        return None

    v = int(m.group(1))
    return {
        "d": datetime.timedelta(days=v),
        "h": datetime.timedelta(hours=v),
        "m": datetime.timedelta(minutes=v),
        "s": datetime.timedelta(seconds=v),
    }[m.group(2)]

def get_tz_context(conn, channel_id, user_id):
    c = conn.cursor()

    c.execute("SELECT timezone FROM user_settings WHERE user_id=?", (user_id,))
    r = c.fetchone()
    if r:
        return dateutil.tz.gettz(r[0]), r[0]

    c.execute("SELECT timezone FROM server_settings WHERE channel_id=?", (channel_id,))
    r = c.fetchone()
    if r:
        return dateutil.tz.gettz(r[0]), r[0]

    return dateutil.tz.UTC, "UTC"

# ================= BOT =================

intents = fluxer.Intents.default()
bot = fluxer.Bot(command_prefix="!", intents=intents)

conn = init_db()
message_queue = deque()
allowed_channel = None
admin_role_id = None

# ================= READY =================

@bot.event
async def on_ready():
    global allowed_channel

    logger.info(f"Logged in as {bot.user.username}")

    allowed_channel = await bot.fetch_channel(CONFIG_CHANNEL_ID)
    await queue_message("🤖 Bot online!")

    bot.loop.create_task(queue_worker())
    bot.loop.create_task(scheduler())

# ================= QUEUE =================

async def queue_message(text):
    message_queue.append(text)

async def queue_worker():
    while True:
        try:
            if not message_queue:
                await asyncio.sleep(0.2)
                continue

            now = datetime.datetime.now()
            batch = []

            while message_queue:
                batch.append(message_queue.popleft())
                if (datetime.datetime.now() - now).total_seconds() > BURST_WINDOW:
                    break

            combined = "\n".join(batch)
            await allowed_channel.send(combined)

            await asyncio.sleep(SEND_INTERVAL)

        except Exception as e:
            logger.error(f"[Queue] {e}")
            await asyncio.sleep(2)

# ================= SCHEDULER =================

async def scheduler():
    while True:
        try:
            now = datetime.datetime.now(dateutil.tz.UTC)
            c = conn.cursor()

            c.execute("SELECT * FROM reminders ORDER BY remind_time ASC")
            reminders = c.fetchall()

            for r in reminders:
                rid, chan, uid, msg, time_str, rrule, interval, tz_name = r
                remind_dt = dateutil.parser.isoparse(time_str)

                if remind_dt <= now:
                    await send_reminder(r)

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"[Scheduler] {e}")
            await asyncio.sleep(5)

async def send_reminder(r):
    rid, chan, uid, msg, time_str, rrule, interval, tz_name = r
    remind_dt = dateutil.parser.isoparse(time_str)

    user_tz = dateutil.tz.gettz(tz_name) or dateutil.tz.UTC
    local = remind_dt.astimezone(user_tz)

    await queue_message(
        f"🔔 Reminder #{rid} for <@{uid}>: {msg} "
        f"({local.strftime('%Y-%m-%d %H:%M:%S %Z')})"
    )

    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()

# ================= COMMANDS =================

@bot.command()
async def help(ctx):
    await ctx.reply(
        "**RemindMeBot Commands:**\n"
        "`!help`\n"
        "`!settz <Timezone>`\n"
        "`!mytz <Timezone>`\n"
        "`!remind <10s|5m|2h|1d> <message>`"
    )

@bot.command()
async def settz(ctx, timezone: str):
    if admin_role_id and admin_role_id not in getattr(ctx.author, "role_ids", []):
        await ctx.reply("❌ Admin only.")
        return

    if not dateutil.tz.gettz(timezone):
        await ctx.reply("Invalid timezone.")
        return

    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO server_settings VALUES (?,?)",
              (str(ctx.channel.id), timezone))
    conn.commit()

    await ctx.reply(f"Timezone set to {timezone}")

@bot.command()
async def mytz(ctx, timezone: str):
    if not dateutil.tz.gettz(timezone):
        await ctx.reply("Invalid timezone.")
        return

    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_settings VALUES (?,?)",
              (str(ctx.author.id), timezone))
    conn.commit()

    await ctx.reply(f"Your timezone set to {timezone}")

@bot.command(aliases=["remindme"])
async def remind(ctx, time_str: str, *, message: str):
    delta = parse_simple_time(time_str)
    if not delta:
        await ctx.reply("Invalid time format. Example: 10s 5m 2h 1d")
        return

    remind_dt = datetime.datetime.now(dateutil.tz.UTC) + delta
    tz_obj, tz_name = get_tz_context(conn, str(ctx.channel.id), str(ctx.author.id))

    c = conn.cursor()
    c.execute("""
        INSERT INTO reminders
        (channel_id,user_id,message,remind_time,rrule_str,interval_hours,original_tz)
        VALUES (?,?,?,?,?,?,?)
    """, (
        str(ctx.channel.id),
        str(ctx.author.id),
        message,
        remind_dt.isoformat(),
        None,
        None,
        tz_name
    ))
    conn.commit()

    rid = c.lastrowid
    local = remind_dt.astimezone(tz_obj)

    await ctx.reply(
        f"✅ Reminder #{rid} set for "
        f"{local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

# ================= RUN =================

if __name__ == "__main__":
    TOKEN, CONFIG_CHANNEL_ID, admin_role_id = get_config()
    bot.run(TOKEN)
