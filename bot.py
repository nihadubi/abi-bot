import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database


load_dotenv()

PREFIX = "abi "
TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", 0))
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", 0))
BASE_DIR = Path(__file__).resolve().parent

# Moderasiya və anti-spam ayarları
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

BRAND_COLOR = 0x5865F2
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245


def truncate_text(value: str, limit: int = 900) -> str:
    """Discord embed sahələrini oxunaqlı və limit daxilində saxlayır."""
    value = value or ""
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


def build_audit_embed(title: str, color: int, member: discord.abc.User):
    embed = discord.Embed(title=title, color=color, timestamp=datetime.utcnow())
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text="abi-bot • Audit Log")
    return embed


async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    # Log kanalına embed göndəririk (Server tənzimləməsi və ya MOD_LOG_CHANNEL_ID əsasında)
    if not guild:
        return

    # İlk öncə bazadakı dinamik quraşdırmanı yoxlayırıq
    target_channel_id = db.get_guild_setting(guild.id, "mod_log_channel")
    if target_channel_id:
        try:
            target_channel_id = int(target_channel_id)
        except ValueError:
            target_channel_id = None

    # Əgər bazada yoxdursa, .env-dəki ümumi ID-yə baxırıq
    if not target_channel_id:
        target_channel_id = MOD_LOG_CHANNEL_ID

    if not target_channel_id:
        return

    channel = guild.get_channel(target_channel_id)
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


async def send_error_card(ctx, title: str, description: str):
    # Gözəl qırmızı/narıncı xəta kartı
    embed = discord.Embed(
        title=f"❌ {title}",
        description=description,
        color=ERROR_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


async def send_success_card(ctx, title: str, description: str, color: int = SUCCESS_COLOR):
    # Gözəl yaşıl uğur kartı
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"İcra edən: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


COMMAND_GUIDANCE = {
    "warn": ("`abi warn @istifadəçi [səbəb]`", "`abi warn @Nihad Spam paylaşdı`"),
    "warnings": ("`abi warnings [@istifadəçi]`", "`abi warnings @Nihad`"),
    "warnlar": ("`abi warnlar [say]`", "`abi warnlar 10`"),
    "delwarn": ("`abi delwarn [warn ID]`", "`abi delwarn 12`"),
    "clearwarn": ("`abi clearwarn @istifadəçi`", "`abi clearwarn @Nihad`"),
    "sil": ("`abi sil [say]`", "`abi sil 25`"),
    "temizle": ("`abi sil [say]`", "`abi sil 25`"),
    "mute": ("`abi mute @istifadəçi [dəqiqə] [səbəb]`", "`abi mute @Nihad 30 Təhqir`"),
    "unmute": ("`abi unmute @istifadəçi`", "`abi unmute @Nihad`"),
    "kick": ("`abi kick @istifadəçi [səbəb]`", "`abi kick @Nihad Qaydaları pozdu`"),
    "ban": ("`abi ban @istifadəçi [səbəb]`", "`abi ban @Nihad Təkrar qayda pozuntusu`"),
    "unban": ("`abi unban [istifadəçi ID] [səbəb]`", "`abi unban 123456789012345678 Səhv ban`"),
    "sifirla": ("`abi sifirla @istifadəçi`", "`abi sifirla @Nihad`"),
    "setlog": ("`abi setlog #kanal`", "`abi setlog #mod-log`"),
    "adminkomandalar": ("`abi adminkomandalar [əmr]`", "`abi adminkomandalar warn`"),
    "poll": ("`abi poll Sual | Variant 1 | Variant 2`", "`abi poll Hansı oyun? | CS2 | Valorant`"),
    "hesabat": ("`abi hesabat [gun/hefte/ay]`", "`abi hesabat hefte`"),
}


def get_command_guidance(command_name: str):
    return COMMAND_GUIDANCE.get(command_name, (f"`abi {command_name}`", "`abi komandalar`"))


PERMISSION_LABELS = {
    "administrator": "Administrator",
    "manage_messages": "Mesajları idarə et",
    "moderate_members": "Üzvləri moderasiya et",
    "kick_members": "Üzvləri at",
    "ban_members": "Üzvləri ban et",
}


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
        activity=discord.CustomActivity(
            name="/komandalar"
        ),
        status=discord.Status.online
    )

    if not xp_task.is_running():
        xp_task.start()

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"{len(synced)} Slash komandası {GUILD_ID} serveri üçün sinxronlaşdırıldı.")
        else:
            synced = await bot.tree.sync()
            logger.info(f"{len(synced)} Slash komandası Discord ilə qlobal sinxronlaşdırıldı.")
    except Exception as e:
        logger.error(f"Slash komandaları sinxronlaşdırılmadı: {e}")

    print(f"{bot.user} olaraq daxil olundu.")


@bot.event
async def on_voice_state_update(member, before, after):
    # Botları izləmədən çıxırıq
    if member.bot:
        return

    # Səsə qoşulma halında sessiyanı başladırıq
    if before.channel is None and after.channel is not None:
        voice_sessions[member.id] = datetime.utcnow()
        embed = build_audit_embed("🎙️ Səs Kanalına Qoşuldu", SUCCESS_COLOR, member)
        embed.add_field(name="Üzv", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Kanal", value=after.channel.mention, inline=True)
        embed.add_field(name="Hadisə", value="Səs kanalına qoşuldu", inline=False)
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

        embed = build_audit_embed("🚪 Səs Kanalından Ayrıldı", ERROR_COLOR, member)
        embed.add_field(name="Üzv", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Kanal", value=before.channel.mention, inline=True)
        embed.add_field(name="Sessiya Müddəti", value=f"`{format_time(seconds)}`" if seconds > 0 else "Qeyd olunmayıb", inline=False)
        await send_mod_log(member.guild, embed)
        return

    # Kanal dəyişməsi
    if before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        embed = build_audit_embed("🔄 Səs Kanalı Dəyişdi", BRAND_COLOR, member)
        embed.add_field(name="Üzv", value=f"{member.mention}\n`{member.id}`", inline=False)
        embed.add_field(name="Əvvəlki Kanal", value=before.channel.mention, inline=True)
        embed.add_field(name="Yeni Kanal", value=after.channel.mention, inline=True)
        await send_mod_log(member.guild, embed)
        return


async def log_deleted_message(message: discord.Message, bulk_delete: bool = False):
    """Tək və ya toplu silinən bütün mesajları audit kanalına yazır."""
    if not message.guild:
        return

    embed = build_audit_embed("🗑️ Mesaj Silindi", ERROR_COLOR, message.author)
    embed.add_field(name="Müəllif", value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
    embed.add_field(name="Kanal", value=message.channel.mention, inline=True)
    embed.add_field(
        name="Məzmun",
        value=truncate_text(message.content or "[Mətn yoxdur — fayl və ya embed]"),
        inline=False,
    )
    if bulk_delete:
        embed.add_field(name="Silmə Növü", value="Toplu təmizləmə", inline=True)
    embed.set_footer(text=f"abi-bot • Audit Log • Message ID: {message.id}")
    await send_mod_log(message.guild, embed)


@bot.event
async def on_message_delete(message: discord.Message):
    await log_deleted_message(message)


@bot.event
async def on_bulk_message_delete(messages):
    # `abi sil` kimi purge əməliyyatları tək-tək deyil, bu event ilə gəlir.
    for message in messages:
        await log_deleted_message(message, bulk_delete=True)


async def log_uncached_delete(guild_id: int, channel_id: int, message_id: int, bulk_delete: bool = False):
    """Cache-də olmayan silinmiş mesajın mövcud metadata-sını loglayır."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    channel = guild.get_channel(channel_id)
    channel_value = channel.mention if channel else f"`{channel_id}`"
    embed = discord.Embed(title="🗑️ Mesaj Silindi", color=ERROR_COLOR, timestamp=datetime.utcnow())
    embed.add_field(name="Kanal", value=channel_value, inline=True)
    embed.add_field(name="Mesaj ID", value=f"`{message_id}`", inline=True)
    embed.add_field(
        name="Məzmun",
        value="[Mesaj cache-də olmadığı üçün məzmun və müəllif mövcud deyil]",
        inline=False,
    )
    if bulk_delete:
        embed.add_field(name="Silmə Növü", value="Toplu təmizləmə", inline=True)
    embed.set_footer(text="abi-bot • Audit Log")
    await send_mod_log(guild, embed)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # Cache-də olan mesajlar artıq on_message_delete ilə yazılır.
    if payload.guild_id and payload.cached_message is None:
        await log_uncached_delete(payload.guild_id, payload.channel_id, payload.message_id)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    # Cache-də olmayanları ayrıca yazırıq ki, toplu silinmədə heç bir ID itirilməsin.
    cached_ids = {message.id for message in payload.cached_messages}
    if payload.guild_id:
        for message_id in payload.message_ids - cached_ids:
            await log_uncached_delete(payload.guild_id, payload.channel_id, message_id, bulk_delete=True)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild:
        return
    if before.content == after.content:
        return

    embed = build_audit_embed("✏️ Mesaj Redaktə Edildi", WARNING_COLOR, before.author)
    embed.add_field(name="Müəllif", value=f"{before.author.mention}\n`{before.author.id}`", inline=True)
    embed.add_field(name="Kanal", value=before.channel.mention, inline=True)
    embed.add_field(name="Əvvəl", value=truncate_text(before.content or "[Boş]", 700), inline=False)
    embed.add_field(name="Sonra", value=truncate_text(after.content or "[Boş]", 700), inline=False)
    embed.add_field(name="Mesaja Keçid", value=f"[Mesajı aç]({after.jump_url})", inline=False)
    embed.set_footer(text=f"abi-bot • Audit Log • Message ID: {after.id}")
    await send_mod_log(before.guild, embed)



@bot.event
async def on_message(message: discord.Message):
    # Botun öz mesajlarını və digər botları ignor edirik
    if message.author.bot:
        return

    # DM-də anti-spam tətbiq etmirik, amma command/AI işləsin
    if message.guild and isinstance(message.author, discord.Member):
        try:
            exempt = is_exempt_member(message.author)

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

                    timeout_until = discord.utils.utcnow() + timedelta(minutes=SPAM_TIMEOUT_MINUTES)
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

    # İnteraktiv və mehriban mesaj cavabları
    clean_text = message.content.strip().lower()
    # Həm "ə", həm "e", hərf dəyişiklikləri və durğu işarələrini nəzərə alırıq
    norm_text = clean_text.replace("ə", "e").replace("ı", "i").replace("ü", "u").replace("ö", "o").replace("ğ", "g").replace("ç", "c").replace("ş", "s")
    norm_text = re.sub(r"[?!.,/\\@#_~-]+", "", norm_text).strip()

    # Xüsusi interaktiv dialoqlar
    if norm_text in ["abi necesen", "abi necəsən", "abi netersen", "abi nətərsən", "abi keyfler nece", "abi keyflər necə"]:
        replies = [
            f"Sağ ol, {message.author.mention}! Bomba kimiyəm, sən necəsən? 😎",
            f"Şükür yaxşılıqdır, {message.author.mention}! Sən nə var nə yox? 🎙️",
            f"Serverin keşiyindəyəm, hər şey əladır! Sən necəsən? 🔥"
        ]
        import random
        await message.reply(random.choice(replies))
        return

    if norm_text in ["abi salam", "salam abi", "selam abi", "abi selam"]:
        await message.reply(f"Salam aleykum, {message.author.mention}! Xoş gördük 👋")
        return

    if norm_text == "abi zibzib":
        await message.reply("https://www.youtube.com/watch?v=DBhs676nka4")
        return

    if norm_text in ["abi ne var ne yox", "abi nə var nə yox", "abi nava nox"]:
        await message.reply("Hər şey qaydasındadır, səs kanallarına nəzarət edirəm! Səndə nə xəbər? 🎧")
        return

    if norm_text in ["abi sag ol", "abi sağ ol", "abi cox sag ol", "abi çox sağ ol", "twk abi", "təsəkkür abi", "tesekkur abi"]:
        await message.reply(f"Dəyməz, {message.author.mention}, hər zaman xidmətindəyəm! 🫡")
        return

    if norm_text in ["abi sevirsen meni", "abi məni sevirsən", "abi meni sevirsen"]:
        await message.reply("Əlbəttə, sən bizim serverin ən dəyərli üzvüsən! ❤️")
        return

    if norm_text in [
        "abi caldirir misin", "abi çaldırır mısın", "abi caldirirmisin", "abi çaldırırmısın",
        "abi caldirirsiniz", "abi caldirirsan", "abi çaldırırsan", "caldirir misin abi"
    ]:
        caldir_replies = [
            f"Çaldırmaq sakso deməkdir, {message.author.mention}... Nə saksosudur bu belə? 🤨🎷",
            f"Ayıb olsun sənə {message.author.mention}, mən serverin abisiyəm, sən nə təklif edirsən? 😳",
            f"Açığı çaldırmaq sakso deməkdir axı... Özünə gəl, {message.author.mention}! 😂🎷"
        ]
        import random
        await message.reply(random.choice(caldir_replies))
        return

    # Mövcud prefix komandalarının işləməsi üçün bunu mütləq çağırırıq
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        required = ", ".join(
            PERMISSION_LABELS.get(permission, permission.replace("_", " ").title())
            for permission in error.missing_permissions
        )
        await send_error_card(
            ctx,
            "İcazə çatışmır",
            f"Bu əmri istifadə etmək üçün bu icazə lazımdır: **{required}**.",
        )
        return
    if isinstance(error, commands.MissingRequiredArgument):
        command_name = ctx.command.qualified_name if ctx.command else "komanda"
        usage, example = get_command_guidance(command_name)
        await send_error_card(
            ctx,
            f"{command_name.title()} üçün məlumat çatışmır",
            (
                f"**`{error.param.name}`** hissəsini yazmamısınız.\n\n"
                f"**Düzgün istifadə:** {usage}\n"
                f"**Nümunə:** {example}\n\n"
                "Ətraflı siyahı üçün: `abi komandalar`"
            ),
        )
        return
    if isinstance(error, commands.BadArgument):
        command_name = ctx.command.qualified_name if ctx.command else "komanda"
        usage, example = get_command_guidance(command_name)
        await send_error_card(
            ctx,
            "Yanlış format",
            (
                "İstifadəçi, kanal, ID və ya rəqəm formatı düzgün deyil.\n\n"
                f"**Düzgün istifadə:** {usage}\n"
                f"**Nümunə:** {example}"
            ),
        )
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
    total_warns = db.get_warning_count(member.id)
    logger.info(f"Warn verildi | mod={ctx.author.id} user={member.id} count={total_warns} reason={reason}")

    # Avtomatik cəza sistemi (Auto-Escalation)
    auto_punishment_note = ""
    if total_warns == 3:
        timeout_until = discord.utils.utcnow() + timedelta(hours=1)
        try:
            await member.timeout(timeout_until, reason="3 xəbərdarlığa çatdı (Avtomatik 1 saat Mute)")
            auto_punishment_note = "\n\n⚠️ **Avtomatik Cəza:** İstifadəçi 3 xəbərdarlığa çatdığı üçün **1 saatlıq Mute (Timeout)** edildi!"
        except Exception:
            pass
    elif total_warns >= 5:
        timeout_until = discord.utils.utcnow() + timedelta(days=1)
        try:
            await member.timeout(timeout_until, reason="5 və ya daha çox xəbərdarlıq (Avtomatik 24 saat Mute)")
            auto_punishment_note = f"\n\n🚨 **Avtomatik Cəza:** İstifadəçi {total_warns} xəbərdarlığa çatdığı üçün **24 saatlıq Mute (Timeout)** edildi!"
        except Exception:
            pass

    embed = discord.Embed(
        title="⚠️ Xəbərdarlıq (Warn) Verildi",
        description=f"**Cəzalandırılan:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Ümumi Xəbərdarlıq Sayı:** `{total_warns}`{auto_punishment_note}\n**Moderator:** {ctx.author.mention}",
        color=0xFEE75C if total_warns < 3 else 0xED4245,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text=f"Ümumi: {total_warns} xəbərdarlıq • abi-bot")
    await ctx.send(embed=embed)
    await send_mod_log(ctx.guild, embed)


@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member = None):
    # İstifadəçinin son xəbərdarlıqlarını göstəririk
    target = member or ctx.author
    rows = db.get_warnings(target.id, limit=10)
    total_count = db.get_warning_count(target.id)

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
        description=f"**Ümumi Xəbərdarlıq:** `{total_count}` ədəd\n\n" + "\n\n".join(lines),
        color=0xFAA61A,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Silmək üçün: abi delwarn [ID] və ya abi clearwarn @user • abi-bot")
    await ctx.send(embed=embed)


def build_warnlar_embed(rows):
    if not rows:
        return discord.Embed(
            title="✅ Warn Cədvəli",
            description="Hazırda heç bir aktiv warn qeydi yoxdur.",
            color=SUCCESS_COLOR,
            timestamp=datetime.utcnow(),
        )

    lines = []
    for index, row in enumerate(rows, start=1):
        medal = get_medal(index)
        user_id = row["user_id"]
        count = row["warning_count"]
        latest_warning = row.get("latest_warning") or "-"
        lines.append(f"{medal} <@{user_id}> — **`{count}` warn**\n└ Son warn: `{latest_warning}`")

    embed = discord.Embed(
        title=f"⚠️ Ümumi Warn Cədvəli • Top {len(rows)}",
        description="\n\n".join(lines),
        color=WARNING_COLOR,
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Ətraflı tarixçə: abi warnings @istifadəçi • abi-bot")
    return embed


@bot.command(name="warnlar", aliases=["warntop"])
@commands.has_permissions(manage_messages=True)
async def warnlar(ctx, number: int = 10):
    """Warn-u olan bütün istifadəçilərin sıralamasını göstərir."""
    number = max(1, min(number, 25))
    await ctx.send(embed=build_warnlar_embed(db.get_warning_leaderboard(number)))


@bot.command(name="delwarn")
@commands.has_permissions(manage_messages=True)
async def delwarn(ctx, warning_id: int):
    # Tək bir xəbərdarlığı ID ilə silirik
    success = db.delete_warning(warning_id)
    if success:
        await send_success_card(
            ctx,
            "Xəbərdarlıq Silindi",
            f"✅ `#{warning_id}` nömrəli xəbərdarlıq bazadan uğurla silindi."
        )
    else:
        await send_error_card(ctx, "Tapılmadı", f"`#{warning_id}` nömrəli xəbərdarlıq tapılmadı.")


@bot.command(name="clearwarn")
@commands.has_permissions(manage_messages=True)
async def clearwarn(ctx, member: discord.Member):
    # İstifadəçinin bütün xəbərdarlıqlarını təmizləyirik
    count = db.clear_warnings(member.id)
    if count > 0:
        await send_success_card(
            ctx,
            "Xəbərdarlıqlar Təmizləndi",
            f"✅ {member.mention} istifadəçisinin bütün (`{count}` ədəd) xəbərdarlıqları silindi."
        )
    else:
        await send_error_card(ctx, "Məlumat Yoxdur", f"{member.mention} üçün silinəcək aktiv xəbərdarlıq tapılmadı.")



@bot.command(name="sil", aliases=["temizle"])
@commands.has_permissions(manage_messages=True)
async def sil(ctx, amount: int = 10):
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
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    try:
        await member.timeout(until, reason=f"{ctx.author} | {reason}")
        logger.info(f"Mute verildi | mod={ctx.author.id} user={member.id} min={minutes} reason={reason}")

        embed = discord.Embed(
            title="🔇 İstifadəçi Mute Edildi (Timeout)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Müddət:** `{minutes} dəqiqə`\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0xE67E22,
            timestamp=discord.utils.utcnow()
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
    embed = build_audit_embed("📥 Üzv Qoşuldu", SUCCESS_COLOR, member)
    embed.add_field(name="Üzv", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Hesabın Yaradılma Tarixi", value=f"<t:{int(member.created_at.timestamp())}:F>", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await send_mod_log(member.guild, embed)


@bot.event
async def on_member_remove(member: discord.Member):
    # Üzv serverdən ayrıldıqda və ya atıldıqda loglayırıq
    embed = build_audit_embed("📤 Üzv Ayrıldı", ERROR_COLOR, member)
    embed.add_field(name="Üzv", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="Hadisə", value="Serverdən ayrıldı və ya çıxarıldı", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
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
            "• `abi warnlar [say]` — Warn-u olan istifadəçilərin ümumi cədvəli\n"
            "• `abi mute @user [dəq] [səbəb]` — Timeout (səs/yazı kəsmə)\n"
            "• `abi unmute @user` — Timeout-u vaxtından əvvəl qaldırır\n"
            "• `abi kick @user [səbəb]` — İstifadəçini serverdən atır\n"
            "• `abi ban @user [səbəb]` — İstifadəçini serverdən qadağan edir\n"
            "• `abi unban [ID] [səbəb]` — İstifadəçinin banını açır\n"
            "• `abi sil [say]` — Kanaldakı mesajları toplu silir"
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

    embed.add_field(
        name="🔐 Administrator bələdçisi",
        value="• `abi adminkomandalar [əmr]` — Moderasiya əmrlərinin istifadə qaydası və nümunələri",
        inline=False
    )

    embed.set_footer(text="Developed for your server • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await ctx.send(embed=embed)


@bot.command(name="adminkomandalar", aliases=["adminhelp"])
@commands.has_permissions(administrator=True)
async def adminkomandalar(ctx, command_name: str = None):
    """Administratorlar üçün moderasiya əmrlərinin izahlı bələdçisi."""
    guides = {
        "warn": {
            "title": "⚠️ Warn sistemi",
            "text": (
                "**İstifadə:** `abi warn @istifadəçi [səbəb]`\n"
                "**İcazə:** Mesajları idarə et (Manage Messages)\n\n"
                "İstifadəçiyə xəbərdarlıq verir, səbəbi bazada saxlayır və mod-log kanalına göndərir.\n"
                "• 3 warn → avtomatik 1 saat timeout\n"
                "• 5 və daha çox warn → avtomatik 24 saat timeout\n\n"
                "**Nümunə:** `abi warn @Nihad Spam paylaşdı`"
            ),
        },
        "warnings": {
            "title": "📋 Warn tarixçəsi",
            "text": (
                "**İstifadə:** `abi warnings [@istifadəçi]`\n"
                "Öz warn-larını və ya göstərilən istifadəçinin warn tarixçəsini göstərir.\n\n"
                "**Nümunə:** `abi warnings @Nihad`"
            ),
        },
        "warnlar": {
            "title": "📊 Ümumi warn cədvəli",
            "text": (
                "**İstifadə:** `abi warnlar [say]`\n"
                "**İcazə:** Mesajları idarə et\n\n"
                "Warn-u olan bütün istifadəçiləri warn sayına görə sıralayır. Maksimum 25 nəticə göstərir.\n\n"
                "**Nümunə:** `abi warnlar 10`"
            ),
        },
        "delwarn": {
            "title": "🗑️ Tək warn silmək",
            "text": (
                "**İstifadə:** `abi delwarn [warn ID]`\n"
                "**İcazə:** Mesajları idarə et\n\n"
                "`abi warnings` nəticəsində görünən ID ilə bir xəbərdarlığı silir.\n\n"
                "**Nümunə:** `abi delwarn 12`"
            ),
        },
        "clearwarn": {
            "title": "🧹 Bütün warn-ları silmək",
            "text": (
                "**İstifadə:** `abi clearwarn @istifadəçi`\n"
                "**İcazə:** Mesajları idarə et\n\n"
                "Seçilmiş istifadəçinin bütün xəbərdarlıqlarını silir. Bu əməl geri qaytarılmır."
            ),
        },
        "mute": {
            "title": "🔇 Timeout (Mute)",
            "text": (
                "**İstifadə:** `abi mute @istifadəçi [dəqiqə] [səbəb]`\n"
                "**İcazə:** Üzvləri moderasiya et (Moderate Members)\n\n"
                "İstifadəçiyə seçilən müddət üçün Discord timeout tətbiq edir.\n\n"
                "**Nümunə:** `abi mute @Nihad 30 Təhqir`"
            ),
        },
        "unmute": {
            "title": "🔊 Timeout-u açmaq",
            "text": (
                "**İstifadə:** `abi unmute @istifadəçi`\n"
                "**İcazə:** Üzvləri moderasiya et\n\n"
                "İstifadəçinin aktiv timeout cəzasını dərhal ləğv edir."
            ),
        },
        "kick": {
            "title": "👢 Kick",
            "text": (
                "**İstifadə:** `abi kick @istifadəçi [səbəb]`\n"
                "**İcazə:** Üzvləri at (Kick Members)\n\n"
                "İstifadəçini serverdən çıxarır; sonradan yenidən qoşula bilər."
            ),
        },
        "ban": {
            "title": "🔨 Ban",
            "text": (
                "**İstifadə:** `abi ban @istifadəçi [səbəb]`\n"
                "**İcazə:** Üzvləri ban et (Ban Members)\n\n"
                "İstifadəçini serverdən qadağan edir. Banı açmaq üçün `abi unban` istifadə edin."
            ),
        },
        "unban": {
            "title": "✅ Banı açmaq",
            "text": (
                "**İstifadə:** `abi unban [istifadəçi ID] [səbəb]`\n"
                "**İcazə:** Üzvləri ban et\n\n"
                "İstifadəçinin banını Discord ID-si ilə açır. Mention yox, yalnız rəqəm ID yazılmalıdır.\n\n"
                "**Nümunə:** `abi unban 123456789012345678 Səhv ban`"
            ),
        },
        "sil": {
            "title": "🧽 Mesajları silmək",
            "text": (
                "**İstifadə:** `abi sil [say]`\n"
                "**İcazə:** Mesajları idarə et\n\n"
                "Cari kanaldan 1–100 arası mesajı silir. Əmr mesajı da silinənlərə daxildir.\n\n"
                "**Nümunə:** `abi sil 25`"
            ),
        },
        "setlog": {
            "title": "📜 Mod-log kanalını təyin etmək",
            "text": (
                "**İstifadə:** `abi setlog #kanal`\n"
                "**İcazə:** Administrator\n\n"
                "Warn, kick, ban, silinən/redaktə edilən mesajlar və səs giriş-çıxış loglarını seçilən kanala göndərir.\n\n"
                "**Nümunə:** `abi setlog #mod-log`"
            ),
        },
        "sifirla": {
            "title": "♻️ Səs statistikasını sıfırlamaq",
            "text": (
                "**İstifadə:** `abi sifirla @istifadəçi`\n"
                "**İcazə:** Administrator\n\n"
                "İstifadəçinin səs vaxtı və bağlı statistik qeydlərini silir. Bu əməl geri qaytarılmır."
            ),
        },
    }

    if not command_name:
        available = ", ".join(f"`{name}`" for name in guides)
        embed = discord.Embed(
            title="🔐 Administrator Komanda Bələdçisi",
            description=(
                f"Ətraflı izah üçün `abi adminkomandalar [əmr]` yazın.\n\n"
                f"**Mövcud əmrlər:**\n{available}"
            ),
            color=0x5865F2,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Nümunə: abi adminkomandalar warn • abi-bot")
        await ctx.send(embed=embed)
        return

    command_name = {"temizle": "sil"}.get(command_name.lower(), command_name.lower())
    guide = guides.get(command_name)
    if not guide:
        await send_error_card(
            ctx,
            "Əmr Tapılmadı",
            "Bu admin əmri üçün bələdçi yoxdur. Siyahı üçün `abi adminkomandalar` yazın.",
        )
        return

    embed = discord.Embed(
        title=guide["title"],
        description=guide["text"],
        color=0x5865F2,
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text=f"Sorğunu açan: {ctx.author.display_name} • abi-bot")
    await ctx.send(embed=embed)


# ==================== SLASH COMMANDS (/) ====================

@bot.tree.command(name="profil", description="İstifadəçinin səs aktivliyi və statistikasını göstərir.")
@app_commands.describe(member="Profilinə baxmaq istədiyiniz üzv (boş qoysanız özünüz)")
async def slash_profil(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
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
    embed.add_field(name="⏱️ Ümumi Aktivlik", value=f"```fix\n{format_time(base_total)}\n```", inline=False)
    embed.add_field(name="📅 Bu gün", value=f"⏱️ `{format_time(today_seconds)}`", inline=True)
    embed.add_field(name="📆 Bu həftə", value=f"⏱️ `{format_time(week_seconds)}`", inline=True)
    embed.add_field(name="🗓️ Bu ay", value=f"⏱️ `{format_time(month_seconds)}`", inline=True)
    embed.set_footer(text=f"İlk aktivlik: {first_seen} • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Serverin səs liderləri cədvəlini göstərir.")
@app_commands.describe(say="Göstəriləcək üzv sayı (məs: 10)")
async def slash_top(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 25))
    leaderboard = get_combined_totals()
    top_rows = leaderboard[:say]

    if not top_rows:
        await interaction.response.send_message("📭 Hələ heç bir səs statistikası qeydə alınmayıb.", ephemeral=True)
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
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="seviyye", description="İstifadəçinin cari Level və XP kartını göstərir.")
@app_commands.describe(member="Levelinə baxmaq istədiyiniz üzv")
async def slash_seviyye(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    user = db.get_user(target.id)

    current_level = int(user.get("level") or 1) if user else 1
    current_xp = int(user.get("xp") or 0) if user else 0
    next_level_xp = db.xp_for_level(current_level + 1)
    needed_xp = max(next_level_xp - current_xp, 0)
    progress_bar = build_progress_bar(current_xp, current_level)

    embed = discord.Embed(
        title=f"⭐ Səviyyə Profili — {target.display_name}",
        color=0x9B59B6,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.description = f"**İstifadəçi:** {target.mention}\n**Cari Səviyyə:** `🏅 Səviyyə {current_level}`"
    embed.add_field(name="✨ Təcrübə (XP)", value=f"```yaml\nCari XP: {current_xp} / {next_level_xp}\nNövbəti səviyyəyə: {needed_xp} XP qaldı\n```", inline=False)
    embed.add_field(name="📈 Səviyyə İrəliləyişi", value=progress_bar, inline=False)
    embed.set_footer(text="Hər 5 dəqiqə səsdə qalmağa 10 XP verilir • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="xptop", description="Serverin ən yüksək səviyyəli üzvlərinin liderlik cədvəli.")
@app_commands.describe(say="Göstəriləcək üzv sayı (məs: 10)")
async def slash_xptop(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 25))
    rows = db.get_level_leaderboard(say)
    if not rows:
        await interaction.response.send_message("📭 Hələ heç bir XP qeydi mövcud deyil.", ephemeral=True)
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
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="userinfo", description="İstifadəçi haqqında ətraflı məlumat göstərir.")
@app_commands.describe(member="Məlumatına baxmaq istədiyiniz üzv")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
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
    embed.set_footer(text=f"Sorğulayan: {interaction.user.display_name} • abi-bot", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="İstifadəçinin böyüdülmüş profil şəklini göstərir.")
@app_commands.describe(member="Avatarına baxmaq istədiyiniz üzv")
async def slash_avatar(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    embed = discord.Embed(
        title=f"🖼️ {target.display_name} — Avatar",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text=f"Sorğulayan: {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="serverinfo", description="Server haqqında əsas statistik məlumatları göstərir.")
async def slash_serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
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

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="kick", description="İstifadəçini serverdən kənarlaşdırır (Kick).")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(member="Serverdən atılacaq üzv", reason="Kick səbəbi")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "Səbəb göstərilməyib"):
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Səninlə eyni və ya daha yüksək rolda olan istifadəçini serverdən ata bilməzsən.", ephemeral=True)
        return

    try:
        await member.kick(reason=f"{interaction.user} | {reason}")
        embed = discord.Embed(
            title="👢 Üzv Serverdən Atıldı (Kick)",
            description=f"**Atılan Üzv:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {interaction.user.mention}",
            color=0xE67E22,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        await send_mod_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botun bu istifadəçini atmağa (kick) icazəsi yoxdur (Rol iyerarxiyasını yoxlayın).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Xəta: {e}", ephemeral=True)


@bot.tree.command(name="ban", description="İstifadəçini serverdən qadağan edir (Ban).")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(member="Serverdən ban ediləcək üzv", reason="Ban səbəbi")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "Səbəb göstərilməyib"):
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Səninlə eyni və ya daha yüksək rolda olan istifadəçini ban edə bilməzsən.", ephemeral=True)
        return

    try:
        await member.ban(reason=f"{interaction.user} | {reason}", delete_message_days=0)
        embed = discord.Embed(
            title="🔨 Üzv Ban Edildi",
            description=f"**Banlanan Üzv:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {interaction.user.mention}",
            color=0xED4245,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        await send_mod_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botun bu istifadəçini ban etməyə səlahiyyəti çatmır.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Xəta: {e}", ephemeral=True)


@bot.tree.command(name="mute", description="İstifadəçiyə timeout tətbiq edir (Mute).")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Timeout veriləcək üzv", minutes="Müddət (dəqiqə ilə)", reason="Mute səbəbi")
async def slash_mute(interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "Səbəb göstərilməyib"):
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Səninlə eyni və ya daha yüksək rolda olan istifadəçiyə timeout verə bilməzsən.", ephemeral=True)
        return

    minutes = max(1, min(minutes, 40320))
    until = discord.utils.utcnow() + timedelta(minutes=minutes)
    try:
        await member.timeout(until, reason=f"{interaction.user} | {reason}")
        embed = discord.Embed(
            title="🔇 İstifadəçi Mute Edildi (Timeout)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Müddət:** `{minutes} dəqiqə`\n**Səbəb:** {reason}\n**Moderator:** {interaction.user.mention}",
            color=0xE67E22,
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_footer(text=f"Müddət bitmə vaxtı: {(until).strftime('%H:%M:%S UTC')} • abi-bot")
        await interaction.response.send_message(embed=embed)
        await send_mod_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botun bu istifadəçiyə timeout verməyə səlahiyyəti çatmır.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Xəta: {e}", ephemeral=True)


@bot.tree.command(name="unmute", description="İstifadəçinin timeout cəzasını qaldırır.")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Timeout-u açılacaq üzv")
async def slash_unmute(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None, reason=f"{interaction.user} tərəfindən unmute")
        embed = discord.Embed(
            title="🔊 Timeout Qaldırıldı (Unmute)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Moderator:** {interaction.user.mention}",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        await send_mod_log(interaction.guild, embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botun timeout-u açmağa yetkisi yoxdur.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Xəta: {e}", ephemeral=True)


@bot.tree.command(name="sil", description="Kanaldakı mesajları toplu silir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(say="Silinəcək mesaj sayı (maksimum 100)")
async def slash_sil(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 100))
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=say)
    
    embed = discord.Embed(
        title="🧹 Mesajlar Təmizləndi",
        description=f"**Kanal:** {interaction.channel.mention}\n**Silinən Mesaj Sayı:** `{len(deleted)}` ədəd",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"İcra edən: {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)
    await send_mod_log(interaction.guild, embed)


@bot.tree.command(name="warn", description="İstifadəçiyə rəsmi xəbərdarlıq qeyd edir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Xəbərdarlıq veriləcək üzv", reason="Xəbərdarlıq səbəbi")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "Səbəb göstərilməyib"):
    db.upsert_user_identity(member.id, member.name, member.display_name)
    db.upsert_user_identity(interaction.user.id, interaction.user.name, interaction.user.display_name)
    db.add_warning(member.id, interaction.user.id, reason)
    total_warns = db.get_warning_count(member.id)

    auto_punishment_note = ""
    if total_warns == 3:
        timeout_until = discord.utils.utcnow() + timedelta(hours=1)
        try:
            await member.timeout(timeout_until, reason="3 xəbərdarlığa çatdı (Avtomatik 1 saat Mute)")
            auto_punishment_note = "\n\n⚠️ **Avtomatik Cəza:** İstifadəçi 3 xəbərdarlığa çatdığı üçün **1 saatlıq Mute (Timeout)** edildi!"
        except Exception:
            pass
    elif total_warns >= 5:
        timeout_until = discord.utils.utcnow() + timedelta(days=1)
        try:
            await member.timeout(timeout_until, reason="5 və ya daha çox xəbərdarlıq (Avtomatik 24 saat Mute)")
            auto_punishment_note = f"\n\n🚨 **Avtomatik Cəza:** İstifadəçi {total_warns} xəbərdarlığa çatdığı üçün **24 saatlıq Mute (Timeout)** edildi!"
        except Exception:
            pass

    embed = discord.Embed(
        title="⚠️ Xəbərdarlıq (Warn) Verildi",
        description=f"**Cəzalandırılan:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Ümumi Xəbərdarlıq Sayı:** `{total_warns}`{auto_punishment_note}\n**Moderator:** {interaction.user.mention}",
        color=0xFEE75C if total_warns < 3 else 0xED4245,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text=f"Ümumi: {total_warns} xəbərdarlıq • abi-bot")
    await interaction.response.send_message(embed=embed)
    await send_mod_log(interaction.guild, embed)


@bot.tree.command(name="warnings", description="İstifadəçinin xəbərdarlıq tarixçəsini göstərir.")
@app_commands.describe(member="Xəbərdarlıqlarına baxılacaq üzv (boş qoysanız özünüz)")
async def slash_warnings(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    rows = db.get_warnings(target.id, limit=10)
    total_count = db.get_warning_count(target.id)

    if not rows:
        embed = discord.Embed(
            title=f"✅ {target.display_name} — Xəbərdarlıqlar",
            description="Bu istifadəçi üçün heç bir xəbərdarlıq qeydə alınmayıb.",
            color=0x57F287,
            timestamp=datetime.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        return

    lines = []
    for row in rows:
        reason = row.get("reason") or "Səbəb yoxdur"
        mod_id = row.get("moderator_id")
        date = row.get("date") or "-"
        lines.append(f"• `#{row['id']}` `[{date}]` — Mod: <@{mod_id}>\n  └ **Səbəb:** {reason}")

    embed = discord.Embed(
        title=f"⚠️ {target.display_name} — Xəbərdarlıq Tarixçəsi",
        description=f"**Ümumi Xəbərdarlıq:** `{total_count}` ədəd\n\n" + "\n\n".join(lines),
        color=0xFAA61A,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Silmək üçün: /delwarn və ya /clearwarn • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnlar", description="Warn-u olan istifadəçilərin ümumi cədvəlini göstərir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(say="Göstəriləcək istifadəçi sayı (maksimum 25)")
async def slash_warnlar(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 25))
    await interaction.response.send_message(embed=build_warnlar_embed(db.get_warning_leaderboard(say)))


@bot.tree.command(name="delwarn", description="Xüsusi ID-li bir xəbərdarlığı silir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(warning_id="Silinəcək xəbərdarlığın ID nömrəsi (məs: 3)")
async def slash_delwarn(interaction: discord.Interaction, warning_id: int):
    success = db.delete_warning(warning_id)
    if success:
        await interaction.response.send_message(f"✅ `#{warning_id}` nömrəli xəbərdarlıq uğurla silindi.")
    else:
        await interaction.response.send_message(f"❌ `#{warning_id}` nömrəli xəbərdarlıq tapılmadı.", ephemeral=True)


@bot.tree.command(name="clearwarn", description="İstifadəçinin bütün xəbərdarlıqlarını təmizləyir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Bütün xəbərdarlıqları təmizlənəcək üzv")
async def slash_clearwarn(interaction: discord.Interaction, member: discord.Member):
    count = db.clear_warnings(member.id)
    if count > 0:
        await interaction.response.send_message(f"✅ {member.mention} istifadəçisinin bütün (`{count}` ədəd) xəbərdarlıqları silindi.")
    else:
        await interaction.response.send_message(f"❌ {member.mention} üçün silinəcək aktiv xəbərdarlıq tapılmadı.", ephemeral=True)


@bot.tree.command(name="setlog", description="Audit & Mod Loglarının göndəriləcəyi kanalı təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Log mesajlarının göndəriləcəyi mətn kanalı")
async def slash_setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return

    db.set_guild_setting(interaction.guild.id, "mod_log_channel", str(channel.id))
    embed = discord.Embed(
        title="⚙️ Log Kanalı Təyin Edildi",
        description=f"✅ Mod və Audit logları artıq {channel.mention} kanalına göndəriləcək.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setchannel", description="Botun avtomatik bildiriş kanallarını təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    type="Quraşdırmaq istədiyiniz bildiriş növü",
    channel="Təyin ediləcək mətn kanalı"
)
@app_commands.choices(type=[
    app_commands.Choice(name="📜 Mod & Audit Log Kanalı", value="mod_log_channel"),
    app_commands.Choice(name="🎉 Səviyyə (Level-Up) Bildiriş Kanalı", value="levelup_channel")
])
async def slash_setchannel(interaction: discord.Interaction, type: app_commands.Choice[str], channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return

    db.set_guild_setting(interaction.guild.id, type.value, str(channel.id))
    embed = discord.Embed(
        title="⚙️ Kanal Quraşdırması Uğurlu",
        description=f"✅ **{type.name}** üçün təyin edilmiş kanal: {channel.mention}",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.command(name="setlog")
@commands.has_permissions(administrator=True)
async def prefix_setlog(ctx, channel: discord.TextChannel):
    # Prefix ilə log kanalını təyin edirik
    db.set_guild_setting(ctx.guild.id, "mod_log_channel", str(channel.id))
    await send_success_card(
        ctx,
        "Log Kanalı Təyin Edildi",
        f"✅ Mod və Audit logları artıq {channel.mention} kanalına göndəriləcək."
    )




@bot.tree.command(name="komandalar", description="Botun bütün əmrlərinin siyahısını və bələdçisini göstərir.")
async def slash_komandalar(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 abi-bot — Komanda Bələdçisi",
        description="Botun bütün əmrləri aşağıdakı kateqoriyalara bölünüb.\nHəm **`/` (Slash)**, həm də **`abi `** prefix-i ilə istifadə edə bilərsiniz.",
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="🎙️ Səs & Aktivlik Statistikası",
        value="• `/profil` — Səs aktivliyi və sıralama profili\n• `/top` — Ən çox səsdə qalanların lider cədvəli\n• `abi hesabat [gun/hefte/ay]` — Periodik hesabat",
        inline=False
    )
    embed.add_field(
        name="⭐ Səviyyə & XP Sistemi",
        value="• `/seviyye` — Level, XP kartı və tərəqqi çubuğu\n• `/xptop` — Ən yüksək səviyyəli üzvlər",
        inline=False
    )
    embed.add_field(
        name="🛡️ Moderasiya & Təhlükəsizlik",
        value="• `/kick` — Üzvü serverdən atır\n• `/ban` — Üzvü ban edir\n• `/mute` — Timeout verir\n• `/unmute` — Timeout-u qaldırır\n• `/sil` — Mesajları toplu silir\n• `abi warn` / `abi warnings` / `abi warnlar` — Xəbərdarlıq sistemi",
        inline=False
    )
    embed.add_field(
        name="🧰 Köməkçi & Digər",
        value="• `/userinfo` — İstifadəçi haqqında məlumat\n• `/serverinfo` — Server statistikası\n• `/avatar` — Profil şəkli\n• `abi poll` — Sorğu\n• `abi sifirla` — Səs sıfırlama",
        inline=False
    )
    embed.set_footer(text="Developed for your server • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)
    await interaction.response.send_message(embed=embed)



@tasks.loop(minutes=5)
async def xp_task():
    # Hər 5 dəqiqədə səsdə olan istifadəçilərə XP veririk
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

        # Aktiv sessiyanı periodik olaraq bazaya yazırıq. Bot yenidən başlasa,
        # ən çox bu interval qədər səs vaxtı itə bilər.
        started_at = voice_sessions.get(user_id)
        if started_at:
            elapsed_seconds = int((datetime.utcnow() - started_at).total_seconds())
            if elapsed_seconds > 0:
                db.add_voice_time(member.id, member.name, member.display_name, elapsed_seconds)
                voice_sessions[user_id] = datetime.utcnow()

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

            if member.guild:
                lvl_chan_id = db.get_guild_setting(member.guild.id, "levelup_channel") or LEVEL_UP_CHANNEL_ID
                if lvl_chan_id:
                    try:
                        lvl_chan = member.guild.get_channel(int(lvl_chan_id))
                        if lvl_chan:
                            embed = discord.Embed(
                                title="🎉 Səviyyə Yüksəldi!",
                                description=f"{member.mention} səviyyə **{new_level}**-ə çatdı!",
                                color=0xFFD700,
                            )
                            await lvl_chan.send(embed=embed)
                    except Exception:
                        pass


@xp_task.before_loop
async def before_xp_task():
    # XP döngüsü başlamadan öncə botun tam hazır olmasını gözləyirik
    await bot.wait_until_ready()


async def handle_ping(request):
    # Render/UptimeRobot üçün keep-alive endpoint
    return web.Response(text="Bot 7/24 aktivdir!", content_type="text/plain")


async def handle_terms(request):
    return web.FileResponse(BASE_DIR / "terms.html")


async def handle_privacy(request):
    return web.FileResponse(BASE_DIR / "privacy.html")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/terms", handle_terms)
    app.router.add_get("/privacy", handle_privacy)

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

