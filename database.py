import sqlite3
import json
from datetime import datetime, timezone, timedelta
from typing import List, Optional

SCHEMA_VERSION = 4


class Database:
    """SQLite storage; member records are isolated by Discord guild."""
    def __init__(self, db_path="voice_stats.db"):
        self.db_path = db_path
        self._create_tables()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _create_tables(self):
        with self._connect() as conn:
            c = conn.cursor()
            # Schema upgrades are additive. Never drop existing guild data here.
            c.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT, display_name TEXT,
                total_seconds INTEGER NOT NULL DEFAULT 0, first_seen TEXT, xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1, streak INTEGER NOT NULL DEFAULT 0, last_streak_date TEXT,
                highest_streak INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id,user_id));
            CREATE TABLE IF NOT EXISTS sessions (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, date TEXT NOT NULL, seconds INTEGER NOT NULL,
                PRIMARY KEY (guild_id,user_id,date), FOREIGN KEY (guild_id,user_id) REFERENCES users(guild_id,user_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                moderator_id INTEGER, reason TEXT, date TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guild_settings (guild_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT,PRIMARY KEY(guild_id,key));
            CREATE TABLE IF NOT EXISTS temp_channels (channel_id INTEGER PRIMARY KEY,guild_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER NOT NULL,title TEXT NOT NULL,description TEXT,link_url TEXT NOT NULL,
                channel_id INTEGER NOT NULL,channel_name TEXT,status TEXT NOT NULL DEFAULT 'sent',discord_message_id TEXT,
                scheduled_time TEXT,created_at TEXT NOT NULL,error_message TEXT);
            CREATE TABLE IF NOT EXISTS post_images (id INTEGER PRIMARY KEY AUTOINCREMENT,post_id INTEGER NOT NULL,filename TEXT,image_url TEXT,image_bytes BLOB,FOREIGN KEY(post_id) REFERENCES admin_posts(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS log_settings (
                guild_id INTEGER NOT NULL, event_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                channel_id INTEGER, PRIMARY KEY(guild_id,event_key));
            CREATE TABLE IF NOT EXISTS log_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, event_key TEXT NOT NULL,
                title TEXT NOT NULL, actor_id INTEGER, actor_name TEXT, target_id INTEGER, target_name TEXT,
                channel_id INTEGER, channel_name TEXT, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invite_snapshots (
                guild_id INTEGER NOT NULL, code TEXT NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
                inviter_id INTEGER, inviter_name TEXT, channel_id INTEGER, url TEXT, expires_at TEXT,
                PRIMARY KEY(guild_id,code));
            CREATE INDEX IF NOT EXISTS idx_warnings_guild_user ON warnings(guild_id,user_id);
            CREATE INDEX IF NOT EXISTS idx_posts_guild ON admin_posts(guild_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_logs_guild_created ON log_events(guild_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_logs_guild_event ON log_events(guild_id,event_key,created_at DESC);
            """)
            # Forget only the retired welcome/auto-role settings; preserve every other guild setting.
            c.execute("DELETE FROM guild_settings WHERE key IN ('autorole','member_role','new_member_role','welcome_channel')")
            c.execute("INSERT OR REPLACE INTO schema_meta VALUES('version',?)", (str(SCHEMA_VERSION),))

    def get_log_settings(self, guild_id):
        with self._connect() as c:
            rows = c.execute("SELECT event_key,enabled,channel_id FROM log_settings WHERE guild_id=?", (guild_id,)).fetchall()
        return {row["event_key"]: {"enabled": bool(row["enabled"]), "channel_id": row["channel_id"]} for row in rows}

    def set_log_setting(self, guild_id, event_key, enabled, channel_id):
        with self._connect() as c:
            c.execute("""INSERT INTO log_settings(guild_id,event_key,enabled,channel_id) VALUES(?,?,?,?)
                ON CONFLICT(guild_id,event_key) DO UPDATE SET enabled=excluded.enabled,channel_id=excluded.channel_id""",
                (guild_id, event_key, int(bool(enabled)), channel_id))

    def add_log_event(self, guild_id, event_key, title, actor_id=None, actor_name=None,
                      target_id=None, target_name=None, channel_id=None, channel_name=None,
                      details=None, created_at=None):
        with self._connect() as c:
            cur = c.execute("""INSERT INTO log_events(guild_id,event_key,title,actor_id,actor_name,target_id,
                target_name,channel_id,channel_name,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (guild_id, event_key, title, actor_id, actor_name, target_id, target_name,
                 channel_id, channel_name, json.dumps(details or {}, ensure_ascii=False),
                 created_at or datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid

    def query_log_events(self, guild_id, start=None, end=None, event_key=None, search=None, limit=100, offset=0):
        clauses, args = ["guild_id=?"], [guild_id]
        if start:
            clauses.append("created_at>=?"); args.append(start)
        if end:
            clauses.append("created_at<=?"); args.append(end)
        if event_key:
            clauses.append("event_key=?"); args.append(event_key)
        if search:
            clauses.append("(title LIKE ? OR actor_name LIKE ? OR target_name LIKE ? OR details LIKE ?)")
            args.extend([f"%{search}%"] * 4)
        where = " AND ".join(clauses)
        with self._connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM log_events WHERE {where}", args).fetchone()[0]
            rows = c.execute(f"SELECT * FROM log_events WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                              [*args, max(1, min(int(limit), 200)), max(0, int(offset))]).fetchall()
        return int(total), [dict(row) for row in rows]

    def get_log_analytics(self, guild_id, start, end):
        with self._connect() as c:
            by_event = c.execute("SELECT event_key,COUNT(*) count FROM log_events WHERE guild_id=? AND created_at>=? AND created_at<=? GROUP BY event_key ORDER BY count DESC",
                                 (guild_id, start, end)).fetchall()
            by_day = c.execute("SELECT substr(created_at,1,10) day,COUNT(*) count FROM log_events WHERE guild_id=? AND created_at>=? AND created_at<=? GROUP BY day ORDER BY day",
                               (guild_id, start, end)).fetchall()
            invites = c.execute("SELECT actor_id,actor_name,COUNT(*) count FROM log_events WHERE guild_id=? AND event_key='invite_use' AND created_at>=? AND created_at<=? AND actor_id IS NOT NULL GROUP BY actor_id,actor_name ORDER BY count DESC LIMIT 10",
                                (guild_id, start, end)).fetchall()
        return {"by_event": [dict(r) for r in by_event], "by_day": [dict(r) for r in by_day],
                "top_inviters": [dict(r) for r in invites]}

    def save_invite_snapshots(self, guild_id, invites):
        with self._connect() as c:
            c.execute("DELETE FROM invite_snapshots WHERE guild_id=?", (guild_id,))
            c.executemany("INSERT INTO invite_snapshots(guild_id,code,uses,inviter_id,inviter_name,channel_id,url,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                          [(guild_id, item["code"], item["uses"], item.get("inviter_id"), item.get("inviter_name"),
                            item.get("channel_id"), item.get("url"), item.get("expires_at")) for item in invites])

    def get_invite_snapshots(self, guild_id):
        with self._connect() as c:
            rows = c.execute("SELECT * FROM invite_snapshots WHERE guild_id=?", (guild_id,)).fetchall()
        return {row["code"]: dict(row) for row in rows}

    @staticmethod
    def _today(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def upsert_user_identity(self,guild_id,user_id,username,display_name):
        with self._connect() as c: c.execute("INSERT INTO users(guild_id,user_id,username,display_name,first_seen) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name",(guild_id,user_id,username,display_name,self._today()))
    def add_voice_time(self,guild_id,user_id,username,display_name,seconds):
        if seconds<=0:return
        with self._connect() as c:
            c.execute("INSERT INTO users(guild_id,user_id,username,display_name,total_seconds,first_seen) VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,total_seconds=users.total_seconds+excluded.total_seconds",(guild_id,user_id,username,display_name,int(seconds),self._today()))
            c.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id,date) DO UPDATE SET seconds=sessions.seconds+excluded.seconds",(guild_id,user_id,self._today(),int(seconds)))
            total=c.execute("SELECT seconds FROM sessions WHERE guild_id=? AND user_id=? AND date=?",(guild_id,user_id,self._today())).fetchone()[0]
        if total>=900:self.update_streak(guild_id,user_id)
    def get_user(self,guild_id,user_id):
        with self._connect() as c: row=c.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?",(guild_id,user_id)).fetchone()
        return dict(row) if row else None
    def get_rank(self,guild_id,user_id):
        user=self.get_user(guild_id,user_id)
        if not user:return None
        with self._connect() as c:return c.execute("SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND total_seconds>?",(guild_id,user['total_seconds'])).fetchone()[0]
    def get_leaderboard(self,guild_id,limit):
        with self._connect() as c: rows=c.execute("SELECT user_id,username,display_name,total_seconds,first_seen FROM users WHERE guild_id=? ORDER BY total_seconds DESC LIMIT ?",(guild_id,limit)).fetchall()
        return [dict(x) for x in rows]
    def _period(self,guild_id,user_id,start):
        with self._connect() as c:return int(c.execute("SELECT COALESCE(SUM(seconds),0) FROM sessions WHERE guild_id=? AND user_id=? AND date>=?",(guild_id,user_id,start)).fetchone()[0])
    def get_today(self,guild_id,user_id):return self._period(guild_id,user_id,self._today())
    def get_week(self,guild_id,user_id):return self._period(guild_id,user_id,(datetime.now(timezone.utc)-timedelta(days=7)).strftime("%Y-%m-%d"))
    def get_month(self,guild_id,user_id):return self._period(guild_id,user_id,(datetime.now(timezone.utc)-timedelta(days=30)).strftime("%Y-%m-%d"))
    def get_period_leaderboard(self,guild_id,period,limit):
        starts={'gun':self._today(),'hefte':(datetime.now(timezone.utc)-timedelta(days=7)).strftime('%Y-%m-%d'),'ay':(datetime.now(timezone.utc)-timedelta(days=30)).strftime('%Y-%m-%d')}
        if period not in starts:return []
        with self._connect() as c: rows=c.execute("SELECT s.user_id,u.username,u.display_name,SUM(s.seconds) total_seconds FROM sessions s JOIN users u ON u.guild_id=s.guild_id AND u.user_id=s.user_id WHERE s.guild_id=? AND s.date>=? GROUP BY s.user_id ORDER BY total_seconds DESC LIMIT ?",(guild_id,starts[period],limit)).fetchall()
        return [dict(x) for x in rows]
    def reset_user(self,guild_id,user_id):
        with self._connect() as c:c.execute("DELETE FROM sessions WHERE guild_id=? AND user_id=?",(guild_id,user_id));c.execute("UPDATE users SET total_seconds=0 WHERE guild_id=? AND user_id=?",(guild_id,user_id))
    def xp_for_level(self,level):
        level=int(level)
        return 0 if level<=1 else 100 if level==2 else 250 if level==3 else 450 if level==4 else 450+sum(n*150 for n in range(5,level+1))
    def _level_from_xp(self,xp):
        level=1
        while xp>=self.xp_for_level(level+1):level+=1
        return level
    def add_xp(self,guild_id,user_id,amount):
        if not self.get_user(guild_id,user_id):self.upsert_user_identity(guild_id,user_id,'Naməlum','Naməlum')
        user=self.get_user(guild_id,user_id); old_xp=int(user['xp']);old_level=int(user['level']);new_xp=old_xp+max(0,int(amount));new_level=self._level_from_xp(new_xp)
        with self._connect() as c:c.execute("UPDATE users SET xp=?,level=? WHERE guild_id=? AND user_id=?",(new_xp,new_level,guild_id,user_id))
        return new_xp,new_level,new_level>old_level
    def get_user_rank(self,guild_id,user_id):
        user=self.get_user(guild_id,user_id)
        if not user:return 0
        with self._connect() as c:return c.execute("SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND xp>?",(guild_id,user['xp'])).fetchone()[0]
    def get_level_leaderboard(self,guild_id,limit):
        with self._connect() as c:rows=c.execute("SELECT user_id,username,display_name,xp,level FROM users WHERE guild_id=? ORDER BY level DESC,xp DESC LIMIT ?",(guild_id,limit)).fetchall()
        return [dict(x) for x in rows]
    def add_warning(self,guild_id,user_id,moderator_id,reason):
        with self._connect() as c:c.execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,date) VALUES(?,?,?,?,?)",(guild_id,user_id,moderator_id,reason,self._today()))
    def get_warnings(self,guild_id,user_id,limit=10):
        with self._connect() as c:rows=c.execute("SELECT id,user_id,moderator_id,reason,date FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",(guild_id,user_id,limit)).fetchall()
        return [dict(x) for x in rows]
    def get_warning_count(self,guild_id,user_id):
        with self._connect() as c:return c.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",(guild_id,user_id)).fetchone()[0]
    def get_warning_leaderboard(self,guild_id,limit=10):
        with self._connect() as c:rows=c.execute("SELECT w.user_id,COALESCE(u.display_name,u.username,'Naməlum') display_name,COUNT(*) warning_count,MAX(w.date) latest_warning FROM warnings w LEFT JOIN users u ON u.guild_id=w.guild_id AND u.user_id=w.user_id WHERE w.guild_id=? GROUP BY w.user_id ORDER BY warning_count DESC,latest_warning DESC LIMIT ?",(guild_id,limit)).fetchall()
        return [dict(x) for x in rows]
    def delete_warning(self,guild_id,warning_id):
        with self._connect() as c:return c.execute("DELETE FROM warnings WHERE guild_id=? AND id=?",(guild_id,warning_id)).rowcount>0
    def clear_warnings(self,guild_id,user_id):
        with self._connect() as c:return c.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",(guild_id,user_id)).rowcount
    def update_streak(self,guild_id,user_id):
        today=self._today(); yesterday=(datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y-%m-%d');user=self.get_user(guild_id,user_id)
        if not user:return 0,False,0
        if user['last_streak_date']==today:return int(user['streak']),False,0
        streak=int(user['streak'])+1 if user['last_streak_date']==yesterday else 1;bonus=min(streak*10,100)
        with self._connect() as c:c.execute("UPDATE users SET streak=?,last_streak_date=?,highest_streak=MAX(highest_streak,?),xp=xp+? WHERE guild_id=? AND user_id=?",(streak,today,streak,bonus,guild_id,user_id))
        return streak,True,bonus
    def get_streak(self,guild_id,user_id):
        user=self.get_user(guild_id,user_id)
        if not user:return {'streak':0,'highest_streak':0,'active_today':False}
        today=self._today();last=user['last_streak_date']; yesterday=(datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y-%m-%d')
        return {'streak':int(user['streak']) if last in (today,yesterday) else 0,'highest_streak':int(user['highest_streak']),'active_today':last==today,'last_streak_date':last or 'Qeyd olunmayıb'}
    def get_streak_leaderboard(self,guild_id,limit=10):
        today=self._today();yesterday=(datetime.now(timezone.utc)-timedelta(days=1)).strftime('%Y-%m-%d')
        with self._connect() as c:rows=c.execute("SELECT user_id,username,display_name,CASE WHEN last_streak_date IN (?,?) THEN streak ELSE 0 END current_streak,highest_streak FROM users WHERE guild_id=? AND (highest_streak>0 OR streak>0) ORDER BY current_streak DESC,highest_streak DESC,total_seconds DESC LIMIT ?",(today,yesterday,guild_id,limit)).fetchall()
        return [dict(x) for x in rows]
    def get_user_daily_history(self,guild_id,user_id,days=7):
        dates=[(datetime.now(timezone.utc)-timedelta(days=days-1-i)).strftime('%Y-%m-%d') for i in range(days)]
        with self._connect() as c:rows=c.execute("SELECT date,seconds FROM sessions WHERE guild_id=? AND user_id=? AND date>=?",(guild_id,user_id,dates[0])).fetchall()
        data={x['date']:x['seconds'] for x in rows};return [{'date':d,'seconds':data.get(d,0)} for d in dates]
    def add_temp_channel(self,channel_id,guild_id,owner_id):
        with self._connect() as c:c.execute("INSERT INTO temp_channels VALUES(?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET owner_id=excluded.owner_id",(channel_id,guild_id,owner_id,datetime.now(timezone.utc).isoformat()))
    def get_temp_channel(self,channel_id):
        with self._connect() as c:row=c.execute("SELECT * FROM temp_channels WHERE channel_id=?",(channel_id,)).fetchone()
        return dict(row) if row else None
    def remove_temp_channel(self,channel_id):
        with self._connect() as c:c.execute("DELETE FROM temp_channels WHERE channel_id=?",(channel_id,))
    def get_guild_temp_channels(self,guild_id):
        with self._connect() as c:rows=c.execute("SELECT * FROM temp_channels WHERE guild_id=?",(guild_id,)).fetchall()
        return [dict(x) for x in rows]
    def create_admin_post(self,guild_id,title,description,link_url,channel_id,channel_name,status='sent',discord_message_id=None,scheduled_time=None,error_message=None,images=None):
        with self._connect() as c:
            post_id=c.execute("INSERT INTO admin_posts(guild_id,title,description,link_url,channel_id,channel_name,status,discord_message_id,scheduled_time,created_at,error_message) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(guild_id,title,description,link_url,channel_id,channel_name,status,discord_message_id,scheduled_time,datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),error_message)).lastrowid
            for image in images or []:c.execute("INSERT INTO post_images(post_id,filename,image_url,image_bytes) VALUES(?,?,?,?)",(post_id,image.get('filename'),image.get('image_url'),image.get('image_bytes')))
        return post_id
    def get_admin_posts(self,limit=50,guild_id=None):
        q='SELECT * FROM admin_posts';args=[]
        if guild_id is not None:q+=' WHERE guild_id=?';args.append(guild_id)
        q+=' ORDER BY id DESC LIMIT ?';args.append(limit)
        with self._connect() as c:rows=c.execute(q,args).fetchall()
        return [dict(x) for x in rows]
    def get_admin_post(self,post_id,guild_id=None):
        q='SELECT * FROM admin_posts WHERE id=?';args=[post_id]
        if guild_id is not None:q+=' AND guild_id=?';args.append(guild_id)
        with self._connect() as c:row=c.execute(q,args).fetchone()
        return dict(row) if row else None
    def get_post_images(self,post_id):
        with self._connect() as c:rows=c.execute('SELECT id,post_id,filename,image_url,image_bytes FROM post_images WHERE post_id=?',(post_id,)).fetchall()
        return [dict(x) for x in rows]
    def update_scheduled_post_status(self,post_id,status,discord_message_id=None,error_message=None):
        with self._connect() as c:c.execute('UPDATE admin_posts SET status=?,discord_message_id=COALESCE(?,discord_message_id),error_message=? WHERE id=?',(status,discord_message_id,error_message,post_id))
    def get_due_scheduled_posts(self):
        with self._connect() as c:rows=c.execute("SELECT * FROM admin_posts WHERE status='scheduled' AND scheduled_time<=?",(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),)).fetchall()
        return [dict(x) for x in rows]
    def delete_admin_post(self,post_id,guild_id=None):
        q='DELETE FROM admin_posts WHERE id=?';args=[post_id]
        if guild_id is not None:q+=' AND guild_id=?';args.append(guild_id)
        with self._connect() as c:return c.execute(q,args).rowcount>0
