# language: Python, file: scheduler.py, target: Python 3.10+, python-telegram-bot v21+
"""
Asynchronous Background Scheduler & Auto-Kick Engine.
Monitors subscriber lifecycles 24/7:
- Dispatches 24h expiration reminders.
- Executes clean, instant auto-kicks (ban + unban) across all VIP channels/groups when expired.
- Performs automated daily atomic database backups.
"""

import os
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
    # 1. EXPIRATION REMINDERS (24h Alert)
    # =========================================================================
    async def _process_reminders(self):
        reminder_hours = self.config.get("reminder_hours_before", 24)
        due_users = db.get_due_reminders(hours_before=reminder_hours)

        for user in due_users:
            user_id = user["user_id"]
            try:
                expiry_dt = datetime.datetime.fromisoformat(user["expires_at"])
                formatted_expiry = expiry_dt.strftime("%d %b %Y, %I:%M %p")

                text = (
                    f"⚠️ <b>সাবস্ক্রিপশন মেয়াদ শেষের নোটিশ!</b>\n\n"
                    f"আপনার <b>{self.product_name}</b> মেম্বারশিপের মেয়াদ আর <b>২৪ ঘণ্টার</b> মধ্যে শেষ হবে।\n\n"
                    f"📦 <b>প্ল্যান:</b> {user['plan_name']}\n"
                    f"⏳ <b>মেয়াদ শেষ:</b> <code>{formatted_expiry}</code>\n\n"
                    f"🚨 <i>নির্ধারিত সময়ে রিনিউ না করলে সবকয়টি VIP চ্যানেল ও গ্রুপ থেকে স্বয়ংক্রিয়ভাবে রিমুভ করা হবে। নিরবচ্ছিন্ন এক্সেস রাখতে এখনই রিনিউ করুন।</i>"
                )

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Renew Subscription", callback_data="user_renew")],
                    [InlineKeyboardButton("👤 My Status", callback_data="user_status")]
                ])

                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

                db.mark_reminder_sent(user_id)
                logger.info(f"24h Expiry reminder delivered to user {user_id}")

            except Forbidden:
                # User blocked the bot
                logger.warning(f"Could not send reminder: User {user_id} has blocked the bot.")
                db.mark_reminder_sent(user_id)
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
                exit_msg = (
                    f"❌ <b>মেম্বারশিপ মেয়াদ সমাপ্ত!</b>\n\n"
                    f"আপনার <b>{self.product_name}</b> এর সাবস্ক্রিপশনের মেয়াদ শেষ হয়েছে।\n\n"
                    f"সিস্টেম পলিসি অনুযায়ী সকল VIP চ্যানেল ও গ্রুপ থেকে আপনাকে স্বয়ংক্রিয়ভাবে রিমুভ করা হয়েছে।\n\n"
                    f"পুনরায় নতুন করে এক্সেস পেতে নিচের বাটনে ক্লিক করে রিনিউ সম্পন্ন করুন।"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 Get New Access / Renew", callback_data="plans_menu")]
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
    # 3. AUTOMATIC DAILY ATOMIC DATABASE BACKUP
    # =========================================================================
    async def _check_daily_backup(self):
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        if self._last_backup_date != today:
            try:
                backup_path = db.backup_database()
                self._last_backup_date = today
                logger.info(f"Automated daily backup completed: {backup_path}")

                filename = os.path.basename(backup_path)
                stats = db.get_stats()
                now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")

                caption = (
                    f"🛡️ <b>[AUTOMATIC DAILY DATABASE BACKUP]</b>\n"
                    f"═══════════════════════════════\n"
                    f"📅 <b>Timestamp:</b> <code>{now_str}</code>\n"
                    f"👥 <b>Active Members:</b> {stats['total_active']}\n"
                    f"👑 <b>Lifetime Members:</b> {stats['total_lifetime']}\n"
                    f"💳 <b>Pending Trx:</b> {stats['pending_payments']}\n"
                    f"═══════════════════════════════\n"
                    f"<i>Automated daily database snapshot. Keep this file safe.</i>"
                )

                # Send backup document directly to all Admin Telegram IDs
                for admin_id in self.admin_ids:
                    try:
                        with open(backup_path, "rb") as doc_file:
                            await self.bot.send_document(
                                chat_id=admin_id,
                                document=doc_file,
                                filename=filename,
                                caption=caption,
                                parse_mode="HTML",
                            )
                        logger.info(f"Daily backup document dispatched to Admin ID {admin_id}")
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
                            )
                    except Exception as err:
                        logger.error(f"Failed to send backup document to log channel: {err}")

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
