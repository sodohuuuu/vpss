"""Telegram VPS Manager Bot.

This program converts the original Discord VPS bot to Telegram while retaining
Docker-backed VPS creation, tmate SSH access, user limits, administration,
SQLite storage, logging, and periodic container-status synchronization.

Run this only on the Docker host. The process needs permission to control the
local Docker daemon. Telegram bot commands that expose VPS data are restricted
to private chats because Telegram group replies are normally visible to others.
"""

from __future__ import annotations

# =============================================================================
# EDIT ONLY THESE SETTINGS BEFORE RUNNING: python3 bot.py
# =============================================================================
BOT_TOKEN = "8737125210:AAHoYa6feASjr1pa6CiAc8GrCWT0VaQdihY"
ADMIN_ID = 8502412097 # Replace 0 with your numeric Telegram user ID from @userinfobot.

BOT_STATUS_NAME = "UnixNodes"
WATERMARK = "Powered by UnixNodes VPS Bot"
DEFAULT_RAM = "2g"
DEFAULT_CPU = "1"
DEFAULT_DISK = "10G"
VPS_HOSTNAME = "unix-free"
# Set this above 0 only if you intentionally want free VPS instances before credits are required.
SERVER_LIMIT = 0
TOTAL_SERVER_LIMIT = 50
REFERRAL_CREDIT_REWARD = 2
VPS_CREDIT_COST = 2
DATABASE_FILE = "vps_bot.db"
STATUS_SYNC_SECONDS = 300

# This block installs the two required Python packages automatically when absent.
import importlib.util
import os
import subprocess
import sys


def ensure_python_dependencies() -> None:
    required = {
        "telegram": "python-telegram-bot[job-queue]>=22.0,<23.0",
        "docker": "docker>=7.0,<8.0",
    }
    missing = [package for module, package in required.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return
    print("Installing required Python packages. Please wait...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", *missing])
    except subprocess.CalledProcessError as exc:
        print(f"Could not install packages automatically: {exc}")
        print("Run: python3 -m pip install python-telegram-bot[job-queue] docker")
        raise SystemExit(1) from exc
    os.execv(sys.executable, [sys.executable, *sys.argv])


ensure_python_dependencies()

import asyncio
import html
import logging
import random
import re
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Iterable, Optional

import docker
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Configuration aliases
# ---------------------------------------------------------------------------
TOKEN = BOT_TOKEN.strip()

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[logging.FileHandler("vps_bot.log"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("telegram_vps_bot")

# Creation may take a few minutes. The lock prevents concurrent requests from
# bypassing per-user/global limits in a single bot process.
creation_lock = asyncio.Lock()
_docker_client: Optional[docker.DockerClient] = None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Create the database and preserve compatibility with the source schema."""
    with sqlite3.connect(DATABASE_FILE, timeout=30) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS vps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                container_id TEXT UNIQUE NOT NULL,
                container_name TEXT NOT NULL,
                os_type TEXT NOT NULL,
                hostname TEXT NOT NULL,
                status TEXT DEFAULT 'stopped',
                ssh_command TEXT,
                ram TEXT DEFAULT '2g',
                cpu TEXT DEFAULT '1',
                disk TEXT DEFAULT '10G',
                suspended INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        columns = {column[1] for column in cursor.execute("PRAGMA table_info(vps)")}
        if "suspended" not in columns:
            cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS credits (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                credited INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                credited_at TIMESTAMP,
                FOREIGN KEY (referred_id) REFERENCES users (user_id),
                FOREIGN KEY (referrer_id) REFERENCES users (user_id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS force_channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                invite_link TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def user_exists(user_id: int) -> bool:
    with get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None


def add_user(user_id: int, username: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )
        conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.execute("INSERT OR IGNORE INTO credits (user_id, balance) VALUES (?, 0)", (user_id,))


def get_credit_balance(user_id: int) -> int:
    with get_db_connection() as conn:
        row = conn.execute("SELECT balance FROM credits WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["balance"]) if row else 0


def add_credits(user_id: int, amount: int) -> None:
    if amount <= 0:
        return
    with get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO credits (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE credits SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def refund_credits(user_id: int, amount: int) -> None:
    add_credits(user_id, amount)


def register_referral(referred_id: int, referrer_id: int) -> bool:
    """Record a first-start referral; it is rewarded only after membership is verified."""
    if referred_id == referrer_id or not user_exists(referrer_id):
        return False
    with get_db_connection() as conn:
        result = conn.execute(
            "INSERT OR IGNORE INTO referrals (referred_id, referrer_id) VALUES (?, ?)",
            (referred_id, referrer_id),
        )
        return result.rowcount == 1


def award_pending_referral(referred_id: int) -> Optional[int]:
    """Atomically award one referral after the invited user clears force join."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ? AND credited = 0", (referred_id,)
        ).fetchone()
        if not row:
            return None
        referrer_id = int(row["referrer_id"])
        changed = conn.execute(
            "UPDATE referrals SET credited = 1, credited_at = CURRENT_TIMESTAMP "
            "WHERE referred_id = ? AND credited = 0",
            (referred_id,),
        ).rowcount
        if not changed:
            return None
        conn.execute("INSERT OR IGNORE INTO credits (user_id, balance) VALUES (?, 0)", (referrer_id,))
        conn.execute(
            "UPDATE credits SET balance = balance + ? WHERE user_id = ?",
            (REFERRAL_CREDIT_REWARD, referrer_id),
        )
        return referrer_id


def reserve_vps_credit(user_id: int) -> tuple[bool, int]:
    """Return whether the user may create a VPS and the refundable credit charge."""
    with get_db_connection() as conn:
        vps_count = conn.execute("SELECT COUNT(*) FROM vps WHERE user_id = ?", (user_id,)).fetchone()[0]
        if vps_count < SERVER_LIMIT:
            return True, 0
        conn.execute("INSERT OR IGNORE INTO credits (user_id, balance) VALUES (?, 0)", (user_id,))
        result = conn.execute(
            "UPDATE credits SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (VPS_CREDIT_COST, user_id, VPS_CREDIT_COST),
        )
        return result.rowcount == 1, VPS_CREDIT_COST if result.rowcount == 1 else 0


def list_force_channels() -> list[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute("SELECT * FROM force_channels ORDER BY added_at ASC").fetchall()


def add_force_channel(chat_id: str, title: str, username: Optional[str], invite_link: Optional[str]) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO force_channels (chat_id, title, username, invite_link) VALUES (?, ?, ?, ?)",
            (str(chat_id), title, username, invite_link),
        )


def remove_force_channel(chat_id: str) -> bool:
    with get_db_connection() as conn:
        return conn.execute("DELETE FROM force_channels WHERE chat_id = ?", (str(chat_id),)).rowcount > 0


def add_ban(user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO bans (user_id) VALUES (?)", (user_id,))


def remove_ban(user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))


def is_banned(user_id: int) -> bool:
    with get_db_connection() as conn:
        return conn.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,)).fetchone() is not None


def add_vps(
    user_id: int,
    container_id: str,
    container_name: str,
    os_type: str,
    hostname: str,
    ssh_command: str,
    ram: str = DEFAULT_RAM,
    cpu: str = DEFAULT_CPU,
    disk: str = DEFAULT_DISK,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO vps (
                user_id, container_id, container_name, os_type, hostname,
                status, ssh_command, ram, cpu, disk, suspended
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0)
            """,
            (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk),
        )


def get_user_vps(user_id: int) -> list[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()


def count_user_vps(user_id: int) -> int:
    return len(get_user_vps(user_id))


def get_vps_by_identifier(user_id: int, identifier: Optional[str]) -> Optional[sqlite3.Row]:
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    needle = identifier.lower()
    for vps in vps_list:
        if (
            needle == str(vps["id"])
            or needle in vps["container_id"].lower()
            or needle in vps["container_name"].lower()
        ):
            return vps
    return None


def update_vps_status(container_id: str, status: str) -> None:
    with get_db_connection() as conn:
        conn.execute("UPDATE vps SET status = ? WHERE container_id = ?", (status, container_id))


def update_vps_ssh(container_id: str, ssh_command: str) -> None:
    with get_db_connection() as conn:
        conn.execute("UPDATE vps SET ssh_command = ? WHERE container_id = ?", (ssh_command, container_id))


def update_vps_suspended(container_id: str, suspended: int) -> None:
    with get_db_connection() as conn:
        conn.execute("UPDATE vps SET suspended = ? WHERE container_id = ?", (suspended, container_id))


def delete_vps(container_id: str) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM vps WHERE container_id = ?", (container_id,))


def get_total_instances() -> int:
    with get_db_connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM vps WHERE status = "running"').fetchone()[0]


def get_all_vps() -> list[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT u.user_id, u.username, v.container_id, v.container_name, v.os_type,
                   v.hostname, v.status, v.ram, v.cpu, v.disk, v.suspended, v.created_at
            FROM vps v JOIN users u ON v.user_id = u.user_id
            ORDER BY v.created_at DESC
            """
        ).fetchall()


def get_users_overview() -> list[sqlite3.Row]:
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT u.user_id, u.username, COUNT(v.id) AS total_vps,
                   SUM(CASE WHEN v.status = 'running' THEN 1 ELSE 0 END) AS running_vps
            FROM users u LEFT JOIN vps v ON u.user_id = v.user_id
            GROUP BY u.user_id, u.username
            ORDER BY total_vps DESC
            """
        ).fetchall()


def get_bot_stats() -> dict[str, float | int]:
    with get_db_connection() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_vps = conn.execute("SELECT COUNT(*) FROM vps").fetchone()[0]
        total_running = conn.execute('SELECT COUNT(*) FROM vps WHERE status = "running"').fetchone()[0]
        total_banned = conn.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
        total_credits = conn.execute("SELECT COALESCE(SUM(balance), 0) FROM credits").fetchone()[0]
        total_channels = conn.execute("SELECT COUNT(*) FROM force_channels").fetchone()[0]
        resource_rows = conn.execute('SELECT ram, cpu, disk FROM vps WHERE status = "running"').fetchall()
    return {
        "users": total_users,
        "vps": total_vps,
        "running": total_running,
        "banned": total_banned,
        "credits": total_credits,
        "channels": total_channels,
        "cpu": sum(float(row["cpu"]) for row in resource_rows),
        "ram": sum(parse_gb(row["ram"]) for row in resource_rows),
        "disk": sum(parse_gb(row["disk"]) for row in resource_rows),
    }


# ---------------------------------------------------------------------------
# Docker and operating-system helpers
# ---------------------------------------------------------------------------
def get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


def parse_gb(resource_str: str) -> float:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mMgG])?\s*", str(resource_str))
    if not match:
        return 0.0
    value = float(match.group(1))
    return value / 1024.0 if (match.group(2) or "g").lower() == "m" else value


def validate_resource_format(ram: str, cpu: str, disk: str) -> Optional[str]:
    if parse_gb(ram) <= 0:
        return "RAM must be a positive value such as <code>2g</code> or <code>512m</code>."
    try:
        if float(cpu) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return "CPU must be a positive number, such as <code>1</code> or <code>0.5</code>."
    if parse_gb(disk) <= 0:
        return "Disk must be a positive value such as <code>10G</code>."
    return None


def get_uptime(container_id: str) -> str:
    try:
        output = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT
        ).decode().strip()
        if output == "<no value>" or output.startswith("0001-01-01"):
            return "Not running"
        start_time = datetime.fromisoformat(output.replace("Z", "+00:00"))
        uptime = datetime.now(timezone.utc) - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as exc:
        logger.warning("Could not obtain uptime for %s: %s", container_id, exc)
        return "Unknown"


def get_stats(container_id: str) -> dict[str, str]:
    try:
        output = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}", container_id],
            stderr=subprocess.STDOUT,
        ).decode().strip()
        parts = output.split("\t")
        if len(parts) == 3:
            return {"cpu": parts[0], "mem": parts[1], "net": parts[2]}
    except Exception as exc:
        logger.warning("Could not obtain stats for %s: %s", container_id, exc)
    return {"cpu": "N/A", "mem": "N/A", "net": "N/A"}


def get_logs(container_id: str, lines: int = 50) -> str:
    lines = max(1, min(lines, 500))
    try:
        output = subprocess.check_output(
            ["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT
        ).decode(errors="replace")
        return output[-3200:] or "No log output."
    except Exception as exc:
        logger.warning("Could not obtain logs for %s: %s", container_id, exc)
        return "Failed to fetch logs."


def get_container_status(container_id: str) -> Optional[str]:
    try:
        return subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Status}}", container_id], stderr=subprocess.STDOUT
        ).decode().strip()
    except subprocess.CalledProcessError:
        return None


async def async_docker_run(image: str, hostname: str, ram: str, cpu: str, container_name: str) -> Optional[str]:
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",  # Kept for compatibility with the source bot.
        "--restart", "unless-stopped",
        f"--memory={ram}",
        f"--cpus={cpu}",
        f"--hostname={hostname}",
        f"--name={container_name}",
        image,
        "tail", "-f", "/dev/null",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0:
            logger.error("Docker run failed: %s", stderr.decode(errors="replace").strip())
            return None
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        logger.error("Docker run timed out")
    except Exception as exc:
        logger.exception("Docker run error: %s", exc)
    return None


async def _docker_action(container_id: str, action: str, timeout: float = 30) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", action, container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode != 0:
            logger.warning("Docker %s failed for %s: %s", action, container_id, stderr.decode(errors="replace"))
        return process.returncode == 0
    except asyncio.TimeoutError:
        logger.warning("Docker %s timed out for %s", action, container_id)
    except Exception as exc:
        logger.exception("Docker %s error for %s: %s", action, container_id, exc)
    return False


async def async_docker_start(container_id: str) -> bool:
    return await _docker_action(container_id, "start")


async def async_docker_stop(container_id: str) -> bool:
    success = await _docker_action(container_id, "stop")
    if not success:
        await _docker_action(container_id, "kill", timeout=15)
    return success


async def async_docker_restart(container_id: str) -> bool:
    return await _docker_action(container_id, "restart")


async def async_docker_rm(container_id: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode != 0:
            logger.warning("Docker rm failed for %s: %s", container_id, stderr.decode(errors="replace"))
        return process.returncode == 0
    except Exception as exc:
        logger.exception("Docker rm error for %s: %s", container_id, exc)
        return False


async def async_install_tmate(container_id: str) -> bool:
    install_cmd = "apt-get update && apt-get install -y tmate curl wget sudo openssh-client"
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        if process.returncode != 0:
            logger.warning("tmate install failed for %s: %s", container_id, stderr.decode(errors="replace"))
            return False
        return True
    except asyncio.TimeoutError:
        logger.error("tmate install timed out for %s", container_id)
    except Exception as exc:
        logger.exception("tmate install failed for %s: %s", container_id, exc)
    return False


async def docker_exec_tmate(container_id: str) -> Optional[asyncio.subprocess.Process]:
    try:
        return await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.exception("tmate execution failed for %s: %s", container_id, exc)
        return None


async def capture_ssh_session_line(process: asyncio.subprocess.Process) -> Optional[str]:
    if not process.stdout:
        return None
    try:
        for _ in range(60):
            output = await asyncio.wait_for(process.stdout.readline(), timeout=5)
            if not output:
                break
            line = output.decode("utf-8", errors="replace").strip()
            if "ssh session:" in line.lower():
                return line.split("ssh session:", 1)[-1].strip()
    except asyncio.TimeoutError:
        logger.warning("Timed out waiting for tmate SSH session")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
    return None


# ---------------------------------------------------------------------------
# Telegram presentation, validation, and shared workflows
# ---------------------------------------------------------------------------
def display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown user"
    if user.username:
        return f"@{user.username}"
    return user.full_name or str(user.id)


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and ADMIN_ID and update.effective_user.id == ADMIN_ID)


async def require_private(update: Update) -> bool:
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "For your VPS security, please open a private chat with this bot and run the command there."
        )
    return False


async def require_admin(update: Update) -> bool:
    if is_admin(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("This command is restricted to the configured administrator.")
    return False


def channel_join_url(channel: sqlite3.Row) -> Optional[str]:
    username = (channel["username"] or "").lstrip("@")
    if username:
        return f"https://t.me/{username}"
    return channel["invite_link"] or None


def force_join_markup() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in list_force_channels():
        url = channel_join_url(channel)
        if url:
            rows.append([InlineKeyboardButton(f"Join {channel['title']}", url=url)])
    rows.append([InlineKeyboardButton("I Have Joined", callback_data="force:check")])
    return InlineKeyboardMarkup(rows)


async def has_joined_all_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check every configured force-join channel; the configured admin always bypasses it."""
    user = update.effective_user
    if not user or is_admin(update):
        return True
    channels = list_force_channels()
    if not channels:
        return True
    missing: list[sqlite3.Row] = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel["chat_id"], user_id=user.id)
            if member.status in {"left", "kicked"}:
                missing.append(channel)
        except (BadRequest, Forbidden) as exc:
            logger.warning("Force-join check failed for %s: %s", channel["chat_id"], exc)
            missing.append(channel)
        except TelegramError as exc:
            logger.warning("Unexpected force-join check failure for %s: %s", channel["chat_id"], exc)
            missing.append(channel)
    if not missing:
        return True
    message = update.effective_message
    if message:
        await message.reply_html(
            "<b>Channel join required</b>\n\n"
            "Please join every channel below, then press <b>I Have Joined</b>.\n"
            "The bot must be an administrator in each configured channel to verify membership.",
            reply_markup=force_join_markup(),
        )
    return False


async def notify_referral_reward(context: ContextTypes.DEFAULT_TYPE, referred_id: int) -> None:
    referrer_id = award_pending_referral(referred_id)
    if not referrer_id:
        return
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                f"<b>Referral reward received</b>\n\n"
                f"Your invited user has joined the required channel(s). "
                f"<b>{REFERRAL_CREDIT_REWARD} credits</b> were added to your balance.\n"
                f"<b>{VPS_CREDIT_COST} credits = 1 VPS</b>."
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.info("Could not notify referrer %s: %s", referrer_id, exc)


async def require_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not await require_private(update):
        return False
    user = update.effective_user
    if not user:
        return False
    add_user(user.id, display_name(update))
    return await has_joined_all_channels(update, context)


def user_keyboard(update: Update) -> ReplyKeyboardMarkup:
    rows = [
        ["Create VPS", "My VPS"],
        ["My Credits", "Invite & Earn"],
        ["About", "Help", "Check Bot"],
    ]
    if is_admin(update):
        rows.append(["Admin Panel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder="Choose an option")


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: Optional[str] = None) -> None:
    message = update.effective_message
    if not message:
        return
    balance = get_credit_balance(update.effective_user.id)
    prefix = f"{clean_html(notice)}\n\n" if notice else ""
    await message.reply_html(
        prefix
        + f"<b>{clean_html(BOT_STATUS_NAME)} VPS Manager</b>\n\n"
        + f"Your credits: <b>{balance}</b>\n"
        + f"<b>{VPS_CREDIT_COST} credits = 1 VPS</b>. Invite a verified user to earn "
        + f"<b>{REFERRAL_CREDIT_REWARD} credits</b>.\n\n"
        + "Use the buttons below to continue.",
        reply_markup=user_keyboard(update),
    )


def os_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Ubuntu 22.04", callback_data="deploy:ubuntu")],
            [InlineKeyboardButton("Debian 12", callback_data="deploy:debian")],
            [InlineKeyboardButton("Back", callback_data="menu:home")],
        ]
    )


def vps_keyboard(vps: sqlite3.Row) -> InlineKeyboardMarkup:
    vps_id = str(vps["id"])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Start", callback_data=f"vact:{vps_id}:start"),
                InlineKeyboardButton("Stop", callback_data=f"vact:{vps_id}:stop"),
                InlineKeyboardButton("Restart", callback_data=f"vact:{vps_id}:restart"),
            ],
            [
                InlineKeyboardButton("New SSH", callback_data=f"vact:{vps_id}:ssh"),
                InlineKeyboardButton("View Logs", callback_data=f"vact:{vps_id}:logs"),
            ],
            [
                InlineKeyboardButton("Reinstall Ubuntu", callback_data=f"vact:{vps_id}:ubuntu"),
            ],
            [
                InlineKeyboardButton("Reinstall Debian", callback_data=f"vact:{vps_id}:debian"),
                InlineKeyboardButton("Remove VPS", callback_data=f"vdel:{vps_id}"),
            ],
            [InlineKeyboardButton("My VPS", callback_data="menu:vps"), InlineKeyboardButton("Main Menu", callback_data="menu:home")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Statistics", callback_data="admin:stats"), InlineKeyboardButton("All VPS", callback_data="admin:vps")],
            [InlineKeyboardButton("Users", callback_data="admin:users"), InlineKeyboardButton("Force Channels", callback_data="admin:channels")],
            [InlineKeyboardButton("Add Channel", callback_data="admin:addchannel"), InlineKeyboardButton("Remove Channel", callback_data="admin:removechannel")],
            [InlineKeyboardButton("User Management", callback_data="admin:tools"), InlineKeyboardButton("User VPS Actions", callback_data="admin:useraction")],
            [InlineKeyboardButton("Stop All VPS", callback_data="admin:killall")],
            [InlineKeyboardButton("Close", callback_data="menu:home")],
        ]
    )


async def show_vps_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    vps_list = get_user_vps(update.effective_user.id)
    if not vps_list:
        await message.reply_text("You have no VPS instances. Earn credits through referrals, then use Create VPS.")
        return
    rows = [
        [InlineKeyboardButton(f"{vps['container_name']} ({vps['status']})", callback_data=f"vps:{vps['id']}")]
        for vps in vps_list
    ]
    rows.append([InlineKeyboardButton("Main Menu", callback_data="menu:home")])
    await message.reply_html("<b>Your VPS instances</b>\nChoose a VPS to manage it.", reply_markup=InlineKeyboardMarkup(rows))


def clean_html(value: object) -> str:
    return html.escape(str(value))


def chunk_text(value: str, maximum: int = 3400) -> Iterable[str]:
    if len(value) <= maximum:
        yield value
        return
    buffer = ""
    for line in value.splitlines(keepends=True):
        if len(buffer) + len(line) > maximum and buffer:
            yield buffer
            buffer = ""
        while len(line) > maximum:
            yield line[:maximum]
            line = line[maximum:]
        buffer += line
    if buffer:
        yield buffer


async def send_code_chunks(message, content: str, title: Optional[str] = None) -> None:
    if title:
        await message.reply_html(f"<b>{clean_html(title)}</b>")
    for chunk in chunk_text(content):
        await message.reply_html(f"<pre>{clean_html(chunk)}</pre>")


def normalize_os(value: str) -> Optional[str]:
    value = value.lower().strip()
    return value if value in {"ubuntu", "debian"} else None


def usage(command: str) -> str:
    return f"Usage: <code>{command}</code>"


def format_vps_details(vps: sqlite3.Row, stats: dict[str, str], uptime: str) -> str:
    os_name = "Ubuntu 22.04" if vps["os_type"] == "ubuntu" else "Debian 12"
    ssh_line = vps["ssh_command"] or "Not generated"
    if len(ssh_line) > 120:
        ssh_line = ssh_line[:117] + "..."
    return (
        f"<b>VPS Details: {clean_html(vps['container_name'])}</b>\n\n"
        f"<b>OS:</b> {os_name}\n"
        f"<b>Hostname:</b> {clean_html(vps['hostname'])}\n"
        f"<b>Status:</b> {clean_html(vps['status'])}\n"
        f"<b>Suspended:</b> {'Yes' if vps['suspended'] else 'No'}\n"
        f"<b>Container ID:</b> <code>{clean_html(vps['container_id'])}</code>\n"
        f"<b>Resources:</b> {clean_html(vps['ram'])} RAM | {clean_html(vps['cpu'])} CPU | {clean_html(vps['disk'])} Disk\n"
        f"<b>Current Usage:</b> CPU {clean_html(stats['cpu'])} | Memory {clean_html(stats['mem'])}\n"
        f"<b>Uptime:</b> {clean_html(uptime)}\n"
        f"<b>Network I/O:</b> {clean_html(stats['net'])}\n"
        f"<b>Created:</b> {clean_html(vps['created_at'])}\n"
        f"<b>Saved SSH:</b> <code>{clean_html(ssh_line)}</code>\n\n"
        f"<i>{clean_html(WATERMARK)}</i>"
    )


async def regenerate_ssh(
    bot,
    user_id: int,
    vps_identifier: Optional[str],
) -> tuple[bool, str]:
    """Create a fresh tmate session and deliver it only to the owner chat."""
    vps = get_vps_by_identifier(user_id, vps_identifier)
    if not vps:
        return False, "No VPS was found."
    if vps["status"] != "running":
        return False, "The VPS must be running before an SSH session can be generated."

    process = await docker_exec_tmate(vps["container_id"])
    if not process:
        return False, "Failed to launch tmate in the VPS."
    ssh_line = await capture_ssh_session_line(process)
    if not ssh_line:
        return False, "tmate did not provide an SSH session."

    update_vps_ssh(vps["container_id"], ssh_line)
    secret = (
        f"<b>New SSH session: {clean_html(vps['container_name'])}</b>\n\n"
        f"<pre>{clean_html(ssh_line)}</pre>\n"
        "This temporary session grants access to your VPS. Do not share it."
    )
    try:
        await bot.send_message(chat_id=user_id, text=secret, parse_mode=ParseMode.HTML, protect_content=True)
    except Forbidden:
        return False, "SSH was generated, but Telegram could not deliver it. The user must start the bot first."
    except TelegramError as exc:
        logger.warning("Could not deliver SSH session to %s: %s", user_id, exc)
        return False, "SSH was generated, but Telegram could not deliver it."
    return True, "A new SSH session has been sent in a protected private message."


async def manage_vps(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    vps_identifier: str,
    action: str,
) -> None:
    message = update.effective_message
    if not message:
        return
    vps = get_vps_by_identifier(owner_id, vps_identifier)
    if not vps:
        await message.reply_text("No matching VPS was found.")
        return
    if action == "start" and vps["suspended"] and owner_id == update.effective_user.id:
        await message.reply_text("This VPS is suspended by an administrator. Please contact support.")
        return

    progress = await message.reply_text(f"{action.title()}ing VPS…")
    if action == "start":
        success = await async_docker_start(vps["container_id"])
        if success:
            update_vps_status(vps["container_id"], "running")
    elif action == "stop":
        success = await async_docker_stop(vps["container_id"])
        if success:
            update_vps_status(vps["container_id"], "stopped")
    else:
        success = await async_docker_restart(vps["container_id"])
        if success:
            update_vps_status(vps["container_id"], "running")

    if not success:
        await progress.edit_text(f"Failed to {action} the VPS. Check Docker and the bot log.")
        return

    os_name = "Ubuntu 22.04" if vps["os_type"] == "ubuntu" else "Debian 12"
    result = f"<b>VPS {action.title()}ed Successfully</b>\nOS: {os_name}"
    if action in {"start", "restart"}:
        ssh_ok, ssh_status = await regenerate_ssh(context.bot, owner_id, vps_identifier)
        result += f"\n\n{clean_html(ssh_status)}"
        if not ssh_ok:
            result += "\nYou can retry with <code>/regen_ssh</code> after resolving the issue."
    await progress.edit_text(result, parse_mode=ParseMode.HTML)


async def create_vps(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    owner_name: str,
    os_type: str,
    ram: str,
    cpu: str,
    disk: str,
    charge_credits: bool = True,
) -> None:
    message = update.effective_message
    if not message:
        return
    error = validate_resource_format(ram, cpu, disk)
    if error:
        await message.reply_html(error)
        return

    async with creation_lock:
        add_user(owner_id, owner_name)
        if is_banned(owner_id):
            await message.reply_text("This user is banned from creating VPS instances.")
            return
        credit_allowed, charged_credits = reserve_vps_credit(owner_id) if charge_credits else (True, 0)
        if not credit_allowed:
            await message.reply_html(
                f"You need <b>{VPS_CREDIT_COST} credits</b> to create one VPS. "
                "Use <b>Invite & Earn</b> to invite a user; a verified referral gives 2 credits."
            )
            return
        if get_total_instances() >= TOTAL_SERVER_LIMIT:
            await message.reply_text(f"The global running-server limit of {TOTAL_SERVER_LIMIT} has been reached.")
            return

        try:
            host_info = await asyncio.to_thread(lambda: get_docker_client().info())
            host_cpus = float(host_info["NCPU"])
            host_mem_gb = float(host_info["MemTotal"]) / (1024 ** 3)
            if float(cpu) > host_cpus:
                await message.reply_text(f"Requested CPU ({cpu}) exceeds the host limit ({host_cpus:g}).")
                return
            if parse_gb(ram) > host_mem_gb:
                await message.reply_text(f"Requested RAM ({ram}) exceeds the host limit ({host_mem_gb:.1f}G).")
                return
        except Exception as exc:
            logger.exception("Resource validation failed: %s", exc)
            refund_credits(owner_id, charged_credits)
            await message.reply_text("Docker resource validation failed. Please contact the administrator.")
            return

        progress = await message.reply_text("Creating your VPS instance. This can take a few minutes…")
        hostname = f"{VPS_HOSTNAME}-{owner_id}"
        suffix = random.randint(1000, 9999)
        container_name = f"{os_type}-vps-{owner_id}-{suffix}"
        image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
        container_id = await async_docker_run(image, hostname, ram, cpu, container_name)
        if not container_id:
            refund_credits(owner_id, charged_credits)
            await progress.edit_text("Failed to create the Docker container. Your credits were refunded. Check the bot log.")
            return

        await progress.edit_text("Container created. Installing the SSH-session utility…")
        installed = await async_install_tmate(container_id)
        if not installed:
            await async_docker_rm(container_id)
            refund_credits(owner_id, charged_credits)
            await progress.edit_text("VPS creation failed while installing the SSH-session utility. The container was removed and your credits were refunded.")
            return

        process = await docker_exec_tmate(container_id)
        ssh_line = await capture_ssh_session_line(process) if process else None
        if not ssh_line:
            await async_docker_rm(container_id)
            refund_credits(owner_id, charged_credits)
            await progress.edit_text("VPS creation failed because an SSH session could not be generated. The container was removed and your credits were refunded.")
            return

        add_vps(owner_id, container_id, container_name, os_type, hostname, ssh_line, ram, cpu, disk)
        os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
        secret = (
            f"<b>VPS Instance Created</b>\n\n"
            f"<b>OS:</b> {os_name}\n"
            f"<b>RAM:</b> {clean_html(ram)} | <b>CPU:</b> {clean_html(cpu)} | <b>Disk:</b> {clean_html(disk)}\n"
            f"<b>Hostname:</b> {clean_html(hostname)}\n\n"
            f"<pre>{clean_html(ssh_line)}</pre>\n"
            "This temporary session grants access to your VPS. Do not share it."
        )
        try:
            await context.bot.send_message(chat_id=owner_id, text=secret, parse_mode=ParseMode.HTML, protect_content=True)
            delivery = "Your VPS is ready. SSH access has been sent in a protected private message."
        except TelegramError as exc:
            logger.warning("Could not deliver new SSH session to %s: %s", owner_id, exc)
            delivery = "Your VPS is ready, but Telegram could not deliver the SSH session. Use /regen_ssh to retry."
        await progress.edit_text(delivery)


async def reinstall_vps(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    vps_identifier: str,
    os_type: str,
) -> None:
    message = update.effective_message
    if not message:
        return
    vps = get_vps_by_identifier(owner_id, vps_identifier)
    if not vps:
        await message.reply_text("No matching VPS was found.")
        return

    progress = await message.reply_text("Reinstalling VPS…")
    old_container = vps["container_id"]
    hostname, ram, cpu, disk = vps["hostname"], vps["ram"], vps["cpu"], vps["disk"]
    await async_docker_stop(old_container)
    await async_docker_rm(old_container)
    delete_vps(old_container)

    suffix = random.randint(1000, 9999)
    new_name = f"{os_type}-vps-{owner_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    new_container = await async_docker_run(image, hostname, ram, cpu, new_name)
    if not new_container:
        await progress.edit_text("Reinstall failed while creating the replacement container. The original VPS was removed.")
        return
    if not await async_install_tmate(new_container):
        await async_docker_rm(new_container)
        await progress.edit_text("Reinstall failed while installing the SSH-session utility. The replacement container was removed.")
        return

    process = await docker_exec_tmate(new_container)
    ssh_line = await capture_ssh_session_line(process) if process else None
    if not ssh_line:
        await async_docker_rm(new_container)
        await progress.edit_text("Reinstall failed because an SSH session could not be generated. The replacement container was removed.")
        return

    add_vps(owner_id, new_container, new_name, os_type, hostname, ssh_line, ram, cpu, disk)
    os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
    secret = (
        f"<b>VPS Reinstalled Successfully</b>\n\n"
        f"<b>OS:</b> {os_name}\n"
        f"<pre>{clean_html(ssh_line)}</pre>\n"
        "This temporary session grants access to your VPS. Do not share it."
    )
    try:
        await context.bot.send_message(chat_id=owner_id, text=secret, parse_mode=ParseMode.HTML, protect_content=True)
        delivery = "VPS has been reinstalled. A new SSH session has been sent privately."
    except TelegramError:
        delivery = "VPS has been reinstalled, but Telegram could not deliver SSH access. Use /regen_ssh to retry."
    await progress.edit_text(delivery)


# ---------------------------------------------------------------------------
# User command handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    user = update.effective_user
    if not user:
        return
    first_start = not user_exists(user.id)
    add_user(user.id, display_name(update))
    if first_start and context.args:
        referrer_id = parse_user_id(context.args[0])
        if referrer_id:
            register_referral(user.id, referrer_id)
    if not await has_joined_all_channels(update, context):
        return
    await notify_referral_reward(context, user.id)
    await show_main_menu(update, context, "Welcome. Your account is ready.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    await update.effective_message.reply_html(
        "<b>How to use the bot</b>\n\n"
        "Use <b>Create VPS</b> to choose Ubuntu or Debian. Use <b>My VPS</b> to select and manage your VPS using buttons. "
        "Use <b>Invite &amp; Earn</b> to get your referral link. Each verified referral earns 2 credits, and 2 credits unlock one additional VPS.\n\n"
        "The <b>Admin Panel</b> button is visible only to the configured administrator."
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    await update.effective_message.reply_html(
        "<b>VPS Manager Bot — About</b>\n\n"
        "A Telegram interface for Docker-backed VPS containers, SSH-session generation, lifecycle controls, logs, and resource monitoring.\n\n"
        "<b>Developer:</b> Hopingboyz\n"
        "<b>Version:</b> 1.0 Telegram conversion\n"
        "<b>Framework:</b> Python with python-telegram-bot\n\n"
        "<a href=\"https://www.youtube.com/@Hopingboyz\">YouTube</a> | "
        "<a href=\"https://github.com/Hopingboyz\">GitHub</a> | "
        "<a href=\"https://instagram.com/hopingboyz\">Instagram</a>"
    )


async def deploy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/deploy &lt;ubuntu|debian&gt;"))
        return
    os_type = normalize_os(context.args[0])
    if not os_type:
        await update.effective_message.reply_html("Supported operating systems: <code>ubuntu</code> and <code>debian</code>.")
        return
    await create_vps(
        update,
        context,
        update.effective_user.id,
        display_name(update),
        os_type,
        DEFAULT_RAM,
        DEFAULT_CPU,
        DEFAULT_DISK,
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    vps_list = get_user_vps(update.effective_user.id)
    if not vps_list:
        await update.effective_message.reply_text("You have no VPS instances.")
        return
    rows = ["Your VPS instances:\n"]
    for vps in vps_list:
        uptime = await asyncio.to_thread(get_uptime, vps["container_id"])
        suspended = " (Suspended)" if vps["suspended"] else ""
        rows.append(
            f"[{vps['status'].upper()}]{suspended} {vps['container_name']} ({vps['os_type']})\n"
            f"ID: {vps['container_id']}\nHostname: {vps['hostname']}\n"
            f"Uptime: {uptime}\nResources: {vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk\n"
        )
    await send_code_chunks(update.effective_message, "\n".join(rows))


async def vps_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    identifier = context.args[0] if context.args else None
    vps = get_vps_by_identifier(update.effective_user.id, identifier)
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found.")
        return
    uptime, stats = await asyncio.gather(
        asyncio.to_thread(get_uptime, vps["container_id"]),
        asyncio.to_thread(get_stats, vps["container_id"]),
    )
    await update.effective_message.reply_html(format_vps_details(vps, stats, uptime))


async def regen_ssh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    identifier = context.args[0] if context.args else None
    status = await update.effective_message.reply_text("Generating a fresh SSH session…")
    success, detail = await regenerate_ssh(context.bot, update.effective_user.id, identifier)
    await status.edit_text(detail)
    if not success:
        logger.info("SSH regeneration failed for Telegram user %s: %s", update.effective_user.id, detail)


async def vps_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/vps_start &lt;vps_id&gt;"))
        return
    await manage_vps(update, context, update.effective_user.id, context.args[0], "start")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/stop &lt;vps_id&gt;"))
        return
    await manage_vps(update, context, update.effective_user.id, context.args[0], "stop")


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/restart &lt;vps_id&gt;"))
        return
    await manage_vps(update, context, update.effective_user.id, context.args[0], "restart")


async def reinstall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/reinstall &lt;vps_id&gt; [ubuntu|debian]"))
        return
    os_type = normalize_os(context.args[1]) if len(context.args) > 1 else "ubuntu"
    if not os_type:
        await update.effective_message.reply_html("Supported operating systems: <code>ubuntu</code> and <code>debian</code>.")
        return
    await reinstall_vps(update, context, update.effective_user.id, context.args[0], os_type)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/remove &lt;vps_id&gt;"))
        return
    vps = get_vps_by_identifier(update.effective_user.id, context.args[0])
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found.")
        return
    progress = await update.effective_message.reply_text("Removing VPS…")
    await async_docker_stop(vps["container_id"])
    removed = await async_docker_rm(vps["container_id"])
    if removed:
        delete_vps(vps["container_id"])
        await progress.edit_text("VPS removed successfully.")
    else:
        await progress.edit_text("Docker could not remove the VPS. The database record was preserved.")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    if not context.args:
        await update.effective_message.reply_html(usage("/logs &lt;vps_id&gt; [lines]"))
        return
    lines = 50
    if len(context.args) > 1:
        with suppress(ValueError):
            lines = int(context.args[1])
    vps = get_vps_by_identifier(update.effective_user.id, context.args[0])
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found.")
        return
    logs = await asyncio.to_thread(get_logs, vps["container_id"], lines)
    await send_code_chunks(update.effective_message, logs, f"Logs: {vps['container_name']}")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_user_access(update, context):
        return
    started = time.perf_counter()
    try:
        await context.bot.get_me()
        latency = round((time.perf_counter() - started) * 1000)
        await update.effective_message.reply_html(f"<b>Pong</b>\nTelegram API latency: <code>{latency} ms</code>")
    except TelegramError:
        await update.effective_message.reply_text("Telegram API latency could not be measured.")


async def show_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    balance = get_credit_balance(update.effective_user.id)
    await update.effective_message.reply_html(
        f"<b>Your Credits</b>\n\n"
        f"Available balance: <b>{balance}</b>\n"
        f"Cost per VPS: <b>{VPS_CREDIT_COST} credits</b>\n"
        f"Reward per verified referral: <b>{REFERRAL_CREDIT_REWARD} credits</b>\n\n"
        "Invite one verified user to earn enough credits for one VPS."
    )


async def show_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_user = await context.bot.get_me()
    if not bot_user.username:
        await update.effective_message.reply_text("The bot username is not configured yet, so a referral link cannot be created.")
        return
    link = f"https://t.me/{bot_user.username}?start={update.effective_user.id}"
    await update.effective_message.reply_html(
        "<b>Invite &amp; Earn</b>\n\n"
        f"Share this link:\n<code>{clean_html(link)}</code>\n\n"
        f"When a new user starts the bot from your link and joins all required channels, you receive "
        f"<b>{REFERRAL_CREDIT_REWARD} credits</b>. <b>{VPS_CREDIT_COST} credits = 1 VPS</b>."
    )


async def show_admin_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    channels = list_force_channels()
    if not channels:
        text = "<b>Force Channels</b>\n\nNo channels are configured. Use <b>Add Channel</b> to enable force join."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Admin Panel", callback_data="admin:panel")]])
    else:
        lines = ["<b>Force Channels</b>", "", "Users must join every listed channel before using the bot.", ""]
        rows: list[list[InlineKeyboardButton]] = []
        for channel in channels:
            label = clean_html(channel["title"])
            lines.append(f"• <b>{label}</b> — <code>{clean_html(channel['chat_id'])}</code>")
            rows.append([InlineKeyboardButton(f"Remove: {channel['title'][:34]}", callback_data=f"chanrm:{channel['chat_id']}")])
        rows.append([InlineKeyboardButton("Admin Panel", callback_data="admin:panel")])
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(rows)
    await update.effective_message.reply_html(text, reply_markup=keyboard)


def admin_tools_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Create VPS for User", callback_data="admin:createuser"), InlineKeyboardButton("VPS Info", callback_data="admin:userinfo")],
            [InlineKeyboardButton("VPS Logs", callback_data="admin:userlogs"), InlineKeyboardButton("Delete User VPS", callback_data="admin:deleteuser")],
            [InlineKeyboardButton("Ban User", callback_data="admin:banuser"), InlineKeyboardButton("Unban User", callback_data="admin:unbanuser")],
            [InlineKeyboardButton("Admin Panel", callback_data="admin:panel")],
        ]
    )


async def admin_action_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_mode"] = "user_action"
    await update.effective_message.reply_html(
        "<b>User VPS Action</b>\n\n"
        "Send this format:\n"
        "<code>user_id | vps_id | start</code>\n\n"
        "Supported actions: <code>start</code>, <code>stop</code>, <code>restart</code>, <code>delete</code>, "
        "<code>suspend</code>, <code>unsuspend</code>.\n"
        "Use the VPS database ID, container name, or container ID. Send <code>cancel</code> to exit."
    )


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    mode = context.user_data.get("admin_mode")
    if not mode or not is_admin(update):
        return False
    text = (update.effective_message.text or "").strip()
    if text.lower() == "cancel":
        context.user_data.pop("admin_mode", None)
        await update.effective_message.reply_text("Admin input cancelled.")
        return True
    if mode == "add_channel":
        parts = [part.strip() for part in text.split("|", 1)]
        target = parts[0]
        invite_link = parts[1] if len(parts) > 1 and parts[1] else None
        if not target:
            await update.effective_message.reply_text("Send a valid @channelusername or numeric channel ID.")
            return True
        chat_target: str | int = target
        if target.lstrip("-").isdigit():
            chat_target = int(target)
        try:
            chat = await context.bot.get_chat(chat_target)
        except TelegramError as exc:
            await update.effective_message.reply_text(
                f"Could not access that channel: {clean_html(exc)}\n\n"
                "Make the bot an administrator in the channel, then try again."
            )
            return True
        if chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
            await update.effective_message.reply_text("Only a channel or supergroup can be used for force join.")
            return True
        add_force_channel(str(chat.id), chat.title or str(chat.id), chat.username, invite_link)
        context.user_data.pop("admin_mode", None)
        await update.effective_message.reply_html(
            f"Force join enabled for <b>{clean_html(chat.title or str(chat.id))}</b>.\n\n"
            "The bot must remain an administrator there so it can verify membership."
        )
        return True
    if mode == "create_user_vps":
        parts = [part.strip() for part in text.split("|")]
        if len(parts) not in {2, 5} or not (target_id := parse_user_id(parts[0])):
            await update.effective_message.reply_html(
                "Use <code>user_id | ubuntu</code> or <code>user_id | debian | 2g | 1 | 10G</code>."
            )
            return True
        os_type = normalize_os(parts[1])
        if not os_type:
            await update.effective_message.reply_text("Supported operating systems are ubuntu and debian.")
            return True
        ram, cpu, disk = (parts[2], parts[3], parts[4]) if len(parts) == 5 else (DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK)
        context.user_data.pop("admin_mode", None)
        await create_vps(update, context, target_id, str(target_id), os_type, ram, cpu, disk, charge_credits=False)
        return True
    if mode in {"vps_info", "vps_logs"}:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) not in {2, 3} or not (target_id := parse_user_id(parts[0])):
            await update.effective_message.reply_html("Use <code>user_id | vps_id</code> (and optionally <code>| lines</code> for logs).")
            return True
        vps = get_vps_by_identifier(target_id, parts[1])
        if not vps:
            await update.effective_message.reply_text("No matching VPS was found for that user.")
            return True
        context.user_data.pop("admin_mode", None)
        if mode == "vps_info":
            uptime, stats = await asyncio.gather(
                asyncio.to_thread(get_uptime, vps["container_id"]),
                asyncio.to_thread(get_stats, vps["container_id"]),
            )
            await update.effective_message.reply_html(
                f"<b>Owner Telegram ID:</b> <code>{target_id}</code>\n\n" + format_vps_details(vps, stats, uptime)
            )
        else:
            lines = 50
            if len(parts) == 3:
                with suppress(ValueError):
                    lines = int(parts[2])
            logs = await asyncio.to_thread(get_logs, vps["container_id"], lines)
            await send_code_chunks(update.effective_message, logs, f"Logs: {vps['container_name']} (owner {target_id})")
        return True
    if mode == "delete_user":
        target_id = parse_user_id(text)
        if not target_id:
            await update.effective_message.reply_text("Send a valid numeric Telegram user ID.")
            return True
        context.user_data.pop("admin_mode", None)
        vps_list = get_user_vps(target_id)
        progress = await update.effective_message.reply_text("Deleting the user's VPS instances…")
        deleted = failures = 0
        for vps in vps_list:
            await async_docker_stop(vps["container_id"])
            if await async_docker_rm(vps["container_id"]):
                delete_vps(vps["container_id"])
                deleted += 1
            else:
                failures += 1
        await progress.edit_text(f"Deleted {deleted} VPS instance(s) for user {target_id}. Failures: {failures}.")
        return True
    if mode in {"ban_user", "unban_user"}:
        target_id = parse_user_id(text)
        if not target_id:
            await update.effective_message.reply_text("Send a valid numeric Telegram user ID.")
            return True
        context.user_data.pop("admin_mode", None)
        if mode == "ban_user":
            add_ban(target_id)
            await update.effective_message.reply_text(f"Telegram user {target_id} has been banned from creating VPS instances.")
        else:
            remove_ban(target_id)
            await update.effective_message.reply_text(f"Telegram user {target_id} has been unbanned.")
        return True
    if mode == "user_action":
        parts = [part.strip() for part in text.split("|")]
        if len(parts) != 3 or not (target_id := parse_user_id(parts[0])):
            await update.effective_message.reply_text("Use exactly: <code>user_id | vps_id | action</code>")
            return True
        identifier, action = parts[1], parts[2].lower()
        if action not in {"start", "stop", "restart", "delete", "suspend", "unsuspend"}:
            await update.effective_message.reply_text("That action is not supported.")
            return True
        context.user_data.pop("admin_mode", None)
        vps = get_vps_by_identifier(target_id, identifier)
        if not vps:
            await update.effective_message.reply_text("No matching VPS was found for that user.")
            return True
        if action in {"start", "stop", "restart"}:
            await manage_vps(update, context, target_id, identifier, action)
            return True
        progress = await update.effective_message.reply_text(f"Running {action}…")
        if action == "delete":
            await async_docker_stop(vps["container_id"])
            if await async_docker_rm(vps["container_id"]):
                delete_vps(vps["container_id"])
                await progress.edit_text(f"Deleted VPS {vps['container_name']} for user {target_id}.")
            else:
                await progress.edit_text("Docker could not remove the VPS; its database record was preserved.")
        elif action == "suspend":
            if await async_docker_stop(vps["container_id"]):
                update_vps_status(vps["container_id"], "stopped")
                update_vps_suspended(vps["container_id"], 1)
                await progress.edit_text(f"Suspended VPS {vps['container_name']}.")
            else:
                await progress.edit_text("The VPS could not be stopped, so suspension was not applied.")
        else:
            update_vps_suspended(vps["container_id"], 0)
            await progress.edit_text(f"Unsuspended VPS {vps['container_name']}.")
        return True
    return False


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if data == "force:check":
        if await has_joined_all_channels(update, context):
            await notify_referral_reward(context, update.effective_user.id)
            await show_main_menu(update, context, "Membership verified successfully.")
        return
    if not await require_user_access(update, context):
        return
    if data == "menu:home":
        await show_main_menu(update, context)
        return
    if data == "menu:create":
        await update.effective_message.reply_html("<b>Create VPS</b>\nChoose the operating system.", reply_markup=os_keyboard())
        return
    if data == "menu:vps":
        await show_vps_list(update, context)
        return
    if data == "menu:credits":
        await show_credits(update, context)
        return
    if data == "menu:refer":
        await show_referral_link(update, context)
        return
    if data == "menu:help":
        await help_command(update, context)
        return
    if data == "menu:about":
        await about_command(update, context)
        return
    if data.startswith("deploy:"):
        os_type = normalize_os(data.split(":", 1)[1])
        if os_type:
            await create_vps(
                update, context, update.effective_user.id, display_name(update), os_type,
                DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK,
            )
        return
    if data.startswith("vps:"):
        vps = get_vps_by_identifier(update.effective_user.id, data.split(":", 1)[1])
        if not vps:
            await update.effective_message.reply_text("That VPS no longer exists.")
            return
        uptime, stats = await asyncio.gather(
            asyncio.to_thread(get_uptime, vps["container_id"]),
            asyncio.to_thread(get_stats, vps["container_id"]),
        )
        await update.effective_message.reply_html(format_vps_details(vps, stats, uptime), reply_markup=vps_keyboard(vps))
        return
    if data.startswith("vact:"):
        _, vps_id, action = data.split(":", 2)
        if action in {"start", "stop", "restart"}:
            await manage_vps(update, context, update.effective_user.id, vps_id, action)
        elif action == "ssh":
            status = await update.effective_message.reply_text("Generating a fresh SSH session…")
            _, detail = await regenerate_ssh(context.bot, update.effective_user.id, vps_id)
            await status.edit_text(detail)
        elif action == "logs":
            vps = get_vps_by_identifier(update.effective_user.id, vps_id)
            if not vps:
                await update.effective_message.reply_text("That VPS no longer exists.")
            else:
                logs = await asyncio.to_thread(get_logs, vps["container_id"], 50)
                await send_code_chunks(update.effective_message, logs, f"Logs: {vps['container_name']}")
        elif action in {"ubuntu", "debian"}:
            await reinstall_vps(update, context, update.effective_user.id, vps_id, action)
        return
    if data.startswith("vdel:"):
        vps_id = data.split(":", 1)[1]
        await update.effective_message.reply_html(
            "<b>Remove VPS?</b>\nThis permanently deletes the selected VPS.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Yes, Remove", callback_data=f"vconfirm:{vps_id}"),
                  InlineKeyboardButton("Cancel", callback_data=f"vps:{vps_id}")]]
            ),
        )
        return
    if data.startswith("vconfirm:"):
        vps = get_vps_by_identifier(update.effective_user.id, data.split(":", 1)[1])
        if not vps:
            await update.effective_message.reply_text("That VPS no longer exists.")
            return
        progress = await update.effective_message.reply_text("Removing VPS…")
        await async_docker_stop(vps["container_id"])
        if await async_docker_rm(vps["container_id"]):
            delete_vps(vps["container_id"])
            await progress.edit_text("VPS removed successfully.")
        else:
            await progress.edit_text("Docker could not remove the VPS. The database record was preserved.")
        return
    if not is_admin(update):
        return
    if data == "admin:panel":
        await update.effective_message.reply_html("<b>Administrator Panel</b>\nChoose an action below.", reply_markup=admin_keyboard())
    elif data == "admin:stats":
        await admin_stats_command(update, context)
    elif data == "admin:vps":
        await admin_list_command(update, context)
    elif data == "admin:users":
        await admin_list_users_command(update, context)
    elif data in {"admin:channels", "admin:removechannel"}:
        await show_admin_channels(update, context)
    elif data == "admin:addchannel":
        context.user_data["admin_mode"] = "add_channel"
        await update.effective_message.reply_html(
            "<b>Add Force Channel</b>\n\n"
            "Send <code>@publicchannel</code> for a public channel, or send "
            "<code>-1001234567890 | https://t.me/+invite</code> for a private channel.\n\n"
            "Before adding it, make this bot an administrator in the channel. Send <code>cancel</code> to exit."
        )
    elif data.startswith("chanrm:"):
        chat_id = data.split(":", 1)[1]
        if remove_force_channel(chat_id):
            await update.effective_message.reply_text("Force channel removed successfully.")
        else:
            await update.effective_message.reply_text("That force channel was already removed.")
    elif data == "admin:tools":
        await update.effective_message.reply_html("<b>User Management</b>\nChoose an administrative action.", reply_markup=admin_tools_keyboard())
    elif data == "admin:createuser":
        context.user_data["admin_mode"] = "create_user_vps"
        await update.effective_message.reply_html(
            "<b>Create VPS for User</b>\n\n"
            "Send <code>user_id | ubuntu</code>, or include resources: "
            "<code>user_id | debian | 2g | 1 | 10G</code>. Send <code>cancel</code> to exit."
        )
    elif data == "admin:userinfo":
        context.user_data["admin_mode"] = "vps_info"
        await update.effective_message.reply_html("<b>User VPS Info</b>\nSend <code>user_id | vps_id</code>. Send <code>cancel</code> to exit.")
    elif data == "admin:userlogs":
        context.user_data["admin_mode"] = "vps_logs"
        await update.effective_message.reply_html("<b>User VPS Logs</b>\nSend <code>user_id | vps_id | lines</code>. Send <code>cancel</code> to exit.")
    elif data == "admin:deleteuser":
        context.user_data["admin_mode"] = "delete_user"
        await update.effective_message.reply_html("<b>Delete User VPS Instances</b>\nSend the numeric <code>user_id</code>. Send <code>cancel</code> to exit.")
    elif data == "admin:banuser":
        context.user_data["admin_mode"] = "ban_user"
        await update.effective_message.reply_html("<b>Ban User</b>\nSend the numeric <code>user_id</code>. Send <code>cancel</code> to exit.")
    elif data == "admin:unbanuser":
        context.user_data["admin_mode"] = "unban_user"
        await update.effective_message.reply_html("<b>Unban User</b>\nSend the numeric <code>user_id</code>. Send <code>cancel</code> to exit.")
    elif data == "admin:useraction":
        await admin_action_prompt(update, context)
    elif data == "admin:killall":
        await update.effective_message.reply_html(
            "<b>Stop all running VPS instances?</b>",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Yes, Stop All", callback_data="admin:killconfirm"),
                  InlineKeyboardButton("Cancel", callback_data="admin:panel")]]
            ),
        )
    elif data == "admin:killconfirm":
        await admin_kill_all_command(update, context)


async def button_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update):
        return
    if await handle_admin_text_input(update, context):
        return
    text = (update.effective_message.text or "").strip()
    if text == "Admin Panel":
        if is_admin(update):
            await update.effective_message.reply_html("<b>Administrator Panel</b>\nChoose an action below.", reply_markup=admin_keyboard())
        else:
            await update.effective_message.reply_text("This button is restricted to the configured administrator.")
        return
    if not await require_user_access(update, context):
        return
    if text == "Create VPS":
        await update.effective_message.reply_html("<b>Create VPS</b>\nChoose the operating system.", reply_markup=os_keyboard())
    elif text == "My VPS":
        await show_vps_list(update, context)
    elif text == "My Credits":
        await show_credits(update, context)
    elif text == "Invite & Earn":
        await show_referral_link(update, context)
    elif text == "About":
        await about_command(update, context)
    elif text == "Help":
        await help_command(update, context)
    elif text == "Check Bot":
        await ping_command(update, context)
    else:
        await show_main_menu(update, context, "Please use one of the menu buttons.")


# ---------------------------------------------------------------------------
# Administrator command handlers
# ---------------------------------------------------------------------------
def parse_user_id(value: str) -> Optional[int]:
    try:
        user_id = int(value)
        return user_id if user_id > 0 else None
    except ValueError:
        return None


async def admin_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_html(
            usage("/admin_create &lt;user_id&gt; &lt;ubuntu|debian&gt; [ram] [cpu] [disk]")
        )
        return
    target_id = parse_user_id(context.args[0])
    os_type = normalize_os(context.args[1])
    if not target_id or not os_type:
        await update.effective_message.reply_text("Provide a valid numeric Telegram user ID and operating system.")
        return
    ram = context.args[2] if len(context.args) > 2 else DEFAULT_RAM
    cpu = context.args[3] if len(context.args) > 3 else DEFAULT_CPU
    disk = context.args[4] if len(context.args) > 4 else DEFAULT_DISK
    await create_vps(update, context, target_id, str(target_id), os_type, ram, cpu, disk, charge_credits=False)


async def admin_manage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if len(context.args) < 3:
        await update.effective_message.reply_html(
            usage("/admin_manage &lt;user_id&gt; &lt;vps_id&gt; &lt;start|stop|restart|delete|suspend|unsuspend&gt;")
        )
        return
    target_id = parse_user_id(context.args[0])
    identifier, action = context.args[1], context.args[2].lower()
    if not target_id or action not in {"start", "stop", "restart", "delete", "suspend", "unsuspend"}:
        await update.effective_message.reply_text("Provide a valid numeric user ID and supported action.")
        return
    vps = get_vps_by_identifier(target_id, identifier)
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found for that user.")
        return
    if action in {"start", "stop", "restart"}:
        await manage_vps(update, context, target_id, identifier, action)
        return

    progress = await update.effective_message.reply_text(f"Running admin action: {action}…")
    if action == "delete":
        await async_docker_stop(vps["container_id"])
        removed = await async_docker_rm(vps["container_id"])
        if removed:
            delete_vps(vps["container_id"])
            await progress.edit_text(f"Deleted VPS {vps['container_name']} for Telegram user {target_id}.")
        else:
            await progress.edit_text("Docker could not remove the VPS; its database record was preserved.")
    elif action == "suspend":
        stopped = await async_docker_stop(vps["container_id"])
        if stopped:
            update_vps_status(vps["container_id"], "stopped")
            update_vps_suspended(vps["container_id"], 1)
            await progress.edit_text(f"Suspended VPS {vps['container_name']}.")
        else:
            await progress.edit_text("The VPS could not be stopped, so suspension was not applied.")
    else:
        update_vps_suspended(vps["container_id"], 0)
        await progress.edit_text(f"Unsuspended VPS {vps['container_name']}. The user may now start it.")


async def admin_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    rows = get_all_vps()
    if not rows:
        await update.effective_message.reply_text("No VPS instances were found.")
        return
    output = ["All VPS instances:\n"]
    for row in rows:
        output.append(
            f"[{row['status'].upper()}] user={row['user_id']} {row['username']}\n"
            f"Name: {row['container_name']} ({row['os_type']})\nID: {row['container_id']}\n"
            f"Hostname: {row['hostname']} | Resources: {row['ram']} RAM / {row['cpu']} CPU / {row['disk']} Disk\n"
            f"Suspended: {'Yes' if row['suspended'] else 'No'}\n"
        )
    await send_code_chunks(update.effective_message, "\n".join(output))


async def admin_list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    rows = get_users_overview()
    if not rows:
        await update.effective_message.reply_text("No users were found.")
        return
    output = ["Users overview:\n"]
    for row in rows:
        output.append(
            f"{row['username']} | ID: {row['user_id']} | VPS: {row['total_vps']} | Running: {row['running_vps'] or 0}"
        )
    await send_code_chunks(update.effective_message, "\n".join(output))


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    stats = get_bot_stats()
    await update.effective_message.reply_html(
        "<b>Bot Statistics</b>\n\n"
        f"<b>Total Users:</b> {stats['users']}\n"
        f"<b>Banned Users:</b> {stats['banned']}\n"
        f"<b>Total VPS:</b> {stats['vps']}\n"
        f"<b>Running VPS:</b> {stats['running']}\n"
        f"<b>Total User Credits:</b> {stats['credits']}\n"
        f"<b>Force Channels:</b> {stats['channels']}\n"
        f"<b>Total CPU Allocated:</b> {stats['cpu']:.2f} cores\n"
        f"<b>Total RAM Allocated:</b> {stats['ram']:.2f} GB\n"
        f"<b>Total Disk Requested:</b> {stats['disk']:.2f} GB"
    )


async def admin_vps_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_html(usage("/admin_vps_info &lt;user_id&gt; &lt;vps_id&gt;"))
        return
    target_id = parse_user_id(context.args[0])
    if not target_id:
        await update.effective_message.reply_text("Provide a valid numeric Telegram user ID.")
        return
    vps = get_vps_by_identifier(target_id, context.args[1])
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found.")
        return
    uptime, stats = await asyncio.gather(
        asyncio.to_thread(get_uptime, vps["container_id"]),
        asyncio.to_thread(get_stats, vps["container_id"]),
    )
    await update.effective_message.reply_html(
        f"<b>Owner Telegram ID:</b> <code>{target_id}</code>\n\n" + format_vps_details(vps, stats, uptime)
    )


async def admin_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_html(usage("/admin_logs &lt;user_id&gt; &lt;vps_id&gt; [lines]"))
        return
    target_id = parse_user_id(context.args[0])
    if not target_id:
        await update.effective_message.reply_text("Provide a valid numeric Telegram user ID.")
        return
    lines = 50
    if len(context.args) > 2:
        with suppress(ValueError):
            lines = int(context.args[2])
    vps = get_vps_by_identifier(target_id, context.args[1])
    if not vps:
        await update.effective_message.reply_text("No matching VPS was found.")
        return
    logs = await asyncio.to_thread(get_logs, vps["container_id"], lines)
    await send_code_chunks(update.effective_message, logs, f"Logs: {vps['container_name']} (owner {target_id})")


async def admin_delete_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if not context.args or not (target_id := parse_user_id(context.args[0])):
        await update.effective_message.reply_html(usage("/admin_delete_user &lt;user_id&gt;"))
        return
    vps_list = get_user_vps(target_id)
    progress = await update.effective_message.reply_text("Deleting the user's VPS instances…")
    deleted = 0
    failures = 0
    for vps in vps_list:
        await async_docker_stop(vps["container_id"])
        if await async_docker_rm(vps["container_id"]):
            delete_vps(vps["container_id"])
            deleted += 1
        else:
            failures += 1
    await progress.edit_text(f"Deleted {deleted} VPS instance(s) for user {target_id}. Failures: {failures}.")


async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if not context.args or not (target_id := parse_user_id(context.args[0])):
        await update.effective_message.reply_html(usage("/admin_ban &lt;user_id&gt;"))
        return
    add_ban(target_id)
    await update.effective_message.reply_text(f"Telegram user {target_id} has been banned from creating VPS instances.")


async def admin_unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    if not context.args or not (target_id := parse_user_id(context.args[0])):
        await update.effective_message.reply_html(usage("/admin_unban &lt;user_id&gt;"))
        return
    remove_ban(target_id)
    await update.effective_message.reply_text(f"Telegram user {target_id} has been unbanned.")


async def admin_kill_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_private(update) or not await require_admin(update):
        return
    with get_db_connection() as conn:
        rows = conn.execute('SELECT container_id FROM vps WHERE status = "running"').fetchall()
    progress = await update.effective_message.reply_text("Stopping all running VPS instances…")
    stopped = 0
    for row in rows:
        if await async_docker_stop(row["container_id"]):
            update_vps_status(row["container_id"], "stopped")
            stopped += 1
    await progress.edit_text(f"Stopped {stopped} of {len(rows)} running VPS instance(s).")


# ---------------------------------------------------------------------------
# Lifecycle, recurring reconciliation, and startup
# ---------------------------------------------------------------------------
async def sync_statuses_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT container_id, status FROM vps").fetchall()
    for row in rows:
        actual_status = await asyncio.to_thread(get_container_status, row["container_id"])
        target_status = actual_status or "stopped"
        if target_status != row["status"]:
            update_vps_status(row["container_id"], target_status)
            logger.info("Updated status of %s to %s", row["container_id"], target_status)


async def post_init(application: Application) -> None:
    # The public Telegram command menu remains intentionally minimal; all user and administrator actions are buttons.
    start_only = [BotCommand("start", "Open the VPS control panel")]
    await application.bot.set_my_commands(start_only)
    if ADMIN_ID:
        await application.bot.set_my_commands(start_only, scope=BotCommandScopeChat(ADMIN_ID))
    if application.job_queue is None:
        raise RuntimeError('Job queue is unavailable. Install python-telegram-bot with the "job-queue" extra.')
    application.job_queue.run_repeating(sync_statuses_job, interval=STATUS_SYNC_SECONDS, first=10, name="sync_statuses")
    logger.info("Telegram bot startup completed; status synchronization runs every %s seconds.", STATUS_SYNC_SECONDS)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with suppress(TelegramError):
            await update.effective_message.reply_text("An unexpected error occurred. Please retry or contact the administrator.")


def build_application() -> Application:
    defaults = Defaults(parse_mode=ParseMode.HTML, tzinfo=timezone.utc)
    application = ApplicationBuilder().token(TOKEN).defaults(defaults).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("deploy", deploy_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("vps_info", vps_info_command))
    application.add_handler(CommandHandler("regen_ssh", regen_ssh_command))
    application.add_handler(CommandHandler("vps_start", vps_start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("reinstall", reinstall_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("ping", ping_command))

    application.add_handler(CommandHandler("admin_create", admin_create_command))
    application.add_handler(CommandHandler("admin_manage", admin_manage_command))
    application.add_handler(CommandHandler("admin_list", admin_list_command))
    application.add_handler(CommandHandler("admin_list_users", admin_list_users_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats_command))
    application.add_handler(CommandHandler("admin_vps_info", admin_vps_info_command))
    application.add_handler(CommandHandler("admin_logs", admin_logs_command))
    application.add_handler(CommandHandler("admin_delete_user", admin_delete_user_command))
    application.add_handler(CommandHandler("admin_ban", admin_ban_command))
    application.add_handler(CommandHandler("admin_unban", admin_unban_command))
    application.add_handler(CommandHandler("admin_kill_all", admin_kill_all_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_text_handler))
    application.add_error_handler(error_handler)
    return application


if __name__ == "__main__":
    if not TOKEN or TOKEN == "PASTE_NEW_BOTFATHER_TOKEN_HERE":
        logger.error("Set BOT_TOKEN at the very top of bot.py before running it.")
        sys.exit(1)
    if not isinstance(ADMIN_ID, int) or ADMIN_ID <= 0:
        logger.error("Set ADMIN_ID at the very top of bot.py to your numeric Telegram user ID.")
        sys.exit(1)
    init_db()
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
