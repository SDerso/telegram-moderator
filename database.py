import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def init(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            default_mute TEXT NOT NULL DEFAULT '10m',
            default_warn TEXT NOT NULL DEFAULT '7d',
            max_warnings INTEGER NOT NULL DEFAULT 3,
            log_chat_id TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS punishments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            type TEXT NOT NULL,
            reason TEXT,
            expires_at INTEGER,
            admin_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE INDEX IF NOT EXISTS idx_punishments_active
        ON punishments(chat_id, status, expires_at);

        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_id INTEGER,
            reason TEXT,
            duration TEXT,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );

        CREATE INDEX IF NOT EXISTS idx_logs_chat
        ON action_logs(chat_id, created_at DESC);
        """)
        self.conn.commit()

    def settings(self, chat_id):
        row = self.conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            return dict(row)
        self.conn.execute("INSERT INTO settings(chat_id) VALUES(?)", (chat_id,))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone())

    def get_setting(self, chat_id, key, default=None):
        return self.settings(chat_id).get(key, default)

    def set_setting(self, chat_id, key, value):
        allowed = {"default_mute", "default_warn", "max_warnings", "log_chat_id"}
        if key not in allowed:
            raise ValueError("Invalid setting")
        self.settings(chat_id)
        self.conn.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, chat_id))
        self.conn.commit()

    def add_punishment(self, chat_id, user_id, username, first_name, kind, reason, expires_at, admin_id):
        cur = self.conn.execute("""
            INSERT INTO punishments(chat_id,user_id,username,first_name,type,reason,expires_at,admin_id)
            VALUES(?,?,?,?,?,?,?,?)
        """, (chat_id,user_id,username,first_name,kind,reason,expires_at,admin_id))
        self.conn.commit()
        return self.get_punishment(cur.lastrowid)

    def get_punishment(self, pid):
        return self.conn.execute("SELECT * FROM punishments WHERE id=?", (pid,)).fetchone()

    def close_punishment(self, pid, status="removed"):
        self.conn.execute("UPDATE punishments SET status=? WHERE id=? AND status='active'", (status, pid))
        self.conn.commit()

    def active_for_user(self, chat_id, user_id, kind=None):
        q = "SELECT * FROM punishments WHERE chat_id=? AND user_id=? AND status='active'"
        args = [chat_id, user_id]
        if kind:
            q += " AND type=?"
            args.append(kind)
        q += " ORDER BY created_at ASC"
        return self.conn.execute(q, args).fetchall()

    def active_expiring(self):
        return self.conn.execute("""
            SELECT * FROM punishments
            WHERE status='active' AND expires_at IS NOT NULL
              AND expires_at > strftime('%s','now')
        """).fetchall()

    def punishments_page(self, chat_id, page, per):
        total = self.conn.execute(
            "SELECT COUNT(*) FROM punishments WHERE chat_id=? AND status='active'",
            (chat_id,)
        ).fetchone()[0]
        rows = self.conn.execute("""
            SELECT * FROM punishments
            WHERE chat_id=? AND status='active'
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (chat_id, per, page * per)).fetchall()
        return rows, total

    def users_page(self, chat_id, page, per):
        total = self.conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT user_id FROM punishments WHERE chat_id=? GROUP BY user_id
            )
        """, (chat_id,)).fetchone()[0]
        rows = self.conn.execute("""
            SELECT user_id, username, first_name, COUNT(*) cnt
            FROM punishments WHERE chat_id=?
            GROUP BY user_id ORDER BY MAX(created_at) DESC
            LIMIT ? OFFSET ?
        """, (chat_id, per, page * per)).fetchall()
        return rows, total

    def add_log(self, chat_id, admin_id, action, target_id, reason="", duration=None):
        self.conn.execute("""
            INSERT INTO action_logs(chat_id,admin_id,action,target_id,reason,duration)
            VALUES(?,?,?,?,?,?)
        """, (chat_id,admin_id,action,target_id,reason,duration))
        self.conn.commit()

    def logs_page(self, chat_id, page, per):
        total = self.conn.execute("SELECT COUNT(*) FROM action_logs WHERE chat_id=?", (chat_id,)).fetchone()[0]
        rows = self.conn.execute("""
            SELECT * FROM action_logs WHERE chat_id=?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (chat_id, per, page * per)).fetchall()
        return rows, total
