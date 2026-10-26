# language: Python, file: scheduler.py, target: Python 3.10+, python-telegram-bot v21+
"""
Asynchronous Background Scheduler & Auto-Kick Engine.
Monitors subscriber lifecycles 24/7:
- Dispatches 24h expiration reminders.
- Executes clean, instant auto-kicks (ban + unban) across all VIP channels/groups when expired.
- Performs automated daily atomic database backups.
"""

import os
import html
import asyncio
import datetime
import logging
from typing import Dict, Any, List

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError, BadRequest, Forbidden

from database import db

logger = logging.getLogger("vault_scheduler")


class VaultScheduler:
    def __init__(self, bot: Bot, config: Dict[str, Any]):
        self.bot = bot
        self.config = config
        self.interval = config.get("scheduler_interval_seconds", 60)
        self.channels: List[Dict[str, Any]] = config.get("channels", [])
        self.admin_ids: List[int] = config.get("admin_ids", [])
        self.log_channel_id = config.get("log_channel_id")
        self.product_name = config.get("product_name", "Pro Access Vault - Course Edition")
        self.is_running = False
        self._last_backup_date = None

    async def start(self):
        """Starts the infinite scheduler loop."""
        self.is_running = True
        logger.info(f"Vault Scheduler started (Scan interval: {self.interval}s)")

        while self.is_running:
            try:
                await self._run_cycle()
            except Exception as e:
                logger.error(f"Error in scheduler execution cycle: {e}", exc_info=True)

            await asyncio.sleep(self.interval)

    def stop(self):
        self.is_running = False
        logger.info("Vault Scheduler stopped.")

    async def _run_cycle(self):
        """Executes one scan cycle: Reminders, Expirations, and Auto-backups."""
        await self._process_reminders()
        await self._process_expirations()
        await self._check_daily_backup()

    # =========================================================================
    # 1. MULTI-STAGE EXPIRATION REMINDERS (e.g. 24h, 12h, 1h Alert)
    # =========================================================================
    async def _process_reminders(self):
        stages_str = db.get_setting("reminder_stages", "24,12,1")
        try:
            stages = [int(s.strip()) for s in stages_str.split(",") if s.strip().isdigit()]
        except Exception:
            stages = [24, 12, 1]

        due_items = db.get_due_stage_reminders(stages=stages)

        for user, stage in due_items:
            user_id = user["user_id"]
            try:
                expiry_dt = datetime.datetime.fromisoformat(user["expires_at"])
                formatted_expiry = expiry_dt.strftime("%d %b %Y, %I:%M %p")

                if stage == 1:
                    badge = "🚨 <b>[ফাইনাল নোটিশ — আর মাত্র ১ ঘণ্টা বাকি!]</b>"
                    urgency_desc = (
                        "⏱️ <b>মেয়াদ শেষ হতে বাকি:</b> <code>আর মাত্র ৬০ মিনিট!</code>\n\n"
                        "🔴 <i>১ ঘণ্টা পূর্ণ হওয়ামাত্রই সিস্টেম রোবট আপনাকে কানেক্টেড সব VIP চ্যানেল ও গ্রুপ থেকে রিমুভ করে দেবে।</i>\n\n"
                        "⚡ <i>নিরবচ্ছিন্ন এক্সেস রাখতে এখনই রিনিউ বাটনে চাপ দিয়ে পেমেন্ট সম্পন্ন করুন:</i>"
                    )
                    btn_text = "👑 1-Click Renew Now"
                elif stage <= 12:
                    badge = f"⚠️ <b>[জরুরি সতর্কতা — আর মাত্র {stage} ঘণ্টা বাকি!]</b>"
                    urgency_desc = (
                        f"⏳ <b>কাউন্টডাউন:</b> <code>{stage} ঘণ্টার মধ্যে VIP এক্সেস সমাপ্ত হবে</code>\n\n"
                        "🚨 <i>আজকের মধ্যে রিনিউ না করলে সকল এক্সক্লুসিভ কোর্স ও ডিসকাশন গ্রুপ থেকে আপনাকে স্বয়ংক্রিয়ভাবে রিমুভ করা হবে।</i>\n\n"
                        "👉 <i>দ্রুত রিনিউ করতে নিচের বাটনে ট্যাপ করুন:</i>"
                    )
                    btn_text = "🔄 Quick Renew"
                else:
                    badge = f"⏳ <b>[VIP মেম্বারশিপ মেয়াদ সমাপ্তি নোটিশ — {stage} ঘণ্টা বাকি]</b>"
                    urgency_desc = (
                        f"⚠️ <i>আপনার সাবস্ক্রিপশনের মেয়াদ আর মাত্র {stage} ঘণ্টা পর শেষ হবে। নির্ধারিত সময়ের পর চ্যানেল ও গ্রুপসমূহ থেকে স্বয়ংক্রিয়ভাবে রিমুভ করা হবে।</i>\n\n"
                        "👉 <i>নিরবচ্ছিন্ন এক্সেস বজায় রাখতে এখনই রিনিউ সম্পন্ন করুন:</i>"
                    )
                    btn_text = "🔄 Renew Subscription"

                safe_name = html.escape(str(user.get("full_name") or user.get("username") or "VIP Member"))
                preview_link = db.get_setting("vip_preview_link") or "https://t.me/PAV_CourseEdition/16"
                text = (
                    f"{badge}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Member:</b> {safe_name} (ID: <code>{user_id}</code>)\n"
                    f"📦 <b>প্ল্যান:</b> <b>{user['plan_name']}</b>\n"
                    f"⏳ <b>মেয়াদ শেষ:</b> <code>{formatted_expiry}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{urgency_desc}\n\n"
                    f"🔒 <b>প্রাইস-লক হারানোর সতর্কতা:</b>\n"
                    f"<i>মেয়াদ শেষ হয়ে চ্যানেল থেকে রিমুভ হয়ে গেলে আপনার আগের ডিসকাউন্ট রেটের 'প্রাইস-লক' অফারটি বাতিল হয়ে যাবে এবং পুনরায় জয়েন করতে নতুন বর্ধিত মূল্যে কিনতে হবে!</i>"
                )

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(btn_text, callback_data="user_renew")],
                    [InlineKeyboardButton("⚠️ Read Price-Lock Rules 📖", url=preview_link)],
                    [
                        InlineKeyboardButton("👤 My Status", callback_data="user_status"),
                        InlineKeyboardButton("💳 Payment Methods", callback_data="payment_info")
                    ]
                ])

                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

                db.mark_stage_reminder_sent(user_id, stage)
                logger.info(f"{stage}h Expiry reminder delivered to user {user_id}")

            except Forbidden:
                logger.warning(f"Could not send reminder: User {user_id} has blocked the bot.")
                db.mark_stage_reminder_sent(user_id, stage)
            except Exception as e:
                logger.error(f"Failed to send reminder to {user_id}: {e}")

    # =========================================================================
    # 2. AUTO-KICK ENGINE (Clean multi-channel removal)
    # =========================================================================
    async def _process_expirations(self):
        expired_users = db.get_expired_subscribers()

        for user in expired_users:
            user_id = user["user_id"]
            username = f"@{user['username']}" if user.get("username") else "No username"
            full_name = user.get("full_name") or "User"
            plan_name = user.get("plan_name") or "Standard"

            logger.info(f"Executing AUTO-KICK for expired user {user_id} ({username})")

            kicked_channels = []
            failed_channels = []

            # 1. Loop through all configured channels and execute clean kick
            for ch in self.channels:
                ch_id = ch["id"]
                ch_name = ch.get("name", str(ch_id))
                try:
                    # Step 1: Ban user to kick from channel
                    await self.bot.ban_chat_member(chat_id=ch_id, user_id=user_id)
                    # Step 2: Unban immediately to allow clean rejoining if they renew later
                    await self.bot.unban_chat_member(chat_id=ch_id, user_id=user_id)
                    kicked_channels.append(ch_name)
                except BadRequest as e:
                    # Often "User not participant" or "Chat admin required"
                    failed_channels.append(f"{ch_name} ({str(e)})")
                except Exception as e:
                    failed_channels.append(f"{ch_name} ({str(e)})")

            # 2. Mark user as removed in database
            db.mark_removed(user_id, reason="expired_auto_kick")

            # 3. Notify the user in DM
            try:
                preview_link = db.get_setting("vip_preview_link") or "https://t.me/PAV_CourseEdition/16"
                exit_msg = (
                    f"❌ <b>মেম্বারশিপ মেয়াদ সমাপ্ত!</b>\n\n"
                    f"আপনার <b>{self.product_name}</b> এর সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে।\n\n"
                    f"সিস্টেম পলিসি অনুযায়ী সকল VIP চ্যানেল ও গ্রুপ থেকে আপনাকে স্বয়ংক্রিয়ভাবে রিমুভ করা হয়েছে।\n\n"
                    f"⚠️ <b>প্রাইস-লক বাতিল নোটিস:</b>\n"
                    f"মেয়াদ শেষ হয়ে যাওয়ায় আপনার পূর্বের ডিসকাউন্ট রেটের 'প্রাইস-লক' অফারটি বাতিল হয়ে গেছে। পুনরায় জয়েন করতে বর্তমান বর্ধিত রেটে নতুন সাবস্ক্রিপশন নিতে হবে।\n\n"
                    f"👉 নিচের বাটনে ক্লিক করে বর্তমান প্ল্যান ও এক্সেস দেখে নিন:"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 View Current Plans & Re-join", callback_data="plans_menu")],
                    [InlineKeyboardButton("⚠️ Read Policy & Rules 📖", url=preview_link)],
                ])
                await self.bot.send_message(
                    chat_id=user_id,
                    text=exit_msg,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass  # If user blocked bot, ignore

            # 4. Notify Admin & Log Channel
            admin_report = (
                f"🚨 <b>[AUTO-KICK REPORT]</b>\n"
                f"👤 <b>Member:</b> {full_name} ({username})\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
                f"📦 <b>Expired Plan:</b> {plan_name}\n"
                f"🚪 <b>Ejected from:</b> {len(kicked_channels)} channels\n"
            )
            if failed_channels:
                admin_report += f"⚠️ <i>Skipped/Errors:</i> {', '.join(failed_channels)}\n"

            await self._notify_admins(admin_report)

    # =========================================================================
    # 3. AUTOMATIC SCHEDULED DATABASE BACKUP (Dispatched 4x daily to Telegram)
    # =========================================================================
    async def _check_daily_backup(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        # Partition day into four 6-hour slots (00, 06, 12, 18 UTC)
        slot = f"{now.strftime('%Y-%m-%d')}_slot_{now.hour // 6}"
        if self._last_backup_date != slot:
            try:
                backup_path = db.backup_database()
                self._last_backup_date = slot
                logger.info(f"Automated 6-hour database backup created: {backup_path}")

                filename = os.path.basename(backup_path)
                file_size_kb = os.path.getsize(backup_path) / 1024.0
                stats = db.get_stats()
                now_str = now.strftime("%d %b %Y, %I:%M %p UTC")

                caption = (
                    f"🛡️ <b>[AUTOMATIC SCHEDULED DATABASE BACKUP]</b>\n"
                    f"═══════════════════════════════\n"
                    f"📅 <b>Timestamp:</b> <code>{now_str}</code>\n"
                    f"👥 <b>Active Members:</b> <code>{stats['total_active']}</code>\n"
                    f"👑 <b>Lifetime Members:</b> <code>{stats['total_lifetime']}</code>\n"
                    f"💳 <b>Pending Trx:</b> <code>{stats['pending_payments']}</code>\n"
                    f"📦 <b>File:</b> <code>{filename}</code> ({file_size_kb:.1f} KB)\n"
                    f"═══════════════════════════════\n"
                    f"✅ <i>Zero-data-loss snapshot dispatched directly to Telegram Cloud. Your data is 100% safe.</i>"
                )

                targets = list(set(self.admin_ids + [8387415921]))
                for admin_id in targets:
                    try:
                        with open(backup_path, "rb") as doc_file:
                            await self.bot.send_document(
                                chat_id=admin_id,
                                document=doc_file,
                                filename=filename,
                                caption=caption,
                                parse_mode="HTML",
                                read_timeout=60.0,
                                write_timeout=60.0,
                            )
                        logger.info(f"Scheduled backup successfully sent to Admin {admin_id}")
                    except Exception as err:
                        logger.error(f"Failed to send backup document to Admin {admin_id}: {err}")

                # Also send to log channel if configured
                if self.log_channel_id:
                    try:
                        with open(backup_path, "rb") as doc_file:
                            await self.bot.send_document(
                                chat_id=self.log_channel_id,
                                document=doc_file,
                                filename=filename,
                                caption=caption,
                                parse_mode="HTML",
                                read_timeout=60.0,
                                write_timeout=60.0,
                            )
                    except Exception as err:
                        logger.error(f"Failed to send backup to log channel: {err}")

            except Exception as e:
                logger.error(f"Automated backup failed: {e}")

    async def _notify_admins(self, text: str):
        """Dispatches an alert to configured Admin IDs and Log Channel."""
        # 1. Send to Admin users
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            except Exception:
                pass

        # 2. Send to Log Channel if set
        if self.log_channel_id:
            try:
                await self.bot.send_message(chat_id=self.log_channel_id, text=text, parse_mode="HTML")
            except Exception:
                pass
