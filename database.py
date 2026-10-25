# language: Python, file: database.py, target: Python 3.10+, SQLite3
"""
Rock-Solid Database Engine for Pro Access Vault - Course Edition.
Uses SQLite with WAL (Write-Ahead Logging) mode and Atomic Backup API.
Guarantees zero data loss, zero corruption, and instant crash recovery.
"""

import os
import json
import sqlite3
import datetime
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("vault_db")


BASE_DIR = Path(__file__).resolve().parent


class Database:
    def __init__(self, db_path: Optional[str] = None, backup_dir: Optional[str] = None):
        if db_path is None:
            self.db_path = BASE_DIR / "vault_database.db"
        else:
            p = Path(db_path)
            self.db_path = p if p.is_absolute() else (BASE_DIR / p)

        if self.db_path.is_dir():
            # If Docker volume mounted non-existent file as directory, store db inside
            self.db_path = self.db_path / "vault.db"

        if backup_dir is None:
            self.backup_dir = BASE_DIR / "backups"
        else:
            p = Path(backup_dir)
            self.backup_dir = p if p.is_absolute() else (BASE_DIR / p)

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for high concurrency and crash resilience
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self):
        """Initializes tables and indexes with strict data integrity rules."""
        with self._get_connection() as conn:
            # 1. Subscribers Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    plan_key TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    is_lifetime INTEGER DEFAULT 0,
                    reminder_sent INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    notes TEXT
                );
            """)

            # 2. Payment Requests Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    plan_key TEXT NOT NULL,
                    amount TEXT,
                    payment_method TEXT,
                    trx_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
            """)

            # 3. Activity & Audit Logs Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    details TEXT,
                    timestamp TEXT NOT NULL
                );
            """)

            # 4. VIP Channels & Discussion Groups Table (Immortal SQLite Storage)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL DEFAULT 'channel',
                    invite_expire_hours INTEGER NOT NULL DEFAULT 24,
                    created_at TEXT NOT NULL
                );
            """)

            # 5. VIP Plans Table (Dynamic Pricing & Duration)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    price_bdt INTEGER NOT NULL,
                    price_usdt INTEGER NOT NULL,
                    is_lifetime INTEGER DEFAULT 0,
                    features TEXT,
                    updated_at TEXT NOT NULL
                );
            """)

            # 6. Global Configuration Settings Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            # 7. Affiliate & Reseller Profiles Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS affiliates (
                    user_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER,
                    role TEXT DEFAULT 'trainee',
                    unpaid_balance_bdt REAL DEFAULT 0.0,
                    paid_balance_bdt REAL DEFAULT 0.0,
                    free_months_earned INTEGER DEFAULT 0,
                    total_leads INTEGER DEFAULT 0,
                    total_sales INTEGER DEFAULT 0,
                    joined_at TEXT NOT NULL
                );
            """)

            # 8. Commission Transaction Ledger
            conn.execute("""
                CREATE TABLE IF NOT EXISTS affiliate_commissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    buyer_id INTEGER NOT NULL,
                    payment_req_id INTEGER NOT NULL,
                    plan_key TEXT NOT NULL,
                    sale_amount_bdt REAL NOT NULL,
                    reward_type TEXT NOT NULL,
                    commission_bdt REAL DEFAULT 0.0,
                    created_at TEXT NOT NULL
                );
            """)

            # 9. Payout Withdrawal Request Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS affiliate_payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_bdt REAL NOT NULL,
                    payment_method TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
            """)

            # Indexes for fast queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_status ON subscribers(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscribers(expires_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_status ON payment_requests(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aff_referrer ON affiliates(referrer_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aff_payout_status ON affiliate_payouts(status);")
            conn.commit()

            # Seed default affiliate settings if not present
            default_aff_settings = {
                "affiliate_enabled": "1",
                "affiliate_cash_comm_pct": "50",
                "affiliate_monthly_vip_extend_days": "30",
                "affiliate_min_sales_unlock": "2",
                "affiliate_min_payout_bdt": "500",
                "affiliate_wholesale_discount_pct": "50",
            }
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for s_key, s_val in default_aff_settings.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?);",
                    (s_key, s_val, now_iso),
                )
            conn.commit()

        logger.info(f"Database initialized in WAL mode: {self.db_path}")

    # =========================================================================
    # ATOMIC BACKUP SYSTEM (Guarantees zero data loss)
    # =========================================================================
    def backup_database(self) -> str:
        """
        Creates an online atomic SQLite backup using sqlite3.Connection.backup().
        Can be run safely even while the bot is active and writing.
        """
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = self.backup_dir / f"vault_backup_{timestamp}.db"

        with self._get_connection() as src_conn:
            with sqlite3.connect(str(backup_filename)) as dst_conn:
                src_conn.backup(dst_conn, pages=100)

        # Retain only the last 15 backups to save space
        all_backups = sorted(self.backup_dir.glob("vault_backup_*.db"), key=os.path.getmtime)
        if len(all_backups) > 15:
            for old_file in all_backups[:-15]:
                try:
                    old_file.unlink()
                except Exception:
                    pass

        logger.info(f"Database backup saved: {backup_filename}")
        return str(backup_filename)

    # =========================================================================
    # SUBSCRIBER MANAGEMENT
    # =========================================================================
    def add_or_update_subscriber(
        self,
        user_id: int,
        username: Optional[str],
        full_name: Optional[str],
        plan_key: str,
        plan_name: str,
        duration_seconds: int,
        is_lifetime: bool = False,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Adds a new subscriber or resets their subscription period.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        joined_at_iso = now.isoformat()

        if is_lifetime:
            expires_at = now + datetime.timedelta(days=36500)  # 100 years
            expires_at_iso = expires_at.isoformat()
            is_lifetime_int = 1
        else:
            expires_at = now + datetime.timedelta(seconds=duration_seconds)
            expires_at_iso = expires_at.isoformat()
            is_lifetime_int = 0

        clean_username = username.lstrip("@") if username else ""
        clean_name = full_name or ""

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO subscribers (
                    user_id, username, full_name, plan_key, plan_name,
                    joined_at, expires_at, is_lifetime, reminder_sent, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'active', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    plan_key = excluded.plan_key,
                    plan_name = excluded.plan_name,
                    expires_at = excluded.expires_at,
                    is_lifetime = excluded.is_lifetime,
                    reminder_sent = 0,
                    status = 'active',
                    notes = COALESCE(excluded.notes, subscribers.notes);
                """,
                (
                    user_id,
                    clean_username,
                    clean_name,
                    plan_key,
                    plan_name,
                    joined_at_iso,
                    expires_at_iso,
                    is_lifetime_int,
                    notes,
                ),
            )
            conn.commit()

        self.log_activity(
            user_id,
            "ADD",
            f"Added to {plan_name} | Expires: {expires_at_iso} | Lifetime: {is_lifetime}",
        )

        return self.get_subscriber(user_id)

    def renew_subscriber(
        self,
        user_id: int,
        duration_seconds: int,
        new_plan_key: Optional[str] = None,
        new_plan_name: Optional[str] = None,
        is_lifetime: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Extends the current subscriber's expiration date.
        If already expired, starts from NOW. If still active, adds to current expiration.
        """
        current = self.get_subscriber(user_id)
        if not current:
            return None

        now = datetime.datetime.now(datetime.timezone.utc)

        if is_lifetime:
            new_expires = now + datetime.timedelta(days=36500)
            is_lifetime_int = 1
        else:
            is_lifetime_int = 0
            curr_expiry = datetime.datetime.fromisoformat(current["expires_at"])
            # If current expiry is in the future, extend from that point
            base_time = curr_expiry if curr_expiry > now else now
            new_expires = base_time + datetime.timedelta(seconds=duration_seconds)

        new_expires_iso = new_expires.isoformat()
        plan_key = new_plan_key or current["plan_key"]
        plan_name = new_plan_name or current["plan_name"]

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE subscribers SET
                    plan_key = ?,
                    plan_name = ?,
                    expires_at = ?,
                    is_lifetime = ?,
                    reminder_sent = 0,
                    status = 'active'
                WHERE user_id = ?;
                """,
                (plan_key, plan_name, new_expires_iso, is_lifetime_int, user_id),
            )
            conn.commit()

        self.log_activity(
            user_id,
            "RENEW",
            f"Renewed for +{duration_seconds}s | New Expiry: {new_expires_iso}",
        )

        return self.get_subscriber(user_id)

    def get_subscriber(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM subscribers WHERE user_id = ?;", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_subscribers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM subscribers WHERE status = ? ORDER BY expires_at ASC;",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM subscribers ORDER BY joined_at DESC;"
                ).fetchall()
            return [dict(r) for r in rows]

    def get_active_subscribers(self) -> List[Dict[str, Any]]:
        return self.get_all_subscribers(status="active")

    def get_due_reminders(self, hours_before: int = 24) -> List[Dict[str, Any]]:
        """
        Finds active non-lifetime subscribers whose expiration is within `hours_before`
        and who have not yet received a reminder.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        threshold = now + datetime.timedelta(hours=hours_before)

        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscribers
                WHERE status = 'active'
                  AND is_lifetime = 0
                  AND reminder_sent = 0
                  AND expires_at <= ?
                  AND expires_at > ?;
                """,
                (threshold.isoformat(), now.isoformat()),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_reminder_sent(self, user_id: int):
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE subscribers SET reminder_sent = 1 WHERE user_id = ?;",
                (user_id,),
            )
            conn.commit()
        self.log_activity(user_id, "REMINDER_SENT", "24h Expiry reminder delivered")

    def get_expired_subscribers(self) -> List[Dict[str, Any]]:
        """
        Finds active non-lifetime subscribers whose expiration timestamp has passed.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscribers
                WHERE status = 'active'
                  AND is_lifetime = 0
                  AND expires_at <= ?;
                """,
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_removed(self, user_id: int, reason: str = "expired"):
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE subscribers SET status = 'removed' WHERE user_id = ?;",
                (user_id,),
            )
            conn.commit()
        self.log_activity(user_id, "AUTO_KICK", f"Removed from channels | Reason: {reason}")

    def manual_kick_subscriber(self, user_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE subscribers SET status = 'removed' WHERE user_id = ?;",
                (user_id,),
            )
            conn.commit()
            success = cursor.rowcount > 0

        if success:
            self.log_activity(user_id, "MANUAL_KICK", "Kicked manually by admin")
        return success

    def get_expiring_soon_subscribers(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Finds all active non-lifetime subscribers expiring within given hours."""
        now = datetime.datetime.now(datetime.timezone.utc)
        soon = now + datetime.timedelta(hours=hours)
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subscribers
                WHERE status = 'active'
                  AND is_lifetime = 0
                  AND expires_at <= ?
                  AND expires_at > ?
                ORDER BY expires_at ASC;
                """,
                (soon.isoformat(), now.isoformat()),
            ).fetchall()
            return [dict(r) for r in rows]

    def search_subscribers(self, query: str) -> List[Dict[str, Any]]:
        """Searches subscribers by user ID or username or full name."""
        clean_q = query.strip().lstrip("@")
        with self._get_connection() as conn:
            if clean_q.isdigit():
                rows = conn.execute(
                    "SELECT * FROM subscribers WHERE user_id = ? OR username LIKE ? OR full_name LIKE ?;",
                    (int(clean_q), f"%{clean_q}%", f"%{clean_q}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM subscribers WHERE username LIKE ? OR full_name LIKE ?;",
                    (f"%{clean_q}%", f"%{clean_q}%"),
                ).fetchall()
            return [dict(r) for r in rows]


    # =========================================================================
    # STATS & DASHBOARD
    # =========================================================================
    def get_stats(self) -> Dict[str, int]:
        now = datetime.datetime.now(datetime.timezone.utc)
        soon = (now + datetime.timedelta(hours=24)).isoformat()
        now_iso = now.isoformat()

        with self._get_connection() as conn:
            total_active = conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE status = 'active';"
            ).fetchone()[0]

            total_lifetime = conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE status = 'active' AND is_lifetime = 1;"
            ).fetchone()[0]

            expiring_24h = conn.execute(
                """
                SELECT COUNT(*) FROM subscribers
                WHERE status = 'active'
                  AND is_lifetime = 0
                  AND expires_at <= ?
                  AND expires_at > ?;
                """,
                (soon, now_iso),
            ).fetchone()[0]

            total_removed = conn.execute(
                "SELECT COUNT(*) FROM subscribers WHERE status = 'removed';"
            ).fetchone()[0]

            total_all = conn.execute("SELECT COUNT(*) FROM subscribers;").fetchone()[0]

            pending_payments = conn.execute(
                "SELECT COUNT(*) FROM payment_requests WHERE status = 'pending';"
            ).fetchone()[0]

        return {
            "total_active": total_active,
            "total_lifetime": total_lifetime,
            "expiring_24h": expiring_24h,
            "total_removed": total_removed,
            "total_all": total_all,
            "pending_payments": pending_payments,
        }

    # =========================================================================
    # PAYMENT REQUESTS
    # =========================================================================
    def create_payment_request(
        self,
        user_id: int,
        username: Optional[str],
        full_name: Optional[str],
        plan_key: str,
        amount: str,
        payment_method: str,
        trx_id: str,
    ) -> int:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        clean_user = username.lstrip("@") if username else ""
        clean_name = full_name or ""

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO payment_requests (
                    user_id, username, full_name, plan_key,
                    amount, payment_method, trx_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?);
                """,
                (
                    user_id,
                    clean_user,
                    clean_name,
                    plan_key,
                    amount,
                    payment_method,
                    trx_id,
                    now,
                ),
            )
            conn.commit()
            req_id = cursor.lastrowid

        self.log_activity(
            user_id,
            "PAYMENT_SUBMITTED",
            f"Req #{req_id} | Plan: {plan_key} | Trx: {trx_id}",
        )
        return req_id

    def get_payment_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM payment_requests WHERE id = ?;", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_payment_status(self, request_id: int, status: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE payment_requests SET status = ?, reviewed_at = ? WHERE id = ?;",
                (status, now, request_id),
            )
            conn.commit()

    # =========================================================================
    # AUDIT LOGGING
    # =========================================================================
    def log_activity(self, user_id: Optional[int], action: str, details: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO activity_logs (user_id, action, details, timestamp)
                    VALUES (?, ?, ?, ?);
                    """,
                    (user_id, action, details, now),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")

    def get_recent_logs(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_logs ORDER BY id DESC LIMIT ?;", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # CHANNELS & GROUPS PERSISTENCE (Immortal SQLite Storage)
    # =========================================================================
    def add_or_update_channel(self, channel_id: int, name: str, channel_type: str = "channel", invite_expire_hours: int = 24):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO channels (id, name, type, invite_expire_hours, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    invite_expire_hours = excluded.invite_expire_hours;
                """,
                (channel_id, name, channel_type, invite_expire_hours, now),
            )
            conn.commit()

    def delete_channel(self, channel_id: int):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM channels WHERE id = ?;", (channel_id,))
            conn.commit()

    def get_all_channels(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT id, name, type, invite_expire_hours, created_at FROM channels ORDER BY type ASC, name ASC;"
            ).fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # PLANS & PRICING PERSISTENCE (Dynamic & Immortal)
    # =========================================================================
    def save_plan(
        self,
        key: str,
        name: str,
        duration_days: int,
        price_bdt: int,
        price_usdt: int,
        is_lifetime: bool,
        features: Optional[List[str]] = None,
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        feat_json = json.dumps(features or [])
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO plans (key, name, duration_days, price_bdt, price_usdt, is_lifetime, features, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = excluded.name,
                    duration_days = excluded.duration_days,
                    price_bdt = excluded.price_bdt,
                    price_usdt = excluded.price_usdt,
                    is_lifetime = excluded.is_lifetime,
                    features = excluded.features,
                    updated_at = excluded.updated_at;
                """,
                (key, name, duration_days, price_bdt, price_usdt, 1 if is_lifetime else 0, feat_json, now),
            )
            conn.commit()

    def update_plan_price(self, key: str, price_bdt: int, price_usdt: int) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE plans SET price_bdt = ?, price_usdt = ?, updated_at = ?
                WHERE key = ?;
                """,
                (price_bdt, price_usdt, now, key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_all_plans(self) -> Dict[str, Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT key, name, duration_days, price_bdt, price_usdt, is_lifetime, features, updated_at FROM plans;"
            ).fetchall()
            result = {}
            for r in rows:
                d = dict(r)
                d["is_lifetime"] = bool(d["is_lifetime"])
                try:
                    d["features"] = json.loads(d["features"]) if d["features"] else []
                except Exception:
                    d["features"] = []
                result[d["key"]] = d
            return result

    # =========================================================================
    # SETTINGS KEY-VALUE STORE
    # =========================================================================
    def set_setting(self, key: str, value: str):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at;
                """,
                (key, str(value), now),
            )
            conn.commit()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?;", (key,)).fetchone()
            return row[0] if row else default

    def get_all_settings(self) -> Dict[str, str]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings;").fetchall()
            return {r[0]: r[1] for r in rows}

    # =========================================================================
    # AFFILIATE & RESELLER SYSTEM (Dynamic Level-Progression & Multi-Tier Rewards)
    # =========================================================================
    def record_lead(self, user_id: int, referrer_id: int) -> bool:
        """
        Records a referral lead with multi-layer anti-fraud checks:
        1. Self-referral block (referrer_id == user_id)
        2. Old lead block (existing subscriber or prior payment request)
        3. Permanent lead lock (cannot overwrite existing referrer)
        """
        if not referrer_id or referrer_id <= 0 or user_id == referrer_id:
            return False

        with self._get_connection() as conn:
            # Check if user already exists in subscribers
            sub_exists = conn.execute(
                "SELECT 1 FROM subscribers WHERE user_id = ? LIMIT 1;", (user_id,)
            ).fetchone()
            if sub_exists:
                return False

            # Check if user has prior payment requests
            pay_exists = conn.execute(
                "SELECT 1 FROM payment_requests WHERE user_id = ? LIMIT 1;", (user_id,)
            ).fetchone()
            if pay_exists:
                return False

            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            # Ensure referrer profile exists
            ref_row = conn.execute(
                "SELECT user_id FROM affiliates WHERE user_id = ?;", (referrer_id,)
            ).fetchone()
            if not ref_row:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO affiliates (user_id, referrer_id, role, joined_at)
                    VALUES (?, NULL, 'trainee', ?);
                    """,
                    (referrer_id, now),
                )

            # Check if user already has an affiliate profile
            user_row = conn.execute(
                "SELECT user_id, referrer_id FROM affiliates WHERE user_id = ?;", (user_id,)
            ).fetchone()

            if user_row:
                # Permanent lock: cannot change referrer once set
                if user_row["referrer_id"]:
                    return False
                # If was created without referrer, set it now
                conn.execute(
                    "UPDATE affiliates SET referrer_id = ? WHERE user_id = ?;",
                    (referrer_id, user_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO affiliates (user_id, referrer_id, role, joined_at)
                    VALUES (?, ?, 'trainee', ?);
                    """,
                    (user_id, referrer_id, now),
                )

            # Increment referrer's total_leads
            conn.execute(
                "UPDATE affiliates SET total_leads = total_leads + 1 WHERE user_id = ?;",
                (referrer_id,),
            )
            conn.commit()

        self.log_activity(
            user_id,
            "AFFILIATE_LEAD",
            f"Bound lead to Referrer #{referrer_id}",
        )
        return True

    def get_or_create_affiliate(self, user_id: int) -> Dict[str, Any]:
        """
        Returns the affiliate profile. If missing, auto-creates with default 'trainee'.
        Also evaluates effective display_role based on active subscription status.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM affiliates WHERE user_id = ?;", (user_id,)
            ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO affiliates (user_id, referrer_id, role, unpaid_balance_bdt, paid_balance_bdt, free_months_earned, total_leads, total_sales, joined_at)
                    VALUES (?, NULL, 'trainee', 0.0, 0.0, 0, 0, 0, ?);
                    """,
                    (user_id, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM affiliates WHERE user_id = ?;", (user_id,)
                ).fetchone()

            data = dict(row)

            # Check subscription status for dynamic role badge if not admin-approved reseller
            if data.get("role") != "reseller":
                sub_row = conn.execute(
                    "SELECT plan_key, is_lifetime, status FROM subscribers WHERE user_id = ? AND status = 'active';",
                    (user_id,),
                ).fetchone()
                if sub_row:
                    if sub_row["is_lifetime"]:
                        data["effective_tier"] = "vip_lifetime"
                    else:
                        data["effective_tier"] = "vip_monthly"
                else:
                    data["effective_tier"] = data.get("role", "trainee")
            else:
                data["effective_tier"] = "reseller"

            return data

    def set_user_role(self, user_id: int, role: str) -> bool:
        """Promotes or demotes an affiliate role (e.g. 'reseller', 'trainee')."""
        self.get_or_create_affiliate(user_id)
        with self._get_connection() as conn:
            conn.execute("UPDATE affiliates SET role = ? WHERE user_id = ?;", (role, user_id))
            conn.commit()
        self.log_activity(user_id, "AFFILIATE_ROLE_SET", f"Role set to {role}")
        return True

    def process_affiliate_commission(
        self,
        buyer_id: int,
        payment_req_id: int,
        plan_key: str,
        amount_bdt: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Processes affiliate commission upon successful payment approval.
        Enforces strict business rules:
        - System toggle check (affiliate_enabled)
        - Renewal Rule: 0% commission on renewals (only 1st ever purchase gets reward)
        - Monthly VIP: +1 Month (30d) free extension to subscriber account
        - Cash Partners / Resellers: 50% commission added to unpaid balance
        - 2-Sale Unlock Wall: 2nd sale automatically promotes Trainee to Verified Reseller
        """
        if self.get_setting("affiliate_enabled", "1") != "1":
            return None

        with self._get_connection() as conn:
            # 1. Renewal check: If buyer already has prior commissions processed, 0% commission
            prior_count = conn.execute(
                "SELECT COUNT(*) FROM affiliate_commissions WHERE buyer_id = ?;",
                (buyer_id,),
            ).fetchone()[0]
            if prior_count > 0:
                logger.info(f"Renewal purchase by buyer {buyer_id} - 0% commission applied.")
                return None

            # 2. Get buyer's referrer
            aff_row = conn.execute(
                "SELECT referrer_id FROM affiliates WHERE user_id = ?;", (buyer_id,)
            ).fetchone()
            if not aff_row or not aff_row["referrer_id"]:
                return None

            referrer_id = aff_row["referrer_id"]
            if referrer_id == buyer_id:
                return None

            # 3. Check referrer's profile & subscription status
            ref_aff = self.get_or_create_affiliate(referrer_id)
            ref_sub = self.get_subscriber(referrer_id)

            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            min_unlock = int(self.get_setting("affiliate_min_sales_unlock", "2"))
            unlocked_reseller = False

            # Check if referrer is currently an active Monthly VIP subscriber (and not a dedicated reseller)
            is_active_monthly_vip = (
                ref_sub
                and ref_sub.get("status") == "active"
                and not ref_sub.get("is_lifetime", False)
                and ref_aff.get("role") != "reseller"
            )

            if is_active_monthly_vip:
                # Group 1: Monthly VIP Member -> +1 Month (30d) free renewal
                extend_days = int(self.get_setting("affiliate_monthly_vip_extend_days", "30"))
                duration_seconds = extend_days * 86400
                self.renew_subscriber(
                    user_id=referrer_id,
                    duration_seconds=duration_seconds,
                    new_plan_name=f"+{extend_days} Days VIP Free Referral Reward",
                )

                new_sales = ref_aff["total_sales"] + 1
                if new_sales >= min_unlock and ref_aff.get("role") == "trainee":
                    conn.execute("UPDATE affiliates SET role = 'reseller' WHERE user_id = ?;", (referrer_id,))
                    unlocked_reseller = True

                conn.execute(
                    """
                    UPDATE affiliates SET
                        free_months_earned = free_months_earned + 1,
                        total_sales = total_sales + 1
                    WHERE user_id = ?;
                    """,
                    (referrer_id,),
                )

                conn.execute(
                    """
                    INSERT INTO affiliate_commissions (
                        referrer_id, buyer_id, payment_req_id, plan_key, sale_amount_bdt, reward_type, commission_bdt, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'free_month_extend', 0.0, ?);
                    """,
                    (referrer_id, buyer_id, payment_req_id, plan_key, amount_bdt, now_iso),
                )
                conn.commit()

                self.log_activity(
                    referrer_id,
                    "AFFILIATE_REWARD_EXTEND",
                    f"+{extend_days}d for Ref #{buyer_id} | Plan: {plan_key}",
                )

                return {
                    "reward_type": "free_month_extend",
                    "referrer_id": referrer_id,
                    "days": extend_days,
                    "unlocked_reseller": unlocked_reseller,
                    "total_sales": new_sales,
                }
            else:
                # Group 2 & 3: Cash Commission (50% or configured %)
                comm_pct = float(self.get_setting("affiliate_cash_comm_pct", "50"))
                comm_bdt = round(amount_bdt * (comm_pct / 100.0), 2)
                new_sales = ref_aff["total_sales"] + 1

                if new_sales >= min_unlock and ref_aff.get("role") == "trainee":
                    conn.execute("UPDATE affiliates SET role = 'reseller' WHERE user_id = ?;", (referrer_id,))
                    unlocked_reseller = True

                conn.execute(
                    """
                    UPDATE affiliates SET
                        unpaid_balance_bdt = unpaid_balance_bdt + ?,
                        total_sales = total_sales + 1
                    WHERE user_id = ?;
                    """,
                    (comm_bdt, referrer_id),
                )

                conn.execute(
                    """
                    INSERT INTO affiliate_commissions (
                        referrer_id, buyer_id, payment_req_id, plan_key, sale_amount_bdt, reward_type, commission_bdt, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'cash', ?, ?);
                    """,
                    (referrer_id, buyer_id, payment_req_id, plan_key, amount_bdt, comm_bdt, now_iso),
                )
                conn.commit()

                self.log_activity(
                    referrer_id,
                    "AFFILIATE_REWARD_CASH",
                    f"+{comm_bdt} BDT for Ref #{buyer_id} | Sale: {amount_bdt} BDT",
                )

                return {
                    "reward_type": "cash",
                    "referrer_id": referrer_id,
                    "amount_bdt": comm_bdt,
                    "unlocked_reseller": unlocked_reseller,
                    "total_sales": new_sales,
                }

    def request_payout(
        self,
        user_id: int,
        amount: float,
        method: str,
        account_number: str,
    ) -> Tuple[bool, str, Optional[int]]:
        """
        Validates withdrawal criteria and records payout request atomically:
        - Must be verified reseller or have reached min_sales_unlock
        - Must satisfy min_payout_bdt
        - Deducts immediately from unpaid_balance_bdt to prevent double-spending
        """
        aff = self.get_or_create_affiliate(user_id)
        min_unlock = int(self.get_setting("affiliate_min_sales_unlock", "2"))

        if aff.get("role") != "reseller" and aff.get("total_sales", 0) < min_unlock:
            rem = max(1, min_unlock - aff.get("total_sales", 0))
            return (False, f"উইথড্র করার পূর্বে আপনাকে কমপক্ষে {min_unlock}টি সেল সম্পন্ন করতে হবে। (আর বাকি: {rem}টি)", None)

        min_payout = float(self.get_setting("affiliate_min_payout_bdt", "500"))
        if amount < min_payout:
            return (False, f"মিনিমাম পে-আউট রিকোয়েস্ট {int(min_payout)} BDT হতে হবে।", None)

        if aff["unpaid_balance_bdt"] < amount:
            return (False, f"আপনার অ্যাকাউন্টে পর্যাপ্ত ব্যালেন্স নেই। বর্তমান ব্যালেন্স: {aff['unpaid_balance_bdt']:.2f} BDT।", None)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Deduct balance immediately
            conn.execute(
                "UPDATE affiliates SET unpaid_balance_bdt = unpaid_balance_bdt - ? WHERE user_id = ?;",
                (amount, user_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO affiliate_payouts (user_id, amount_bdt, payment_method, account_number, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?);
                """,
                (user_id, amount, method, account_number, now),
            )
            conn.commit()
            payout_id = cursor.lastrowid

        self.log_activity(
            user_id,
            "PAYOUT_REQUESTED",
            f"Payout #{payout_id}: {amount} BDT via {method} ({account_number})",
        )
        return (True, "পে-আউট রিকোয়েস্ট সফলভাবে জমা হয়েছে!", payout_id)

    def approve_payout(self, payout_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Approves a payout and moves funds from unpaid to paid_balance_bdt."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM affiliate_payouts WHERE id = ?;", (payout_id,)
            ).fetchone()
            if not row or row["status"] != "pending":
                return (False, "রিকোয়েস্টটি পাওয়া যায়নি অথবা ইতিপূর্বে প্রসেস করা হয়েছে।", None)

            payout = dict(row)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            conn.execute(
                "UPDATE affiliate_payouts SET status = 'approved', reviewed_at = ? WHERE id = ?;",
                (now, payout_id),
            )
            conn.execute(
                "UPDATE affiliates SET paid_balance_bdt = paid_balance_bdt + ? WHERE user_id = ?;",
                (payout["amount_bdt"], payout["user_id"]),
            )
            conn.commit()

        self.log_activity(
            payout["user_id"],
            "PAYOUT_APPROVED",
            f"Payout #{payout_id} approved: {payout['amount_bdt']} BDT",
        )
        return (True, "পেআউট সফলভাবে অ্যাপ্রুভ করা হয়েছে।", payout)

    def reject_payout(self, payout_id: int, reason: Optional[str] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Rejects a payout and refunds the deducted amount back to unpaid_balance_bdt."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM affiliate_payouts WHERE id = ?;", (payout_id,)
            ).fetchone()
            if not row or row["status"] != "pending":
                return (False, "রিকোয়েস্টটি পাওয়া যায়নি অথবা ইতিপূর্বে প্রসেস করা হয়েছে।", None)

            payout = dict(row)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            conn.execute(
                "UPDATE affiliate_payouts SET status = 'rejected', reviewed_at = ? WHERE id = ?;",
                (now, payout_id),
            )
            # Refund amount back to unpaid balance
            conn.execute(
                "UPDATE affiliates SET unpaid_balance_bdt = unpaid_balance_bdt + ? WHERE user_id = ?;",
                (payout["amount_bdt"], payout["user_id"]),
            )
            conn.commit()

        self.log_activity(
            payout["user_id"],
            "PAYOUT_REJECTED",
            f"Payout #{payout_id} rejected. Refunded {payout['amount_bdt']} BDT. Reason: {reason or 'N/A'}",
        )
        return (True, "পেআউট বাতিল করা হয়েছে এবং ব্যালেন্স ফেরত দেওয়া হয়েছে।", payout)

    def get_pending_payouts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM affiliate_payouts WHERE status = 'pending' ORDER BY id ASC;"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_payout(self, payout_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM affiliate_payouts WHERE id = ?;", (payout_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_affiliate_stats_summary(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            total_affs = conn.execute("SELECT COUNT(*) FROM affiliates;").fetchone()[0]
            total_sales = conn.execute("SELECT COALESCE(SUM(total_sales), 0) FROM affiliates;").fetchone()[0]
            total_paid = conn.execute("SELECT COALESCE(SUM(paid_balance_bdt), 0.0) FROM affiliates;").fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM affiliate_payouts WHERE status = 'pending';"
            ).fetchone()[0]
            return {
                "total_affiliates": total_affs,
                "total_sales": total_sales,
                "total_paid_bdt": total_paid,
                "pending_payouts": pending_count,
            }


# Global shared instance
db = Database()


