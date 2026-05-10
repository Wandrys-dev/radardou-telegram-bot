"""SQLite local: chat_id -> chave da API + bookkeeping de alertas proativos."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class UserStorage:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    api_key TEXT NOT NULL,
                    notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    last_alert_check TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migrações idempotentes p/ usuários antigos
            cur = conn.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in cur.fetchall()}
            if "notifications_enabled" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN notifications_enabled INTEGER NOT NULL DEFAULT 1")
            if "last_alert_check" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN last_alert_check TEXT")

            # Dedup de alertas: cada (chat_id, alert_id, publicacao_id) so eh
            # notificada uma unica vez. Mesmo padrao da AlertNotification da
            # app web (Prisma), pra consistencia entre canais email e Telegram.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_notifications (
                    chat_id INTEGER NOT NULL,
                    alert_id INTEGER NOT NULL,
                    publicacao_id TEXT NOT NULL,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, alert_id, publicacao_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_notif_notified_at "
                "ON alert_notifications(notified_at)"
            )

    # ---------- chave ----------

    def set_api_key(self, chat_id: int, api_key: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO users (chat_id, api_key) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "api_key = excluded.api_key, updated_at = CURRENT_TIMESTAMP",
                (chat_id, api_key),
            )

    def get_api_key(self, chat_id: int) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT api_key FROM users WHERE chat_id = ?", (chat_id,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def delete_user(self, chat_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))

    def count_users(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]

    # ---------- alertas proativos ----------

    def list_users_for_alerts(self) -> list[tuple[int, str, Optional[str]]]:
        """Retorna (chat_id, api_key, last_alert_check) de usuários com notificações ON."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT chat_id, api_key, last_alert_check FROM users "
                "WHERE notifications_enabled = 1"
            )
            return cur.fetchall()

    def set_last_alert_check(self, chat_id: int, when_iso: Optional[str] = None):
        ts = when_iso or datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET last_alert_check = ? WHERE chat_id = ?",
                (ts, chat_id),
            )

    def set_notifications(self, chat_id: int, enabled: bool):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET notifications_enabled = ? WHERE chat_id = ?",
                (1 if enabled else 0, chat_id),
            )

    def get_notifications(self, chat_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT notifications_enabled FROM users WHERE chat_id = ?",
                (chat_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else False

    # ---------- dedup de notificacoes de alerta ----------

    def filter_unnotified_pubs(
        self, chat_id: int, alert_id: int, pub_ids: list[str]
    ) -> set[str]:
        """Retorna o subset de pub_ids que ainda NAO foi notificado pra esse
        chat_id+alert_id. Usado em batch pra evitar N queries."""
        if not pub_ids:
            return set()
        ids_str = [str(p) for p in pub_ids]
        placeholders = ",".join("?" * len(ids_str))
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT publicacao_id FROM alert_notifications "
                f"WHERE chat_id = ? AND alert_id = ? "
                f"AND publicacao_id IN ({placeholders})",
                (chat_id, alert_id, *ids_str),
            )
            already = {row[0] for row in cur.fetchall()}
        return {pid for pid in ids_str if pid not in already}

    def mark_notified(self, chat_id: int, alert_id: int, pub_ids: list[str]):
        """Registra que essas pubs foram notificadas pra esse alerta. INSERT
        OR IGNORE protege contra race condition se o cron rodar paralelo."""
        if not pub_ids:
            return
        rows = [(chat_id, alert_id, str(p)) for p in pub_ids]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO alert_notifications "
                "(chat_id, alert_id, publicacao_id) VALUES (?, ?, ?)",
                rows,
            )

    def cleanup_old_notifications(self, days: int = 90) -> int:
        """Remove registros de dedup com mais de N dias. Retorna quantos."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM alert_notifications "
                "WHERE notified_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            return cur.rowcount
