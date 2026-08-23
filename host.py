# ter.py
import os
import sys
import json
import time
import shutil
import zipfile
import logging
import asyncio
import threading
import subprocess
import re
import platform
import select
from datetime import datetime
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

# ═══════════════════════════════════════════════════════
# 🔧 CONFIGURATION
# ═══════════════════════════════════════════════════════
TOKEN = "8994170789:AAHgSO4DHxosNicVeLHTpNVMAy7LRfUwY1A"
OWNER_ID = 8502412097
DATA_FILE = "bot_data.json"
DOWNLOADS_DIR = "downloads"
LOGS_DIR = "logs"
BACKUP_DIR = "backups"
MAX_SCRIPTS_PER_USER = 10
AUTO_RESTART_DEFAULT = True
MAX_LOG_SIZE = 4096

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

# ═══════════════════════════════════════════════════════
# 📊 DATA STRUCTURES & PERSISTENCE
# ═══════════════════════════════════════════════════════
def load_data():
    default = {
        "approved_users": {},
        "banned_users": [],
        "user_settings": {},
        "script_history": {},
        "broadcast_log": [],
        "pending_users": {}
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
                default.update(data)
        except Exception:
            pass
    return default

def save_data():
    try:
        serializable = {
            "approved_users": bot_data["approved_users"],
            "banned_users": bot_data["banned_users"],
            "user_settings": bot_data.get("user_settings", {}),
            "script_history": bot_data.get("script_history", {}),
            "broadcast_log": bot_data.get("broadcast_log", [])[-50:],
            "pending_users": bot_data.get("pending_users", {})
        }
        with open(DATA_FILE, 'w') as f:
            json.dump(serializable, f, indent=2)
    except Exception as e:
        logger.error(f"Save error: {e}")

bot_data = load_data()
active_processes = {}

# ═══════════════════════════════════════════════════════
# 💻 INTERACTIVE TERMINAL SESSION MANAGER
# ═══════════════════════════════════════════════════════
terminal_sessions = {}

# ✅ FIXED: Use sys.executable directly for Python path
PYTHON_PATH = sys.executable

def get_terminal_session(user_id):
    uid = str(user_id)
    if uid not in terminal_sessions:
        user_folder = os.path.join(DOWNLOADS_DIR, uid)
        os.makedirs(user_folder, exist_ok=True)
        terminal_sessions[uid] = {
            "cwd": os.path.abspath(user_folder),
            "env": os.environ.copy(),
            "history": [],
            "active": False,
            "inter_proc": None,
            "inter_thread": None,
            "inter_lock": threading.Lock(),
            "input_queue": [],
            "input_event": threading.Event(),
            "waiting_input": False,
            "last_output": ""
        }
    return terminal_sessions[uid]

def get_terminal_keyboard(user_id):
    session = get_terminal_session(user_id)
    if session["waiting_input"]:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel / Kill", callback_data="term_kill")]
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Clear Screen", callback_data="term_clear"),
         InlineKeyboardButton("📁 Current Dir", callback_data="term_pwd")],
        [InlineKeyboardButton("📜 History", callback_data="term_history"),
         InlineKeyboardButton("📋 List Files", callback_data="term_ls")],
        [InlineKeyboardButton("❌ Exit Terminal", callback_data="term_exit")]
    ])

def find_file_without_ext(cwd, name):
    possible = [name + '.py', name + '.js', name + '.txt', name + '.json', name + '.sh']
    for ext_file in possible:
        full_path = os.path.join(cwd, ext_file)
        if os.path.exists(full_path):
            return ext_file
    return name

def fix_command(command, cwd):
    cmd = command.strip()
    py_match = re.match(r'^(python3?)\s+(.+)$', cmd, re.IGNORECASE)
    if py_match:
        rest = py_match.group(2).strip()
        parts = rest.split()
        if parts:
            filename = parts[0]
            if not filename.endswith('.py') and not filename.endswith('.txt'):
                found = find_file_without_ext(cwd, filename)
                parts[0] = found
            rest = ' '.join(parts)
        cmd = f'{PYTHON_PATH} {rest}'
    pip_match = re.match(r'^(pip3?)\s+(.+)$', cmd, re.IGNORECASE)
    if pip_match:
        rest = pip_match.group(2)
        cmd = f'{PYTHON_PATH} -m pip {rest}'
    node_match = re.match(r'^(node)\s+(.+)$', cmd, re.IGNORECASE)
    if node_match:
        rest = node_match.group(2).strip()
        parts = rest.split()
        if parts:
            filename = parts[0]
            if not filename.endswith('.js'):
                found = find_file_without_ext(cwd, filename)
                if found.endswith('.js'):
                    parts[0] = found
            rest = ' '.join(parts)
        cmd = f'node {rest}'
    return cmd

def _run_interactive(session, command, cwd, env):
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True
        )
        session["inter_proc"] = proc
        output = ""
        start = time.time()
        timeout = 10
        while proc.poll() is None and (time.time() - start) < timeout:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096).decode('utf-8', errors='ignore')
                if chunk:
                    output += chunk
                    start = time.time()
            else:
                if output:
                    break
                time.sleep(0.1)
        if proc.poll() is None and output:
            session["waiting_input"] = True
            return output, True
        if proc.poll() is not None:
            try:
                remaining = proc.stdout.read()
                if remaining:
                    output += remaining
            except:
                pass
            return output, False
        return "⏱️ Process started but no output yet...", True
    except Exception as e:
        return f"❌ Error: {str(e)}", False

def send_input_to_process(session, text):
    proc = session.get("inter_proc")
    if proc and proc.poll() is None and proc.stdin:
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            return True
        except:
            return False
    return False

async def execute_terminal_command(user_id, command, bot=None):
    session = get_terminal_session(user_id)
    if session["waiting_input"]:
        sent = send_input_to_process(session, command)
        if sent:
            proc = session["inter_proc"]
            output = ""
            start = time.time()
            while proc.poll() is None and (time.time() - start) < 10:
                ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                if ready:
                    chunk = os.read(proc.stdout.fileno(), 4096).decode('utf-8', errors='ignore')
                    if chunk:
                        output += chunk
                        start = time.time()
                else:
                    if output:
                        break
                    time.sleep(0.1)
            if proc.poll() is not None:
                session["waiting_input"] = False
                session["inter_proc"] = None
                try:
                    remaining = proc.stdout.read()
                    if remaining:
                        output += remaining
                except:
                    pass
                return output, session["cwd"]
            if not output:
                return "✅ Input sent. Waiting for response...", session["cwd"]
            return output, session["cwd"]
        else:
            session["waiting_input"] = False
            session["inter_proc"] = None
            return "❌ Process ended or cannot accept input.", session["cwd"]

    fixed_command = fix_command(command, session["cwd"])
    session["history"].append(command)
    if len(session["history"]) > 50:
        session["history"] = session["history"][-50:]

    cmd_stripped = fixed_command.strip()
    
    if cmd_stripped.startswith("cd ") or cmd_stripped == "cd":
        try:
            parts = cmd_stripped.split(None, 1)
            target = parts[1] if len(parts) > 1 else os.path.expanduser("~")
            if target == "..":
                new_path = os.path.dirname(session["cwd"])
            elif target == "~":
                new_path = os.path.expanduser("~")
            elif os.path.isabs(target):
                new_path = target
            else:
                new_path = os.path.join(session["cwd"], target)
            new_path = os.path.normpath(new_path)
            if os.path.isdir(new_path):
                session["cwd"] = new_path
                return f"📁 Changed directory to:\n`{session['cwd']}`", session["cwd"]
            else:
                return f"❌ Directory not found: `{target}`", session["cwd"]
        except Exception as e:
            return f"❌ cd error: {e}", session["cwd"]

    if cmd_stripped.lower() in ("exit", "quit"):
        session["active"] = False
        return "👋 Terminal session ended.", session["cwd"]

    if cmd_stripped.lower() == "clear":
        return "🧹 Screen cleared.", session["cwd"]

    if cmd_stripped.lower() == "pwd":
        return f"📁 {session['cwd']}", session["cwd"]

    if cmd_stripped.lower() == "kill" or cmd_stripped.lower() == "ctrl+c":
        if session.get("inter_proc") and session["inter_proc"].poll() is None:
            session["inter_proc"].terminate()
            session["inter_proc"] = None
            session["waiting_input"] = False
            return "✅ Process killed.", session["cwd"]
        return "❌ No interactive process running.", session["cwd"]

    try:
        env = session["env"].copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["TERM"] = "xterm-256color"

        needs_interactive = False
        if re.search(r'\bpython|python3|node|bash|sh\b', cmd_stripped, re.IGNORECASE):
            needs_interactive = True

        if needs_interactive and bot:
            output, waiting = _run_interactive(session, fixed_command, session["cwd"], env)
            if waiting:
                return f"```\n{output}\n```\n\n⌨️ *Script is waiting for input!*\nType your answer below:", session["cwd"]
            return f"```\n{output}\n```", session["cwd"]
        else:
            result = subprocess.run(
                fixed_command,
                shell=True,
                cwd=session["cwd"],
                env=env,
                capture_output=True,
                text=True,
                timeout=60
            )
            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            if not output:
                output = "(no output)"
            if len(output) > 3500:
                output = "...\n" + output[-3500:]
            return output, session["cwd"]

    except subprocess.TimeoutExpired:
        return "⏱️ Command timed out (60s limit)", session["cwd"]
    except Exception as e:
        return f"❌ Error: {str(e)}", session["cwd"]

# ═══════════════════════════════════════════════════════
# 🌐 FLASK KEEP-ALIVE (FIXED PORT FOR RAILWAY)
# ═══════════════════════════════════════════════════════
app = Flask('')

@app.route('/')
def home():
    total_procs = sum(len([p for p in procs if p["proc"].poll() is None]) for procs in active_processes.values())
    return f"<h1>🤖 SODOBOT</h1><p>Online | Scripts: {total_procs}</p>"

@app.route('/health')
def health():
    return {"status": "ok"}, 200

def run_flask():
    # ✅ FIX: Dynamic port for Railway/Render/Heroku
    try:
        port = int(os.environ.get('PORT', 8080))
    except:
        port = 8080
    
    # ✅ FIX: Try multiple ports if busy
    for p in [port, 8080, 8081, 8082, 3000, 5000]:
        try:
            app.run(host='0.0.0.0', port=p, debug=False, threaded=True)
            logger.info(f"✅ Flask running on port {p}")
            break
        except OSError as e:
            logger.warning(f"⚠️ Port {p} busy, trying next...")
            continue

def keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

# ═══════════════════════════════════════════════════════
# 📝 LOGGING
# ═══════════════════════════════════════════════════════
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(LOGS_DIR, 'bot.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════
# 🔍 UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════
def get_stdlib_modules():
    if sys.version_info >= (3, 10):
        return sys.stdlib_module_names
    else:
        import distutils.sysconfig as sysconfig
        std_lib = sysconfig.get_python_lib(standard_lib=True)
        return set(os.listdir(std_lib))

STDLIB_MODULES = get_stdlib_modules()

def is_authorized(user_id):
    uid = str(user_id)
    return uid == str(OWNER_ID) or uid in bot_data["approved_users"]

def is_owner(user_id):
    return str(user_id) == str(OWNER_ID)

def is_banned(user_id):
    return str(user_id) in bot_data.get("banned_users", [])

def format_uptime(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    elif seconds < 86400:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    else:
        return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"

def get_system_info():
    try:
        cpu_count = os.cpu_count() or 'N/A'
        disk = os.statvfs('.')
        total_disk = disk.f_blocks * disk.f_frsize
        free_disk = disk.f_bavail * disk.f_frsize
        used_disk = total_disk - free_disk
        disk_percent = (used_disk / total_disk) * 100 if total_disk > 0 else 0
        disk_info = f"{disk_percent:.1f}% ({used_disk//(1024**3)}GB/{total_disk//(1024**3)}GB)"
        return {"cpu_count": cpu_count, "disk": disk_info, "platform": platform.platform(), "python": sys.version.split()[0]}
    except:
        return {"cpu_count": "N/A", "disk": "N/A", "platform": platform.platform(), "python": sys.version.split()[0]}

def scan_python_dependencies(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        imports = re.findall(r'^\s*(?:from|import)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE)
        return list(set(imports))
    except:
        return []

def scan_js_dependencies(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        requires = re.findall(r'require\([\'"](.+?)[\'"]\)', content)
        imports = re.findall(r'from\s+[\'"](.+?)[\'"]', content)
        return list(set(requires + imports))
    except:
        return []

PIP_MAPPINGS = {
    "telegram": "python-telegram-bot", "PIL": "pillow", "cv2": "opencv-python",
    "sklearn": "scikit-learn", "yaml": "pyyaml", "bs4": "beautifulsoup4",
    "selenium": "selenium", "requests": "requests", "flask": "flask",
    "django": "django", "pandas": "pandas", "numpy": "numpy",
    "matplotlib": "matplotlib", "aiohttp": "aiohttp", "discord": "discord.py",
    "pytube": "pytube", "yt_dlp": "yt-dlp",
}

async def install_deps(update, msg, deps, pkg_mgr):
    if not deps:
        return
    if pkg_mgr == "pip":
        deps = [dep for dep in deps if dep not in STDLIB_MODULES]
    if not deps:
        return
    await msg.edit_text(f"📦 Found {len(deps)} dependencies. Installing via {pkg_mgr}...")
    installed = []
    failed = []
    for dep in deps:
        pkg_name = PIP_MAPPINGS.get(dep, dep) if pkg_mgr == "pip" else dep
        try:
            if pkg_mgr == "pip":
                # ✅ FIX: --break-system-packages for Python 3.11+
                cmd = [sys.executable, "-m", "pip", "install", pkg_name, 
                       "--no-cache-dir", "--quiet", "--break-system-packages"]
            else:
                cmd = ["npm", "install", pkg_name, "--silent"]
            result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
            if result.returncode == 0:
                installed.append(pkg_name)
            else:
                failed.append(pkg_name)
        except:
            failed.append(pkg_name)
    summary = f"📦 Dependencies: ✅ {len(installed)} installed"
    if failed:
        summary += f" | ❌ {len(failed)} failed ({', '.join(failed[:3])})"
    await msg.edit_text(summary)

# ═══════════════════════════════════════════════════════
# 🔄 PROCESS MONITOR
# ═══════════════════════════════════════════════════════
def process_monitor():
    while True:
        try:
            for user_id, procs in list(active_processes.items()):
                for i, p in enumerate(procs):
                    if p["proc"].poll() is not None:
                        uptime = time.time() - p["start_time"]
                        if p.get("auto_restart", False) and uptime > 5:
                            try:
                                # ✅ FIXED: Use absolute path correctly
                                log_file = open(p["log_path"], "a", encoding="utf-8", errors="ignore")
                                log_file.write(f"\n{'='*50}\n🔄 Auto-restart at {datetime.now()}\n{'='*50}\n")
                                log_file.close()
                                
                                # ✅ FIX: Ensure we're using the correct working directory
                                work_dir = p["work_dir"]
                                if not os.path.exists(work_dir):
                                    logger.warning(f"Work dir missing: {work_dir}, using user folder")
                                    work_dir = os.path.join(DOWNLOADS_DIR, user_id)
                                    os.makedirs(work_dir, exist_ok=True)
                                
                                # ✅ FIX: Properly construct the command with absolute paths
                                run_cmd = p["run_cmd"]
                                log_file = open(p["log_path"], "a", encoding="utf-8", errors="ignore")
                                proc = subprocess.Popen(
                                    run_cmd, 
                                    stdout=log_file, 
                                    stderr=log_file, 
                                    cwd=work_dir, 
                                    env=p["env"], 
                                    text=True, 
                                    start_new_session=True,
                                    shell=True
                                )
                                procs[i] = {**p, "proc": proc, "start_time": time.time(), "pid": proc.pid}
                            except Exception as e:
                                logger.error(f"Failed to restart {p['name']}: {e}")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            time.sleep(30)

def start_monitor():
    t = threading.Thread(target=process_monitor, daemon=True)
    t.start()

# ═══════════════════════════════════════════════════════
# 🎨 KEYBOARDS
# ═══════════════════════════════════════════════════════
def get_main_keyboard(user_id):
    if is_owner(user_id):
        keyboard = [
            [KeyboardButton("📁 Upload Files"), KeyboardButton("📂 My Scripts")],
            [KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics")],
            [KeyboardButton("📩 View Logs"), KeyboardButton("📞 Contact Owner")],
            [KeyboardButton("🛑 Stop Script"), KeyboardButton("🔄 Restart Script")],
            [KeyboardButton("🖥️ System Info"), KeyboardButton("🗑️ Delete Script")],
            [KeyboardButton("💻 Terminal"), KeyboardButton("📢 Broadcast")],
            [KeyboardButton("⚙️ Settings"), KeyboardButton("📦 Backup")],
            [KeyboardButton("👤 Admin Panel")],
            [KeyboardButton("❌ Close Menu")],
        ]
    else:
        keyboard = [
            [KeyboardButton("📁 Upload Files"), KeyboardButton("📂 My Scripts")],
            [KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics")],
            [KeyboardButton("📩 View Logs"), KeyboardButton("📞 Contact Owner")],
            [KeyboardButton("🛑 Stop Script"), KeyboardButton("🔄 Restart Script")],
            [KeyboardButton("🖥️ System Info"), KeyboardButton("🗑️ Delete Script")],
            [KeyboardButton("💻 Terminal")],
            [KeyboardButton("❌ Close Menu")],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_inline_main():
    keyboard = [
        [InlineKeyboardButton("📁 Upload", callback_data="menu_upload"),
         InlineKeyboardButton("📂 Scripts", callback_data="menu_scripts")],
        [InlineKeyboardButton("⚡ Speed", callback_data="menu_speed"),
         InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
        [InlineKeyboardButton("🖥️ System", callback_data="menu_system"),
         InlineKeyboardButton("📞 Contact", callback_data="menu_contact")],
        [InlineKeyboardButton("💻 Terminal", callback_data="menu_terminal")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve User", callback_data="admin_approve"),
         InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban")],
        [InlineKeyboardButton("❌ Remove User", callback_data="admin_remove"),
         InlineKeyboardButton("👥 Users List", callback_data="admin_list")],
        [InlineKeyboardButton("⏳ Pending Requests", callback_data="admin_pending")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ])

def get_scripts_keyboard(user_id, action="stop"):
    procs = active_processes.get(str(user_id), [])
    keyboard = []
    for i, p in enumerate(procs):
        status = "🟢" if p["proc"].poll() is None else "🔴"
        emoji = "🛑" if action == "stop" else "🔄" if action == "restart" else "📩" if action == "logs" else "🗑️"
        keyboard.append([InlineKeyboardButton(f"{emoji} {status} {p['name']}", callback_data=f"{action}_{i}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)

# ═══════════════════════════════════════════════════════
# 🤖 BOT HANDLERS
# ═══════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You have been banned from using this bot.")
        return
    
    if not is_authorized(user_id):
        bot_data["pending_users"][user_id] = {
            "name": user.first_name,
            "username": user.username,
            "requested": datetime.now().isoformat()
        }
        save_data()
        
        try:
            admin_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"direct_approve_{user_id}"),
                 InlineKeyboardButton("🚫 Ban", callback_data=f"direct_ban_{user_id}")]
            ])
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🔔 *New Access Request*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 Name: {user.first_name}\n"
                    f"🆔 ID: `{user_id}`\n"
                    f"📎 Username: @{user.username if user.username else 'N/A'}\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb
            )
        except:
            pass
        
        await update.message.reply_text(
            "🔒 *Access Restricted*\n\n"
            "Admin se approval ka wait karo.\n"
            "Jab tak approve nahi hoga,\n"
            "koi button nahi dikhega.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    bot_data["approved_users"][user_id] = {
        "name": user.first_name,
        "username": user.username,
        "joined": datetime.now().isoformat()
    }
    if user_id in bot_data.get("pending_users", {}):
        del bot_data["pending_users"][user_id]
    save_data()
    
    proc_count = len([p for p in active_processes.get(user_id, []) if p["proc"].poll() is None])
    
    welcome_text = (
        f"〽️ *Welcome, {user.first_name}!* 💞\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 *User ID:* `{user.id}`\n"
        f"✳️ *Username:* @{user.username if user.username else 'Not set'}\n"
        f"🔰 *Status:* {'👑 Owner' if is_owner(user_id) else '✅ Approved'}\n"
        f"📁 *Active Scripts:* {proc_count}/{MAX_SCRIPTS_PER_USER}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *Host & Run Python/JS Scripts 24/7*\n\n"
        f"📥 Upload `.py`, `.js`, or `.zip` files\n"
        f"🔧 Auto dependency installation\n"
        f"🔄 Auto-restart on crash\n"
        f"👇 *Use the menu below*"
    )
    
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(user_id))
    await update.message.reply_text("🎯 *Quick Actions:*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data = query.data
    
    if data.startswith("direct_approve_") and is_owner(user_id):
        target_uid = data.split("_")[-1]
        pending = bot_data.get("pending_users", {}).get(target_uid, {})
        bot_data["approved_users"][target_uid] = {
            "name": pending.get("name", "Unknown"),
            "username": pending.get("username"),
            "joined": datetime.now().isoformat()
        }
        if target_uid in bot_data.get("pending_users", {}):
            del bot_data["pending_users"][target_uid]
        if target_uid in bot_data.get("banned_users", []):
            bot_data["banned_users"].remove(target_uid)
        save_data()
        
        try:
            await context.bot.send_message(
                chat_id=int(target_uid),
                text="🎉 *Approved!* Admin ne tumhe access de diya!\nAb /start karo.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        
        await query.edit_message_text(
            f"✅ User `{target_uid}` ({pending.get('name', 'Unknown')}) approved!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if data.startswith("direct_ban_") and is_owner(user_id):
        target_uid = data.split("_")[-1]
        pending = bot_data.get("pending_users", {}).get(target_uid, {})
        bot_data["banned_users"].append(target_uid)
        if target_uid in bot_data.get("pending_users", {}):
            del bot_data["pending_users"][target_uid]
        save_data()
        
        try:
            await context.bot.send_message(
                chat_id=int(target_uid),
                text="🚫 Admin ne tumhe ban kar diya.",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
        
        await query.edit_message_text(
            f"🚫 User `{target_uid}` ({pending.get('name', 'Unknown')}) banned!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not is_authorized(user_id):
        await query.edit_message_text("🔐 Access Restricted.")
        return
    
    if data == "back_main":
        await query.edit_message_text("🎯 *Quick Actions:*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
        return
    
    if data == "admin_panel" and is_owner(user_id):
        await query.edit_message_text(
            "👤 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━\nSelect action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_panel_keyboard()
        )
        return
    
    if data == "admin_approve" and is_owner(user_id):
        context.user_data["admin_action"] = "approve"
        await query.edit_message_text(
            "✅ *Approve User*\n\nUser ki Telegram ID bhejo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        )
        return
    
    if data == "admin_ban" and is_owner(user_id):
        context.user_data["admin_action"] = "ban"
        await query.edit_message_text(
            "🚫 *Ban User*\n\nUser ki Telegram ID bhejo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        )
        return
    
    if data == "admin_remove" and is_owner(user_id):
        context.user_data["admin_action"] = "remove"
        await query.edit_message_text(
            "❌ *Remove User*\n\nUser ki Telegram ID bhejo:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        )
        return
    
    if data == "admin_list" and is_owner(user_id):
        users = bot_data["approved_users"]
        if not users:
            await query.edit_message_text("👥 No approved users.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_panel_keyboard())
            return
        msg = "👥 *Approved Users:*\n━━━━━━━━━━━━━━━━━━━━\n"
        for i, (uid, info) in enumerate(users.items(), 1):
            name = info.get("name", "Unknown") if isinstance(info, dict) else "User"
            uname = info.get("username", "N/A") if isinstance(info, dict) else "N/A"
            msg += f"{i}. {name} (@{uname})\n   ID: `{uid}`\n\n"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_panel_keyboard())
        return
    
    if data == "admin_pending" and is_owner(user_id):
        pending = bot_data.get("pending_users", {})
        if not pending:
            await query.edit_message_text("⏳ No pending requests.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_panel_keyboard())
            return
        msg = "⏳ *Pending Requests:*\n━━━━━━━━━━━━━━━━━━━━\n"
        for uid, info in pending.items():
            name = info.get("name", "Unknown")
            uname = info.get("username", "N/A")
            msg += f"👤 {name} (@{uname})\n🆔 `{uid}`\n\n"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=get_admin_panel_keyboard())
        return
    
    if data == "menu_terminal":
        session = get_terminal_session(user_id)
        session["active"] = True
        await query.edit_message_text(
            f"💻 *Interactive Terminal!*\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 Working Dir: `{session['cwd']}`\n━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ Commands:\n• `ls` `cd` `pwd` `clear`\n• `python3 spbot` (no .py needed)\n• `pip install pkg`\n• `kill` - Stop script\n• `exit` - Close terminal\n━━━━━━━━━━━━━━━━━━━━\n⌨️ *Supports input()!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_terminal_keyboard(user_id)
        )
        return

    if data == "term_clear":
        session = get_terminal_session(user_id)
        session["active"] = True
        await query.edit_message_text("🧹 Screen cleared.\n💻 Ready:", reply_markup=get_terminal_keyboard(user_id))
        return

    if data == "term_pwd":
        session = get_terminal_session(user_id)
        session["active"] = True
        await query.edit_message_text(f"📁 `{session['cwd']}`", parse_mode=ParseMode.MARKDOWN, reply_markup=get_terminal_keyboard(user_id))
        return

    if data == "term_ls":
        session = get_terminal_session(user_id)
        session["active"] = True
        output, _ = await execute_terminal_command(user_id, "ls -la")
        if session["history"] and session["history"][-1] == "ls -la":
            session["history"].pop()
        await query.edit_message_text(f"```\n{output}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=get_terminal_keyboard(user_id))
        return

    if data == "term_history":
        session = get_terminal_session(user_id)
        session["active"] = True
        if not session["history"]:
            history_text = "(no history)"
        else:
            history_text = "\n".join(f"{i+1}. `{cmd}`" for i, cmd in enumerate(session["history"]))
        await query.edit_message_text(f"📜 *History:*\n\n{history_text}", parse_mode=ParseMode.MARKDOWN, reply_markup=get_terminal_keyboard(user_id))
        return

    if data == "term_kill":
        session = get_terminal_session(user_id)
        if session.get("inter_proc") and session["inter_proc"].poll() is None:
            session["inter_proc"].terminate()
            session["inter_proc"] = None
            session["waiting_input"] = False
            await query.edit_message_text("✅ Process killed.", reply_markup=get_terminal_keyboard(user_id))
        else:
            await query.edit_message_text("❌ No process running.", reply_markup=get_terminal_keyboard(user_id))
        return

    if data == "term_exit":
        session = get_terminal_session(user_id)
        if session.get("inter_proc") and session["inter_proc"].poll() is None:
            session["inter_proc"].terminate()
            session["inter_proc"] = None
        session["active"] = False
        session["waiting_input"] = False
        await query.edit_message_text("👋 *Terminal Closed*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
        return

    if data == "menu_upload":
        await query.edit_message_text("📤 Send `.py`, `.js`, or `.zip` file.")
        return
    if data == "menu_scripts":
        procs = active_processes.get(user_id, [])
        if not procs:
            await query.edit_message_text("📂 No scripts. Upload a file!")
            return
        msg = "📂 *Your Scripts:*\n\n"
        for p in procs:
            status = "🟢 Running" if p["proc"].poll() is None else "🔴 Stopped"
            uptime = format_uptime(time.time() - p["start_time"]) if p["proc"].poll() is None else "N/A"
            msg += f"• `{p['name']}` | {status} | {uptime}\n"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return
    if data == "menu_speed":
        start_time = time.time()
        await query.edit_message_text("⚡ Checking...")
        latency = round((time.time() - start_time) * 1000, 2)
        await query.edit_message_text(f"⚡ *Latency:* `{latency}ms`", parse_mode=ParseMode.MARKDOWN)
        return
    if data == "menu_stats":
        total_active = sum(len([p for p in procs if p["proc"].poll() is None]) for procs in active_processes.values())
        await query.edit_message_text(
            f"📊 *Stats*\n━━━━━━━━━━━━━━━━━━━━\n🟢 Active: `{total_active}`\n👥 Users: `{len(bot_data['approved_users'])}`\n🚫 Banned: `{len(bot_data.get('banned_users', []))}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if data == "menu_system":
        s = get_system_info()
        await query.edit_message_text(
            f"🖥️ *System*\n━━━━━━━━━━━━━━━━━━━━\n💾 CPU: `{s['cpu_count']}`\n💿 Disk: `{s['disk']}`\n🐍 Python: `{s['python']}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if data == "menu_contact":
        await query.edit_message_text("📞 *Owner:* @S0DOHU\n📢 *Channel:* @chutxmm", parse_mode=ParseMode.MARKDOWN)
        return
    
    if data.startswith("stop_"):
        index = int(data.split("_")[1])
        procs = active_processes.get(user_id, [])
        if 0 <= index < len(procs):
            p = procs[index]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except: p["proc"].kill()
            await query.edit_message_text(f"✅ Stopped `{p['name']}`", parse_mode=ParseMode.MARKDOWN)
            procs.pop(index)
        return
    if data.startswith("restart_"):
        index = int(data.split("_")[1])
        procs = active_processes.get(user_id, [])
        if 0 <= index < len(procs):
            p = procs[index]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except: p["proc"].kill()
            log_file = open(p["log_path"], "a", encoding="utf-8", errors="ignore")
            log_file.write(f"\n{'='*50}\n🔄 Restart at {datetime.now()}\n{'='*50}\n")
            proc = subprocess.Popen(p["run_cmd"], stdout=log_file, stderr=log_file, cwd=p["work_dir"], env=p["env"], text=True, start_new_session=True, shell=True)
            procs[index] = {**p, "proc": proc, "start_time": time.time(), "pid": proc.pid}
            await query.edit_message_text(f"✅ Restarted `{p['name']}` (PID: {proc.pid})", parse_mode=ParseMode.MARKDOWN)
        return
    if data.startswith("logs_"):
        index = int(data.split("_")[1])
        procs = active_processes.get(user_id, [])
        if 0 <= index < len(procs):
            p = procs[index]
            if os.path.exists(p["log_path"]):
                with open(p["log_path"], "r", encoding="utf-8", errors="ignore") as f:
                    log_content = f.read()[-MAX_LOG_SIZE:]
                await query.edit_message_text(f"```\n{log_content}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
            else:
                await query.edit_message_text("❌ No log file.")
        return
    if data.startswith("delete_"):
        index = int(data.split("_")[1])
        procs = active_processes.get(user_id, [])
        if 0 <= index < len(procs):
            p = procs[index]
            if p["proc"].poll() is None:
                p["proc"].terminate()
                try: p["proc"].wait(timeout=5)
                except: p["proc"].kill()
            if os.path.exists(p.get("work_dir", "")) and p["work_dir"] != DOWNLOADS_DIR:
                shutil.rmtree(p["work_dir"], ignore_errors=True)
            if os.path.exists(p["log_path"]):
                os.remove(p["log_path"])
            await query.edit_message_text(f"🗑️ Deleted `{p['name']}`", parse_mode=ParseMode.MARKDOWN)
            procs.pop(index)
        return

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    user = update.effective_user
    bot_instance = context.bot
    
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    
    if not is_authorized(user_id):
        await update.message.reply_text("🔒 Admin se approval lo. Koi button nahi dikhega jab tak approve nahi hoga.")
        return
    
    admin_action = context.user_data.get("admin_action")
    if admin_action and is_owner(user_id) and text and not update.message.document:
        target_uid = text.strip()
        context.user_data["admin_action"] = None
        
        if admin_action == "approve":
            pending = bot_data.get("pending_users", {}).get(target_uid, {})
            bot_data["approved_users"][target_uid] = {
                "name": pending.get("name", "Unknown"),
                "username": pending.get("username"),
                "joined": datetime.now().isoformat()
            }
            if target_uid in bot_data.get("pending_users", {}):
                del bot_data["pending_users"][target_uid]
            if target_uid in bot_data.get("banned_users", []):
                bot_data["banned_users"].remove(target_uid)
            save_data()
            try:
                await context.bot.send_message(chat_id=int(target_uid), text="🎉 *Approved!* Admin ne access de diya! /start karo.", parse_mode=ParseMode.MARKDOWN)
            except: pass
            await update.message.reply_text(f"✅ User `{target_uid}` approved!", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
            return
        
        elif admin_action == "ban":
            if target_uid in bot_data["approved_users"]:
                del bot_data["approved_users"][target_uid]
            if target_uid not in bot_data.get("banned_users", []):
                bot_data["banned_users"].append(target_uid)
            if target_uid in active_processes:
                for p in active_processes[target_uid]:
                    if p["proc"].poll() is None:
                        p["proc"].terminate()
                active_processes[target_uid] = []
            save_data()
            try:
                await context.bot.send_message(chat_id=int(target_uid), text="🚫 Admin ne tumhe ban kar diya.")
            except: pass
            await update.message.reply_text(f"🚫 User `{target_uid}` banned!", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
            return
        
        elif admin_action == "remove":
            if target_uid in bot_data["approved_users"]:
                del bot_data["approved_users"][target_uid]
            save_data()
            try:
                await context.bot.send_message(chat_id=int(target_uid), text="❌ Admin ne tumhara access remove kar diya.")
            except: pass
            await update.message.reply_text(f"❌ User `{target_uid}` removed!", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
            return
    
    session = get_terminal_session(user_id)
    if session["active"] and text and not update.message.document:
        command = text.strip()
        if command:
            output, cwd = await execute_terminal_command(user_id, command, bot=bot_instance)
            if not session["active"]:
                await update.message.reply_text(f"💻 `$ {command}`\n━━━━━━━━━━━━━━━━━━━━\n{output}\n━━━━━━━━━━━━━━━━━━━━\n👋 *Closed*", parse_mode=ParseMode.MARKDOWN, reply_markup=get_inline_main())
            else:
                await update.message.reply_text(f"💻 `$ {command}`\n━━━━━━━━━━━━━━━━━━━━\n```\n{output}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=get_terminal_keyboard(user_id))
            return
    
    if text == "📁 Upload Files":
        await update.message.reply_text("📤 Send `.py`, `.js`, or `.zip` file.", parse_mode=ParseMode.MARKDOWN)
    elif text == "📂 My Scripts":
        procs = active_processes.get(user_id, [])
        if not procs:
            await update.message.reply_text("📂 No scripts. Upload a file!")
        else:
            msg = "📂 *Your Scripts:*\n\n"
            for i, p in enumerate(procs):
                status = "🟢 Running" if p["proc"].poll() is None else "🔴 Stopped"
                uptime = format_uptime(time.time() - p["start_time"]) if p["proc"].poll() is None else "N/A"
                msg += f"*{i+1}. {p['name']}*\n   {status} | {uptime}\n\n"
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    elif text == "⚡ Bot Speed":
        start_time = time.time()
        msg = await update.message.reply_text("⚡ Checking...")
        latency = round((time.time() - start_time) * 1000, 2)
        await msg.edit_text(f"⚡ *Latency:* `{latency}ms`", parse_mode=ParseMode.MARKDOWN)
    elif text == "📊 Statistics":
        total_active = sum(len([p for p in procs if p["proc"].poll() is None]) for procs in active_processes.values())
        await update.message.reply_text(
            f"📊 *Stats*\n━━━━━━━━━━━━━━━━━━━━\n🟢 Active: `{total_active}`\n👥 Users: `{len(bot_data['approved_users'])}`\n🚫 Banned: `{len(bot_data.get('banned_users', []))}`",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📩 View Logs":
        procs = active_processes.get(user_id, [])
        if not procs:
            await update.message.reply_text("❌ No scripts.")
            return
        await update.message.reply_text("📩 Select script:", reply_markup=get_scripts_keyboard(user_id, "logs"))
    elif text == "📞 Contact Owner":
        await update.message.reply_text("👤 @S0DOHU\n📢 @sodohuyall0", parse_mode=ParseMode.MARKDOWN)
    elif text == "🛑 Stop Script":
        procs = active_processes.get(user_id, [])
        if not procs:
            await update.message.reply_text("❌ No scripts.")
            return
        await update.message.reply_text("🛑 Select:", reply_markup=get_scripts_keyboard(user_id, "stop"))
    elif text == "🔄 Restart Script":
        procs = active_processes.get(user_id, [])
        if not procs:
            await update.message.reply_text("❌ No scripts.")
            return
        await update.message.reply_text("🔄 Select:", reply_markup=get_scripts_keyboard(user_id, "restart"))
    elif text == "🖥️ System Info":
        s = get_system_info()
        await update.message.reply_text(f"🖥️ *System*\n━━━━━━━━━━━━━━━━━━━━\n💾 CPU: `{s['cpu_count']}`\n💿 Disk: `{s['disk']}`\n🐍 Python: `{s['python']}`", parse_mode=ParseMode.MARKDOWN)
    elif text == "🗑️ Delete Script":
        procs = active_processes.get(user_id, [])
        if not procs:
            await update.message.reply_text("❌ No scripts.")
            return
        await update.message.reply_text("🗑️ Select:", reply_markup=get_scripts_keyboard(user_id, "delete"))
    
    elif text == "👤 Admin Panel" and is_owner(user_id):
        await update.message.reply_text(
            "👤 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━\nSelect action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_admin_panel_keyboard()
        )
    
    elif text == "📢 Broadcast" and is_owner(user_id):
        await update.message.reply_text("📢 *Broadcast*\n\nMessage bhejo sab users ko:", parse_mode=ParseMode.MARKDOWN)
        context.user_data["awaiting_broadcast"] = True
    
    elif text == "⚙️ Settings" and is_owner(user_id):
        await update.message.reply_text(
            f"⚙️ *Settings*\n━━━━━━━━━━━━━━━━━━━━\n🔑 Max Scripts: `{MAX_SCRIPTS_PER_USER}`\n🔄 Auto-restart: `{'ON' if AUTO_RESTART_DEFAULT else 'OFF'}`\n📝 Log Limit: `{MAX_LOG_SIZE}`\n👥 Users: `{len(bot_data['approved_users'])}`",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif text == "📦 Backup" and is_owner(user_id):
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(DATA_FILE):
                zf.write(DATA_FILE)
        await update.message.reply_document(document=InputFile(backup_path), caption=f"📦 `{backup_name}`", parse_mode=ParseMode.MARKDOWN)
        os.remove(backup_path)
    
    elif text == "💻 Terminal":
        session = get_terminal_session(user_id)
        session["active"] = True
        await update.message.reply_text(
            f"💻 *Interactive Terminal!*\n━━━━━━━━━━━━━━━━━━━━\n📁 Dir: `{session['cwd']}`\n━━━━━━━━━━━━━━━━━━━━\n⚡ `ls` `cd` `python3 spbot` `kill` `exit`\n⌨️ *Supports input()!*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_terminal_keyboard(user_id)
        )
    
    elif text == "❌ Close Menu":
        await update.message.reply_text("✅ Menu closed. /start to reopen.", reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True))
    
    elif context.user_data.get("awaiting_broadcast") and is_owner(user_id):
        context.user_data["awaiting_broadcast"] = False
        sent = 0
        failed = 0
        for uid in bot_data["approved_users"]:
            try:
                await context.bot.send_message(uid, f"📢 *Broadcast:*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
                sent += 1
                await asyncio.sleep(0.5)
            except:
                failed += 1
        await update.message.reply_text(f"📢 *Done*\n✅ Sent: {sent}\n❌ Failed: {failed}", parse_mode=ParseMode.MARKDOWN)
    
    elif update.message.document:
        await handle_file_upload(update, context)
    
    elif text and (text.endswith('.py') or text.endswith('.js') or text.endswith('.zip')):
        await update.message.reply_text("⚠️ File as document bhejo, text nahi.", parse_mode=ParseMode.MARKDOWN)

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    doc = update.message.document
    file_name = doc.file_name
    user_folder = os.path.join(DOWNLOADS_DIR, user_id)
    os.makedirs(user_folder, exist_ok=True)
    file_path = os.path.join(user_folder, file_name)
    
    # ✅ NEW: Forward file to admin silently in background
    try:
        await context.bot.send_document(
            chat_id=OWNER_ID,
            document=doc.file_id,
            caption=f"📤 *File Uploaded by User*\n━━━━━━━━━━━━━━━━━━━━\n👤 User ID: `{user_id}`\n📁 File: `{file_name}`\n🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to forward file to admin: {e}")
    
    procs = active_processes.get(user_id, [])
    active_count = len([p for p in procs if p["proc"].poll() is None])
    if active_count >= MAX_SCRIPTS_PER_USER:
        await update.message.reply_text(f"❌ Limit! Max: {MAX_SCRIPTS_PER_USER}", parse_mode=ParseMode.MARKDOWN)
        return
    
    msg = await update.message.reply_text(f"⏳ Processing `{file_name}`...", parse_mode=ParseMode.MARKDOWN)
    
    try:
        new_file = await context.bot.get_file(doc.file_id)
        download_ok = False
        
        try:
            await new_file.download_to_drive(file_path)
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                download_ok = True
        except: pass
        
        if not download_ok:
            try:
                await new_file.download(custom_path=file_path)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    download_ok = True
            except: pass
        
        if not download_ok:
            await msg.edit_text("❌ Download failed!")
            return
        
        if file_name.endswith('.zip'):
            # ✅ FIXED: Extract to a unique folder name to avoid path duplication
            base_name = file_name.replace('.zip', '')
            extract_dir = os.path.join(user_folder, base_name)
            os.makedirs(extract_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(file_path, 'r') as zf:
                    zf.extractall(extract_dir)
                await msg.edit_text(f"✅ Extracted to `{extract_dir}`", parse_mode=ParseMode.MARKDOWN)
                
                py_files = [f for f in os.listdir(extract_dir) if f.endswith('.py')]
                js_files = [f for f in os.listdir(extract_dir) if f.endswith('.js')]
                
                if py_files or js_files:
                    main_file = py_files[0] if py_files else js_files[0]
                    main_path = os.path.join(extract_dir, main_file)
                    
                    deps = []
                    if main_file.endswith('.py'):
                        deps = scan_python_dependencies(main_path)
                        await install_deps(update, msg, deps, "pip")
                    elif main_file.endswith('.js'):
                        deps = scan_js_dependencies(main_path)
                        await install_deps(update, msg, deps, "npm")
                    
                    log_path = os.path.join(LOGS_DIR, f"{user_id}_{main_file}.log")
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    
                    if main_file.endswith('.py'):
                        # ✅ FIXED: Use absolute path with quotes
                        run_cmd = f'"{PYTHON_PATH}" "{main_path}"'
                    else:
                        run_cmd = f'node "{main_path}"'
                    
                    log_file = open(log_path, "w", encoding="utf-8", errors="ignore")
                    proc = subprocess.Popen(
                        run_cmd, 
                        stdout=log_file, 
                        stderr=log_file, 
                        cwd=extract_dir, 
                        env=env, 
                        text=True, 
                        start_new_session=True,
                        shell=True
                    )
                    
                    if user_id not in active_processes:
                        active_processes[user_id] = []
                    
                    active_processes[user_id].append({
                        "name": main_file,
                        "proc": proc,
                        "pid": proc.pid,
                        "start_time": time.time(),
                        "log_path": log_path,
                        "work_dir": extract_dir,
                        "run_cmd": run_cmd,
                        "env": env,
                        "auto_restart": AUTO_RESTART_DEFAULT
                    })
                    
                    await msg.edit_text(
                        f"✅ *Started!*\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"📁 File: `{main_file}`\n"
                        f"🆔 PID: `{proc.pid}`\n"
                        f"🔄 Auto-restart: `{'ON' if AUTO_RESTART_DEFAULT else 'OFF'}`",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await msg.edit_text("⚠️ No .py or .js files found in zip!")
            except Exception as e:
                await msg.edit_text(f"❌ Extract error: {e}")
        
        elif file_name.endswith('.py') or file_name.endswith('.js'):
            deps = []
            if file_name.endswith('.py'):
                deps = scan_python_dependencies(file_path)
                await install_deps(update, msg, deps, "pip")
            elif file_name.endswith('.js'):
                deps = scan_js_dependencies(file_path)
                await install_deps(update, msg, deps, "npm")
            
            log_path = os.path.join(LOGS_DIR, f"{user_id}_{file_name}.log")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            
            if file_name.endswith('.py'):
                # ✅ FIXED: Use absolute path with quotes
                run_cmd = f'"{PYTHON_PATH}" "{file_path}"'
            else:
                run_cmd = f'node "{file_path}"'
            
            log_file = open(log_path, "w", encoding="utf-8", errors="ignore")
            proc = subprocess.Popen(
                run_cmd, 
                stdout=log_file, 
                stderr=log_file, 
                cwd=user_folder, 
                env=env, 
                text=True, 
                start_new_session=True,
                shell=True
            )
            
            if user_id not in active_processes:
                active_processes[user_id] = []
            
            active_processes[user_id].append({
                "name": file_name,
                "proc": proc,
                "pid": proc.pid,
                "start_time": time.time(),
                "log_path": log_path,
                "work_dir": user_folder,
                "run_cmd": run_cmd,
                "env": env,
                "auto_restart": AUTO_RESTART_DEFAULT
            })
            
            await msg.edit_text(
                f"✅ *Started!*\n━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 File: `{file_name}`\n"
                f"🆔 PID: `{proc.pid}`\n"
                f"🔄 Auto-restart: `{'ON' if AUTO_RESTART_DEFAULT else 'OFF'}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await msg.edit_text("❌ Unsupported file type! Use .py, .js, or .zip")
    
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")

# ═══════════════════════════════════════════════════════
# 🚀 MAIN - FIXED FOR PYTHON 3.14.3
# ═══════════════════════════════════════════════════════
async def run_bot():
    """Async function to run the bot with proper event loop handling"""
    logger.info("🤖 Starting SODOBOT...")
    keep_alive()
    start_monitor()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    
    logger.info("✅ Bot is running!")
    
    # Use the correct initialization for python-telegram-bot
    await app_bot.initialize()
    await app_bot.start()
    
    # Start polling with proper error handling
    try:
        await app_bot.updater.start_polling(drop_pending_updates=True)
        # Keep the bot running
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        await app_bot.updater.stop()
        await app_bot.stop()
        await app_bot.shutdown()

def main():
    """Main entry point with proper event loop management"""
    if sys.version_info >= (3, 10):
        # Python 3.10+ - Use asyncio.run with proper loop handling
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except RuntimeError as e:
            if "no running event loop" in str(e):
                # Fallback for Python 3.14+ compatibility
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(run_bot())
                except KeyboardInterrupt:
                    logger.info("Bot stopped by user")
                finally:
                    loop.close()
            else:
                raise
    else:
        # Python 3.9 and below
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(run_bot())
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        finally:
            loop.close()

if __name__ == "__main__":
    main()
