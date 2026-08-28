import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta

import discord
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database


load_dotenv()

PREFIX = "abi "
TOKEN = os.getenv("TOKEN")
REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", 0))
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", 0))
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", 0))

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


async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    # Log kanalına embed göndəririk (MOD_LOG_CHANNEL_ID təyin olunubsa)
    if not guild or not MOD_LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.warning(f"Mod log göndərilə bilmədi: {e}")


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


async def send_error_card(ctx, title: str, description: str):
    # Gözəl qırmızı/narıncı xəta kartı
    embed = discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=0xED4245,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


async def send_success_card(ctx, title: str, description: str, color: int = 0x57F287):
    # Gözəl yaşıl uğur kartı
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"İcra edən: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


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
        embed = discord.Embed(
            description=f"🎙️ {member.mention} **{after.channel.name}** səs kanalına qoşuldu.",
            color=0x2ECC71,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await send_mod_log(member.guild, embed)
        return

    # Səsdən çıxma halında sessiyanı yadda saxlayırıq
    if before.channel is not None and after.channel is None:
        started_at = voice_sessions.get(member.id)
        seconds = 0
        if started_at:
            seconds = int((datetime.utcnow() - started_at).total_seconds())
            if seconds > 0:
                db.add_voice_time(member.id, member.name, member.display_name, seconds)
            voice_sessions.pop(member.id, None)

        embed = discord.Embed(
            description=f"🚪 {member.mention} **{before.channel.name}** səs kanalından ayrıldı." + (f" (Müddət: {format_time(seconds)})" if seconds > 0 else ""),
            color=0xE74C3C,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await send_mod_log(member.guild, embed)
        return

    # Kanal dəyişməsi
    if before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        embed = discord.Embed(
            description=f"🔄 {member.mention} kanal dəyişdi: **{before.channel.name}** ➔ **{after.channel.name}**",
            color=0x3498DB,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await send_mod_log(member.guild, embed)
        return


@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    embed = discord.Embed(
        title="🗑️ Mesaj Silindi",
        description=f"**Müəllif:** {message.author.mention} (`{message.author.id}`)\n**Kanal:** {message.channel.mention}\n**Məzmun:**\n{message.content or '*[Mətn yoxdur / Fayl və ya Embed]*'}",
        color=0xE74C3C,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
    await send_mod_log(message.guild, embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return

    embed = discord.Embed(
        title="✏️ Mesaj Redaktə Edildi",
        description=f"**Müəllif:** {before.author.mention} (`{before.author.id}`)\n**Kanal:** {before.channel.mention}\n[Mesaja keçid]({after.jump_url})\n\n**Əvvəl:**\n{before.content or '*[Boş]*'}\n\n**Sonra:**\n{after.content or '*[Boş]*'}",
        color=0xF1C40F,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
    await send_mod_log(before.guild, embed)



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

    # Mövcud prefix komandalarının işləməsi üçün bunu mütləq çağırırıq
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await send_error_card(ctx, "İcazə Çatışmır", "Bu əmri istifadə etmək üçün tələb olunan yetkiniz yoxdur.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await send_error_card(ctx, "Çatışmayan Arqument", f"`{error.param.name}` parametri daxil edilməyib.\nDüzgün istifadə üçün: `abi komandalar`")
        return
    if isinstance(error, commands.BadArgument):
        await send_error_card(ctx, "Yanlış Format", "Daxil etdiyiniz parametr və ya istifadəçi formatı yanlışdır.")
        return

    logger.exception(f"Komanda xətası: {error}")
    await send_error_card(ctx, "Xəta Baş Verdi", "Əmr icra edilərkən gözlənilməz xəta yarandı.")


@bot.command(name="profil")
async def profil(ctx, member: discord.Member = None):
    # İstifadəçi seçilməyibsə, əmri yazan şəxsin profilini göstəririk
    target = member or ctx.author
    user = db.get_user(target.id)

    base_total = int(user["total_seconds"]) if user else 0
    today_seconds = db.get_today(target.id)
    week_seconds = db.get_week(target.id)
    month_seconds = db.get_month(target.id)
    first_seen = user["first_seen"] if user and user.get("first_seen") else datetime.utcnow().strftime("%d.%m.%Y")

    live_seconds = get_live_seconds(target.id)
    is_in_voice = live_seconds > 0
    if is_in_voice:
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
        rank = db_rank if db_rank is not None else "—"

    status_str = "🟢 Hal-hazırda səsdədir" if is_in_voice else "⚪ Səsdə deyil"
    avatar_url = target.display_avatar.url if target.display_avatar else discord.Embed.Empty

    embed = discord.Embed(
        title=f"🎙️ Səs Statistikası — {target.display_name}",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=avatar_url)
    embed.description = f"**Status:** `{status_str}`\n**Səs Sıralaması:** `🏆 #{rank}`"
    
    embed.add_field(
        name="⏱️ Ümumi Aktivlik",
        value=f"```fix\n{format_time(base_total)}\n```",
        inline=False
    )
    embed.add_field(name="📅 Bu gün", value=f"⏱️ `{format_time(today_seconds)}`", inline=True)
    embed.add_field(name="📆 Bu həftə", value=f"⏱️ `{format_time(week_seconds)}`", inline=True)
    embed.add_field(name="🗓️ Bu ay", value=f"⏱️ `{format_time(month_seconds)}`", inline=True)
    
    embed.set_footer(text=f"İlk aktivlik: {first_seen} • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

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
        embed = discord.Embed(
            description="📭 Hələ heç bir səs statistikası qeydə alınmayıb.",
            color=0xFEE75C
        )
        await ctx.send(embed=embed)
        return

    lines = []
    for index, row in enumerate(top_rows, start=1):
        medal = get_medal(index)
        is_live = row["user_id"] in voice_sessions
        live_dot = "🟢 " if is_live else ""
        display_name = row.get("display_name") or row.get("username") or "Naməlum"
        total_seconds = int(row.get("total_seconds") or 0)

        lines.append(f"{medal} {live_dot}**{display_name}** ➔ `{format_time(total_seconds)}`")

    embed = discord.Embed(
        title=f"🏆 Səs Liderləri Top {len(top_rows)}",
        description="\n".join(lines),
        color=0xFEE75C,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="🟢 = Hal-hazırda səsdədir • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

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
    await send_success_card(
        ctx,
        "Statistika Sıfırlandı",
        f"✅ {member.mention} (`{member.id}`) istifadəçisinin bütün səs aktivliyi və qeydləri sıfırlandı."
    )


@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "Səbəb göstərilməyib"):
    # İstifadəçiyə xəbərdarlıq əlavə edirik
    db.upsert_user_identity(member.id, member.name, member.display_name)
    db.upsert_user_identity(ctx.author.id, ctx.author.name, ctx.author.display_name)
    db.add_warning(member.id, ctx.author.id, reason)
    logger.info(f"Warn verildi | mod={ctx.author.id} user={member.id} reason={reason}")

    embed = discord.Embed(
        title="⚠️ Xəbərdarlıq Verildi",
        description=f"**Cəzalandırılan:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
        color=0xFEE75C,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text="Xəbərdarlıq qeydə alındı • abi-bot")
    await ctx.send(embed=embed)
    await send_mod_log(ctx.guild, embed)


@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member = None):
    # İstifadəçinin son xəbərdarlıqlarını göstəririk
    target = member or ctx.author
    rows = db.get_warnings(target.id, limit=10)

    if not rows:
        embed = discord.Embed(
            title=f"✅ {target.display_name} — Xəbərdarlıqlar",
            description="Bu istifadəçi üçün heç bir xəbərdarlıq qeydə alınmayıb.",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)
        return

    lines = []
    for row in rows:
        reason = row.get("reason") or "Səbəb yoxdur"
        mod_id = row.get("moderator_id")
        date = row.get("date") or "-"
        lines.append(f"• `#{row['id']}` `[{date}]` — Mod: <@{mod_id}>\n  └ **Səbəb:** {reason}")

    embed = discord.Embed(
        title=f"⚠️ {target.display_name} — Xəbərdarlıq Tarixçəsi",
        description="\n\n".join(lines),
        color=0xFAA61A,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"Cəmi {len(rows)} xəbərdarlıq göstərilir • abi-bot")
    await ctx.send(embed=embed)


@bot.command(name="temizle")
@commands.has_permissions(manage_messages=True)
async def temizle(ctx, amount: int = 10):
    # Kanaldan mesajları toplu silirik
    amount = max(1, min(amount, 100))
    deleted = await ctx.channel.purge(limit=amount + 1)
    
    embed = discord.Embed(
        title="🧹 Mesajlar Təmizləndi",
        description=f"**Kanal:** {ctx.channel.mention}\n**Silinən Mesaj Sayı:** `{len(deleted) - 1}` ədəd",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"İcra edən: {ctx.author.display_name}")
    info = await ctx.send(embed=embed)
    await send_mod_log(ctx.guild, embed)

    await asyncio.sleep(4)
    try:
        await info.delete()
    except Exception:
        pass


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "Səbəb göstərilməyib"):
    # İstifadəçiyə timeout tətbiq edirik
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await send_error_card(ctx, "İcazə Yoxdur", "Səninlə eyni və ya daha yüksək rolda olan istifadəçiyə timeout verə bilməzsən.")
        return

    minutes = max(1, min(minutes, 40320))
    until = datetime.utcnow() + timedelta(minutes=minutes)
    try:
        await member.timeout(until, reason=f"{ctx.author} | {reason}")
        logger.info(f"Mute verildi | mod={ctx.author.id} user={member.id} min={minutes} reason={reason}")

        embed = discord.Embed(
            title="🔇 İstifadəçi Mute Edildi (Timeout)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Müddət:** `{minutes} dəqiqə`\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0xE67E22,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_footer(text=f"Müddət bitmə vaxtı: {(until).strftime('%H:%M:%S UTC')} • abi-bot")
        await ctx.send(embed=embed)
        await send_mod_log(ctx.guild, embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçiyə timeout verməyə səlahiyyəti çatmır (Rol iyerarxiyasını yoxlayın).")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    # Timeout-u ləğv edirik
    try:
        await member.timeout(None, reason=f"{ctx.author} tərəfindən unmute")
        logger.info(f"Unmute verildi | mod={ctx.author.id} user={member.id}")

        embed = discord.Embed(
            title="🔊 Timeout Qaldırıldı (Unmute)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)
        await send_mod_log(ctx.guild, embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçinin səsini/timeout-unu açmağa səlahiyyəti çatmır.")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "Səbəb göstərilməyib"):
    # Serverdən istifadəçini kənarlaşdırırıq
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await send_error_card(ctx, "İcazə Yoxdur", "Səninlə eyni və ya daha yüksək rolda olan istifadəçini serverdən ata bilməzsən.")
        return

    try:
        await member.kick(reason=f"{ctx.author} | {reason}")
        logger.info(f"Kick olundu | mod={ctx.author.id} user={member.id} reason={reason}")

        embed = discord.Embed(
            title="👢 Üzv Serverdən Atıldı (Kick)",
            description=f"**Atılan Üzv:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0xE67E22,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)
        await send_mod_log(ctx.guild, embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçini atmağa (kick) icazəsi yoxdur (Rol iyerarxiyasını yoxlayın).")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "Səbəb göstərilməyib"):
    # Serverdən istifadəçini ban edirik
    if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await send_error_card(ctx, "İcazə Yoxdur", "Səninlə eyni və ya daha yüksək rolda olan istifadəçini ban edə bilməzsən.")
        return

    try:
        await member.ban(reason=f"{ctx.author} | {reason}", delete_message_days=0)
        logger.info(f"Ban olundu | mod={ctx.author.id} user={member.id} reason={reason}")

        embed = discord.Embed(
            title="🔨 Üzv Ban Edildi",
            description=f"**Banlanan Üzv:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0xED4245,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)
        await send_mod_log(ctx.guild, embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçini ban etməyə səlahiyyəti çatmır (Rol iyerarxiyasında botun rolu daha aşağıdadır).")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason: str = "Səbəb göstərilməyib"):
    # Banı açırıq
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author} | {reason}")
        logger.info(f"Unban olundu | mod={ctx.author.id} user={user_id} reason={reason}")

        embed = discord.Embed(
            title="🔓 Ban Qaldırıldı",
            description=f"**İstifadəçi:** **{user}** (`{user_id}`)\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        await ctx.send(embed=embed)
        await send_mod_log(ctx.guild, embed)
    except discord.NotFound:
        await send_error_card(ctx, "Tapılmadı", "Bu ID-yə uyğun istifadəçi tapılmadı və ya ban siyahısında deyil.")
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun ban qaldırmağa (unban) səlahiyyəti çatmır.")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")



@bot.event
async def on_member_join(member: discord.Member):
    # Yeni üzv qatıldıqda loglayırıq
    embed = discord.Embed(
        title="📥 Üzv Qatıldı",
        description=f"{member.mention} (`{member.id}`) serverə daxil oldu.\n**Hesab yaranma tarixi:** {member.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    await send_mod_log(member.guild, embed)


@bot.event
async def on_member_remove(member: discord.Member):
    # Üzv serverdən ayrıldıqda və ya atıldıqda loglayırıq
    embed = discord.Embed(
        title="📤 Üzv Ayrıldı",
        description=f"**{member}** (`{member.id}`) serverdən ayrıldı.",
        color=0xED4245,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    await send_mod_log(member.guild, embed)




def build_progress_bar(current_xp: int, current_level: int) -> str:
    # Cari level intervalına görə müasir proqres çubuğu qururuq
    level_start = db.xp_for_level(current_level)
    level_end = db.xp_for_level(current_level + 1)
    range_xp = max(level_end - level_start, 1)
    progress_ratio = (current_xp - level_start) / range_xp
    progress_ratio = max(0.0, min(1.0, progress_ratio))

    total_blocks = 12
    filled = int(progress_ratio * total_blocks)
    filled = max(0, min(total_blocks, filled))
    empty = total_blocks - filled
    percent = int(progress_ratio * 100)
    return f"`[{'■' * filled}{'□' * empty}]` **{percent}%**"


@bot.command(name="seviyye")
async def seviyye(ctx, member: discord.Member = None):
    # İstifadəçinin level və XP məlumatlarını göstəririk
    target = member or ctx.author
    user = db.get_user(target.id)

    current_level = int(user.get("level") or 1) if user else 1
    current_xp = int(user.get("xp") or 0) if user else 0
    next_level_xp = db.xp_for_level(current_level + 1)
    level_start_xp = db.xp_for_level(current_level)
    needed_xp = max(next_level_xp - current_xp, 0)

    progress_bar = build_progress_bar(current_xp, current_level)

    embed = discord.Embed(
        title=f"⭐ Səviyyə Profili — {target.display_name}",
        color=0x9B59B6,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.description = f"**İstifadəçi:** {target.mention}\n**Cari Səviyyə:** `🏅 Səviyyə {current_level}`"

    embed.add_field(
        name="✨ Təcrübə (XP)",
        value=f"```yaml\nCari XP: {current_xp} / {next_level_xp}\nNövbəti səviyyəyə: {needed_xp} XP qaldı\n```",
        inline=False
    )
    embed.add_field(name="📈 Səviyyə İrəliləyişi", value=progress_bar, inline=False)
    embed.set_footer(text="Hər 5 dəqiqə səsdə qalmağa 10 XP verilir • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

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
        embed = discord.Embed(
            description="📭 Hələ heç bir XP qeydi mövcud deyil.",
            color=0x9B59B6
        )
        await ctx.send(embed=embed)
        return

    lines = []
    for index, row in enumerate(rows, start=1):
        medal = get_medal(index)
        display_name = row.get("display_name") or row.get("username") or "Naməlum"
        level = int(row.get("level") or 1)
        xp = int(row.get("xp") or 0)
        lines.append(f"{medal} **{display_name}** ➔ `Lv.{level}` • `{xp:,} XP`")

    embed = discord.Embed(
        title=f"⭐ XP & Səviyyə Liderləri Top {len(rows)}",
        description="\n".join(lines),
        color=0x9B59B6,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Səsdə qalaraq səviyyənizi artırın • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await ctx.send(embed=embed)


@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    # İstifadəçi haqqında əsas məlumatları göstəririk
    target = member or ctx.author
    created = target.created_at.strftime("%d.%m.%Y • %H:%M UTC") if target.created_at else "-"
    joined = target.joined_at.strftime("%d.%m.%Y • %H:%M UTC") if target.joined_at else "-"
    roles = [r.mention for r in target.roles if r.name != "@everyone"]
    roles_str = ", ".join(roles[:8]) if roles else "Rol yoxdur"
    if len(roles) > 8:
        roles_str += f" (+{len(roles)-8} rol)"

    embed = discord.Embed(
        title=f"👤 İstifadəçi Məlumatı — {target.display_name}",
        color=0x3498DB,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏷️ Tag", value=f"`{target}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{target.id}`", inline=True)
    embed.add_field(name="👑 Ən Yüksək Rol", value=target.top_role.mention, inline=True)
    embed.add_field(name="📅 Qeydiyyat Tarixi", value=f"`{created}`", inline=True)
    embed.add_field(name="📥 Serverə Qoşuldu", value=f"`{joined}`", inline=True)
    embed.add_field(name="🎭 Bütün Rollar", value=roles_str, inline=False)
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name} • abi-bot", icon_url=ctx.author.display_avatar.url)
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
    category_count = len(guild.categories)
    member_count = guild.member_count or 0
    created = guild.created_at.strftime("%d.%m.%Y • %H:%M UTC") if guild.created_at else "-"
    owner = guild.owner.mention if guild.owner else "Naməlum"

    embed = discord.Embed(
        title=f"🏠 Server Məlumatı — {guild.name}",
        color=0x2ECC71,
        timestamp=datetime.utcnow()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    embed.add_field(name="👑 Server Sahibi", value=owner, inline=True)
    embed.add_field(name="🆔 Server ID", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="👥 Ümumi Üzvlər", value=f"`{member_count:,}` üzv", inline=True)
    embed.add_field(name="💬 Mətn Kanalları", value=f"`{text_count}` kanal", inline=True)
    embed.add_field(name="🎙️ Səs Kanalları", value=f"`{voice_count}` kanal", inline=True)
    embed.add_field(name="📁 Kateqoriyalar", value=f"`{category_count}` kateqoriya", inline=True)
    embed.add_field(name="📅 Yaranma Tarixi", value=f"`{created}`", inline=False)
    
    embed.set_footer(text=f"Server: {guild.name} • abi-bot", icon_url=guild.icon.url if guild.icon else None)
    await ctx.send(embed=embed)


@bot.command(name="avatar")
async def avatar(ctx, member: discord.Member = None):
    # İstifadəçi avatarını böyüdülmüş göstəririk
    target = member or ctx.author
    embed = discord.Embed(
        title=f"🖼️ {target.display_name} — Avatar",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
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

    lines = [f"{emojis[i]} **{option}**" for i, option in enumerate(options)]
    embed = discord.Embed(
        title=f"📊 Sorğu: {question}",
        description="\n\n".join(lines),
        color=0xF1C40F,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Sorğunu başlatdı: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


@bot.command(name="komandalar")
async def komandalar(ctx):
    # Bütün əmrləri kateqoriyalarla gözəl menyuda göstəririk
    embed = discord.Embed(
        title="🤖 abi-bot — Komanda Bələdçisi",
        description="Botun bütün əmrləri aşağıdakı kateqoriyalara bölünüb.\nƏmrlərin önünə **`abi `** prefix-ini yazın.",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="🎙️ Səs & Aktivlik Statistikası",
        value=(
            "• `abi profil [@user]` — Səs aktivliyi və sıralama profili\n"
            "• `abi top [say]` — Ən çox səsdə qalanların ümumi lider cədvəli\n"
            "• `abi hesabat [gun/hefte/ay]` — Periodik səs hesabatı"
        ),
        inline=False
    )

    embed.add_field(
        name="⭐ Səviyyə & XP Sistemi",
        value=(
            "• `abi seviyye [@user]` — Level, XP kartı və tərəqqi çubuğu\n"
            "• `abi xptop [say]` — Ən yüksək səviyyəli üzvlər"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ Moderasiya & Təhlükəsizlik",
        value=(
            "• `abi warn @user [səbəb]` — İstifadəçiyə xəbərdarlıq qeyd edir\n"
            "• `abi warnings [@user]` — Xəbərdarlıq tarixçəsini göstərir\n"
            "• `abi mute @user [dəq] [səbəb]` — Timeout (səs/yazı kəsmə)\n"
            "• `abi unmute @user` — Timeout-u vaxtından əvvəl qaldırır\n"
            "• `abi kick @user [səbəb]` — İstifadəçini serverdən atır\n"
            "• `abi ban @user [səbəb]` — İstifadəçini serverdən qadağan edir\n"
            "• `abi unban [ID] [səbəb]` — İstifadəçinin banını açır\n"
            "• `abi temizle [say]` — Kanaldakı mesajları toplu silir"
        ),
        inline=False
    )

    embed.add_field(
        name="🧰 Köməkçi & Digər Əmrlər",
        value=(
            "• `abi userinfo [@user]` — İstifadəçi haqqında detallı məlumat\n"
            "• `abi serverinfo` — Serverin ümumi statistikası\n"
            "• `abi avatar [@user]` — Böyüdülmüş profil şəkli\n"
            "• `abi poll Sual | V1 | V2` — İnteraktiv səsvermə sorğusu\n"
            "• `abi sifirla @user` — *(Admin)* Səs statistikasını sıfırlayır"
        ),
        inline=False
    )

    embed.set_footer(text="Developed for your server • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

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


async def handle_ping(request):
    # Render/UptimeRobot üçün keep-alive endpoint
    return web.Response(text="Bot 7/24 aktivdir!", content_type="text/plain")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Keep-alive veb server port {port}-də başladı.")


async def main():
    await start_web_server()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN tapılmadı! Zəhmət olmasa .env faylında TOKEN qeyd edin.")
    else:
        asyncio.run(main())

