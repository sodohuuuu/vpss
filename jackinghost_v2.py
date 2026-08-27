# bot.py — v2 | full featured
import os
import sys
import ast
import json
import time
import shlex
import shutil
import zipfile
import logging
import asyncio
import threading
import subprocess
import re
from collections import defaultdict, deque
from datetime import datetime

from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
TOKEN        = "8742801995:AAGAHwPiI0nK7QQY5P_Wg3V3jhzS_Khjc8Q"
OWNER_ID     = 8502412097
PASSWORD     = "ジェイ"
DOWNLOADS_DIR = "downloads"
LOGS_DIR      = "logs"
SHARED_DIR    = "shared_scripts"   # admin uploads go here, visible to all users

MAX_SCRIPT_RUNTIME = 3600          # seconds — auto-kill after 1 hour
RATE_LIMIT_CMDS    = 8             # max commands per window
RATE_LIMIT_WINDOW  = 10            # seconds

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SHARED_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA_FILE  = "bot_data.json"
STATS_FILE = "usage_stats.json"

# ═══════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHABCDJK]|\x1b\([A-Z]|\x1b=|\r')

def clean_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)

def safe_md(text: str) -> str:
    """Escape markdown special chars for display."""
    return str(text).translate(str.maketrans('', '', '*_`['))

PIP_MAP = {
    "telegram":  "python-telegram-bot",
    "PIL":       "pillow",
    "cv2":       "opencv-python",
    "bs4":       "beautifulsoup4",
    "sklearn":   "scikit-learn",
    "yaml":      "pyyaml",
    "discord":   "discord.py",
    "google":    "google-api-python-client",
    "dotenv":    "python-dotenv",
    "requests":  "requests",
    "aiohttp":   "aiohttp",
    "flask":     "flask",
    "fastapi":   "fastapi",
}

STDLIB_EXTRAS = {
    'os','sys','time','json','re','asyncio','logging','threading',
    'subprocess','datetime','pathlib','collections','itertools',
    'functools','math','random','string','io','typing','abc','copy',
    'hashlib','base64','struct','socket','ssl','http','urllib','email',
    'html','xml','csv','sqlite3','argparse','configparser','tempfile',
    'shutil','glob','fnmatch','traceback','warnings','contextlib','enum',
    'numbers','decimal','fractions','statistics','pprint','textwrap',
    'difflib','zipfile','tarfile','gzip','bz2','lzma','zlib','pickle',
    'shelve','dbm','sched','queue','multiprocessing','concurrent',
    'socketserver','ipaddress','uuid','secrets','hmac','dataclasses',
    'operator','ctypes','array','weakref','types','inspect','dis','code',
    'ast','token','tokenize','plistlib','tty','termios','pty','fcntl',
    'grp','pwd','crypt','syslog','os.path','builtins'
}

# ═══════════════════════════════════════════════════
# DATA
# ═══════════════════════════════════════════════════
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                d = json.load(f)
                d.setdefault("approved_users", [])
                d.setdefault("banned_users", [])
                d.setdefault("pending_users", {})
                d.setdefault("user_info", {})
                d.setdefault("pending_files", {})   # uid -> list of pending file paths
                return d
        except Exception as e:
            logger.error(f"load_data: {e}")
    return {
        "approved_users": [], "banned_users": [],
        "pending_users": {}, "user_info": {}, "pending_files": {}
    }

def save_data(data):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save_data: {e}")

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_stats(stats):
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        logger.error(f"save_stats: {e}")

def record_stat(uid_str: str, key: str, val=1):
    stats = load_stats()
    u = stats.setdefault(uid_str, {"scripts_run": 0, "errors": 0, "total_runtime": 0, "commands": 0})
    u[key] = u.get(key, 0) + val
    save_stats(stats)

bot_data       = load_data()
active_processes: dict[str, list] = {}
terminal_active: dict[int, dict]  = {}
waiting_for_input: dict[str, dict]= {}
process_stdin_waiting: dict[str, dict] = {}
stdin_input_queues: dict[str, deque]   = defaultdict(deque)

# rate limiter — per user command timestamps
_rate_buckets: dict[str, list] = defaultdict(list)

# ═══════════════════════════════════════════════════
# RATE LIMITER
# ═══════════════════════════════════════════════════
def check_rate_limit(uid_str: str) -> bool:
    """Returns True if allowed, False if rate limited."""
    now = time.time()
    bucket = _rate_buckets[uid_str]
    # drop old timestamps outside window
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW:
        bucket.pop(0)
    if len(bucket) >= RATE_LIMIT_CMDS:
        return False
    bucket.append(now)
    return True

# ═══════════════════════════════════════════════════
# PIP
# ═══════════════════════════════════════════════════
def find_pip():
    for cmd in [["pip3"],["pip"],["python3","-m","pip"],[sys.executable,"-m","pip"]]:
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return cmd
        except Exception:
            pass
    return ["pip3"]

PIP_CMD = find_pip()

def pip_install(pkg: str = "", extra_args=None):
    cmd = PIP_CMD + ["install","--break-system-packages","--user"] + (extra_args or [])
    if pkg:
        cmd.append(pkg)
    return subprocess.run(cmd, capture_output=True, text=True)

# ═══════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════
def is_owner(uid): return uid == OWNER_ID
def is_auth(uid):  return is_owner(uid) or uid in bot_data.get("approved_users", [])
def is_banned(uid):return uid in bot_data.get("banned_users", [])

# ═══════════════════════════════════════════════════
# PROCESS CLEANUP
# ═══════════════════════════════════════════════════
def cleanup_dead_processes(uid_str: str):
    procs = active_processes.get(uid_str, [])
    active_processes[uid_str] = [p for p in procs if p["proc"].poll() is None]

def cleanup_all_dead():
    for uid_str in list(active_processes.keys()):
        cleanup_dead_processes(uid_str)

# ═══════════════════════════════════════════════════
# INPUT DETECTION — AST based
# ═══════════════════════════════════════════════════
def has_input_function(filepath: str) -> bool:
    try:
        with open(filepath, 'r', errors='ignore') as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == 'input':
                    return True
        return False
    except SyntaxError:
        try:
            with open(filepath, 'r', errors='ignore') as f:
                return 'input(' in f.read()
        except Exception:
            return False
    except Exception:
        return False

# ═══════════════════════════════════════════════════
# DEPENDENCY EXTRACTION
# ═══════════════════════════════════════════════════
def extract_external_deps(source: str) -> list[str]:
    imports = re.findall(r'^(?:from|import)\s+([a-zA-Z0-9_]+)', source, re.MULTILINE)
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else set()
    stdlib |= STDLIB_EXTRAS
    external = [i for i in set(imports) if i not in stdlib]
    return [PIP_MAP.get(dep, dep) for dep in external]

# ═══════════════════════════════════════════════════
# SHARED SCRIPTS (admin uploads → all users can run)
# ═══════════════════════════════════════════════════
def list_shared_scripts() -> list[str]:
    try:
        return [f for f in os.listdir(SHARED_DIR)
                if os.path.isfile(os.path.join(SHARED_DIR, f))]
    except Exception:
        return []

def shared_scripts_kb():
    files = list_shared_scripts()
    if not files:
        return None
    buttons = []
    for fname in files:
        buttons.append([InlineKeyboardButton(f"▶️ {fname}", callback_data=f"run_shared_{fname}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def admin_shared_kb():
    files = list_shared_scripts()
    buttons = []
    for fname in files:
        buttons.append([
            InlineKeyboardButton(f"🗑 {fname}", callback_data=f"del_shared_{fname}")
        ])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="adm_back")])
    return InlineKeyboardMarkup(buttons)

# ═══════════════════════════════════════════════════
# TERMINAL
# ═══════════════════════════════════════════════════
def get_session(uid: int) -> dict:
    if uid not in terminal_active:
        folder = os.path.join(DOWNLOADS_DIR, str(uid))
        os.makedirs(folder, exist_ok=True)
        terminal_active[uid] = {"cwd": os.path.abspath(folder), "active": False}
    return terminal_active[uid]

def term_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clear", callback_data="t_clear"),
         InlineKeyboardButton("📁 PWD",   callback_data="t_pwd")],
        [InlineKeyboardButton("📋 LS",    callback_data="t_ls"),
         InlineKeyboardButton("❌ Exit",  callback_data="t_exit")]
    ])

def find_file_in_dir(cwd: str, name: str) -> str | None:
    for ext in ['.py','.js','.txt','.json','.sh']:
        full = os.path.join(cwd, name + ext)
        if os.path.isfile(full):
            return name + ext
    try:
        for f in os.listdir(cwd):
            if f.startswith(name + '.') and os.path.isfile(os.path.join(cwd, f)):
                return f
    except Exception:
        pass
    return None

BUILTINS = {
    'ls','cd','pwd','mkdir','rmdir','rm','cp','mv','cat','head','tail',
    'grep','find','chmod','touch','echo','clear','exit','quit',
    'whoami','id','uname','date','uptime','free','df','du','ps','kill',
    'wget','curl','tar','zip','unzip','which','ping','ifconfig','ip',
    'apt','apt-get','pip','pip3','npm','npx','node','python','python3',
    'git','nano','vim','vi','wc','sort','bash','sh','sudo'
}

def is_builtin_cmd(cmd: str) -> bool:
    return cmd.lower() in BUILTINS

def fix_cmd(cmd: str, cwd: str) -> str:
    c = cmd.strip()
    if not c: return c
    try:
        parts = shlex.split(c)
    except ValueError:
        parts = c.split()

    first = parts[0]

    if first.lower() in ('python','python3'):
        if len(parts) > 1 and not parts[1].startswith('-'):
            found = find_file_in_dir(cwd, parts[1])
            if found: parts[1] = found
        return f"{sys.executable} {shlex.join(parts[1:])}"

    if first.lower() in ('pip','pip3'):
        rest = shlex.join(parts[1:])
        if 'install' in parts and '--break-system-packages' not in rest:
            return f"{sys.executable} -m pip {rest} --break-system-packages"
        return f"{sys.executable} -m pip {rest}"

    if first.lower() == 'node':
        if len(parts) > 1 and not parts[1].startswith('-'):
            found = find_file_in_dir(cwd, parts[1])
            if found and found.endswith('.js'): parts[1] = found
        return c

    if not is_builtin_cmd(first) and '/' not in first and '.' not in first:
        found = find_file_in_dir(cwd, first)
        if found:
            full = os.path.join(cwd, found)
            if found.endswith('.py'):   return f"{sys.executable} {shlex.quote(full)}"
            elif found.endswith('.js'): return f"node {shlex.quote(full)}"
            elif found.endswith('.sh'): return f"bash {shlex.quote(full)}"
    return c

async def run_term_cmd(uid: int, cmd: str, user_input_val: str | None = None):
    session = get_session(uid)
    cwd = session["cwd"]
    fixed = fix_cmd(cmd, cwd)
    stripped = cmd.strip()

    if stripped.startswith("cd ") or stripped == "cd":
        parts = stripped.split(None, 1)
        target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
        if target == "..":        new = os.path.dirname(cwd)
        elif target == "~":       new = os.path.expanduser("~")
        elif os.path.isabs(target): new = target
        else:                     new = os.path.join(cwd, target)
        new = os.path.normpath(new)
        if os.path.isdir(new):
            session["cwd"] = new
            return f"📁 `{new}`", new
        return f"❌ Not found: `{target}`", cwd

    if stripped.lower() in ("exit","quit"):
        session["active"] = False
        return "👋 Terminal closed.", cwd
    if stripped.lower() == "clear": return "🧹 Cleared.", cwd
    if stripped.lower() == "pwd":   return f"📁 `{cwd}`", cwd

    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        final_cmd = (f"echo {shlex.quote(user_input_val)} | {fixed}"
                     if user_input_val is not None else fixed)

        proc = subprocess.run(
            final_cmd, shell=True, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=120
        )
        out = ""
        if proc.stdout: out += proc.stdout
        if proc.stderr: out += ("\n" if out else "") + proc.stderr
        if not out:     out = "(no output)"
        if len(out) > 4000: out = "...\n" + out[-4000:]

        record_stat(str(uid), "commands")
        return out, cwd

    except subprocess.TimeoutExpired:
        return "⏱️ Timeout (120s)", cwd
    except Exception as e:
        return f"❌ {e}", cwd

# ═══════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════
def stop_kb(uid: int):
    cleanup_dead_processes(str(uid))
    procs = active_processes.get(str(uid), [])
    buttons = []
    for i, p in enumerate(procs):
        st = "🟢" if p["proc"].poll() is None else "🔴"
        buttons.append([InlineKeyboardButton(
            f"🛑 {st} {p['name']}", callback_data=f"stop_{i}"
        )])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def logs_kb(uid: int):
    procs = active_processes.get(str(uid), [])
    buttons = [
        [InlineKeyboardButton(f"📝 {p['name']}", callback_data=f"logs_{i}"),
         InlineKeyboardButton(f"⬇️ DL",          callback_data=f"dllog_{i}")]
        for i, p in enumerate(procs)
    ]
    buttons.append([InlineKeyboardButton("🔙", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def main_kb(uid=None):
    rows = [
        [KeyboardButton("💻 Terminal"),      KeyboardButton("📁 Upload File")],
        [KeyboardButton("🛑 Stop Script"),   KeyboardButton("📂 My Scripts")],
        [KeyboardButton("📝 View Logs"),     KeyboardButton("🌐 Shared Scripts")],
        [KeyboardButton("📊 My Stats")]
    ]
    if uid and is_owner(uid):
        rows.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users",         callback_data="adm_users"),
         InlineKeyboardButton("⏳ Pending",        callback_data="adm_pending")],
        [InlineKeyboardButton("🖥️ All Scripts",   callback_data="adm_scripts"),
         InlineKeyboardButton("📊 Stats",          callback_data="adm_stats")],
        [InlineKeyboardButton("🚫 Banned",         callback_data="adm_banned"),
         InlineKeyboardButton("📢 Broadcast",      callback_data="adm_broadcast")],
        [InlineKeyboardButton("📂 Shared Scripts", callback_data="adm_shared"),
         InlineKeyboardButton("⏳ File Approvals", callback_data="adm_file_approvals")],
        [InlineKeyboardButton("🔙 Close",          callback_data="adm_close")]
    ])

def approval_kb(target_uid: int):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_uid}"),
        InlineKeyboardButton("🚫 Ban",    callback_data=f"ban_{target_uid}")
    ]])

def file_approval_kb(uid: int, fname: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Allow Run",   callback_data=f"fapprove_{uid}_{fname}"),
        InlineKeyboardButton("🚫 Reject",     callback_data=f"freject_{uid}_{fname}"),
        InlineKeyboardButton("📂 Add Shared", callback_data=f"fshared_{uid}_{fname}")
    ]])

# ═══════════════════════════════════════════════════
# FLASK
# ═══════════════════════════════════════════════════
flask_app = Flask('')

@flask_app.route('/')
def index(): return "<h1>Bot Online</h1>"

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False)

# ═══════════════════════════════════════════════════
# BROADCAST STATE
# ═══════════════════════════════════════════════════
_broadcast_waiting: set[int] = set()   # owner uids waiting to type broadcast msg

# ═══════════════════════════════════════════════════
# ACCESS
# ═══════════════════════════════════════════════════
async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user
    uid_str = str(uid)

    if uid_str in bot_data.get("pending_users", {}):
        await update.message.reply_text(
            "⏳ *Request already bheja hai.*\nAdmin approve kare tabtak ruko.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    bot_data.setdefault("pending_users", {})[uid_str] = {
        "uid": uid, "name": user.full_name,
        "username": f"@{user.username}" if user.username else "N/A",
        "time": datetime.now().strftime("%d/%m %H:%M")
    }
    save_data(bot_data)

    await update.message.reply_text(
        "📨 *Access Request bhej diya!*\nAdmin approve kare tabtak wait karo ⏳",
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(f"🔔 *Naya Access Request!*\n\n"
                  f"👤 {user.full_name}\n🆔 `{uid}`\n"
                  f"📛 @{user.username or 'N/A'}\n"
                  f"🕐 {datetime.now().strftime('%d/%m %H:%M')}"),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=approval_kb(uid)
        )
    except Exception as e:
        logger.warning(f"admin notify failed: {e}")

# ═══════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user

    if is_banned(uid):
        await update.message.reply_text("🚫 Banned.")
        return
    if not is_auth(uid):
        await request_access(update, context)
        return

    bot_data.setdefault("user_info", {})[str(uid)] = {
        "name": user.full_name,
        "username": f"@{user.username}" if user.username else "N/A",
        "join_time": datetime.now().strftime("%d/%m %H:%M")
    }
    bot_data.get("pending_users", {}).pop(str(uid), None)
    save_data(bot_data)

    await update.message.reply_text(
        f"👋 *Welcome {user.first_name}!*\n\n"
        f"⚡ Script me `input()` hoga toh bot khud maang lega!\n"
        f"🌐 Admin ke shared scripts bhi run kar sakte ho\n\n"
        f"👇 Tap below",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_kb(uid)
    )

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("🚫 Sirf admin.")
        return
    cleanup_all_dead()
    pending  = len(bot_data.get("pending_users", {}))
    approved = len(bot_data.get("approved_users", []))
    running  = sum(1 for procs in active_processes.values()
                   for p in procs if p["proc"].poll() is None)
    await update.message.reply_text(
        f"👑 *Admin Panel*\n\n✅ {approved} | ⏳ {pending}\n🖥️ Running: {running}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_kb()
    )

# ═══════════════════════════════════════════════════
# CALLBACKS
# ═══════════════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    uid_str = str(uid)
    data    = query.data

    # admin-only callbacks
    ADMIN_PREFIXES = (
        "approve_","ban_","unban_","kill_","fapprove_","freject_","fshared_",
        "del_shared_",
        "adm_users","adm_pending","adm_scripts","adm_stats",
        "adm_banned","adm_back","adm_close","adm_broadcast",
        "adm_shared","adm_file_approvals"
    )
    if any(data.startswith(p) for p in ADMIN_PREFIXES):
        if not is_owner(uid):
            await query.answer("🚫 Sirf admin!", show_alert=True)
            return
        await _admin_callback(query, context, uid, data)
        return

    if not is_auth(uid):
        await query.answer("🔐 Access nahi!", show_alert=True)
        return

    # shared script run
    if data.startswith("run_shared_"):
        fname = data[len("run_shared_"):]
        fpath = os.path.join(SHARED_DIR, fname)
        if not os.path.exists(fpath):
            await query.edit_message_text("❌ Script nahi mila.")
            return
        # copy to user dir and run
        user_dir = os.path.join(DOWNLOADS_DIR, uid_str)
        os.makedirs(user_dir, exist_ok=True)
        dest = os.path.join(user_dir, fname)
        shutil.copy2(fpath, dest)
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"🚀 Shared script `{fname}` run ho rahi hai...",
            parse_mode=ParseMode.MARKDOWN
        )
        await _launch_script(uid, uid_str, fname, dest, user_dir,
                             context, query.message.chat_id, msg)
        return

    # terminal
    if data == "t_clear":
        get_session(uid)["active"] = True
        await query.edit_message_text("🧹 Cleared. Type command:", reply_markup=term_kb())
    elif data == "t_pwd":
        s = get_session(uid); s["active"] = True
        await query.edit_message_text(f"📁 `{s['cwd']}`",
                                       parse_mode=ParseMode.MARKDOWN, reply_markup=term_kb())
    elif data == "t_ls":
        s = get_session(uid); s["active"] = True
        out, _ = await run_term_cmd(uid, "ls -la")
        await query.edit_message_text(f"```\n{out}\n```",
                                       parse_mode=ParseMode.MARKDOWN, reply_markup=term_kb())
    elif data == "t_exit":
        get_session(uid)["active"] = False
        waiting_for_input.pop(uid_str, None)
        await query.edit_message_text("👋 Terminal closed.")
    elif data == "back_main":
        await query.edit_message_text("👇 Menu:")

    elif data.startswith("stop_"):
        idx = int(data.split("_")[1])
        procs = active_processes.get(uid_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except subprocess.TimeoutExpired: p["proc"].kill()
                await query.edit_message_text(f"✅ Stopped `{p['name']}`",
                                               parse_mode=ParseMode.MARKDOWN)
            else:
                await query.edit_message_text(f"ℹ️ Already stopped", parse_mode=ParseMode.MARKDOWN)
            procs.pop(idx)

    elif data.startswith("logs_"):
        idx = int(data.split("_")[1])
        procs = active_processes.get(uid_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if os.path.exists(p["log_path"]):
                with open(p["log_path"], 'r', errors='ignore') as f:
                    log = clean_ansi(f.read())[-3000:]
                await query.edit_message_text(
                    f"📝 *{p['name']}*:\n```\n{log}\n```",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=logs_kb(uid)
                )
            else:
                await query.edit_message_text("❌ No log file")

    elif data.startswith("dllog_"):
        idx = int(data.split("_")[1])
        procs = active_processes.get(uid_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if os.path.exists(p["log_path"]):
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=open(p["log_path"], 'rb'),
                    filename=f"{p['name']}.log",
                    caption=f"📄 Full log: `{p['name']}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.answer("❌ Log nahi mila", show_alert=True)


async def _admin_callback(query, context, uid, data):
    if data.startswith("approve_"):
        target = int(data.split("_")[1])
        t_str  = str(target)
        info   = bot_data.get("pending_users", {}).pop(t_str, None)
        if target not in bot_data["approved_users"]:
            bot_data["approved_users"].append(target)
        save_data(bot_data)
        name = info["name"] if info else str(target)
        await query.edit_message_text(
            f"✅ *{safe_md(name)}* approved!\n🆔 `{target}`",
            parse_mode=ParseMode.MARKDOWN
        )
        try:
            await context.bot.send_message(
                chat_id=target,
                text="✅ *Admin ne approve kar diya!*\nAbh /start karo.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception: pass

    elif data.startswith("ban_"):
        target = int(data.split("_")[1])
        t_str  = str(target)
        bot_data.get("pending_users", {}).pop(t_str, None)
        if target in bot_data.get("approved_users", []):
            bot_data["approved_users"].remove(target)
        if target not in bot_data.get("banned_users", []):
            bot_data.setdefault("banned_users", []).append(target)
        save_data(bot_data)
        name = safe_md(bot_data.get("user_info", {}).get(t_str, {}).get("name", str(target)))
        await query.edit_message_text(f"🚫 {name} ban ho gaya!")
        try: await context.bot.send_message(chat_id=target, text="🚫 Admin ne ban kar diya.")
        except Exception: pass

    elif data.startswith("unban_"):
        target = int(data.split("_")[1])
        if target in bot_data.get("banned_users", []):
            bot_data["banned_users"].remove(target)
        if target not in bot_data.get("approved_users", []):
            bot_data.setdefault("approved_users", []).append(target)
        save_data(bot_data)
        name = bot_data.get("user_info", {}).get(str(target), {}).get("name", str(target))
        await query.edit_message_text(
            f"✅ *{safe_md(name)}* unban!\n🆔 `{target}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb()
        )
        try:
            await context.bot.send_message(
                chat_id=target,
                text="✅ *Ban hata diya!*\nAbh /start karo.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception: pass

    elif data == "adm_stats":
        cleanup_all_dead()
        stats    = load_stats()
        pending  = len(bot_data.get("pending_users", {}))
        approved = len(bot_data.get("approved_users", []))
        banned   = len(bot_data.get("banned_users", []))
        running  = sum(1 for procs in active_processes.values()
                       for p in procs if p["proc"].poll() is None)
        total_scripts = sum(u.get("scripts_run", 0) for u in stats.values())
        total_errors  = sum(u.get("errors", 0) for u in stats.values())
        total_cmds    = sum(u.get("commands", 0) for u in stats.values())

        # top users by scripts run
        top = sorted(stats.items(), key=lambda x: x[1].get("scripts_run", 0), reverse=True)[:3]
        top_str = ""
        users_info = bot_data.get("user_info", {})
        for u_str, u_stat in top:
            uname = safe_md(users_info.get(u_str, {}).get("name", u_str))
            top_str += f"  • {uname}: {u_stat.get('scripts_run', 0)} scripts\n"

        await query.edit_message_text(
            f"📊 *Bot Stats*\n\n"
            f"✅ Approved: {approved} | ⏳ Pending: {pending} | 🚫 Banned: {banned}\n"
            f"🖥️ Running now: {running}\n\n"
            f"📈 *All time:*\n"
            f"  Scripts run: {total_scripts}\n"
            f"  Errors: {total_errors}\n"
            f"  Terminal cmds: {total_cmds}\n\n"
            f"🏆 *Top users:*\n{top_str or '  (koi data nahi)'}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb()
        )

    elif data == "adm_pending":
        pending = bot_data.get("pending_users", {})
        if not pending:
            await query.edit_message_text("⏳ Koi pending nahi.", reply_markup=admin_kb())
            return
        msg = "⏳ *Pending Requests:*\n\n"
        buttons = []
        for u_str, info in pending.items():
            msg += (f"👤 {safe_md(info.get('name', u_str))} | "
                    f"{safe_md(info.get('username', 'N/A'))} | "
                    f"ID: {info.get('uid', u_str)}\n")
            buttons.append([
                InlineKeyboardButton("✅", callback_data=f"approve_{info['uid']}"),
                InlineKeyboardButton("🚫", callback_data=f"ban_{info['uid']}")
            ])
        buttons.append([InlineKeyboardButton("🔙", callback_data="adm_back")])
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.edit_message_text(
                f"⏳ {len(pending)} pending.\n({e})",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

    elif data == "adm_users":
        approved   = bot_data.get("approved_users", [])
        users_info = bot_data.get("user_info", {})
        if not approved:
            await query.edit_message_text("👥 Koi approved user nahi.", reply_markup=admin_kb())
            return
        msg = "👥 *Approved Users:*\n\n"
        buttons = []
        for u_id in approved:
            info  = users_info.get(str(u_id), {})
            name  = safe_md(info.get("name", u_id))
            uname = safe_md(info.get("username", "N/A"))
            msg += f"• {name} | {uname} | {u_id}\n"
            buttons.append([InlineKeyboardButton(f"🚫 Ban {name}", callback_data=f"ban_{u_id}")])
        buttons.append([InlineKeyboardButton("🔙", callback_data="adm_back")])
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.edit_message_text(f"👥 {len(approved)} users.\n({e})",
                                           reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "adm_scripts":
        cleanup_all_dead()
        if not any(active_processes.values()):
            await query.edit_message_text("🖥️ Koi script nahi.", reply_markup=admin_kb())
            return
        msg = "🖥️ *All Scripts:*\n\n"
        buttons = []
        users_info = bot_data.get("user_info", {})
        for u_str, procs in active_processes.items():
            uname = safe_md(users_info.get(u_str, {}).get("name", u_str))
            for i, p in enumerate(procs):
                st = "🟢" if p["proc"].poll() is None else "🔴"
                up = int(time.time() - p["start_time"])
                m, s = divmod(up, 60)
                msg += f"{st} {safe_md(p['name'])} ({uname}) — {m}m{s}s\n"
                if p["proc"].poll() is None:
                    buttons.append([InlineKeyboardButton(
                        f"🛑 Kill {safe_md(p['name'])}",
                        callback_data=f"kill_{u_str}_{i}"
                    )])
        buttons.append([InlineKeyboardButton("🔙", callback_data="adm_back")])
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.edit_message_text(f"🖥️ Scripts chal rahi.\n({e})",
                                           reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("kill_"):
        parts = data.split("_", 2)
        t_str, idx = parts[1], int(parts[2])
        procs = active_processes.get(t_str, [])
        if 0 <= idx < len(procs):
            p = procs[idx]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except subprocess.TimeoutExpired: p["proc"].kill()
                await query.edit_message_text(
                    f"🛑 Killed `{safe_md(p['name'])}`", parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.edit_message_text("ℹ️ Already stopped.")
            procs.pop(idx)
        else:
            await query.edit_message_text("❌ Nahi mila.")

    elif data == "adm_back":
        cleanup_all_dead()
        pending  = len(bot_data.get("pending_users", {}))
        approved = len(bot_data.get("approved_users", []))
        running  = sum(1 for procs in active_processes.values()
                       for p in procs if p["proc"].poll() is None)
        await query.edit_message_text(
            f"👑 *Admin Panel*\n✅ {approved} | ⏳ {pending} | 🖥️ {running}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb()
        )

    elif data == "adm_banned":
        banned     = bot_data.get("banned_users", [])
        users_info = bot_data.get("user_info", {})
        if not banned:
            await query.edit_message_text("✅ Koi banned nahi.", reply_markup=admin_kb())
            return
        msg = "🚫 *Banned Users:*\n\n"
        buttons = []
        for b_id in banned:
            info  = users_info.get(str(b_id), {})
            name  = safe_md(info.get("name", str(b_id)))
            uname = safe_md(info.get("username", "N/A"))
            msg += f"• {name} | {uname} | {b_id}\n"
            buttons.append([InlineKeyboardButton(f"✅ Unban {name}", callback_data=f"unban_{b_id}")])
        buttons.append([InlineKeyboardButton("🔙", callback_data="adm_back")])
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            await query.edit_message_text(f"🚫 {len(banned)} banned.\n({e})",
                                           reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "adm_broadcast":
        _broadcast_waiting.add(uid)
        await query.edit_message_text(
            "📢 *Broadcast Mode*\n\nAbh jo message type karo woh sab approved users ko jayega.\n"
            "Cancel karne ke liye /cancel likho.",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_shared":
        files = list_shared_scripts()
        if not files:
            await query.edit_message_text(
                "📂 *Shared Scripts*\n\nKoi file nahi hai.\nUpload karo aur admin panel se yahan aao.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=admin_kb()
            )
            return
        await query.edit_message_text(
            f"📂 *Shared Scripts* ({len(files)} files)\n\nDelete karne ke liye tap karo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_shared_kb()
        )

    elif data.startswith("del_shared_"):
        fname = data[len("del_shared_"):]
        fpath = os.path.join(SHARED_DIR, fname)
        if os.path.exists(fpath):
            os.remove(fpath)
            await query.edit_message_text(
                f"🗑 `{fname}` delete ho gaya.", parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb()
            )
        else:
            await query.edit_message_text("❌ File nahi mili.")

    elif data == "adm_file_approvals":
        pending_files = bot_data.get("pending_files", {})
        if not pending_files:
            await query.edit_message_text("✅ Koi pending file nahi.", reply_markup=admin_kb())
            return
        msg = "📁 *Pending File Approvals:*\n\n"
        buttons = []
        for u_str, files in pending_files.items():
            uname = safe_md(bot_data.get("user_info", {}).get(u_str, {}).get("name", u_str))
            for fname in files:
                msg += f"• {uname}: `{fname}`\n"
                buttons.append([
                    InlineKeyboardButton(f"✅ {fname}", callback_data=f"fapprove_{u_str}_{fname}"),
                    InlineKeyboardButton("🚫",          callback_data=f"freject_{u_str}_{fname}"),
                    InlineKeyboardButton("📂 Shared",   callback_data=f"fshared_{u_str}_{fname}")
                ])
        buttons.append([InlineKeyboardButton("🔙", callback_data="adm_back")])
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                           reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            await query.edit_message_text(
                "📁 Pending files hain.", reply_markup=InlineKeyboardMarkup(buttons)
            )

    elif data.startswith("fapprove_"):
        _, u_str, fname = data.split("_", 2)
        fpath = os.path.join(DOWNLOADS_DIR, u_str, fname)
        # remove from pending
        bot_data.get("pending_files", {}).get(u_str, [fname]).remove(fname) \
            if fname in bot_data.get("pending_files", {}).get(u_str, []) else None
        save_data(bot_data)
        await query.edit_message_text(f"✅ `{fname}` approved — user ko notify kar diya.",
                                       parse_mode=ParseMode.MARKDOWN)
        try:
            target = int(u_str)
            await context.bot.send_message(
                chat_id=target,
                text=f"✅ *Admin ne `{fname}` approve kar diya!*\nTerminal me run karo.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception: pass

    elif data.startswith("freject_"):
        _, u_str, fname = data.split("_", 2)
        fpath = os.path.join(DOWNLOADS_DIR, u_str, fname)
        if os.path.exists(fpath): os.remove(fpath)
        pf = bot_data.get("pending_files", {}).get(u_str, [])
        if fname in pf: pf.remove(fname)
        save_data(bot_data)
        await query.edit_message_text(f"🚫 `{fname}` reject kar diya.", parse_mode=ParseMode.MARKDOWN)
        try:
            await context.bot.send_message(
                chat_id=int(u_str),
                text=f"🚫 *Admin ne `{fname}` reject kar diya.*",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception: pass

    elif data.startswith("fshared_"):
        _, u_str, fname = data.split("_", 2)
        src = os.path.join(DOWNLOADS_DIR, u_str, fname)
        dst = os.path.join(SHARED_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
        pf = bot_data.get("pending_files", {}).get(u_str, [])
        if fname in pf: pf.remove(fname)
        save_data(bot_data)
        await query.edit_message_text(
            f"📂 `{fname}` shared scripts me add ho gaya — sab use kar sakte hain!",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_close":
        await query.delete_message()

# ═══════════════════════════════════════════════════
# SCRIPT LAUNCHER (shared helper for upload + shared run)
# ═══════════════════════════════════════════════════
async def _launch_script(uid, uid_str, fname, fpath, work_dir, context, chat_id, msg):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    log_path = os.path.join(LOGS_DIR, f"{uid}_{fname}_{int(time.time())}.log")

    if fname.endswith('.py'):
        run_cmd = [sys.executable, os.path.abspath(fpath)]
    elif fname.endswith('.js'):
        run_cmd = ["node", os.path.abspath(fpath)]
    else:
        await msg.edit_text("❌ Unsupported file type")
        return

    max_retries = 5
    proc  = None
    log_f = None

    for attempt in range(max_retries):
        if log_f and not log_f.closed:
            log_f.close()
        log_f = open(log_path, "w", encoding="utf-8", errors="ignore")
        proc  = subprocess.Popen(
            run_cmd, stdin=subprocess.PIPE,
            stdout=log_f, stderr=log_f,
            cwd=work_dir, env=env, text=True,
            start_new_session=True
        )

        await asyncio.sleep(2)

        if proc.poll() is None:
            break  # alive

        log_f.close()
        with open(log_path, 'r', errors='ignore') as f:
            err = f.read()

        missing = re.search(r"ModuleNotFoundError: No module named '([^']+)'", err)
        if missing:
            mod = missing.group(1).split('.')[0]
            pkg = PIP_MAP.get(mod, mod)
            await msg.edit_text(f"📦 Missing `{mod}` — installing `{pkg}`... ({attempt+1}/{max_retries})")
            result = pip_install(pkg)
            if result.returncode != 0:
                await msg.edit_text(
                    f"❌ Install failed `{pkg}`:\n```\n{result.stderr[-500:]}\n```",
                    parse_mode=ParseMode.MARKDOWN
                )
                record_stat(uid_str, "errors")
                return
            continue
        else:
            clean = clean_ansi(err).strip()
            await msg.edit_text(
                f"❌ `{fname}` crash!\n```\n{clean[-1000:]}\n```",
                parse_mode=ParseMode.MARKDOWN
            )
            record_stat(uid_str, "errors")
            return
    else:
        if log_f and not log_f.closed: log_f.close()
        with open(log_path, 'r', errors='ignore') as f:
            err = f.read()[-1000:]
        await msg.edit_text(
            f"❌ `{fname}` crashed after {max_retries} tries!\n```\n{err}\n```",
            parse_mode=ParseMode.MARKDOWN
        )
        record_stat(uid_str, "errors")
        return

    if uid_str not in active_processes:
        active_processes[uid_str] = []
    proc_entry = {
        "name":       fname,
        "proc":       proc,
        "log_path":   log_path,
        "log_f":      log_f,
        "start_time": time.time()
    }
    active_processes[uid_str].append(proc_entry)
    record_stat(uid_str, "scripts_run")

    await msg.edit_text(
        f"✅ `{fname}` running! (PID: {proc.pid})\n"
        f"📤 Output forward hoga\n⌨️ Input maangega toh bot poochega",
        parse_mode=ParseMode.MARKDOWN
    )

    asyncio.create_task(monitor_process_stdin(uid_str, proc_entry, context, chat_id))
    asyncio.create_task(enforce_timeout(uid_str, proc_entry, context, chat_id))


async def enforce_timeout(uid_str, proc_entry, context, chat_id):
    """Auto-kill script after MAX_SCRIPT_RUNTIME seconds."""
    await asyncio.sleep(MAX_SCRIPT_RUNTIME)
    proc = proc_entry["proc"]
    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ `{proc_entry['name']}` auto-kill — {MAX_SCRIPT_RUNTIME//60} min limit.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception: pass

# ═══════════════════════════════════════════════════
# FILE UPLOAD
# ═══════════════════════════════════════════════════
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    uid_str = str(uid)
    doc     = update.message.document
    fname   = doc.file_name
    user_dir = os.path.join(DOWNLOADS_DIR, uid_str)
    os.makedirs(user_dir, exist_ok=True)
    fpath = os.path.join(user_dir, fname)

    msg = await update.message.reply_text(f"⏳ Downloading `{fname}`...")

    try:
        file = await context.bot.get_file(doc.file_id)
        downloaded = False

        for attempt_fn in [
            lambda: file.download_to_drive(fpath),
            lambda: file.download(custom_path=fpath),
        ]:
            if downloaded: break
            try:
                await attempt_fn()
                downloaded = os.path.exists(fpath) and os.path.getsize(fpath) > 0
            except Exception: pass

        if not downloaded:
            try:
                content = await file.download_as_bytearray()
                with open(fpath, 'wb') as f:
                    f.write(content)
                downloaded = os.path.exists(fpath) and os.path.getsize(fpath) > 0
            except Exception: pass

        if not downloaded:
            await msg.edit_text("❌ Download failed")
            return

        # ── if owner: save to shared + ask, or run directly
        if is_owner(uid):
            await msg.edit_text(
                f"✅ Downloaded `{fname}`\n\n"
                f"Admin hone ke naate direct run hogi ya shared me add karo?\n"
                f"📂 Shared me add karo toh sab use kar sakte hain.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Direct Run",    callback_data=f"fapprove_{uid_str}_{fname}"),
                     InlineKeyboardButton("📂 Add to Shared", callback_data=f"fshared_{uid_str}_{fname}")]
                ])
            )
            # pre-save to pending so fapprove/fshared can find it
            bot_data.setdefault("pending_files", {}).setdefault(uid_str, [])
            if fname not in bot_data["pending_files"][uid_str]:
                bot_data["pending_files"][uid_str].append(fname)
            save_data(bot_data)
            return

        # ── regular user: install deps then send for approval
        await msg.edit_text(f"✅ Downloaded `{fname}`\n📨 Admin ke paas approval ke liye bhej raha hoon...")

        if fname.endswith('.py'):
            with open(fpath, 'r', errors='ignore') as f:
                source = f.read()
            for pkg in extract_external_deps(source):
                await msg.edit_text(f"📦 Installing {pkg}...")
                pip_install(pkg)

        bot_data.setdefault("pending_files", {}).setdefault(uid_str, [])
        if fname not in bot_data["pending_files"][uid_str]:
            bot_data["pending_files"][uid_str].append(fname)
        save_data(bot_data)

        await msg.edit_text(
            f"📨 `{fname}` admin ke paas approval ke liye bhej diya.\n"
            f"Approve hone par tum run kar sakoge. ⏳",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"📁 *File Approval Request*\n\n"
                    f"👤 {safe_md(update.effective_user.full_name)}\n"
                    f"🆔 `{uid}`\n"
                    f"📄 `{fname}`"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=file_approval_kb(uid, fname)
            )
        except Exception as e:
            logger.warning(f"file approval notify failed: {e}")

    except Exception as e:
        logger.error(f"handle_upload: {e}")
        await msg.edit_text(f"❌ Error: {e}")

# ═══════════════════════════════════════════════════
# MESSAGE HANDLER
# ═══════════════════════════════════════════════════
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    uid_str = str(uid)
    text    = update.message.text

    if is_banned(uid):
        await update.message.reply_text("🚫 Banned.")
        return

    if not is_auth(uid):
        if uid_str in bot_data.get("pending_users", {}): return
        await request_access(update, context)
        return

    # broadcast input from owner
    if uid in _broadcast_waiting and text and not update.message.document:
        if text.strip().lower() == "/cancel":
            _broadcast_waiting.discard(uid)
            await update.message.reply_text("❌ Broadcast cancel.", reply_markup=main_kb(uid))
            return
        _broadcast_waiting.discard(uid)
        approved = bot_data.get("approved_users", [])
        sent = 0
        fail = 0
        status = await update.message.reply_text(f"📢 Broadcasting to {len(approved)} users...")
        for target in approved:
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text=f"📢 *Admin message:*\n\n{text}",
                    parse_mode=ParseMode.MARKDOWN
                )
                sent += 1
            except Exception:
                fail += 1
        await status.edit_text(f"📢 Broadcast done!\n✅ Sent: {sent} | ❌ Failed: {fail}")
        return

    # rate limit
    if not is_owner(uid) and not check_rate_limit(uid_str):
        await update.message.reply_text(
            f"⚡ Thoda slow karo — {RATE_LIMIT_CMDS} cmds per {RATE_LIMIT_WINDOW}s limit.",
        )
        return

    session = get_session(uid)

    # stdin for running process
    if uid_str in process_stdin_waiting and text and not update.message.document:
        proc_entry = process_stdin_waiting.pop(uid_str)
        proc = proc_entry["proc"]
        if proc.poll() is None:
            try:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
                await update.message.reply_text(
                    f"📥 Sent: `{text}`", parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Stdin error: {e}")
        else:
            await update.message.reply_text("ℹ️ Script khatam ho gaya.")
        return

    # waiting_for_input (terminal input injection)
    if uid_str in waiting_for_input and text and not update.message.document:
        info     = waiting_for_input.pop(uid_str)
        cmd      = info["cmd"]
        user_val = text.strip()
        await update.message.reply_text(f"⏳ Running `{info['name']}` with input...")
        out, cwd = await run_term_cmd(uid, cmd, user_input_val=user_val)
        await update.message.reply_text(
            f"💻 `$ {cmd}`\n📥 *Input:* `{user_val}`\n```\n{out}\n```\n📁 `{cwd}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=term_kb()
        )
        return

    # terminal active
    if session["active"] and text and not update.message.document:
        cmd = text.strip()
        cwd = session["cwd"]
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()

        target_file = None
        if parts[0].lower() in ('python','python3') and len(parts) > 1:
            found = find_file_in_dir(cwd, parts[1])
            if found and found.endswith('.py'):
                target_file = os.path.join(cwd, found)
        elif not is_builtin_cmd(parts[0]) and '/' not in parts[0] and '.' not in parts[0]:
            found = find_file_in_dir(cwd, parts[0])
            if found and found.endswith('.py'):
                target_file = os.path.join(cwd, found)

        if target_file and has_input_function(target_file):
            waiting_for_input[uid_str] = {
                "cmd": cmd, "cwd": cwd, "name": os.path.basename(target_file)
            }
            await update.message.reply_text(
                f"⚠️ `{os.path.basename(target_file)}` me `input()` mila!\n👇 Type karo:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=term_kb()
            )
            return

        out, new_cwd = await run_term_cmd(uid, cmd)

        if not session["active"]:
            await update.message.reply_text(
                f"💻 `$ {cmd}`\n```\n{out}\n```\n👋 Terminal closed.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_kb(uid)
            )
        else:
            await update.message.reply_text(
                f"💻 `$ {cmd}`\n```\n{out}\n```\n📁 `{new_cwd}`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=term_kb()
            )
        return

    # menu buttons
    if text == "💻 Terminal":
        session["active"] = True
        await update.message.reply_text(
            f"💻 *Terminal Active*\n📁 `{session['cwd']}`\n\n"
            f"⚡ Auto Input ON\n• `ls` `cd` `pwd`\n• `exit` to close",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=term_kb()
        )

    elif text == "📁 Upload File":
        if is_owner(uid):
            await update.message.reply_text(
                "📤 Send .py / .js / .zip\n"
                "Admin hone ke naate directly run ya shared me add kar sakte ho.",
                reply_markup=main_kb(uid)
            )
        else:
            await update.message.reply_text(
                "📤 Send .py / .js / .zip\n"
                "⚠️ File admin approval ke baad run hogi.",
                reply_markup=main_kb(uid)
            )

    elif text == "🛑 Stop Script":
        cleanup_dead_processes(uid_str)
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("❌ No scripts running")
        else:
            await update.message.reply_text("🛑 Select:", reply_markup=stop_kb(uid))

    elif text == "📂 My Scripts":
        cleanup_dead_processes(uid_str)
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("📂 Koi script nahi")
        else:
            msg = "📂 *Scripts:*\n\n"
            for i, p in enumerate(procs):
                st = "🟢" if p["proc"].poll() is None else "🔴"
                up = int(time.time() - p["start_time"])
                m, s = divmod(up, 60)
                msg += f"{i+1}. {st} `{p['name']}` ({m}m{s}s)\n"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    elif text == "📝 View Logs":
        procs = active_processes.get(uid_str, [])
        if not procs:
            await update.message.reply_text("❌ Koi script nahi")
        else:
            await update.message.reply_text("📝 Select:", reply_markup=logs_kb(uid))

    elif text == "🌐 Shared Scripts":
        files = list_shared_scripts()
        if not files:
            await update.message.reply_text("🌐 Abhi koi shared script nahi hai.")
        else:
            await update.message.reply_text(
                f"🌐 *Shared Scripts* — {len(files)} available\nSelect karo:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=shared_scripts_kb()
            )

    elif text == "📊 My Stats":
        stats = load_stats()
        u = stats.get(uid_str, {})
        scripts  = u.get("scripts_run", 0)
        errors   = u.get("errors", 0)
        commands = u.get("commands", 0)
        runtime  = u.get("total_runtime", 0)
        await update.message.reply_text(
            f"📊 *Tumhara Stats:*\n\n"
            f"▶️ Scripts run: {scripts}\n"
            f"❌ Errors: {errors}\n"
            f"💻 Terminal commands: {commands}",
            parse_mode=ParseMode.MARKDOWN
        )

    elif text == "👑 Admin Panel":
        if not is_owner(uid):
            await update.message.reply_text("🚫 Sirf admin.")
            return
        cleanup_all_dead()
        pending  = len(bot_data.get("pending_users", {}))
        approved = len(bot_data.get("approved_users", []))
        running  = sum(1 for procs in active_processes.values()
                       for p in procs if p["proc"].poll() is None)
        pf_count = sum(len(v) for v in bot_data.get("pending_files", {}).values())
        await update.message.reply_text(
            f"👑 *Admin Panel*\n\n"
            f"✅ Approved: {approved}\n"
            f"⏳ Pending users: {pending}\n"
            f"📁 Pending files: {pf_count}\n"
            f"🖥️ Running: {running}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_kb()
        )

    elif update.message.document:
        await handle_upload(update, context)

# ═══════════════════════════════════════════════════
# PROCESS MONITOR
# ═══════════════════════════════════════════════════
async def monitor_process_stdin(uid_str, proc_entry, context, chat_id):
    proc      = proc_entry["proc"]
    log_path  = proc_entry["log_path"]
    last_size = 0
    last_output_time = time.time()
    stdin_prompted   = False
    live_msg         = None
    last_edit_time   = 0
    EDIT_INTERVAL    = 3
    INPUT_WAIT_SEC   = 3
    input_prompt_count = 0

    while proc.poll() is None:
        await asyncio.sleep(1)
        if proc.poll() is not None: break

        try:
            current_size = os.path.getsize(log_path)
        except Exception:
            current_size = last_size

        if current_size > last_size:
            last_size = current_size
            last_output_time = time.time()
            if uid_str not in process_stdin_waiting:
                stdin_prompted = False

            now = time.time()
            if now - last_edit_time >= EDIT_INTERVAL:
                last_edit_time = now
                try:
                    with open(log_path, 'r', errors='ignore') as f:
                        raw = f.read()
                    snippet = clean_ansi(raw)[-3500:].strip()
                except Exception:
                    snippet = ""

                if snippet:
                    msg_text = f"📺 *Live Output:*\n```\n{snippet}\n```"
                    try:
                        if live_msg is None:
                            live_msg = await context.bot.send_message(
                                chat_id=chat_id, text=msg_text, parse_mode=ParseMode.MARKDOWN
                            )
                        else:
                            await live_msg.edit_text(msg_text, parse_mode=ParseMode.MARKDOWN)
                    except Exception: pass

        elif (not stdin_prompted
              and (time.time() - last_output_time) > INPUT_WAIT_SEC
              and last_size > 0
              and proc.poll() is None):
            stdin_prompted = True
            input_prompt_count += 1
            process_stdin_waiting[uid_str] = proc_entry
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⌨️ *Script input maang rahi hai* — type karo:\n_(Input #{input_prompt_count})_",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception: pass

    if process_stdin_waiting.get(uid_str) is proc_entry:
        del process_stdin_waiting[uid_str]

    log_f = proc_entry.get("log_f")
    if log_f and not log_f.closed:
        try: log_f.close()
        except Exception: pass

    try:
        with open(log_path, 'r', errors='ignore') as f:
            raw = f.read()
        snippet = clean_ansi(raw)[-3500:].strip()
    except Exception:
        snippet = ""

    exit_code = proc.returncode
    runtime   = int(time.time() - proc_entry["start_time"])
    record_stat(uid_str, "total_runtime", runtime)

    if exit_code == 0:           status_line = "✅ *Script khatam hua!*"
    elif exit_code in (-15,-9):  status_line = f"🛑 *Manually stop kiya* (signal {exit_code})"
    elif exit_code == 1:         status_line = "❌ *Error se ruka* (exit 1)"
    else:                        status_line = f"⚠️ *Ruka* (exit: {exit_code})"

    if exit_code not in (0,-15,-9):
        record_stat(uid_str, "errors")

    done_text = (
        f"{status_line}\n📄 `{proc_entry['name']}` | ⏱ {runtime//60}m{runtime%60}s\n```\n{snippet}\n```"
        if snippet else
        f"{status_line}\n📄 `{proc_entry['name']}` | ⏱ {runtime//60}m{runtime%60}s"
    )
    try:
        if live_msg:
            await live_msg.edit_text(done_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=done_text, parse_mode=ParseMode.MARKDOWN
            )
    except Exception: pass

# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("Bot v2 starting...")
    app.run_polling(drop_pending_updates=True)
