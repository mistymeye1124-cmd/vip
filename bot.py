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
import json
import logging
import datetime
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

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
    ChatMember,
    InputFile,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.request import HTTPXRequest

from database import db
from scheduler import VaultScheduler

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vault_bot")

# Load Environment Variables (.env) with bulletproof fallback
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_file = Path(".env")
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
CONFIG_PATH = Path("config.json")
if not CONFIG_PATH.exists():
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

CHANNELS: List[Dict[str, Any]] = CONFIG.get("channels", [])
PLANS: Dict[str, Any] = CONFIG.get("plans", {})
PAYMENT_INFO: Dict[str, str] = CONFIG.get("payment_info", {})

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

# Force Channel Join Configuration
FORCE_CHANNEL_ID: str = os.getenv("FORCE_CHANNEL_ID") or str(CONFIG.get("force_channel_id") or "")
FORCE_CHANNEL_LINK: str = os.getenv("FORCE_CHANNEL_LINK") or str(CONFIG.get("force_channel_link") or "")
FORCE_CHANNEL_NAME: str = os.getenv("FORCE_CHANNEL_NAME") or str(CONFIG.get("force_channel_name") or "Official Channel")

CONFIG["force_channel_id"] = FORCE_CHANNEL_ID
CONFIG["force_channel_link"] = FORCE_CHANNEL_LINK
CONFIG["force_channel_name"] = FORCE_CHANNEL_NAME

# Pending user states in memory (for conversation steps)
USER_STATES: Dict[int, Dict[str, Any]] = {}


def save_config():
    """Persists in-memory channels, payment settings, and force channel to config.json."""
    CONFIG["channels"] = CHANNELS
    CONFIG["payment_info"] = PAYMENT_INFO
    CONFIG["force_channel_id"] = FORCE_CHANNEL_ID
    CONFIG["force_channel_link"] = FORCE_CHANNEL_LINK
    CONFIG["force_channel_name"] = FORCE_CHANNEL_NAME
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2)
    logger.info("Configuration saved successfully to config.json")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
async def generate_single_use_links(bot, user_id: int) -> List[Tuple[str, str]]:
    """
    Creates individual single-use invite links (member_limit=1) for each channel.
    Links expire in 24 hours if not clicked.
    Returns: List of (channel_name, invite_url)
    """
    links = []
    expire_date = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)

    for ch in CHANNELS:
        ch_id = ch["id"]
        ch_name = ch.get("name", str(ch_id))
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=ch_id,
                name=f"User_{user_id}",
                member_limit=1,
                expire_date=expire_date,
            )
            links.append((ch_name, invite.invite_link))
        except BadRequest as e:
            logger.error(f"Failed to create invite for {ch_name} ({ch_id}): {e.message}")
            links.append((ch_name, f"Error: Bot lacks 'Invite Users' admin rights in {ch_name}"))
        except Exception as e:
            logger.error(f"Invite creation error for {ch_name}: {e}")
            links.append((ch_name, "Error generating link"))

    return links


async def deliver_welcome_access(bot, user_id: int, plan_name: str, expires_at_iso: str, is_lifetime: bool):
    """Sends the rich welcome card with 1-time channel buttons to the user."""
    links = await generate_single_use_links(bot, user_id)

    if is_lifetime:
        duration_text = "👑 <b>Lifetime Unlimited Access</b> (চিরস্থায়ী)"
    else:
        dt = datetime.datetime.fromisoformat(expires_at_iso)
        duration_text = f"⏳ <b>মেয়াদ:</b> <code>{dt.strftime('%d %b %Y, %I:%M %p (UTC)')}</code>"

    text = (
        f"🎉 <b>অভিনন্দন! আপনার সাবস্ক্রিপশন অ্যাক্টিভ হয়েছে!</b>\n\n"
        f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
        f"📦 <b>প্ল্যান:</b> {plan_name}\n"
        f"{duration_text}\n\n"
        f"🔐 <b>সিকিউর ওয়ান-টাইম এক্সেস লিংক:</b>\n"
        f"<i>নিচের বাটনগুলোতে ক্লিক করে আপনার চ্যানেলগুলোতে দ্রুত জয়েন করুন। প্রতিটি লিংক মাত্র ১ বার ব্যবহারযোগ্য এবং ২৪ ঘণ্টা পর এক্সপায়ার হয়ে যাবে।</i>"
    )

    buttons = []
    for ch_name, link in links:
        if link.startswith("https://"):
            buttons.append([InlineKeyboardButton(f"🔹 {ch_name}", url=link)])
        else:
            text += f"\n\n⚠️ {ch_name}: {link}"

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

    welcome_text = (
        f"👋 <b>স্বাগতম, {user.first_name}!</b>\n\n"
        f"🏛️ <b>{PRODUCT_NAME}</b> এ আপনাকে স্বাগতম।\n"
        f"এখানে আপনি পাবেন সব এক্সক্লুসিভ কোর্স, সিক্রেট মেথডস, প্রিমিয়াম লেকচার এবং ভিআইপি সাপোর্ট।\n\n"
        f"নিচের বাটনগুলো ব্যবহার করে প্ল্যান দেখুন অথবা আপনার মেম্বারশিপ স্ট্যাটাস চেক করুন:"
    )

    keyboard = [
        [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
        [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
        [InlineKeyboardButton("💳 Payment Methods & Info", callback_data="payment_info")],
    ]

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

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

    if not sub or sub["status"] != "active":
        text = (
            f"❌ <b>আপনার কোনো অ্যাক্টিভ মেম্বারশিপ নেই!</b>\n\n"
            f"<b>{PRODUCT_NAME}</b> এর এক্সেস পেতে নিচের বাটনে ক্লিক করে প্ল্যান সিলেক্ট করুন।"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 View Plans", callback_data="plans_menu")]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        return

    # User has active plan
    now = datetime.datetime.now(datetime.timezone.utc)
    if sub["is_lifetime"]:
        time_left = "👑 <b>Lifetime Unlimited</b> (Never Expires)"
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
        f"👤 <b>আপনার মেম্বারশিপ বিস্তারিত:</b>\n\n"
        f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
        f"📦 <b>প্ল্যান:</b> {sub['plan_name']}\n"
        f"📅 <b>মেয়াদ শেষ:</b> <code>{expiry_str}</code>\n"
        f"⏱️ <b>বাকি সময়:</b> {time_left}\n"
        f"🟢 <b>স্ট্যাটাস:</b> Active\n\n"
        f"<i>মেয়াদ শেষ হওয়ার আগেই রিনিউ করে নিরবচ্ছিন্ন এক্সেস নিশ্চিত করুন।</i>"
    )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Renew Subscription", callback_data="user_renew")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


# =============================================================================
# PLANS & PAYMENT INTERACTIVE FLOW
# =============================================================================
async def render_plans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👑 <b>{PRODUCT_NAME} — মেম্বারশিপ প্ল্যানস</b>\n\n"
        f"আপনার পছন্দের প্ল্যানটি বেছে নিন:\n\n"
        f"<b>১. 📅 Monthly Access (30 Days)</b>\n"
        f"• ফুল কোর্স ভল্ট ও ডিসকাশন গ্রুপ এক্সেস\n"
        f"• প্রাইস: <b>{PLANS['monthly']['price_bdt']} BDT / ${PLANS['monthly']['price_usdt']} USDT</b>\n\n"
        f"<b>২. 📆 Yearly Access (365 Days)</b>\n"
        f"• ৩৬৫ দিন নিরবচ্ছিন্ন আপডেট ও প্রায় ৬০% সেভিংস\n"
        f"• প্রাইস: <b>{PLANS['yearly']['price_bdt']} BDT / ${PLANS['yearly']['price_usdt']} USDT</b>\n\n"
        f"<b>৩. 👑 Lifetime Access (Forever)</b>\n"
        f"• একবার কিনলে সারা জীবনের জন্য ফুল ফ্রি এক্সেস\n"
        f"• প্রাইস: <b>{PLANS['lifetime']['price_bdt']} BDT / ${PLANS['lifetime']['price_usdt']} USDT</b>"
    )

    keyboard = [
        [InlineKeyboardButton("📅 Buy Monthly (30 Days)", callback_data="buy_monthly")],
        [InlineKeyboardButton("📆 Buy Yearly (365 Days)", callback_data="buy_yearly")],
        [InlineKeyboardButton("👑 Buy Lifetime (Forever)", callback_data="buy_lifetime")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_key: str):
    query = update.callback_query
    await query.answer()

    plan = PLANS.get(plan_key)
    if not plan:
        return

    text = (
        f"💳 <b>পেমেন্ট ইনস্ট্রাকশন: {plan['name']}</b>\n\n"
        f"💰 <b>মূল্য:</b> {plan['price_bdt']} BDT অথবা ${plan['price_usdt']} USDT\n\n"
        f"নিচের যেকোনো মাধ্যমে পেমেন্ট সম্পন্ন করুন:\n"
        f"📱 <b>বিকাশ (Personal):</b> <code>{PAYMENT_INFO.get('bkash', 'N/A')}</code>\n"
        f"📱 <b>নগদ (Personal):</b> <code>{PAYMENT_INFO.get('nagad', 'N/A')}</code>\n"
        f"🌐 <b>Binance Pay ID:</b> <code>{PAYMENT_INFO.get('binance_pay_id', 'N/A')}</code>\n"
        f"🪙 <b>USDT (TRC20):</b> <code>{PAYMENT_INFO.get('usdt_trc20', 'N/A')}</code>\n\n"
        f"⚠️ <b>টাকা পাঠানোর পর:</b>\n"
        f"নিচের <b>[ ✍️ Submit Trx ID ]</b> বাটনে ক্লিক করে আপনার Transaction ID বা লাস্ট ৪ ডিজিট লিখে পাঠান।\n"
        f"এডমিন ভেরিফাই করা মাত্রই আপনার ইনবক্সে সিক্রেট লিংক চলে আসবে!"
    )

    keyboard = [
        [InlineKeyboardButton("✍️ Submit Transaction ID", callback_data=f"submit_trx_{plan_key}")],
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
        f"✍️ <b>Transaction ID সাবমিট করুন:</b>\n\n"
        f"আপনার পেমেন্টের TrxID অথবা যে নাম্বার থেকে টাকা পাঠিয়েছেন তা এই চ্যাটে লিখে পাঠান:\n\n"
        f"<i>(বাতিল করতে চাইলে /cancel লিখুন)</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML")


async def handle_user_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text submissions (like TrxID or Admin inputs)."""
    user = update.effective_user
    user_id = user.id

    if not is_admin(user_id):
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if not is_subbed:
            await send_force_sub_message(update, context, unjoined_link, unjoined_name)
            return

    state = USER_STATES.get(user_id)

    if not state:
        return

    action = state.get("action")

    # 1. User Submitting TrxID
    if action == "awaiting_trx":
        trx_id = update.message.text.strip()
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
            payment_method="Mobile / Crypto",
            trx_id=trx_id,
        )

        await update.message.reply_text(
            f"✅ <b>পেমেন্ট রিকোয়েস্ট সাবমিট হয়েছে! (ID: #{req_id})</b>\n\n"
            f"📦 <b>প্ল্যান:</b> {plan.get('name')}\n"
            f"🔢 <b>Trx ID:</b> <code>{trx_id}</code>\n\n"
            f"অ্যাডমিন ভেরিফাই করা মাত্রই আপনি এই বটের ইনবক্সে সমস্ত VIP চ্যানেলের ওয়ান-টাইম জয়েন লিংক পেয়ে যাবেন।",
            parse_mode="HTML",
        )

        # Alert Admins with Interactive Approval Card
        admin_card = (
            f"💳 <b>[NEW PAYMENT SUBMISSION #{req_id}]</b>\n\n"
            f"👤 <b>User:</b> <a href='tg://user?id={user_id}'>{user.full_name}</a> (@{user.username or 'none'})\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📦 <b>Plan:</b> {plan.get('name')}\n"
            f"💰 <b>Amount:</b> {plan.get('price_bdt')} BDT\n"
            f"🔢 <b>Trx ID / Note:</b> <code>{trx_id}</code>"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_{req_id}"),
            ]
        ])

        for adm in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=adm, text=admin_card, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass
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
                    save_config()
                    await update.message.reply_text(f"✅ Channel <code>{ch_id}</code> updated to: <b>{ch_name}</b>", parse_mode="HTML")
                    return

            try:
                member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                status_text = "✅ Bot is Admin" if member.status in ("administrator", "creator") else "⚠️ Bot is NOT admin yet!"
            except Exception as e:
                status_text = f"⚠️ Could not verify admin rights: {e}"

            CHANNELS.append({"id": ch_id, "name": ch_name, "invite_expire_hours": 24})
            save_config()
            await update.message.reply_text(
                f"✅ <b>VIP Channel Added!</b>\n"
                f"📚 Name: <b>{ch_name}</b>\n"
                f"🆔 ID: <code>{ch_id}</code>\n"
                f"⚙️ Status: {status_text}",
                parse_mode="HTML",
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid channel ID. Must be numeric (e.g. -1001234567890).")
        except Exception as e:
            await update.message.reply_text(f"❌ Error adding channel: {e}")
        return



# =============================================================================
# ADMIN DASHBOARD & CONTROLS
# =============================================================================
async def render_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    stats = db.get_stats()

    text = (
        f"👑 <b>PRO ACCESS VAULT — ADMIN CONTROL CENTER</b>\n"
        f"════════════════════════════════════\n"
        f"📊 <b>Active Subscribers:</b> {stats['total_active']}\n"
        f"⏳ <b>Expiring within 24h:</b> {stats['expiring_24h']}\n"
        f"👑 <b>Lifetime Members:</b> {stats['total_lifetime']}\n"
        f"💀 <b>Removed / Expired:</b> {stats['total_removed']}\n"
        f"💳 <b>Pending Payments:</b> {stats['pending_payments']}\n"
        f"════════════════════════════════════\n"
        f"<i>Select an operation below:</i>"
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
            InlineKeyboardButton("🗑️ Del VIP Channel", callback_data="adm_action_del_channel"),
        ],
        [
            InlineKeyboardButton("💳 Edit Payments", callback_data="adm_action_edit_payments"),
            InlineKeyboardButton(f"⚙️ Channels List ({len(CHANNELS)})", callback_data="adm_action_channels"),
        ],
        [
            InlineKeyboardButton("💾 Backup Database Now", callback_data="adm_action_backup"),
            InlineKeyboardButton("📢 Broadcast DM", callback_data="adm_action_broadcast"),
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

        CHANNELS.append({"id": ch_id, "name": ch_name, "invite_expire_hours": 24})
        save_config()

        await update.message.reply_text(
            f"✅ <b>VIP Channel Added Successfully!</b>\n"
            f"📚 <b>Name:</b> {ch_name}\n"
            f"🆔 <b>ID:</b> <code>{ch_id}</code>\n"
            f"⚙️ <b>Status:</b> {admin_status_text}\n\n"
            f"<i>This channel is now active for 1-time invite links and auto-kicks!</i>",
            parse_mode="HTML",
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid Channel ID! Must be a numeric ID (usually starts with -100).")
    except Exception as e:
        await update.message.reply_text(f"❌ Error adding channel: {e}")


async def cmd_delchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /delchannel <channel_id>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("<b>Usage:</b> <code>/delchannel &lt;channel_id&gt;</code>", parse_mode="HTML")
        return

    try:
        ch_id = int(args[0])
        initial_len = len(CHANNELS)
        CHANNELS[:] = [ch for ch in CHANNELS if ch["id"] != ch_id]

        if len(CHANNELS) < initial_len:
            save_config()
            await update.message.reply_text(f"🗑️ Channel <code>{ch_id}</code> removed from bot management.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Channel <code>{ch_id}</code> not found in configured list.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Channel ID.")


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all managed VIP channels with status check."""
    if not is_admin(update.effective_user.id):
        return

    if not CHANNELS:
        await update.message.reply_text(
            "ℹ️ No VIP channels configured yet.\nUse <code>/addchannel &lt;id&gt; &lt;name&gt;</code> to add one!",
            parse_mode="HTML",
        )
        return

    lines = ["⚙️ <b>CONFIGURED VIP CHANNELS:</b>\n"]
    for idx, ch in enumerate(CHANNELS, 1):
        ch_id = ch["id"]
        ch_name = ch.get("name", str(ch_id))
        try:
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
            status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
        except Exception as e:
            status = f"❌ Error: {e}"
        lines.append(f"{idx}. <b>{ch_name}</b>\n   ID: <code>{ch_id}</code> | Status: {status}\n")

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


# =============================================================================
# CALLBACK QUERY ROUTER (Inline Button Actions)
# =============================================================================
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # 0. Force Channel Join Check Action
    if data == "check_force_sub":
        is_subbed, unjoined_id, unjoined_link, unjoined_name = await check_force_sub(user_id, context)
        if is_subbed:
            await query.answer("✅ ধন্যবাদ! ভেরিফিকেশন সফল হয়েছে।", show_alert=True)
            user = query.from_user
            welcome_text = (
                f"👋 <b>স্বাগতম, {user.first_name}!</b>\n\n"
                f"🏛️ <b>{PRODUCT_NAME}</b> এ আপনাকে স্বাগতম।\n"
                f"এখানে আপনি পাবেন সব এক্সক্লুসিভ কোর্স, সিক্রেট মেথডস, প্রিমিয়াম লেকচার এবং ভিআইপি সাপোর্ট।\n\n"
                f"নিচের বাটনগুলো ব্যবহার করে প্ল্যান দেখুন অথবা আপনার মেম্বারশিপ স্ট্যাটাস চেক করুন:"
            )
            keyboard = [
                [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
                [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
                [InlineKeyboardButton("💳 Payment Methods & Info", callback_data="payment_info")],
            ]
            if is_admin(user_id):
                keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

            await query.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
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
        welcome_text = (
            f"🏛️ <b>{PRODUCT_NAME}</b>\n\n"
            f"Select an option below to explore plans or check status:"
        )
        keyboard = [
            [InlineKeyboardButton("👑 VIP Plans & Pricing", callback_data="plans_menu")],
            [InlineKeyboardButton("👤 My Subscription Status", callback_data="user_status")],
            [InlineKeyboardButton("💳 Payment Methods & Info", callback_data="payment_info")],
        ]
        if is_admin(user_id):
            keyboard.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="admin_dashboard")])

        await query.edit_message_text(welcome_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "plans_menu":
        await render_plans_menu(update, context)
        return

    if data == "user_status":
        await query.answer()
        # Delegate to status rendering
        sub = db.get_subscriber(user_id)
        if not sub or sub["status"] != "active":
            text = (
                f"❌ <b>আপনার কোনো অ্যাক্টিভ মেম্বারশিপ নেই!</b>\n\n"
                f"<b>{PRODUCT_NAME}</b> এর এক্সেস পেতে নিচের বাটনে ক্লিক করে প্ল্যান সিলেক্ট করুন।"
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("👑 View Plans", callback_data="plans_menu")]])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        if sub["is_lifetime"]:
            time_left = "👑 <b>Lifetime Unlimited</b> (Never Expires)"
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
            f"👤 <b>আপনার মেম্বারশিপ বিস্তারিত:</b>\n\n"
            f"📚 <b>প্রোডাক্ট:</b> {PRODUCT_NAME}\n"
            f"📦 <b>প্ল্যান:</b> {sub['plan_name']}\n"
            f"📅 <b>মেয়াদ শেষ:</b> <code>{expiry_str}</code>\n"
            f"⏱️ <b>বাকি সময়:</b> {time_left}\n"
            f"🟢 <b>স্ট্যাটাস:</b> Active\n\n"
            f"<i>মেয়াদ শেষ হওয়ার আগেই রিনিউ করে নিরবচ্ছিন্ন এক্সেস নিশ্চিত করুন।</i>"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Renew Subscription", callback_data="user_renew")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if data == "user_renew":
        await render_plans_menu(update, context)
        return

    if data == "payment_info":
        await query.answer()
        text = (
            f"💳 <b>অফিশিয়াল পেমেন্ট মেথডস</b>\n\n"
            f"📱 <b>বিকাশ (Personal):</b> <code>{PAYMENT_INFO.get('bkash', 'N/A')}</code>\n"
            f"📱 <b>নগদ (Personal):</b> <code>{PAYMENT_INFO.get('nagad', 'N/A')}</code>\n"
            f"🌐 <b>Binance Pay ID:</b> <code>{PAYMENT_INFO.get('binance_pay_id', 'N/A')}</code>\n"
            f"🪙 <b>USDT (TRC20):</b> <code>{PAYMENT_INFO.get('usdt_trc20', 'N/A')}</code>\n\n"
            f"টাকা পাঠানোর পর 'VIP Plans' মেনু থেকে প্ল্যান সিলেক্ট করে Trx ID সাবমিট করুন।"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Select Plan & Pay", callback_data="plans_menu")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")],
        ])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
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
        req_id = int(data.replace("adm_app_", ""))
        req = db.get_payment_request(req_id)
        if not req or req["status"] != "pending":
            await query.answer("Already processed!", show_alert=True)
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

        # Deliver links to the buyer
        await deliver_welcome_access(context.bot, req["user_id"], plan["name"], sub["expires_at"], is_lifetime)

        await query.edit_message_text(
            f"✅ <b>PAYMENT APPROVED (#{req_id})</b>\n\n"
            f"👤 Member: <code>{req['user_id']}</code> (@{req['username']})\n"
            f"📦 Plan: <b>{plan['name']}</b>\n"
            f"🔢 Trx: <code>{req['trx_id']}</code>\n"
            f"🚀 1-Time Invite Links dispatched to member's DM.\n"
            f"👮 Approved by: @{query.from_user.username or query.from_user.id}",
            parse_mode="HTML",
        )
        return

    if data.startswith("adm_rej_"):
        req_id = int(data.replace("adm_rej_", ""))
        db.update_payment_status(req_id, "rejected")
        req = db.get_payment_request(req_id)
        if req:
            try:
                await context.bot.send_message(
                    chat_id=req["user_id"],
                    text=f"❌ Your payment submission (Trx: <code>{req['trx_id']}</code>) could not be verified by administration. Please contact support.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        await query.edit_message_text(
            f"❌ <b>Payment Rejected (#{req_id})</b>\n👮 Action by: @{query.from_user.username or query.from_user.id}",
            parse_mode="HTML",
        )
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
        lines = ["⚙️ <b>CONFIGURED VIP CHANNELS:</b>\n"]
        if not CHANNELS:
            lines.append("<i>No VIP channels configured yet. Click 'Add VIP Channel' below or use /addchannel.</i>")
        else:
            for ch in CHANNELS:
                ch_id = ch["id"]
                ch_name = ch.get("name", str(ch_id))
                try:
                    member = await context.bot.get_chat_member(chat_id=ch_id, user_id=context.bot.id)
                    status = "✅ Admin" if member.status in ("administrator", "creator") else "⚠️ Not Admin"
                except Exception as e:
                    status = f"❌ Error: {e}"
                lines.append(f"• <b>{ch_name}</b>\n  ID: <code>{ch_id}</code> | Status: {status}\n")

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]])
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
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

    if data == "adm_action_del_channel":
        if not CHANNELS:
            await query.answer("No channels configured to delete!", show_alert=True)
            return
        buttons = []
        for ch in CHANNELS:
            buttons.append([InlineKeyboardButton(f"🗑️ Remove: {ch['name']}", callback_data=f"adm_confirm_del_ch_{ch['id']}")])
        buttons.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")])
        await query.edit_message_text("🗑️ <b>Select Channel to Remove:</b>", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        return

    if data.startswith("adm_confirm_del_ch_"):
        ch_id = int(data.replace("adm_confirm_del_ch_", ""))
        CHANNELS[:] = [ch for ch in CHANNELS if ch["id"] != ch_id]
        save_config()
        await query.answer("Channel removed!", show_alert=True)
        await render_admin_dashboard(update, context)
        return

    if data == "adm_action_edit_payments":
        text = (
            "💳 <b>CURRENT PAYMENT INFO:</b>\n\n"
            f"📱 <b>bKash:</b> <code>{PAYMENT_INFO.get('bkash', 'N/A')}</code>\n"
            f"📱 <b>Nagad:</b> <code>{PAYMENT_INFO.get('nagad', 'N/A')}</code>\n"
            f"🌐 <b>Binance Pay:</b> <code>{PAYMENT_INFO.get('binance_pay_id', 'N/A')}</code>\n"
            f"🪙 <b>USDT TRC20:</b> <code>{PAYMENT_INFO.get('usdt_trc20', 'N/A')}</code>\n\n"
            "To update any number directly, send:\n"
            "<code>/setpayment &lt;bkash|nagad|binance|usdt&gt; &lt;new_number&gt;</code>"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_dashboard")]])
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
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
    """Starts the 24/7 background scheduler when the bot launches."""
    scheduler = VaultScheduler(bot=application.bot, config=CONFIG)
    application.create_task(scheduler.start())
    logger.info("Background Auto-Kick Scheduler task created successfully.")


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
    app.add_handler(CommandHandler("delchannel", cmd_delchannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("setpayment", cmd_setpayment))
    app.add_handler(CommandHandler("setforcechannel", cmd_setforcechannel))

    # Callbacks and Text Handlers
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
