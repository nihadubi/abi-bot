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

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_settings (
                        guild_id INTEGER,
                        key TEXT,
                        value TEXT,
                        PRIMARY KEY (guild_id, key)
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS temp_channels (
                        channel_id INTEGER PRIMARY KEY,
                        guild_id INTEGER,
                        owner_id INTEGER,
                        created_at TEXT
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
                if "streak" not in existing_columns:
                    cursor.execute("ALTER TABLE users ADD COLUMN streak INTEGER DEFAULT 0")
                if "last_streak_date" not in existing_columns:
                    cursor.execute("ALTER TABLE users ADD COLUMN last_streak_date TEXT")
                if "highest_streak" not in existing_columns:
                    cursor.execute("ALTER TABLE users ADD COLUMN highest_streak INTEGER DEFAULT 0")

                conn.commit()
        except Exception as error:
            print(f"[DB Xətası] Cədvəllər yaradılmadı: {error}")

    def set_guild_setting(self, guild_id: int, key: str, value: str):
        # Server tənzimləməsini bazaya yazırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO guild_settings (guild_id, key, value)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value
                    """,
                    (guild_id, key, str(value)),
                )
                conn.commit()
                return True
        except Exception as error:
            print(f"[DB Xətası] Tənzimləmə saxlanılmadı: {error}")
            return False

    def get_guild_setting(self, guild_id: int, key: str, default=None):
        # Server tənzimləməsini bazadan oxuyuruq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
                    (guild_id, key),
                )
                row = cursor.fetchone()
                return row[0] if row else default
        except Exception as error:
            print(f"[DB Xətası] Tənzimləmə oxunmadı: {error}")
            return default

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

                cursor.execute(
                    "SELECT seconds FROM sessions WHERE user_id = ? AND date = ?",
                    (user_id, today),
                )
                today_row = cursor.fetchone()
                conn.commit()

            # Əgər bu gün ən azı 15 dəqiqə (900 saniyə) səsdə olubsa, streak-i yeniləyirik
            if today_row and today_row[0] >= 900:
                self.update_streak(user_id)
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

    def get_user_rank(self, user_id: int) -> int:
        # İstifadəçinin XP-yə görə serverdəki sıralamasını (Rank #) qaytarır
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT xp FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if not row:
                    return 0
                user_xp = row[0] or 0
                cursor.execute("SELECT COUNT(*) FROM users WHERE xp > ?", (user_xp,))
                higher_count = cursor.fetchone()[0]
                return higher_count + 1
        except Exception as error:
            print(f"[DB Xətası] Rank alınmadı: {error}")
            return 0

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

    def get_warning_count(self, user_id):
        # İstifadəçinin ümumi xəbərdarlıq sayını qaytarırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                return row[0] if row else 0
        except Exception as error:
            print(f"[DB Xətası] Xəbərdarlıq sayı alınmadı: {error}")
            return 0

    def get_warning_leaderboard(self, limit=10):
        # Warn-u olan istifadəçiləri ümumi sayına görə sıralayırıq
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        warnings.user_id,
                        COALESCE(users.display_name, users.username, 'Naməlum') AS display_name,
                        COUNT(*) AS warning_count,
                        MAX(warnings.date) AS latest_warning
                    FROM warnings
                    LEFT JOIN users ON users.user_id = warnings.user_id
                    GROUP BY warnings.user_id
                    ORDER BY warning_count DESC, latest_warning DESC, warnings.user_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as error:
            print(f"[DB Xətası] Warn lider cədvəli alınmadı: {error}")
            return []

        # ID üzrə xüsusi xəbərdarlığı silirik
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM warnings WHERE id = ?", (warning_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as error:
            print(f"[DB Xətası] Xəbərdarlıq silinmədi: {error}")
            return False

    def clear_warnings(self, user_id):
        # İstifadəçinin bütün xəbərdarlıqlarını təmizləyirik
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM warnings WHERE user_id = ?", (user_id,))
                conn.commit()
                return cursor.rowcount
        except Exception as error:
            print(f"[DB Xətası] Xəbərdarlıqlar təmizlənmədi: {error}")
            return 0

    def update_streak(self, user_id: int):
        """Gündəlik səs aktivliyinə görə streak-i yeniləyir.
        Qaytarır: (current_streak: int, updated_today: bool, bonus_xp: int)
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT streak, last_streak_date, highest_streak, xp FROM users WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()

                if not row:
                    return 0, False, 0

                current_streak = row["streak"] or 0
                last_date = row["last_streak_date"]
                highest = row["highest_streak"] or 0

                # Əgər bu gün artıq seriya sayılıbsa
                if last_date == today:
                    return current_streak, False, 0

                # Əgər dünən səsdə olubsa, seriya davam edir
                if last_date == yesterday:
                    new_streak = current_streak + 1
                else:
                    # Əgər dünən səsdə olmayıbsa, seriya 1-dən yenidən başlayır
                    new_streak = 1

                new_highest = max(highest, new_streak)
                # Seriya bonusu: hər gün üçün new_streak * 10 XP (maksimum 100 XP)
                bonus_xp = min(new_streak * 10, 100)

                cursor.execute(
                    """
                    UPDATE users
                    SET streak = ?, last_streak_date = ?, highest_streak = ?, xp = xp + ?
                    WHERE user_id = ?
                    """,
                    (new_streak, today, new_highest, bonus_xp, user_id),
                )
                conn.commit()
                return new_streak, True, bonus_xp
        except Exception as error:
            print(f"[DB Xətası] Streak yenilənmədi: {error}")
            return 0, False, 0

    def get_streak(self, user_id: int):
        """İstifadəçinin aktiv və ən yüksək streak məlumatını qaytarır."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT streak, last_streak_date, highest_streak FROM users WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return {"streak": 0, "highest_streak": 0, "active_today": False}

                last_date = row["last_streak_date"]
                current_streak = row["streak"] or 0
                highest = row["highest_streak"] or 0

                # Əgər son aktivlik dünən və ya bu gün deyilsə, aktiv seriya sıfırlanmış sayılır
                if last_date not in (today, yesterday):
                    current_streak = 0

                return {
                    "streak": current_streak,
                    "highest_streak": highest,
                    "active_today": (last_date == today),
                    "last_streak_date": last_date or "Qeyd olunmayıb",
                }
        except Exception as error:
            print(f"[DB Xətası] Streak oxunmadı: {error}")
            return {"streak": 0, "highest_streak": 0, "active_today": False}

    def get_streak_leaderboard(self, limit: int = 10):
        """Ən yüksək aktiv və rekord səs seriyasına sahib istifadəçilər."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT user_id, username, display_name,
                           CASE WHEN last_streak_date IN (?, ?) THEN streak ELSE 0 END AS current_streak,
                           highest_streak
                    FROM users
                    WHERE highest_streak > 0 OR streak > 0
                    ORDER BY current_streak DESC, highest_streak DESC, total_seconds DESC
                    LIMIT ?
                    """,
                    (today, yesterday, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as error:
            print(f"[DB Xətası] Streak lider cədvəli alınmadı: {error}")
            return []

    def get_user_daily_history(self, user_id: int, days: int = 7):
        """İstifadəçinin son N günlük səs aktivliyini (saniyələrlə) qaytarır."""
        start_date = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT date, seconds
                    FROM sessions
                    WHERE user_id = ? AND date >= ?
                    ORDER BY date ASC
                    """,
                    (user_id, start_date),
                )
                data_dict = {row[0]: row[1] for row in cursor.fetchall()}

            result = []
            for i in range(days):
                d = (datetime.utcnow() - timedelta(days=days - 1 - i))
                date_str = d.strftime("%Y-%m-%d")
                weekday_az = ["B.e", "Ç.a", "Ç", "C.a", "C", "Ş", "B"][d.weekday()]
                result.append({
                    "date": date_str,
                    "day_label": f"{weekday_az}\n{d.strftime('%d.%m')}",
                    "weekday": weekday_az,
                    "seconds": data_dict.get(date_str, 0),
                })
            return result
        except Exception as error:
            print(f"[DB Xətası] Günlük statistika tarixi alınmadı: {error}")
            return []

    def add_temp_channel(self, channel_id: int, guild_id: int, owner_id: int):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute(
                    """
                    INSERT INTO temp_channels (channel_id, guild_id, owner_id, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET owner_id = excluded.owner_id
                    """,
                    (channel_id, guild_id, owner_id, now),
                )
                conn.commit()
                return True
        except Exception as error:
            print(f"[DB Xətası] Temp kanal əlavə edilmədi: {error}")
            return False

    def get_temp_channel(self, channel_id: int):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM temp_channels WHERE channel_id = ?", (channel_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as error:
            print(f"[DB Xətası] Temp kanal oxunmadı: {error}")
            return None

    def remove_temp_channel(self, channel_id: int):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM temp_channels WHERE channel_id = ?", (channel_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as error:
            print(f"[DB Xətası] Temp kanal silinmədi: {error}")
            return False

    def get_guild_temp_channels(self, guild_id: int):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM temp_channels WHERE guild_id = ?", (guild_id,))
                return [dict(row) for row in cursor.fetchall()]
        except Exception as error:
            print(f"[DB Xətası] Server temp kanalları oxunmadı: {error}")
            return []
