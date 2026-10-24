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
VIP_PREVIEW_NAME: str = db.get_setting("vip_preview_name") or os.getenv("VIP_PREVIEW_NAME") or str(CONFIG.get("vip_preview_name") or "VIP Preview & Proof Channel")
CONFIG["vip_preview_link"] = VIP_PREVIEW_LINK
CONFIG["vip_preview_name"] = VIP_PREVIEW_NAME

# Force Channel Join Configuration (Loaded from SQLite first)
FORCE_CHANNEL_ID: str = db.get_setting("force_channel_id") or os.getenv("FORCE_CHANNEL_ID") or str(CONFIG.get("force_channel_id") or "")
FORCE_CHANNEL_LINK: str = db.get_setting("force_channel_link") or os.getenv("FORCE_CHANNEL_LINK") or str(CONFIG.get("force_channel_link") or "")
FORCE_CHANNEL_NAME: str = db.get_setting("force_channel_name") or os.getenv("FORCE_CHANNEL_NAME") or str(CONFIG.get("force_channel_name") or "Official Channel")
CONFIG["force_channel_id"] = FORCE_CHANNEL_ID
CONFIG["force_channel_link"] = FORCE_CHANNEL_LINK
CONFIG["force_channel_name"] = FORCE_CHANNEL_NAME

# Pending user states in memory (for conversation steps)
USER_STATES: Dict[int, Dict[str, Any]] = {}


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
    return user_id in ADMIN_IDS


def get_persistent_menu(is_admin_user: bool = False) -> ReplyKeyboardMarkup:
    """Constructs the high-aesthetic persistent bottom reply keyboard."""
    keyboard = [
        [KeyboardButton("👑 VIP Plans & Pricing"), KeyboardButton("💳 Payment Methods")],
        [KeyboardButton("👤 My Subscription"), KeyboardButton("📢 Official Channel")],
    ]
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
async def generate_single_use_links(bot, user_id: int) -> List[Dict[str, Any]]:
    """
    Creates individual single-use invite links (member_limit=1) for each channel and group.
    Links expire in 24 hours if not clicked.
    Returns: List of dicts with name, url, type, id, error
    """
    links = []
    expire_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

    for ch in CHANNELS:
        ch_id = ch["id"]
        ch_name = ch.get("name", str(ch_id))
        ch_type = ch.get("type", "channel")
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
    links = await generate_single_use_links(bot, user_id)

    if is_lifetime:
        duration_text = "👑 <b>Lifetime Unlimited Access</b> (চিরস্থায়ী)"
    else:
        dt = datetime.datetime.fromisoformat(expires_at_iso)
        duration_text = f"⏳ <b>মেয়াদ:</b> <code>{dt.strftime('%d %b %Y, %I:%M %p (UTC)')}</code>"

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
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome dashboard with plan details, self-service checks, and admin link."""
    user = update.effective_user
    user_id = user.id

    # Enforce mandatory channel join
    is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
    if not is_subbed:
        await send_force_sub_message(update, context, unjoined_link, unjoined_name)
        return

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

    welcome_text = (
        f"{em_crw} <b>{PRODUCT_NAME} — VIP HUB</b> {em_dia}\n"
        f"<i>স্বাগতম, {html.escape(user.first_name or 'VIP Member')}! এক্সক্লুসিভ কোর্স ভল্ট ও প্রিমিয়াম হাব।</i>\n\n"
        f"{box_border}\n"
        f"{em_usr} <b>Customer ID:</b> <code>#{user_id}</code>\n"
        f"{em_shd} <b>Account Tier:</b> <code>{tier_label}</code>\n"
        f"{em_chk} <b>Service Status:</b> <code>{status_text}</code>\n"
        f"{em_zap} <b>Delivery:</b> <code>Anti-Leak 1-Time Invite Links</code>\n"
        f"{box_border}\n\n"
        f"🏛️ <b>VIP Vault Services:</b>\n"
        f"• {em_crs} <b>Courses:</b> লাইফটাইম ক্লাউড ড্রাইভ ও সিক্রেট মেথডস\n"
        f"• {em_chn} <b>VIP Channels:</b> প্রতিদিন নতুন কোর্স রিলিজ ও রিসোর্স\n"
        f"• {em_grp} <b>VIP Groups:</b> সিক্রেট ডিসকাশন ও স্টুডেন্ট নেটওয়ার্ক\n"
        f"• {em_sup} <b>Live Support:</b> ২৪/৭ ডিরেক্ট অ্যাডমিন অ্যাসিস্ট্যান্স\n\n"
        f"👉 <i>নিচের বাটনগুলো ব্যবহার করে প্ল্যান ব্রাউজ বা অ্যাকাউন্ট পরিচালনা করুন:</i>"
    )

    keyboard = [
        [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
        [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
        [InlineKeyboardButton("💳 Payment Methods & Info", callback_data="payment_info")],
    ]

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

    # Pin persistent bottom navigation menu bar
    try:
        await update.message.reply_text(
            f"🏛️ <b>{PRODUCT_NAME}</b>\n<i>Navigation menu loaded below.</i>",
            parse_mode="HTML",
            reply_markup=get_persistent_menu(is_admin(user_id)),
        )
    except Exception:
        pass

    await update.message.reply_text(
        text=welcome_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


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

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Access VIP Channels & Groups", callback_data="get_my_links")],
        [InlineKeyboardButton("🔄 Renew Subscription", callback_data="user_renew")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows active VIP members to retrieve or refresh their 1-time channel invite links anytime."""
    user_id = update.effective_user.id
    sub = db.get_subscriber(user_id)
    if sub and sub.get("status") == "active":
        await deliver_welcome_access(
            context.bot,
            user_id,
            sub["plan_name"],
            sub["expires_at"],
            sub["is_lifetime"]
        )
    else:
        await update.message.reply_text(
            "❌ <b>আপনার কোনো সক্রিয় VIP মেম্বারশিপ নেই!</b>\n\nপ্ল্যান দেখতে /plans লিখুন।",
            parse_mode="HTML"
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

    p_mon = PLANS.get("monthly", {"price_bdt": 1000, "price_usdt": 10})
    p_yr = PLANS.get("yearly", {"price_bdt": 5000, "price_usdt": 50})
    p_life = PLANS.get("lifetime", {"price_bdt": 8000, "price_usdt": 80})

    preview_text = ""
    if VIP_PREVIEW_LINK:
        preview_text = (
            f"\n\n👁️ <b>VIP Preview / কোর্স প্রুফ:</b>\n"
            f"👉 জয়েন করার আগে কোর্স ভল্ট ও প্রুফ দেখতে <a href='{VIP_PREVIEW_LINK}'>এখানে ক্লিক করুন</a> 🎬"
        )

    text = (
        f"{em_crw} <b>{PRODUCT_NAME} — VIP PLANS & PRICING</b> {em_dia}\n"
        f"{box_border}\n"
        f"আপনার পছন্দের প্রিমিয়াম প্ল্যানটি বেছে নিন:\n\n"
        f"<b>১. 📅 Monthly VIP (30 Days)</b>\n"
        f"• ফুল কোর্স ভল্ট ও ডিসকাশন গ্রুপ এক্সেস\n"
        f"• ইনভেস্টমেন্ট: <b>{p_mon.get('price_bdt', 1000)} BDT / ${p_mon.get('price_usdt', 10)} USDT</b>\n\n"
        f"<b>২. 📆 Yearly VIP (365 Days)</b>\n"
        f"• ৩৬৫ দিন নিরবচ্ছিন্ন আপডেট ও প্রায় ৬০% সেভিংস {em_fire}\n"
        f"• ইনভেস্টমেন্ট: <b>{p_yr.get('price_bdt', 5000)} BDT / ${p_yr.get('price_usdt', 50)} USDT</b>\n\n"
        f"<b>৩. 👑 Lifetime Access (Forever VIP)</b>\n"
        f"• একবার পেমেন্টে আজীবনের জন্য ফুল ফ্রি এক্সেস {em_zap}\n"
        f"• ইনভেস্টমেন্ট: <b>{p_life.get('price_bdt', 8000)} BDT / ${p_life.get('price_usdt', 80)} USDT</b>\n"
        f"{box_border}"
        f"{preview_text}\n\n"
        f"👉 <i>যে প্ল্যানটি নিতে চান নিচের বাটনে ট্যাপ করুন:</i>"
    )

    keyboard = []
    if VIP_PREVIEW_LINK:
        keyboard.append([InlineKeyboardButton("👁️ View VIP Preview Channel 🎬", url=VIP_PREVIEW_LINK)])

    keyboard.extend([
        [InlineKeyboardButton("📅 Buy Monthly (30 Days)", callback_data="buy_monthly")],
        [InlineKeyboardButton("📆 Buy Yearly (365 Days)", callback_data="buy_yearly")],
        [InlineKeyboardButton("👑 Buy Lifetime (Forever)", callback_data="buy_lifetime")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)


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
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Select Plan & Pay", callback_data="plans_menu")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


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


async def handle_buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    query = update.callback_query
    await query.answer()

    plan = PLANS.get(plan_key)
    if not plan:
        return

    bkash_num = PAYMENT_INFO.get("bkash", "01718539406")
    nagad_num = PAYMENT_INFO.get("nagad", "01718539406")
    binance_id = PAYMENT_INFO.get("binance_pay_id", "892612368")
    usdt_addr = PAYMENT_INFO.get("usdt_trc20", "TNPg8qkhm1xQsMNsgvH6p71tNwVXV7qaJe")

    text = (
        f"{em_tag('wallet', '💳')} <b>পেমেন্ট ইনস্ট্রাকশন: {plan['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{em_tag('cash', '💰')} <b>প্রদেয় মূল্য:</b> <b>{plan['price_bdt']} BDT</b> অথবা <b>${plan['price_usdt']} USDT</b>\n\n"
        f"{em_tag('bkash', '📱')} <b>bKash (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{bkash_num}</code>\n\n"
        f"{em_tag('nagad', '📱')} <b>Nagad (Agent - Cash Out Only):</b>\n"
        f"👉 <code>{nagad_num}</code>\n\n"
        f"⚠️ <b>জরুরি সতর্কতা:</b>\n"
        f"✔️ <b>bkash, Nagad Cashout Only (Agent)</b>\n"
        f"👆 শুধুমাত্র ক্যাশআউট করবেন (Send Money গ্রহণযোগ্য নয়)।\n\n"
        f"{em_tag('binance', '🌐')} <b>Binance Pay ID:</b>\n"
        f"👉 <code>{binance_id}</code>\n\n"
        f"{em_tag('usdt', '💲')} <b>Usdt Trx (Trc20):</b>\n"
        f"👉 <code>{usdt_addr}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"➡️ <b>Send ScreenShot Must</b>\n"
        f"ক্যাশআউট বা ট্রান্সফার করার পর স্ক্রিনশট বা Trx ID পাঠানো বাধ্যতামূলক।\n\n"
        f"👇 নিচের <b>[ ✍️ Submit Trx ID / Proof ]</b> বাটনে ক্লিক করে প্রুফ পাঠান:"
    )

    keyboard = [
        [InlineKeyboardButton("✍️ Submit Trx ID / Proof", callback_data=f"submit_trx_{plan_key}")],
        [InlineKeyboardButton("🔙 Back to Plans", callback_data="plans_menu")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def start_trx_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    USER_STATES[user_id] = {
        "action": "awaiting_trx",
        "plan_key": plan_key,
    }

    text = (
        f"✍️ <b>পেমেন্ট প্রুফ ও Trx ID সাবমিট করুন:</b>\n\n"
        f"📸 <b>পেমেন্টের স্ক্রিনশট (Screenshot)</b> অথবা <b>Trx ID / যে নাম্বার থেকে ক্যাশআউট করেছেন</b> তা এখানে সরাসরি লিখে বা ফটো আকারে পাঠান।\n\n"
        f"➡️ <b>Send ScreenShot Must</b>\n\n"
        f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML")


async def handle_user_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text submissions and payment screenshots (like TrxID or Admin inputs)."""
    user = update.effective_user
    user_id = user.id

    if not is_admin(user_id):
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if not is_subbed:
            await send_force_sub_message(update, context, unjoined_link, unjoined_name)
            return

    msg_text = update.message.text.strip() if update.message.text else ""

    # Persistent Bottom Menu Bar Intercepts (Always Active)
    if msg_text in ("👑 VIP Plans & Pricing", "👑 VIP Plans", "VIP Plans & Pricing", "/plans"):
        await render_plans_menu(update, context)
        return
    if msg_text in ("💳 Payment Methods", "💳 Payment Info", "Payment Methods & Info", "/payment"):
        await send_payment_info(update, context)
        return
    if msg_text in ("👤 My Subscription", "👤 My Plan", "My Subscription Status", "/myplan"):
        await cmd_myplan(update, context)
        return
    if msg_text in ("📢 Official Channel", "📢 Channel", "Official Channel"):
        await send_official_channel_info(update, context)
        return
    if msg_text in ("⚙️ Admin Control Panel", "⚙️ Admin Panel", "/admin"):
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
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            trx_id = update.message.caption.strip() if update.message.caption else "📸 Screenshot Attached"
        else:
            trx_id = update.message.text.strip() if update.message.text else "N/A"

        plan_key = state.get("plan_key")
        plan = PLANS.get(plan_key, {"name": plan_key, "price_bdt": "N/A"})

        USER_STATES.pop(user_id, None)

        # Create record in DB
        req_id = db.create_payment_request(
            user_id=user_id,
            username=user.username,
            full_name=user.full_name,
            plan_key=plan_key,
            amount=f"{plan.get('price_bdt')} BDT",
            payment_method="Agent Cashout / Crypto",
            trx_id=trx_id,
        )

        safe_trx = html.escape(trx_id)
        safe_full_name = html.escape(user.full_name or "VIP Member")
        safe_username = html.escape(user.username or "none")

        await update.message.reply_text(
            f"✅ <b>পেমেন্ট রিকোয়েস্ট সাবমিট হয়েছে! (ID: #{req_id})</b>\n\n"
            f"📦 <b>প্ল্যান:</b> {plan.get('name')}\n"
            f"🔢 <b>প্রুফ / Trx ID:</b> <code>{safe_trx}</code>\n\n"
            f"অ্যাডমিন ভেরিফাই করা মাত্রই আপনি এই বটের ইনবক্সে সমস্ত VIP চ্যানেলের ওয়ান-টাইম জয়েন লিংক পেয়ে যাবেন।",
            parse_mode="HTML",
        )

        # Alert Admins with Interactive Approval Card
        admin_card = (
            f"💳 <b>[NEW PAYMENT SUBMISSION #{req_id}]</b>\n\n"
            f"👤 <b>User:</b> <a href='tg://user?id={user_id}'>{safe_full_name}</a> (@{safe_username})\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📦 <b>Plan:</b> {plan.get('name')}\n"
            f"💰 <b>Amount:</b> {plan.get('price_bdt')} BDT / ${plan.get('price_usdt', 'N/A')} USDT\n"
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
            usdt = int(nums[1]) if len(nums) > 1 else max(1, round(bdt / 100))

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
                f"💰 <b>নতুন প্রদেয় মূল্য:</b> <b>{bdt} BDT</b> / <b>${usdt} USDT</b>\n"
                f"💾 <i>ডাটাবেজ ও বটে পার্মানেন্টলি সেভ হয়ে গেছে!</i>",
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ এরর: {e}")
        return

    # 11. Admin Awaiting Preview Link Input
    if action == "admin_awaiting_preview_link":
        text_in = update.message.text.strip()
        USER_STATES.pop(user_id, None)

        if text_in.lower() in ("off", "disable", "none", "remove", "clear"):
            VIP_PREVIEW_LINK = ""
            VIP_PREVIEW_NAME = ""
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
            f"💾 <i>ডাটাবেজে স্থায়ীভাবে সেভ করা হয়েছে। এখন ইউজাররা /plans মেনুতে প্রিভিউ বাটন দেখতে পাবে।</i>",
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
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


# =============================================================================
# ADMIN DASHBOARD & CONTROLS
# =============================================================================
async def render_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    stats = db.get_stats()

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
            InlineKeyboardButton("💳 Edit Payments", callback_data="adm_action_edit_payments"),
        ],
        [
            InlineKeyboardButton("💾 Backup DB Now", callback_data="adm_action_backup"),
            InlineKeyboardButton("📢 Broadcast DM", callback_data="adm_action_broadcast"),
        ],
        [
            InlineKeyboardButton(f"👁️ VIP Preview Link ({'Active' if VIP_PREVIEW_LINK else 'None'})", callback_data="adm_action_preview"),
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
            "• <code>/setprice monthly 500 5</code>",
            "• <code>/setprice yearly 3000 30</code>",
            "• <code>/setprice lifetime 5000 50</code>\n",
            "<b>Current Active Pricing:</b>",
        ]
        for pk, pv in PLANS.items():
            lines.append(f"• <b>{pv['name']}:</b> <code>{pv['price_bdt']} BDT / ${pv['price_usdt']} USDT</code>")
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
        usdt = int(nums[1]) if len(nums) > 1 else max(1, round(bdt / 100))

        PLANS[plan_key]["price_bdt"] = bdt
        PLANS[plan_key]["price_usdt"] = usdt
        db.update_plan_price(plan_key, bdt, usdt)
        save_config()
        db.log_activity(update.effective_user.id, "PRICE_UPDATED", f"Plan {plan_key}: {bdt} BDT / ${usdt} USDT")

        await update.message.reply_text(
            f"✅ <b>Plan Pricing Updated!</b>\n\n"
            f"📦 <b>Plan:</b> {PLANS[plan_key]['name']}\n"
            f"💰 <b>New Price:</b> <b>{bdt} BDT</b> / <b>${usdt} USDT</b>\n"
            f"💾 <i>Saved permanently in SQLite DB & Config.</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error updating price: {e}")


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
        VIP_PREVIEW_LINK = ""
        VIP_PREVIEW_NAME = ""
        save_config()
        await update.message.reply_text("✅ <b>VIP Preview Channel disabled!</b>", parse_mode="HTML")
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
    save_config()

    await update.message.reply_text(
        f"✅ <b>VIP Preview Link Configured!</b>\n\n"
        f"👁️ <b>Name:</b> {name}\n"
        f"🔗 <b>Link:</b> {link}\n\n"
        f"<i>ইউজাররা /plans মেনুতে এখন এই প্রিভিউ চ্যানেলের ডিরেক্ট বাটন ও লিংক দেখতে পাবেন!</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
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


# =============================================================================
# CALLBACK QUERY ROUTER (Inline Button Actions)
# =============================================================================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # Fast acknowledge callback to instantly clear loading spinner in Telegram client
    # (Skip for callbacks that display a modal alert or interactive toast)
    if not (data == "check_force_sub" or data.startswith(("adm_app_", "adm_rej_", "adm_action_backup", "adm_quick_dur_"))):
        try:
            await query.answer()
        except Exception:
            pass

    # 0. Force Channel Join Check Action
    if data == "check_force_sub":
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if is_subbed:
            await query.answer("✅ ধন্যবাদ! ভেরিফিকেশন সফল হয়েছে।", show_alert=True)
            data = "main_menu"
        else:
            await query.answer("❌ আপনি এখনো চ্যানেলে জয়েন করেননি! দয়া করে চ্যানেলে জয়েন করে আবার ভেরিফাই করুন।", show_alert=True)
            return

    # Verify force sub for any other callbacks from non-admin users
    if not is_admin(user_id):
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if not is_subbed:
            await query.answer("⚠️ অনুগ্রহ করে আগে আমাদের চ্যানেলে জয়েন করুন!", show_alert=True)
            await send_force_sub_message(update, context, unjoined_link, unjoined_name)
            return

    # 1. Main Navigation
    if data == "main_menu":
        await query.answer()
        user = query.from_user
        uid = user.id

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

        sub = db.get_subscriber(uid)
        is_vip = sub and sub.get("status") == "active"
        tier_label = f"👑 VIP ({sub['plan_name']})" if is_vip else "⭐ Free Guest"
        status_text = "Active & Verified" if is_vip else "24/7 Instant Auto-Delivery"
        box_border = "━━━━━━━━━━━━━━━━━━━━"

        welcome_text = (
            f"{em_crw} <b>{PRODUCT_NAME} — VIP HUB</b> {em_dia}\n"
            f"<i>স্বাগতম, {html.escape(user.first_name or 'VIP Member')}! এক্সক্লুসিভ কোর্স ভল্ট ও প্রিমিয়াম হাব।</i>\n\n"
            f"{box_border}\n"
            f"{em_usr} <b>Customer ID:</b> <code>#{uid}</code>\n"
            f"{em_shd} <b>Account Tier:</b> <code>{tier_label}</code>\n"
            f"{em_chk} <b>Service Status:</b> <code>{status_text}</code>\n"
            f"{em_zap} <b>Delivery:</b> <code>Anti-Leak 1-Time Invite Links</code>\n"
            f"{box_border}\n\n"
            f"🏛️ <b>VIP Vault Services:</b>\n"
            f"• {em_crs} <b>Courses:</b> লাইফটাইম ক্লাউড ড্রাইভ ও সিক্রেট মেথডস\n"
            f"• {em_chn} <b>VIP Channels:</b> প্রতিদিন নতুন কোর্স রিলিজ ও রিসোর্স\n"
            f"• {em_grp} <b>VIP Groups:</b> সিক্রেট ডিসকাশন ও স্টুডেন্ট নেটওয়ার্ক\n"
            f"• {em_sup} <b>Live Support:</b> ২৪/৭ ডিরেক্ট অ্যাডমিন অ্যাসিস্ট্যান্স\n\n"
            f"👉 <i>নিচের বাটনগুলো ব্যবহার করে প্ল্যান ব্রাউজ বা অ্যাকাউন্ট পরিচালনা করুন:</i>"
        )

        keyboard = [
            [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
            [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
            [InlineKeyboardButton("💳 Payment Methods & Info", callback_data="payment_info")],
        ]
        if is_admin(uid):
            keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

        await query.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "plans_menu":
        await render_plans_menu(update, context)
        return

    if data == "user_status":
        await query.answer()
        await cmd_myplan(update, context)
        return

    if data == "user_renew":
        await render_plans_menu(update, context)
        return

    if data == "payment_info":
        await query.answer()
        await send_payment_info(update, context)
        return

    if data == "get_my_links":
        sub = db.get_subscriber(user_id)
        if sub and sub.get("status") == "active":
            await query.answer("Fetching your secure 1-time invite links...")
            await deliver_welcome_access(
                context.bot,
                user_id,
                sub["plan_name"],
                sub["expires_at"],
                sub["is_lifetime"]
            )
        else:
            await query.answer("❌ আপনার কোনো সক্রিয় VIP মেম্বারশিপ নেই!", show_alert=True)
        return

    # Plan Selection Callbacks
    if data.startswith("buy_"):
        plan_key = data.replace("buy_", "")
        await handle_buy_plan(update, context, plan_key)
        return

    if data.startswith("submit_trx_"):
        plan_key = data.replace("submit_trx_", "")
        await start_trx_submission(update, context, plan_key)
        return

    # 2. Admin Callbacks
    if not is_admin(user_id):
        return

    if data == "admin_dashboard":
        await render_admin_dashboard(update, context)
        return

    # Admin Payment Approval / Rejection
    if data.startswith("adm_app_"):
        await query.answer("Processing approval...")
        req_id = int(data.replace("adm_app_", ""))
        req = db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await query.answer("This request has already been processed!", show_alert=True)
            return

        plan_key = req["plan_key"]
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
        except Exception as e:
            logger.error(f"Failed to deliver links to user {req['user_id']}: {e}")

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
            if query.message.photo:
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
        await query.answer("Processing rejection...")
        req_id = int(data.replace("adm_rej_", ""))
        req = db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await query.answer("This request has already been processed!", show_alert=True)
            return

        db.update_payment_status(req_id, "rejected")
        safe_rej_trx = html.escape(str(req["trx_id"] or "N/A"))
        try:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=f"❌ Your payment submission (Trx: <code>{safe_rej_trx}</code>) could not be verified by administration. Please contact support.",
                parse_mode="HTML",
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
            if query.message.photo:
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
            await query.answer("Session expired. Please start again.", show_alert=True)
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
        await query.answer("Creating backup...")
        backup_file = db.backup_database()
        with open(backup_file, "rb") as f:
            await context.bot.send_document(
                chat_id=user_id,
                document=f,
                filename=os.path.basename(backup_file),
                caption=f"🛡️ <b>Manual Database Backup</b>\n📅 <code>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>",
                parse_mode="HTML",
            )
        await query.answer("Backup sent to your Telegram!", show_alert=True)
        return

    if data == "adm_action_list_active":
        await query.answer()
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
        await query.answer()
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
            await query.answer("Session expired. Please start again.", show_alert=True)
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
        await query.answer()
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
        text = (
            "👁️ <b>VIP PREVIEW CHANNEL LINK</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"বর্তমান লিংক: {curr}\n\n"
            "এই লিংকটি ইউজারদের /plans মেনুতে 'View VIP Preview Channel' বাটন আকারে প্রদর্শিত হবে যাতে ইউজাররা কেনার আগে কোর্স ভল্ট ও প্রুফ দেখতে পারে।\n\n"
            "নতুন প্রিভিউ লিংক সেট করতে নিচের বাটনে চাপ দিন অথবা কমান্ড পাঠান:\n"
            "<code>/setpreview &lt;link&gt;</code>"
        )
        buttons = [
            [InlineKeyboardButton("✏️ Set / Change Preview Link", callback_data="adm_set_preview_prompt")],
        ]
        if VIP_PREVIEW_LINK:
            buttons.append([InlineKeyboardButton("❌ Remove Preview Link", callback_data="adm_remove_preview_link")])
        buttons.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")])
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
        VIP_PREVIEW_LINK = ""
        VIP_PREVIEW_NAME = ""
        save_config()
        await query.answer("Preview link removed!", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_action_del_channel":
        if not CHANNELS:
            await query.answer("No channels or groups configured to delete!", show_alert=True)
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
        await query.answer("Destination removed!", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_action_clear_logs":
        live_log_buffer.clear()
        await query.answer("🧹 Live logs & error buffer cleared!", show_alert=True)
        data = "adm_action_logs"

    if data == "adm_action_logs":
        await query.answer()

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
        em_crw = em_tag("crown", "👑")
        em_dia = em_tag("diamond", "💎")
        box_border = "━━━━━━━━━━━━━━━━━━━━"

        lines = [
            f"{em_dia} <b>MANAGE VIP PLAN PRICING</b> {em_crw}\n",
            f"{box_border}",
            "যেকোনো প্ল্যানের মূল্য (BDT ও USDT) পরিবর্তন করতে নিচের বাটনে ট্যাপ করুন:\n"
        ]
        for pk in ("monthly", "yearly", "lifetime"):
            pv = PLANS.get(pk)
            if pv:
                lines.append(f"• <b>{pv['name']}:</b>\n  মূল্য: <b>{pv['price_bdt']} BDT</b> / <b>${pv['price_usdt']} USDT</b>\n")
        lines.append(f"{box_border}")
        lines.append("<i>বাটনে ক্লিক করার পর নতুন মূল্য লিখুন অথবা কমান্ড ব্যবহার করুন: <code>/setprice &lt;plan&gt; &lt;bdt&gt; [usdt]</code></i>")

        buttons = [
            [InlineKeyboardButton(f"📅 Change Monthly ({PLANS.get('monthly', {}).get('price_bdt', 0)} BDT)", callback_data="adm_change_price_monthly")],
            [InlineKeyboardButton(f"📆 Change Yearly ({PLANS.get('yearly', {}).get('price_bdt', 0)} BDT)", callback_data="adm_change_price_yearly")],
            [InlineKeyboardButton(f"👑 Change Lifetime ({PLANS.get('lifetime', {}).get('price_bdt', 0)} BDT)", callback_data="adm_change_price_lifetime")],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")],
        ]
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("adm_change_price_"):
        plan_key = data.replace("adm_change_price_", "")
        if plan_key in PLANS:
            USER_STATES[user_id] = {"action": "admin_awaiting_plan_price", "plan_key": plan_key}
            await query.edit_message_text(
                f"💰 <b>Set Price for {PLANS[plan_key]['name']}</b>\n\n"
                f"বর্তমান মূল্য: <b>{PLANS[plan_key]['price_bdt']} BDT / ${PLANS[plan_key]['price_usdt']} USDT</b>\n\n"
                f"নতুন <b>BDT প্রাইস</b> এবং স্পেস দিয়ে <b>USDT প্রাইস</b> লিখে পাঠান।\n"
                f"উদাহরণ: <code>500 5</code> (অথবা শুধু <code>500</code> লিখলে USDT অটো ক্যালকুলেট হবে)\n\n"
                f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>",
                parse_mode="HTML",
            )
        return



async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aborts any pending interactive prompt."""
    user_id = update.effective_user.id
    USER_STATES.pop(user_id, None)
    await update.message.reply_text("Action cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))


# =============================================================================
# APPLICATION INITIALIZER & RUNNER
# =============================================================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches and logs unhandled exceptions or network timeouts gracefully."""
    logger.error("Exception occurred while handling an update: %s", context.error, exc_info=context.error)


async def post_init(application: Application):
    """Starts the 24/7 background scheduler and registers Telegram Menu commands."""
    scheduler = VaultScheduler(bot=application.bot, config=CONFIG)
    application.create_task(scheduler.start())
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
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("plans", render_plans_menu))
    app.add_handler(CommandHandler("payment", send_payment_info))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Admin Commands
    app.add_handler(CommandHandler("admin", cmd_admin))
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
    app.add_handler(CommandHandler("setforcechannel", cmd_setforcechannel))
    app.add_handler(CommandHandler("setpreview", cmd_setpreview))

    # Channel & Group Automatic Addition Hook
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Callbacks and Text/Photo Handlers
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_user_text_message))

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
