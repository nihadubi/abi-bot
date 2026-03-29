import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
import httpx
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database


load_dotenv()

PREFIX = "abi "
TOKEN = os.getenv("TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", 0))
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", 0))

# Moderasiya və anti-spam ayarları
ANTI_LINK_ENABLED = True
ANTI_SPAM_ENABLED = True
SPAM_WINDOW_SECONDS = 8
SPAM_MESSAGE_THRESHOLD = 5
SPAM_TIMEOUT_MINUTES = 2

# Level mükafat rolları (admin server rol ID-lərini doldurur)
LEVEL_ROLE_REWARDS = {
    # 5: 123456789012345678,
    # 10: 234567890123456789,
}

XP_AWARD_COOLDOWN_SECONDS = 600
XP_MIN_MEMBERS_IN_VOICE = 2
LINK_REGEX = re.compile(r"(https?://\S+|www\.\S+|discord\.gg/\S+)", re.IGNORECASE)


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
db = Database("voice_stats.db")

# Log sistemini aktivləşdiririk
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("abi_bot")

# Səsdə olan istifadəçilərin aktiv sessiya başlanğıc vaxtını yadda saxlayırıq
voice_sessions = {}

# Mesaj spamını izləmək üçün istifadəçi vaxtlarını saxlayırıq
spam_tracker = defaultdict(deque)

# XP anti-farm üçün son mükafat vaxtını saxlayırıq
last_xp_award = {}


def is_exempt_member(member: discord.Member) -> bool:
    # Admin və mesaj idarə etmə icazəsi olan istifadəçiləri filtrlərdən azad edirik
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.manage_messages:
        return True
    return False


def is_link_message(content: str) -> bool:
    # Mesajda link olub-olmadığını yoxlayırıq
    return bool(LINK_REGEX.search(content or ""))


def format_time(seconds: int) -> str:
    # Saniyəni daha oxunaqlı mətnə çeviririk
    seconds = int(seconds)
    if seconds <= 0:
        return "0 san"

    if seconds < 60:
        return f"{seconds} san"

    if seconds < 3600:
        minutes = seconds // 60
        remain_seconds = seconds % 60
        if remain_seconds > 0:
            return f"{minutes} dəq {remain_seconds} san"
        return f"{minutes} dəq"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if minutes > 0:
        return f"{hours} saat {minutes} dəq"
    return f"{hours} saat"


def get_medal(position: int) -> str:
    # Mövqeyə görə medal və ya nömrə qaytarırıq
    if position == 1:
        return "🥇"
    if position == 2:
        return "🥈"
    if position == 3:
        return "🥉"
    return f"{position}."


def get_live_seconds(user_id: int) -> int:
    # Aktiv səs sessiyası varsa, canlı keçən saniyəni hesablayırıq
    if user_id in voice_sessions:
        return int((datetime.utcnow() - voice_sessions[user_id]).total_seconds())
    return 0


def get_combined_totals():
    # Bazadakı ümumi vaxtı canlı sessiyalarla birləşdirib qaytarırıq
    rows = db.get_leaderboard(1_000_000)
    combined = {}

    for row in rows:
        user_id = row["user_id"]
        combined[user_id] = {
            "user_id": user_id,
            "username": row.get("username") or "Naməlum",
            "display_name": row.get("display_name") or row.get("username") or "Naməlum",
            "total_seconds": int(row.get("total_seconds") or 0),
            "first_seen": row.get("first_seen") or datetime.utcnow().strftime("%Y-%m-%d"),
        }

    for user_id, started_at in voice_sessions.items():
        live_seconds = int((datetime.utcnow() - started_at).total_seconds())
        if live_seconds < 0:
            live_seconds = 0

        if user_id in combined:
            combined[user_id]["total_seconds"] += live_seconds
        else:
            combined[user_id] = {
                "user_id": user_id,
                "username": "Naməlum",
                "display_name": "Naməlum",
                "total_seconds": live_seconds,
                "first_seen": datetime.utcnow().strftime("%Y-%m-%d"),
            }

    return sorted(combined.values(), key=lambda x: x["total_seconds"], reverse=True)


async def ask_ai(prompt: str) -> str:
    # Groq API ilə AI cavabını alırıq
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "Sən abi adlı Discord botsan. Azərbaycan dilində danış, qısa cavab ver, casual ol, bəzən zarafat elə. Özünü təqdim etmə, izah vermə, sadəcə normal danış."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500
            },
            timeout=30
        )
        data = response.json()
        print("Groq cavabı:", data)
        return data["choices"][0]["message"]["content"]


@bot.event
async def on_ready():
    # Bot açıldıqda hazırda səsdə olanları aktiv sessiyaya əlavə edirik
    voice_sessions.clear()

    for guild in bot.guilds:
        for channel in guild.voice_channels:
            for member in channel.members:
                if member.bot:
                    continue
                voice_sessions[member.id] = datetime.utcnow()

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="👁️ Big bro is watching you all"
        ),
        status=discord.Status.online
    )

    if not daily_report.is_running():
        daily_report.start()

    if not xp_task.is_running():
        xp_task.start()

    print(f"{bot.user} olaraq daxil olundu.")


@bot.event
async def on_voice_state_update(member, before, after):
    # Botları izləmədən çıxırıq
    if member.bot:
        return

    # Səsə qoşulma halında sessiyanı başladırıq
    if before.channel is None and after.channel is not None:
        voice_sessions[member.id] = datetime.utcnow()
        return

    # Səsdən çıxma halında sessiyanı yadda saxlayırıq
    if before.channel is not None and after.channel is None:
        started_at = voice_sessions.get(member.id)
        if started_at:
            seconds = int((datetime.utcnow() - started_at).total_seconds())
            if seconds > 0:
                db.add_voice_time(member.id, member.name, member.display_name, seconds)
            voice_sessions.pop(member.id, None)
        return

    # Kanal dəyişməsində heç nə etmirik, sessiya davam edir
    if before.channel is not None and after.channel is not None:
        return


@bot.event
async def on_message(message: discord.Message):
    # Botun öz mesajlarını və digər botları ignor edirik
    if message.author.bot:
        return

    # DM-də anti-spam/link tətbiq etmirik, amma command/AI işləsin
    if message.guild and isinstance(message.author, discord.Member):
        try:
            exempt = is_exempt_member(message.author)

            if ANTI_LINK_ENABLED and not exempt and is_link_message(message.content):
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.channel.send(
                    f"⚠️ {message.author.mention}, link paylaşmaq bu kanalda qadağandır.",
                    delete_after=6,
                )
                logger.info(f"Anti-link işlədi | user={message.author.id} guild={message.guild.id}")
                return

            if ANTI_SPAM_ENABLED and not exempt:
                now = datetime.utcnow()
                timestamps = spam_tracker[message.author.id]
                timestamps.append(now)

                window_start = now - timedelta(seconds=SPAM_WINDOW_SECONDS)
                while timestamps and timestamps[0] < window_start:
                    timestamps.popleft()

                if len(timestamps) >= SPAM_MESSAGE_THRESHOLD:
                    try:
                        await message.delete()
                    except Exception:
                        pass

                    timeout_until = now + timedelta(minutes=SPAM_TIMEOUT_MINUTES)
                    try:
                        await message.author.timeout(timeout_until, reason="Anti-spam: çox sürətli mesaj")
                        await message.channel.send(
                            f"🚫 {message.author.mention} spam səbəbilə {SPAM_TIMEOUT_MINUTES} dəqiqə timeout aldı.",
                            delete_after=8,
                        )
                    except Exception as error:
                        logger.warning(f"Timeout tətbiq olunmadı: {error}")
                        await message.channel.send(
                            f"⚠️ {message.author.mention}, spam aşkarlandı. Dayan, yoxsa cəza artacaq.",
                            delete_after=8,
                        )

                    timestamps.clear()
                    logger.info(f"Anti-spam işlədi | user={message.author.id} guild={message.guild.id}")
                    return
        except Exception as error:
            logger.warning(f"on_message filtr xətası: {error}")

    content_lower = message.content.lower()

    if content_lower.startswith("abi"):
        # "abi" sözündən sonrakı məzmunu ayırırıq
        user_text = message.content[3:].strip()

        # Komanda adlarını AI axınından kənarda saxlayırıq
        command_names = {
            "profil", "top", "hesabat", "sifirla", "komandalar", "seviyye", "xptop",
            "warn", "warnings", "temizle", "mute", "unmute",
            "userinfo", "serverinfo", "avatar", "poll",
        }
        first_word = user_text.split()[0].lower() if user_text else ""

        if not user_text:
            await message.reply("Nə istəyirsən?")
        elif first_word not in command_names:
            try:
                async with message.channel.typing():
                    ai_text = await ask_ai(user_text)
                await message.reply(ai_text)
            except Exception as e:
                logger.error(f"AI xətası: {e}")
                await message.reply("Bir xəta baş verdi, sonra yenə yaz.")

    # Mövcud prefix komandalarının işləməsi üçün bunu mütləq çağırırıq
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # AI handles these, ignore silently
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bu əmri istifadə etmək üçün icazən yoxdur.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Arqument çatışmır. `abi komandalar` yazıb düzgün istifadə formasına bax.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Daxil etdiyin arqument formatı yanlışdır.")
        return

    logger.exception(f"Komanda xətası: {error}")
    await ctx.send("⚠️ Gözlənilməz xəta baş verdi.")


@bot.command(name="profil")
async def profil(ctx, member: discord.Member = None):
    # İstifadəçi seçilməyibsə, əmri yazan şəxsin profilini göstəririk
    target = member or ctx.author
    user = db.get_user(target.id)

    base_total = int(user["total_seconds"]) if user else 0
    today_seconds = db.get_today(target.id)
    week_seconds = db.get_week(target.id)
    month_seconds = db.get_month(target.id)
    first_seen = user["first_seen"] if user and user.get("first_seen") else datetime.utcnow().strftime("%Y-%m-%d")

    live_seconds = get_live_seconds(target.id)
    if live_seconds > 0:
        base_total += live_seconds
        today_seconds += live_seconds
        week_seconds += live_seconds
        month_seconds += live_seconds

    combined = get_combined_totals()
    rank = None
    for index, row in enumerate(combined, start=1):
        if row["user_id"] == target.id:
            rank = index
            break

    if rank is None:
        db_rank = db.get_rank(target.id)
        rank = db_rank if db_rank is not None else 0

    avatar_url = target.display_avatar.url if target.display_avatar else discord.Embed.Empty

    embed = discord.Embed(
        title=f"🎙️ {target.display_name} — Səs Profili",
        color=0x5865F2,
    )
    embed.set_thumbnail(url=avatar_url)
    embed.add_field(name="⏱️ Ümumi Vaxt", value=format_time(base_total), inline=False)
    embed.add_field(name="🏆 Sıralama", value=f"#{rank}", inline=True)
    embed.add_field(name="📅 Bu gün", value=format_time(today_seconds), inline=True)
    embed.add_field(name="📆 Bu həftə", value=format_time(week_seconds), inline=True)
    embed.add_field(name="🗓️ Bu ay", value=format_time(month_seconds), inline=True)
    embed.set_footer(text=f"İlk qeyd: {first_seen}")

    await ctx.send(embed=embed)


@bot.command(name="top")
async def top(ctx, number: int = 10):
    # Göstəriləcək istifadəçi sayını məhdudlaşdırırıq
    if number <= 0:
        number = 10
    if number > 25:
        number = 25

    leaderboard = get_combined_totals()
    top_rows = leaderboard[:number]

    if not top_rows:
        await ctx.send("📭 Hələ statistik məlumat yoxdur.")
        return

    lines = []
    for index, row in enumerate(top_rows, start=1):
        medal = get_medal(index)
        is_live = row["user_id"] in voice_sessions
        live_prefix = "🟢 " if is_live else ""
        display_name = row.get("display_name") or row.get("username") or "Naməlum"
        total_seconds = int(row.get("total_seconds") or 0)

        lines.append(f"{medal} {live_prefix}**{display_name}** — {format_time(total_seconds)}")

    embed = discord.Embed(
        title=f"🏆 Top {number} — Səs Liderləri",
        description="\n".join(lines),
        color=0xFFD700,
    )
    embed.set_footer(text="🟢 = hal-hazırda sesdədir")

    await ctx.send(embed=embed)


@bot.command(name="hesabat")
async def hesabat(ctx, period: str = None):
    # Dövr arqumentini yoxlayırıq
    if period not in ["gun", "hefte", "ay"]:
        await ctx.send("❌ `abi hesabat gun` / `abi hesabat hefte` / `abi hesabat ay`")
        return

    rows = db.get_period_leaderboard(period, 10)
    if not rows:
        description = "📭 Bu dövr üçün statistik məlumat yoxdur."
    else:
        lines = []
        for index, row in enumerate(rows, start=1):
            medal = get_medal(index)
            display_name = row.get("display_name") or row.get("username") or "Naməlum"
            total_seconds = int(row.get("total_seconds") or 0)
            lines.append(f"{medal} **{display_name}** — {format_time(total_seconds)}")
        description = "\n".join(lines)

    period_titles = {
        "gun": "Günlük",
        "hefte": "Həftəlik",
        "ay": "Aylıq",
    }

    embed = discord.Embed(
        title=f"📊 {period_titles[period]} Hesabat",
        description=description,
        color=0x57F287,
    )

    await ctx.send(embed=embed)


@bot.command(name="sifirla")
@commands.has_permissions(administrator=True)
async def sifirla(ctx, member: discord.Member):
    # Seçilmiş istifadəçinin statistikasını sıfırlayırıq
    db.reset_user(member.id)
    await ctx.send(f"✅ {member.display_name} istifadəçisinin statistikası sıfırlandı.")


@sifirla.error
async def sifirla_error(ctx, error):
    # İcazə olmadıqda məlumat mesajı göndəririk
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Bu əmri yalnız administrator istifadə edə bilər.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ İstifadə: `abi sifirla @user`")


@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "Səbəb göstərilməyib"):
    # İstifadəçiyə xəbərdarlıq əlavə edirik
    db.upsert_user_identity(member.id, member.name, member.display_name)
    db.upsert_user_identity(ctx.author.id, ctx.author.name, ctx.author.display_name)
    db.add_warning(member.id, ctx.author.id, reason)
    logger.info(f"Warn verildi | mod={ctx.author.id} user={member.id} reason={reason}")
    await ctx.send(f"⚠️ {member.mention} üçün xəbərdarlıq qeyd edildi. Səbəb: {reason}")


@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member = None):
    # İstifadəçinin son xəbərdarlıqlarını göstəririk
    target = member or ctx.author
    rows = db.get_warnings(target.id, limit=10)

    if not rows:
        await ctx.send(f"✅ {target.display_name} üçün xəbərdarlıq qeydi yoxdur.")
        return

    lines = []
    for row in rows:
        reason = row.get("reason") or "Səbəb yoxdur"
        mod_id = row.get("moderator_id")
        date = row.get("date") or "-"
        lines.append(f"`#{row['id']}` • {date} • Mod: <@{mod_id}> • {reason}")

    embed = discord.Embed(
        title=f"⚠️ {target.display_name} — Xəbərdarlıqlar",
        description="\n".join(lines),
        color=0xFAA61A,
    )
    await ctx.send(embed=embed)


@bot.command(name="temizle")
@commands.has_permissions(manage_messages=True)
async def temizle(ctx, amount: int = 10):
    # Kanaldan mesajları toplu silirik
    amount = max(1, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount + 1)
    info = await ctx.send(f"🧹 {len(deleted) - 1} mesaj silindi.")
    await asyncio.sleep(4)
    try:
        await info.delete()
    except Exception:
        pass


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "Səbəb göstərilməyib"):
    # İstifadəçiyə timeout tətbiq edirik
    minutes = max(1, min(minutes, 40320))
    until = datetime.utcnow() + timedelta(minutes=minutes)
    await member.timeout(until, reason=f"{ctx.author} | {reason}")
    logger.info(f"Mute verildi | mod={ctx.author.id} user={member.id} min={minutes} reason={reason}")
    await ctx.send(f"🔇 {member.mention} {minutes} dəqiqəlik mute edildi. Səbəb: {reason}")


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    # Timeout-u ləğv edirik
    await member.timeout(None, reason=f"{ctx.author} tərəfindən unmute")
    logger.info(f"Unmute verildi | mod={ctx.author.id} user={member.id}")
    await ctx.send(f"🔊 {member.mention} üçün mute ləğv edildi.")


def build_progress_bar(current_xp: int, current_level: int) -> str:
    # Cari level intervalına görə 10 bloklu proqres çubuğu qururuq
    level_start = db.xp_for_level(current_level)
    level_end = db.xp_for_level(current_level + 1)
    range_xp = max(level_end - level_start, 1)
    progress_ratio = (current_xp - level_start) / range_xp
    progress_ratio = max(0.0, min(1.0, progress_ratio))

    filled = int(progress_ratio * 10)
    if filled > 10:
        filled = 10
    empty = 10 - filled
    return "█" * filled + "░" * empty


@bot.command(name="seviyye")
async def seviyye(ctx, member: discord.Member = None):
    # İstifadəçinin level və XP məlumatlarını göstəririk
    target = member or ctx.author
    user = db.get_user(target.id)

    current_level = int(user.get("level") or 1) if user else 1
    current_xp = int(user.get("xp") or 0) if user else 0
    next_level_xp = db.xp_for_level(current_level + 1)
    needed_xp = max(next_level_xp - current_xp, 0)

    progress_bar = build_progress_bar(current_xp, current_level)

    embed = discord.Embed(
        title=f"⭐ {target.display_name} — Səviyyə Profili",
        color=0x9B59B6,
    )
    embed.add_field(name="🏅 Səviyyə", value=str(current_level), inline=False)
    embed.add_field(name="✨ XP", value=f"{current_xp} / {next_level_xp} (qalıb: {needed_xp})", inline=False)
    embed.add_field(name="📊 Proqres", value=progress_bar, inline=False)

    await ctx.send(embed=embed)


@bot.command(name="xptop")
async def xptop(ctx, number: int = 10):
    # XP lider siyahısında göstəriləcək istifadəçi sayını məhdudlaşdırırıq
    if number <= 0:
        number = 10
    if number > 25:
        number = 25

    rows = db.get_level_leaderboard(number)
    if not rows:
        await ctx.send("📭 Hələ XP statistikası yoxdur.")
        return

    lines = []
    for index, row in enumerate(rows, start=1):
        medal = get_medal(index)
        display_name = row.get("display_name") or row.get("username") or "Naməlum"
        level = int(row.get("level") or 1)
        xp = int(row.get("xp") or 0)
        lines.append(f"{medal} **{display_name}** — Səviyyə {level} ({xp} XP)")

    embed = discord.Embed(
        title="⭐ XP Liderləri",
        description="\n".join(lines),
        color=0x9B59B6,
    )

    await ctx.send(embed=embed)


@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    # İstifadəçi haqqında əsas məlumatları göstəririk
    target = member or ctx.author
    created = target.created_at.strftime("%Y-%m-%d %H:%M UTC") if target.created_at else "-"
    joined = target.joined_at.strftime("%Y-%m-%d %H:%M UTC") if target.joined_at else "-"

    embed = discord.Embed(title=f"👤 {target}", color=0x3498DB)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="ID", value=str(target.id), inline=False)
    embed.add_field(name="Hesab yaradılıb", value=created, inline=True)
    embed.add_field(name="Serverə qoşulub", value=joined, inline=True)
    embed.add_field(name="Ən yüksək rol", value=target.top_role.mention, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
async def serverinfo(ctx):
    # Server haqqında əsas statistikanı göstəririk
    guild = ctx.guild
    if guild is None:
        await ctx.send("❌ Bu əmr yalnız serverdə işləyir.")
        return

    text_count = len(guild.text_channels)
    voice_count = len(guild.voice_channels)
    member_count = guild.member_count or 0

    embed = discord.Embed(title=f"🏠 {guild.name}", color=0x2ECC71)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="ID", value=str(guild.id), inline=False)
    embed.add_field(name="Üzvlər", value=str(member_count), inline=True)
    embed.add_field(name="Mətn kanalları", value=str(text_count), inline=True)
    embed.add_field(name="Səs kanalları", value=str(voice_count), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="avatar")
async def avatar(ctx, member: discord.Member = None):
    # İstifadəçi avatarını böyüdülmüş göstəririk
    target = member or ctx.author
    embed = discord.Embed(title=f"🖼️ {target.display_name} avatarı", color=0x5865F2)
    embed.set_image(url=target.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="poll")
@commands.has_permissions(manage_messages=True)
async def poll(ctx, *, text: str):
    # Sadə sorğu yaradırıq: sual | variant1 | variant2 ...
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if len(parts) < 3:
        await ctx.send("❌ İstifadə: `abi poll Sual | Variant 1 | Variant 2 [| Variant 3 ...]`")
        return

    question = parts[0]
    options = parts[1:11]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    lines = [f"{emojis[i]} {option}" for i, option in enumerate(options)]
    embed = discord.Embed(title=f"📊 {question}", description="\n".join(lines), color=0xF1C40F)
    embed.set_footer(text=f"Sorğunu başladan: {ctx.author.display_name}")

    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


@bot.command(name="komandalar")
async def komandalar(ctx):
    # Bütün əmrləri kömək bölməsində göstəririk
    embed = discord.Embed(
        title="📚 Komandalar",
        description="Bot əmrlərinin siyahısı:",
        color=0x5865F2,
    )
    embed.add_field(name="abi profil [@user]", value="Səs statistikası.", inline=False)
    embed.add_field(name="abi top [number]", value="Səs liderləri.", inline=False)
    embed.add_field(name="abi hesabat [gun/hefte/ay]", value="Period liderləri.", inline=False)
    embed.add_field(name="abi seviyye [@user]", value="Level və XP.", inline=False)
    embed.add_field(name="abi xptop [number]", value="XP liderləri.", inline=False)
    embed.add_field(name="abi warn @user [səbəb]", value="(Mod) Xəbərdarlıq verir.", inline=False)
    embed.add_field(name="abi warnings [@user]", value="Xəbərdarlıqları göstərir.", inline=False)
    embed.add_field(name="abi temizle [say]", value="(Mod) Mesajları silir.", inline=False)
    embed.add_field(name="abi mute @user [dəq] [səbəb]", value="(Mod) Timeout verir.", inline=False)
    embed.add_field(name="abi unmute @user", value="(Mod) Timeout-u açır.", inline=False)
    embed.add_field(name="abi userinfo [@user]", value="İstifadəçi məlumatı.", inline=False)
    embed.add_field(name="abi serverinfo", value="Server məlumatı.", inline=False)
    embed.add_field(name="abi avatar [@user]", value="Avatarı göstərir.", inline=False)
    embed.add_field(name="abi poll sual|v1|v2...", value="(Mod) Sorğu yaradır.", inline=False)
    embed.add_field(name="abi sifirla @user", value="(Admin) Səs statistikasını sıfırlayır.", inline=False)

    await ctx.send(embed=embed)


@tasks.loop(minutes=5)
async def xp_task():
    # Hər 5 dəqiqədə səsdə olan istifadəçilərə XP veririk
    level_channel = bot.get_channel(LEVEL_UP_CHANNEL_ID)

    for user_id in list(voice_sessions.keys()):
        if not isinstance(user_id, int):
            continue

        member = None
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                break

        if member is None or member.bot:
            continue
        if not member.voice or not member.voice.channel:
            continue

        # Anti-farm: kanalda minimum real istifadəçi sayı olmalıdır
        human_members = [m for m in member.voice.channel.members if not m.bot]
        if len(human_members) < XP_MIN_MEMBERS_IN_VOICE:
            continue

        # Anti-farm: cooldown dolmadan XP verilmir
        now = datetime.utcnow()
        last_award = last_xp_award.get(user_id)
        if last_award and (now - last_award).total_seconds() < XP_AWARD_COOLDOWN_SECONDS:
            continue

        db.upsert_user_identity(member.id, member.name, member.display_name)
        new_xp, new_level, leveled_up = db.add_xp(user_id, 10)
        last_xp_award[user_id] = now

        if leveled_up:
            logger.info(f"Level artdı | user={member.id} level={new_level} xp={new_xp}")

            # Level mükafat rolunu veririk (uyğun id varsa)
            if member.guild is not None and LEVEL_ROLE_REWARDS:
                eligible_levels = [lv for lv in LEVEL_ROLE_REWARDS.keys() if new_level >= lv]
                if eligible_levels:
                    target_level = max(eligible_levels)
                    role_id = LEVEL_ROLE_REWARDS.get(target_level)
                    role = member.guild.get_role(role_id) if role_id else None
                    if role and role not in member.roles:
                        try:
                            await member.add_roles(role, reason=f"Level reward: {new_level}")
                        except Exception as error:
                            logger.warning(f"Level rolu verilə bilmədi | user={member.id} role={role_id} err={error}")

            if level_channel is not None:
                embed = discord.Embed(
                    title="🎉 Səviyyə Yüksəldi!",
                    description=f"{member.mention} səviyyə **{new_level}**-ə çatdı!",
                    color=0xFFD700,
                )
                await level_channel.send(embed=embed)


@xp_task.before_loop
async def before_xp_task():
    # XP döngüsü başlamadan öncə botun tam hazır olmasını gözləyirik
    await bot.wait_until_ready()


@tasks.loop(hours=24)
async def daily_report():
    # Hər 24 saatdan bir günlük hesabat göndəririk
    channel = bot.get_channel(REPORT_CHANNEL_ID)
    if channel is None:
        logger.warning("Daily report kanalı tapılmadı.")
        return

    rows = db.get_period_leaderboard("gun", 10)
    if not rows:
        description = "📭 Bu gün üçün statistik məlumat yoxdur."
    else:
        lines = []
        for index, row in enumerate(rows, start=1):
            medal = get_medal(index)
            display_name = row.get("display_name") or row.get("username") or "Naməlum"
            total_seconds = int(row.get("total_seconds") or 0)
            lines.append(f"{medal} **{display_name}** — {format_time(total_seconds)}")
        description = "\n".join(lines)

    today_footer = datetime.utcnow().strftime("%d.%m.%Y")

    embed = discord.Embed(
        title="🌙 Günlük Avtomatik Hesabat",
        description=description,
        color=0xEB459E,
    )
    embed.set_footer(text=today_footer)

    await channel.send(embed=embed)


@daily_report.before_loop
async def before_daily_report():
    # Döngü başlamadan öncə botun tam hazır olmasını gözləyirik
    await bot.wait_until_ready()


bot.run(TOKEN)
