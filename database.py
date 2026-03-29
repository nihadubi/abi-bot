import sqlite3
from datetime import datetime, timedelta


class Database:
    def __init__(self, db_path: str = "voice_stats.db"):
        self.db_path = db_path
        self._create_tables()

    def _create_tables(self):
        # Cədvəlləri ilkin olaraq yaradırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        display_name TEXT,
                        total_seconds INTEGER DEFAULT 0,
                        first_seen TEXT,
                        xp INTEGER DEFAULT 0,
                        level INTEGER DEFAULT 1
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        date TEXT,
                        seconds INTEGER,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS warnings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        moderator_id INTEGER,
                        reason TEXT,
                        date TEXT
                    )
                    """
                )

                # Mövcud bazada çatışmayan sütunları əlavə edirik
                cursor.execute("PRAGMA table_info(users)")
                existing_columns = {row[1] for row in cursor.fetchall()}

                if "xp" not in existing_columns:
                    cursor.execute("ALTER TABLE users ADD COLUMN xp INTEGER DEFAULT 0")
                if "level" not in existing_columns:
                    cursor.execute("ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 1")

                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] Cədvəllər yaradılmadı: {error}")

    def add_voice_time(self, user_id, username, display_name, seconds):
        # İstifadəçinin səs vaxtını ümumi və günlük statistikalara əlavə edirik
        try:
            if seconds <= 0:
                return

            today = datetime.utcnow().strftime("%Y-%m-%d")

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                existing_user = cursor.fetchone()

                if existing_user:
                    cursor.execute(
                        """
                        UPDATE users
                        SET username = ?, display_name = ?, total_seconds = total_seconds + ?
                        WHERE user_id = ?
                        """,
                        (username, display_name, int(seconds), user_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO users (user_id, username, display_name, total_seconds, first_seen)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (user_id, username, display_name, int(seconds), today),
                    )

                cursor.execute(
                    "SELECT id FROM sessions WHERE user_id = ? AND date = ?",
                    (user_id, today),
                )
                existing_session = cursor.fetchone()

                if existing_session:
                    cursor.execute(
                        """
                        UPDATE sessions
                        SET seconds = seconds + ?
                        WHERE user_id = ? AND date = ?
                        """,
                        (int(seconds), user_id, today),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO sessions (user_id, date, seconds)
                        VALUES (?, ?, ?)
                        """,
                        (user_id, today, int(seconds)),
                    )

                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] Səs vaxtı əlavə olunmadı: {error}")

    def get_user(self, user_id):
        # İstifadəçinin ümumi məlumatlarını qaytarırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as error:
            print(f"[DB Xətası] İstifadəçi alınmadı: {error}")
            return None

    def get_rank(self, user_id):
        # İstifadəçinin ümumi vaxta görə sıralamasını hesablayırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT total_seconds FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if not row:
                    return None

                total_seconds = row[0]
                cursor.execute(
                    "SELECT COUNT(*) + 1 FROM users WHERE total_seconds > ?",
                    (total_seconds,),
                )
                rank_row = cursor.fetchone()
                return rank_row[0] if rank_row else None
        except Exception as error:
            print(f"[DB Xətası] Sıralama alınmadı: {error}")
            return None

    def get_leaderboard(self, limit):
        # Ümumi lider siyahısını qaytarırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT user_id, username, display_name, total_seconds, first_seen
                    FROM users
                    ORDER BY total_seconds DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as error:
            print(f"[DB Xətası] Lider cədvəli alınmadı: {error}")
            return []

    def get_today(self, user_id):
        # Bu günün saniyələrini qaytarırıq
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(SUM(seconds), 0) FROM sessions WHERE user_id = ? AND date = ?",
                    (user_id, today),
                )
                row = cursor.fetchone()
                return int(row[0] or 0)
        except Exception as error:
            print(f"[DB Xətası] Bu günün vaxtı alınmadı: {error}")
            return 0

    def get_week(self, user_id):
        # Son 7 günün saniyələrini qaytarırıq
        try:
            start_date = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(SUM(seconds), 0) FROM sessions WHERE user_id = ? AND date >= ?",
                    (user_id, start_date),
                )
                row = cursor.fetchone()
                return int(row[0] or 0)
        except Exception as error:
            print(f"[DB Xətası] Həftəlik vaxt alınmadı: {error}")
            return 0

    def get_month(self, user_id):
        # Son 30 günün saniyələrini qaytarırıq
        try:
            start_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(SUM(seconds), 0) FROM sessions WHERE user_id = ? AND date >= ?",
                    (user_id, start_date),
                )
                row = cursor.fetchone()
                return int(row[0] or 0)
        except Exception as error:
            print(f"[DB Xətası] Aylıq vaxt alınmadı: {error}")
            return 0

    def get_period_leaderboard(self, period, limit):
        # Verilmiş period üzrə lider cədvəlini qaytarırıq
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            week_start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
            month_start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                if period == "gun":
                    cursor.execute(
                        """
                        SELECT s.user_id, u.username, u.display_name, COALESCE(SUM(s.seconds), 0) AS total_seconds
                        FROM sessions s
                        JOIN users u ON u.user_id = s.user_id
                        WHERE s.date = ?
                        GROUP BY s.user_id
                        ORDER BY total_seconds DESC
                        LIMIT ?
                        """,
                        (today, limit),
                    )
                elif period == "hefte":
                    cursor.execute(
                        """
                        SELECT s.user_id, u.username, u.display_name, COALESCE(SUM(s.seconds), 0) AS total_seconds
                        FROM sessions s
                        JOIN users u ON u.user_id = s.user_id
                        WHERE s.date >= ?
                        GROUP BY s.user_id
                        ORDER BY total_seconds DESC
                        LIMIT ?
                        """,
                        (week_start, limit),
                    )
                elif period == "ay":
                    cursor.execute(
                        """
                        SELECT s.user_id, u.username, u.display_name, COALESCE(SUM(s.seconds), 0) AS total_seconds
                        FROM sessions s
                        JOIN users u ON u.user_id = s.user_id
                        WHERE s.date >= ?
                        GROUP BY s.user_id
                        ORDER BY total_seconds DESC
                        LIMIT ?
                        """,
                        (month_start, limit),
                    )
                else:
                    return []

                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as error:
            print(f"[DB Xətası] Period lider cədvəli alınmadı: {error}")
            return []

    def reset_user(self, user_id):
        # İstifadəçinin bütün səs statistikasını sıfırlayırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                cursor.execute("UPDATE users SET total_seconds = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] İstifadəçi sıfırlanmadı: {error}")

    def xp_for_level(self, level):
        # Verilmiş səviyyə üçün lazım olan ümumi XP həddini hesablayırıq
        try:
            level = int(level)
            if level <= 1:
                return 0
            if level == 2:
                return 100
            if level == 3:
                return 250
            if level == 4:
                return 450

            threshold = 450
            for current_level in range(5, level + 1):
                threshold += current_level * 150
            return threshold
        except Exception as error:
            print(f"[DB Xətası] XP həddi hesablanmadı: {error}")
            return 0

    def _level_from_xp(self, xp):
        # Mövcud ümumi XP dəyərindən səviyyəni müəyyən edirik
        level = 1
        while xp >= self.xp_for_level(level + 1):
            level += 1
        return level

    def add_xp(self, user_id, amount):
        # İstifadəçiyə XP əlavə edir və level artımını yoxlayırıq
        try:
            amount = int(amount)
            if amount <= 0:
                user = self.get_user(user_id)
                if user:
                    return int(user.get("xp") or 0), int(user.get("level") or 1), False
                return 0, 1, False

            today = datetime.utcnow().strftime("%Y-%m-%d")

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()

                if not user:
                    cursor.execute(
                        """
                        INSERT INTO users (user_id, username, display_name, total_seconds, first_seen, xp, level)
                        VALUES (?, ?, ?, 0, ?, 0, 1)
                        """,
                        (user_id, "Naməlum", "Naməlum", today),
                    )
                    old_xp = 0
                    old_level = 1
                else:
                    old_xp = int(user["xp"] or 0)
                    old_level = int(user["level"] or 1)

                new_xp = old_xp + amount
                new_level = self._level_from_xp(new_xp)

                cursor.execute(
                    "UPDATE users SET xp = ?, level = ? WHERE user_id = ?",
                    (new_xp, new_level, user_id),
                )
                conn.commit()

                return new_xp, new_level, new_level > old_level
        except Exception as error:
            print(f"[DB Xətası] XP əlavə olunmadı: {error}")
            return 0, 1, False

    def get_level_leaderboard(self, limit):
        # Səviyyə və XP-yə görə lider siyahısını qaytarırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT user_id, username, display_name, xp, level
                    FROM users
                    ORDER BY level DESC, xp DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as error:
            print(f"[DB Xətası] XP lider cədvəli alınmadı: {error}")
            return []

    def upsert_user_identity(self, user_id, username, display_name):
        # İstifadəçinin əsas məlumatlarını əlavə və ya yenilə edirik
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                existing = cursor.fetchone()

                if existing:
                    cursor.execute(
                        """
                        UPDATE users
                        SET username = ?, display_name = ?
                        WHERE user_id = ?
                        """,
                        (username, display_name, user_id),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO users (user_id, username, display_name, total_seconds, first_seen, xp, level)
                        VALUES (?, ?, ?, 0, ?, 0, 1)
                        """,
                        (user_id, username, display_name, today),
                    )
                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] İstifadəçi məlumatı yenilənmədi: {error}")

    def add_warning(self, user_id, moderator_id, reason):
        # İstifadəçiyə xəbərdarlıq əlavə edirik
        try:
            warning_date = datetime.utcnow().strftime("%Y-%m-%d")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO warnings (user_id, moderator_id, reason, date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, moderator_id, reason, warning_date),
                )
                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] Xəbərdarlıq əlavə olunmadı: {error}")

    def get_warnings(self, user_id, limit=10):
        # İstifadəçinin son xəbərdarlıqlarını qaytarırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, user_id, moderator_id, reason, date
                    FROM warnings
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as error:
            print(f"[DB Xətası] Xəbərdarlıqlar alınmadı: {error}")
            return []
