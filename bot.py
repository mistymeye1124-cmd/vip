# language: Python, file: bot.py, target: Python 3.10+, python-telegram-bot v21+
"""
Pro Access Vault - Course Edition VIP Subscription & Multi-Channel Auto-Kick Bot.
Main Application Runner with:
- 1-Time Anti-Leak Single-Use Invite Link Dispatcher
- Expiry Tracking & Real-Time Countdown
- Interactive Admin GUI Panel (/admin) & Quick Commands
- Instant Payment Submission & 1-Click Admin Approval
- Multi-Channel Ban/Unban Auto-Kick Engine Integration
- Atomic One-Click Database Backups to Telegram DM
"""

import os
import sys
import re
import html
import json
import logging
import datetime
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from collections import deque
import time

# Ensure clean UTF-8 console output on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telegram import (
    Update,
    Bot,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    ChatMember,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.request import HTTPXRequest

from database import db
from scheduler import VaultScheduler
from emojis import (
    render_premium_html,
    em_tag,
    get_tg_emoji_tag,
    strip_tg_emoji,
    strip_all_emojis,
    patch_bot_for_premium_emojis,
)

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vault_bot")


class LiveLogBuffer(logging.Handler):
    """In-memory thread-safe buffer capturing the latest log events for live admin diagnostics."""
    def __init__(self, maxlen: int = 30):
        super().__init__()
        self.buffer: deque = deque(maxlen=maxlen)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.buffer.append(msg)
        except Exception:
            pass

    def get_logs(self, limit: int = 12) -> List[str]:
        return list(self.buffer)[-limit:]

    def clear(self):
        self.buffer.clear()


live_log_buffer = LiveLogBuffer(maxlen=40)
live_log_buffer.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(live_log_buffer)

BASE_DIR = Path(__file__).resolve().parent

# Load Environment Variables (.env) with bulletproof fallback
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

# Load Configuration
CONFIG_PATH = BASE_DIR / "config.json"
if CONFIG_PATH.is_dir():
    CONFIG_PATH = CONFIG_PATH / "config.json"

if not CONFIG_PATH.exists() or CONFIG_PATH.is_dir():
    CONFIG: Dict[str, Any] = {}
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)

# Priority: .env > config.json
BOT_TOKEN: str = os.getenv("BOT_TOKEN") or CONFIG.get("bot_token", "")
CONFIG["bot_token"] = BOT_TOKEN

env_admins = os.getenv("ADMIN_IDS")
if env_admins:
    try:
        ADMIN_IDS: List[int] = [int(x.strip()) for x in env_admins.split(",") if x.strip().lstrip("-").isdigit()]
    except Exception:
        ADMIN_IDS = CONFIG.get("admin_ids", [])
else:
    ADMIN_IDS = CONFIG.get("admin_ids", [])
CONFIG["admin_ids"] = ADMIN_IDS

PRODUCT_NAME: str = os.getenv("PRODUCT_NAME") or CONFIG.get("product_name", "Pro Access Vault - Course Edition")
CONFIG["product_name"] = PRODUCT_NAME

# Synchronize VIP Channels from SQLite Database (Immortal Storage across Docker / Git updates)
try:
    db_channels = db.get_all_channels()
    if db_channels:
        CHANNELS: List[Dict[str, Any]] = db_channels
        CONFIG["channels"] = CHANNELS
    else:
        CHANNELS: List[Dict[str, Any]] = CONFIG.get("channels", [])
        for ch in CHANNELS:
            if "type" not in ch:
                ch["type"] = "channel"
            db.add_or_update_channel(
                ch["id"],
                ch.get("name", str(ch["id"])),
                ch.get("type", "channel"),
                ch.get("invite_expire_hours", 24)
            )
except Exception as e:
    logger.error(f"Failed to synchronize channels from SQLite DB: {e}")
    CHANNELS = CONFIG.get("channels", [])

# Synchronize Plans & Pricing from SQLite Database (Immortal Storage across updates)
try:
    db_plans = db.get_all_plans()
    if db_plans:
        PLANS: Dict[str, Any] = db_plans
        CONFIG["plans"] = PLANS
    else:
        PLANS: Dict[str, Any] = CONFIG.get("plans", {})
        for p_key, p_val in PLANS.items():
            db.save_plan(
                key=p_key,
                name=p_val.get("name", p_key),
                duration_days=p_val.get("duration_days", 30),
                price_bdt=p_val.get("price_bdt", 1000),
                price_usdt=p_val.get("price_usdt", 10),
                is_lifetime=p_val.get("is_lifetime", False),
                features=p_val.get("features", []),
            )
except Exception as e:
    logger.error(f"Failed to synchronize plans from SQLite DB: {e}")
    PLANS = CONFIG.get("plans", {})

# Synchronize Payment Info from SQLite Database
try:
    pay_settings = db.get_all_settings()
    PAYMENT_INFO: Dict[str, str] = CONFIG.get("payment_info", {})
    for k in ("bkash", "nagad", "binance_pay_id", "usdt_trc20"):
        db_val = pay_settings.get(f"pay_{k}")
        if db_val:
            PAYMENT_INFO[k] = db_val
        elif k in PAYMENT_INFO:
            db.set_setting(f"pay_{k}", PAYMENT_INFO[k])
except Exception as e:
    logger.error(f"Failed to synchronize payment settings from SQLite DB: {e}")
    PAYMENT_INFO = CONFIG.get("payment_info", {})

# Payment overrides from ENV if set
if os.getenv("BKASH_NUMBER"):
    PAYMENT_INFO["bkash"] = os.getenv("BKASH_NUMBER")
if os.getenv("NAGAD_NUMBER"):
    PAYMENT_INFO["nagad"] = os.getenv("NAGAD_NUMBER")
if os.getenv("BINANCE_PAY_ID"):
    PAYMENT_INFO["binance_pay_id"] = os.getenv("BINANCE_PAY_ID")
if os.getenv("USDT_TRC20"):
    PAYMENT_INFO["usdt_trc20"] = os.getenv("USDT_TRC20")
CONFIG["payment_info"] = PAYMENT_INFO

# VIP Preview Channel Configuration (Loaded from SQLite first)
VIP_PREVIEW_LINK: str = db.get_setting("vip_preview_link") or os.getenv("VIP_PREVIEW_LINK") or str(CONFIG.get("vip_preview_link") or "")
if not VIP_PREVIEW_LINK or not VIP_PREVIEW_LINK.strip():
    VIP_PREVIEW_LINK = "https://t.me/PAV_CourseEdition/16"
VIP_PREVIEW_NAME: str = db.get_setting("vip_preview_name") or os.getenv("VIP_PREVIEW_NAME") or str(CONFIG.get("vip_preview_name") or "VIP Preview & Proof Channel")
CONFIG["vip_preview_link"] = VIP_PREVIEW_LINK
CONFIG["vip_preview_name"] = VIP_PREVIEW_NAME

# VIP Rules & Price-Lock Notice Configuration (Editable via /setrules or Admin GUI)
DEFAULT_RULES_TEXT = (
    "⚠️ <b>জরুরি নিয়মাবলী ও প্রাইস-লক পলিসি (MUST READ):</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "• 🔄 <b>প্রাইস হাইক:</b> প্রতি মাসে নতুন কোর্সের সাথে VIP ফি বৃদ্ধি পাবে।\n"
    "• 🔒 <b>প্রাইস-লক সুবিধা:</b> আজ যে মূল্যে জয়েন করবেন, আজীবন ঠিক সেই নির্দিষ্ট মূল্যেই রিনিউ করতে পারবেন!\n"
    "• ⚠️ <b>কড়া শর্ত:</b> মেয়াদ শেষ হওয়ার আগেই টানা রিনিউ রাখতে হবে। মাঝখানে গ্যাপ পড়লে বা মেয়াদ পার হলে প্রাইস-লক অফার বাতিল হয়ে নতুন বর্ধিত রেটে কিনতে হবে।\n"
    "• 🚫 <b>অ্যান্টি-লিক ও সিকিউরিটি:</b> ১-টাইম সিঙ্গেল ইউজ লিংক। শেয়ার বা লিক করার চেষ্টা করলে কোনো নোটিস ছাড়াই পার্মানেন্ট ব্যান।\n"
    "• ❌ <b>নো রিফান্ড:</b> ডিজিটাল ভল্ট অ্যাক্সেস ডেলিভারির পর কোনো রিফান্ড প্রযোজ্য নয়।\n"
    "• 👁️ <b>লাইভ প্রুফ:</b> কেনার আগে সম্পূর্ণ নিয়ম ও কোর্স প্রুফ দেখতে প্রিভিউ চ্যানেল চেক করুন।"
)

def get_rules_text() -> str:
    """Returns the custom VIP rules text if set by admin, otherwise returns DEFAULT_RULES_TEXT."""
    try:
        saved = db.get_setting("vip_rules_text")
        if saved and saved.strip():
            return saved.strip()
    except Exception:
        pass
    return DEFAULT_RULES_TEXT

# Force Channel Join Configuration (Loaded from SQLite first)
FORCE_CHANNEL_ID: str = db.get_setting("force_channel_id") or os.getenv("FORCE_CHANNEL_ID") or str(CONFIG.get("force_channel_id") or "")
FORCE_CHANNEL_LINK: str = db.get_setting("force_channel_link") or os.getenv("FORCE_CHANNEL_LINK") or str(CONFIG.get("force_channel_link") or "")
FORCE_CHANNEL_NAME: str = db.get_setting("force_channel_name") or os.getenv("FORCE_CHANNEL_NAME") or str(CONFIG.get("force_channel_name") or "Official Channel")
CONFIG["force_channel_id"] = FORCE_CHANNEL_ID
CONFIG["force_channel_link"] = FORCE_CHANNEL_LINK
CONFIG["force_channel_name"] = FORCE_CHANNEL_NAME

# Pending user states in memory (for conversation steps)
USER_STATES: Dict[int, Dict[str, Any]] = {}


def get_dollar_rate() -> float:
    """Returns the current exchange rate (BDT per 1 USD) from SQLite settings."""
    try:
        val = db.get_setting("usdt_rate_bdt", "125")
        rate = float(val) if val else 125.0
        return rate if rate > 0 else 125.0
    except Exception:
        return 125.0


def set_dollar_rate(rate: float):
    """Saves the exchange rate to SQLite settings and updates plans in memory & DB."""
    if rate <= 0:
        rate = 125.0
    db.set_setting("usdt_rate_bdt", str(rate))
    for pk, pv in PLANS.items():
        pv["price_usdt"] = calc_usd(pv.get("price_bdt", 0), rate)
        try:
            db.update_plan_price(pk, pv["price_bdt"], pv["price_usdt"])
        except Exception:
            pass


def calc_usd(bdt_amount: float, rate: Optional[float] = None) -> Any:
    """Dynamically converts BDT to USD based on current exchange rate."""
    r = rate if rate is not None else get_dollar_rate()
    if r <= 0:
        r = 125.0
    val = round(bdt_amount / r, 2)
    return int(val) if val.is_integer() else val


def format_price(bdt_amount: float, usd_amount: Optional[float] = None) -> str:
    """Formats full price displaying both BDT and USD: '750 BDT / $6 USDT'"""
    usd = usd_amount if usd_amount is not None else calc_usd(bdt_amount)
    usd_str = f"${int(usd)}" if isinstance(usd, int) or (isinstance(usd, float) and usd.is_integer()) else f"${usd:.2f}"
    return f"{int(bdt_amount):,} BDT / {usd_str} USDT"


def format_btn_price(bdt_amount: float, usd_amount: Optional[float] = None) -> str:
    """Short format for buttons: '750 BDT / $6'"""
    usd = usd_amount if usd_amount is not None else calc_usd(bdt_amount)
    usd_str = f"${int(usd)}" if isinstance(usd, int) or (isinstance(usd, float) and usd.is_integer()) else f"${usd:.2f}"
    return f"{int(bdt_amount):,} BDT / {usd_str}"


# Initial synchronization of plan USDT prices according to stored dollar rate
try:
    for pk, pv in PLANS.items():
        pv["price_usdt"] = calc_usd(pv.get("price_bdt", 0))
except Exception:
    pass


def save_config():
    """Persists channels, plans, and payment settings to both SQLite DB and config.json so they are NEVER lost."""
    global CHANNELS, PLANS, PAYMENT_INFO, FORCE_CHANNEL_ID, FORCE_CHANNEL_LINK, FORCE_CHANNEL_NAME, VIP_PREVIEW_LINK, VIP_PREVIEW_NAME
    for ch in CHANNELS:
        try:
            db.add_or_update_channel(
                ch["id"],
                ch.get("name", str(ch["id"])),
                ch.get("type", "channel"),
                ch.get("invite_expire_hours", 24)
            )
        except Exception as e:
            logger.error(f"Failed to persist channel {ch.get('id')} to SQLite DB: {e}")

    for p_key, p_val in PLANS.items():
        try:
            db.save_plan(
                key=p_key,
                name=p_val.get("name", p_key),
                duration_days=p_val.get("duration_days", 30),
                price_bdt=p_val.get("price_bdt", 1000),
                price_usdt=p_val.get("price_usdt", 10),
                is_lifetime=p_val.get("is_lifetime", False),
                features=p_val.get("features", []),
            )
        except Exception as e:
            logger.error(f"Failed to persist plan {p_key} to SQLite DB: {e}")

    for pay_k, pay_v in PAYMENT_INFO.items():
        try:
            db.set_setting(f"pay_{pay_k}", pay_v)
        except Exception as e:
            pass

    try:
        db.set_setting("vip_preview_link", VIP_PREVIEW_LINK or "")
        db.set_setting("vip_preview_name", VIP_PREVIEW_NAME or "")
        db.set_setting("force_channel_id", FORCE_CHANNEL_ID or "")
        db.set_setting("force_channel_link", FORCE_CHANNEL_LINK or "")
        db.set_setting("force_channel_name", FORCE_CHANNEL_NAME or "")
    except Exception as e:
        logger.error(f"Failed to persist settings to SQLite DB: {e}")

    CONFIG["channels"] = CHANNELS
    CONFIG["plans"] = PLANS
    CONFIG["payment_info"] = PAYMENT_INFO
    CONFIG["force_channel_id"] = FORCE_CHANNEL_ID
    CONFIG["force_channel_link"] = FORCE_CHANNEL_LINK
    CONFIG["force_channel_name"] = FORCE_CHANNEL_NAME
    CONFIG["vip_preview_link"] = VIP_PREVIEW_LINK
    CONFIG["vip_preview_name"] = VIP_PREVIEW_NAME
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
        logger.info("Configuration saved successfully to config.json & SQLite DB")
    except Exception as e:
        logger.error(f"Failed to write config.json: {e}")



def is_admin(user_id: int) -> bool:
    try:
        u_id = int(user_id)
    except Exception:
        return False
    for a in ADMIN_IDS:
        try:
            if int(a) == u_id:
                return True
        except Exception:
            pass
    try:
        extra = db.get_setting("extra_admin_ids", "")
        if extra:
            e_ids = [int(x.strip()) for x in extra.split(",") if x.strip().lstrip("-").isdigit()]
            if u_id in e_ids:
                return True
    except Exception:
        pass
    return False


def get_persistent_menu(is_admin_user: bool = False, is_vip_user: bool = False) -> ReplyKeyboardMarkup:
    """Constructs the high-aesthetic persistent bottom reply keyboard."""
    keyboard = [
        [KeyboardButton("👑 VIP Plans & Pricing"), KeyboardButton("💳 Payment Methods")],
        [KeyboardButton("👤 My Subscription"), KeyboardButton("🤝 Affiliate Hub")],
    ]
    if is_vip_user:
        keyboard.append([KeyboardButton("🔗 Access VIP Channels"), KeyboardButton("📢 Official Channel")])
    else:
        keyboard.append([KeyboardButton("📢 Official Channel")])
    if is_admin_user:
        keyboard.append([KeyboardButton("⚙️ Admin Control Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


# =============================================================================
# DURATION PARSER (Supports aliases, days, hours, compound)
# =============================================================================
def parse_duration(text: str) -> Tuple[int, str, bool]:
    """
    Parses flexible duration inputs:
      - 'monthly' -> (30*86400, 'Monthly (30 Days)', False)
      - 'yearly' -> (365*86400, 'Yearly (365 Days)', False)
      - 'lifetime' -> (36500*86400, 'Lifetime Access', True)
      - '15d' -> (15*86400, '15 Days', False)
      - '12h' -> (12*3600, '12 Hours', False)
      - '3d12h' -> (3*86400 + 12*3600, '3 Days 12 Hours', False)
    Returns: (seconds, human_label, is_lifetime)
    """
    clean = text.strip().lower()

    if clean in ("monthly", "month", "1m"):
        return 30 * 86400, "Monthly (30 Days)", False
    if clean in ("yearly", "year", "1y"):
        return 365 * 86400, "Yearly (365 Days)", False
    if clean in ("lifetime", "forever", "perm"):
        return 36500 * 86400, "Lifetime Access", True

    # Regex for compound: e.g. 3d12h or 15d or 6h
    pattern = r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$"
    match = re.match(pattern, clean)

    if match and any(match.groups()):
        days = int(match.group(1) or 0)
        hours = int(match.group(2) or 0)
        minutes = int(match.group(3) or 0)
        total_seconds = (days * 86400) + (hours * 3600) + (minutes * 60)

        parts = []
        if days:
            parts.append(f"{days} Day{'s' if days > 1 else ''}")
        if hours:
            parts.append(f"{hours} Hour{'s' if hours > 1 else ''}")
        if minutes:
            parts.append(f"{minutes} Min{'s' if minutes > 1 else ''}")
        label = " ".join(parts)

        return total_seconds, label, False

    # Plain integer defaults to days (e.g. "15" -> 15 days)
    if clean.isdigit():
        days = int(clean)
        return days * 86400, f"{days} Day{'s' if days > 1 else ''}", False

    raise ValueError(f"Invalid duration format: '{text}'. Use e.g. 30d, 12h, monthly, yearly, lifetime.")


# =============================================================================
# 1-TIME INVITE LINK GENERATOR
# =============================================================================
async def is_user_in_chat(bot, chat_id: int, user_id: int) -> bool:
    """Checks whether the user is currently inside the given chat (member/admin/creator/restricted)."""
    try:
        m = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return m.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False


async def is_user_in_all_channels(bot, user_id: int) -> Tuple[bool, List[Dict[str, Any]]]:
    """Checks whether the user is already inside all managed channels/groups."""
    if not CHANNELS:
        return True, []
    unjoined = []
    for ch in CHANNELS:
        ch_id = ch["id"]
        if not await is_user_in_chat(bot, ch_id, user_id):
            unjoined.append(ch)
    return len(unjoined) == 0, unjoined


async def generate_single_use_links(bot, user_id: int) -> List[Dict[str, Any]]:
    """
    Creates individual single-use invite links (member_limit=1) ONLY for channels the user has not yet joined.
    Links expire in 24 hours if not clicked.
    Returns: List of dicts with name, url, type, id, error
    """
    links = []
    expire_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

    for ch in CHANNELS:
        ch_id = ch["id"]
        ch_name = ch.get("name", str(ch_id))
        ch_type = ch.get("type", "channel")

        # Anti-Abuse: If regular user is already inside this channel, DO NOT generate a new link!
        if not is_admin(user_id) and await is_user_in_chat(bot, ch_id, user_id):
            logger.info(f"User {user_id} is already inside {ch_name} ({ch_id}). Skipping link creation.")
            continue

        try:
            invite = await bot.create_chat_invite_link(
                chat_id=ch_id,
                name=f"User_{user_id}",
                member_limit=1,
                expire_date=expire_date,
            )
            links.append({
                "id": ch_id,
                "name": ch_name,
                "type": ch_type,
                "url": invite.invite_link,
                "error": None,
            })
        except BadRequest as e:
            logger.error(f"Failed to create invite for {ch_name} ({ch_id}): {e}")
            links.append({
                "id": ch_id,
                "name": ch_name,
                "type": ch_type,
                "url": None,
                "error": f"Bot lacks 'Invite Users' admin rights in {ch_name}",
            })
        except Exception as e:
            logger.error(f"Invite creation error for {ch_name}: {e}")
            links.append({
                "id": ch_id,
                "name": ch_name,
                "type": ch_type,
                "url": None,
                "error": "Error generating link",
            })

    return links


async def deliver_welcome_access(bot, user_id: int, plan_name: str, expires_at_iso: str, is_lifetime: bool):
    """Sends the rich welcome card with 1-time channel and group buttons to the user."""
    # Check if user is already inside all channels (Admins exempt for testing)
    if not is_admin(user_id):
        all_joined, unjoined = await is_user_in_all_channels(bot, user_id)
        if all_joined and CHANNELS:
            already_text = (
                f"🔒 <b>VIP এক্সেস সক্রিয় ও সুরক্ষিত</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>আপনি ইতিমধ্যেই আমাদের সব VIP চ্যানেল ও গ্রুপে যুক্ত আছেন!</b>\n\n"
                f"🛡️ <b>নিরাপত্তা সতর্কতা:</b>\n"
                f"লিংক কপি, লিকেজ বা অন্য কারো সাথে শেয়ারিং সম্পূর্ণ নিষিদ্ধ থাকায় নতুন কোনো ইনভাইট লিংক তৈরি হবে না।\n\n"
                f"💡 আপনি সরাসরি টেলিগ্রামের চ্যাট লিস্ট থেকে আপনার যুক্ত থাকা VIP গ্রুপ ও চ্যানেলে প্রবেশ করতে পারেন।"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
                [InlineKeyboardButton("💬 Contact Support", url="https://t.me/proaccessteam")],
            ])
            await bot.send_message(
                chat_id=user_id,
                text=already_text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

    links = await generate_single_use_links(bot, user_id)

    if is_lifetime:
        duration_text = "👑 <b>Lifetime Unlimited Access</b> (চিরস্থায়ী)"
    else:
        try:
            dt = datetime.datetime.fromisoformat(expires_at_iso)
            duration_text = f"⏳ <b>মেয়াদ:</b> <code>{dt.strftime('%d %b %Y, %I:%M %p (UTC)')}</code>"
        except Exception:
            duration_text = f"⏳ <b>মেয়াদ:</b> <code>{expires_at_iso}</code>"

    if not links:
        text = (
            f"🎉 <b>অভিনন্দন! আপনার সাবস্ক্রিপশন অ্যাক্টিভ হয়েছে!</b>\n\n"
            f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
            f"📦 <b>প্ল্যান:</b> {plan_name}\n"
            f"{duration_text}\n\n"
            f"✅ <i>আপনার মেম্বারশিপ সফলভাবে ডাটাবেজে সক্রিয় হয়েছে। কোনো চ্যানেল বা গ্রুপ লিংক না দেখতে পেলে অ্যাডমিনের সাথে যোগাযোগ করুন।</i>"
        )
    else:
        text = (
            f"🎉 <b>অভিনন্দন! আপনার সাবস্ক্রিপশন অ্যাক্টিভ হয়েছে!</b>\n\n"
            f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
            f"📦 <b>প্ল্যান:</b> {plan_name}\n"
            f"{duration_text}\n\n"
            f"🔐 <b>সিকিউর ওয়ান-টাইম এক্সেস লিংক:</b>\n"
            f"<i>নিচের বাটনগুলোতে ক্লিক করে আপনার চ্যানেল ও গ্রুপগুলোতে দ্রুত জয়েন করুন। প্রতিটি লিংক মাত্র ১ বার ব্যবহারযোগ্য এবং ২৪ ঘণ্টা পর এক্সপায়ার হয়ে যাবে।</i>"
        )

    buttons = []
    # 1. VIP Channels first
    for item in links:
        if item.get("type") == "channel":
            url = item.get("url")
            name = item.get("name")
            if url and url.startswith("https://"):
                buttons.append([InlineKeyboardButton(f"📢 {name} (Course Vault)", url=url)])
            else:
                text += f"\n\n⚠️ 📢 {name}: {item.get('error', 'Link error')}"

    # 2. VIP Groups second
    for item in links:
        if item.get("type") == "group":
            url = item.get("url")
            name = item.get("name")
            if url and url.startswith("https://"):
                buttons.append([InlineKeyboardButton(f"👥 {name} (Discussion Group)", url=url)])
            else:
                text += f"\n\n⚠️ 👥 {name}: {item.get('error', 'Link error')}"

    buttons.append([InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")])

    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# =============================================================================
# FORCE CHANNEL JOIN VERIFIER
# =============================================================================
async def check_force_sub(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Checks if a user is subscribed to the mandatory force channel.
    Admins are always exempt.
    Returns: (is_joined: bool, channel_id, channel_link, channel_name)
    """
    if is_admin(user_id):
        return True, None, None, None

    global FORCE_CHANNEL_ID, FORCE_CHANNEL_LINK, FORCE_CHANNEL_NAME

    ch_id_str = str(FORCE_CHANNEL_ID).strip() if FORCE_CHANNEL_ID else ""
    if not ch_id_str or ch_id_str in ("0", "None", "null", "-100xxxxxxxxxx", "YOUR_CHANNEL_ID"):
        return True, None, None, None

    # Handle numeric vs username channel
    try:
        target_chat_id = int(ch_id_str)
    except ValueError:
        target_chat_id = ch_id_str

    link = FORCE_CHANNEL_LINK
    if not link or link in ("https://t.me/your_channel_or_link", "null", "None"):
        if isinstance(target_chat_id, str) and target_chat_id.startswith("@"):
            link = f"https://t.me/{target_chat_id[1:]}"
        else:
            link = "https://t.me"

    name = FORCE_CHANNEL_NAME or "Official Updates Channel"

    try:
        member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=user_id)
        if member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
            "creator",
            "administrator",
            "member",
            "restricted",
        ):
            return True, None, None, None
        else:
            return False, str(target_chat_id), link, name
    except BadRequest as e:
        err = str(e).lower()
        if "user not found" in err or "participant_id_invalid" in err:
            return False, str(target_chat_id), link, name
        elif "chat not found" in err or "bot is not a member" in err or "admin" in err:
            logger.warning(f"Force Sub channel ({target_chat_id}) inaccessible by bot: {e}. Bypassing check so bot does not block legitimate users.")
            return True, None, None, None
        else:
            logger.warning(f"BadRequest checking force sub for user {user_id}: {e}")
            return False, str(target_chat_id), link, name
    except Exception as e:
        logger.warning(f"Unexpected error checking force sub for user {user_id}: {e}")
        return True, None, None, None


async def send_force_sub_message(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_link: str, channel_name: str):
    """Sends or edits message prompting the user to join the mandatory channel first."""
    text = (
        f"⚠️ <b>চ্যানেলে জয়েন করা বাধ্যতামূলক!</b>\n\n"
        f"🏛️ <b>{PRODUCT_NAME}</b> বটটি ব্যবহার করার জন্য আপনাকে প্রথমে আমাদের অফিসিয়াল চ্যানেলে যুক্ত হতে হবে।\n\n"
        f"📢 <b>চ্যানেল:</b> {channel_name or 'Official Telegram Channel'}\n\n"
        f"👇 নিচের <b>'📢 Join Channel'</b> বাটনে ক্লিক করে জয়েন করুন, এরপর <b>'🔄 Verify / Check'</b> বাটনে চাপ দিন:"
    )
    keyboard = [
        [InlineKeyboardButton("📢 Join Channel", url=channel_link)],
        [InlineKeyboardButton("🔄 Verify / Check", callback_data="check_force_sub")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            await update.callback_query.message.reply_text(
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
    elif update.message:
        await update.message.reply_text(
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )


# =============================================================================
# USER COMMANDS & HANDLERS
# =============================================================================
async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Renders the aesthetic VIP Hub Dashboard with interactive buttons."""
    user = update.effective_user
    user_id = user.id

    em_crw = em_tag("crown", "👑")
    em_dia = em_tag("diamond", "💎")
    em_usr = em_tag("profile", "👤")
    em_shd = em_tag("verify", "🛡️")
    em_crs = em_tag("courses", "🎓")
    em_chn = em_tag("broadcast", "📢")
    em_grp = em_tag("referral", "👥")
    em_wal = em_tag("wallet", "💳")
    em_zap = em_tag("zap", "⚡")
    em_chk = em_tag("check", "🟢")
    em_sup = em_tag("support", "💬")

    sub = db.get_subscriber(user_id)
    is_vip = sub and sub.get("status") == "active"
    tier_label = f"👑 VIP ({sub['plan_name']})" if is_vip else "⭐ Free Guest"
    status_text = "Active & Verified" if is_vip else "24/7 Instant Auto-Delivery"
    box_border = "━━━━━━━━━━━━━━━━━━━━"
    preview_url = VIP_PREVIEW_LINK or "https://t.me/PAV_CourseEdition/16"

    welcome_text = (
        f"{em_crw} <b>{PRODUCT_NAME} — VIP HUB</b> {em_dia}\n"
        f"<i>স্বাগতম, {html.escape(user.first_name or 'VIP Member')}! এক্সক্লুসিভ কোর্স ভল্ট ও প্রিমিয়াম হাব।</i>\n\n"
        f"{box_border}\n"
        f"{em_usr} <b>Customer ID:</b> <code>#{user_id}</code>\n"
        f"{em_shd} <b>Account Tier:</b> <code>{tier_label}</code>\n"
        f"{em_chk} <b>Service Status:</b> <code>{status_text}</code>\n"
        f"{em_zap} <b>Delivery:</b> <code>Anti-Leak 1-Time Invite Links</code>\n"
        f"{box_border}\n\n"
        f"🏛️ <b>VIP Vault Services & Benefits:</b>\n"
        f"• {em_crs} <b>Courses:</b> লাইফটাইম ক্লাউড ড্রাইভ ও সিক্রেট মেথডস\n"
        f"• {em_chn} <b>VIP Channels:</b> প্রতিদিন নতুন কোর্স রিলিজ ও সফটওয়্যার\n"
        f"• {em_grp} <b>VIP Groups:</b> সিক্রেট ডিসকাশন ও স্টুডেন্ট নেটওয়ার্ক\n"
        f"• {em_sup} <b>Live Support:</b> ২৪/৭ ডিরেক্ট অ্যাডমিন অ্যাসিস্ট্যান্স\n"
        f"• 👁️ <b>Live Proof:</b> <a href='{preview_url}'>কোর্স ডেমো ও প্রুফ দেখতে এখানে ক্লিক করুন</a>\n\n"
        f"⚠️ <b>প্রাইস-লক অফার:</b> প্রতি মাসে মূল্য বৃদ্ধি পাবে। আজ যে মূল্যে জয়েন করবেন, একটানা রিনিউ রাখলে আজীবন সেই মূল্যেই রিনিউ করতে পারবেন (Price-Lock Guarantee)!\n\n"
        f"👉 <i>নিচের বাটনগুলো ব্যবহার করে প্ল্যান ব্রাউজ বা অ্যাকাউন্ট পরিচালনা করুন:</i>"
    )

    keyboard = [
        [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
        [InlineKeyboardButton("⚠️ Important Rules & Preview 📖", url=preview_url)],
    ]
    if is_vip:
        keyboard.append([InlineKeyboardButton("🔓 Access VIP Channels & Groups 🚀", callback_data="get_my_links")])

    keyboard.append([
        InlineKeyboardButton("👤 My Subscription", callback_data="user_status"),
        InlineKeyboardButton("💳 Payment Methods", callback_data="payment_info"),
    ])
    keyboard.append([InlineKeyboardButton("🤝 Affiliate & Reseller Hub", callback_data="affiliate_menu")])

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await update.callback_query.message.reply_text(
                    text=welcome_text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
    elif update.message:
        try:
            await update.message.reply_text(
                f"🏛️ <b>{PRODUCT_NAME}</b>\n<i>Navigation menu loaded below.</i>",
                parse_mode="HTML",
                reply_markup=get_persistent_menu(is_admin(user_id), is_vip_user=is_vip),
            )
        except Exception:
            pass

        await update.message.reply_text(
            text=welcome_text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome dashboard with plan details, self-service checks, and admin link."""
    user = update.effective_user
    user_id = user.id
    logger.info(f"🚀 /start command received from user {user_id} (@{user.username})")

    # Check for referral deep-link (?start=ref_<USER_ID>)
    if context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.replace("ref_", ""))
                if ref_id != user_id:
                    recorded = db.record_lead(user_id=user_id, referrer_id=ref_id)
                    if recorded:
                        try:
                            lead_user_name = html.escape(user.username or user.first_name or "VIP Member")
                            lead_msg = (
                                f"🔔 <b>New Referral Lead!</b>\n\n"
                                f"👤 একজন নতুন ইউজার (@{lead_user_name}, ID: <code>{user_id}</code>) "
                                f"আপনার রেফারেল লিংক ব্যবহার করে বটে প্রবেশ করেছেন।\n"
                                f"🎯 ইউজার কোনো VIP প্ল্যান পারচেজ করলেই আপনার অ্যাকাউন্টে রিওয়ার্ড যুক্ত হবে!"
                            )
                            await context.bot.send_message(chat_id=ref_id, text=lead_msg, parse_mode="HTML")
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Error handling ref deep link: {e}")

    # Enforce mandatory channel join
    is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
    if not is_subbed:
        await send_force_sub_message(update, context, unjoined_link, unjoined_name)
        return

    # Check for direct section routing (?start=plans, ?start=vip, ?start=pricing)
    if context.args and len(context.args) > 0:
        arg_lower = context.args[0].strip().lower()
        if arg_lower in ("plans", "vip", "buy", "pricing"):
            await render_plans_menu(update, context)
            return

    await render_main_menu(update, context)


async def cmd_myplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks the user's active plan and remaining duration."""
    user_id = update.effective_user.id

    # Enforce mandatory channel join
    is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
    if not is_subbed:
        await send_force_sub_message(update, context, unjoined_link, unjoined_name)
        return

    sub = db.get_subscriber(user_id)
    em_crw = em_tag("crown", "👑")
    em_dia = em_tag("diamond", "💎")
    em_usr = em_tag("profile", "👤")
    em_shd = em_tag("verify", "🛡️")
    em_crs = em_tag("cross", "❌")
    em_chk = em_tag("check", "🟢")
    em_zap = em_tag("zap", "⚡")
    em_ord = em_tag("orders", "📦")
    box_border = "━━━━━━━━━━━━━━━━━━━━"

    if not sub or sub["status"] != "active":
        text = (
            f"{em_crs} <b>আপনার কোনো অ্যাক্টিভ VIP মেম্বারশিপ নেই!</b>\n\n"
            f"{box_border}\n"
            f"{em_usr} <b>Customer ID:</b> <code>#{user_id}</code>\n"
            f"{em_shd} <b>Account Tier:</b> <code>Unverified Guest</code>\n"
            f"{em_ord} <b>Active Plan:</b> <code>None</code>\n"
            f"{box_border}\n\n"
            f"🏛️ <b>{PRODUCT_NAME}</b> এর আনলিমিটেড কোর্স এক্সেস পেতে নিচের বাটনে ক্লিক করে আপনার পছন্দের প্ল্যানটি বেছে নিন।"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 View Plans", callback_data="plans_menu")]])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif update.message:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return

    # User has active plan
    now = datetime.datetime.now(datetime.timezone.utc)
    if sub["is_lifetime"]:
        time_left = f"{em_crw} <b>Lifetime Unlimited</b> (Never Expires)"
        expiry_str = "চিরস্থায়ী এক্সেস"
    else:
        expiry_dt = datetime.datetime.fromisoformat(sub["expires_at"])
        expiry_str = expiry_dt.strftime("%d %b %Y, %I:%M %p (UTC)")
        diff = expiry_dt - now

        if diff.total_seconds() > 0:
            days = diff.days
            hours, remainder = divmod(diff.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            time_left = f"⏳ <b>{days} দিন, {hours} ঘণ্টা, {minutes} মিনিট বাকি</b>"
        else:
            time_left = "⚠️ <i>Expired (Awaiting Auto-Kick)</i>"

    text = (
        f"{em_dia} <b>আপনার VIP মেম্বারশিপ বিস্তারিত:</b> {em_crw}\n"
        f"{box_border}\n"
        f"{em_usr} <b>Customer ID:</b> <code>#{user_id}</code>\n"
        f"{em_shd} <b>Account Tier:</b> <code>👑 Verified VIP Member</code>\n"
        f"{em_ord} <b>Active Plan:</b> <b>{sub['plan_name']}</b>\n"
        f"📅 <b>মেয়াদ শেষ:</b> <code>{expiry_str}</code>\n"
        f"⏱️ <b>বাকি সময়:</b> {time_left}\n"
        f"{em_chk} <b>স্ট্যাটাস:</b> <code>Active & Protected</code>\n"
        f"{box_border}\n\n"
        f"<i>মেয়াদ শেষ হওয়ার আগেই রিনিউ করে নিরবচ্ছিন্ন এক্সেস বজায় রাখুন।</i>"
    )

    all_joined, _ = await is_user_in_all_channels(context.bot, user_id)
    if not is_admin(user_id) and all_joined and CHANNELS:
        link_btn = InlineKeyboardButton("✅ VIP Channels (Already Joined)", callback_data="already_joined_alert")
    else:
        link_btn = InlineKeyboardButton("🔗 Access VIP Channels & Groups", callback_data="get_my_links")

    markup = InlineKeyboardMarkup([
        [link_btn],
        [InlineKeyboardButton("🔄 Renew Subscription", callback_data="user_renew")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows active VIP members to retrieve or check their 1-time channel invite links."""
    user_id = update.effective_user.id
    sub = db.get_subscriber(user_id)
    if not sub or sub.get("status") != "active":
        if update.callback_query:
            await update.callback_query.answer("❌ আপনার কোনো সক্রিয় VIP মেম্বারশিপ নেই!", show_alert=True)
        elif update.message:
            await update.message.reply_text(
                "❌ <b>আপনার কোনো সক্রিয় VIP মেম্বারশিপ নেই!</b>\n\nপ্ল্যান দেখতে /plans লিখুন।",
                parse_mode="HTML"
            )
        return

    # Check if user is already inside all channels (Admins exempt for testing)
    if not is_admin(user_id):
        all_joined, _ = await is_user_in_all_channels(context.bot, user_id)
        if all_joined and CHANNELS:
            already_text = (
                f"🔒 <b>VIP এক্সেস সক্রিয় ও সুরক্ষিত</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>আপনি ইতিমধ্যেই আমাদের সব VIP চ্যানেল ও গ্রুপে যুক্ত আছেন!</b>\n\n"
                f"🛡️ <b>নিরাপত্তা সতর্কতা:</b>\n"
                f"লিংক কপি বা অন্য কারো সাথে শেয়ারিং সম্পূর্ণ নিষিদ্ধ থাকায় নতুন কোনো ইনভাইট লিংক তৈরি হবে না। সরাসরি আপনার চ্যাট লিস্ট থেকে গ্রুপে প্রবেশ করুন।"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
                [InlineKeyboardButton("💬 Contact Support", url="https://t.me/proaccessteam")],
            ])
            if update.callback_query:
                await update.callback_query.answer("আপনি ইতিমধ্যে যুক্ত আছেন! লিংক শেয়ারিং লক করা হয়েছে।", show_alert=True)
                if update.callback_query.message:
                    try:
                        await update.callback_query.edit_message_text(already_text, parse_mode="HTML", reply_markup=markup)
                    except Exception:
                        await context.bot.send_message(chat_id=user_id, text=already_text, parse_mode="HTML", reply_markup=markup)
            elif update.message:
                await update.message.reply_text(already_text, parse_mode="HTML", reply_markup=markup)
            return

    # User has not joined all channels yet! Deliver the 1-time single-use join links with direct buttons
    if update.callback_query:
        await update.callback_query.answer("আপনার ওয়ান-টাইম সিকিউর জয়েন লিংক পাঠানো হচ্ছে...")

    await deliver_welcome_access(
        context.bot,
        user_id,
        sub["plan_name"],
        sub["expires_at"],
        sub["is_lifetime"]
    )


# =============================================================================
# PLANS & PAYMENT INTERACTIVE FLOW
# =============================================================================
async def render_plans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    em_crw = em_tag("crown", "👑")
    em_dia = em_tag("diamond", "💎")
    em_fire = em_tag("fire", "🔥")
    em_zap = em_tag("zap", "⚡")
    em_ord = em_tag("orders", "📦")
    box_border = "━━━━━━━━━━━━━━━━━━━━"

    p_mon = PLANS.get("monthly", {"price_bdt": 1000})
    p_yr = PLANS.get("yearly", {"price_bdt": 5000})
    p_life = PLANS.get("lifetime", {"price_bdt": 8000})

    mon_bdt = p_mon.get("price_bdt", 1000)
    yr_bdt = p_yr.get("price_bdt", 5000)
    life_bdt = p_life.get("price_bdt", 8000)

    rules_text = get_rules_text()
    preview_url = VIP_PREVIEW_LINK or "https://t.me/PAV_CourseEdition/16"

    text = (
        f"{em_crw} <b>{PRODUCT_NAME} — VIP PLANS & PRICING</b> {em_dia}\n"
        f"{box_border}\n"
        f"আপনার পছন্দের প্রিমিয়াম প্ল্যানটি বেছে নিন:\n\n"
        f"<b>১. 📅 Monthly VIP (30 Days)</b>\n"
        f"• ফুল কোর্স ভল্ট ও ডিসকাশন গ্রুপ এক্সেস\n"
        f"• ইনভেস্টমেন্ট: <b>{format_price(mon_bdt)}</b> <i>(Price-Locked)</i>\n\n"
        f"<b>২. 📆 Yearly VIP (365 Days)</b>\n"
        f"• ৩৬৫ দিন নিরবচ্ছিন্ন আপডেট ও প্রায় ৬০% সেভিংস {em_fire}\n"
        f"• ইনভেস্টমেন্ট: <b>{format_price(yr_bdt)}</b>\n\n"
        f"<b>৩. 👑 Lifetime Access (Forever VIP)</b>\n"
        f"• একবার পেমেন্টে আজীবনের জন্য ফুল ফ্রি এক্সেস {em_zap}\n"
        f"• ইনভেস্টমেন্ট: <b>{format_price(life_bdt)}</b> <i>(এককালীন দিলে রিনিউয়ের ঝামেলাই নেই!)</i>\n"
        f"{box_border}\n\n"
        f"{rules_text}\n\n"
        f"👉 <i>যে প্ল্যানটি নিতে চান নিচের বাটনে ট্যাপ করুন:</i>"
    )

    keyboard = [
        [InlineKeyboardButton("⚠️ Important: Read Rules & Preview (Post #16) 📖", url=preview_url)],
        [InlineKeyboardButton(f"📅 Monthly VIP ({format_btn_price(mon_bdt)})", callback_data="buy_monthly")],
        [InlineKeyboardButton(f"📆 Yearly VIP ({format_btn_price(yr_bdt)})", callback_data="buy_yearly")],
        [InlineKeyboardButton(f"👑 Lifetime VIP ({format_btn_price(life_bdt)})", callback_data="buy_lifetime")],
        [
            InlineKeyboardButton("💳 Payment Methods", callback_data="payment_info"),
            InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu"),
        ],
    ]

    if update.callback_query:
        msg = update.callback_query.message
        if msg and (msg.photo or msg.document):
            try:
                await msg.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
            return
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception:
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    else:
        await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)


async def send_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the official payment guide and credentials card."""
    bkash_num = PAYMENT_INFO.get("bkash", "01718539406")
    nagad_num = PAYMENT_INFO.get("nagad", "01718539406")
    binance_id = PAYMENT_INFO.get("binance_pay_id", "892612368")
    usdt_addr = PAYMENT_INFO.get("usdt_trc20", "TNPg8qkhm1xQsMNsgvH6p71tNwVXV7qaJe")

    text = (
        f"{em_tag('wallet', '💳')} <b>অফিশিয়াল পেমেন্ট মেথডস ও গাইড</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{em_tag('bkash', '📱')} <b>bKash (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{bkash_num}</code>\n\n"
        f"{em_tag('nagad', '📱')} <b>Nagad (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{nagad_num}</code>\n\n"
        f"⚠️ <b>জরুরি সতর্কতা:</b>\n"
        f"✔️ <b>bkash, Nagad Cashout Only (Agent)</b>\n"
        f"👆 <b>শুধুমাত্র ক্যাশআউট করবেন</b> (Send Money গ্রহণযোগ্য নয়)।\n\n"
        f"{em_tag('binance', '🌐')} <b>Binance Pay ID:</b>\n"
        f"👉 <code>{binance_id}</code>\n\n"
        f"{em_tag('usdt', '💲')} <b>Usdt Trx (Trc20):</b>\n"
        f"👉 <code>{usdt_addr}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"➡️ <b>Send ScreenShot Must</b>\n"
        f"টাকা পাঠানোর পর '👑 Select Plan & Pay' তে গিয়ে প্ল্যান সিলেক্ট করে স্ক্রিনশট ও Trx ID সাবমিট করুন।"
    )
    preview_url = VIP_PREVIEW_LINK or "https://t.me/PAV_CourseEdition/16"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Select Plan & Pay", callback_data="plans_menu")],
        [InlineKeyboardButton("⚠️ Important Rules & Preview 📖", url=preview_url)],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])
    if update.callback_query:
        msg = update.callback_query.message
        if msg and (msg.photo or msg.document):
            try:
                await msg.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=markup)
            return
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await context.bot.send_message(chat_id=update.effective_user.id, text=text, parse_mode="HTML", reply_markup=markup)


async def send_official_channel_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the official announcement channel details."""
    channel_link = FORCE_CHANNEL_LINK or "https://t.me/PAV_CourseEdition"
    channel_name = FORCE_CHANNEL_NAME or "Pro Access Vault - Course Edition"
    text = (
        f"📢 <b>{channel_name}</b>\n\n"
        f"আমাদের অফিশিয়াল চ্যানেলে যুক্ত থাকুন সব সর্বশেষ কোর্স রিলিজ, সিক্রেট মেথড আপডেট এবং স্পেশাল অফার পেতে।\n\n"
        f"🔗 <b>অফিশিয়াল লিংক:</b> {channel_link}"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Open Official Channel", url=channel_link)],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def broadcast_vip_join(
    bot: Bot,
    buyer_id: int,
    buyer_username: Optional[str],
    buyer_name: Optional[str],
    plan_name: str,
):
    """
    Dispatches a high-converting social-proof announcement when a new VIP member enrolls.
    Target: Public announcement/preview channel or bot users (if configured).
    """
    enabled = db.get_setting("vip_join_broadcast_enabled", "1") == "1"
    if not enabled:
        return

    # Mask name for privacy & premium feel
    clean_name = buyer_name or "VIP Member"
    parts = clean_name.split()
    if len(parts) > 1:
        masked_name = f"{parts[0]} {parts[1][0]}."
    else:
        masked_name = clean_name

    handle_str = f" (@{buyer_username})" if buyer_username else ""

    # Telegram Premium Animated Custom Emojis (<tg-emoji emoji-id="...">char</tg-emoji>)
    em_fire = em_tag("fire", "🔥")
    em_crw = em_tag("crown", "👑")
    em_dia = em_tag("diamond", "💎")
    em_zap = em_tag("zap", "⚡")
    em_chk = em_tag("check", "✅")
    em_box = em_tag("box", "📦")
    em_spk = em_tag("sparkle", "✨")
    em_clm = em_tag("free_claim", "🎉")
    em_ver = em_tag("verify", "🛡️")
    box_border = "━━━━━━━━━━━━━━━━━━━━"

    text = (
        f"{em_fire} <b>NEW VIP MEMBER ENROLLED!</b> {em_zap}\n"
        f"{box_border}\n"
        f"{em_clm} অভিনন্দন <b>{html.escape(masked_name)}</b>{handle_str}!\n"
        f"{em_box} <b>এনরোল্ড প্ল্যান:</b> <b>{html.escape(plan_name)}</b>\n"
        f"{em_dia} <b>স্ট্যাটাস:</b> <code>Full Vault Access Unlocked {em_chk}</code>\n"
        f"{box_border}\n\n"
        f"{em_ver} <b>{PRODUCT_NAME}</b>-এর প্রিমিয়াম কোর্স, সিক্রেট মেথড ও লাইফটাইম রিসোর্সে যুক্ত হতে আপনিও আজই আপনার স্লট নিশ্চিত করুন!\n\n"
        f"{em_spk} <i>বর্তমান অফার মূল্যে জয়েন করতে অথবা ফ্রি প্রিভিউ দেখতে নিচের বাটনে ক্লিক করুন:</i>\n"
    )

    try:
        bot_info = await bot.get_me()
        bot_uname = bot_info.username or "PAV_CourseEdition_bot"
    except Exception:
        bot_uname = "PAV_CourseEdition_bot"

    bot_start_url = f"https://t.me/{bot_uname}?start=plans"
    preview_url = VIP_PREVIEW_LINK or "https://t.me/PAV_CourseEdition/16"

    # In Telegram channels, inline buttons MUST be URL buttons.
    # Button 1 deep-links into bot's VIP Plans & Pricing menu (?start=plans).
    # Button 2 links directly to Post #16 (VIP Preview Video & Proof).
    buttons = [
        [InlineKeyboardButton("👑 Get VIP Access / Buy Now 🚀", url=bot_start_url)],
        [InlineKeyboardButton("👁️ Free VIP Preview & Proof 🎬", url=preview_url)],
    ]
    markup = InlineKeyboardMarkup(buttons)

    # 1. Post to Announcement / Force Channel if configured
    target_ch = FORCE_CHANNEL_ID or db.get_setting("vip_broadcast_channel_id")
    if target_ch:
        try:
            await bot.send_message(
                chat_id=target_ch,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            logger.info(f"VIP Join Broadcast posted to channel {target_ch}")
        except Exception as e:
            logger.warning(f"Could not post VIP join broadcast to channel {target_ch}: {e}")

    # 2. Also log to Log Channel if configured
    log_ch = CONFIG.get("log_channel_id")
    if log_ch and str(log_ch) != str(target_ch):
        try:
            await bot.send_message(
                chat_id=log_ch,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


async def handle_buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    plan = PLANS.get(plan_key)
    if not plan:
        return

    bkash_num = PAYMENT_INFO.get("bkash", "01718539406")
    nagad_num = PAYMENT_INFO.get("nagad", "01718539406")
    binance_id = PAYMENT_INFO.get("binance_pay_id", "892612368")
    usdt_addr = PAYMENT_INFO.get("usdt_trc20", "TNPg8qkhm1xQsMNsgvH6p71tNwVXV7qaJe")

    preview_url = VIP_PREVIEW_LINK or "https://t.me/PAV_CourseEdition/16"

    text = (
        f"{em_tag('wallet', '💳')} <b>পেমেন্ট ইনস্ট্রাকশন: {plan['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{em_tag('cash', '💰')} <b>প্রদেয় মূল্য:</b> <b>{format_price(plan['price_bdt'])}</b>\n\n"
        f"{em_tag('bkash', '📱')} <b>bKash (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{bkash_num}</code>\n"
        f"<i>(নম্বরে ট্যাপ করলেই কপি হয়ে যাবে)</i>\n\n"
        f"{em_tag('nagad', '📱')} <b>Nagad (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{nagad_num}</code>\n"
        f"<i>(নম্বরে ট্যাপ করলেই কপি হয়ে যাবে)</i>\n\n"
        f"⚠️ <b>জরুরি সতর্কতা:</b>\n"
        f"✔️ <b>bKash, Nagad Cashout Only (Agent)</b>\n"
        f"👆 <b>শুধুমাত্র ক্যাশআউট করবেন</b> (Send Money গ্রহণযোগ্য নয়)।\n\n"
        f"{em_tag('binance', '🌐')} <b>Binance Pay ID:</b>\n"
        f"👉 <code>{binance_id}</code>\n"
        f"<i>(আইডিতে ট্যাপ করলেই কপি হয়ে যাবে)</i>\n\n"
        f"{em_tag('usdt', '💲')} <b>USDT Trx (TRC20):</b>\n"
        f"👉 <code>{usdt_addr}</code>\n"
        f"<i>(অ্যাড্রেসে ট্যাপ করলেই কপি হয়ে যাবে)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>জরুরি নিয়মাবলী ও প্রাইস-লক পলিসি:</b>\n"
        f"• 🔒 <b>প্রাইস-লক:</b> আজ যে মূল্যে নিচ্ছেন, একটানা রিনিউ রাখলে আজীবন এই রেট থাকবে। কিন্তু মেয়াদ শেষ হয়ে গ্যাপ পড়লে প্রাইজ-লক বাতিল হবে!\n"
        f"• 🚫 <b>সিঙ্গেল-ইউজ লিংক:</b> ১টি লিংক ১ জনের জন্য। শেয়ার বা লিক করলে কোনো নোটিস ছাড়াই পার্মানেন্ট ব্যান।\n"
        f"• ❌ <b>নো-রিফান্ড:</b> ডিজিটাল ভল্ট অ্যাক্সেস ডেলিভারির পর কোনো রিফান্ড প্রযোজ্য নয়।\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"➡️ <b>Send ScreenShot Must</b>\n"
        f"ক্যাশআউট বা ট্রান্সফার করার পর স্ক্রিনশট বা Trx ID পাঠানো বাধ্যতামূলক।\n\n"
        f"👇 নিচের <b>[ ✍️ Submit Trx ID / Proof ]</b> বাটনে ক্লিক করে প্রুফ পাঠান:"
    )

    keyboard = [
        [InlineKeyboardButton("✍️ Submit Trx ID / Proof", callback_data=f"submit_trx_{plan_key}")],
        [InlineKeyboardButton("⚠️ Read Important Rules & Preview 📖", url=preview_url)],
        [
            InlineKeyboardButton("👑 Other Plans", callback_data="plans_menu"),
            InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"),
        ]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def start_trx_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except Exception:
        pass

    USER_STATES[user_id] = {
        "action": "awaiting_trx",
        "plan_key": plan_key,
    }

    text = (
        f"✍️ <b>পেমেন্ট প্রুফ ও Trx ID সাবমিট করুন:</b>\n\n"
        f"📸 <b>পেমেন্টের স্ক্রিনশট (Screenshot)</b> অথবা <b>Trx ID / যে নাম্বার থেকে ক্যাশআউট করেছেন</b> তা এখানে সরাসরি লিখে বা ফটো আকারে পাঠান।\n\n"
        f"➡️ <b>Send ScreenShot Must</b>\n\n"
        f"<i>(বাতিল করতে চাইলে /cancel লিখুন অথবা নিচের বাটনে চাপ দিন)</i>"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back to Plans", callback_data="plans_menu")]])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_user_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text submissions and payment screenshots (like TrxID or Admin inputs)."""
    global VIP_PREVIEW_LINK, VIP_PREVIEW_NAME
    user = update.effective_user
    user_id = user.id
    msg_text = update.message.text.strip() if update.message.text else ""

    logger.info(f"💬 MESSAGE [User: {user_id} (@{user.username})]: '{msg_text}'")

    if not is_admin(user_id):
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if not is_subbed:
            await send_force_sub_message(update, context, unjoined_link, unjoined_name)
            return

    # Persistent Bottom Menu Bar Intercepts (Always Active with or without emojis)
    clean_msg = strip_all_emojis(msg_text).strip() if msg_text else ""
    if clean_msg in ("VIP Plans & Pricing", "VIP Plans") or msg_text in ("👑 VIP Plans & Pricing", "👑 VIP Plans", "/plans"):
        USER_STATES.pop(user_id, None)
        await render_plans_menu(update, context)
        return
    if clean_msg in ("Payment Methods", "Payment Info", "Payment Methods & Info") or msg_text in ("💳 Payment Methods", "💳 Payment Info", "/payment"):
        USER_STATES.pop(user_id, None)
        await send_payment_info(update, context)
        return
    if clean_msg in ("My Subscription", "My Plan", "My Subscription Status") or msg_text in ("👤 My Subscription", "👤 My Plan", "/myplan"):
        USER_STATES.pop(user_id, None)
        await cmd_myplan(update, context)
        return
    if clean_msg in ("Access VIP Channels", "Access VIP Channels & Groups") or msg_text in ("🔗 Access VIP Channels", "🔗 Access VIP Channels & Groups", "/links"):
        USER_STATES.pop(user_id, None)
        await cmd_links(update, context)
        return
    if clean_msg in ("Official Channel", "Channel") or msg_text in ("📢 Official Channel", "📢 Channel"):
        USER_STATES.pop(user_id, None)
        await send_official_channel_info(update, context)
        return
    if clean_msg in ("Affiliate Hub", "Affiliate & Reseller Hub", "Affiliate") or msg_text in ("🤝 Affiliate Hub", "🤝 Affiliate & Reseller Hub", "/affiliate", "/ref"):
        USER_STATES.pop(user_id, None)
        await render_affiliate_hub(update, context)
        return
    if clean_msg in ("Admin Control Panel", "Admin Panel") or msg_text in ("⚙️ Admin Control Panel", "⚙️ Admin Panel", "/admin"):
        USER_STATES.pop(user_id, None)
        if is_admin(user_id):
            await render_admin_dashboard(update, context)
        else:
            await update.message.reply_text("⛔ You are not authorized to access the Admin Control Panel.")
        return

    state = USER_STATES.get(user_id)

    if not state:
        return

    action = state.get("action")

    # 1. User Submitting TrxID or Screenshot
    if action == "awaiting_trx":
        photo_file_id = None
        doc_file_id = None
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            trx_id = update.message.caption.strip() if update.message.caption else "📸 Screenshot Attached"
        elif update.message.document:
            doc_file_id = update.message.document.file_id
            trx_id = update.message.caption.strip() if update.message.caption else "📄 Document Attached"
        else:
            trx_id = update.message.text.strip() if update.message.text else "N/A"

        plan_key = state.get("plan_key")
        is_wholesale = plan_key.startswith("ws_") if plan_key else False
        base_plan_key = plan_key.replace("ws_", "") if is_wholesale else plan_key
        plan = PLANS.get(base_plan_key, {"name": base_plan_key, "price_bdt": 0, "price_usdt": 0})

        USER_STATES.pop(user_id, None)

        if is_wholesale:
            ws_discount = float(db.get_setting("affiliate_wholesale_discount_pct", "50"))
            ws_bdt = max(1, round(plan.get("price_bdt", 0) * ((100 - ws_discount) / 100.0)))
            ws_usdt = calc_usd(ws_bdt)
            amount_display = f"{format_price(ws_bdt, ws_usdt)} (Wholesale {int(ws_discount)}% Off)"
            plan_display_name = f"{plan.get('name')} (Wholesale)"
        else:
            plan_bdt = plan.get('price_bdt', 0)
            amount_display = format_price(plan_bdt)
            plan_display_name = plan.get("name")

        # Create record in DB
        req_id = db.create_payment_request(
            user_id=user_id,
            username=user.username,
            full_name=user.full_name,
            plan_key=plan_key,
            amount=amount_display,
            payment_method="Agent Cashout / Crypto",
            trx_id=trx_id,
        )

        safe_trx = html.escape(trx_id)
        safe_full_name = html.escape(user.full_name or "VIP Member")
        safe_username = html.escape(user.username or "none")

        await update.message.reply_text(
            f"✅ <b>পেমেন্ট রিকোয়েস্ট সাবমিট হয়েছে! (ID: #{req_id})</b>\n\n"
            f"📦 <b>প্ল্যান:</b> {plan_display_name}\n"
            f"🔢 <b>প্রুফ / Trx ID:</b> <code>{safe_trx}</code>\n\n"
            f"অ্যাডমিন ভেরিফাই করা মাত্রই আপনার ইনবক্সে ওয়ান-টাইম জয়েন লিংক চলে আসবে।",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
        )

        # Alert Admins with Interactive Approval Card
        admin_card = (
            f"💳 <b>[{'WHOLESALE CLIENT ORDER' if is_wholesale else 'NEW PAYMENT SUBMISSION'} #{req_id}]</b>\n\n"
            f"👤 <b>{'Reseller' if is_wholesale else 'User'}:</b> <a href='tg://user?id={user_id}'>{safe_full_name}</a> (@{safe_username})\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📦 <b>Plan:</b> {plan_display_name}\n"
            f"💰 <b>Amount:</b> {amount_display}\n"
            f"🔢 <b>Trx ID / Note:</b> <code>{safe_trx}</code>"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{req_id}"),
            ]
        ])

        for adm in ADMIN_IDS:
            try:
                if photo_file_id:
                    await context.bot.send_photo(
                        chat_id=adm,
                        photo=photo_file_id,
                        caption=admin_card,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                elif doc_file_id:
                    await context.bot.send_document(
                        chat_id=adm,
                        document=doc_file_id,
                        caption=admin_card,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                else:
                    await context.bot.send_message(chat_id=adm, text=admin_card, parse_mode="HTML", reply_markup=markup)
            except Exception as e:
                logger.error(f"Failed to alert admin {adm}: {e}")
        return

    # 2. Admin Interactive Prompts
    if action == "admin_awaiting_add_id":
        target_id_str = update.message.text.strip()
        if not target_id_str.isdigit():
            await update.message.reply_text("❌ Invalid Telegram User ID. Must be numeric. Try again or /cancel.")
            return

        state["target_user_id"] = int(target_id_str)
        state["action"] = "admin_awaiting_add_duration"

        buttons = [
            [
                InlineKeyboardButton("📅 30 Days", callback_data="adm_quick_dur_30d"),
                InlineKeyboardButton("📆 1 Year", callback_data="adm_quick_dur_365d"),
            ],
            [
                InlineKeyboardButton("👑 Lifetime", callback_data="adm_quick_dur_lifetime"),
                InlineKeyboardButton("⏱️ Custom Time", callback_data="adm_quick_dur_custom"),
            ]
        ]
        await update.message.reply_text(
            f"👤 Target User ID: <code>{target_id_str}</code>\n"
            f"Select duration or write custom time (e.g. <code>15d</code>, <code>45d</code>, <code>12h</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "admin_awaiting_custom_duration":
        dur_str = update.message.text.strip()
        target_id = state.get("target_user_id")
        USER_STATES.pop(user_id, None)

        try:
            seconds, label, is_lifetime = parse_duration(dur_str)
            sub = db.add_or_update_subscriber(
                user_id=target_id,
                username=None,
                full_name=None,
                plan_key="custom",
                plan_name=label,
                duration_seconds=seconds,
                is_lifetime=is_lifetime,
            )
            # Deliver links to user
            await deliver_welcome_access(context.bot, target_id, label, sub["expires_at"], is_lifetime)
            await update.message.reply_text(
                f"✅ <b>Successfully Added Member!</b>\n"
                f"🆔 User ID: <code>{target_id}</code>\n"
                f"📦 Plan: <b>{label}</b>\n"
                f"⏳ Expiry: <code>{sub['expires_at']}</code>\n"
                f"Links delivered to user in DM.",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error adding user: {e}")
        return

    # 3. Admin Awaiting Renew ID
    if action == "admin_awaiting_renew_id":
        target_id_str = update.message.text.strip()
        if not target_id_str.isdigit():
            await update.message.reply_text("❌ Invalid Telegram User ID. Must be numeric. Try again or /cancel.")
            return

        target_id = int(target_id_str)
        sub = db.get_subscriber(target_id)
        if not sub:
            await update.message.reply_text(f"❌ User <code>{target_id}</code> not found in database. Use Add Member instead.", parse_mode="HTML")
            USER_STATES.pop(user_id, None)
            return

        state["target_user_id"] = target_id
        state["action"] = "admin_awaiting_renew_duration"

        buttons = [
            [
                InlineKeyboardButton("+30 Days", callback_data="adm_quick_renew_30d"),
                InlineKeyboardButton("+1 Year", callback_data="adm_quick_renew_365d"),
            ],
            [
                InlineKeyboardButton("👑 Lifetime", callback_data="adm_quick_renew_lifetime"),
                InlineKeyboardButton("⏱️ Custom Time", callback_data="adm_quick_renew_custom"),
            ]
        ]
        await update.message.reply_text(
            f"🔄 <b>Renewing Member:</b> <code>{target_id}</code>\n"
            f"Current Plan: <b>{sub['plan_name']}</b> | Status: <b>{sub['status']}</b>\n"
            f"Select duration extension to add:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "admin_awaiting_custom_renew_duration":
        dur_str = update.message.text.strip()
        target_id = state.get("target_user_id")
        USER_STATES.pop(user_id, None)

        try:
            seconds, label, is_lifetime = parse_duration(dur_str)
            sub = db.renew_subscriber(
                user_id=target_id,
                duration_seconds=seconds,
                new_plan_name=label,
                is_lifetime=is_lifetime,
            )
            try:
                renew_msg = (
                    f"🎉 <b>আপনার সাবস্ক্রিপশন সফলভাবে রিনিউ হয়েছে!</b>\n\n"
                    f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
                    f"➕ <b>বর্ধিত সময়:</b> +{label}\n"
                    f"⏳ <b>নতুন মেয়াদ শেষ:</b> <code>{sub['expires_at']}</code>"
                )
                await context.bot.send_message(chat_id=target_id, text=renew_msg, parse_mode="HTML")
            except Exception:
                pass

            await update.message.reply_text(
                f"✅ <b>Member Renewed Successfully!</b>\n"
                f"🆔 User ID: <code>{target_id}</code>\n"
                f"➕ Added: <b>+{label}</b>\n"
                f"⏳ New Expiry: <code>{sub['expires_at']}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error renewing user: {e}")
        return

    # 4. Admin Awaiting Kick ID
    if action == "admin_awaiting_kick_id":
        target_id_str = update.message.text.strip()
        USER_STATES.pop(user_id, None)
        if not target_id_str.isdigit():
            await update.message.reply_text("❌ Invalid Telegram User ID. Must be numeric.")
            return

        target_id = int(target_id_str)
        kicked_count = 0
        for ch in CHANNELS:
            ch_id = ch["id"]
            try:
                await context.bot.ban_chat_member(chat_id=ch_id, user_id=target_id)
                await context.bot.unban_chat_member(chat_id=ch_id, user_id=target_id)
                kicked_count += 1
            except Exception:
                pass

        db.mark_removed(target_id, reason="admin_gui_kick")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"❌ Your access to <b>{PRODUCT_NAME}</b> channels has been revoked by administration.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"🚫 <b>Member Ejected!</b>\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"Ejected from {kicked_count}/{len(CHANNELS)} channels.\n"
            f"Status marked 'removed' in database.",
            parse_mode="HTML",
        )
        return

    # 5. Admin Awaiting Search Query
    if action == "admin_awaiting_search_query":
        query_str = update.message.text.strip()
        USER_STATES.pop(user_id, None)
        results = db.search_subscribers(query_str)
        if not results:
            await update.message.reply_text(f"❌ No subscriber found matching '<code>{query_str}</code>'.", parse_mode="HTML")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        for sub in results[:5]:
            if sub["is_lifetime"]:
                time_left = "👑 Lifetime Unlimited"
            else:
                expiry_dt = datetime.datetime.fromisoformat(sub["expires_at"])
                diff = expiry_dt - now
                time_left = f"{diff.days}d {diff.seconds // 3600}h" if diff.total_seconds() > 0 else "Expired"

            text = (
                f"🔍 <b>SUBSCRIBER DOSSIER</b>\n"
                f"═════════════════════════\n"
                f"🆔 <b>ID:</b> <code>{sub['user_id']}</code>\n"
                f"👤 <b>Name:</b> {sub['full_name'] or 'N/A'}\n"
                f"🏷️ <b>Username:</b> @{sub['username'] or 'N/A'}\n"
                f"📦 <b>Plan:</b> {sub['plan_name']}\n"
                f"🟢 <b>Status:</b> {sub['status'].upper()}\n"
                f"⏱️ <b>Time Remaining:</b> {time_left}\n"
                f"📅 <b>Joined:</b> <code>{sub['joined_at']}</code>\n"
                f"⏳ <b>Expires:</b> <code>{sub['expires_at']}</code>"
            )
            await update.message.reply_text(text, parse_mode="HTML")
        return

    # 6. Admin Awaiting Broadcast Text
    if action == "admin_awaiting_broadcast_text":
        b_text = update.message.text.strip()
        USER_STATES.pop(user_id, None)
        active_subs = db.get_active_subscribers()
        if not active_subs:
            await update.message.reply_text("ℹ️ No active members to broadcast to.")
            return

        progress_msg = await update.message.reply_text(f"📢 Broadcasting to {len(active_subs)} active members...")
        sent_count = 0
        failed_count = 0
        for s in active_subs:
            uid = s["user_id"]
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 <b>[ANNOUNCEMENT]</b>\n\n{b_text}",
                    parse_mode="HTML",
                )
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed_count += 1

        await progress_msg.edit_text(
            f"✅ <b>Broadcast Completed!</b>\n"
            f"✉️ Delivered: <b>{sent_count}</b>\n"
            f"⚠️ Failed: <b>{failed_count}</b>",
            parse_mode="HTML",
        )
        return

    # 7. Admin Awaiting Channel Input
    if action == "admin_awaiting_channel_input":
        text_in = update.message.text.strip()
        USER_STATES.pop(user_id, None)
        parts = text_in.split(maxsplit=1)
        try:
            ch_id = int(parts[0])
            ch_name = parts[1] if len(parts) > 1 else f"Channel {ch_id}"

            for ch in CHANNELS:
                if ch["id"] == ch_id:
                    ch["name"] = ch_name
                    ch["type"] = "channel"
                    save_config()
                    await update.message.reply_text(f"✅ Channel <code>{ch_id}</code> updated to: <b>{ch_name}</b>", parse_mode="HTML")
                    return

            try:
                member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                status_text = "✅ Bot is Admin" if member.status in ("administrator", "creator") else "⚠️ Bot is NOT admin yet!"
            except Exception as e:
                status_text = f"⚠️ Could not verify admin rights: {e}"

            CHANNELS.append({"id": ch_id, "name": ch_name, "type": "channel", "invite_expire_hours": 24})
            save_config()
            db.log_activity(user_id, "CHANNEL_ADDED", f"Channel: {ch_name} ({ch_id})")
            await update.message.reply_text(
                f"✅ <b>VIP Channel Added!</b>\n"
                f"📢 Name: <b>{ch_name}</b>\n"
                f"🆔 ID: <code>{ch_id}</code>\n"
                f"⚙️ Status: {status_text}",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid channel ID. Must be numeric (e.g. -1001234567890).")
        except Exception as e:
            await update.message.reply_text(f"❌ Error adding channel: {e}")
        return

    # 8. Admin Awaiting Group Input
    if action == "admin_awaiting_group_input":
        text_in = update.message.text.strip()
        USER_STATES.pop(user_id, None)
        parts = text_in.split(maxsplit=1)
        try:
            grp_id = int(parts[0])
            grp_name = parts[1] if len(parts) > 1 else f"Group {grp_id}"

            for ch in CHANNELS:
                if ch["id"] == grp_id:
                    ch["name"] = grp_name
                    ch["type"] = "group"
                    save_config()
                    await update.message.reply_text(f"✅ Group <code>{grp_id}</code> updated to: <b>{grp_name}</b>", parse_mode="HTML")
                    return

            try:
                member = await context.bot.get_chat_member(chat_id=grp_id, user_id=context.bot.id)
                status_text = "✅ Bot is Admin in Group" if member.status in ("administrator", "creator") else "⚠️ Bot is NOT admin in group yet!"
            except Exception as e:
                status_text = f"⚠️ Could not verify admin rights: {e}"

            CHANNELS.append({"id": grp_id, "name": grp_name, "type": "group", "invite_expire_hours": 24})
            save_config()
            db.log_activity(user_id, "GROUP_ADDED", f"Group: {grp_name} ({grp_id})")
            await update.message.reply_text(
                f"✅ <b>VIP Discussion Group Added!</b>\n"
                f"👥 Name: <b>{grp_name}</b>\n"
                f"🆔 ID: <code>{grp_id}</code>\n"
                f"⚙️ Status: {status_text}",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid group ID. Must be numeric (e.g. -1001234567890).")
        except Exception as e:
            await update.message.reply_text(f"❌ Error adding group: {e}")
        return

    # 9. Admin Awaiting Plan Price Input
    if action == "admin_awaiting_plan_price":
        text_in = update.message.text.strip()
        state = USER_STATES.get(user_id, {})
        plan_key = state.get("plan_key")
        USER_STATES.pop(user_id, None)

        if not plan_key or plan_key not in PLANS:
            await update.message.reply_text("❌ Session expired or invalid plan. Please try again from /admin.")
            return

        nums = re.findall(r"\d+", text_in)
        if not nums:
            await update.message.reply_text("❌ কোনো সংখ্যা পাওয়া যায়নি! অনুগ্রহ করে সংখ্যা লিখুন (যেমন: 500 অথবা 500 5)।")
            return

        try:
            bdt = int(nums[0])
            usdt = int(nums[1]) if len(nums) > 1 else calc_usd(bdt)

            PLANS[plan_key]["price_bdt"] = bdt
            PLANS[plan_key]["price_usdt"] = usdt
            db.update_plan_price(plan_key, bdt, usdt)
            save_config()
            db.log_activity(user_id, "PRICE_UPDATED", f"Plan {plan_key}: {bdt} BDT / ${usdt} USDT")

            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("💰 Plan Pricing Control", callback_data="adm_action_edit_prices"),
                    InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard"),
                ]
            ])

            await update.message.reply_text(
                f"✅ <b>{PLANS[plan_key]['name']} এর মূল্য সফলভাবে আপডেট হয়েছে!</b>\n\n"
                f"💰 <b>নতুন প্রদেয় মূল্য:</b> <b>{format_price(bdt, usdt)}</b>\n"
                f"💾 <i>ডাটাবেজ ও বটে পার্মানেন্টলি সেভ হয়ে গেছে!</i>",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ এরর: {e}")
        return

    # Admin Awaiting Dollar Exchange Rate Input
    if action == "admin_awaiting_dollar_rate":
        text_in = update.message.text.strip() if update.message.text else ""
        nums = re.findall(r"\d+(?:\.\d+)?", text_in)
        if not nums:
            await update.message.reply_text("❌ অবৈধ রেট! সংখ্যা লিখে পাঠান (যেমন: 125 বা 130)।")
            return

        new_rate = float(nums[0])
        USER_STATES.pop(user_id, None)
        set_dollar_rate(new_rate)
        db.log_activity(user_id, "DOLLAR_RATE_UPDATED", f"1 USD = {new_rate} BDT")

        lines = [
            f"✅ <b>ডলার রেট সফলভাবে আপডেট হয়েছে!</b>\n",
            f"💵 <b>নতুন রেট:</b> <code>1 USD = {new_rate:g} BDT</code>\n",
            f"━━━━━━━━━━━━━━━━━━━━\n",
            f"স্বয়ংক্রিয়ভাবে পরিবর্তিত নতুন প্ল্যান মূল্যসমূহ:\n",
        ]
        for pk in ("monthly", "yearly", "lifetime"):
            pv = PLANS.get(pk)
            if pv:
                lines.append(f"• <b>{pv['name']}:</b> <code>{format_price(pv['price_bdt'])}</code>")
        lines.append("\n💾 <i>ডাটাবেজে পার্মানেন্টলি সেভ হয়েছে এবং সব ইউজার স্ক্রিনে লাইভ হয়ে গেছে!</i>")

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Plan Pricing Control", callback_data="adm_action_edit_prices")],
            [InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard")],
        ])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
        return

    # 11. Admin Awaiting Preview Link Input
    if action == "admin_awaiting_preview_link":
        text_in = update.message.text.strip()
        USER_STATES.pop(user_id, None)

        if text_in.lower() in ("off", "disable", "none", "remove", "clear"):
            VIP_PREVIEW_LINK = ""
            VIP_PREVIEW_NAME = ""
            db.set_setting("vip_preview_link", "")
            db.set_setting("vip_preview_name", "")
            save_config()
            await update.message.reply_text("✅ VIP Preview link disabled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard")]]))
            return

        if text_in.startswith("@"):
            link = f"https://t.me/{text_in[1:]}"
        elif not text_in.startswith("http"):
            link = f"https://t.me/{text_in}"
        else:
            link = text_in

        VIP_PREVIEW_LINK = link
        VIP_PREVIEW_NAME = "VIP Preview Channel"
        db.set_setting("vip_preview_link", link)
        db.set_setting("vip_preview_name", VIP_PREVIEW_NAME)
        save_config()
        db.log_activity(user_id, "PREVIEW_LINK_UPDATED", link)

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👁️ Test Preview Link", url=link),
                InlineKeyboardButton("👑 View Plans Menu", callback_data="plans_menu"),
            ],
            [
                InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard"),
            ]
        ])

        await update.message.reply_text(
            f"✅ <b>VIP Preview Link সফলভাবে সেট হয়েছে!</b>\n\n"
            f"🔗 <b>লিংক:</b> {link}\n"
            f"💾 <i>ডাটাবেজে স্থায়ীভাবে সেভ করা হয়েছে। এখন ইউজাররা /plans ও /start মেনুতে প্রিভিউ বাটন দেখতে পাবে।</i>",
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        return

    # 12. Admin Awaiting Rules Text Input
    if action == "admin_awaiting_rules_text":
        new_rules = update.message.text.strip()
        USER_STATES.pop(user_id, None)

        if new_rules.lower() in ("default", "reset"):
            db.set_setting("vip_rules_text", "")
            db.log_activity(user_id, "RULES_RESET_DEFAULT", "Reset to default via text")
            await update.message.reply_text(
                "✅ <b>ভিআইপি নিয়মাবলী ও প্রাইস-লক নোটিস ডিফল্টে রিসেট হয়েছে!</b>\n\n"
                f"{DEFAULT_RULES_TEXT}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard")]])
            )
            return

        db.set_setting("vip_rules_text", new_rules)
        db.log_activity(user_id, "RULES_UPDATED", new_rules[:100])

        await update.message.reply_text(
            f"✅ <b>ভিআইপি নিয়মাবলী সফলভাবে আপডেট হয়েছে!</b>\n\n"
            f"💾 <i>ডাটাবেজে পার্মানেন্টলি সেভ হয়েছে এবং সব ইউজার স্ক্রিনে (/plans, /start, checkout) লাইভ হয়ে গেছে।</i>\n\n"
            f"<b>নতুন নোটিস প্রিভিউ:</b>\n{new_rules}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 View Plans Menu", callback_data="plans_menu")],
                [InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard")]
            ]),
            disable_web_page_preview=True
        )
        return

    # 10. Admin Awaiting Payment Input
    if action == "admin_awaiting_payment_input":
        text_in = update.message.text.strip()
        pay_key = state.get("pay_key")
        USER_STATES.pop(user_id, None)

        if not pay_key:
            return

        if pay_key in ("bkash", "nagad"):
            PAYMENT_INFO[pay_key] = text_in
            db.set_setting(f"pay_{pay_key}", text_in)
        elif pay_key in ("binance", "binance_pay_id"):
            PAYMENT_INFO["binance_pay_id"] = text_in
            db.set_setting("pay_binance_pay_id", text_in)
        elif pay_key in ("usdt", "usdt_trc20"):
            PAYMENT_INFO["usdt_trc20"] = text_in
            db.set_setting("pay_usdt_trc20", text_in)
        else:
            PAYMENT_INFO[pay_key] = text_in
            db.set_setting(f"pay_{pay_key}", text_in)

        save_config()
        db.log_activity(user_id, "PAYMENT_INFO_UPDATED", f"{pay_key}: {text_in}")

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💳 Payment Settings", callback_data="adm_action_edit_payments"),
                InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard"),
            ]
        ])

        await update.message.reply_text(
            f"✅ <b>{pay_key.upper()} সফলভাবে আপডেট হয়েছে!</b>\n\n"
            f"নতুন নম্বর / তথ্য: <code>{text_in}</code>\n"
            f"💾 <i>ডাটাবেজ ও বটে পার্মানেন্টলি সেভ হয়ে গেছে!</i>",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    # Admin Awaiting Reminder Stages Input
    if action == "admin_awaiting_reminder_stages":
        nums = [int(n) for n in re.findall(r"\d+", msg_text) if int(n) > 0]
        USER_STATES.pop(user_id, None)
        if not nums:
            await update.message.reply_text("❌ অবৈধ ইনপুট! সংখ্যা লিখুন (যেমন: 24 12 1)।")
            return
        nums = sorted(list(set(nums)), reverse=True)
        new_val = ",".join(str(n) for n in nums)
        db.set_setting("reminder_stages", new_val)
        db.log_activity(user_id, "REMINDER_STAGES_UPDATED", new_val)
        await update.message.reply_text(
            f"✅ <b>Reminder Schedule Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ নতুন ওয়ার্নিং শিডিউল: <b>{new_val} ঘণ্টা আগে</b>\n"
            f"💾 <i>ডাটাবেজে সেভ হয়েছে এবং শিডিউলার এই সময়ে স্বয়ংক্রিয় নোটিশ পাঠাবে।</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_dashboard")]]),
        )
        return

    # 12. User Submitting Payout Request Info
    if action == "aff_awaiting_payout_info":
        text_in = update.message.text.strip()
        method = state.get("method", "bKash")
        USER_STATES.pop(user_id, None)

        parts = text_in.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ ফরম্যাট সঠিক হয়নি! অনুগ্রহ করে এক লাইনে অ্যাকাউন্ট নম্বর ও টাকার পরিমাণ দিন (যেমন: 01718539406 500)।")
            return

        account_num = parts[0]
        try:
            amount = float(parts[1])
        except ValueError:
            await update.message.reply_text("❌ টাকার পরিমাণ সঠিক সংখ্যায় দিন (যেমন: 500)।")
            return

        ok, err_msg, payout_id = db.request_payout(user_id, amount, method, account_num)
        if not ok:
            await update.message.reply_text(f"❌ {err_msg}")
            return

        await update.message.reply_text(
            f"✅ <b>উইথড্রয়াল রিকোয়েস্ট সফলভাবে জমা হয়েছে! (#{payout_id})</b>\n\n"
            f"💰 <b>পরিমাণ:</b> <b>{amount:.2f} BDT</b>\n"
            f"💳 <b>পেমেন্ট মেথড:</b> <b>{method}</b>\n"
            f"📱 <b>অ্যাকাউন্ট:</b> <code>{account_num}</code>\n\n"
            f"⏳ <i>অ্যাডমিন ভেরিফাই করে দ্রুত আপনার নম্বরে টাকা পাঠিয়ে দেবে।</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤝 Affiliate Hub", callback_data="affiliate_menu")]]),
        )

        for adm_id in ADMIN_IDS:
            try:
                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve Payout", callback_data=f"aff_p_app_{payout_id}"),
                        InlineKeyboardButton("❌ Reject Payout", callback_data=f"aff_p_rej_{payout_id}"),
                    ]
                ])
                safe_username = html.escape(user.username or "none")
                await context.bot.send_message(
                    chat_id=adm_id,
                    text=(
                        f"💸 <b>NEW PAYOUT REQUEST (#{payout_id})</b>\n\n"
                        f"👤 User: <code>{user_id}</code> (@{safe_username})\n"
                        f"💰 Amount: <b>{amount:.2f} BDT</b>\n"
                        f"💳 Method: <b>{method}</b>\n"
                        f"📱 Account: <code>{account_num}</code>\n\n"
                        f"👉 <i>অ্যাপ্রুভ বা রিজেক্ট করতে নিচের বাটন ব্যবহার করুন:</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except Exception as e:
                logger.error(f"Failed to alert admin {adm_id} about payout #{payout_id}: {e}")
        return

    # User Submitting Reseller Application (Marketing Plan & Channels)
    if action == "aff_awaiting_reseller_app":
        text_in = update.message.text.strip() if update.message.text else ""
        if len(text_in) < 15:
            await update.message.reply_text(
                "⚠️ <b>অনুগ্রহ করে বিস্তারিত তথ্য প্রদান করুন!</b>\n"
                "আপনার মার্কেটিং কৌশল এবং চ্যানেল/গ্রুপ লিঙ্কসহ কমপক্ষে কয়েকটি লাইনে বিস্তারিত লিখে পাঠান।\n\n"
                "<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
                parse_mode="HTML",
            )
            return

        USER_STATES.pop(user_id, None)
        app_id = db.create_reseller_application(
            user_id=user_id,
            username=user.username,
            full_name=user.full_name,
            marketing_plan=text_in,
        )

        safe_plan = html.escape(text_in)
        await update.message.reply_text(
            f"✅ <b>আপনার ভেরিফাইড রিসেলার আবেদন সফলভাবে জমা হয়েছে! (ID: #{app_id})</b>\n\n"
            f"📋 <b>জমা দেওয়া মার্কেটিং বিবরণ ও লিঙ্ক:</b>\n"
            f"<blockquote>{safe_plan}</blockquote>\n\n"
            f"⏳ <i>আমাদের অ্যাডমিন টিম আপনার ট্রাফিক সোর্স ও প্ল্যান যাচাই করে দ্রুত অনুমোদন প্রদান করবেন। সিদ্ধান্ত জানা মাত্রই আপনার ইনবক্সে নোটিফিকেশন চলে আসবে।</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🤝 Back to Affiliate Hub", callback_data="affiliate_menu")]]),
        )

        safe_name = html.escape(user.full_name or "Applicant")
        safe_uname = f"@{user.username}" if user.username else "No @"
        admin_card = (
            f"📝 <b>NEW VERIFIED RESELLER APPLICATION (#{app_id})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>আবেদনকারী:</b> {safe_name} ({safe_uname})\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📅 <b>তারিখ:</b> <code>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
            f"🎯 <b>মার্কেটিং স্ট্র্যাটেজি ও চ্যানেল লিঙ্ক:</b>\n"
            f"<blockquote>{safe_plan}</blockquote>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👇 <i>রিভিউ করে সিদ্ধান্ত নিন:</i>"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve Reseller", callback_data=f"res_app_ok_{app_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"res_app_no_{app_id}"),
            ]
        ])

        for adm_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=adm_id,
                    text=admin_card,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to alert admin {adm_id} about reseller app #{app_id}: {e}")
        return

    # 13. Admin Editing Affiliate Settings
    if action == "admin_awaiting_aff_setting":
        text_in = update.message.text.strip()
        setting_key = state.get("key")
        setting_label = state.get("label", setting_key)
        USER_STATES.pop(user_id, None)

        if not setting_key:
            return

        db.set_setting(setting_key, text_in)
        db.log_activity(user_id, "AFFILIATE_SETTING_UPDATED", f"{setting_key}: {text_in}")

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⚙️ Edit More Settings", callback_data="adm_aff_settings"),
                InlineKeyboardButton("🤝 Affiliate Center", callback_data="adm_aff_center"),
            ]
        ])

        await update.message.reply_text(
            f"✅ <b>{setting_label} সফলভাবে আপডেট হয়েছে!</b>\n\n"
            f"নতুন ভ্যালু: <b>{text_in}</b>\n"
            f"💾 <i>ডাটাবেজে পার্মানেন্টলি সেভ হয়ে গেছে!</i>",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return


# =============================================================================
# ADMIN DASHBOARD & CONTROLS
# =============================================================================
async def render_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    stats = db.get_stats()
    pending_res_apps = len(db.get_all_pending_reseller_apps() or [])
    pending_payouts = len(db.get_pending_payouts() or [])
    aff_alert_badge = f" ({pending_res_apps + pending_payouts} ⚠️)" if (pending_res_apps + pending_payouts) > 0 else ""

    em_crw = em_tag("crown", "👑")
    em_dia = em_tag("diamond", "💎")
    em_set = em_tag("settings", "⚙️")
    em_wal = em_tag("wallet", "💳")
    em_usr = em_tag("referral", "👥")
    box_border = "━━━━━━━━━━━━━━━━━━━━"

    text = (
        f"{em_crw} <b>{PRODUCT_NAME} — ADMIN CONTROL CENTER</b> {em_set}\n"
        f"{box_border}\n"
        f"{em_usr} <b>Active Subscribers:</b> <b>{stats['total_active']}</b> Members\n"
        f"⏳ <b>Expiring within 24h:</b> <b>{stats['expiring_24h']}</b> Members\n"
        f"{em_crw} <b>Lifetime Members:</b> <b>{stats['total_lifetime']}</b> Members\n"
        f"💀 <b>Removed / Expired:</b> <b>{stats['total_removed']}</b> Users\n"
        f"{em_wal} <b>Pending Payments:</b> <b>{stats['pending_payments']}</b> Requests\n"
        f"📝 <b>Pending Reseller Apps:</b> <b>{pending_res_apps}</b> Applications\n"
        f"💸 <b>Pending Payouts:</b> <b>{pending_payouts}</b> Requests\n"
        f"{box_border}\n"
        f"👉 <i>নিচের অপশনগুলো ব্যবহার করে পরিচালনা করুন:</i>"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Member", callback_data="adm_action_add"),
            InlineKeyboardButton("🔄 Renew Member", callback_data="adm_action_renew"),
        ],
        [
            InlineKeyboardButton("🚫 Kick User", callback_data="adm_action_kick"),
            InlineKeyboardButton("🔍 Search User", callback_data="adm_action_search"),
        ],
        [
            InlineKeyboardButton(f"👥 Active List ({stats['total_active']})", callback_data="adm_action_list_active"),
            InlineKeyboardButton(f"⏳ Expiring ({stats['expiring_24h']})", callback_data="adm_action_expiring"),
        ],
        [
            InlineKeyboardButton("➕ Add VIP Channel", callback_data="adm_action_add_channel"),
            InlineKeyboardButton("➕ Add VIP Group", callback_data="adm_action_add_group"),
        ],
        [
            InlineKeyboardButton("🗑️ Del Channel/Group", callback_data="adm_action_del_channel"),
            InlineKeyboardButton(f"⚙️ Channels & Groups ({len(CHANNELS)})", callback_data="adm_action_channels"),
        ],
        [
            InlineKeyboardButton("💰 Edit Plan Prices", callback_data="adm_action_edit_prices"),
            InlineKeyboardButton(f"🤝 Affiliate Center{aff_alert_badge}", callback_data="adm_aff_center"),
        ],
        [
            InlineKeyboardButton("💳 Edit Payments", callback_data="adm_action_edit_payments"),
            InlineKeyboardButton("💾 Backup DB Now", callback_data="adm_action_backup"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast DM", callback_data="adm_action_broadcast"),
            InlineKeyboardButton(f"🔥 Join Broadcast ({'ON' if db.get_setting('vip_join_broadcast_enabled', '1') == '1' else 'OFF'})", callback_data="adm_toggle_join_bc"),
        ],
        [
            InlineKeyboardButton(f"👁️ VIP Preview Link ({'Active' if VIP_PREVIEW_LINK else 'None'})", callback_data="adm_action_preview"),
            InlineKeyboardButton(f"⏱️ Reminders ({db.get_setting('reminder_stages', '24,12,1')}h)", callback_data="adm_action_reminders"),
        ],
        [
            InlineKeyboardButton("📋 Live System Logs & Diagnostics", callback_data="adm_action_logs"),
        ],
        [
            InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"),
        ]
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await render_admin_dashboard(update, context)


# =============================================================================
# ADMIN SHORTHAND COMMANDS (/add, /renew, /remove, /status, /users, /backup)
# =============================================================================
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /add <user_id> <monthly|yearly|lifetime|30d|15d|12h>"""
    if not is_admin(update.effective_user.id):
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/add &lt;user_id&gt; &lt;duration&gt;</code>\n"
            "<b>Examples:</b>\n"
            "• <code>/add 123456789 monthly</code> (30 days)\n"
            "• <code>/add 123456789 yearly</code> (365 days)\n"
            "• <code>/add 123456789 lifetime</code> (Forever)\n"
            "• <code>/add 123456789 15d</code> (15 days)\n"
            "• <code>/add 123456789 12h</code> (12 hours)",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(args[0])
        duration_str = args[1]
        seconds, label, is_lifetime = parse_duration(duration_str)

        sub = db.add_or_update_subscriber(
            user_id=target_id,
            username=None,
            full_name=None,
            plan_key="admin_added",
            plan_name=label,
            duration_seconds=seconds,
            is_lifetime=is_lifetime,
        )

        # Deliver 1-time links to user
        await deliver_welcome_access(context.bot, target_id, label, sub["expires_at"], is_lifetime)

        await update.message.reply_text(
            f"✅ <b>Member Added Successfully!</b>\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"📦 Plan: <b>{label}</b>\n"
            f"⏳ Expiry: <code>{sub['expires_at']}</code>\n"
            f"🚀 1-Time Invite Links dispatched to member's DM.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error adding user: {e}")


async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /renew <user_id> <duration>"""
    if not is_admin(update.effective_user.id):
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/renew &lt;user_id&gt; &lt;duration&gt;</code>\n"
            "<b>Example:</b> <code>/renew 123456789 30d</code> or <code>monthly</code>",
            parse_mode="HTML",
        )
        return

    try:
        target_id = int(args[0])
        duration_str = args[1]
        seconds, label, is_lifetime = parse_duration(duration_str)

        sub = db.renew_subscriber(
            user_id=target_id,
            duration_seconds=seconds,
            new_plan_name=label,
            is_lifetime=is_lifetime,
        )

        if not sub:
            await update.message.reply_text(f"❌ User <code>{target_id}</code> not found in database.", parse_mode="HTML")
            return

        # Notify User
        try:
            renew_msg = (
                f"🎉 <b>আপনার সাবস্ক্রিপশন সফলভাবে রিনিউ হয়েছে!</b>\n\n"
                f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
                f"➕ <b>বর্ধিত সময়:</b> +{label}\n"
                f"⏳ <b>নতুন মেয়াদ শেষ:</b> <code>{sub['expires_at']}</code>\n\n"
                f"চ্যানেলগুলোতে আপনার এক্সেস নিরবচ্ছিন্ন থাকবে।"
            )
            await context.bot.send_message(chat_id=target_id, text=renew_msg, parse_mode="HTML")
        except Exception:
            pass

        await update.message.reply_text(
            f"✅ <b>Renewed User <code>{target_id}</code></b>\n"
            f"➕ Added: <b>+{label}</b>\n"
            f"⏳ New Expiration: <code>{sub['expires_at']}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error renewing user: {e}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /remove <user_id> (Immediately kicks from all channels)"""
    if not is_admin(update.effective_user.id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("<b>Usage:</b> <code>/remove &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    try:
        target_id = int(args[0])
        kicked_count = 0

        # Kick from each channel
        for ch in CHANNELS:
            ch_id = ch["id"]
            try:
                await context.bot.ban_chat_member(chat_id=ch_id, user_id=target_id)
                await context.bot.unban_chat_member(chat_id=ch_id, user_id=target_id)
                kicked_count += 1
            except Exception:
                pass

        db.mark_removed(target_id, reason="admin_manual_kick")

        # Notify user
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"❌ Your access to <b>{PRODUCT_NAME}</b> channels has been revoked by administration.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await update.message.reply_text(
            f"🚫 <b>User <code>{target_id}</code> Kicked!</b>\n"
            f"Ejected from {kicked_count}/{len(CHANNELS)} channels.\n"
            f"Status updated to 'removed' in database.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error kicking user: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inspects a user's subscription details."""
    if not is_admin(update.effective_user.id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("<b>Usage:</b> <code>/status &lt;user_id&gt;</code>", parse_mode="HTML")
        return

    try:
        target_id = int(args[0])
        sub = db.get_subscriber(target_id)
        if not sub:
            await update.message.reply_text(f"❌ User <code>{target_id}</code> not found in database.", parse_mode="HTML")
            return

        text = (
            f"🔍 <b>SUBSCRIBER DOSSIER</b>\n"
            f"═════════════════════════\n"
            f"🆔 <b>ID:</b> <code>{sub['user_id']}</code>\n"
            f"👤 <b>Name:</b> {sub['full_name'] or 'N/A'}\n"
            f"🏷️ <b>Username:</b> @{sub['username'] or 'N/A'}\n"
            f"📦 <b>Plan:</b> {sub['plan_name']}\n"
            f"👑 <b>Lifetime:</b> {'Yes' if sub['is_lifetime'] else 'No'}\n"
            f"🟢 <b>Status:</b> {sub['status'].upper()}\n"
            f"📅 <b>Joined:</b> <code>{sub['joined_at']}</code>\n"
            f"⏳ <b>Expires:</b> <code>{sub['expires_at']}</code>\n"
            f"⚠️ <b>Reminder Sent:</b> {'Yes' if sub['reminder_sent'] else 'No'}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays table of active subscribers."""
    if not is_admin(update.effective_user.id):
        return

    active_subs = db.get_active_subscribers()
    if not active_subs:
        await update.message.reply_text("ℹ️ No active subscribers currently in database.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [f"👥 <b>ACTIVE SUBSCRIBERS ({len(active_subs)})</b>\n"]

    for idx, s in enumerate(active_subs[:30], 1):
        if s["is_lifetime"]:
            rem = "Lifetime"
        else:
            diff = datetime.datetime.fromisoformat(s["expires_at"]) - now
            rem = f"{diff.days}d {diff.seconds // 3600}h" if diff.total_seconds() > 0 else "Expired"

        uname = f"@{s['username']}" if s.get("username") else str(s["user_id"])
        lines.append(f"{idx}. <code>{s['user_id']}</code> | {uname} | <b>{rem}</b> | {s['plan_name']}")

    if len(active_subs) > 30:
        lines.append(f"\n<i>...and {len(active_subs) - 30} more.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates an atomic SQLite backup and sends the file directly to Admin's Telegram."""
    if not is_admin(update.effective_user.id):
        return

    msg = await update.message.reply_text("⏳ Creating atomic SQLite database backup...")
    try:
        backup_file = db.backup_database()
        with open(backup_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=os.path.basename(backup_file),
                caption=f"🛡️ <b>Database Snapshot Backup</b>\n📅 <code>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>",
                parse_mode="HTML",
            )
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Backup failed: {e}")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a broadcast message to all active subscribers."""
    if not is_admin(update.effective_user.id):
        return

    text = " ".join(context.args) if context.args else None
    if not text:
        await update.message.reply_text("<b>Usage:</b> <code>/broadcast &lt;Your message here&gt;</code>", parse_mode="HTML")
        return

    active_subs = db.get_active_subscribers()
    sent_count = 0
    failed_count = 0

    progress_msg = await update.message.reply_text(f"📢 Broadcasting to {len(active_subs)} active members...")

    for s in active_subs:
        uid = s["user_id"]
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 <b>[ANNOUNCEMENT]</b>\n\n{text}",
                parse_mode="HTML",
            )
            sent_count += 1
            await asyncio.sleep(0.05)  # Rate pacing
        except Exception:
            failed_count += 1

    await progress_msg.edit_text(
        f"✅ <b>Broadcast Completed!</b>\n"
        f"✉️ Delivered: <b>{sent_count}</b>\n"
        f"⚠️ Blocked/Failed: <b>{failed_count}</b>",
        parse_mode="HTML",
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /addchannel <channel_id> [channel_name]"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/addchannel &lt;channel_id&gt; [name]</code>\n"
            "<b>Example:</b> <code>/addchannel -1001234567890 Main Course Vault</code>\n"
            "<i>Note: The bot MUST be an Administrator in the channel with 'Invite Users' and 'Ban Users' rights.</i>",
            parse_mode="HTML",
        )
        return

    try:
        ch_id = int(args[0])
        ch_name = " ".join(args[1:]) if len(args) > 1 else f"Channel {ch_id}"

        # Check if already exists
        for ch in CHANNELS:
            if ch["id"] == ch_id:
                ch["name"] = ch_name
                ch["type"] = "channel"
                save_config()
                await update.message.reply_text(
                    f"✅ Channel <code>{ch_id}</code> updated with name: <b>{ch_name}</b>",
                    parse_mode="HTML",
                )
                return

        # Test Bot Permissions in that channel
        try:
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
            is_bot_admin = member.status in ("administrator", "creator")
            admin_status_text = "✅ Bot is Admin" if is_bot_admin else "⚠️ Warning: Bot is NOT admin in this channel yet!"
        except Exception as e:
            admin_status_text = f"⚠️ Could not verify bot admin status: {e}"

        CHANNELS.append({"id": ch_id, "name": ch_name, "type": "channel", "invite_expire_hours": 24})
        save_config()
        db.log_activity(update.effective_user.id, "CHANNEL_ADDED", f"Channel: {ch_name} ({ch_id})")

        await update.message.reply_text(
            f"✅ <b>VIP Channel Added Successfully!</b>\n"
            f"📢 <b>Name:</b> {ch_name}\n"
            f"🆔 <b>ID:</b> <code>{ch_id}</code>\n"
            f"⚙️ <b>Status:</b> {admin_status_text}\n\n"
            f"<i>This channel is now active for 1-time invite links and auto-kicks!</i>",
            parse_mode="HTML",
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid Channel ID! Must be a numeric ID (usually starts with -100).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error adding channel: {e}")


async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /addgroup <group_id> [group_name]"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/addgroup &lt;group_id&gt; [name]</code>\n"
            "<b>Example:</b> <code>/addgroup -1001234567890 VIP Support & Discussion Group</code>\n"
            "<i>Note: The bot MUST be an Administrator in the group with 'Invite Users' and 'Ban Users' rights.</i>",
            parse_mode="HTML",
        )
        return

    try:
        grp_id = int(args[0])
        grp_name = " ".join(args[1:]) if len(args) > 1 else f"Group {grp_id}"

        # Check if already exists
        for ch in CHANNELS:
            if ch["id"] == grp_id:
                ch["name"] = grp_name
                ch["type"] = "group"
                save_config()
                await update.message.reply_text(
                    f"✅ Group <code>{grp_id}</code> updated with name: <b>{grp_name}</b>",
                    parse_mode="HTML",
                )
                return

        # Test Bot Permissions in that group
        try:
            member = await context.bot.get_chat_member(chat_id=grp_id, user_id=context.bot.id)
            is_bot_admin = member.status in ("administrator", "creator")
            admin_status_text = "✅ Bot is Admin in Group" if is_bot_admin else "⚠️ Warning: Bot is NOT admin in this group yet!"
        except Exception as e:
            admin_status_text = f"⚠️ Could not verify bot admin status: {e}"

        CHANNELS.append({"id": grp_id, "name": grp_name, "type": "group", "invite_expire_hours": 24})
        save_config()
        db.log_activity(update.effective_user.id, "GROUP_ADDED", f"Group: {grp_name} ({grp_id})")

        await update.message.reply_text(
            f"✅ <b>VIP Discussion Group Added Successfully!</b>\n"
            f"👥 <b>Name:</b> {grp_name}\n"
            f"🆔 <b>ID:</b> <code>{grp_id}</code>\n"
            f"⚙️ <b>Status:</b> {admin_status_text}\n\n"
            f"<i>This group is now active for 1-time invite links and auto-kicks!</i>",
            parse_mode="HTML",
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid Group ID! Must be a numeric ID (usually starts with -100).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error adding group: {e}")


async def cmd_delchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /delchannel <channel_or_group_id>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("<b>Usage:</b> <code>/delchannel &lt;channel_or_group_id&gt;</code>", parse_mode="HTML")
        return

    try:
        ch_id = int(args[0])
        initial_len = len(CHANNELS)
        db.delete_channel(ch_id)
        CHANNELS[:] = [ch for ch in CHANNELS if ch["id"] != ch_id]

        if len(CHANNELS) < initial_len:
            save_config()
            await update.message.reply_text(f"🗑️ Channel/Group <code>{ch_id}</code> removed from bot management.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ ID <code>{ch_id}</code> not found in configured list.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all managed VIP channels and discussion groups with status check."""
    if not is_admin(update.effective_user.id):
        return

    channels_list = [c for c in CHANNELS if c.get("type", "channel") == "channel"]
    groups_list = [c for c in CHANNELS if c.get("type") == "group"]

    lines = ["⚙️ <b>CONFIGURED VIP DESTINATIONS:</b>\n"]

    lines.append(f"📢 <b>VIP BROADCAST CHANNELS ({len(channels_list)}):</b>")
    if not channels_list:
        lines.append("<i>• No broadcast channels configured. (Use /addchannel)</i>")
    else:
        for idx, ch in enumerate(channels_list, 1):
            ch_id = ch["id"]
            ch_name = ch.get("name", str(ch_id))
            try:
                member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
            except Exception as e:
                status = f"❌ Error: {e}"
            lines.append(f"  {idx}. <b>{ch_name}</b>\n     ID: <code>{ch_id}</code> | Status: {status}")

    lines.append(f"\n👥 <b>VIP DISCUSSION GROUPS ({len(groups_list)}):</b>")
    if not groups_list:
        lines.append("<i>• No discussion groups configured. (Use /addgroup)</i>")
    else:
        for idx, grp in enumerate(groups_list, 1):
            grp_id = grp["id"]
            grp_name = grp.get("name", str(grp_id))
            try:
                member = await context.bot.get_chat_member(chat_id=grp_id, user_id=context.bot.id)
                status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
            except Exception as e:
                status = f"❌ Error: {e}"
            lines.append(f"  {idx}. <b>{grp_name}</b>\n     ID: <code>{grp_id}</code> | Status: {status}")

    lines.append("\n📢 <b>FORCE JOIN CHANNEL (Mandatory):</b>")
    if FORCE_CHANNEL_ID:
        lines.append(f"• Name: <b>{FORCE_CHANNEL_NAME}</b>\n  Target: <code>{FORCE_CHANNEL_ID}</code>\n  Link: {FORCE_CHANNEL_LINK}")
    else:
        lines.append("• <i>Disabled / Not set (Use /setforcechannel to configure)</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_setpayment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setpayment <bkash|nagad|binance|usdt> <details>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> <code>/setpayment &lt;bkash|nagad|binance|usdt&gt; &lt;value&gt;</code>\n"
            "<b>Example:</b> <code>/setpayment bkash 01712345678 (Personal)</code>",
            parse_mode="HTML",
        )
        return

    key = args[0].lower()
    val = " ".join(args[1:])

    if key in ("bkash", "nagad"):
        PAYMENT_INFO[key] = val
    elif key in ("binance", "binance_pay_id"):
        PAYMENT_INFO["binance_pay_id"] = val
    elif key in ("usdt", "usdt_trc20"):
        PAYMENT_INFO["usdt_trc20"] = val
    else:
        PAYMENT_INFO[key] = val

    save_config()
    await update.message.reply_text(f"✅ Updated <b>{key.upper()}</b> to: <code>{val}</code>", parse_mode="HTML")


async def cmd_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setprice <monthly|yearly|lifetime> <price_bdt> [price_usdt]"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args or len(args) < 2:
        lines = [
            "💰 <b>Usage:</b> <code>/setprice &lt;monthly|yearly|lifetime&gt; &lt;price_bdt&gt; [price_usdt]</code>\n",
            "<b>Examples:</b>",
            "• <code>/setprice monthly 750</code> <i>(ডলার অটো ক্যালকুলেট হবে)</i>",
            "• <code>/setprice yearly 5000</code>",
            "• <code>/setprice lifetime 8000</code>\n",
            "<b>Current Active Pricing:</b>",
        ]
        for pk, pv in PLANS.items():
            lines.append(f"• <b>{pv['name']}:</b> <code>{format_price(pv['price_bdt'])}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    plan_key = args[0].lower()
    if plan_key not in PLANS:
        await update.message.reply_text(
            f"❌ Invalid plan key: <code>{plan_key}</code>. Allowed: <code>{', '.join(PLANS.keys())}</code>",
            parse_mode="HTML",
        )
        return

    try:
        price_text = " ".join(args[1:])
        nums = re.findall(r"\d+", price_text)
        if not nums:
            await update.message.reply_text("❌ Price must contain numeric values (e.g. 500 or 500 5).")
            return

        bdt = int(nums[0])
        usdt = int(nums[1]) if len(nums) > 1 else calc_usd(bdt)

        PLANS[plan_key]["price_bdt"] = bdt
        PLANS[plan_key]["price_usdt"] = usdt
        db.update_plan_price(plan_key, bdt, usdt)
        save_config()
        db.log_activity(update.effective_user.id, "PRICE_UPDATED", f"Plan {plan_key}: {bdt} BDT / ${usdt} USDT")

        await update.message.reply_text(
            f"✅ <b>Plan Pricing Updated!</b>\n\n"
            f"📦 <b>Plan:</b> {PLANS[plan_key]['name']}\n"
            f"💰 <b>New Price:</b> <b>{format_price(bdt, usdt)}</b>\n"
            f"💾 <i>Saved permanently in SQLite DB & Config.</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error updating price: {e}")


async def cmd_setdollar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setdollar <rate> (e.g. /setdollar 125)"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    args = context.args
    if not args:
        cur_rate = get_dollar_rate()
        lines = [
            f"💵 <b>CURRENT DOLLAR RATE & PRICING</b>\n",
            f"• <b>রেট:</b> <code>1 USD = {cur_rate:g} BDT</code>\n",
            "<b>বর্তমান প্ল্যান মূল্যসমূহ:</b>",
        ]
        for pk, pv in PLANS.items():
            lines.append(f"• <b>{pv['name']}:</b> <code>{format_price(pv['price_bdt'])}</code>")
        lines.append("\n👉 নতুন রেট সেট করতে লিখুন: <code>/setdollar 125</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    try:
        nums = re.findall(r"\d+(?:\.\d+)?", args[0])
        if not nums:
            await update.message.reply_text("❌ অবৈধ রেট! সংখ্যা লিখুন (যেমন: 125 বা 130)।")
            return

        new_rate = float(nums[0])
        set_dollar_rate(new_rate)
        db.log_activity(user_id, "DOLLAR_RATE_UPDATED", f"1 USD = {new_rate} BDT")

        lines = [
            f"✅ <b>Dollar Rate Updated!</b>\n",
            f"💵 <b>নতুন রেট:</b> <code>1 USD = {new_rate:g} BDT</code>\n",
            f"━━━━━━━━━━━━━━━━━━━━\n",
            f"স্বয়ংক্রিয়ভাবে পরিবর্তিত নতুন প্ল্যান মূল্যসমূহ:\n",
        ]
        for pk in ("monthly", "yearly", "lifetime"):
            pv = PLANS.get(pk)
            if pv:
                lines.append(f"• <b>{pv['name']}:</b> <code>{format_price(pv['price_bdt'])}</code>")
        lines.append("\n💾 <i>ডাটাবেজে পার্মানেন্টলি সেভ হয়েছে এবং সব ইউজার স্ক্রিনে লাইভ হয়ে গেছে!</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ এরর: {e}")


async def cmd_setreminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setreminders 24 12 1"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        curr = db.get_setting("reminder_stages", "24,12,1")
        await update.message.reply_text(
            f"⏱️ <b>CURRENT EXPIRATION REMINDER SCHEDULE:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>সক্রিয় স্টেজসমূহ:</b> <code>{curr} ঘণ্টা আগে</code>\n\n"
            f"👉 নতুন স্টেজ সেট করতে লিখুন (স্পেস দিয়ে ঘণ্টার সংখ্যা):\n"
            f"<code>/setreminders 24 12 1</code>\n"
            f"<i>(উদাহরণ: ২৪ ঘণ্টা, ১২ ঘণ্টা এবং ১ ঘণ্টা আগে অটোমেটিক ওয়ার্নিং যাবে)</i>",
            parse_mode="HTML"
        )
        return

    try:
        nums = [int(n) for n in re.findall(r"\d+", " ".join(args)) if int(n) > 0]
        if not nums:
            await update.message.reply_text("❌ অবৈধ স্টেজ! সংখ্যা লিখুন (যেমন: /setreminders 24 12 1)।")
            return
        nums = sorted(list(set(nums)), reverse=True)
        new_val = ",".join(str(n) for n in nums)
        db.set_setting("reminder_stages", new_val)
        db.log_activity(update.effective_user.id, "REMINDER_STAGES_UPDATED", new_val)
        await update.message.reply_text(
            f"✅ <b>Reminder Schedule Updated!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱️ নতুন ওয়ার্নিং শিডিউল: <b>{new_val} ঘণ্টা আগে</b>\n"
            f"💾 <i>ডাটাবেজে সেভ হয়েছে এবং শিডিউলার এই সময়ে স্বয়ংক্রিয় নোটিশ পাঠাবে।</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ এরর: {e}")


async def cmd_setpreview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage:
      /setpreview <link> [Name]
      /setpreview off (To disable)
    """
    if not is_admin(update.effective_user.id):
        return

    global VIP_PREVIEW_LINK, VIP_PREVIEW_NAME
    args = context.args

    if not args:
        curr = f"Link: {VIP_PREVIEW_LINK}\nName: {VIP_PREVIEW_NAME}" if VIP_PREVIEW_LINK else "<i>Disabled / Not set</i>"
        await update.message.reply_text(
            f"👁️ <b>VIP PREVIEW CHANNEL CONFIGURATION:</b>\n\n"
            f"{curr}\n\n"
            f"<b>Commands:</b>\n"
            f"• Set Preview: <code>/setpreview &lt;invite_link&gt; [name]</code>\n"
            f"• Example: <code>/setpreview https://t.me/your_preview_channel VIP Free Preview</code>\n"
            f"• Disable: <code>/setpreview off</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if args[0].lower() in ("off", "disable", "none", "remove", "clear"):
        VIP_PREVIEW_LINK = "https://t.me/PAV_CourseEdition/16"
        VIP_PREVIEW_NAME = "VIP Preview & Proof Channel"
        db.set_setting("vip_preview_link", "")
        db.set_setting("vip_preview_name", "")
        save_config()
        await update.message.reply_text("✅ <b>VIP Preview Channel reset to default!</b>", parse_mode="HTML")
        return

    raw_link = args[0].strip()
    if raw_link.startswith("@"):
        link = f"https://t.me/{raw_link[1:]}"
    elif not raw_link.startswith("http"):
        link = f"https://t.me/{raw_link}"
    else:
        link = raw_link

    name = " ".join(args[1:]) if len(args) > 1 else "VIP Preview Channel"

    VIP_PREVIEW_LINK = link
    VIP_PREVIEW_NAME = name
    db.set_setting("vip_preview_link", link)
    db.set_setting("vip_preview_name", name)
    save_config()
    db.log_activity(update.effective_user.id, "PREVIEW_LINK_UPDATED", link)

    await update.message.reply_text(
        f"✅ <b>VIP Preview Link Configured!</b>\n\n"
        f"👁️ <b>Name:</b> {name}\n"
        f"🔗 <b>Link:</b> {link}\n\n"
        f"<i>ইউজাররা /plans ও /start মেনুতে এখন এই প্রিভিউ চ্যানেলের ডিরেক্ট বাটন ও লিংক দেখতে পাবেন!</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage:
      /setrules <custom_rules_text>
      /setrules default (To reset to default rules)
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    args = context.args
    if not args:
        curr_rules = get_rules_text()
        text = (
            "📝 <b>VIP RULES & NOTICE CONFIGURATION</b>\n\n"
            f"<b>বর্তমান নিয়মাবলী ও নোটিস:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{curr_rules}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Commands:</b>\n"
            "• Set Rules: <code>/setrules &lt;your_text&gt;</code>\n"
            "• Reset to Default: <code>/setrules default</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    full_text = " ".join(args).strip()
    if full_text.lower() in ("default", "reset"):
        db.set_setting("vip_rules_text", "")
        db.log_activity(user_id, "RULES_RESET_DEFAULT", "Reset via /setrules command")
        await update.message.reply_text(
            f"✅ <b>ভিআইপি নিয়মাবলী ডিফল্টে রিসেট করা হয়েছে!</b>\n\n{DEFAULT_RULES_TEXT}",
            parse_mode="HTML"
        )
        return

    db.set_setting("vip_rules_text", full_text)
    db.log_activity(user_id, "RULES_UPDATED", full_text[:100])
    await update.message.reply_text(
        f"✅ <b>ভিআইপি নিয়মাবলী সফলভাবে আপডেট হয়েছে!</b>\n\n"
        f"💾 <i>ডাটাবেজে সেভ হয়েছে এবং সব ইউজার স্ক্রিনে লাইভ হয়ে গেছে।</i>\n\n"
        f"<b>নতুন নোটিস:</b>\n{full_text}",
        parse_mode="HTML"
    )


async def cmd_setforcechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage:
      /setforcechannel <channel_id_or_@username> <invite_link> [Channel Name]
      /setforcechannel off (To disable)
    """
    if not is_admin(update.effective_user.id):
        return

    global FORCE_CHANNEL_ID, FORCE_CHANNEL_LINK, FORCE_CHANNEL_NAME
    args = context.args

    if not args:
        curr = f"Target: <code>{FORCE_CHANNEL_ID}</code>\nLink: {FORCE_CHANNEL_LINK}\nName: {FORCE_CHANNEL_NAME}" if FORCE_CHANNEL_ID else "<i>Disabled / Not configured</i>"
        await update.message.reply_text(
            f"📢 <b>FORCE JOIN CHANNEL CONFIGURATION:</b>\n\n"
            f"{curr}\n\n"
            f"<b>Commands:</b>\n"
            f"• Set Channel: <code>/setforcechannel &lt;id_or_@user&gt; &lt;link&gt; [name]</code>\n"
            f"• Example: <code>/setforcechannel @my_updates https://t.me/my_updates VIP Updates</code>\n"
            f"• Disable: <code>/setforcechannel off</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if args[0].lower() in ("off", "disable", "none", "remove"):
        FORCE_CHANNEL_ID = ""
        FORCE_CHANNEL_LINK = ""
        FORCE_CHANNEL_NAME = ""
        save_config()
        await update.message.reply_text("✅ <b>Force Channel Join disabled!</b>", parse_mode="HTML")
        return

    if len(args) < 2:
        await update.message.reply_text(
            "❌ <b>Error:</b> Both channel ID/username and invite link are required.\n"
            "Example: <code>/setforcechannel @my_channel https://t.me/my_channel Official Channel</code>",
            parse_mode="HTML",
        )
        return

    ch_id = args[0]
    link = args[1]
    name = " ".join(args[2:]) if len(args) > 2 else "Official Channel"

    FORCE_CHANNEL_ID = ch_id
    FORCE_CHANNEL_LINK = link
    FORCE_CHANNEL_NAME = name
    save_config()

    await update.message.reply_text(
        f"✅ <b>Force Channel Join Configured!</b>\n\n"
        f"📢 <b>Name:</b> {name}\n"
        f"🆔 <b>Target:</b> <code>{ch_id}</code>\n"
        f"🔗 <b>Link:</b> {link}\n\n"
        f"<i>Make sure the bot is added as an <b>Administrator</b> in this channel so it can verify member status!</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically registers any VIP Channel or Group when the bot is promoted to Admin."""
    chat = update.effective_chat
    if not update.my_chat_member or not update.my_chat_member.new_chat_member:
        return

    new_member = update.my_chat_member.new_chat_member
    is_now_admin = new_member.status in ("administrator", "creator")
    if is_now_admin and chat:
        chat_type = "channel" if chat.type == "channel" else "group"
        chat_id = chat.id
        chat_title = chat.title or f"{chat_type.title()} {chat_id}"

        # Do not auto-register if it's the force join channel itself
        if str(chat_id) == str(FORCE_CHANNEL_ID):
            return

        existing = [c for c in CHANNELS if c["id"] == chat_id]
        if not existing:
            db.add_or_update_channel(chat_id, chat_title, chat_type, invite_expire_hours=24)
            CHANNELS.append({
                "id": chat_id,
                "name": chat_title,
                "type": chat_type,
                "invite_expire_hours": 24,
            })
            save_config()
            logger.info(f"Auto-registered new {chat_type}: {chat_title} ({chat_id}) into VIP Database.")

            notify_text = (
                f"🎉 <b>VIP {chat_type.title()} Auto-Registered!</b>\n\n"
                f"📢 <b>Title:</b> {chat_title}\n"
                f"🆔 <b>ID:</b> <code>{chat_id}</code>\n"
                f"⚙️ <b>Type:</b> <code>{chat_type}</code>\n"
                f"🚀 <i>The bot is administrator. This destination is now permanently saved in SQLite DB and ready for 1-time invite links!</i>"
            )
            for adm in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=adm, text=notify_text, parse_mode="HTML")
                except Exception:
                    pass


async def handle_chat_member_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Real-Time Anti-Leak Ejection Engine:
    Monitors all member joins across VIP channels and groups.
    If an unauthorized user (not an active subscriber and not admin) joins via a leaked/shared link,
    they are immediately ejected and banned!
    """
    if not update.chat_member:
        return

    chat = update.chat_member.chat
    chat_id = chat.id
    new_member = update.chat_member.new_chat_member
    old_member = update.chat_member.old_chat_member

    # Only monitor our managed VIP channels/groups
    managed_ids = [c["id"] for c in CHANNELS]
    if chat_id not in managed_ids:
        return

    was_member = old_member.status in ("member", "administrator", "creator", "restricted")
    is_now_member = new_member.status in ("member", "restricted")

    if not was_member and is_now_member:
        joined_user = new_member.user
        joined_user_id = joined_user.id

        # Admins and bot itself are exempt
        if is_admin(joined_user_id) or joined_user_id == context.bot.id:
            return

        sub = db.get_subscriber(joined_user_id)
        if not sub or sub.get("status") != "active":
            logger.warning(f"🚨 UNAUTHORIZED USER {joined_user_id} (@{joined_user.username}) joined {chat.title}! Ejecting immediately...")
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=joined_user_id)
                await context.bot.unban_chat_member(chat_id=chat_id, user_id=joined_user_id)
                logger.info(f"✅ Successfully ejected unauthorized user {joined_user_id} from {chat.title}")
            except Exception as e:
                logger.error(f"Failed to eject unauthorized user {joined_user_id}: {e}")

            # Notify Admins of the blocked leak attempt
            safe_u_name = html.escape(str(joined_user.full_name or "Unknown"))
            safe_u_handle = html.escape(str(joined_user.username or "none"))
            alert_text = (
                f"🚨 <b>UNAUTHORIZED LINK LEAK BLOCKED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>User:</b> <a href='tg://user?id={joined_user_id}'>{safe_u_name}</a> (@{safe_u_handle})\n"
                f"🆔 <b>User ID:</b> <code>{joined_user_id}</code>\n"
                f"📢 <b>Target:</b> {html.escape(str(chat.title or chat_id))}\n"
                f"🛡️ <b>Action:</b> Ejected immediately (Zero active subscription found) ✅"
            )
            for adm in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=adm, text=alert_text, parse_mode="HTML")
                except Exception:
                    pass
        else:
            logger.info(f"Verified VIP Subscriber {joined_user_id} (@{joined_user.username}) joined {chat.title} successfully.")


# =============================================================================
# CALLBACK QUERY ROUTER (Inline Button Actions)
# =============================================================================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global VIP_PREVIEW_LINK, VIP_PREVIEW_NAME
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id

    logger.info(f"⚡ CALLBACK [User: {user_id} (@{user.username})]: '{data}'")

    _answered = False
    async def safe_ack(text: Optional[str] = None, show_alert: bool = False):
        nonlocal _answered
        if not _answered:
            try:
                if text:
                    await query.answer(text=text, show_alert=show_alert)
                else:
                    await query.answer()
                _answered = True
            except Exception:
                pass

    # 0. Force Channel Join Check Action
    if data == "check_force_sub":
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if is_subbed:
            await safe_ack("✅ ধন্যবাদ! ভেরিফিকেশন সফল হয়েছে।", show_alert=True)
            await render_main_menu(update, context)
            return
        else:
            await safe_ack("❌ আপনি এখনো চ্যানেলে জয়েন করেননি! দয়া করে চ্যানেলে জয়েন করে আবার ভেরিফাই করুন।", show_alert=True)
            return

    # Verify force sub for any other callbacks from non-admin users
    if not is_admin(user_id):
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if not is_subbed:
            await safe_ack("⚠️ অনুগ্রহ করে আগে আমাদের চ্যানেলে জয়েন করুন!", show_alert=True)
            await send_force_sub_message(update, context, unjoined_link, unjoined_name)
            return

    # 1. Main User Navigation Callbacks (Public)
    if data in ("main_menu", "plans_menu", "user_status", "payment_info", "affiliate_menu"):
        USER_STATES.pop(user_id, None)

    if data == "main_menu":
        await safe_ack()
        await render_main_menu(update, context)
        return

    if data == "plans_menu":
        await safe_ack()
        await render_plans_menu(update, context)
        return

    if data == "user_status":
        await safe_ack()
        await cmd_myplan(update, context)
        return

    if data == "user_renew":
        await safe_ack()
        await render_plans_menu(update, context)
        return

    if data == "payment_info":
        await safe_ack()
        await send_payment_info(update, context)
        return

    if data == "get_my_links":
        await cmd_links(update, context)
        return

    if data == "already_joined_alert":
        await safe_ack("✅ আপনি অলরেডি যুক্ত আছেন! লিংক শেয়ারিং সম্পূর্ণ লক করা হয়েছে।", show_alert=True)
        return

    # Plan Selection Callbacks
    if data.startswith("buy_"):
        await safe_ack()
        plan_key = data.replace("buy_", "")
        await handle_buy_plan(update, context, plan_key)
        return

    if data.startswith("submit_trx_"):
        await safe_ack()
        plan_key = data.replace("submit_trx_", "")
        await start_trx_submission(update, context, plan_key)
        return

    # -------------------------------------------------------------------------
    # Affiliate & Reseller User Callbacks (Accessible to ALL Users)
    # -------------------------------------------------------------------------
    if data == "affiliate_menu":
        await safe_ack()
        await render_affiliate_hub(update, context)
        return

    if data == "aff_rules_guide":
        await safe_ack()
        await render_affiliate_rules_guide(query)
        return

    if data == "aff_wholesale_locked":
        await safe_ack("🔒 এই ফিচারটি শুধুমাত্র Verified Reseller দের জন্য! ২টি সফল পেইড সেল সম্পন্ন করলে স্বয়ংক্রিয়ভাবে আনলক হবে।", show_alert=True)
        return

    if data == "aff_wholesale_plans":
        await safe_ack()
        await render_wholesale_plans(query, user_id)
        return

    if data.startswith("ws_buy_"):
        await safe_ack()
        plan_key = data.replace("ws_buy_", "")
        await handle_buy_wholesale(update, context, plan_key)
        return

    if data == "aff_apply_reseller":
        await safe_ack()
        await handle_reseller_apply_init(update, context)
        return

    if data == "aff_app_pending":
        await safe_ack("⏳ আপনার আবেদনটি রিভিউ করা হচ্ছে! অ্যাডমিন যাচাই করার পর স্বয়ংক্রিয়ভাবে নোটিফিকেশন পাবেন।", show_alert=True)
        return

    if data == "aff_payout_req":
        await safe_ack()
        await handle_payout_request_init(update, context)
        return

    if data.startswith("aff_pay_m_"):
        await safe_ack()
        meth = data.replace("aff_pay_m_", "")
        await handle_payout_method_selected(update, context, meth)
        return

    # -------------------------------------------------------------------------
    # 2. Admin Callbacks (Only accessible to Admins)
    # -------------------------------------------------------------------------
    if not is_admin(user_id):
        await safe_ack()
        return

    if data == "admin_dashboard":
        await render_admin_dashboard(update, context)
        return

    # Admin Payment Approval / Rejection
    if data.startswith("adm_app_"):
        await safe_ack("Processing approval...")
        req_id = int(data.replace("adm_app_", ""))
        req = db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await safe_ack("This request has already been processed!", show_alert=True)
            return

        plan_key = req["plan_key"]
        is_wholesale = plan_key.startswith("ws_") if plan_key else False

        if is_wholesale:
            base_plan_key = plan_key.replace("ws_", "")
            plan = PLANS.get(base_plan_key, {"name": base_plan_key, "duration_days": 30, "is_lifetime": False})
            db.update_payment_status(req_id, "approved")

            # Generate single use links for reseller's client
            links = await generate_single_use_links(context.bot, req["user_id"])
            links_lines = []
            for item in links:
                if item.get("url"):
                    tag = "📢 Channel" if item.get("type") == "channel" else "👥 Group"
                    links_lines.append(f"• <b>{item.get('name')}:</b> {item.get('url')}")
            links_block = "\n".join(links_lines) if links_lines else "<i>(কোনো চ্যানেল কনফিগার করা নেই)</i>"

            safe_req_user = html.escape(str(req["username"] or "none"))
            safe_req_trx = html.escape(str(req["trx_id"] or "N/A"))
            safe_admin_name = html.escape(str(query.from_user.username or query.from_user.id))

            reseller_msg = (
                f"🎉 <b>আপনার হোলসেল অর্ডার সফলভাবে অ্যাপ্রুভ হয়েছে! (#{req_id})</b>\n\n"
                f"📦 <b>প্ল্যান:</b> <b>{plan['name']}</b>\n"
                f"🔢 <b>Trx ID:</b> <code>{safe_req_trx}</code>\n\n"
                f"🔗 <b>আপনার ক্লায়েন্টের জন্য এক্সক্লুসিভ ১-টাইম অ্যান্টি-লিক লিংকসমূহ:</b>\n\n"
                f"{links_block}\n\n"
                f"⚠️ <i>এই লিংকগুলো সিঙ্গেল-ইউজ (১ জন ১ বারই জয়েন করতে পারবে)। আপনার ক্লায়েন্টকে এই মেসেজটি ফরোয়ার্ড করে দিন।</i>"
            )

            try:
                await context.bot.send_message(
                    chat_id=req["user_id"],
                    text=reseller_msg,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to DM wholesale links to reseller {req['user_id']}: {e}")

            approved_text = (
                f"✅ <b>WHOLESALE ORDER APPROVED (#{req_id})</b>\n\n"
                f"👤 Reseller: <code>{req['user_id']}</code> (@{safe_req_user})\n"
                f"📦 Plan: <b>{plan['name']} (Wholesale)</b>\n"
                f"🔢 Trx: <code>{safe_req_trx}</code>\n"
                f"🚀 1-Time VIP Links sent to Reseller's DM.\n"
                f"👮 Approved by: @{safe_admin_name}"
            )

            try:
                if query.message.photo or query.message.document:
                    await query.edit_message_caption(caption=approved_text, parse_mode="HTML")
                else:
                    await query.edit_message_text(text=approved_text, parse_mode="HTML")
            except Exception:
                pass
            return

        plan = PLANS.get(plan_key, {"name": plan_key, "duration_days": 30, "is_lifetime": False})
        duration_seconds = plan.get("duration_days", 30) * 86400
        is_lifetime = plan.get("is_lifetime", False)

        sub = db.add_or_update_subscriber(
            user_id=req["user_id"],
            username=req["username"],
            full_name=req["full_name"],
            plan_key=plan_key,
            plan_name=plan["name"],
            duration_seconds=duration_seconds,
            is_lifetime=is_lifetime,
            notes=f"Approved Trx: {req['trx_id']}",
        )
        db.update_payment_status(req_id, "approved")

        # Deliver links to the buyer safely
        delivery_ok = False
        try:
            await deliver_welcome_access(context.bot, req["user_id"], plan["name"], sub["expires_at"], is_lifetime)
            delivery_ok = True
            asyncio.create_task(
                broadcast_vip_join(
                    bot=context.bot,
                    buyer_id=req["user_id"],
                    buyer_username=req.get("username"),
                    buyer_name=req.get("full_name"),
                    plan_name=plan["name"],
                )
            )
        except Exception as e:
            logger.error(f"Failed to deliver links to user {req['user_id']}: {e}")

        # Process Affiliate Commission
        try:
            comm_res = db.process_affiliate_commission(
                buyer_id=req["user_id"],
                payment_req_id=req_id,
                plan_key=plan_key,
                amount_bdt=float(plan.get("price_bdt", 0)),
            )
            if comm_res:
                ref_id = comm_res["referrer_id"]
                if comm_res["reward_type"] == "free_month_extend":
                    ext_days = comm_res["days"]
                    ref_alert = (
                        f"🎉 <b>রেফারেল রিওয়ার্ড অ্যালার্ট!</b>\n\n"
                        f"👤 আপনার রেফারেল লিংক ব্যবহার করে একজন কাস্টমার VIP প্ল্যান গ্রহণ করেছেন!\n"
                        f"👑 আপনার অ্যাকাউন্টে <b>+{ext_days} দিন VIP সাবস্ক্রিপশন সম্পূর্ণ ফ্রিতে</b> বৃদ্ধি করা হয়েছে!\n"
                        f"🎯 মোট সফল পেইড রেফারেল: <b>{comm_res['total_sales']}</b> টি"
                    )
                    if comm_res.get("unlocked_reseller"):
                        ref_alert += (
                            f"\n\n🚀 <b>CONGRATULATIONS!</b>\n"
                            f"আপনি সফলভাবে ২টি সেল সম্পন্ন করে <b>Verified Reseller</b> লেভেলে উন্নীত হয়েছেন! "
                            f"এখন থেকে আপনার ক্যাশ উইথড্র আনলক এবং আপনি সরাসরি ৫০% পাইকারি রেটে ক্লায়েন্ট লিংক কিনতে পারবেন!"
                        )
                    await context.bot.send_message(chat_id=ref_id, text=ref_alert, parse_mode="HTML")
                elif comm_res["reward_type"] == "cash":
                    c_bdt = comm_res["amount_bdt"]
                    ref_alert = (
                        f"💰 <b>কমিশন ক্রেডিট অ্যালার্ট!</b>\n\n"
                        f"👤 আপনার রেফারেল লিংক ব্যবহার করে একজন কাস্টমার VIP প্ল্যান গ্রহণ করেছেন!\n"
                        f"💵 আপনার ওয়ালেটে <b>+{c_bdt:.2f} BDT</b> কমিশন যোগ হয়েছে!\n"
                        f"🎯 মোট সফল পেইড রেফারেল: <b>{comm_res['total_sales']}</b> টি"
                    )
                    if comm_res.get("unlocked_reseller"):
                        ref_alert += (
                            f"\n\n🚀 <b>CONGRATULATIONS!</b>\n"
                            f"আপনি সফলভাবে ২টি সেল সম্পন্ন করে <b>Verified Reseller</b> লেভেলে উন্নীত হয়েছেন! "
                            f"এখন থেকে আপনার ক্যাশ উইথড্র আনলক এবং আপনি সরাসরি ৫০% পাইকারি রেটে ক্লায়েন্ট লিংক কিনতে পারবেন!"
                        )
                    await context.bot.send_message(chat_id=ref_id, text=ref_alert, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error processing affiliate commission for req #{req_id}: {e}")

        dispatch_status = "🚀 1-Time Invite Links dispatched to member's DM." if delivery_ok else "⚠️ Member added in DB, but failed to DM links (User may have blocked bot)."
        safe_req_user = html.escape(str(req["username"] or "none"))
        safe_req_trx = html.escape(str(req["trx_id"] or "N/A"))
        safe_admin_name = html.escape(str(query.from_user.username or query.from_user.id))

        approved_text = (
            f"✅ <b>PAYMENT APPROVED (#{req_id})</b>\n\n"
            f"👤 Member: <code>{req['user_id']}</code> (@{safe_req_user})\n"
            f"📦 Plan: <b>{plan['name']}</b>\n"
            f"🔢 Trx: <code>{safe_req_trx}</code>\n"
            f"{dispatch_status}\n"
            f"👮 Approved by: @{safe_admin_name}"
        )

        try:
            if query.message.photo or query.message.document:
                await query.edit_message_caption(caption=approved_text, parse_mode="HTML")
            else:
                await query.edit_message_text(text=approved_text, parse_mode="HTML")
        except Exception:
            try:
                await query.edit_message_caption(caption=approved_text, parse_mode="HTML")
            except Exception:
                pass
        return

    if data.startswith("adm_rej_"):
        await safe_ack("Processing rejection...")
        req_id = int(data.replace("adm_rej_", ""))
        req = db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await safe_ack("This request has already been processed!", show_alert=True)
            return

        db.update_payment_status(req_id, "rejected")
        safe_rej_trx = html.escape(str(req["trx_id"] or "N/A"))
        rej_user_msg = (
            f"❌ <b>পেমেন্ট যাচাইকরণ ব্যর্থ হয়েছে</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"আপনার প্রেরিত পেমেন্ট প্রুফ / Trx ID (<code>{safe_rej_trx}</code>) অ্যাডমিন কর্তৃক ভেরিফাই করা সম্ভব হয়নি।\n\n"
            f"📌 <b>সম্ভাব্য কারণ:</b>\n"
            f"• ভুল Trx ID অথবা সেন্ড মানি করা হয়েছে (ক্যাশআউট ব্যতীত)\n"
            f"• অস্পষ্ট স্ক্রিনশট বা পূর্বের ব্যবহৃত Trx ID\n\n"
            f"💡 অনুগ্রহ করে সঠিক স্ক্রিনশট ও Trx ID সহ পুনরায় সাবমিট করুন অথবা সহায়তার জন্য অ্যাডমিন সাপোর্টে যোগাযোগ করুন।"
        )
        rej_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Select Plan & Retry", callback_data="plans_menu")],
            [InlineKeyboardButton("💬 Contact Support", url="https://t.me/proaccessteam")],
        ])
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=rej_user_msg,
                parse_mode="HTML",
                reply_markup=rej_markup,
            )
        except Exception:
            pass

        safe_admin_name = html.escape(str(query.from_user.username or query.from_user.id))
        rejected_text = (
            f"❌ <b>Payment Rejected (#{req_id})</b>\n"
            f"👤 Member: <code>{req['user_id']}</code>\n"
            f"🔢 Trx: <code>{safe_rej_trx}</code>\n"
            f"👮 Action by: @{safe_admin_name}"
        )
        try:
            if query.message.photo or query.message.document:
                await query.edit_message_caption(caption=rejected_text, parse_mode="HTML")
            else:
                await query.edit_message_text(text=rejected_text, parse_mode="HTML")
        except Exception:
            try:
                await query.edit_message_caption(caption=rejected_text, parse_mode="HTML")
            except Exception:
                pass
        return

    # Admin Interactive Actions
    if data == "adm_action_add":
        USER_STATES[user_id] = {"action": "admin_awaiting_add_id"}
        await query.edit_message_text(
            "➕ <b>Add Member:</b>\n\nPlease enter the target user's Telegram User ID:\n\n<i>(Type /cancel to abort)</i>",
            parse_mode="HTML",
        )
        return

    if data.startswith("adm_quick_dur_"):
        dur_type = data.replace("adm_quick_dur_", "")
        state = USER_STATES.get(user_id)
        if not state or "target_user_id" not in state:
            await safe_ack("Session expired. Please start again.", show_alert=True)
            return

        target_id = state["target_user_id"]

        if dur_type == "custom":
            state["action"] = "admin_awaiting_custom_duration"
            await query.edit_message_text(
                f"👤 Target: <code>{target_id}</code>\n\nPlease type the custom duration (e.g. <code>15d</code>, <code>45d</code>, <code>12h</code>, <code>3d12h</code>):",
                parse_mode="HTML",
            )
            return

        # Fixed presets
        seconds, label, is_lifetime = parse_duration(dur_type)
        USER_STATES.pop(user_id, None)

        sub = db.add_or_update_subscriber(
            user_id=target_id,
            username=None,
            full_name=None,
            plan_key="admin_preset",
            plan_name=label,
            duration_seconds=seconds,
            is_lifetime=is_lifetime,
        )
        await deliver_welcome_access(context.bot, target_id, label, sub["expires_at"], is_lifetime)

        await query.edit_message_text(
            f"✅ <b>Successfully Added Member!</b>\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"📦 Plan: <b>{label}</b>\n"
            f"⏳ Expiry: <code>{sub['expires_at']}</code>\n"
            f"🚀 1-Time Invite Links dispatched to member's DM.",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_backup":
        await safe_ack("Creating backup...")
        backup_file = db.backup_database()
        with open(backup_file, "rb") as f:
            await context.bot.send_document(
                chat_id=user_id,
                document=f,
                filename=os.path.basename(backup_file),
                caption=f"🛡️ <b>Manual Database Backup</b>\n📅 <code>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>",
                parse_mode="HTML",
            )
        await safe_ack("Backup sent to your Telegram!", show_alert=True)
        return

    if data == "adm_action_list_active":
        await safe_ack()
        active_subs = db.get_active_subscribers()
        if not active_subs:
            await query.edit_message_text("ℹ️ No active subscribers found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_dashboard")]]))
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        lines = [f"👥 <b>ACTIVE SUBSCRIBERS ({len(active_subs)})</b>\n"]
        for idx, s in enumerate(active_subs[:25], 1):
            if s["is_lifetime"]:
                rem = "Lifetime"
            else:
                diff = datetime.datetime.fromisoformat(s["expires_at"]) - now
                rem = f"{diff.days}d {diff.seconds // 3600}h" if diff.total_seconds() > 0 else "Expired"
            lines.append(f"{idx}. <code>{s['user_id']}</code> | <b>{rem}</b> | {s['plan_name']}")

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
        return

    if data == "adm_action_channels":
        await safe_ack()
        channels_list = [c for c in CHANNELS if c.get("type", "channel") == "channel"]
        groups_list = [c for c in CHANNELS if c.get("type") == "group"]

        lines = ["⚙️ <b>CONFIGURED VIP DESTINATIONS:</b>\n"]

        lines.append(f"📢 <b>VIP BROADCAST CHANNELS ({len(channels_list)}):</b>")
        if not channels_list:
            lines.append("<i>• No broadcast channels configured yet.</i>")
        else:
            for idx, ch in enumerate(channels_list, 1):
                ch_id = ch["id"]
                ch_name = ch.get("name", str(ch_id))
                try:
                    member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                    status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
                except Exception as e:
                    status = f"❌ Error: {e}"
                lines.append(f"  {idx}. <b>{ch_name}</b>\n     ID: <code>{ch_id}</code> | Status: {status}")

        lines.append(f"\n👥 <b>VIP DISCUSSION GROUPS ({len(groups_list)}):</b>")
        if not groups_list:
            lines.append("<i>• No discussion groups configured yet.</i>")
        else:
            for idx, grp in enumerate(groups_list, 1):
                grp_id = grp["id"]
                grp_name = grp.get("name", str(grp_id))
                try:
                    member = await context.bot.get_chat_member(chat_id=grp_id, user_id=context.bot.id)
                    status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
                except Exception as e:
                    status = f"❌ Error: {e}"
                lines.append(f"  {idx}. <b>{grp_name}</b>\n     ID: <code>{grp_id}</code> | Status: {status}")

        lines.append("\n📢 <b>FORCE JOIN CHANNEL:</b>")
        if FORCE_CHANNEL_ID:
            lines.append(f"• Name: <b>{FORCE_CHANNEL_NAME}</b>\n  ID: <code>{FORCE_CHANNEL_ID}</code>\n  Link: {FORCE_CHANNEL_LINK}")
        else:
            lines.append("• <i>Disabled / Not set</i>")

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return

    if data == "adm_action_renew":
        USER_STATES[user_id] = {"action": "admin_awaiting_renew_id"}
        await query.edit_message_text(
            "🔄 <b>Renew Member</b>\n\n"
            "Please send the target member's <b>Telegram User ID</b>:\n\n"
            "<i>(Type /cancel to abort)</i>",
            parse_mode="HTML",
        )
        return

    if data.startswith("adm_quick_renew_"):
        dur_type = data.replace("adm_quick_renew_", "")
        state = USER_STATES.get(user_id)
        if not state or "target_user_id" not in state:
            await safe_ack("Session expired. Please start again.", show_alert=True)
            return

        target_id = state["target_user_id"]
        if dur_type == "custom":
            state["action"] = "admin_awaiting_custom_renew_duration"
            await query.edit_message_text(
                f"👤 Target: <code>{target_id}</code>\n\nPlease type the extension duration (e.g. <code>15d</code>, <code>45d</code>, <code>12h</code>):",
                parse_mode="HTML",
            )
            return

        seconds, label, is_lifetime = parse_duration(dur_type)
        USER_STATES.pop(user_id, None)

        sub = db.renew_subscriber(
            user_id=target_id,
            duration_seconds=seconds,
            new_plan_name=label,
            is_lifetime=is_lifetime,
        )

        try:
            renew_msg = (
                f"🎉 <b>আপনার সাবস্ক্রিপশন সফলভাবে রিনিউ হয়েছে!</b>\n\n"
                f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
                f"➕ <b>বর্ধিত সময়:</b> +{label}\n"
                f"⏳ <b>নতুন মেয়াদ শেষ:</b> <code>{sub['expires_at']}</code>"
            )
            await context.bot.send_message(chat_id=target_id, text=renew_msg, parse_mode="HTML")
        except Exception:
            pass

        await query.edit_message_text(
            f"✅ <b>Member Renewed Successfully!</b>\n"
            f"🆔 User ID: <code>{target_id}</code>\n"
            f"➕ Added: <b>+{label}</b>\n"
            f"⏳ New Expiry: <code>{sub['expires_at']}</code>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_kick":
        USER_STATES[user_id] = {"action": "admin_awaiting_kick_id"}
        await query.edit_message_text(
            "🚫 <b>Kick Member</b>\n\n"
            "Please send the target member's <b>Telegram User ID</b> to immediately eject them from all channels:\n\n"
            "<i>(Type /cancel to abort)</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_search":
        USER_STATES[user_id] = {"action": "admin_awaiting_search_query"}
        await query.edit_message_text(
            "🔍 <b>Search Member</b>\n\n"
            "Please send the <b>User ID</b> or <b>@username</b> to look up:\n\n"
            "<i>(Type /cancel to abort)</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_expiring":
        await safe_ack()
        expiring_users = db.get_expiring_soon_subscribers(hours=24)
        if not expiring_users:
            await query.edit_message_text(
                "ℹ️ No subscriptions expiring within the next 24 hours.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]]),
            )
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        lines = [f"⏳ <b>EXPIRING WITHIN 24 HOURS ({len(expiring_users)})</b>\n"]
        for idx, s in enumerate(expiring_users[:25], 1):
            diff = datetime.datetime.fromisoformat(s["expires_at"]) - now
            hrs = max(0, diff.seconds // 3600)
            mins = max(0, (diff.seconds % 3600) // 60)
            uname = f"@{s['username']}" if s.get("username") else "No @"
            lines.append(f"{idx}. <code>{s['user_id']}</code> ({uname})\n   ⏳ Left: <b>{hrs}h {mins}m</b> | {s['plan_name']}")

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
        return

    if data == "adm_action_broadcast":
        USER_STATES[user_id] = {"action": "admin_awaiting_broadcast_text"}
        await query.edit_message_text(
            "📢 <b>Broadcast Announcement</b>\n\n"
            "Type the message you want to broadcast to all active members (HTML formatting supported):\n\n"
            "<i>(Type /cancel to abort)</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_add_channel":
        USER_STATES[user_id] = {"action": "admin_awaiting_channel_input"}
        await query.edit_message_text(
            "➕ <b>Add VIP Channel</b>\n\n"
            "Please send the <b>Channel ID</b> and optional <b>Name</b>:\n"
            "Example: <code>-1001234567890 Main Course Vault</code>\n\n"
            "<i>Make sure the bot is already an Admin in that channel with 'Invite' and 'Ban' rights.\nType /cancel to abort.</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_add_group":
        USER_STATES[user_id] = {"action": "admin_awaiting_group_input"}
        await query.edit_message_text(
            "➕ <b>Add VIP Discussion / Support Group</b>\n\n"
            "Please send the <b>Group ID</b> and optional <b>Name</b>:\n"
            "Example: <code>-1001234567890 VIP Support & Discussion</code>\n\n"
            "<i>Make sure the bot is already an Admin in that group with 'Invite' and 'Ban' rights.\nType /cancel to abort.</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_preview":
        curr = f"<code>{VIP_PREVIEW_LINK}</code> ({VIP_PREVIEW_NAME})" if VIP_PREVIEW_LINK else "<i>Disabled / Not set</i>"
        rules_preview = get_rules_text()
        text = (
            "👁️ <b>VIP PREVIEW CHANNEL & RULES MANAGER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>বর্তমান প্রিভিউ লিংক:</b> {curr}\n\n"
            f"📝 <b>বর্তমান নিয়মাবলী ও প্রাইস-লক নোটিস:</b>\n"
            f"<i>{rules_preview}</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>কুইক কমান্ডস:</b>\n"
            "• লিংক সেট করতে: <code>/setpreview &lt;link&gt;</code>\n"
            "• নিয়মাবলী সেট করতে: <code>/setrules &lt;text&gt;</code>\n"
            "• নিয়মাবলী রিসেট করতে: <code>/setrules default</code>"
        )
        buttons = [
            [
                InlineKeyboardButton("✏️ Set Preview Link", callback_data="adm_set_preview_prompt"),
                InlineKeyboardButton("📝 Edit Rules Text", callback_data="adm_set_rules_prompt"),
            ],
            [
                InlineKeyboardButton("🔄 Reset Rules (Default)", callback_data="adm_reset_rules_default"),
            ],
        ]
        if VIP_PREVIEW_LINK and VIP_PREVIEW_LINK != "https://t.me/PAV_CourseEdition/16":
            buttons[1].append(InlineKeyboardButton("❌ Remove Custom Link", callback_data="adm_remove_preview_link"))
        buttons.append([InlineKeyboardButton("🔙 Back to Admin Dashboard", callback_data="admin_dashboard")])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
        return

    if data == "adm_set_rules_prompt":
        USER_STATES[user_id] = {"action": "admin_awaiting_rules_text"}
        curr_rules = get_rules_text()
        await query.edit_message_text(
            "📝 <b>ভিআইপি নিয়মাবলী ও প্রাইস-লক নোটিস এডিটর:</b>\n\n"
            "বর্তমান নোটিস:\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{curr_rules}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            "👉 আপনার নতুন নিয়মাবলী টেক্সটটি লিখে পাঠিয়ে দিন (HTML ট্যাগ সাপোর্ট করে)।\n\n"
            "<i>(ডিফল্টে ফিরে যেতে চাইলে 'default' লিখে পাঠান, অথবা বাতিল করতে /cancel লিখুন)</i>",
            parse_mode="HTML"
        )
        return

    if data == "adm_reset_rules_default":
        db.set_setting("vip_rules_text", "")
        db.log_activity(user_id, "RULES_RESET_DEFAULT", "Reset via admin button")
        await safe_ack("✅ নিয়মাবলী সফলভাবে ডিফল্টে রিসেট করা হয়েছে!", show_alert=True)
        curr = f"<code>{VIP_PREVIEW_LINK}</code> ({VIP_PREVIEW_NAME})" if VIP_PREVIEW_LINK else "<i>Disabled / Not set</i>"
        rules_preview = DEFAULT_RULES_TEXT
        text = (
            "👁️ <b>VIP PREVIEW CHANNEL & RULES MANAGER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>বর্তমান প্রিভিউ লিংক:</b> {curr}\n\n"
            f"📝 <b>বর্তমান নিয়মাবলী ও প্রাইস-লক নোটিস (ডিফল্ট):</b>\n"
            f"<i>{rules_preview}</i>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>কুইক কমান্ডস:</b>\n"
            "• লিংক সেট করতে: <code>/setpreview &lt;link&gt;</code>\n"
            "• নিয়মাবলী সেট করতে: <code>/setrules &lt;text&gt;</code>\n"
            "• নিয়মাবলী রিসেট করতে: <code>/setrules default</code>"
        )
        buttons = [
            [
                InlineKeyboardButton("✏️ Set Preview Link", callback_data="adm_set_preview_prompt"),
                InlineKeyboardButton("📝 Edit Rules Text", callback_data="adm_set_rules_prompt"),
            ],
            [
                InlineKeyboardButton("🔄 Reset Rules (Default)", callback_data="adm_reset_rules_default"),
            ],
        ]
        if VIP_PREVIEW_LINK and VIP_PREVIEW_LINK != "https://t.me/PAV_CourseEdition/16":
            buttons[1].append(InlineKeyboardButton("❌ Remove Custom Link", callback_data="adm_remove_preview_link"))
        buttons.append([InlineKeyboardButton("🔙 Back to Admin Dashboard", callback_data="admin_dashboard")])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=True)
        return

    if data == "adm_set_preview_prompt":
        USER_STATES[user_id] = {"action": "admin_awaiting_preview_link"}
        await query.edit_message_text(
            "👁️ <b>Set VIP Preview Channel Link:</b>\n\n"
            "দয়া করে আপনার VIP Preview চ্যানেলের লিংক (যেমন: <code>https://t.me/your_preview_channel</code> বা <code>@channel_username</code>) পাঠিয়ে দিন:\n\n"
            "<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_remove_preview_link":
        VIP_PREVIEW_LINK = "https://t.me/PAV_CourseEdition/16"
        VIP_PREVIEW_NAME = "VIP Preview & Proof Channel"
        db.set_setting("vip_preview_link", "")
        db.set_setting("vip_preview_name", "")
        save_config()
        await safe_ack("Custom preview link removed, reset to default!", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_action_del_channel":
        if not CHANNELS:
            await safe_ack("No channels or groups configured to delete!", show_alert=True)
            return
        buttons = []
        for ch in CHANNELS:
            icon = "📢" if ch.get("type", "channel") == "channel" else "👥"
            buttons.append([InlineKeyboardButton(f"🗑️ Remove {icon} {ch['name']}", callback_data=f"adm_confirm_del_ch_{ch['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")])
        await query.edit_message_text("🗑️ <b>Select Channel or Group to Remove:</b>", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("adm_confirm_del_ch_"):
        ch_id = int(data.replace("adm_confirm_del_ch_", ""))
        db.delete_channel(ch_id)
        CHANNELS[:] = [ch for ch in CHANNELS if ch["id"] != ch_id]
        save_config()
        await safe_ack("Destination removed!", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_toggle_join_bc":
        cur_bc = db.get_setting("vip_join_broadcast_enabled", "1") == "1"
        new_bc = "0" if cur_bc else "1"
        db.set_setting("vip_join_broadcast_enabled", new_bc)
        db.log_activity(user_id, "JOIN_BROADCAST_TOGGLED", f"Set to {new_bc}")
        status_label = "ENABLED ✅" if new_bc == "1" else "DISABLED ❌"
        await safe_ack(f"VIP Join Broadcast is now {status_label}", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_action_reminders":
        USER_STATES[user_id] = {"action": "admin_awaiting_reminder_stages"}
        cur_stages = db.get_setting("reminder_stages", "24,12,1")
        await query.edit_message_text(
            f"⏱️ <b>SET EXPIRATION REMINDER SCHEDULE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"বর্তমান শিডিউল: <b>{cur_stages} ঘণ্টা আগে</b>\n\n"
            f"অটো-কিকের কত কত ঘণ্টা আগে নোটিশ পাঠাতে চান তা স্পেস বা কমা দিয়ে লিখে পাঠান:\n"
            f"উদাহরণ: <code>24 12 1</code> অথবা <code>48 24 6 1</code>\n\n"
            f"<i>💡 প্রতিটি স্টেজ ইউজারের কাছে স্বয়ংক্রিয়ভাবে আলাদা আলাদা কাউন্টডাউন ও রিনিউ বাটনসহ পৌঁছাবে।</i>\n\n"
            f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back", callback_data="admin_dashboard")]]),
        )
        return

    if data == "adm_action_clear_logs":
        live_log_buffer.clear()
        await safe_ack("🧹 Live logs & error buffer cleared!", show_alert=True)
        data = "adm_action_logs"

    if data == "adm_action_logs":
        await safe_ack()

        # 1. API Latency Test
        t0 = time.perf_counter()
        try:
            await context.bot.get_me()
            ping_ms = round((time.perf_counter() - t0) * 1000)
            ping_status = f"🟢 Connected ({ping_ms}ms latency)"
        except Exception as e:
            ping_status = f"🔴 Telegram API Error: {e}"

        # 2. Database Health
        try:
            db_size_kb = round(os.path.getsize(db.db_path) / 1024, 1)
            db_status = f"🟢 Healthy (WAL Mode | Size: {db_size_kb} KB)"
        except Exception as e:
            db_status = f"⚠️ Status: {e}"

        stats = db.get_stats()

        # 3. Channels and Groups Audit
        audit_lines = []
        if not CHANNELS:
            audit_lines.append("• <i>No VIP channels or groups configured yet.</i>")
        else:
            for ch in CHANNELS:
                ch_id = ch["id"]
                ch_name = ch.get("name", str(ch_id))
                ch_type = ch.get("type", "channel")
                icon = "📢" if ch_type == "channel" else "👥"
                try:
                    member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                    is_adm = member.status in ("administrator", "creator")
                    if is_adm:
                        can_inv = getattr(member, "can_invite_users", True)
                        can_ban = getattr(member, "can_restrict_members", True)
                        if can_inv and can_ban:
                            perm_str = "🟢 OK (Invite: ✅ | Ban: ✅)"
                        else:
                            perm_str = f"⚠️ Limited (Invite: {'✅' if can_inv else '❌'} | Ban: {'✅' if can_ban else '❌'})"
                    else:
                        perm_str = "🔴 Bot is NOT Admin!"
                except Exception as e:
                    perm_str = f"🔴 Error: {str(e)[:30]}"
                audit_lines.append(f"• {icon} <b>{ch_name}</b>: {perm_str}")

        if FORCE_CHANNEL_ID:
            try:
                f_id = int(FORCE_CHANNEL_ID) if FORCE_CHANNEL_ID.lstrip("-").isdigit() else FORCE_CHANNEL_ID
                f_mem = await context.bot.get_chat_member(chat_id=f_id, user_id=context.bot.id)
                f_status = "🟢 Verified (Admin)" if f_mem.status in ("administrator", "creator") else "⚠️ Bot not admin"
            except Exception as e:
                f_status = f"⚠️ Warning ({str(e)[:25]})"
            audit_lines.append(f"• 📢 <b>Force Join ({FORCE_CHANNEL_NAME})</b>: {f_status}")

        # 4. Logs Box
        mem_logs = live_log_buffer.get_logs(limit=8)
        log_entries = []
        if mem_logs:
            for l in mem_logs:
                log_entries.append(l.replace("<", "&lt;").replace(">", "&gt;"))
        else:
            recent_db = db.get_recent_logs(limit=6)
            for r in recent_db:
                ts = r['timestamp'][11:19] if len(r['timestamp']) >= 19 else r['timestamp']
                log_entries.append(f"[{ts}] {r['action']}: {r['details']}")

        if not log_entries:
            log_box = "<i>No recent system events or errors recorded. System normal.</i>"
        else:
            log_box = "<pre>" + "\n".join(log_entries[-10:]) + "</pre>"

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S UTC")

        diag_text = (
            f"📋 <b>SYSTEM HEALTH &amp; LIVE DIAGNOSTICS</b>\n"
            f"════════════════════════════════════\n"
            f"⏱️ <b>Checked at:</b> <code>{now_utc}</code>\n"
            f"⚡ <b>Telegram API:</b> {ping_status}\n"
            f"💾 <b>Database Engine:</b> {db_status}\n\n"
            f"📊 <b>LIVE METRICS:</b>\n"
            f"• Active VIP Subscribers: <b>{stats['total_active']}</b>\n"
            f"• Expiring within 24h: <b>{stats['expiring_24h']}</b>\n"
            f"• Lifetime Members: <b>{stats['total_lifetime']}</b>\n"
            f"• Pending Payment Approvals: <b>{stats['pending_payments']}</b>\n\n"
            f"🛡️ <b>PERMISSIONS AUDIT:</b>\n"
            + "\n".join(audit_lines) + "\n\n"
            f"📋 <b>LIVE SYSTEM LOG STREAM:</b>\n"
            f"{log_box}\n"
            f"════════════════════════════════════"
        )

        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh Logs", callback_data="adm_action_logs"),
                InlineKeyboardButton("🧹 Clear Buffer", callback_data="adm_action_clear_logs"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard"),
            ]
        ]
        try:
            await query.edit_message_text(diag_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
        except Exception:
            pass
        return

    if data == "adm_action_edit_payments":
        text = (
            "💳 <b>CURRENT PAYMENT CREDENTIALS:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>bKash:</b> <code>{PAYMENT_INFO.get('bkash', 'N/A')}</code>\n"
            f"📱 <b>Nagad:</b> <code>{PAYMENT_INFO.get('nagad', 'N/A')}</code>\n"
            f"🌐 <b>Binance Pay:</b> <code>{PAYMENT_INFO.get('binance_pay_id', 'N/A')}</code>\n"
            f"🪙 <b>USDT TRC20:</b> <code>{PAYMENT_INFO.get('usdt_trc20', 'N/A')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "যেকোনো পেমেন্ট মেথড আপডেট করতে নিচের বাটনে ট্যাপ করুন অথবা কমান্ড পাঠান:\n"
            "<code>/setpayment &lt;bkash|nagad|binance|usdt&gt; &lt;details&gt;</code>"
        )
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📱 Edit bKash", callback_data="adm_change_pay_bkash"),
                InlineKeyboardButton("📱 Edit Nagad", callback_data="adm_change_pay_nagad"),
            ],
            [
                InlineKeyboardButton("🌐 Edit Binance Pay", callback_data="adm_change_pay_binance"),
                InlineKeyboardButton("🪙 Edit USDT TRC20", callback_data="adm_change_pay_usdt"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard"),
            ]
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data.startswith("adm_change_pay_"):
        pay_key = data.replace("adm_change_pay_", "")
        USER_STATES[user_id] = {"action": "admin_awaiting_payment_input", "pay_key": pay_key}
        label = {"bkash": "bKash Agent Number", "nagad": "Nagad Agent Number", "binance": "Binance Pay ID", "usdt": "USDT TRC20 Address"}.get(pay_key, pay_key)
        curr = PAYMENT_INFO.get("binance_pay_id" if pay_key == "binance" else ("usdt_trc20" if pay_key == "usdt" else pay_key), "N/A")
        await query.edit_message_text(
            f"✏️ <b>Update {label}</b>\n\n"
            f"বর্তমান ভ্যালু: <code>{curr}</code>\n\n"
            f"নতুন নম্বর বা অ্যাড্রেসটি লিখে মেসেজ পাঠান:\n\n"
            f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
            parse_mode="HTML",
        )
        return

    if data == "adm_action_edit_prices":
        rate = get_dollar_rate()
        em_crw = em_tag("crown", "👑")
        em_dia = em_tag("diamond", "💎")
        box_border = "━━━━━━━━━━━━━━━━━━━━"

        lines = [
            f"{em_dia} <b>MANAGE VIP PLAN PRICING & DOLLAR RATE</b> {em_crw}\n",
            f"{box_border}",
            f"💵 <b>Current Dollar Rate:</b> <code>1 USD = {rate:g} BDT</code>\n",
            "যেকোনো প্ল্যানের মূল্য অথবা ডলার রেট পরিবর্তন করতে নিচের বাটনে ট্যাপ করুন:\n",
        ]
        for pk in ("monthly", "yearly", "lifetime"):
            pv = PLANS.get(pk)
            if pv:
                lines.append(f"• <b>{pv['name']}:</b>\n  মূল্য: <b>{format_price(pv['price_bdt'])}</b>\n")
        lines.append(f"{box_border}")
        lines.append("<i>বাটনে ক্লিক করার পর নতুন মান লিখুন অথবা কমান্ড ব্যবহার করুন: <code>/setdollar &lt;rate&gt;</code></i>")

        buttons = [
            [InlineKeyboardButton(f"💵 Set Dollar Rate (1 USD = {rate:g} BDT)", callback_data="adm_change_dollar_rate")],
            [InlineKeyboardButton(f"📅 Change Monthly ({format_btn_price(PLANS.get('monthly', {}).get('price_bdt', 0))})", callback_data="adm_change_price_monthly")],
            [InlineKeyboardButton(f"📆 Change Yearly ({format_btn_price(PLANS.get('yearly', {}).get('price_bdt', 0))})", callback_data="adm_change_price_yearly")],
            [InlineKeyboardButton(f"👑 Change Lifetime ({format_btn_price(PLANS.get('lifetime', {}).get('price_bdt', 0))})", callback_data="adm_change_price_lifetime")],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")],
        ]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "adm_change_dollar_rate":
        USER_STATES[user_id] = {"action": "admin_awaiting_dollar_rate"}
        cur_rate = get_dollar_rate()
        await query.edit_message_text(
            f"💵 <b>SET DOLLAR EXCHANGE RATE (USDT/USD)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"বর্তমান রেট: <b>1 USD = {cur_rate:g} BDT</b>\n\n"
            f"প্রতি ১ ডলারে কত টাকা হবে তা লিখে পাঠান (যেমন: <code>125</code> অথবা <code>130</code>):\n\n"
            f"<i>💡 রেট সেট করার সাথে সাথে সবকয়টি প্ল্যানের ডলারের দাম স্বয়ংক্রিয়ভাবে রিয়েল-টাইমে আপডেট হয়ে যাবে!</i>\n\n"
            f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back", callback_data="adm_action_edit_prices")]]),
        )
        return

    if data.startswith("adm_change_price_"):
        plan_key = data.replace("adm_change_price_", "")
        if plan_key in PLANS:
            USER_STATES[user_id] = {"action": "admin_awaiting_plan_price", "plan_key": plan_key}
            await query.edit_message_text(
                f"💰 <b>Set Price for {PLANS[plan_key]['name']}</b>\n\n"
                f"বর্তমান মূল্য: <b>{format_price(PLANS[plan_key]['price_bdt'])}</b>\n\n"
                f"নতুন <b>BDT প্রাইস</b> লিখে মেসেজ পাঠান (যেমন: <code>750</code>)।\n"
                f"<i>(ডলার রেট অনুযায়ী USDT এর মূল্য স্বয়ংক্রিয়ভাবে ক্যালকুলেট হয়ে যাবে)</i>\n\n"
                f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back", callback_data="adm_action_edit_prices")]]),
            )
        return

    # -------------------------------------------------------------------------
    # Admin Affiliate Management Callbacks
    # -------------------------------------------------------------------------
    if data == "adm_aff_center":
        if is_admin(user_id):
            await safe_ack()
            await render_admin_affiliate_center(query)
        return

    if data == "adm_aff_payouts":
        if is_admin(user_id):
            await safe_ack()
            await render_admin_pending_payouts(query)
        return

    if data == "adm_aff_settings":
        if is_admin(user_id):
            await safe_ack()
            await render_admin_affiliate_settings(query)
        return

    if data == "adm_cfg_toggle":
        if is_admin(user_id):
            curr = db.get_setting("affiliate_enabled", "1")
            new_val = "0" if curr == "1" else "1"
            db.set_setting("affiliate_enabled", new_val)
            await safe_ack(f"System: {'ON 🟢' if new_val == '1' else 'OFF 🔴'}")
            await render_admin_affiliate_settings(query)
        return

    if data.startswith("adm_cfg_"):
        if is_admin(user_id):
            await safe_ack()
            sub_key = data.replace("adm_cfg_", "")
            cfg_meta = {
                "comm": ("affiliate_cash_comm_pct", "কমিশন শতকরা হার (%)", "উদাহরণ: 50"),
                "extend": ("affiliate_monthly_vip_extend_days", "Monthly VIP ফ্রি বর্ধিত দিন সংখ্যা", "উদাহরণ: 30"),
                "unlock": ("affiliate_min_sales_unlock", "রিসেলার আনলকের জন্য ন্যূনতম সেল", "উদাহরণ: 2"),
                "minpay": ("affiliate_min_payout_bdt", "ন্যূনতম পে-আউট পরিমাণ (BDT)", "উদাহরণ: 500"),
                "wsdisc": ("affiliate_wholesale_discount_pct", "হোলসেল পাইকারি ছাড়ের হার (%)", "উদাহরণ: 50"),
            }
            if sub_key in cfg_meta:
                real_key, label, example = cfg_meta[sub_key]
                USER_STATES[user_id] = {
                    "action": "admin_awaiting_aff_setting",
                    "key": real_key,
                    "label": label,
                }
                curr_val = db.get_setting(real_key, "")
                await query.edit_message_text(
                    f"⚙️ <b>Edit Setting: {label}</b>\n\n"
                    f"বর্তমান মান: <b>{curr_val}</b>\n\n"
                    f"নতুন মান লিখে পাঠান ({example}):\n\n"
                    f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
                    parse_mode="HTML",
                )
        return

    if data.startswith("aff_p_app_"):
        if is_admin(user_id):
            await safe_ack("Approving payout...")
            payout_id = int(data.replace("aff_p_app_", ""))
            ok, msg, p_data = db.approve_payout(payout_id)
            if ok and p_data:
                try:
                    await context.bot.send_message(
                        chat_id=p_data["user_id"],
                        text=(
                            f"🎉 <b>আপনার পে-আউট অ্যাপ্রুভ হয়েছে!</b>\n\n"
                            f"💰 <b>পরিমাণ:</b> <b>{p_data['amount_bdt']:.2f} BDT</b>\n"
                            f"💳 <b>মেথড:</b> <b>{p_data['payment_method']}</b>\n"
                            f"📱 <b>অ্যাকাউন্ট:</b> <code>{p_data['account_number']}</code>\n\n"
                            f"✅ টাকা আপনার অ্যাকাউন্টে পাঠিয়ে দেওয়া হয়েছে। ধন্যবাদ!"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                approved_str = (
                    f"✅ <b>PAYOUT APPROVED (#{payout_id})</b>\n\n"
                    f"👤 User: <code>{p_data['user_id']}</code>\n"
                    f"💰 Amount: <b>{p_data['amount_bdt']:.2f} BDT</b>\n"
                    f"💳 Method: <b>{p_data['payment_method']}</b> (<code>{p_data['account_number']}</code>)\n"
                    f"👮 Action by: @{html.escape(str(query.from_user.username or query.from_user.id))}"
                )
                try:
                    await query.edit_message_text(approved_str, parse_mode="HTML")
                except Exception:
                    pass
            else:
                await safe_ack(f"❌ {msg}", show_alert=True)
        return

    if data.startswith("aff_p_rej_"):
        if is_admin(user_id):
            await safe_ack("Rejecting payout...")
            payout_id = int(data.replace("aff_p_rej_", ""))
            ok, msg, p_data = db.reject_payout(payout_id, "Rejected by admin")
            if ok and p_data:
                try:
                    await context.bot.send_message(
                        chat_id=p_data["user_id"],
                        text=(
                            f"❌ <b>পে-আউট বাতিল করা হয়েছে!</b>\n\n"
                            f"💰 <b>পরিমাণ:</b> <b>{p_data['amount_bdt']:.2f} BDT</b>\n"
                            f"💳 <b>মেথড:</b> <b>{p_data['payment_method']}</b>\n\n"
                            f"টাকা আপনার ওয়ালেটে ফেরত দেওয়া হয়েছে। কোনো জিজ্ঞাসা থাকলে সাপোর্টে যোগাযোগ করুন।"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                rejected_str = (
                    f"❌ <b>PAYOUT REJECTED (#{payout_id})</b>\n\n"
                    f"👤 User: <code>{p_data['user_id']}</code>\n"
                    f"💰 Amount: <b>{p_data['amount_bdt']:.2f} BDT (Refunded)</b>\n"
                    f"👮 Action by: @{html.escape(str(query.from_user.username or query.from_user.id))}"
                )
                try:
                    await query.edit_message_text(rejected_str, parse_mode="HTML")
                except Exception:
                    pass
            else:
                await safe_ack(f"❌ {msg}", show_alert=True)
    if data == "adm_aff_apps":
        if is_admin(user_id):
            await safe_ack()
            await render_admin_pending_reseller_apps(query)
        return

    if data.startswith("res_app_ok_"):
        if is_admin(user_id):
            await safe_ack("Approving application...")
            app_id = int(data.replace("res_app_ok_", ""))
            ok, msg, app_data = db.approve_reseller_application(app_id, admin_id=user_id)
            if ok and app_data:
                congrats_text = (
                    f"🎉 <b>অভিনন্দন! আপনার Verified Reseller আবেদন অনুমোদিত হয়েছে!</b>\n\n"
                    f"💼 <b>আবেদন আইডি:</b> <code>#{app_id}</code>\n"
                    f"🎖️ <b>নতুন স্ট্যাটাস:</b> <b>Level 2: Verified Reseller</b> 🟢\n\n"
                    f"🚀 <b>আপনার জন্য নতুন সুবিধা:</b>\n"
                    f"• এখন থেকে পাইকারি (৫০% ছাড়) রেটে ক্লায়েন্টের জন্য ইনস্ট্যান্ট ১-টাইম VIP লিঙ্ক কিনতে পারবেন\n"
                    f"• ক্লায়েন্টের কাছ থেকে পুরো টাকা নিয়ে বাকি অংশ আপনার নিজের লাভ হিসেবে থাকবে\n"
                    f"• রেফারেল ক্যাশ পে-আউট সরাসরি bKash/Nagad/Binance এ তুলতে পারবেন\n\n"
                    f"👇 নিচের বাটনে চাপ দিয়ে এখনই পোর্টাল দেখুন:"
                )
                congrats_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🤝 Open Reseller Hub", callback_data="affiliate_menu")]])
                try:
                    await context.bot.send_message(
                        chat_id=app_data["user_id"],
                        text=congrats_text,
                        parse_mode="HTML",
                        reply_markup=congrats_markup,
                    )
                except Exception as e:
                    logger.error(f"Failed to DM approved reseller {app_data['user_id']}: {e}")

                safe_admin_name = html.escape(str(query.from_user.username or user_id))
                safe_user_uname = html.escape(str(app_data.get("username") or "none"))
                safe_plan = html.escape(str(app_data.get("marketing_plan") or ""))
                updated_text = (
                    f"✅ <b>VERIFIED RESELLER APPLICATION APPROVED (#{app_id})</b>\n\n"
                    f"👤 <b>Applicant:</b> <code>{app_data['user_id']}</code> (@{safe_user_uname})\n"
                    f"🎖️ <b>Status:</b> Upgraded to <b>Verified Reseller</b>\n"
                    f"👮 <b>Approved by:</b> @{safe_admin_name}\n\n"
                    f"📝 <b>Submitted Marketing Details:</b>\n"
                    f"<blockquote>{safe_plan}</blockquote>"
                )
                try:
                    await query.edit_message_text(updated_text, parse_mode="HTML")
                except Exception:
                    pass
            else:
                await safe_ack(f"❌ {msg}", show_alert=True)
        return

    if data.startswith("res_app_no_"):
        if is_admin(user_id):
            await safe_ack("Rejecting application...")
            app_id = int(data.replace("res_app_no_", ""))
            ok, msg, app_data = db.reject_reseller_application(app_id, admin_id=user_id, reason="Rejected by admin")
            if ok and app_data:
                rej_text = (
                    f"ℹ️ <b>ভেরিফাইড রিসেলার আবেদন সংক্রান্ত তথ্য (#{app_id})</b>\n\n"
                    f"আপনার রিসেলার আবেদনটি এই মুহূর্তে গৃহীত হয়নি।\n"
                    f"আপনার চ্যানেল/গ্রুপ লিঙ্ক ও মার্কেটিং পরিকল্পনা আরো বিস্তারিতভাবে উল্লেখ করে পরবর্তীতে পুনরায় আবেদন করতে পারেন অথবা ২টি সফল রেফারেল সেল সম্পন্ন করে স্বয়ংক্রিয়ভাবে রিসেলার হতে পারেন।"
                )
                try:
                    await context.bot.send_message(
                        chat_id=app_data["user_id"],
                        text=rej_text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Failed to DM rejected applicant {app_data['user_id']}: {e}")

                safe_admin_name = html.escape(str(query.from_user.username or user_id))
                safe_user_uname = html.escape(str(app_data.get("username") or "none"))
                safe_plan = html.escape(str(app_data.get("marketing_plan") or ""))
                updated_text = (
                    f"❌ <b>RESELLER APPLICATION REJECTED (#{app_id})</b>\n\n"
                    f"👤 <b>Applicant:</b> <code>{app_data['user_id']}</code> (@{safe_user_uname})\n"
                    f"👮 <b>Rejected by:</b> @{safe_admin_name}\n\n"
                    f"📝 <b>Submitted Marketing Details:</b>\n"
                    f"<blockquote>{safe_plan}</blockquote>"
                )
                try:
                    await query.edit_message_text(updated_text, parse_mode="HTML")
                except Exception:
                    pass
            else:
                await safe_ack(f"❌ {msg}", show_alert=True)
        return

    # Fallback acknowledge if no handler answered
    await safe_ack()


# =============================================================================
# AFFILIATE & RESELLER HUB UI & ADMIN CONTROLS
# =============================================================================
async def render_affiliate_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Renders the aesthetic Affiliate & Reseller Hub Dashboard."""
    user = update.effective_user
    user_id = user.id

    aff = db.get_or_create_affiliate(user_id)
    min_unlock = int(db.get_setting("affiliate_min_sales_unlock", "2"))
    extend_days = int(db.get_setting("affiliate_monthly_vip_extend_days", "30"))
    ws_discount = float(db.get_setting("affiliate_wholesale_discount_pct", "50"))
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username or "PAV_CourseEdition_bot"
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    is_reseller = aff.get("role") == "reseller" or aff.get("total_sales", 0) >= min_unlock
    effective_tier = aff.get("effective_tier", "trainee")

    if is_reseller:
        badge = "💼 <b>Level 2: Verified Reseller</b> 🟢"
        tier_perk = f"• <i>ক্যাশ উইথড্র সম্পূর্ণ আনলকড ও ক্লায়েন্টের জন্য {int(ws_discount)}% পাইকারি রেটে লিংক কেনার সুবিধা সক্রিয়।</i>"
    elif effective_tier == "vip_monthly":
        badge = f"👑 <b>VIP Member (+{extend_days}d Free Renewal Mode)</b>"
        tier_perk = f"• <i>প্রতিটি সফল পেইড রেফারেলে আপনার VIP অ্যাকাউন্টে <b>+{extend_days} দিন সম্পূর্ণ ফ্রি</b> যোগ হবে!</i>"
    else:
        badge = "⭐ <b>Level 1: Trainee Promoter</b>"
        tier_perk = f"• <i>কমিশন ওয়ালেটে জমা হচ্ছে। দ্রুত রিসেলার হতে <b>'Apply for Verified Reseller'</b> বাটনে আবেদন করুন অথবা {min_unlock}টি সেল সম্পন্ন করুন।</i>"

    sales = aff.get("total_sales", 0)
    filled_blocks = min(5, int((sales / max(1, min_unlock)) * 5))
    progress_bar = "█" * filled_blocks + "░" * (5 - filled_blocks)
    box_border = "━━━━━━━━━━━━━━━━━━━━"

    text = (
        f"🤝 <b>{PRODUCT_NAME} — AFFILIATE & RESELLER HUB</b>\n"
        f"{box_border}\n"
        f"👤 <b>প্রোফাইল আইডি:</b> <code>#{user_id}</code>\n"
        f"🎖️ <b>বর্তমান টায়ার:</b> {badge}\n"
        f"{tier_perk}\n"
        f"{box_border}\n"
        f"📊 <b>লাইভ পারফরম্যান্স ড্যাশবোর্ড:</b>\n"
        f"• 👥 <b>টোটাল রেফারেল লিড:</b> <b>{aff['total_leads']}</b> জন\n"
        f"• 🎯 <b>সফল পেইড সেলস:</b> <b>{sales}</b> টি\n"
    )

    if not is_reseller and effective_tier != "vip_monthly":
        text += f"• 🚀 <b>রিসেলার আনলক প্রোগ্রেস:</b> <code>[{progress_bar}] {sales}/{min_unlock}</code>\n"

    text += (
        f"• 💰 <b>উইথড্রযোগ্য ব্যালেন্স:</b> <b>{aff['unpaid_balance_bdt']:.2f} BDT</b>\n"
        f"• 💳 <b>মোট পেইড (উইথড্রন):</b> <b>{aff['paid_balance_bdt']:.2f} BDT</b>\n"
    )

    if aff.get("free_months_earned", 0) > 0 or effective_tier == "vip_monthly":
        text += f"• 👑 <b>ফ্রি VIP দিন/মাস অর্জন:</b> <b>{aff['free_months_earned']}</b> মাস (+{aff['free_months_earned'] * extend_days} দিন)\n"

    text += (
        f"{box_border}\n"
        f"🔗 <b>আপনার পার্সোনাল রেফারেল লিংক:</b>\n"
        f"<code>{ref_link}</code>\n"
        f"<i>(লিংকের ওপর ট্যাপ করলেই কপি হয়ে যাবে। যেকোনো গ্রুপ বা সোশ্যাল মিডিয়ায় প্রমোট করুন)</i>\n"
    )

    keyboard = []
    if is_reseller or is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🛒 Reseller Wholesale Portal (50% Off)", callback_data="aff_wholesale_plans")])
        keyboard.append([InlineKeyboardButton("💸 Request Payout", callback_data="aff_payout_req")])
    else:
        pending_app = db.get_pending_reseller_app(user_id)
        if pending_app:
            keyboard.append([InlineKeyboardButton("⏳ Reseller Application Under Review", callback_data="aff_app_pending")])
        else:
            keyboard.append([InlineKeyboardButton("📝 Apply for Verified Reseller", callback_data="aff_apply_reseller")])
        keyboard.append([
            InlineKeyboardButton("💸 Request Payout", callback_data="aff_payout_req"),
            InlineKeyboardButton("🛒 Wholesale (Locked 🔒)", callback_data="aff_wholesale_locked"),
        ])

    keyboard.append([InlineKeyboardButton("📜 Rules & Progression Guide", callback_data="aff_rules_guide")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])

    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def render_affiliate_rules_guide(query: Any):
    """Explains the 3-Tier model, progression, wholesale benefits, and anti-fraud rules."""
    extend_days = int(db.get_setting("affiliate_monthly_vip_extend_days", "30"))
    comm_pct = int(float(db.get_setting("affiliate_cash_comm_pct", "50")))
    min_unlock = int(db.get_setting("affiliate_min_sales_unlock", "2"))
    min_payout = int(float(db.get_setting("affiliate_min_payout_bdt", "500")))
    ws_discount = int(float(db.get_setting("affiliate_wholesale_discount_pct", "50")))

    guide_text = (
        f"📜 <b>AFFILIATE & RESELLER SYSTEM — নিয়মাবলী ও গাইড</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>১. 👑 Monthly VIP Members (কমিশনের বদলে ফ্রি মেম্বারশিপ):</b>\n"
        f"• আপনি যদি বর্তমান মান্থলি VIP মেম্বার হন, তবে প্রতিটি সফল রেফারেলে ক্যাশের বদলে সরাসরি <b>+{extend_days} দিন VIP মেয়াদ ফ্রি বৃদ্ধি</b> পাবে!\n"
        f"• যত রেফারেল করবেন, তত মাস ফ্রিতে VIP কোর্স এক্সেস উপভোগ করবেন।\n\n"
        f"<b>২. 👤 General Promoters (লেভেল প্রোগ্রেশন):</b>\n"
        f"• <b>Level 1 (Trainee):</b> আপনি শুধু রেফারেল লিংক শেয়ার করবেন। কাস্টমার বটে এসে ফুল প্রাইসে কিনলে আপনার অ্যাকাউন্টে {comm_pct}% কমিশন জমা হবে। ব্যালেন্স উইথড্র করতে কমপক্ষে {min_unlock}টি সেল সম্পন্ন করতে হবে।\n"
        f"• <b>Level 2 (Verified Reseller):</b> {min_unlock} টি পেইড সেল সম্পন্ন হলেই আপনার উইথড্র আনলক হবে এবং ৩য় সেল থেকে <b>হোলসেল পাইকারি সুবিধা</b> আনলক হয়ে যাবে!\n\n"
        f"<b>৩. 🛒 Wholesale (পাইকারি রেটে ক্লায়েন্ট লিংক কেনা):</b>\n"
        f"• ভেরিফাইড রিসেলাররা ক্লায়েন্টের কাছ থেকে ফুল টাকা নিজের কাছে রেখে, মালিককে মাত্র {ws_discount}% পাইকারি রেট দিয়ে সাথে সাথে ১-টাইম অ্যান্টি-লিক লিংক নিয়ে কাস্টমারকে দিতে পারবেন!\n\n"
        f"<b>৪. 🔄 রিনিউয়াল রুল (সবচেয়ে গুরুত্বপূর্ণ):</b>\n"
        f"• কমিশন প্রযোজ্য হবে শুধুমাত্র কাস্টমারের <b>১ম মাসের/১ম বারের পারচেজে</b>। কাস্টমার যখন ২য় মাস থেকে রিনিউ করবে, সেই রিনিউয়াল ফি ১০০% মালিকের প্রাপ্য (০% কমিশন)।\n\n"
        f"<b>৫. 🛡️ অ্যান্টি-ফ্রড নীতি:</b>\n"
        f"• নিজের একাউন্ট নিজে রেফার করা নিষিদ্ধ। বট শুরুর লিড পার্মানেন্টলি লক থাকে। মিনিমাম উইথড্র {min_payout} BDT (bKash/Nagad/Binance)।"
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Affiliate Hub", callback_data="affiliate_menu")]])
    await query.edit_message_text(guide_text, parse_mode="HTML", reply_markup=markup)


async def render_wholesale_plans(query: Any, user_id: int):
    """Renders wholesale plans with dynamic discount for verified resellers."""
    aff = db.get_or_create_affiliate(user_id)
    min_unlock = int(db.get_setting("affiliate_min_sales_unlock", "2"))
    if aff.get("role") != "reseller" and aff.get("total_sales", 0) < min_unlock and not is_admin(user_id):
        try:
            await query.answer("🔒 এই ফিচারটি শুধুমাত্র Verified Reseller দের জন্য!", show_alert=True)
        except Exception:
            pass
        return

    ws_discount = float(db.get_setting("affiliate_wholesale_discount_pct", "50"))
    text = (
        f"🛒 <b>RESELLER WHOLESALE PORTAL — পাইকারি রেট</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"এখানে আপনি পাইকারি মূল্যে ক্লায়েন্টের জন্য ইনস্ট্যান্ট ১-টাইম VIP লিংক কিনতে পারবেন।\n"
        f"👉 ক্লায়েন্টের কাছ থেকে ফুল প্রাইস নিন, আর মালিককে শুধুমাত্র পাইকারি রেট দিন ({int(ws_discount)}% ছাড়)!\n\n"
        f"📦 <b>উপলব্ধ পাইকারি প্ল্যানসমূহ:</b>\n"
    )

    keyboard = []
    for pk, pv in PLANS.items():
        orig_bdt = pv["price_bdt"]
        ws_bdt = max(1, round(orig_bdt * ((100 - ws_discount) / 100.0)))
        ws_usdt = calc_usd(ws_bdt)
        text += f"• <b>{pv['name']}:</b> <code>{format_price(ws_bdt, ws_usdt)}</code> <i>(রেগুলার {format_price(orig_bdt)})</i>\n"
        keyboard.append([InlineKeyboardButton(f"🛒 Buy {pv['name']} ({format_btn_price(ws_bdt, ws_usdt)})", callback_data=f"ws_buy_{pk}")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Affiliate Hub", callback_data="affiliate_menu")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_buy_wholesale(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    """Starts checkout for a wholesale plan at 50% discount for client dispatch."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except Exception:
        pass

    if plan_key not in PLANS:
        await query.edit_message_text("❌ প্ল্যান পাওয়া যায়নি।")
        return

    plan = PLANS[plan_key]
    ws_discount = float(db.get_setting("affiliate_wholesale_discount_pct", "50"))
    ws_bdt = max(1, round(plan["price_bdt"] * ((100 - ws_discount) / 100.0)))
    ws_usdt = calc_usd(ws_bdt)

    bkash = PAYMENT_INFO.get("bkash", "01718539406")
    nagad = PAYMENT_INFO.get("nagad", "01718539406")
    binance = PAYMENT_INFO.get("binance_pay_id", "892612368")
    usdt_addr = PAYMENT_INFO.get("usdt_trc20", "TNPg8qkhm1xQsMNsgvH6p71tNwVXV7qaJe")

    USER_STATES[user_id] = {
        "action": "awaiting_trx",
        "plan_key": f"ws_{plan_key}",
    }

    text = (
        f"🛒 <b>WHOLESALE ORDER CHECKOUT — ক্লায়েন্ট লিংক পারচেজ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>প্ল্যান:</b> <b>{plan['name']}</b>\n"
        f"💰 <b>পাইকারি প্রদেয় মূল্য ({int(ws_discount)}% Off):</b> <b>{format_price(ws_bdt, ws_usdt)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📲 <b>পেমেন্ট অ্যাকাউন্টসমূহ:</b>\n"
        f"• 🔴 <b>bKash (Agent Cashout):</b> <code>{bkash}</code>\n"
        f"• 🟠 <b>Nagad (Agent Cashout):</b> <code>{nagad}</code>\n"
        f"• 🟡 <b>Binance Pay ID:</b> <code>{binance}</code>\n"
        f"• 🌐 <b>USDT (TRC20):</b> <code>{usdt_addr}</code>\n\n"
        f"👉 <b>টাকা পাঠানোর পর ট্রানজেকশন আইডি (TrxID) বা স্ক্রিনশট পাঠিয়ে দিন।</b>\n"
        f"<i>অ্যাডমিন অ্যাপ্রুভ করলেই ক্লায়েন্টের জন্য এক্সক্লুসিভ ১-টাইম অ্যান্টি-লিক লিংক আপনার ইনবক্সে চলে আসবে।</i>\n\n"
        f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>"
    )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back", callback_data="aff_wholesale_plans")]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_payout_request_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    aff = db.get_or_create_affiliate(user_id)
    min_unlock = int(db.get_setting("affiliate_min_sales_unlock", "2"))
    if aff.get("role") != "reseller" and aff.get("total_sales", 0) < min_unlock and not is_admin(user_id):
        rem = max(1, min_unlock - aff.get("total_sales", 0))
        try:
            await query.answer(f"🔒 উইথড্র লকড! আনলক করতে আরও {rem}টি পেইড সেল সম্পন্ন করতে হবে।", show_alert=True)
        except Exception:
            pass
        return

    min_payout = float(db.get_setting("affiliate_min_payout_bdt", "500"))
    if aff["unpaid_balance_bdt"] < min_payout:
        try:
            await query.answer(f"⚠️ অপর্যাপ্ত ব্যালেন্স! মিনিমাম পে-আউট {int(min_payout)} BDT। (বর্তমান: {aff['unpaid_balance_bdt']:.2f} BDT)", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass
    text = (
        f"💸 <b>REQUEST PAYOUT — কমিশন উইথড্রয়াল</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>আপনার মোট জমা ব্যালেন্স:</b> <b>{aff['unpaid_balance_bdt']:.2f} BDT</b>\n"
        f"💵 <b>মিনিমাম পে-আউট:</b> <b>{int(min_payout)} BDT</b>\n\n"
        f"👉 <i>যে মাধ্যমে টাকা গ্রহণ করতে চান তা সিলেক্ট করুন:</i>"
    )

    keyboard = [
        [
            InlineKeyboardButton("🔴 bKash", callback_data="aff_pay_m_bkash"),
            InlineKeyboardButton("🟠 Nagad", callback_data="aff_pay_m_nagad"),
        ],
        [
            InlineKeyboardButton("🟡 Binance Pay ID", callback_data="aff_pay_m_binance"),
        ],
        [InlineKeyboardButton("🔙 Back to Affiliate Hub", callback_data="affiliate_menu")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_payout_method_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, method: str):
    query = update.callback_query
    user_id = query.from_user.id
    try:
        await query.answer()
    except Exception:
        pass

    method_label = {
        "bkash": "bKash (Personal / Agent)",
        "nagad": "Nagad (Personal / Agent)",
        "binance": "Binance Pay ID / USDT",
    }.get(method.lower(), method.upper())

    USER_STATES[user_id] = {
        "action": "aff_awaiting_payout_info",
        "method": method_label,
    }

    aff = db.get_or_create_affiliate(user_id)
    text = (
        f"📱 <b>{method_label} — অ্যাকাউন্ট ও টাকার পরিমাণ দিন</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>বর্তমান ব্যালেন্স:</b> <b>{aff['unpaid_balance_bdt']:.2f} BDT</b>\n\n"
        f"অনুগ্রহ করে আপনার <b>অ্যাকাউন্ট নম্বর</b> এবং <b>টাকার পরিমাণ</b> এক লাইনে স্পেস দিয়ে লিখে পাঠান:\n\n"
        f"<b>উদাহরণ:</b>\n"
        f"<code>01718539406 500</code>\n\n"
        f"<i>(বাতিল করতে /cancel টাইপ করুন)</i>"
    )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="affiliate_menu")]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_reseller_apply_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompts the user with a structured questionnaire to apply for Verified Reseller status."""
    query = update.callback_query
    user_id = query.from_user.id

    aff = db.get_or_create_affiliate(user_id)
    if aff.get("role") == "reseller" or is_admin(user_id):
        try:
            await query.answer("✅ আপনি ইতিমধ্যে একজন ভেরিফাইড রিসেলার!", show_alert=True)
        except Exception:
            pass
        await render_affiliate_hub(update, context)
        return

    pending_app = db.get_pending_reseller_app(user_id)
    if pending_app:
        try:
            await query.answer("⏳ আপনার একটি আবেদন ইতিমধ্যে রিভিউতে রয়েছে! অনুগ্রহ করে অপেক্ষা করুন।", show_alert=True)
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass

    USER_STATES[user_id] = {"action": "aff_awaiting_reseller_app"}

    text = (
        f"📝 <b>VERIFIED RESELLER APPLICATION FORM</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"আমাদের এক্সক্লুসিভ <b>Verified Reseller</b> পার্টনারশিপ প্রোগ্রামে আবেদন করার জন্য নিচে উল্লেখিত তথ্যগুলো মেসেজে লিখে সেন্ড করুন:\n\n"
        f"1️⃣ <b>মার্কেটিং স্ট্র্যাটেজি:</b>\n"
        f"👉 আপনি কোর্সগুলো কীভাবে মার্কেটিং ও সেলস করবেন? (যেমন: সোশ্যাল মিডিয়া, ফ্রেন্ড সার্কেল, ফোরাম ইত্যাদি)\n\n"
        f"2️⃣ <b>ট্রাফিক সোর্স / চ্যানেল লিঙ্ক:</b>\n"
        f"👉 আপনার টেলিগ্রাম চ্যানেল, সাপোর্ট গ্রুপ বা ফেসবুক পেজের পাবলিক লিংক দিন।\n\n"
        f"3️⃣ <b>পূর্ব অভিজ্ঞতা ও টার্গেট:</b>\n"
        f"👉 আপনার কোনো পূর্ব কাজের অভিজ্ঞতা বা প্রতিদিন/প্রতি সপ্তাহে আনুমানিক কতগুলো সেল করতে পারবেন?\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✍️ <i>সব তথ্য একসাথে সাজিয়ে সরাসরি একটি মেসেজে লিখে পাঠিয়ে দিন। অ্যাডমিন রিভিউ করে আপনার আবেদনটি অনুমোদন করবেন।</i>\n\n"
        f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>"
    )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / Back", callback_data="affiliate_menu")]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def render_admin_affiliate_center(query: Any):
    """Renders the comprehensive Admin Affiliate & Reseller Management Center."""
    stats = db.get_affiliate_stats_summary()
    enabled = db.get_setting("affiliate_enabled", "1") == "1"
    status_icon = "🟢 ACTIVE / ON" if enabled else "🔴 DISABLED / OFF"
    pending_apps = db.get_all_pending_reseller_apps()

    text = (
        f"🤝 <b>AFFILIATE & RESELLER CONTROL CENTER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>সিস্টেম স্ট্যাটাস:</b> <b>{status_icon}</b>\n\n"
        f"📊 <b>সামগ্রিক পরিসংখ্যান:</b>\n"
        f"• 👥 <b>মোট নিবন্ধিত এফিলিয়েট:</b> <b>{stats['total_affiliates']}</b> জন\n"
        f"• 🎯 <b>মোট রেফারেল সেলস:</b> <b>{stats['total_sales']}</b> টি\n"
        f"• 💳 <b>মোট পরিশোধিত কমিশন:</b> <b>{stats['total_paid_bdt']:.2f} BDT</b>\n"
        f"• 📝 <b>পেন্ডিং রিসেলার আবেদন:</b> <b>{len(pending_apps)}</b> টি\n"
        f"• ⏳ <b>পেন্ডিং উইথড্র রিকোয়েস্ট:</b> <b>{stats['pending_payouts']}</b> টি\n\n"
        f"👉 <i>নিচের অপশনগুলো ব্যবহার করে পেআউট ও কনফিগারেশন নিয়ন্ত্রণ করুন:</i>"
    )

    keyboard = [
        [InlineKeyboardButton(f"📝 Reseller Applications ({len(pending_apps)})", callback_data="adm_aff_apps")],
        [InlineKeyboardButton(f"📋 Pending Payouts ({stats['pending_payouts']})", callback_data="adm_aff_payouts")],
        [InlineKeyboardButton("⚙️ Edit Affiliate Settings", callback_data="adm_aff_settings")],
        [InlineKeyboardButton("🔙 Back to Admin Dashboard", callback_data="admin_dashboard")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def render_admin_affiliate_settings(query: Any):
    """Interactive Settings Control for Affiliate Engine."""
    comm_pct = db.get_setting("affiliate_cash_comm_pct", "50")
    extend_days = db.get_setting("affiliate_monthly_vip_extend_days", "30")
    min_unlock = db.get_setting("affiliate_min_sales_unlock", "2")
    min_payout = db.get_setting("affiliate_min_payout_bdt", "500")
    ws_discount = db.get_setting("affiliate_wholesale_discount_pct", "50")
    enabled = db.get_setting("affiliate_enabled", "1") == "1"

    text = (
        f"⚙️ <b>AFFILIATE & RESELLER SETTINGS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"যে কোনো বাটনে ক্লিক করে সংশ্লিষ্ট মান সরাসরি পরিবর্তন করতে পারবেন:\n"
    )

    keyboard = [
        [
            InlineKeyboardButton(f"💰 Commission: {comm_pct}%", callback_data="adm_cfg_comm"),
            InlineKeyboardButton(f"📅 VIP Extend: {extend_days}d", callback_data="adm_cfg_extend"),
        ],
        [
            InlineKeyboardButton(f"🔒 Unlock Sales: {min_unlock}", callback_data="adm_cfg_unlock"),
            InlineKeyboardButton(f"💵 Min Payout: {min_payout} BDT", callback_data="adm_cfg_minpay"),
        ],
        [
            InlineKeyboardButton(f"🛒 Wholesale Disc: {ws_discount}%", callback_data="adm_cfg_wsdisc"),
            InlineKeyboardButton(f"🔌 System: {'ON 🟢' if enabled else 'OFF 🔴'}", callback_data="adm_cfg_toggle"),
        ],
        [InlineKeyboardButton("🔙 Back to Affiliate Center", callback_data="adm_aff_center")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def render_admin_pending_payouts(query: Any):
    """Lists pending payout requests with 1-click Approve / Reject buttons."""
    payouts = db.get_pending_payouts()
    if not payouts:
        text = (
            "📋 <b>PENDING PAYOUT REQUESTS</b>\n\n"
            "<i>বর্তমানে কোনো পেন্ডিং পেআউট রিকোয়েস্ট নেই!</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Affiliate Center", callback_data="adm_aff_center")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    text = f"📋 <b>পেন্ডিং পেআউট রিকোয়েস্টসমূহ ({len(payouts)} টি):</b>\n\n"
    keyboard = []
    for p in payouts[:8]:
        pid = p["id"]
        uid = p["user_id"]
        amt = p["amount_bdt"]
        meth = p["payment_method"]
        acc = p["account_number"]
        text += (
            f"• <b>Req #{pid}:</b> <code>{amt:.2f} BDT</code> via <b>{meth}</b>\n"
            f"  User: <code>{uid}</code> | Acc: <code>{acc}</code>\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ Approve #{pid}", callback_data=f"aff_p_app_{pid}"),
            InlineKeyboardButton(f"❌ Reject #{pid}", callback_data=f"aff_p_rej_{pid}"),
        ])

    keyboard.append([InlineKeyboardButton("🔙 Back to Affiliate Center", callback_data="adm_aff_center")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def render_admin_pending_reseller_apps(query: Any):
    """Lists pending reseller applications with 1-click Approve / Reject buttons."""
    apps = db.get_all_pending_reseller_apps()
    if not apps:
        text = (
            "📝 <b>PENDING RESELLER APPLICATIONS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>বর্তমানে কোনো পেন্ডিং রিসেলার আবেদন নেই!</i>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Affiliate Center", callback_data="adm_aff_center")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    text = f"📝 <b>পেন্ডিং ভেরিফাইড রিসেলার আবেদনসমূহ ({len(apps)} টি):</b>\n\n"
    keyboard = []
    for app in apps[:6]:
        aid = app["id"]
        uid = app["user_id"]
        safe_name = html.escape(str(app.get("full_name") or app.get("username") or uid))
        safe_plan = html.escape(str(app.get("marketing_plan") or "N/A"))
        if len(safe_plan) > 200:
            safe_plan = safe_plan[:200] + "..."
        text += (
            f"• <b>App #{aid}:</b> <b>{safe_name}</b> (ID: <code>{uid}</code>)\n"
            f"  📝 <b>প্ল্যান ও লিঙ্ক:</b> <blockquote>{safe_plan}</blockquote>\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ Approve #{aid}", callback_data=f"res_app_ok_{aid}"),
            InlineKeyboardButton(f"❌ Reject #{aid}", callback_data=f"res_app_no_{aid}"),
        ])

    keyboard.append([InlineKeyboardButton("🔙 Back to Affiliate Center", callback_data="adm_aff_center")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_setreseller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to promote a user to Verified Reseller. Usage: /setreseller <user_id>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: <code>/setreseller &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        target_id = int(args[0])
        db.set_user_role(target_id, "reseller")
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been promoted to <b>Verified Reseller</b>!", parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "🎉 <b>CONGRATULATIONS!</b>\n\n"
                    "অ্যাডমিন আপনাকে সরাসরি <b>Verified Reseller</b> হিসেবে অনুমোদন করেছেন!\n"
                    "🚀 আপনার অ্যাকাউন্টে ক্যাশ উইথড্র আনলক হয়েছে এবং আপনি এখন থেকে ৫০% পাইকারি রেটে ক্লায়েন্ট লিংক কিনতে পারবেন।\n"
                    "👉 হাব দেখতে /start দিন অথবা '🤝 Affiliate Hub' প্রেস করুন।"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ User ID অবশ্যই সংখ্যা হতে হবে।")


async def cmd_delreseller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to demote a reseller to Trainee. Usage: /delreseller <user_id>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: <code>/delreseller &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        target_id = int(args[0])
        db.set_user_role(target_id, "trainee")
        await update.message.reply_text(f"✅ User <code>{target_id}</code> role demoted to <b>Trainee</b>.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ User ID অবশ্যই সংখ্যা হতে হবে।")


async def cmd_affiliate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens the Affiliate & Reseller Hub."""
    await render_affiliate_hub(update, context)



async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aborts any pending interactive prompt."""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    await update.message.reply_text(
        "❌ <b>অপারেশনটি সফলভাবে বাতিল করা হয়েছে।</b>\n\nনিচের বাটনে ট্যাপ করে মূল মেন্যুতে ফিরে যেতে পারেন:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
    )


# =============================================================================
# APPLICATION INITIALIZER & RUNNER
# =============================================================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches and logs unhandled exceptions or network timeouts gracefully."""
    logger.error("Exception occurred while handling an update: %s", context.error, exc_info=context.error)


async def post_init(application: Application):
    """Starts the 24/7 background scheduler and registers Telegram Menu commands."""
    scheduler = VaultScheduler(bot=application.bot, config=CONFIG)
    asyncio.create_task(scheduler.start())
    logger.info("Background Auto-Kick Scheduler task created successfully.")

    # Register official bot commands for the bottom-left Menu button
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "🚀 Main Menu & Plans"),
            BotCommand("myplan", "👤 My Subscription Status"),
            BotCommand("links", "🔗 Access VIP Channels & Groups"),
            BotCommand("cancel", "❌ Cancel Current Action"),
            BotCommand("admin", "⚙️ Admin Control Panel"),
        ])
        logger.info("Bot commands registered successfully.")
    except Exception as e:
        logger.warning(f"Failed to register bot commands: {e}")

    # Dispatch startup/deployment safety database snapshot to Admins
    try:
        backup_file = db.backup_database()
        dhaka_now = datetime.datetime.now().strftime("%d %b %Y, %I:%M:%S %p")
        stats = db.get_stats()
        sub_count = stats.get("total_active", 0)
        total_users = stats.get("total_all", 0)
        for aid in ADMIN_IDS:
            try:
                with open(backup_file, "rb") as bf:
                    await application.bot.send_document(
                        chat_id=aid,
                        document=bf,
                        filename=os.path.basename(backup_file),
                        caption=(
                            "🛡️ <b>[BOT UPDATE / STARTUP SAFETY BACKUP]</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            f"📅 <b>Time:</b> <code>{dhaka_now}</code>\n"
                            f"👥 <b>Total Users:</b> <code>{total_users}</code> | <b>Active VIPs:</b> <code>{sub_count}</code>\n"
                            "✅ <b>Status:</b> Bot is online & running live!\n"
                            "💾 <i>Automatic snapshot dispatched for 100% data security.</i>"
                        ),
                        parse_mode="HTML",
                    )
                logger.info(f"Startup safety backup dispatched to Admin ID {aid}")
            except Exception as se:
                logger.warning(f"Could not dispatch startup backup to admin {aid}: {se}")
    except Exception as e:
        logger.error(f"Startup safety backup failed: {e}")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n" + "=" * 60)
        print("❌ CRITICAL: Please edit 'config.json' and set your real BOT_TOKEN!")
        print("=" * 60 + "\n")
        return

    print("\n" + "=" * 60)
    print(f"🚀 Starting {PRODUCT_NAME} VIP Bot...")
    print(f"👑 Admins: {ADMIN_IDS}")
    print(f"📚 Connected Channels: {len(CHANNELS)}")
    print("=" * 60 + "\n")

    # Universally activate Telegram Premium Custom Emoji Hook across all Bot messages
    patch_bot_for_premium_emojis()

    # High-durability HTTPX request client with resilient timeouts
    request_client = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request_client)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(global_error_handler)

    # User Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myplan", cmd_myplan))
    app.add_handler(CommandHandler("affiliate", cmd_affiliate))
    app.add_handler(CommandHandler("ref", cmd_affiliate))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("plans", render_plans_menu))
    app.add_handler(CommandHandler("payment", send_payment_info))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Admin Commands
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("setreseller", cmd_setreseller))
    app.add_handler(CommandHandler("delreseller", cmd_delreseller))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("renew", cmd_renew))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("addgroup", cmd_addgroup))
    app.add_handler(CommandHandler("delchannel", cmd_delchannel))
    app.add_handler(CommandHandler("delgroup", cmd_delchannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("setpayment", cmd_setpayment))
    app.add_handler(CommandHandler("setprice", cmd_setprice))
    app.add_handler(CommandHandler("setdollar", cmd_setdollar))
    app.add_handler(CommandHandler("setrate", cmd_setdollar))
    app.add_handler(CommandHandler("setreminders", cmd_setreminders))
    app.add_handler(CommandHandler("setforcechannel", cmd_setforcechannel))
    app.add_handler(CommandHandler("setpreview", cmd_setpreview))
    app.add_handler(CommandHandler("setrules", cmd_setrules))

    # Channel & Group Automatic Addition Hook
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_chat_member_join, ChatMemberHandler.CHAT_MEMBER))

    # Callbacks and Text/Photo Handlers
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, handle_user_text_message))

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
