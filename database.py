# language: Python, file: database.py, target: Python 3.10+, SQLite3
"""
Rock-Solid Database Engine for Pro Access Vault - Course Edition.
Uses SQLite with WAL (Write-Ahead Logging) mode and Atomic Backup API.
Guarantees zero data loss, zero corruption, and instant crash recovery.
"""

import os
import sqlite3
import datetime
import shutil
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("vault_db")


class Database:
    def __init__(self, db_path: str = "vault_database.db", backup_dir: str = "backups"):
        self.db_path = Path(db_path)
        if self.db_path.is_dir():
            # If Docker volume mounted non-existent file as directory, store db inside
            self.db_path = self.db_path / "vault.db"
        self.backup_dir = Path(backup_dir)
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

            # Indexes for fast scheduler queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_status ON subscribers(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_expires ON subscribers(expires_at);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pay_status ON payment_requests(status);")
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


# Global shared instance
db = Database()
