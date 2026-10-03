# SHUVO X HOST — merged bot
# Token, owner ID and deployment identity are configured ONLY in this file.
# Uploaded Python programs execute as OS processes; production deployments should
# isolate untrusted workloads in containers/VMs with resource limits.

import os
import re
import html
import signal
import sqlite3
import subprocess
import threading
import time
import json
import shutil
import traceback
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types
import psutil

# =========================
# SINGLE SOURCE CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("[CRITICAL] BOT_TOKEN environment variable is missing.")

OWNER_ID_ENV = os.getenv("OWNER_ID")
if not OWNER_ID_ENV:
    raise SystemExit("[CRITICAL] OWNER_ID environment variable is missing.")
try:
    OWNER_ID = int(OWNER_ID_ENV)
except ValueError:
    raise SystemExit("[CRITICAL] OWNER_ID must be a valid Telegram user ID.")

ADMIN_IDS = {OWNER_ID}
for _x in os.getenv("ADMIN_IDS", "").split(","):
    if _x.strip().isdigit():
        ADMIN_IDS.add(int(_x.strip()))

bot = telebot.TeleBot(BOT_TOKEN)

DB_FILE = os.getenv("DB_FILE", "hostnexa.db")
DATA_DIR = Path(os.getenv("DATA_DIR", "hosted_files"))
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = "allowed_users.json"
CONFIG_FILE = "bot_config.json"
MAX_CHANNELS = 10
START_TIME = time.time()
current_dir = os.getcwd()
bg_processes = {}
pending_requests = set()
waiting_for_channel_forward = set()

TRIAL_DAYS = 2
DEFAULT_FILE_LIMIT = 1
MAX_UPLOAD_MB = 2
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
PAYMENT_NUMBER = os.getenv("PAYMENT_NUMBER", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")
FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "")
FORCE_JOIN_URL = os.getenv("FORCE_JOIN_URL", "")
DAILY_CLAIM_POINTS = 10
REFERRAL_POINTS = 20
REFERRAL_BONUS_POINTS = 10
DAILY_STREAK_BONUS = 2
MAX_DAILY_STREAK_BONUS = 20
BROADCAST_BATCH = 25
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable)

PLANS = {
    "starter":{"name":"🚀 Starter","days":7,"file_limit":1,"price":50,"points":50},
    "basic":{"name":"🌱 Basic","days":15,"file_limit":2,"price":80,"points":80},
    "pro":{"name":"⚡ Pro","days":30,"file_limit":3,"price":120,"points":120},
    "pro_plus":{"name":"🔥 Pro Plus","days":45,"file_limit":5,"price":180,"points":180},
    "premium":{"name":"👑 Premium","days":90,"file_limit":5,"price":300,"points":300},
    "advance":{"name":"❄️ Advance","days":90,"file_limit":10,"price":400,"points":400},
    "ultimate":{"name":"💎 Ultimate","days":180,"file_limit":15,"price":650,"points":650},
    "business":{"name":"🏢 Business","days":365,"file_limit":25,"price":1000,"points":1000},
    "unlimited":{"name":"♾️ Unlimited","days":365,"file_limit":999,"price":1500,"points":1500},
}

processes = {}
process_lock = threading.Lock()

def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            plan TEXT DEFAULT 'Trial',
            expires_at TEXT NOT NULL,
            file_limit INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            banned INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, filename)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS plan_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_key TEXT NOT NULL,
            txid TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        )
    """)

    # Migrate existing databases safely.
    user_cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    for col, definition in [
        ("points", "INTEGER DEFAULT 0"),
        ("referred_by", "INTEGER DEFAULT NULL"),
        ("referral_count", "INTEGER DEFAULT 0"),
        ("daily_claim_at", "TEXT DEFAULT ''"),
    ]:
        if col not in user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            points INTEGER NOT NULL,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            expires_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS redeem_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TEXT NOT NULL,
            UNIQUE(code, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
    """)

    # Extended admin/economy/analytics tables.
    extra_cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    for col, definition in [
        ("streak_count", "INTEGER DEFAULT 0"),
        ("last_claim_date", "TEXT DEFAULT ''"),
        ("temp_ban_until", "TEXT DEFAULT ''"),
        ("role", "TEXT DEFAULT 'user'"),
        ("note", "TEXT DEFAULT ''"),
    ]:
        if col not in extra_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")

    cur.execute("""CREATE TABLE IF NOT EXISTS point_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL, balance_after INTEGER NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL, admin_id INTEGER DEFAULT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
        action TEXT NOT NULL, target_id INTEGER DEFAULT NULL, details TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS plan_settings (
        plan_key TEXT PRIMARY KEY, name TEXT NOT NULL, days INTEGER NOT NULL,
        file_limit INTEGER NOT NULL, price INTEGER NOT NULL, points INTEGER NOT NULL, enabled INTEGER DEFAULT 1)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, text TEXT NOT NULL,
        target TEXT NOT NULL, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS payment_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, user_id INTEGER NOT NULL,
        plan_key TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL,
        txid TEXT DEFAULT '', created_at TEXT NOT NULL, reviewed_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS code_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, action TEXT NOT NULL,
        admin_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")

    # Pro v2 platform tables.
    cur.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        user_id INTEGER NOT NULL, filename TEXT NOT NULL, auto_restart INTEGER DEFAULT 1,
        restart_count INTEGER DEFAULT 0, last_exit_code INTEGER DEFAULT NULL,
        last_started_at TEXT DEFAULT '', last_stopped_at TEXT DEFAULT '',
        log_path TEXT DEFAULT '', env_json TEXT DEFAULT '{}', notes TEXT DEFAULT '',
        PRIMARY KEY(user_id, filename))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS app_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, filename TEXT NOT NULL,
        event TEXT NOT NULL, details TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, filename TEXT NOT NULL,
        backup_path TEXT NOT NULL, size_bytes INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, subject TEXT NOT NULL,
        message TEXT NOT NULL, status TEXT DEFAULT 'open', priority TEXT DEFAULT 'normal',
        admin_id INTEGER DEFAULT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admin_roles (
        user_id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'moderator', permissions TEXT DEFAULT '{}', created_at TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL,
        status TEXT DEFAULT 'open', created_at TEXT NOT NULL, resolved_at TEXT DEFAULT '')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS usage_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, filename TEXT NOT NULL,
        cpu_percent REAL DEFAULT 0, memory_mb REAL DEFAULT 0, disk_mb REAL DEFAULT 0,
        captured_at TEXT NOT NULL)""")
    # Resource-aware plan columns.
    plan_cols={r[1] for r in cur.execute("PRAGMA table_info(plan_settings)").fetchall()}
    for col,definition in [("ram_mb","INTEGER DEFAULT 256"),("cpu_percent","INTEGER DEFAULT 50"),("storage_mb","INTEGER DEFAULT 512"),("bandwidth_mb","INTEGER DEFAULT 0"),("backup_days","INTEGER DEFAULT 0")]:
        if col not in plan_cols: cur.execute(f"ALTER TABLE plan_settings ADD COLUMN {col} {definition}")
    file_cols={r[1] for r in cur.execute("PRAGMA table_info(files)").fetchall()}
    for col,definition in [("status","TEXT DEFAULT 'STOPPED'"),("last_error","TEXT DEFAULT ''"),("deploy_count","INTEGER DEFAULT 0")]:
        if col not in file_cols: cur.execute(f"ALTER TABLE files ADD COLUMN {col} {definition}")

    for k,v in {
        "maintenance_mode":"0", "mandatory_join":"1", "support_username":SUPPORT_USERNAME,
        "payment_number":PAYMENT_NUMBER, "daily_claim":"10", "referral_reward":"20",
        "referral_bonus":"10", "point_transfer":"1", "daily_streak":"1"
    }.items():
        cur.execute("INSERT OR IGNORE INTO bot_settings(key,value) VALUES(?,?)", (k,str(v)))
    for key, plan in PLANS.items():
        cur.execute("""INSERT OR IGNORE INTO plan_settings(plan_key,name,days,file_limit,price,points,enabled)
                       VALUES(?,?,?,?,?,?,1)""", (key,plan["name"],plan["days"],plan["file_limit"],plan["price"],plan["points"]))


    # Fill sensible resource defaults for existing plans.
    for pk,pv in PLANS.items():
        cur.execute("UPDATE plan_settings SET ram_mb=COALESCE(ram_mb,256), cpu_percent=COALESCE(cpu_percent,50), storage_mb=COALESCE(storage_mb,512), bandwidth_mb=COALESCE(bandwidth_mb,0), backup_days=COALESCE(backup_days,0) WHERE plan_key=?", (pk,))

    # Old versions had a free Trial. New SHUVO X HOST requires plan purchase.
    cur.execute("""
        UPDATE users
        SET plan='No Plan', file_limit=0, expires_at=?
        WHERE plan='Trial'
    """, (iso(now_utc()),))

    conn.commit()
    conn.close()


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return now_utc()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row


def create_user(tg_user, referral_id=None):
    user_id = tg_user.id
    existing = get_user(user_id)

    if existing:
        conn = db()
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (tg_user.username or "", tg_user.first_name or "", user_id)
        )
        conn.commit()
        conn.close()
        return get_user(user_id)

    expires = now_utc()

    conn = db()
    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name, plan, expires_at,
         file_limit, created_at, banned, points, referred_by, referral_count, daily_claim_at)
        VALUES (?, ?, ?, 'No Plan', ?, 0, ?, 0, 0, ?, 0, '')
    """, (
        user_id,
        tg_user.username or "",
        tg_user.first_name or "",
        iso(expires),
        iso(now_utc()),
        referral_id
    ))
    conn.commit()
    conn.close()

    user_dir(user_id)
    return get_user(user_id)


def user_dir(user_id):
    path = DATA_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_expired(row):
    return now_utc() >= parse_iso(row["expires_at"])


def access_ok(user_id):
    row = get_user(user_id)
    if not row:
        return False, "Account not found. Send /start first."

    if row["banned"]:
        return False, "🚫 Your account is banned."

    if temp_banned(row):
        return False, f"⏳ Your account is temporarily banned until <b>{html.escape(row['temp_ban_until'])}</b>."

    if not is_admin(user_id) and maintenance_on():
        return False, "🔧 Bot is currently under maintenance. Please try again later."

    if is_admin(user_id):
        return True, ""

    if row["plan"] in ("No Plan", "Expired", "") or row["file_limit"] <= 0 or is_expired(row):
        return False, "💎 Hosting করতে আগে একটি Plan কিনে Admin approval নিতে হবে।"

    return True, ""


def remaining_text(row):
    if is_admin(row["user_id"]):
        return "♾️ Admin"

    if is_expired(row):
        return "Expired"

    seconds = int((parse_iso(row["expires_at"]) - now_utc()).total_seconds())
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(minutes, 0)}m"


# =========================
# EXTENDED ADMIN / ECONOMY HELPERS
# =========================

def setting(key, default=""):
    conn=db(); row=conn.execute("SELECT value FROM bot_settings WHERE key=?",(key,)).fetchone(); conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn=db(); conn.execute("INSERT INTO bot_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value))); conn.commit(); conn.close()

def admin_log(admin_id, action, target_id=None, details=""):
    conn=db(); conn.execute("INSERT INTO admin_logs(admin_id,action,target_id,details,created_at) VALUES(?,?,?,?,?)",(admin_id,action,target_id,details,iso(now_utc()))); conn.commit(); conn.close()

def record_points(user_id, amount, reason, admin_id=None):
    bal=points_of(user_id)
    conn=db(); conn.execute("INSERT INTO point_transactions(user_id,amount,balance_after,reason,created_at,admin_id) VALUES(?,?,?,?,?,?)",(user_id,amount,bal,reason,iso(now_utc()),admin_id)); conn.commit(); conn.close()

def add_points_logged(user_id, amount, reason, admin_id=None):
    if not get_user(user_id): return False
    conn=db(); conn.execute("UPDATE users SET points=MAX(0,points+?) WHERE user_id=?",(amount,user_id)); conn.commit(); conn.close(); record_points(user_id,amount,reason,admin_id); return True

def get_runtime_plans():
    conn=db(); rows=conn.execute("SELECT * FROM plan_settings WHERE enabled=1 ORDER BY rowid").fetchall(); conn.close()
    if not rows: return PLANS
    return {r["plan_key"]:{"name":r["name"],"days":r["days"],"file_limit":r["file_limit"],"price":r["price"],"points":r["points"]} for r in rows}

def sync_plans():
    global PLANS
    PLANS=get_runtime_plans()

def temp_banned(row):
    v=row["temp_ban_until"] if "temp_ban_until" in row.keys() else ""
    return bool(v) and now_utc() < parse_iso(v)

def maintenance_on(): return setting("maintenance_mode","0") == "1"

def log_point_change(user_id, amount, reason, admin_id=None):
    try: record_points(user_id,amount,reason,admin_id)
    except Exception: pass

def set_plan_for_user(user_id, plan_key, admin_id=None, extend=False):
    plans=get_runtime_plans(); p=plans.get(plan_key)
    if not p: return False
    old=get_user(user_id)
    if not old: return False
    base=parse_iso(old["expires_at"]) if extend and not is_expired(old) else now_utc()
    expires=base+timedelta(days=p["days"])
    conn=db(); conn.execute("UPDATE users SET plan=?,expires_at=?,file_limit=?,banned=0 WHERE user_id=?",(p["name"],iso(expires),p["file_limit"],user_id)); conn.commit(); conn.close()
    if admin_id: admin_log(admin_id,"plan_grant",user_id,f"{plan_key} {p['days']}d limit={p['file_limit']}")
    return True

def get_user_by_username(value):
    value=value.strip().lstrip("@").lower()
    conn=db(); row=conn.execute("SELECT * FROM users WHERE lower(username)=?",(value,)).fetchone(); conn.close(); return row

def user_lookup(token):
    try: return get_user(int(token))
    except Exception: return get_user_by_username(token)

def user_summary(row):
    if not row: return "❌ User not found."
    return (f"👤 <b>User</b>\n\n🆔 <code>{row['user_id']}</code>\n"
            f"👤 @{html.escape(row['username']) if row['username'] else 'none'}\n"
            f"💎 Plan: <b>{html.escape(row['plan'])}</b>\n⏳ {remaining_text(row)}\n"
            f"📁 Limit: <b>{row['file_limit']}</b>\n🪙 Points: <b>{row['points']}</b>\n"
            f"👥 Referrals: <b>{row['referral_count']}</b>\n🚫 Banned: <b>{'Yes' if row['banned'] else 'No'}</b>\n"
            f"🛡 Role: <b>{html.escape(row['role'])}</b>\n📝 Note: {html.escape(row['note'] or '—')}")

def admin_dashboard_text():
    conn=db(); now=iso(now_utc())
    vals={
      "users":conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
      "active":conn.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND plan NOT IN ('No Plan','Expired') AND expires_at>?",(now,)).fetchone()[0],
      "banned":conn.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0],
      "apps":conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
      "pending":conn.execute("SELECT COUNT(*) FROM plan_requests WHERE status='pending'").fetchone()[0],
      "tickets":conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0],
      "backups":conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0],
      "alerts":conn.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0],
      "revenue":conn.execute("SELECT COALESCE(SUM(amount),0) FROM payment_history WHERE status='approved'").fetchone()[0],
    }; conn.close(); total,running,crashed=hosting_overview()
    return ("✨ <b>SHUVO X HOST — COMMAND CENTER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Users      <b>{vals['users']}</b>   • 🟢 Active <b>{vals['active']}</b>\n"
            f"🖥 Apps       <b>{vals['apps']}</b>   • ▶️ Running <b>{running}</b>\n"
            f"🔴 Crashed    <b>{crashed}</b>   • 🧾 Pending <b>{vals['pending']}</b>\n"
            f"🎫 Tickets    <b>{vals['tickets']}</b>   • 🔔 Alerts <b>{vals['alerts']}</b>\n"
            f"💾 Backups    <b>{vals['backups']}</b>   • 💰 Revenue <b>৳{vals['revenue']}</b>\n\n"
            "🧠 <b>Platform status:</b> ONLINE\n"
            "🔐 <b>Control:</b> OWNER / ROLE BASED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Use the modules below to manage users, apps, plans, payments and operations.")

def admin_users_keyboard():
    kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("🔎 Search User",callback_data="adm:user_search"),types.InlineKeyboardButton("🆕 Recent",callback_data="adm:recent"))
    kb.row(types.InlineKeyboardButton("🚫 Banned",callback_data="adm:banned"),types.InlineKeyboardButton("💎 Active Plans",callback_data="adm:active"))
    kb.row(types.InlineKeyboardButton("🏆 Referrers",callback_data="adm:toprefs"),types.InlineKeyboardButton("🪙 Top Points",callback_data="adm:toppoints"))
    kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); return kb

def admin_action(call,data):
    action=data.split(":",1)[1]
    if action in ("dash","home"): show_admin(call); return
    if action=="hosting":
        total,running,crashed=hosting_overview(); conn=db(); rows=conn.execute("SELECT user_id,filename,status,last_error FROM files ORDER BY id DESC LIMIT 18").fetchall(); conn.close();
        text=f"🖥 <b>APP COMMAND CENTER</b>\n\n📦 Apps: <b>{total}</b>\n🟢 Running: <b>{running}</b>\n🔴 Crashed: <b>{crashed}</b>\n\n"
        kb=types.InlineKeyboardMarkup(row_width=2)
        for r in rows: text+=f"<code>{r['user_id']}</code> • <b>{html.escape(r['filename'])}</b> • {r['status']}\n"
        kb.row(types.InlineKeyboardButton("📜 Recent Events","adm:appevents"),types.InlineKeyboardButton("⛔ Stop All","adm:stopall"))
        kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,text,kb); return
    if action=="appevents":
        conn=db(); rows=conn.execute("SELECT * FROM app_events ORDER BY id DESC LIMIT 35").fetchall(); conn.close(); text="📜 <b>APP EVENTS</b>\n\n"+"\n".join(f"{r['event']} • <code>{r['user_id']}</code>/{html.escape(r['filename'])} • {html.escape(r['details'] or '')}" for r in rows); edit(call,text or "No events.",admin_main_keyboard()); return
    if action=="stopall":
        conn=db(); rows=conn.execute("SELECT user_id,filename FROM files").fetchall(); conn.close(); stopped=0
        for r in rows:
            if is_running(r['user_id'],r['filename']):
                ok,_=stop_process(r['user_id'],r['filename']); stopped+=1 if ok else 0
        admin_log(call.from_user.id,"emergency_stop_all",details=str(stopped)); create_alert("hosting",f"Emergency stop-all executed by {call.from_user.id}: {stopped} apps"); edit(call,f"🛑 <b>Emergency stop completed.</b>\n\nStopped: <b>{stopped}</b>",admin_main_keyboard()); return
    if action=="backups":
        conn=db(); rows=conn.execute("SELECT * FROM backups ORDER BY id DESC LIMIT 25").fetchall(); conn.close(); text="💾 <b>BACKUP CENTER</b>\n\n"+"\n".join(f"#{r['id']} • <code>{r['user_id']}</code>/{html.escape(r['filename'])} • {r['size_bytes']} B" for r in rows); edit(call,text or "No backups yet.",admin_main_keyboard()); return
    if action=="tickets":
        conn=db(); rows=conn.execute("SELECT * FROM support_tickets ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,id DESC LIMIT 20").fetchall(); conn.close(); text="🎫 <b>SUPPORT CENTER</b>\n\n"+"\n".join(f"#{r['id']} • <code>{r['user_id']}</code> • {html.escape(r['subject'])} • <b>{r['status']}</b>" for r in rows); edit(call,text or "No tickets.",admin_main_keyboard()); return
    if action=="roles":
        conn=db(); rows=conn.execute("SELECT * FROM admin_roles ORDER BY created_at DESC").fetchall(); conn.close(); text="👑 <b>ADMIN ROLES</b>\n\nOwner IDs are configured by ADMIN_IDS.\n"+"\n".join(f"<code>{r['user_id']}</code> • {html.escape(r['role'])}" for r in rows); kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("➕ Add/Change Role","adm:setrole"),types.InlineKeyboardButton("🔙 Home",callback_data="admin")); edit(call,text,kb); return
    if action=="setrole":
        edit(call,"👑 Send: <code>USER_ID ROLE</code>\nRoles: moderator / support / operator",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,receive_set_role); return
    if action=="alerts":
        conn=db(); rows=conn.execute("SELECT * FROM alerts WHERE status='open' ORDER BY id DESC LIMIT 25").fetchall(); conn.close(); text="🔔 <b>OPEN ALERTS</b>\n\n"+"\n".join(f"#{r['id']} • {html.escape(r['kind'])} • {html.escape(r['message'])}" for r in rows); edit(call,text or "✅ No open alerts.",admin_main_keyboard()); return
    if action=="users": edit(call,"👥 <b>User Management</b>\n\nSearch, inspect, ban, unban, note and manage users.",admin_users_keyboard()); return
    if action in ("recent","active","banned","toprefs","toppoints"):
        conn=db()
        if action=="recent": rows=conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 25").fetchall(); title="🆕 Recent Users"
        elif action=="active": rows=conn.execute("SELECT * FROM users WHERE banned=0 AND expires_at>? AND plan NOT IN ('No Plan','Expired') ORDER BY expires_at DESC LIMIT 25",(iso(now_utc()),)).fetchall(); title="💎 Active Plans"
        elif action=="banned": rows=conn.execute("SELECT * FROM users WHERE banned=1 ORDER BY created_at DESC LIMIT 25").fetchall(); title="🚫 Banned Users"
        elif action=="toprefs": rows=conn.execute("SELECT * FROM users ORDER BY referral_count DESC LIMIT 25").fetchall(); title="🏆 Top Referrers"
        else: rows=conn.execute("SELECT * FROM users ORDER BY points DESC LIMIT 25").fetchall(); title="🪙 Top Points"
        conn.close(); lines=[title,"━━━━━━━━━━━━━━━━━━━━"]
        for u in rows: lines.append(f"<code>{u['user_id']}</code> @{html.escape(u['username'] or 'none')} • 🪙{u['points']} • 👥{u['referral_count']} • {html.escape(u['plan'])}")
        edit(call,"\n".join(lines) if rows else title+"\n\nNo users found.",admin_users_keyboard()); return
    if action=="user_search":
        edit(call,"🔎 <b>Search User</b>\n\nSend User ID or @username.",admin_users_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,receive_admin_user_search); return
    if action=="payments":
        conn=db(); rows=conn.execute("SELECT pr.*,u.username,u.first_name FROM plan_requests pr LEFT JOIN users u ON u.user_id=pr.user_id WHERE pr.status='pending' ORDER BY pr.created_at DESC LIMIT 15").fetchall(); conn.close()
        kb=types.InlineKeyboardMarkup(row_width=1); text="🧾 <b>Pending Payments</b>\n\n"
        for r in rows: text+=f"#{r['id']} • <code>{r['user_id']}</code> • {html.escape(r['plan_key'])} • <code>{html.escape(r['txid'])}</code>\n"; kb.row(types.InlineKeyboardButton(f"✅ Approve #{r['id']}",callback_data=f"approve:{r['id']}"),types.InlineKeyboardButton(f"❌ Reject #{r['id']}",callback_data=f"reject:{r['id']}"))
        kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,text+("No pending requests." if not rows else ""),kb); return
    if action=="plans":
        plans=get_runtime_plans(); kb=types.InlineKeyboardMarkup(row_width=1); text="💎 <b>Plan Management</b>\n\n"
        for k,p in plans.items(): text+=f"{p['name']} — {p['days']}d • {p['file_limit']} files • ৳{p['price']} • 🪙{p['points']}\n"; kb.add(types.InlineKeyboardButton(f"✏️ Edit {p['name']}",callback_data=f"adm:editplan:{k}"))
        kb.row(types.InlineKeyboardButton("➕ Create Plan","adm:createplan"),types.InlineKeyboardButton("🔄 Reload","adm:plans")); kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,text,kb); return
    if action.startswith("editplan:"):
        key=action.split(":",1)[1]; p=get_runtime_plans().get(key)
        if not p: edit(call,"❌ Plan not found.",admin_main_keyboard()); return
        edit(call,f"✏️ <b>Edit {p['name']}</b>\n\nSend: <code>NAME | DAYS | FILE_LIMIT | PRICE | POINTS</code>\n\nResource limits can be configured in the plan table/env later.\n\nExample: <code>⚡ Pro | 30 | 3 | 120 | 120</code>",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m,k=key:receive_plan_edit(m,k)); return
    if action=="createplan":
        edit(call,"➕ <b>Create Plan</b>\n\nSend: <code>KEY | NAME | DAYS | FILE_LIMIT | PRICE | POINTS</code>",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,receive_plan_create); return
    if action=="economy":
        edit(call,f"🪙 <b>Economy Management</b>\n\n🎁 Daily Claim: <b>{setting('daily_claim','10')}</b>\n👥 Referral: <b>{setting('referral_reward','20')}</b>\n🎉 Referral Bonus: <b>{setting('referral_bonus','10')}</b>\n🔄 Point Transfer: <b>{'ON' if setting('point_transfer','1')=='1' else 'OFF'}</b>\n🔥 Streak: <b>{'ON' if setting('daily_streak','1')=='1' else 'OFF'}</b>",types.InlineKeyboardMarkup(row_width=2)); kb=types.InlineKeyboardMarkup(row_width=2); kb.row(types.InlineKeyboardButton("🎁 Set Daily","adm:setdaily"),types.InlineKeyboardButton("👥 Set Referral","adm:setref")); kb.row(types.InlineKeyboardButton("🔄 Transfer ON/OFF","adm:toggletransfer"),types.InlineKeyboardButton("🔥 Streak ON/OFF","adm:togglestreak")); kb.add(types.InlineKeyboardButton("🎁 Gift Points","adm:gift"),types.InlineKeyboardButton("➖ Remove Points","adm:removepoints")); kb.add(types.InlineKeyboardButton("📜 Point History","adm:pointhistory"),types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,"🪙 <b>Economy Management</b>\n\nConfigure rewards and point operations.",kb); return
    if action in ("setdaily","setref","gift","removepoints"):
        prompt={"setdaily":"Send daily points:","setref":"Send: <code>REFERRER_POINTS REFEREE_BONUS</code>","gift":"Send: <code>USER_ID POINTS</code>","removepoints":"Send: <code>USER_ID POINTS</code>"}[action]; edit(call,"🪙 "+prompt,admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m,a=action:receive_economy_action(m,a)); return
    if action=="toggletransfer": set_setting("point_transfer","0" if setting("point_transfer","1")=="1" else "1"); admin_log(call.from_user.id,"toggle_point_transfer",details=setting("point_transfer")); show_admin(call); return
    if action=="togglestreak": set_setting("daily_streak","0" if setting("daily_streak","1")=="1" else "1"); admin_log(call.from_user.id,"toggle_streak",details=setting("daily_streak")); show_admin(call); return
    if action=="pointhistory":
        conn=db(); rows=conn.execute("SELECT * FROM point_transactions ORDER BY id DESC LIMIT 30").fetchall(); conn.close(); text="📜 <b>Point History</b>\n\n"+"\n".join(f"<code>{r['user_id']}</code> {'+' if r['amount']>=0 else ''}{r['amount']} • {html.escape(r['reason'])}" for r in rows); edit(call,text or "No history.",admin_main_keyboard()); return
    if action=="codes":
        conn=db(); rows=conn.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC LIMIT 30").fetchall(); conn.close(); kb=types.InlineKeyboardMarkup(row_width=1); text="🎟 <b>Redeem Code Manager</b>\n\n"
        for r in rows: text+=f"<code>{r['code']}</code> • 🪙{r['points']} • {r['used_count']}/{('∞' if r['max_uses']==0 else r['max_uses'])}\n"; kb.add(types.InlineKeyboardButton(f"⛔ Disable {r['code']}",callback_data=f"adm:disablecode:{r['code']}"))
        kb.add(types.InlineKeyboardButton("➕ Create Code",callback_data="adm:makecode")); kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,text,kb); return
    if action.startswith("disablecode:"):
        code=action.split(":",1)[1]; conn=db(); conn.execute("UPDATE redeem_codes SET expires_at=? WHERE code=?",(iso(now_utc()),code)); conn.commit(); conn.close(); admin_log(call.from_user.id,"disable_redeem",details=code); show_admin(call); return
    if action=="makecode":
        edit(call,"🎟 Send: <code>POINTS USES EXPIRE_DAYS</code>",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,receive_makecode); return
    if action=="gift": edit(call,"🎁 Send: <code>USER_ID POINTS</code>",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m:receive_economy_action(m,"gift")); return
    if action=="broadcast":
        kb=types.InlineKeyboardMarkup(row_width=2); kb.row(types.InlineKeyboardButton("📢 All Users","adm:bcast:all"),types.InlineKeyboardButton("🟢 Active Plans","adm:bcast:active")); kb.row(types.InlineKeyboardButton("🚫 Banned","adm:bcast:banned"),types.InlineKeyboardButton("💎 Specific Plan","adm:bcast:plan")); kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,"📢 <b>Broadcast</b>\n\nChoose audience.",kb); return
    if action.startswith("bcast:"):
        target=action.split(":",1)[1]; edit(call,f"📢 Send the broadcast text now.\nTarget: <b>{html.escape(target)}</b>",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m,t=target:receive_broadcast(m,t)); return
    if action=="hosting":
        conn=db(); running=sum(1 for uid in [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()] for f in user_files(uid) if is_running(uid,f['filename'])); total=conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]; conn.close(); edit(call,f"🖥 <b>Hosting Management</b>\n\n📂 Files: <b>{total}</b>\n▶️ Running: <b>{running}</b>\n\nUse user search to manage individual processes.",admin_users_keyboard()); return
    if action=="reports":
        conn=db(); today=now_utc().date().isoformat(); new=conn.execute("SELECT COUNT(*) FROM users WHERE substr(created_at,1,10)=?",(today,)).fetchone()[0]; approved=conn.execute("SELECT COUNT(*) FROM payment_history WHERE status='approved' AND substr(created_at,1,10)=?",(today,)).fetchone()[0]; revenue=conn.execute("SELECT COALESCE(SUM(amount),0) FROM payment_history WHERE status='approved' AND substr(created_at,1,10)=?",(today,)).fetchone()[0]; redeemed=conn.execute("SELECT COUNT(*) FROM redeem_uses WHERE substr(used_at,1,10)=?",(today,)).fetchone()[0]; conn.close(); edit(call,f"📈 <b>Reports</b>\n\n📅 Today\n🆕 New users: <b>{new}</b>\n💳 Approved payments: <b>{approved}</b>\n💰 Revenue: <b>৳{revenue}</b>\n🎟 Redeems: <b>{redeemed}</b>",admin_main_keyboard()); return
    if action=="settings":
        kb=types.InlineKeyboardMarkup(row_width=2); kb.row(types.InlineKeyboardButton("📢 Join ON/OFF","adm:join"),types.InlineKeyboardButton("🔧 Maintenance","adm:maintenance")); kb.row(types.InlineKeyboardButton("💳 Payment Number","adm:payment"),types.InlineKeyboardButton("💬 Support","adm:support")); kb.add(types.InlineKeyboardButton("🔙 Admin Home",callback_data="admin")); edit(call,f"⚙️ <b>Bot Settings</b>\n\n📢 Mandatory Join: <b>{'ON' if setting('mandatory_join','1')=='1' else 'OFF'}</b>\n🔧 Maintenance: <b>{'ON' if maintenance_on() else 'OFF'}</b>\n💳 Payment: <code>{html.escape(setting('payment_number',PAYMENT_NUMBER))}</code>\n💬 Support: {html.escape(setting('support_username',SUPPORT_USERNAME))}",kb); return
    if action=="join": set_setting("mandatory_join","0" if setting("mandatory_join","1")=="1" else "1"); admin_log(call.from_user.id,"toggle_join"); show_admin(call); return
    if action=="maintenance": set_setting("maintenance_mode","0" if maintenance_on() else "1"); admin_log(call.from_user.id,"toggle_maintenance"); show_admin(call); return
    if action in ("payment","support"):
        key="payment_number" if action=="payment" else "support_username"; edit(call,f"⚙️ Send new value for <b>{key}</b>.",admin_main_keyboard()); bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m,k=key:receive_setting(m,k)); return
    if action=="security":
        edit(call,"🛡 <b>Security</b>\n\n• Mandatory Join\n• Maintenance Mode\n• Ban/Unban\n• Temporary Ban\n• Admin action logs\n• Redeem abuse protection\n• Point transaction logs\n\nUploaded Python files should still run in isolated containers/VMs for production security.",admin_main_keyboard()); return
    if action=="logs":
        conn=db(); rows=conn.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 30").fetchall(); conn.close(); text="📜 <b>Admin Logs</b>\n\n"+"\n".join(f"👑{r['admin_id']} • {html.escape(r['action'])} • {r['target_id'] or '-'} • {html.escape(r['details'] or '')}" for r in rows); edit(call,text or "No logs.",admin_main_keyboard()); return

# =========================
# ADMIN PANEL INPUT HANDLERS
# =========================

def receive_set_role(message):
    if not admin_only(message): return
    try:
        uid,role=(message.text or '').split()[:2]; uid=int(uid); role=role.lower()
        if role not in {"moderator","support","operator"} or not get_user(uid): raise ValueError
        conn=db(); conn.execute("INSERT INTO admin_roles(user_id,role,permissions,created_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",(uid,role,"{}",iso(now_utc()))); conn.commit(); conn.close(); admin_log(message.from_user.id,"set_admin_role",uid,role); bot.reply_to(message,f"✅ Role set: <code>{uid}</code> → <b>{role}</b>",reply_markup=admin_main_keyboard())
    except Exception: bot.reply_to(message,"❌ Format: USER_ID ROLE",reply_markup=admin_main_keyboard())

def receive_admin_user_search(message):
    if not admin_only(message): return
    u=user_lookup(message.text or "")
    if not u: bot.reply_to(message,"❌ User not found.",reply_markup=admin_main_keyboard()); return
    kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("🎁 Gift",callback_data=f"adm:giftuser:{u['user_id']}"),types.InlineKeyboardButton("➖ Remove Points",callback_data=f"adm:removeuser:{u['user_id']}"))
    kb.row(types.InlineKeyboardButton("🚫 Ban",callback_data=f"adm:banuser:{u['user_id']}"),types.InlineKeyboardButton("♻️ Unban",callback_data=f"adm:unbanuser:{u['user_id']}"))
    kb.add(types.InlineKeyboardButton("🔙 Users",callback_data="adm:users"))
    bot.send_message(message.chat.id,user_summary(u),reply_markup=kb)

def receive_plan_edit(message,key):
    if not admin_only(message): return
    try:
        name,days,limit,price,points=[x.strip() for x in (message.text or '').split('|')]
        days=int(days); limit=int(limit); price=int(price); points=int(points)
        if days<1 or limit<1 or price<0 or points<0: raise ValueError
        conn=db(); conn.execute("UPDATE plan_settings SET name=?,days=?,file_limit=?,price=?,points=? WHERE plan_key=?",(name,days,limit,price,points,key)); conn.commit(); conn.close(); sync_plans(); admin_log(message.from_user.id,"edit_plan",details=key); bot.send_message(message.chat.id,"✅ Plan updated.",reply_markup=admin_main_keyboard())
    except Exception: bot.send_message(message.chat.id,"❌ Format: NAME | DAYS | FILE_LIMIT | PRICE | POINTS",reply_markup=admin_main_keyboard())

def receive_plan_create(message):
    if not admin_only(message): return
    try:
        key,name,days,limit,price,points=[x.strip() for x in (message.text or '').split('|')]; days=int(days); limit=int(limit); price=int(price); points=int(points)
        if not re.fullmatch(r'[a-z0-9_]{2,32}',key) or days<1 or limit<1 or price<0 or points<0: raise ValueError
        conn=db(); conn.execute("INSERT INTO plan_settings(plan_key,name,days,file_limit,price,points,enabled) VALUES(?,?,?,?,?,?,1)",(key,name,days,limit,price,points)); conn.commit(); conn.close(); sync_plans(); admin_log(message.from_user.id,"create_plan",details=key); bot.send_message(message.chat.id,"✅ Plan created.",reply_markup=admin_main_keyboard())
    except Exception as e: bot.send_message(message.chat.id,f"❌ Could not create plan: <code>{html.escape(str(e))}</code>",reply_markup=admin_main_keyboard())

def receive_fixed_user_points(message,user_id,add):
    if not admin_only(message): return
    try:
        pts=int((message.text or '').strip()); assert pts>0 and get_user(user_id)
        amount=pts if add else -pts
        add_points_logged(user_id,amount,"Admin gift" if add else "Admin removal",message.from_user.id)
        admin_log(message.from_user.id,"gift_points" if add else "remove_points",user_id,str(pts))
        bot.reply_to(message,f"✅ {'+' if add else '-'}{pts} points {'gifted' if add else 'removed'} for <code>{user_id}</code>.",reply_markup=admin_main_keyboard())
    except Exception: bot.reply_to(message,"❌ Invalid points amount.",reply_markup=admin_main_keyboard())

def receive_economy_action(message,action):
    if not admin_only(message): return
    try:
        parts=(message.text or '').split()
        if action=='setdaily':
            n=int(parts[0]); assert n>=0; set_setting('daily_claim',n); admin_log(message.from_user.id,'set_daily',details=str(n)); bot.reply_to(message,f'✅ Daily claim set to {n}.'); return
        if action=='setref':
            a,b=map(int,parts[:2]); assert a>=0 and b>=0; set_setting('referral_reward',a); set_setting('referral_bonus',b); admin_log(message.from_user.id,'set_referral',details=f'{a}/{b}'); bot.reply_to(message,'✅ Referral rewards updated.'); return
        uid=int(parts[0]); pts=int(parts[1]); u=get_user(uid); assert u and pts>0
        if action=='gift': add_points_logged(uid,pts,'Admin gift',message.from_user.id); bot.reply_to(message,f'✅ +{pts} points gifted.');
        else: add_points_logged(uid,-pts,'Admin removal',message.from_user.id); bot.reply_to(message,f'✅ Up to {pts} points removed.')
        admin_log(message.from_user.id,action,uid,str(pts))
    except Exception: bot.reply_to(message,'❌ Invalid format.')

def receive_setting(message,key):
    if not admin_only(message): return
    val=(message.text or '').strip()
    if not val: bot.reply_to(message,'❌ Empty value.'); return
    set_setting(key,val); admin_log(message.from_user.id,'setting_change',details=f'{key}={val}'); bot.reply_to(message,'✅ Setting updated.',reply_markup=admin_main_keyboard())

def receive_broadcast(message,target):
    if not admin_only(message): return
    text=message.text or ''; conn=db()
    if target=='all': rows=conn.execute('SELECT user_id FROM users WHERE banned=0').fetchall()
    elif target=='active': rows=conn.execute("SELECT user_id FROM users WHERE banned=0 AND expires_at>? AND plan NOT IN ('No Plan','Expired')",(iso(now_utc()),)).fetchall()
    elif target=='banned': rows=conn.execute('SELECT user_id FROM users WHERE banned=1').fetchall()
    elif target=='plan': rows=[]
    else: rows=[]
    conn.close(); sent=failed=0
    for r in rows:
        try: bot.send_message(r['user_id'],text); sent+=1
        except Exception: failed+=1
        if (sent+failed)%BROADCAST_BATCH==0: time.sleep(1)
    conn=db(); cur=conn.execute('INSERT INTO broadcasts(admin_id,text,target,sent,failed,created_at) VALUES(?,?,?,?,?,?)',(message.from_user.id,text,target,sent,failed,iso(now_utc()))); conn.commit(); conn.close(); admin_log(message.from_user.id,'broadcast',details=f'{target} sent={sent} failed={failed}'); bot.send_message(message.chat.id,f'📢 Broadcast finished.\n\n✅ Sent: {sent}\n❌ Failed: {failed}',reply_markup=admin_main_keyboard())

# =========================
# ADMIN COMMANDS
# =========================

def admin_only(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "🚫 Admin only.")
        return False
    return True


@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if not admin_only(message):
        return

    user = create_user(message.from_user)

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🛠 Open Admin Panel", callback_data="admin"
    ))

    bot.reply_to(
        message,
        "🛠 <b>Admin Panel</b>",
        reply_markup=kb
    )


def receive_makecode(message):
    if not admin_only(message):
        return
    parts=(message.text or "").split()
    if len(parts) < 1 or len(parts) > 3:
        bot.reply_to(message, "Usage: <code>POINTS [USES] [EXPIRE_DAYS]</code>")
        return
    try:
        points=int(parts[0]); uses=int(parts[1]) if len(parts)>1 else 1; days=int(parts[2]) if len(parts)>2 else 0
        if points<=0 or uses<0 or days<0: raise ValueError
    except ValueError:
        bot.reply_to(message,"❌ Invalid values.")
        return
    code=create_redeem_code(points,uses,days)
    if not code:
        bot.reply_to(message,"❌ Code create করা যায়নি.")
        return
    expiry=f"{days} days" if days else "No expiry"
    bot.reply_to(message, f"🎟 <b>Redeem Code Created</b>\n\n🔑 Code: <code>{code}</code>\n🪙 Points: <b>{points}</b>\n👥 Uses: <b>{'Unlimited' if uses==0 else uses}</b>\n⏳ Expiry: <b>{expiry}</b>")


@bot.message_handler(commands=["makecode"])
def makecode_cmd(message):
    if not admin_only(message): return
    parts=message.text.split()
    if len(parts) < 2 or len(parts) > 4:
        bot.reply_to(message, "Usage: <code>/makecode POINTS [USES] [EXPIRE_DAYS]</code>")
        return
    try:
        points=int(parts[1]); uses=int(parts[2]) if len(parts)>2 else 1; days=int(parts[3]) if len(parts)>3 else 0
        if points<=0 or uses<0 or days<0: raise ValueError
    except ValueError:
        bot.reply_to(message,"❌ Invalid values."); return
    code=create_redeem_code(points,uses,days)
    if not code:
        bot.reply_to(message,"❌ Code create করা যায়নি।"); return
    expiry = f"{days} days" if days else "No expiry"
    bot.reply_to(message, f"🎟 <b>Redeem Code Created</b>\n\n🔑 Code: <code>{code}</code>\n🪙 Points: <b>{points}</b>\n👥 Uses: <b>{'Unlimited' if uses==0 else uses}</b>\n⏳ Expiry: <b>{expiry}</b>")


@bot.message_handler(commands=["gift"])
def gift_cmd(message):
    if not admin_only(message): return
    parts=message.text.split()
    if len(parts)!=3:
        bot.reply_to(message,"Usage: <code>/gift USER_ID POINTS</code>"); return
    try: uid=int(parts[1]); pts=int(parts[2])
    except ValueError:
        bot.reply_to(message,"❌ Invalid values."); return
    if send_points_gift(uid,pts,message.from_user.id):
        bot.reply_to(message,f"🎁 +{pts} Points gifted to <code>{uid}</code>.\n💰 New balance: <b>{points_of(uid)}</b>")
    else:
        bot.reply_to(message,"❌ User not found or invalid points.")


@bot.message_handler(commands=["points"])
def points_cmd(message):
    if not is_channel_member(message.from_user.id):
        join_required(message.chat.id)
        return
    user=create_user(message.from_user)
    bot.reply_to(message, f"🪙 <b>Your Points</b>\n\n💰 Balance: <b>{user['points']}</b> Points", reply_markup=points_keyboard())


@bot.message_handler(commands=["users"])
def users_cmd(message):
    if not admin_only(message):
        return

    conn = db()
    rows = conn.execute(
        "SELECT * FROM users ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()

    if not rows:
        bot.reply_to(message, "No users.")
        return

    text = "👥 <b>Users</b>\n\n"
    for u in rows:
        text += (
            f"• <code>{u['user_id']}</code> "
            f"{html.escape(u['first_name'] or '')} — "
            f"{html.escape(u['plan'])} — "
            f"{remaining_text(u)}\n"
        )

    bot.reply_to(message, text)


@bot.message_handler(commands=["grant"])
def grant_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 4:
        bot.reply_to(
            message,
            "Usage:\n<code>/grant USER_ID DAYS FILE_LIMIT</code>"
        )
        return

    try:
        user_id = int(parts[1])
        days = int(parts[2])
        limit = int(parts[3])

        if days < 1 or limit < 1:
            raise ValueError

    except ValueError:
        bot.reply_to(message, "❌ Invalid values.")
        return

    target = get_user(user_id)

    if not target:
        bot.reply_to(message, "❌ User not found. They must /start first.")
        return

    expires = now_utc() + timedelta(days=days)

    conn = db()
    conn.execute("""
        UPDATE users
        SET plan='Premium', expires_at=?, file_limit=?, banned=0
        WHERE user_id=?
    """, (iso(expires), limit, user_id))
    conn.commit()
    conn.close()

    bot.reply_to(
        message,
        f"✅ Granted Premium to <code>{user_id}</code>\n"
        f"⏳ {days} days\n"
        f"📁 Limit: {limit}"
    )

    try:
        bot.send_message(
            user_id,
            f"🎉 <b>Your plan was upgraded!</b>\n\n"
            f"💎 Premium\n"
            f"⏳ {days} days\n"
            f"📁 File limit: {limit}"
        )
    except Exception:
        pass


@bot.message_handler(commands=["revoke"])
def revoke_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/revoke USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    expires = now_utc()

    conn = db()
    conn.execute(
        "UPDATE users SET plan='Expired', expires_at=?, file_limit=0 WHERE user_id=?",
        (iso(expires), user_id)
    )
    conn.commit()
    conn.close()

    # Stop all user processes.
    for f in user_files(user_id):
        stop_process(user_id, f["filename"])

    bot.reply_to(message, f"✅ Access revoked for <code>{user_id}</code>")


@bot.message_handler(commands=["limit"])
def limit_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: <code>/limit USER_ID FILE_LIMIT</code>"
        )
        return

    try:
        user_id = int(parts[1])
        limit = int(parts[2])

        if limit < 1:
            raise ValueError

    except ValueError:
        bot.reply_to(message, "❌ Invalid values.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET file_limit=? WHERE user_id=?",
        (limit, user_id)
    )
    conn.commit()
    conn.close()

    bot.reply_to(
        message,
        f"✅ File/process limit updated to <b>{limit}</b> "
        f"for <code>{user_id}</code>"
    )


@bot.message_handler(commands=["ban"])
def ban_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/ban USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    if not get_user(user_id):
        bot.reply_to(message, "❌ User not found.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET banned=1 WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()

    for f in user_files(user_id):
        stop_process(user_id, f["filename"])

    bot.reply_to(message, f"🚫 Banned <code>{user_id}</code>")


@bot.message_handler(commands=["unban"])
def unban_cmd(message):
    if not admin_only(message):
        return

    parts = message.text.split()

    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/unban USER_ID</code>")
        return

    try:
        user_id = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ Invalid user ID.")
        return

    conn = db()
    conn.execute(
        "UPDATE users SET banned=0 WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()

    bot.reply_to(message, f"♻️ Unbanned <code>{user_id}</code>")


# =========================
# CLEANUP
# =========================

def cleanup_expired_users():
    while True:
        try:
            conn = db()
            rows = conn.execute("""
                SELECT * FROM users
                WHERE banned=0 AND plan != 'Expired'
            """).fetchall()
            conn.close()

            for user in rows:
                if is_admin(user["user_id"]):
                    continue

                if is_expired(user):
                    for f in user_files(user["user_id"]):
                        stop_process(user["user_id"], f["filename"])

                    conn = db()
                    conn.execute(
                        "UPDATE users SET plan='Expired', file_limit=0 WHERE user_id=?",
                        (user["user_id"],)
                    )
                    conn.commit()
                    conn.close()

        except Exception as e:
            print("Cleanup error:", e)

        time.sleep(60)


# =========================
# REPLY KEYBOARD BUTTONS
# =========================

@bot.message_handler(func=lambda m: m.text in {
    "🟢 📤 Upload File", "🔵 📂 My Files", "🟣 💎 Plans",
    "🟡 ⚡ Latency", "🟠 📊 Statistics", "🔷 👤 Account",
    "🩷 🎬 Tutorial", "🟢 💬 Support", "🪙 💰 Points", "🔴 🛠 Admin Panel"
})
def reply_keyboard_buttons(message):
    user = create_user(message.from_user)
    text = message.text

    if not is_channel_member(message.from_user.id):
        join_required(message.chat.id)
        return

    if text == "🟢 📤 Upload File":
        ok, reason = access_ok(message.from_user.id)
        if not ok:
            bot.send_message(message.chat.id, "❌ " + reason, reply_markup=main_keyboard(message.from_user.id))
            return
        bot.send_message(
            message.chat.id,
            "📤 <b>Upload File</b>\n\n"
            "Send your Python <code>.py</code> file here.\n"
            f"Maximum size: <b>{MAX_UPLOAD_MB} MB</b>",
            reply_markup=main_keyboard(message.from_user.id)
        )
        return

    if text == "🔵 📂 My Files":
        files = user_files(message.from_user.id)
        if not files:
            bot.send_message(message.chat.id, "📂 <b>My Files</b>\n\nNo files uploaded yet.", reply_markup=main_keyboard(message.from_user.id))
            return
        msg = "📂 <b>My Files</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for f in files:
            state = "🟢 Running" if is_running(message.from_user.id, f["filename"]) else "🔴 Stopped"
            msg += f"• <code>{html.escape(f['filename'])}</code> — {state}\n"
            kb.add(types.InlineKeyboardButton(f"⚙️ {f['filename']}", callback_data=f"status:{f['filename']}"))
        bot.send_message(message.chat.id, msg, reply_markup=kb)
        return

    if text == "🟣 💎 Plans":
        msg = "💎 <b>SHUVO X HOST — HOSTING PLANS</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for key, p in PLANS.items():
            msg += f"{p['name']}\n⏳ {p['days']} Days • 📁 {p['file_limit']} File\n💰 Price: <b>৳{p['price']}</b>\n\n"
            kb.add(types.InlineKeyboardButton(f"🛒 Buy {p['name']} • ৳{p['price']}", callback_data=f"buy:{key}"))
        msg += "━━━━━━━━━━━━━━━━━━━━\n🛒 Plan কিনে Transaction ID দিলে request Admin-এর কাছে যাবে।\n✅ Admin approve করলে hosting access চালু হবে।"
        bot.send_message(message.chat.id, msg, reply_markup=kb)
        return

    if text == "🪙 💰 Points":
        # Use a fake callback-like display helper without requiring callback query.
        bot.send_message(message.chat.id,
                         "🪙 <b>POINT WALLET</b>\n\n"
                         f"💰 Balance: <b>{user['points']}</b> Points\n\n"
                         f"🎁 Daily Claim: +{DAILY_CLAIM_POINTS} Points\n"
                         f"👥 Referral: +{REFERRAL_POINTS} Points\n"
                         f"🎉 Referred User Bonus: +{REFERRAL_BONUS_POINTS} Points",
                         reply_markup=points_keyboard())
        return

    if text == "🟡 ⚡ Latency":
        start = time.perf_counter()
        latency = round((time.perf_counter() - start) * 1000, 2)
        bot.send_message(message.chat.id, f"⚡ <b>Latency</b>\n\nTelegram response check: <b>{latency} ms</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🟠 📊 Statistics":
        files = user_files(message.from_user.id)
        running = sum(1 for f in files if is_running(message.from_user.id, f["filename"]))
        bot.send_message(message.chat.id, f"📊 <b>Statistics</b>\n\n📁 Total files: <b>{len(files)}</b>\n🟢 Running: <b>{running}</b>\n🔴 Stopped: <b>{len(files)-running}</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🔷 👤 Account":
        username = "@" + html.escape(user["username"]) if user["username"] else "Not set"
        bot.send_message(message.chat.id, f"👤 <b>Account</b>\n\n🆔 ID: <code>{user['user_id']}</code>\n👤 Username: {username}\n💎 Plan: <b>{html.escape(user['plan'])}</b>\n⏳ Remaining: <b>{remaining_text(user)}</b>\n📁 File limit: <b>{'∞' if is_admin(user['user_id']) else user['file_limit']}</b>", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🩷 🎬 Tutorial":
        bot.send_message(message.chat.id, "🎬 <b>Tutorial</b>\n\n1️⃣ 💎 Plans থেকে একটি plan নিন\n2️⃣ Payment করে TXID submit করুন\n3️⃣ Admin approve করলে hosting চালু হবে\n4️⃣ 📤 Upload File চাপুন\n5️⃣ 📂 My Files → ▶️ Start\n6️⃣ ⏹ Stop / 🗑 Delete ব্যবহার করুন", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🟢 💬 Support":
        bot.send_message(message.chat.id, f"💬 <b>Support</b>\n\nSupport: {html.escape(SUPPORT_USERNAME)}", reply_markup=main_keyboard(message.from_user.id))
        return

    if text == "🔴 🛠 Admin Panel":
        if not is_admin(message.from_user.id):
            bot.send_message(message.chat.id, "🚫 Admin only.", reply_markup=main_keyboard(message.from_user.id))
            return
        bot.send_message(message.chat.id, admin_dashboard_text(), reply_markup=admin_main_keyboard())


# =========================
# ERROR HANDLER
# =========================

@bot.message_handler(func=lambda m: m.text == "💾 Backups", content_types=["text"])
def user_backups_menu(message):
    u=create_user(message.from_user); conn=db(); rows=conn.execute("SELECT * FROM backups WHERE user_id=? ORDER BY id DESC LIMIT 12",(u['user_id'],)).fetchall(); conn.close(); text="💾 <b>MY BACKUPS</b>\n\n"+"\n".join(f"#{r['id']} • {html.escape(r['filename'])} • {r['size_bytes']} B" for r in rows); bot.send_message(message.chat.id,text or "No backups yet.",reply_markup=main_keyboard(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "🎫 Support Ticket", content_types=["text"])
def ticket_start(message):
    bot.send_message(message.chat.id,"🎫 <b>Support Ticket</b>\n\nSend: <code>SUBJECT | MESSAGE</code>",reply_markup=main_keyboard(message.from_user.id))
    bot.register_next_step_handler_by_chat_id(message.chat.id,receive_ticket)

def receive_ticket(message):
    try:
        subject,body=(message.text or "").split("|",1); subject=subject.strip()[:100]; body=body.strip()[:2000]
        if not subject or not body: raise ValueError
        conn=db(); cur=conn.execute("INSERT INTO support_tickets(user_id,subject,message,created_at,updated_at) VALUES(?,?,?,?,?)",(message.from_user.id,subject,body,iso(now_utc()),iso(now_utc()))); tid=cur.lastrowid; conn.commit(); conn.close()
        for aid in ADMIN_IDS:
            try: bot.send_message(aid,f"🎫 <b>New Ticket #{tid}</b>\n👤 <code>{message.from_user.id}</code>\n<b>{html.escape(subject)}</b>\n{html.escape(body)}")
            except Exception: pass
        bot.send_message(message.chat.id,f"✅ Ticket <b>#{tid}</b> created.",reply_markup=main_keyboard(message.from_user.id))
    except Exception: bot.send_message(message.chat.id,"❌ Format: SUBJECT | MESSAGE",reply_markup=main_keyboard(message.from_user.id))



# =========================
# RUN
# =========================

def process_watchdog():
    while True:
        try:
            conn=db(); rows=conn.execute("SELECT user_id,filename FROM files").fetchall(); conn.close()
            for r in rows:
                uid,fn=r["user_id"],r["filename"]
                with process_lock: p=processes.get(uid,{}).get(fn)
                if p and p.poll() is not None:
                    code=p.returncode; processes.get(uid,{}).pop(fn,None)
                    meta=app_meta(uid,fn); auto=bool(meta and meta["auto_restart"])
                    conn=db(); conn.execute("UPDATE files SET status=?,last_error=? WHERE user_id=? AND filename=?",("CRASHED" if code else "STOPPED",f"exit={code}",uid,fn)); conn.execute("UPDATE app_settings SET last_exit_code=?,restart_count=restart_count+? WHERE user_id=? AND filename=?",(code,1 if auto and code else 0,uid,fn)); conn.commit(); conn.close(); app_log(uid,fn,"EXITED",f"code={code}")
                    if auto and code and not is_expired(get_user(uid)):
                        time.sleep(1); start_process(uid,fn)
            time.sleep(3)
        except Exception:
            time.sleep(5)


# =========================
# HOSTING HELPERS MISSING FROM ORIGINAL V2 SOURCE
# =========================

def edit(call, text, markup=None):
    try:
        bot.edit_message_text(call.message.chat.id, call.message.message_id, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try: bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        except Exception: pass


def admin_main_keyboard():
    kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("🖥 Hosting","adm:hosting"),types.InlineKeyboardButton("👥 Users","adm:users"))
    kb.row(types.InlineKeyboardButton("🧾 Payments","adm:payments"),types.InlineKeyboardButton("💎 Plans","adm:plans"))
    kb.row(types.InlineKeyboardButton("🪙 Economy","adm:economy"),types.InlineKeyboardButton("🎟 Codes","adm:codes"))
    kb.row(types.InlineKeyboardButton("📢 Broadcast","adm:broadcast"),types.InlineKeyboardButton("⚙️ Settings","adm:settings"))
    kb.row(types.InlineKeyboardButton("💾 Backups","adm:backups"),types.InlineKeyboardButton("🎫 Tickets","adm:tickets"))
    kb.row(types.InlineKeyboardButton("👑 Roles","adm:roles"),types.InlineKeyboardButton("🔔 Alerts","adm:alerts"))
    kb.add(types.InlineKeyboardButton("🛡 Security","adm:security"),types.InlineKeyboardButton("📜 Logs","adm:logs"))
    return kb


def show_admin(call):
    edit(call, admin_dashboard_text(), admin_main_keyboard())


def points_of(user_id):
    row=get_user(user_id); return int(row["points"] or 0) if row else 0


def points_keyboard():
    kb=types.InlineKeyboardMarkup(row_width=2)
    kb.row(types.InlineKeyboardButton("🎁 Daily Claim","points:claim"),types.InlineKeyboardButton("🔗 Referral","points:ref"))
    return kb


def main_keyboard(user_id):
    kb=types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row("🟢 📤 Upload File","🔵 📂 My Files")
    kb.row("🟣 💎 Plans","🟡 ⚡ Latency")
    kb.row("🟠 📊 Statistics","🔷 👤 Account")
    kb.row("🩷 🎬 Tutorial","🟢 💬 Support")
    kb.row("🪙 💰 Points","💾 Backups")
    kb.row("🎫 Support Ticket")
    if is_admin(user_id): kb.row("🔴 🛠 Admin Panel")
    return kb


def is_channel_member(user_id):
    if is_admin(user_id): return True
    if not setting("mandatory_join","1"): return True
    if not FORCE_JOIN_CHANNEL: return True
    try:
        m=bot.get_chat_member(FORCE_JOIN_CHANNEL,user_id)
        return m.status in ("member","administrator","creator")
    except Exception:
        return False


def join_required(chat_id):
    kb=types.InlineKeyboardMarkup()
    if FORCE_JOIN_URL: kb.add(types.InlineKeyboardButton("📢 Join Channel",url=FORCE_JOIN_URL))
    bot.send_message(chat_id,"📢 <b>Channel Join Required</b>\n\nPlease join the required channel, then try again.",parse_mode="HTML",reply_markup=kb)


def user_files(user_id):
    conn=db(); rows=conn.execute("SELECT * FROM files WHERE user_id=? ORDER BY id DESC",(user_id,)).fetchall(); conn.close(); return rows


def is_running(user_id, filename):
    with process_lock:
        p=processes.get(user_id,{}).get(filename)
    return bool(p and p.poll() is None)


def app_meta(user_id, filename):
    conn=db(); r=conn.execute("SELECT * FROM app_settings WHERE user_id=? AND filename=?",(user_id,filename)).fetchone(); conn.close(); return r


def app_log(user_id, filename, event, details=""):
    conn=db(); conn.execute("INSERT INTO app_events(user_id,filename,event,details,created_at) VALUES(?,?,?,?,?)",(user_id,filename,event,details,iso(now_utc()))); conn.commit(); conn.close()


def create_alert(kind, message):
    conn=db(); conn.execute("INSERT INTO alerts(kind,message,created_at) VALUES(?,?,?)",(kind,message,iso(now_utc()))); conn.commit(); conn.close()


def hosting_overview():
    conn=db(); total=conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]; crashed=conn.execute("SELECT COUNT(*) FROM files WHERE status='CRASHED'").fetchone()[0]; conn.close()
    conn=db(); user_ids=[r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]; conn.close()
    running=sum(1 for uid in user_ids for f in user_files(uid) if is_running(uid,f["filename"]))
    return total,running,crashed


def start_process(user_id, filename):
    row=get_user(user_id); ok,reason=access_ok(user_id)
    if not ok: return False,reason
    fr=next((r for r in user_files(user_id) if r["filename"]==filename),None)
    if not fr: return False,"File not found."
    path=Path(fr["path"]).resolve(); base=user_dir(user_id).resolve()
    if base not in path.parents or not path.is_file(): return False,"Invalid file path."
    if path.suffix.lower() != ".py": return False,"Only .py files can be started."
    if is_running(user_id,filename): return True,"Already running."
    p=subprocess.Popen([PYTHON_BIN,str(path)],cwd=str(base),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    with process_lock: processes.setdefault(user_id,{})[filename]=p
    conn=db(); conn.execute("UPDATE files SET status='RUNNING',last_error='',deploy_count=deploy_count+1 WHERE user_id=? AND filename=?",(user_id,filename)); conn.execute("INSERT INTO app_settings(user_id,filename,last_started_at) VALUES(?,?,?) ON CONFLICT(user_id,filename) DO UPDATE SET last_started_at=excluded.last_started_at",(user_id,filename,iso(now_utc()))); conn.commit(); conn.close(); app_log(user_id,filename,"STARTED")
    return True,f"Started PID {p.pid}"


def stop_process(user_id, filename):
    with process_lock: p=processes.get(user_id,{}).get(filename)
    if not p or p.poll() is not None:
        conn=db(); conn.execute("UPDATE files SET status='STOPPED' WHERE user_id=? AND filename=?",(user_id,filename)); conn.commit(); conn.close(); return False,"Not running."
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        try: p.terminate()
        except Exception: pass
    try: p.wait(timeout=5)
    except Exception:
        try: p.kill()
        except Exception: pass
    with process_lock: processes.get(user_id,{}).pop(filename,None)
    conn=db(); conn.execute("UPDATE files SET status='STOPPED' WHERE user_id=? AND filename=?",(user_id,filename)); conn.execute("UPDATE app_settings SET last_stopped_at=? WHERE user_id=? AND filename=?",(iso(now_utc()),user_id,filename)); conn.commit(); conn.close(); app_log(user_id,filename,"STOPPED")
    return True,"Stopped."


def create_redeem_code(points, uses, days, admin_id=None):
    import secrets
    code="SX-"+secrets.token_hex(4).upper()
    expiry=iso(now_utc()+timedelta(days=days)) if days else ""
    try:
        conn=db(); conn.execute("INSERT INTO redeem_codes(code,points,max_uses,expires_at,created_at,created_by) VALUES(?,?,?,?,?,?)",(code,points,uses,expiry,iso(now_utc()),admin_id or OWNER_ID)); conn.commit(); conn.close(); return code
    except Exception: return None


def send_points_gift(user_id, points, admin_id):
    if points<=0: return False
    return add_points_logged(user_id,points,"Admin gift",admin_id)


# =========================
# HOSTING USER CALLBACKS / UPLOADS / PAYMENT FLOW
# =========================

def host_callback(call):
    data=call.data or ""
    uid=call.from_user.id
    try:
        if data=="admin" or data.startswith("adm:"):
            if not is_admin(uid): return bot.answer_callback_query(call.id,"Admin only.",show_alert=True)
            admin_action(call,data); bot.answer_callback_query(call.id); return
        if data.startswith("status:"):
            fn=data.split(":",1)[1]; row=next((r for r in user_files(uid) if r["filename"]==fn),None)
            if not row: return bot.answer_callback_query(call.id,"File not found",show_alert=True)
            running=is_running(uid,fn); ok,reason=access_ok(uid)
            kb=types.InlineKeyboardMarkup(row_width=2)
            if running: kb.add(types.InlineKeyboardButton("⏹ Stop",callback_data=f"stopapp:{fn}"))
            else: kb.add(types.InlineKeyboardButton("▶️ Start",callback_data=f"startapp:{fn}"))
            kb.add(types.InlineKeyboardButton("🗑 Delete",callback_data=f"deleteapp:{fn}"))
            kb.add(types.InlineKeyboardButton("🔙 Files",callback_data="files"))
            bot.edit_message_text(call.message.chat.id,call.message.message_id,f"📄 <b>{html.escape(fn)}</b>\n\nStatus: <b>{'RUNNING' if running else row['status']}</b>\nAccess: <b>{'OK' if ok else html.escape(reason)}</b>",parse_mode="HTML",reply_markup=kb); bot.answer_callback_query(call.id); return
        if data=="files":
            fake=types.SimpleNamespace(message=call.message,from_user=call.from_user)
            files=user_files(uid); text="📂 <b>My Files</b>\n\n"; kb=types.InlineKeyboardMarkup(row_width=1)
            for f in files: text+=f"• <code>{html.escape(f['filename'])}</code> — {'🟢' if is_running(uid,f['filename']) else '🔴'}\n"; kb.add(types.InlineKeyboardButton(f"⚙️ {f['filename']}",callback_data=f"status:{f['filename']}"))
            bot.edit_message_text(call.message.chat.id,call.message.message_id,text if files else "📂 <b>No files.</b>",parse_mode="HTML",reply_markup=kb); bot.answer_callback_query(call.id); return
        if data.startswith("startapp:"):
            fn=data.split(":",1)[1]; ok,msg=start_process(uid,fn); bot.answer_callback_query(call.id,msg,show_alert=not ok); return
        if data.startswith("stopapp:"):
            fn=data.split(":",1)[1]; ok,msg=stop_process(uid,fn); bot.answer_callback_query(call.id,msg,show_alert=not ok); return
        if data.startswith("deleteapp:"):
            fn=data.split(":",1)[1]; row=next((r for r in user_files(uid) if r["filename"]==fn),None)
            if row:
                stop_process(uid,fn); p=Path(row["path"])
                try: p.unlink(missing_ok=True)
                except Exception: pass
                conn=db(); conn.execute("DELETE FROM files WHERE user_id=? AND filename=?",(uid,fn)); conn.execute("DELETE FROM app_settings WHERE user_id=? AND filename=?",(uid,fn)); conn.commit(); conn.close(); bot.answer_callback_query(call.id,"Deleted")
            return
        if data.startswith("buy:"):
            key=data.split(":",1)[1]; p=get_runtime_plans().get(key)
            if not p: return bot.answer_callback_query(call.id,"Plan unavailable",show_alert=True)
            bot.send_message(call.message.chat.id,f"🛒 <b>{html.escape(p['name'])}</b>\n\n💰 Price: <b>৳{p['price']}</b>\n💳 Payment: <code>{html.escape(PAYMENT_NUMBER or 'Not configured')}</code>\n\nPayment করার পর আপনার Transaction ID পাঠান।",parse_mode="HTML")
            bot.register_next_step_handler_by_chat_id(call.message.chat.id,lambda m,k=key: receive_txid(m,k)); bot.answer_callback_query(call.id); return
        if data.startswith("approve:") or data.startswith("reject:"):
            if not is_admin(uid): return
            action,rid=data.split(":",1); conn=db(); req=conn.execute("SELECT * FROM plan_requests WHERE id=?",(int(rid),)).fetchone()
            if not req or req["status"]!="pending": conn.close(); return bot.answer_callback_query(call.id,"Already reviewed",show_alert=True)
            if action=="approve":
                set_plan_for_user(req["user_id"],req["plan_key"],uid); status="approved"; p=get_runtime_plans()[req["plan_key"]]; conn.execute("INSERT INTO payment_history(request_id,user_id,plan_key,amount,status,txid,created_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?)",(req["id"],req["user_id"],req["plan_key"],p["price"],status,req["txid"],req["created_at"],iso(now_utc()))); bot.send_message(req["user_id"],f"✅ <b>Payment Approved</b>\n\nPlan: <b>{html.escape(p['name'])}</b>\nHosting access is now active.",parse_mode="HTML")
            else:
                status="rejected"; conn.execute("INSERT INTO payment_history(request_id,user_id,plan_key,amount,status,txid,created_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?)",(req["id"],req["user_id"],req["plan_key"],get_runtime_plans().get(req["plan_key"],{}).get("price",0),status,req["txid"],req["created_at"],iso(now_utc()))); bot.send_message(req["user_id"],"❌ <b>Payment request rejected.</b>",parse_mode="HTML")
            conn.execute("UPDATE plan_requests SET status=?,reviewed_at=? WHERE id=?",(status,iso(now_utc()),int(rid))); conn.commit(); conn.close(); bot.answer_callback_query(call.id,status.title()); admin_action(call,"adm:payments"); return
        if data=="points:claim":
            u=get_user(uid); today=now_utc().date().isoformat()
            if not u: return
            if u["last_claim_date"]==today: return bot.answer_callback_query(call.id,"Already claimed today",show_alert=True)
            add=DAILY_CLAIM_POINTS; conn=db(); conn.execute("UPDATE users SET points=points+?,last_claim_date=? WHERE user_id=?",(add,today,uid)); conn.commit(); conn.close(); record_points(uid,add,"Daily claim"); bot.answer_callback_query(call.id,f"+{add} points")
    except Exception as e:
        print("Host callback error:",e)
        try: bot.answer_callback_query(call.id,"Action failed",show_alert=True)
        except Exception: pass


def receive_txid(message, plan_key):
    if not is_channel_member(message.from_user.id): join_required(message.chat.id); return
    p=get_runtime_plans().get(plan_key)
    txid=(message.text or "").strip()[:200]
    if not p or not txid: return bot.reply_to(message,"❌ Invalid Transaction ID.")
    u=create_user(message.from_user)
    conn=db(); cur=conn.execute("INSERT INTO plan_requests(user_id,plan_key,txid,status,created_at) VALUES(?,?,?,?,?)",(u["user_id"],plan_key,txid,"pending",iso(now_utc()))); rid=cur.lastrowid; conn.commit(); conn.close()
    bot.reply_to(message,f"✅ Request #{rid} submitted. Admin approval pending.")
    for aid in ADMIN_IDS:
        try:
            kb=types.InlineKeyboardMarkup(row_width=2); kb.add(types.InlineKeyboardButton("✅ Approve",callback_data=f"approve:{rid}"),types.InlineKeyboardButton("❌ Reject",callback_data=f"reject:{rid}"))
            bot.send_message(aid,f"🧾 <b>New Payment Request #{rid}</b>\nUser: <code>{u['user_id']}</code>\nPlan: <b>{html.escape(p['name'])}</b>\nAmount: <b>৳{p['price']}</b>\nTXID: <code>{html.escape(txid)}</code>",parse_mode="HTML",reply_markup=kb)
        except Exception: pass


@bot.message_handler(content_types=["document"])
def unified_document_handler(message):
    uid=message.from_user.id
    # Hosting upload is preferred when the user has a hosting account/plan.
    u=create_user(message.from_user)
    ok,reason=access_ok(uid)
    if ok:
        doc=message.document
        if doc.file_size and doc.file_size>MAX_UPLOAD_BYTES: return bot.reply_to(message,f"❌ File too large. Max {MAX_UPLOAD_MB} MB.")
        name=os.path.basename(doc.file_name or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.py",name,re.I): return bot.reply_to(message,"❌ Only safe `.py` filenames are allowed.")
        if len(user_files(uid)) >= u["file_limit"] and not is_admin(uid): return bot.reply_to(message,"❌ Your file limit has been reached.")
        info=bot.get_file(doc.file_id); data=bot.download_file(info.file_path); path=user_dir(uid)/name; path.write_bytes(data)
        conn=db(); conn.execute("INSERT INTO files(user_id,filename,path,created_at,status) VALUES(?,?,?,?,?) ON CONFLICT(user_id,filename) DO UPDATE SET path=excluded.path,status='STOPPED'",(uid,name,str(path),iso(now_utc()),"STOPPED")); conn.execute("INSERT INTO app_settings(user_id,filename) VALUES(?,?) ON CONFLICT(user_id,filename) DO NOTHING",(uid,name)); conn.commit(); conn.close(); bot.reply_to(message,f"✅ <b>Uploaded:</b> <code>{html.escape(name)}</code>",parse_mode="HTML",reply_markup=main_keyboard(uid)); return
    # Fall back to the legacy terminal upload for whitelisted users.
    if is_allowed_user(uid):
        try:
            info=bot.get_file(message.document.file_id); data=bot.download_file(info.file_path); safe=os.path.basename(message.document.file_name or "upload.bin"); out=Path(current_dir)/safe; out.write_bytes(data); bot.reply_to(message,f"📤 Uploaded to legacy workspace: <code>{html.escape(str(out))}</code>",parse_mode="HTML")
        except Exception as e: bot.reply_to(message,f"❌ Upload failed: {html.escape(str(e))}")
        return
    bot.reply_to(message,"⛔️ Access restricted.")


# This handler must be registered before the legacy direct-terminal fallback.
@bot.message_handler(commands=["hoststart"])
def hoststart_cmd(message):
    if not is_channel_member(message.from_user.id): join_required(message.chat.id); return
    u=create_user(message.from_user)
    bot.send_message(message.chat.id,"🖥 <b>SHUVO X HOST</b>",parse_mode="HTML",reply_markup=main_keyboard(u["user_id"]))

def escape_markdown(text):
    """
    Escapes Telegram legacy Markdown special characters (*, _, `, [).
    Prevents 'can't parse entities' errors when usernames or names contain symbols.
    """
    if not text:
        return ""
    chars = ['*', '_', '`', '[']
    for ch in chars:
        text = text.replace(ch, f"\\{ch}")
    return text

# ----------------- STORAGE HELPERS -----------------

def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return {OWNER_ID}
    return {OWNER_ID}

def save_users(users_set):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users_set), f)

def load_config():
    default_config = {"required_channels": []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "required_channel_id" in data and data["required_channel_id"]:
                    data["required_channels"] = [{
                        "id": data["required_channel_id"],
                        "title": data.get("required_channel_title") or "Required Channel",
                        "username": data.get("required_channel_username"),
                        "invite_link": data.get("required_channel_invite_link")
                    }]
                if "required_channels" not in data:
                    data["required_channels"] = []
                return data
        except Exception:
            return default_config
    return default_config

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

allowed_users = load_users()
bot_config = load_config()

# ----------------- VERIFICATION HELPERS -----------------

def is_owner(user_id):
    return user_id == OWNER_ID

def is_allowed_user(user_id):
    return user_id in allowed_users or user_id == OWNER_ID

def check_channel_subscriptions(user_id):
    """
    Checks if a user is subscribed to ALL configured channels (up to 10).
    Returns: (is_all_joined: bool, missing_channels: list of dicts)
    """
    if is_owner(user_id):
        return True, []

    channels = bot_config.get("required_channels", [])
    if not channels:
        return True, []

    missing = []
    for ch in channels:
        ch_id = ch.get("id")
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                missing.append(ch)
        except Exception as e:
            print(f"[!] Error checking channel {ch_id}: {e}")
            missing.append(ch)

    return (len(missing) == 0), missing

def get_join_channels_keyboard(missing_channels):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, ch in enumerate(missing_channels, 1):
        link = ch.get("invite_link")
        title = ch.get("title", f"Channel {i}")
        if link:
            markup.add(types.InlineKeyboardButton(f"📢 Join {title}", url=link))
    markup.add(types.InlineKeyboardButton("🔄 Verify Membership", callback_data="verify_membership"))
    return markup

def get_uptime():
    uptime_seconds = int(time.time() - START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    return f"{days}d {hours}h {minutes}m {seconds}s"

# ----------------- UI / KEYBOARDS -----------------

def get_main_menu_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_status = types.InlineKeyboardButton("⏳ Status", callback_data="btn_status")
    btn_vitals = types.InlineKeyboardButton("🧬 Vitals", callback_data="btn_sysinfo")
    btn_ram = types.InlineKeyboardButton("🧠 RAM", callback_data="btn_memory")
    btn_disk = types.InlineKeyboardButton("💽 Disk", callback_data="btn_disk")
    btn_ps = types.InlineKeyboardButton("⚙️ Engines (PS)", callback_data="btn_ps")
    btn_myid = types.InlineKeyboardButton("🪪 My ID", callback_data="btn_myid")
    btn_help = types.InlineKeyboardButton("📖 Help Guide", callback_data="btn_help")

    markup.add(btn_status, btn_vitals)
    markup.add(btn_ram, btn_disk)
    markup.add(btn_ps, btn_myid)

    if is_owner(user_id):
        btn_channel = types.InlineKeyboardButton("📢 Channel Manager", callback_data="btn_channel_info")
        btn_users = types.InlineKeyboardButton("👥 Users List", callback_data="btn_list_users")
        markup.add(btn_channel, btn_users)

    markup.add(btn_help)
    return markup

def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_main_menu"))
    return markup

def get_help_text():
    channels = bot_config.get("required_channels", [])
    count = len(channels)
    return f"""⚡️ *DEV X HOST | NEON TERMINAL* ⚡️
*═════════════════════════*
Welcome to the core system. You have full terminal access. 🚀

💻 *TERMINAL ACTIONS:*
▪️ Direct commands (`ls`, `mkdir`, `git status`)
▪️ `cd <dir>` - Switch active directory 📂
▪️ `pip install <pkg>` - Install Python package 💉
▪️ `python <script.py>` - Execute scripts 🔥

🗂 *FILE OPERATIONS:*
▪️ Send document directly to bot - Uploads to active dir 📤
▪️ `/download <filename>` - Download file from server 📥

⚙️ *BACKGROUND ENGINES:*
▪️ `/run <cmd>` - Start background engine 🟢
▪️ `/stop <pid>` - Kill running engine 🛑
▪️ `/ps` - List all active engines 📊

🖥 *SYSTEM VITALS:*
▪️ `/status` - Live uptime & overview ⏳
▪️ `/sysinfo` - CPU & RAM utilization 🧬
▪️ `/disk` - Storage details 💽
▪️ `/memory` - RAM usage breakdown 🧠

🔑 *ACCESS & CONTROL:*
▪️ `/myid` - View your Telegram User ID 🪪
▪️ `/menu` or `/start` - Interactive Dashboard 🎛
▪️ `/channel` - Add channel via forward *(Max 10)* 📢
▪️ `/channels` - View & remove channels *(Owner only)* 📋
▪️ `/channel_del <id>` - Remove specific channel ❌
▪️ `/add <id>` & `/remove <id>` - Whitelist access 👥

🔒 *Active Channels:* `{count} / {MAX_CHANNELS}`
*═════════════════════════*
*SYSTEM READY >_* Type a command or use the buttons below:"""

# ----------------- OWNER CHANNEL MANAGEMENT -----------------

@bot.message_handler(commands=['channel'])
def set_channel_prompt(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Privilege escalation failed. Owner only.", parse_mode="Markdown")
        return
    
    current_count = len(bot_config.get("required_channels", []))
    if current_count >= MAX_CHANNELS:
        bot.reply_to(
            message,
            f"⚠️ *[LIMIT REACHED]* You already have `{MAX_CHANNELS}` channels added!\n"
            f"Use `/channels` or `/channel_del <id>` to remove one first.",
            parse_mode="Markdown"
        )
        return

    waiting_for_channel_forward.add(message.from_user.id)
    text = (
        f"📢 *[CHANNEL SETUP MODE]* ({current_count}/{MAX_CHANNELS} active)\n\n"
        f"1. Make sure you add this bot as an **Administrator** in your channel with invite link permissions.\n"
        f"2. **Forward any post/message from that channel to this chat right now.**\n\n"
        f"Send `/cancel` at any time to cancel."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("❌ Cancel Setup", callback_data="cancel_channel_setup"))
    bot.reply_to(message, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['channels'])
def list_channels_cmd(message):
    if not is_owner(message.from_user.id): return
    show_channel_manager(message.chat.id, None)

@bot.message_handler(commands=['channel_del'])
def delete_channel_by_arg(message):
    if not is_owner(message.from_user.id): return
    args = message.text.split(" ")
    if len(args) < 2:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse `/channel_del <channel_id>` or `/channels` to manage with buttons.", parse_mode="Markdown")
        return
    try:
        del_id = int(args[1])
        channels = bot_config.get("required_channels", [])
        before = len(channels)
        bot_config["required_channels"] = [c for c in channels if c.get("id") != del_id]
        if len(bot_config["required_channels"]) < before:
            save_config(bot_config)
            bot.reply_to(message, f"✅ Removed channel `{del_id}`.", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"❌ Channel ID `{del_id}` not found in list.", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "⚠️ Channel ID must be a numeric integer.")

def show_channel_manager(chat_id, message_id=None):
    channels = bot_config.get("required_channels", [])
    markup = types.InlineKeyboardMarkup(row_width=1)

    if not channels:
        text = "📢 *[CHANNEL MANAGER]*\n\nNo required channels configured yet (0/10).\nSend `/channel` to add one!"
    else:
        text = f"📢 *[CHANNEL MANAGER]* ({len(channels)}/{MAX_CHANNELS} Active)\n\n"
        for i, ch in enumerate(channels, 1):
            clean_title = escape_markdown(ch.get('title', 'Channel'))
            text += f"{i}. *{clean_title}*\n   ID: `{ch.get('id')}`\n   Link: {ch.get('invite_link') or 'None'}\n\n"
            markup.add(types.InlineKeyboardButton(f"🗑 Remove: {ch.get('title', 'Channel')[:25]}", callback_data=f"del_ch_{ch.get('id')}"))

    if len(channels) < MAX_CHANNELS:
        markup.add(types.InlineKeyboardButton("➕ Add New Channel", callback_data="add_new_channel_btn"))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_main_menu"))

    if message_id:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['cancel'])
def cancel_action(message):
    if message.from_user.id in waiting_for_channel_forward:
        waiting_for_channel_forward.remove(message.from_user.id)
        bot.reply_to(message, "❌ Setup cancelled.")

@bot.message_handler(commands=['add'])
def add_user(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "💀 *[ACCESS DENIED]* Owner only.", parse_mode="Markdown")
        return
    try:
        new_id = int(message.text.split(" ")[1])
        allowed_users.add(new_id)
        save_users(allowed_users)
        bot.reply_to(message, f"⚡️ *[ACCESS GRANTED]*\nUser `{new_id}` added to authorized list! 🚀", parse_mode="Markdown")
        try:
            bot.send_message(new_id, "🎉 *[ACCESS APPROVED]*\nThe Admin has approved your terminal access! Type /start to begin.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/add <userid>`", parse_mode="Markdown")

@bot.message_handler(commands=['remove'])
def remove_user(message):
    if not is_owner(message.from_user.id): return
    try:
        del_id = int(message.text.split(" ")[1])
        if del_id == OWNER_ID:
            bot.reply_to(message, "👑 *[SYSTEM ERROR]* Cannot disconnect the master owner! 🧠", parse_mode="Markdown")
            return
        if del_id in allowed_users:
            allowed_users.remove(del_id)
            save_users(allowed_users)
            bot.reply_to(message, f"🗑 *[USER REMOVED]*\nUser `{del_id}` removed from authorized list! 🔌", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ User ID not found in whitelist.")
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/remove <userid>`", parse_mode="Markdown")

# ----------------- CHANNEL FORWARD CAPTURE (UP TO 10) -----------------

@bot.message_handler(func=lambda msg: msg.from_user.id in waiting_for_channel_forward and msg.forward_from_chat is not None)
def handle_channel_forward(message):
    user_id = message.from_user.id
    chat = message.forward_from_chat
    waiting_for_channel_forward.discard(user_id)

    if chat.type != 'channel':
        bot.reply_to(message, "❌ The forwarded message must be from a **Channel**. Setup cancelled.", parse_mode="Markdown")
        return

    channels = bot_config.get("required_channels", [])
    if len(channels) >= MAX_CHANNELS:
        bot.reply_to(message, f"⚠️ Maximum limit of {MAX_CHANNELS} channels reached. Remove one first using `/channels`.", parse_mode="Markdown")
        return

    channel_id = chat.id
    channel_title = chat.title or "Required Channel"
    channel_username = chat.username

    if any(c.get("id") == channel_id for c in channels):
        bot.reply_to(message, f"⚠️ Channel *{escape_markdown(channel_title)}* (`{channel_id}`) is already in your required list!", parse_mode="Markdown")
        return

    invite_link = None
    try:
        chat_info = bot.get_chat(channel_id)
        if chat_info.invite_link:
            invite_link = chat_info.invite_link
        elif channel_username:
            invite_link = f"https://t.me/{channel_username}"
        else:
            link_obj = bot.create_chat_invite_link(channel_id)
            invite_link = link_obj.invite_link
    except Exception as e:
        if channel_username:
            invite_link = f"https://t.me/{channel_username}"
        print(f"[!] Could not create or fetch invite link: {e}")

    new_channel = {
        "id": channel_id,
        "title": channel_title,
        "username": channel_username,
        "invite_link": invite_link
    }
    channels.append(new_channel)
    bot_config["required_channels"] = channels
    save_config(bot_config)

    clean_title = escape_markdown(channel_title)
    success_text = (
        f"✅ *[CHANNEL #{len(channels)} ADDED SUCCESSFULLY]*\n\n"
        f"📌 *Title:* {clean_title}\n"
        f"🆔 *Channel ID:* `{channel_id}`\n"
        f"🔗 *Link:* {invite_link or 'No link generated'}\n\n"
        f"Total Active Channels: `{len(channels)}/{MAX_CHANNELS}`\n"
        f"Users must now join all active channels before accessing the bot!"
    )
    bot.reply_to(message, success_text, parse_mode="Markdown")

# ----------------- GENERAL & DASHBOARD COMMANDS -----------------

@bot.message_handler(commands=['start', 'help', 'menu', 'commands'])
def send_menu(message):
    user_id = message.from_user.id
    try:
        create_user(message.from_user)
        bot.send_message(message.chat.id, "🖥 <b>SHUVO X HOST</b> — Hosting menu", parse_mode="HTML", reply_markup=main_keyboard(user_id))
    except Exception:
        pass

    if is_owner(user_id):
        text = get_help_text()
        bot.reply_to(message, text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard(user_id))
        return

    # Check All Channel Memberships
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(
            message,
            f"⚠️ *[MEMBERSHIP REQUIRED]*\nYou must join all `{len(missing)}` pending channel(s) below to access this terminal.",
            parse_mode="Markdown",
            reply_markup=get_join_channels_keyboard(missing)
        )
        return

    # If all joined, check Whitelist Authorization
    if not is_allowed_user(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📩 Request Authorization from Admin", callback_data="send_auth_request"))
        bot.reply_to(
            message,
            "✅ *[CHANNELS VERIFIED]*\nYou are a member of all required channels.\n\n"
            "🔒 *[AUTHORIZATION REQUIRED]*\nYour account is not whitelisted by the Admin yet.\n"
            "Click below to send an authorization request to the Admin.",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return

    text = get_help_text()
    bot.reply_to(message, text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard(user_id))

@bot.message_handler(commands=['myid'])
def my_id(message):
    bot.reply_to(message, f"🪪 *YOUR TELEGRAM ID:* `{message.from_user.id}` ⚡️", parse_mode="Markdown")

# ----------------- SYSTEM INFO / VITALS -----------------

@bot.message_handler(commands=['status'])
def server_status(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    uptime = get_uptime()
    bot.reply_to(
        message,
        f"🟢 *SERVER STATUS:* `ONLINE`\n⏳ *RUNTIME:* `{uptime}`\n⚙️ *ACTIVE ENGINES:* `{len(bg_processes)}`\n⚡️ *CONNECTION:* `SECURE`",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(user_id)
    )

@bot.message_handler(commands=['sysinfo', 'disk', 'memory'])
def sys_info(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    if message.text == '/memory':
        mem = psutil.virtual_memory()
        bot.reply_to(message, f"🧠 *MEMORY CORE:*\n*Total:* `{mem.total / (1024**3):.2f} GB`\n*Used:* `{mem.used / (1024**3):.2f} GB` ({mem.percent}%) ⚡️", parse_mode="Markdown")
    elif message.text == '/disk':
        disk = psutil.disk_usage('/')
        bot.reply_to(message, f"💽 *STORAGE VAULT:*\n*Total:* `{disk.total / (1024**3):.2f} GB`\n*Used:* `{disk.used / (1024**3):.2f} GB` ({disk.percent}%) 📂", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"🧬 *SYSTEM VITALS:*\n*CPU Usage:* `{psutil.cpu_percent()}%` 🔥\n*RAM Usage:* `{psutil.virtual_memory().percent}%` 🧠", parse_mode="Markdown")

# ----------------- BACKGROUND ENGINES (RUN, PS, STOP) -----------------

@bot.message_handler(commands=['run'])
def run_bg(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    global current_dir
    cmd = message.text.replace('/run', '', 1).strip()
    if not cmd:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/run <command>` (e.g. `/run python app.py`)", parse_mode="Markdown")
        return

    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=current_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        bg_processes[proc.pid] = {'process': proc, 'cmd': cmd}
        bot.reply_to(message, f"🟢 *[ENGINE STARTED]*\n*PID:* `{proc.pid}` ⚙️\n*CMD:* `{cmd}`\nRunning in background... 🥷", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ *[CRASH]* Engine failed: {e}")

@bot.message_handler(commands=['ps'])
def list_ps(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    if not bg_processes:
        bot.reply_to(message, "💤 *[SYSTEM IDLE]* No background engines running.")
        return

    res = "📊 *[ACTIVE ENGINES]*\n*════════════════*\n"
    active_count = 0
    markup = types.InlineKeyboardMarkup()

    for pid, pinfo in list(bg_processes.items()):
        if pinfo['process'].poll() is None:
            active_count += 1
            res += f"⚙️ *PID:* `{pid}` | *CMD:* `{pinfo['cmd']}`\n"
            markup.add(types.InlineKeyboardButton(f"🛑 Kill PID {pid}", callback_data=f"kill_{pid}"))
        else:
            del bg_processes[pid]

    if active_count == 0:
        bot.reply_to(message, "💤 *[SYSTEM IDLE]* No background engines running.")
    else:
        bot.reply_to(message, res, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['stop'])
def stop_ps(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    try:
        pid = int(message.text.split(" ")[1])
        if pid in bg_processes:
            bg_processes[pid]['process'].terminate()
            del bg_processes[pid]
            bot.reply_to(message, f"🛑 *[ENGINE KILLED]* PID `{pid}` terminated successfully! 💀", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ *[NOT FOUND]* Target PID does not exist in active processes.")
    except Exception:
        bot.reply_to(message, "⚠️ *Format Error:*\nUse: `/stop <pid>`", parse_mode="Markdown")

# ----------------- FILE OPERATIONS -----------------

@bot.message_handler(commands=['download'])
def download_file(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    global current_dir
    filename = message.text.replace('/download', '', 1).strip()
    if not filename:
        bot.reply_to(message, "⚠️ *Usage:* `/download <filename>`", parse_mode="Markdown")
        return

    filepath = os.path.join(current_dir, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        bot.reply_to(message, "📥 *[EXTRACTING]* Transmitting file... ⏳", parse_mode="Markdown")
        try:
            with open(filepath, 'rb') as f:
                bot.send_document(message.chat.id, f)
        except Exception as e:
            bot.reply_to(message, f"❌ Failed to send file: {e}")
    else:
        bot.reply_to(message, "❌ *[404]* File not found in current directory! 🔍")

# Hosting/legacy document upload is handled by unified_document_handler above.

# ----------------- DIRECT TERMINAL COMMANDS -----------------

@bot.message_handler(func=lambda message: not message.text.startswith('/'))
def direct_terminal(message):
    user_id = message.from_user.id
    if not is_allowed_user(user_id):
        bot.reply_to(message, "⛔️ *[UNAUTHORIZED]* Access restricted.", parse_mode="Markdown")
        return
    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.reply_to(message, "⚠️ *Access Denied:* Please join all channels.", reply_markup=get_join_channels_keyboard(missing), parse_mode="Markdown")
        return

    global current_dir
    cmd = message.text.strip()

    if cmd.startswith("cd "):
        new_dir = cmd[3:].strip()
        target_path = os.path.abspath(os.path.join(current_dir, new_dir))
        if os.path.exists(target_path) and os.path.isdir(target_path):
            current_dir = target_path
            bot.reply_to(message, f"📂 *[DIR CHANGED]*\nNow in:\n`{current_dir}` ⚡️", parse_mode="Markdown")
        else:
            bot.reply_to(message, "❌ *[404]* Directory does not exist!")
        return

    try:
        bot.send_chat_action(message.chat.id, 'typing')
        result = subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.STDOUT, cwd=current_dir)

        if not result.strip():
            bot.reply_to(message, "✅ *[EXECUTED]* Command succeeded with empty output. 🥷", parse_mode="Markdown")
        else:
            if len(result) > 4000:
                out_path = "output.txt"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(result)
                with open(out_path, "rb") as f:
                    bot.send_document(message.chat.id, f, caption="⚠️ *[OUTPUT TRUNCATED]* Output exceeds limits. Transmitted as file.", parse_mode="Markdown")
            else:
                bot.reply_to(message, f"```\n{result}\n```", parse_mode="Markdown")
    except subprocess.CalledProcessError as e:
        bot.reply_to(message, f"🛑 *[COMMAND ERROR]*:\n```\n{e.output}\n```", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ *[SYSTEM ERROR]*: {e}")

# ----------------- INLINE CALLBACK HANDLERS -----------------

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    if (call.data == "admin" or call.data.startswith(("adm:", "approve:", "reject:", "buy:", "status:", "startapp:", "stopapp:", "deleteapp:", "files", "points:"))):
        host_callback(call)
        return

    # 1. Verification of Channel Subscriptions
    if call.data == "verify_membership":
        is_all_joined, missing = check_channel_subscriptions(user_id)
        if is_all_joined:
            if is_allowed_user(user_id):
                bot.answer_callback_query(call.id, "✅ Verified! Welcome back.")
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=get_help_text(),
                    parse_mode="Markdown",
                    reply_markup=get_main_menu_keyboard(user_id)
                )
            else:
                bot.answer_callback_query(call.id, "✅ All channels verified!")
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📩 Request Authorization from Admin", callback_data="send_auth_request"))
                channels_count = len(bot_config.get("required_channels", []))
                text = (
                    f"🎉 *[CHANNELS VERIFICATION SUCCESS]*\n\n"
                    f"You have successfully joined all `{channels_count}` official channel(s)! 🚀\n\n"
                    f"⚠️ *Next Step:* Terminal access requires Administrator approval.\n"
                    f"Click below to send an authorization request to the Admin."
                )
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
        else:
            bot.answer_callback_query(call.id, f"❌ You still have {len(missing)} channel(s) left to join!", show_alert=True)
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=get_join_channels_keyboard(missing)
            )
        return

    # 2. User Sends Auth Request to Admin (FIXED: Escaped characters & fallback)
    if call.data == "send_auth_request":
        is_all_joined, missing = check_channel_subscriptions(user_id)
        if not is_all_joined:
            bot.answer_callback_query(call.id, "❌ You must join all channels first!", show_alert=True)
            return

        if is_allowed_user(user_id):
            bot.answer_callback_query(call.id, "✅ You are already authorized!")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=get_help_text(),
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return

        # Sanitize names to prevent Markdown parse error (byte offset issue)
        raw_full_name = f"{call.from_user.first_name} {call.from_user.last_name or ''}".strip()
        safe_full_name = escape_markdown(raw_full_name)
        
        if call.from_user.username:
            safe_username = "@" + escape_markdown(call.from_user.username)
        else:
            safe_username = "None"

        admin_markup = types.InlineKeyboardMarkup(row_width=2)
        btn_allow = types.InlineKeyboardButton("✅ Allow Access", callback_data=f"auth_allow_{user_id}")
        btn_deny = types.InlineKeyboardButton("❌ Deny Access", callback_data=f"auth_deny_{user_id}")
        admin_markup.add(btn_allow, btn_deny)

        channels_count = len(bot_config.get("required_channels", []))
        admin_text = (
            f"🔔 *[NEW ACCESS REQUEST]*\n\n"
            f"👤 *Name:* {safe_full_name}\n"
            f"🔗 *Username:* {safe_username}\n"
            f"🆔 *User ID:* `{user_id}`\n"
            f"📢 *Channels Status:* Verified Member of all `{channels_count}` channels ✅\n\n"
            f"Would you like to grant terminal access to this user?"
        )

        try:
            bot.send_message(OWNER_ID, admin_text, parse_mode="Markdown", reply_markup=admin_markup)
            pending_requests.add(user_id)
            bot.answer_callback_query(call.id, "📨 Request sent to Admin!")
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="⏳ *[REQUEST TRANSMITTED]*\n\nYour authorization request has been sent to the System Admin.\nYou will receive a notification as soon as it is approved! 🚀",
                parse_mode="Markdown"
            )
        except Exception as e:
            # Fallback to plain text if Markdown still encounters formatting conflicts
            try:
                plain_text = (
                    f"🔔 [NEW ACCESS REQUEST]\n\n"
                    f"Name: {raw_full_name}\n"
                    f"Username: @{call.from_user.username if call.from_user.username else 'None'}\n"
                    f"User ID: {user_id}\n"
                    f"Channels Status: Verified Member of all {channels_count} channels\n\n"
                    f"Would you like to grant terminal access to this user?"
                )
                bot.send_message(OWNER_ID, plain_text, reply_markup=admin_markup)
                pending_requests.add(user_id)
                bot.answer_callback_query(call.id, "📨 Request sent to Admin!")
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text="⏳ *[REQUEST TRANSMITTED]*\n\nYour authorization request has been sent to the System Admin.\nYou will receive a notification as soon as it is approved! 🚀",
                    parse_mode="Markdown"
                )
            except Exception as e2:
                bot.answer_callback_query(call.id, f"Error reaching Admin: {e2}", show_alert=True)
        return

    # 3. Admin Decision: ALLOW
    if call.data.startswith("auth_allow_"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only.", show_alert=True)
            return
        target_uid = int(call.data.replace("auth_allow_", ""))
        allowed_users.add(target_uid)
        save_users(allowed_users)
        pending_requests.discard(target_uid)

        bot.answer_callback_query(call.id, f"User {target_uid} approved!")
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=call.message.text + f"\n\n🟢 DECISION: Allowed by Admin on {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception:
            pass

        try:
            user_markup = types.InlineKeyboardMarkup()
            user_markup.add(types.InlineKeyboardButton("🚀 Launch Terminal Dashboard", callback_data="btn_main_menu"))
            bot.send_message(
                target_uid,
                "🎉 *[ACCESS APPROVED]*\n\nThe System Admin has approved your request! You now have full terminal access.",
                parse_mode="Markdown",
                reply_markup=user_markup
            )
        except Exception as e:
            print(f"[!] Notification to user {target_uid} failed: {e}")
        return

    # 4. Admin Decision: DENY
    if call.data.startswith("auth_deny_"):
        if not is_owner(user_id):
            bot.answer_callback_query(call.id, "⛔ Owner only.", show_alert=True)
            return
        target_uid = int(call.data.replace("auth_deny_", ""))
        pending_requests.discard(target_uid)

        bot.answer_callback_query(call.id, f"User {target_uid} denied.")
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=call.message.text + f"\n\n🔴 DECISION: Denied by Admin on {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        except Exception:
            pass

        try:
            bot.send_message(target_uid, "🚫 *[ACCESS DENIED]*\nThe System Admin has rejected your authorization request.", parse_mode="Markdown")
        except Exception:
            pass
        return

    # Channel Manager Actions
    if call.data == "btn_channel_info":
        if not is_owner(user_id): return
        show_channel_manager(call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
        return

    if call.data == "add_new_channel_btn":
        if not is_owner(user_id): return
        current_count = len(bot_config.get("required_channels", []))
        if current_count >= MAX_CHANNELS:
            bot.answer_callback_query(call.id, f"Limit reached ({MAX_CHANNELS} channels maximum).", show_alert=True)
            return
        waiting_for_channel_forward.add(user_id)
        text = (
            f"📢 *[ADD CHANNEL]* ({current_count}/{MAX_CHANNELS})\n\n"
            f"Make sure bot is admin in the channel.\n"
            f"Now **forward any message from that channel** here.\n\n"
            f"Send `/cancel` to cancel."
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_channel_setup"))
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, parse_mode="Markdown", reply_markup=markup)
        bot.answer_callback_query(call.id)
        return

    if call.data.startswith("del_ch_"):
        if not is_owner(user_id): return
        target_ch_id = int(call.data.replace("del_ch_", ""))
        channels = bot_config.get("required_channels", [])
        bot_config["required_channels"] = [c for c in channels if c.get("id") != target_ch_id]
        save_config(bot_config)
        bot.answer_callback_query(call.id, "Channel removed!")
        show_channel_manager(call.message.chat.id, call.message.message_id)
        return

    # Check authorization for other controls
    if not is_allowed_user(user_id):
        bot.answer_callback_query(call.id, "⛔ Access denied. Unauthorized.", show_alert=True)
        return

    is_all_joined, missing = check_channel_subscriptions(user_id)
    if not is_all_joined:
        bot.answer_callback_query(call.id, "⚠️ Channel membership required!", show_alert=True)
        return

    # Kill Process Callback
    if call.data.startswith("kill_"):
        pid = int(call.data.replace("kill_", ""))
        if pid in bg_processes:
            try:
                bg_processes[pid]['process'].terminate()
                del bg_processes[pid]
                bot.answer_callback_query(call.id, f"Engine {pid} killed!")
                bot.send_message(call.message.chat.id, f"🛑 *[ENGINE KILLED]* PID `{pid}` successfully terminated.", parse_mode="Markdown")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "PID already stopped or not found.")
        return

    # Dashboard Actions
    if call.data == "btn_main_menu":
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=get_help_text(),
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_status":
        uptime = get_uptime()
        text = (
            f"🟢 *SERVER STATUS:* `ONLINE`\n"
            f"⏳ *RUNTIME:* `{uptime}`\n"
            f"⚙️ *ACTIVE ENGINES:* `{len(bg_processes)}`\n"
            f"⚡️ *CONNECTION:* `SECURE`"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_sysinfo":
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        text = f"🧬 *SYSTEM VITALS:*\n*CPU Usage:* `{cpu}%` 🔥\n*RAM Usage:* `{ram}%` 🧠"
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_memory":
        mem = psutil.virtual_memory()
        text = (
            f"🧠 *MEMORY CORE:*\n"
            f"*Total:* `{mem.total / (1024**3):.2f} GB`\n"
            f"*Used:* `{mem.used / (1024**3):.2f} GB` ({mem.percent}%) ⚡️\n"
            f"*Available:* `{mem.available / (1024**3):.2f} GB`"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_disk":
        disk = psutil.disk_usage('/')
        text = (
            f"💽 *STORAGE VAULT:*\n"
            f"*Total:* `{disk.total / (1024**3):.2f} GB`\n"
            f"*Used:* `{disk.used / (1024**3):.2f} GB` ({disk.percent}%) 📂\n"
            f"*Free:* `{disk.free / (1024**3):.2f} GB`"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_ps":
        if not bg_processes:
            text = "💤 *[SYSTEM IDLE]* No background engines currently active."
            markup = get_back_keyboard()
        else:
            text = "📊 *[ACTIVE ENGINES]*\n*════════════════*\n"
            markup = types.InlineKeyboardMarkup()
            for pid, pinfo in list(bg_processes.items()):
                if pinfo['process'].poll() is None:
                    text += f"⚙️ *PID:* `{pid}` | *CMD:* `{pinfo['cmd']}`\n"
                    markup.add(types.InlineKeyboardButton(f"🛑 Kill PID {pid}", callback_data=f"kill_{pid}"))
                else:
                    del bg_processes[pid]
            markup.add(types.InlineKeyboardButton("🔙 Back to Main Menu", callback_data="btn_main_menu"))
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_myid":
        text = f"🪪 *YOUR TELEGRAM ID:* `{call.from_user.id}` ⚡️"
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_help":
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=get_help_text(),
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        bot.answer_callback_query(call.id)

    elif call.data == "btn_list_users":
        if not is_owner(user_id): return
        users_list_str = "\n".join([f"▪️ `{uid}`" for uid in allowed_users])
        text = f"👥 *[WHITELISTED USERS]*\n\n{users_list_str}\n\n• Use `/add <id>` or `/remove <id>`."
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )
        bot.answer_callback_query(call.id)

    elif call.data == "cancel_channel_setup":
        waiting_for_channel_forward.discard(user_id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Channel setup was cancelled."
        )
        bot.answer_callback_query(call.id)


# =========================
# STARTUP
# =========================
if __name__ == "__main__":
    init_db()
    sync_plans()
    try:
        threading.Thread(target=cleanup_expired_users, daemon=True).start()
        threading.Thread(target=process_watchdog, daemon=True).start()
    except Exception as e:
        print("Background worker start error:", e)
    print("⚡️ SHUVO X HOST merged bot is online")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message","callback_query"])
