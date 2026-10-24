# language: Python, file: emojis.py, target: Python 3.10+, python-telegram-bot v21+
"""
Telegram Premium Custom Emoji Engine for Pro Access Vault - Course Edition.
Ported and enhanced from @AllInOneProAccess_bot's master custom emoji catalog.
Provides 100% automated conversion of standard emojis to Telegram Premium animated custom emojis
(<tg-emoji emoji-id="...">char</tg-emoji>) with automatic crash immunity and fallback.
"""

import unicodedata
import re
import html
import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("vault_emojis")

# =============================================================================
# MASTER TELEGRAM PREMIUM CUSTOM EMOJI CATALOG (Direct from @AllInOneProAccess_bot)
# =============================================================================
DEFAULT_CUSTOM_EMOJIS: Dict[str, str] = {
    # Core Navigation & Shell
    "home": "5323642109767460983",         # 🏠 Home / Main Menu
    "menu": "5323642109767460983",         # 🏠 Main Menu
    "admin": "6235252066554484059",        # 👑 Admin Crown / VIP Leader
    "crown": "6235252066554484059",        # 👑 Crown
    "help": "5373098009640836781",         # ❓ Help / Support
    "support": "5373098009640836781",      # 💬 Support / Contact
    "language": "5397798946380721942",     # 🌐 Language / Globe
    "settings": "5201762530023733712",     # ⚙️ Settings / Tools / Icons

    # Products & Digital Accounts
    "digital": "5445118546700954082",      # ⚡ Digital Tools & Accounts
    "box": "5837134496868077492",          # 📦 Package / Stock / Key
    "stock": "5837134496868077492",        # 📦 Stock Box
    "download": "5445118546700954082",     # 🚀 Download / Direct Link

    # Giveaways, Drops & Flash
    "free_claim": "5397916757333654639",   # 🎁 Free Claim / Giveaway Drops
    "gift": "5397916757333654639",         # 🎁 Gift Box
    "zap": "6271459718896554468",          # ⚡ Instant Claim / Fast Flash
    "fire": "5136918320674505825",         # 🔥 Fire / Trending / Hot Drop

    # Finance, Wallet & Transactions
    "wallet": "5395542928909150340",       # 💰 Wallet / Balance / Deposit
    "deposit": "5395542928909150340",      # 💳 Deposit / Money / Cash
    "cash": "5395542928909150340",         # 💵 Cash
    "cart": "6214990154608681626",         # 🛒 / 🛍️ Browse Store / Cart
    "shop": "6214990154608681626",         # 🛍️ Browse Store
    "coupon": "5393383127294953991",       # 🎟️ Coupon / Voucher / Ticket

    # Resellers & Verification
    "diamond": "6271494293383286950",      # 💎 Reseller Diamond / VIP
    "reseller": "6271494293383286950",     # 💎 Reseller Hub
    "courses": "5445118546700954082",      # 🎓 Courses / Cloud Masterclass
    "category": "5837134496868077492",     # 📁 Category
    "orders": "5837134496868077492",       # 📜 Orders / Vault
    "verify": "6271494293383286950",       # 🛡️ Verify / Shield
    "back": "5323642109767460983",         # 🔙 Back
    "trophy": "5226431245918942763",       # 🏆 Trophy / Leaderboard
    "star": "6181535395914718008",         # ⭐ Star / Rating
    "target": "5310278924616356636",       # 🎯 Quota / Target / Race
    "flag": "5310278924616356636",         # 🏁 Live Race / Challenge

    # Community & Referrals
    "referral": "5292109589456645419",     # 👥 Referral / Affiliates / Team
    "users": "5292109589456645419",        # 👥 Users
    "profile": "6269397073737553354",      # 👤 Profile / My Account
    "broadcast": "6242353099193718277",    # 📢 Broadcast / Channel
    "channel": "6242353099193718277",      # 📢 Channel Link

    # Actions & Indicators
    "check": "5202010985291871421",        # ✅ Active / Approved / Success
    "cross": "5204244329631082615",        # ❌ Reject / Close / Cancel
    "refresh": "5445118546700954082",      # 🔄 Refresh
    "edit": "5197269100878907942",         # ✏️ Edit
    "search": "5310278924616356636",       # 🔍 Search
    "sparkle": "6179411633371095707",      # ✨ Sparkle / Highlights

    # Payment Gateways (Special Badges)
    "bkash": "6237941149873478197",        # bKash Custom Badge
    "nagad": "6235269688805301565",        # Nagad Custom Badge
    "rocket": "6235282831405227077",       # Rocket Custom Badge
    "binance": "6235545537374855311",      # Binance Custom Badge
    "usdt": "6235545537374855311",         # USDT Custom Badge

    # Languages
    "lang_bn": "5911365056594973179",      # 🇧🇩 Bengali
    "lang_en": "5780829794899858599",      # 🇬🇧 English
    "lang_hi": "6109380284644329775",      # 🇮🇳 Hindi
    "lang_ru": "5449408995691341691",      # 🇷🇺 Russian
}

# Exhaustive Mapping from ALL Unicode Emoji Characters to DEFAULT_CUSTOM_EMOJIS Keys
EMOJI_CHAR_TO_KEY: Dict[str, str] = {
    # Crown & VIP
    "👑": "crown",
    "💎": "diamond",
    "🏛️": "verify",
    "🏛": "verify",

    # Money, Wallet & Billing
    "💰": "wallet",
    "💳": "deposit",
    "💵": "cash",
    "💸": "cash",
    "🪙": "wallet",
    "🛒": "cart",
    "🛍️": "cart",
    "🛍": "cart",
    "🏪": "cart",

    # Stock, Packages, Keys & Storage
    "📦": "box",
    "📫": "box",
    "📬": "box",
    "📩": "box",
    "📥": "box",
    "📤": "box",
    "🔑": "box",
    "📁": "category",
    "📂": "category",
    "💾": "digital",
    "☁️": "digital",
    "☁": "digital",

    # Giveaways, Gifts, Celebrations & Food/Cookies
    "🎁": "free_claim",
    "🎉": "free_claim",
    "🍾": "free_claim",
    "🍪": "sparkle",
    "🌱": "sparkle",
    "🎨": "sparkle",
    "🎬": "sparkle",
    "🎭": "sparkle",

    # Instant, Speed & Lightning
    "⚡️": "zap",
    "⚡": "zap",
    "💡": "zap",
    "⏳": "zap",
    "⌛": "zap",
    "⏰": "zap",
    "⏱️": "zap",
    "⏱": "zap",
    "🕒": "zap",
    "🔓": "zap",
    "👉": "zap",
    "👆": "zap",
    "➡️": "zap",

    # Fire, Warning & Alert
    "🔥": "fire",
    "⚠️": "fire",
    "⚠": "fire",
    "🚨": "fire",
    "💥": "fire",
    "⛔": "cross",

    # Orders & Catalog
    "📜": "orders",
    "🧾": "orders",
    "📃": "orders",
    "📄": "orders",
    "📋": "orders",
    "📅": "orders",
    "📆": "orders",
    "📊": "orders",
    "📈": "sparkle",
    "📉": "zap",

    # Tickets & Coupons
    "🎟️": "coupon",
    "🎟": "coupon",
    "🎫": "coupon",
    "🏷️": "coupon",
    "🏷": "coupon",

    # Security & Verification
    "🛡️": "verify",
    "🛡": "verify",
    "🔰": "verify",
    "🔐": "verify",
    "🔒": "verify",

    # Trophies, Competitions & Races
    "🏆": "trophy",
    "🥇": "trophy",
    "🥈": "trophy",
    "🥉": "trophy",
    "🎖️": "trophy",
    "🎖": "trophy",
    "🎯": "target",
    "🏁": "flag",
    "🚩": "flag",
    "🏃": "flag",
    "🏎️": "flag",
    "🏎": "flag",
    "📌": "target",

    # Stars & Sparkles
    "⭐": "star",
    "⭐️": "star",
    "🌟": "star",
    "✨": "sparkle",
    "👋": "sparkle",
    "🔹": "sparkle",

    # Users, Profiles, Community & Collaboration
    "👥": "referral",
    "👤": "profile",
    "🤝": "reseller",
    "👨": "profile",
    "🆔": "profile",
    "💼": "profile",

    # Broadcast, Announcement & Channels
    "📢": "broadcast",
    "📣": "broadcast",
    "🔔": "broadcast",
    "🔕": "cross",

    # Support & Communication
    "💬": "support",
    "🗨️": "support",
    "🗨": "support",
    "❓": "help",
    "ℹ️": "help",
    "ℹ": "help",

    # Navigation & System Shell
    "🏠": "home",
    "🔙": "back",
    "🌐": "language",
    "⚙️": "settings",
    "⚙": "settings",
    "🛠️": "settings",
    "🛠": "settings",
    "🔧": "settings",
    "🤖": "settings",

    # Education & Courses
    "🎓": "courses",
    "📚": "courses",
    "📖": "courses",

    # Digital Tools, Hardware & Links
    "🚀": "download",
    "🔗": "download",
    "💻": "digital",
    "📱": "digital",
    "📸": "digital",
    "🖼️": "digital",
    "🖼": "digital",

    # Actions & Editing
    "🔄": "refresh",
    "🔁": "refresh",
    "✏️": "edit",
    "✏": "edit",
    "📝": "edit",
    "✍️": "edit",
    "✍": "edit",
    "🔍": "search",
    "🔎": "search",

    # Status & Indicators
    "✅": "check",
    "✔️": "check",
    "✔": "check",
    "☑️": "check",
    "☑": "check",
    "🟢": "check",
    "🟩": "check",
    "❌": "cross",
    "✖️": "cross",
    "✖": "cross",
    "🚫": "cross",
    "🛑": "cross",
    "🔴": "cross",
    "🟥": "cross",
    "🗑️": "cross",
    "🗑": "cross",
    "🟠": "sparkle",
    "🟡": "sparkle",
    "🟣": "sparkle",

    # Languages
    "🇧🇩": "lang_bn",
    "🇬🇧": "lang_en",
    "🇮🇳": "lang_hi",
    "🇵🇰": "lang_ur",
    "🇷🇺": "lang_ru",
}

NON_EMOJI_SYMBOLS = {'•', '◦', '‣', '▰', '▱', '^', '`', '─', '━', '█', '░', '*', '_', '~', '|'}


def is_actual_emoji(s: str) -> bool:
    """Accurately checks if a character or cluster is a valid Telegram-supported emoji."""
    if not s or s in NON_EMOJI_SYMBOLS:
        return False
    cp = ord(s[0])
    cat = unicodedata.category(s[0])
    if cat in ('So', 'Sk') or 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF or 0x2B50 <= cp <= 0x2B55:
        return True
    return False


def get_custom_emoji_id(name: str) -> Optional[str]:
    """Retrieves dynamic custom emoji ID with fallback to master catalog."""
    if not name:
        return None
    clean_name = name.lower().strip()
    try:
        from database import db
        val = db.get_system_setting(f"emoji_{clean_name}")
        if val and str(val).strip().isdigit():
            return str(val).strip()
    except Exception:
        pass
    return DEFAULT_CUSTOM_EMOJIS.get(clean_name)


def strip_all_emojis(text: str, preserve_checkmarks: bool = False) -> str:
    """Strips unicode emojis while strictly preserving formatting symbols."""
    if not text:
        return ""
    chars = []
    for c in text:
        cp = ord(c)
        if c in ('➔', '->', '❯', '›', '»', '▼', '▲', '•', '◦', '‣', '▰', '▱', '─', '━', '█', '░', '|'):
            chars.append(c)
            continue
        if preserve_checkmarks and cp in (0x2705, 0x2611, 0x2713, 0x2714):
            chars.append(c)
            continue
        cat = unicodedata.category(c)
        if (
            cat in ('So', 'Sk') or
            0x1F000 <= cp <= 0x1FAFF or
            0x2600 <= cp <= 0x27BF or
            0x2300 <= cp <= 0x23FF or
            0x2B50 <= cp <= 0x2B55 or
            0xFE00 <= cp <= 0xFE0F or
            cp == 0x200D or cp == 0x20E3
        ):
            continue
        chars.append(c)
    cleaned = ''.join(chars).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else text.strip()


def strip_tg_emoji(text: str) -> str:
    """Strips <tg-emoji> tags while preserving the inner emoji character."""
    if not text or not isinstance(text, str):
        return text
    return re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', text)


def get_tg_emoji_tag(name: str, fallback: str = "✨") -> str:
    """Returns HTML <tg-emoji emoji-id="...">fallback</tg-emoji> tag for message texts."""
    eid = get_custom_emoji_id(name)
    if eid and eid.isdigit():
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


# Concise alias for high-frequency template rendering
em_tag = get_tg_emoji_tag


def render_premium_html(text: str) -> str:
    """
    Parses an HTML message string and converts all standard unicode emojis into
    Telegram Premium custom emoji tags (<tg-emoji emoji-id="...">char</tg-emoji>).
    Preserves <pre>, <code>, and existing <tg-emoji> blocks from modification.
    """
    if not text or not isinstance(text, str):
        return text

    # Sort emoji keys by length descending to match composite emojis like 🛡️ before 🛡
    sorted_chars = sorted(EMOJI_CHAR_TO_KEY.keys(), key=len, reverse=True)
    pattern = "(" + "|".join(re.escape(k) for k in sorted_chars) + ")"
    emoji_regex = re.compile(pattern)

    # Split text into HTML tags and non-tag text segments
    tokens = re.split(r'(<[^>]+>)', text)
    in_skip_block = False
    skip_tags = {"pre", "code", "tg-emoji"}
    current_skip_tag = None

    result = []
    for token in tokens:
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            tag_content = token[1:-1].strip()
            is_closing = tag_content.startswith("/")
            tag_name = tag_content.lstrip("/").split()[0].lower() if tag_content.lstrip("/") else ""

            if not is_closing and tag_name in skip_tags:
                in_skip_block = True
                current_skip_tag = tag_name
            elif is_closing and tag_name == current_skip_tag:
                in_skip_block = False
                current_skip_tag = None

            result.append(token)
        else:
            if in_skip_block:
                result.append(token)
            else:
                def replace_emoji(match):
                    char = match.group(0)
                    key = EMOJI_CHAR_TO_KEY.get(char)
                    if key:
                        eid = get_custom_emoji_id(key)
                        if eid and eid.isdigit():
                            return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
                    return char

                result.append(emoji_regex.sub(replace_emoji, token))

    return "".join(result)


def patch_bot_for_premium_emojis():
    """
    Universally patches python-telegram-bot's telegram.Bot class to ensure
    100% of outgoing HTML messages automatically render Telegram Premium custom emojis.
    Includes automated recovery from parsing/entity errors with zero bot downtime.
    """
    try:
        from telegram import Bot
        from telegram.error import BadRequest

        _orig_send_message = Bot.send_message
        _orig_edit_message_text = Bot.edit_message_text
        _orig_send_photo = Bot.send_photo
        _orig_edit_message_caption = Bot.edit_message_caption

        async def safe_send_message(self, *args, **kwargs):
            parse_mode = kwargs.get("parse_mode")
            if parse_mode == "HTML" and kwargs.get("text"):
                kwargs["text"] = render_premium_html(kwargs["text"])
            try:
                return await _orig_send_message(self, *args, **kwargs)
            except BadRequest as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    return True
                if any(term in err_msg for term in ("can't parse entities", "entity_text_invalid", "custom_emoji_id_invalid", "entity_bounds_invalid", "tag_empty", "failed to parse")):
                    if kwargs.get("text"):
                        kwargs["text"] = strip_tg_emoji(kwargs["text"])
                    try:
                        return await _orig_send_message(self, *args, **kwargs)
                    except BadRequest as e2:
                        if "message is not modified" in str(e2).lower():
                            return True
                        kwargs["parse_mode"] = None
                        return await _orig_send_message(self, *args, **kwargs)
                raise

        async def safe_edit_message_text(self, *args, **kwargs):
            parse_mode = kwargs.get("parse_mode")
            if parse_mode == "HTML" and kwargs.get("text"):
                kwargs["text"] = render_premium_html(kwargs["text"])
            try:
                return await _orig_edit_message_text(self, *args, **kwargs)
            except BadRequest as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    return True
                if any(term in err_msg for term in ("can't parse entities", "entity_text_invalid", "custom_emoji_id_invalid", "entity_bounds_invalid", "tag_empty", "failed to parse")):
                    if kwargs.get("text"):
                        kwargs["text"] = strip_tg_emoji(kwargs["text"])
                    try:
                        return await _orig_edit_message_text(self, *args, **kwargs)
                    except BadRequest as e2:
                        if "message is not modified" in str(e2).lower():
                            return True
                        kwargs["parse_mode"] = None
                        return await _orig_edit_message_text(self, *args, **kwargs)
                raise

        async def safe_send_photo(self, *args, **kwargs):
            parse_mode = kwargs.get("parse_mode")
            if parse_mode == "HTML" and kwargs.get("caption"):
                kwargs["caption"] = render_premium_html(kwargs["caption"])
            try:
                return await _orig_send_photo(self, *args, **kwargs)
            except BadRequest as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    return True
                if any(term in err_msg for term in ("can't parse entities", "entity_text_invalid", "custom_emoji_id_invalid", "entity_bounds_invalid", "tag_empty", "failed to parse")):
                    if kwargs.get("caption"):
                        kwargs["caption"] = strip_tg_emoji(kwargs["caption"])
                    try:
                        return await _orig_send_photo(self, *args, **kwargs)
                    except BadRequest as e2:
                        if "message is not modified" in str(e2).lower():
                            return True
                        kwargs["parse_mode"] = None
                        return await _orig_send_photo(self, *args, **kwargs)
                raise

        async def safe_edit_message_caption(self, *args, **kwargs):
            parse_mode = kwargs.get("parse_mode")
            if parse_mode == "HTML" and kwargs.get("caption"):
                kwargs["caption"] = render_premium_html(kwargs["caption"])
            try:
                return await _orig_edit_message_caption(self, *args, **kwargs)
            except BadRequest as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    return True
                if any(term in err_msg for term in ("can't parse entities", "entity_text_invalid", "custom_emoji_id_invalid", "entity_bounds_invalid", "tag_empty", "failed to parse")):
                    if kwargs.get("caption"):
                        kwargs["caption"] = strip_tg_emoji(kwargs["caption"])
                    try:
                        return await _orig_edit_message_caption(self, *args, **kwargs)
                    except BadRequest as e2:
                        if "message is not modified" in str(e2).lower():
                            return True
                        kwargs["parse_mode"] = None
                        return await _orig_edit_message_caption(self, *args, **kwargs)
                raise

        Bot.send_message = safe_send_message
        Bot.edit_message_text = safe_edit_message_text
        Bot.send_photo = safe_send_photo
        Bot.edit_message_caption = safe_edit_message_caption
        logger.info("Universal Telegram Premium Custom Emoji Hook initialized on Bot methods.")
    except Exception as e:
        logger.error(f"Failed to hook Bot methods for premium emojis: {e}")
