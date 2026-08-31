import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
import graphics
from music import GuildMusicPlayer, Song, ytdl, MusicControlView, search_song_info


load_dotenv()

PREFIX = "abi "
TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
LEVEL_UP_CHANNEL_ID = int(os.getenv("LEVEL_UP_CHANNEL_ID", 0))
TEMPVOICE_CHANNEL_ID = os.getenv("TEMPVOICE_CHANNEL_ID", "1544037874226307152")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID") or os.getenv("WELCOME_CHANNEL", "1467565789447196765")
AUTOROLE_ID = os.getenv("AUTOROLE_ID", "1198359102968041615")
UPDATE_LOG_CHANNEL_ID = os.getenv("UPDATE_LOG_CHANNEL_ID", "1544040943446003749")
BASE_DIR = Path(__file__).resolve().parent

# Bot versiyası və ən son yenilənmə jurnalı (Update Log)
BOT_VERSION = "2.2.2"
LATEST_CHANGELOG = {
    "version": "v2.2.2",
    "title": "🎵 Musiqi & Sistem Təkmilləşdirmələri",
    "date": datetime.utcnow().strftime("%d.%m.%Y"),
    "changes": [
        "**🎧 Musiqi Oxutma Xətası Həll Edildi**: FFmpeg axın tənzimləmələri və avtomatik binar inteqrasiyası tamamlandı.",
        "**🛡️ YouTube / SoundCloud Bypass**: Datacenter IP bloklamaları aradan qaldırıldı, mahnılar kəsintisiz və yüksək keyfiyyətlə oxunur.",
        "**📢 Avtomatik Update Log**: Yenilənmə jurnalı bu kanala birbaşa inteqrasiya edildi.",
        "**🚪 TempVoice & Canlı XP**: Səs otaqlarında düyməli idarəetmə və dəqiqəlik XP qazanma aktivdir."
    ]
}

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

XP_AWARD_COOLDOWN_SECONDS = 60
XP_MIN_MEMBERS_IN_VOICE = int(os.getenv("XP_MIN_MEMBERS_IN_VOICE", "1"))


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

# XP üçün vaxt qeydləri
last_xp_award = {}
last_msg_xp = {}

BRAND_COLOR = 0x5865F2
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245


def truncate_text(value: str, limit: int = 900) -> str:
    """Discord embed sahələrini oxunaqlı və limit daxilində saxlayır."""
    value = value or ""
    return value if len(value) <= limit else f"{value[:limit - 1]}…"


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


# ==================== TEMPVOICE UI (MODALLAR VƏ DÜYMƏLƏR) ====================

class RenameRoomModal(ui.Modal, title="Otağın Adını Dəyiş"):
    new_name = ui.TextInput(
        label="Yeni Otaq Adı",
        placeholder="Məsələn: Söhbət Otağım",
        max_length=32,
        min_length=1,
        required=True
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = self.new_name.value.strip()
            await self.channel.edit(name=val)
            await interaction.response.send_message(f"✅ Otağın adı **{val}** olaraq dəyişdirildi.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ad dəyişdirilə bilmədi: {e}", ephemeral=True)


class RoomLimitModal(ui.Modal, title="İstifadəçi Limiti Təyin Et"):
    limit_val = ui.TextInput(
        label="Limit (0 = Limitsiz, Max = 99)",
        placeholder="0-99 arası rəqəm yazın",
        max_length=2,
        min_length=1,
        required=True
    )

    def __init__(self, channel: discord.VoiceChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.limit_val.value.strip())
            val = max(0, min(val, 99))
            await self.channel.edit(user_limit=val)
            limit_str = f"**{val} nəfər**" if val > 0 else "**Limitsiz**"
            await interaction.response.send_message(f"✅ Otağın limiti {limit_str} təyin edildi.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Zəhmət olmasa yalnız rəqəm daxil edin (0-99).", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Limit dəyişdirilə bilmədi: {e}", ephemeral=True)


class KickMemberSelect(ui.UserSelect):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(placeholder="Otaqdan atmaq istədiyiniz üzvü seçin...", min_values=1, max_values=1)
        self.channel = channel

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target.id == interaction.user.id:
            await interaction.response.send_message("❌ Özünüzü otaqdan ata bilməzsiniz.", ephemeral=True)
            return

        member = interaction.guild.get_member(target.id) if interaction.guild else None
        if member and member.voice and member.voice.channel == self.channel:
            try:
                await member.move_to(None, reason="TempVoice: Otaq sahibi tərəfindən çıxarıldı")
                await self.channel.set_permissions(member, connect=False)
                await interaction.response.send_message(f"👢 {member.mention} otaqdan çıxarıldı və təkrar girişi bağlandı.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ İstifadəçini çıxarmaq olmadı: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {target.mention} hazırda sizin otaqda deyil.", ephemeral=True)


class KickMemberView(ui.View):
    def __init__(self, channel: discord.VoiceChannel):
        super().__init__(timeout=60)
        self.add_item(KickMemberSelect(channel))


class TempVoiceControlView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _get_target_channel(self, interaction: discord.Interaction) -> tuple[discord.VoiceChannel | None, dict | None, bool]:
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return None, None, False
        if not interaction.user.voice or not interaction.user.voice.channel:
            return None, None, False

        channel = interaction.user.voice.channel
        temp_data = db.get_temp_channel(channel.id)
        if not temp_data:
            return None, None, False

        is_owner = (temp_data["owner_id"] == interaction.user.id or interaction.user.guild_permissions.administrator)
        return channel, temp_data, is_owner

    @ui.button(label="Kilidlə / Aç", style=discord.ButtonStyle.primary, emoji="🔒", custom_id="tempvoice_toggle_lock")
    async def toggle_lock(self, interaction: discord.Interaction, button: ui.Button):
        channel, temp_data, is_owner = self._get_target_channel(interaction)
        if not channel:
            await interaction.response.send_message("❌ Hazırda heç bir şəxsi TempVoice otağında deyilsiniz.", ephemeral=True)
            return
        if not is_owner:
            await interaction.response.send_message("❌ Bu əmri yalnız otaq sahibi istifadə edə bilər.", ephemeral=True)
            return

        current_perm = channel.overwrites_for(interaction.guild.default_role).connect
        if current_perm is False:
            await channel.set_permissions(interaction.guild.default_role, connect=True)
            await interaction.response.send_message("🔓 Otağın kilidi açıldı! Hər kəs qoşula bilər.", ephemeral=True)
        else:
            await channel.set_permissions(interaction.guild.default_role, connect=False)
            await interaction.response.send_message("🔒 Otaq kilidləndi! İcazəsiz heç kim qoşula bilməz.", ephemeral=True)

    @ui.button(label="Ad Dəyiş", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id="tempvoice_rename")
    async def rename(self, interaction: discord.Interaction, button: ui.Button):
        channel, temp_data, is_owner = self._get_target_channel(interaction)
        if not channel:
            await interaction.response.send_message("❌ Hazırda heç bir şəxsi TempVoice otağında deyilsiniz.", ephemeral=True)
            return
        if not is_owner:
            await interaction.response.send_message("❌ Bu əmri yalnız otaq sahibi istifadə edə bilər.", ephemeral=True)
            return

        await interaction.response.send_modal(RenameRoomModal(channel))

    @ui.button(label="Limit Qoy", style=discord.ButtonStyle.secondary, emoji="👥", custom_id="tempvoice_limit")
    async def set_limit(self, interaction: discord.Interaction, button: ui.Button):
        channel, temp_data, is_owner = self._get_target_channel(interaction)
        if not channel:
            await interaction.response.send_message("❌ Hazırda heç bir şəxsi TempVoice otağında deyilsiniz.", ephemeral=True)
            return
        if not is_owner:
            await interaction.response.send_message("❌ Bu əmri yalnız otaq sahibi istifadə edə bilər.", ephemeral=True)
            return

        await interaction.response.send_modal(RoomLimitModal(channel))

    @ui.button(label="İstifadəçi At", style=discord.ButtonStyle.danger, emoji="👢", custom_id="tempvoice_kick")
    async def kick_member(self, interaction: discord.Interaction, button: ui.Button):
        channel, temp_data, is_owner = self._get_target_channel(interaction)
        if not channel:
            await interaction.response.send_message("❌ Hazırda heç bir şəxsi TempVoice otağında deyilsiniz.", ephemeral=True)
            return
        if not is_owner:
            await interaction.response.send_message("❌ Bu əmri yalnız otaq sahibi istifadə edə bilər.", ephemeral=True)
            return

        await interaction.response.send_message("Otaqdan çıxarmaq istədiyiniz üzvü seçin:", view=KickMemberView(channel), ephemeral=True)

    @ui.button(label="Liderlik Al", style=discord.ButtonStyle.success, emoji="👑", custom_id="tempvoice_claim")
    async def claim_ownership(self, interaction: discord.Interaction, button: ui.Button):
        channel, temp_data, is_owner = self._get_target_channel(interaction)
        if not channel:
            await interaction.response.send_message("❌ Hazırda heç bir şəxsi TempVoice otağında deyilsiniz.", ephemeral=True)
            return

        owner_id = temp_data["owner_id"]
        owner_present = any(m.id == owner_id for m in channel.members)
        if owner_present and owner_id != interaction.user.id:
            await interaction.response.send_message("❌ Otağın əsl sahibi hələ də kanaldadır.", ephemeral=True)
            return

        db.add_temp_channel(channel.id, interaction.guild.id, interaction.user.id)
        await channel.set_permissions(interaction.user, manage_channels=True, move_members=True, mute_members=True, deafen_members=True)
        await interaction.response.send_message(f"👑 Təbriklər, artıq bu otağın rəhbəri {interaction.user.mention}!", ephemeral=True)


@bot.event
async def on_ready():
    # Bot açıldıqda View-ləri qeydiyyatdan keçiririk
    bot.add_view(TempVoiceControlView())

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

    # Başlanğıcda boş qalmış köhnə temp kanalları təmizləyirik və default ayarları yazırıq
    for guild in bot.guilds:
        # Default environment ayarları serverə tətbiq olunur
        if TEMPVOICE_CHANNEL_ID and str(TEMPVOICE_CHANNEL_ID) != "0":
            db.set_guild_setting(guild.id, "tempvoice_channel", str(TEMPVOICE_CHANNEL_ID))
        if WELCOME_CHANNEL_ID and str(WELCOME_CHANNEL_ID) != "0":
            db.set_guild_setting(guild.id, "welcome_channel", str(WELCOME_CHANNEL_ID))
        if AUTOROLE_ID and str(AUTOROLE_ID) != "0":
            db.set_guild_setting(guild.id, "autorole", str(AUTOROLE_ID))
        if LEVEL_UP_CHANNEL_ID and LEVEL_UP_CHANNEL_ID != 0:
            db.set_guild_setting(guild.id, "levelup_channel", str(LEVEL_UP_CHANNEL_ID))
        if UPDATE_LOG_CHANNEL_ID and str(UPDATE_LOG_CHANNEL_ID) != "0":
            db.set_guild_setting(guild.id, "update_log_channel", str(UPDATE_LOG_CHANNEL_ID))

        # Avtomatik Update Log Bildirişi (Yalnız yeni versiya çıxdıqda 1 dəfə göndərilir)
        log_channel_id = db.get_guild_setting(guild.id, "update_log_channel") or (UPDATE_LOG_CHANNEL_ID if UPDATE_LOG_CHANNEL_ID and str(UPDATE_LOG_CHANNEL_ID) != "0" else None)
        if log_channel_id:
            try:
                update_chan = guild.get_channel(int(log_channel_id))
                if update_chan is None:
                    try:
                        update_chan = await bot.fetch_channel(int(log_channel_id))
                    except Exception as fe:
                        logger.warning(f"Update log kanalı fetch edilə bilmədi ({log_channel_id}): {fe}")

                if update_chan:
                    already_posted = False
                    try:
                        async for msg in update_chan.history(limit=15):
                            if msg.author == bot.user and msg.embeds:
                                for emb in msg.embeds:
                                    if f"Versiya: {BOT_VERSION}" in (emb.footer.text if emb.footer else "") or f"({LATEST_CHANGELOG['version']})" in (emb.title or ""):
                                        already_posted = True
                                        break
                            if already_posted:
                                break
                    except Exception as he:
                        logger.warning(f"Kanal tarixçəsi oxuna bilmədi: {he}")

                    if not already_posted:
                        embed = discord.Embed(
                            title=f"📢 {LATEST_CHANGELOG['title']} ({LATEST_CHANGELOG['version']})",
                            description="Server üçün botda aşağıdakı yeni funksiyalar və təkmilləşdirmələr tətbiq edildi:\n\n" + "\n\n".join(f"• {c}" for c in LATEST_CHANGELOG["changes"]),
                            color=0x5865F2,
                            timestamp=datetime.utcnow()
                        )
                        embed.set_footer(text=f"Abi Bot Yenilənmə Sistemi • Versiya: {LATEST_CHANGELOG['version']} • {LATEST_CHANGELOG['date']}", icon_url=bot.user.display_avatar.url if bot.user else None)
                        await update_chan.send(embed=embed)
                        logger.info(f"Update log {log_channel_id} kanalına uğurla göndərildi ({BOT_VERSION}).")
                        print(f"✅ Update log {log_channel_id} kanalına uğurla göndərildi ({BOT_VERSION}).")
            except Exception as err:
                logger.warning(f"Update log göndərilə bilmədi: {err}")
                print(f"❌ Update log xətası: {err}")

        temp_list = db.get_guild_temp_channels(guild.id)
        for t in temp_list:
            chan = guild.get_channel(t["channel_id"])
            if chan is None or len(chan.members) == 0:
                db.remove_temp_channel(t["channel_id"])
                if chan:
                    try:
                        await chan.delete(reason="TempVoice boş kanal təmizləndi")
                    except Exception:
                        pass

    print(f"{bot.user} olaraq daxil olundu.")


@bot.event
async def on_voice_state_update(member, before, after):
    # Botları izləmədən çıxırıq
    if member.bot:
        return

    # Səsə qoşulma halında sessiyanı başladırıq
    if before.channel is None and after.channel is not None:
        voice_sessions[member.id] = datetime.utcnow()

    # Səsdən çıxma halında sessiyanı yadda saxlayırıq
    if before.channel is not None and after.channel is None:
        started_at = voice_sessions.get(member.id)
        if started_at:
            seconds = int((datetime.utcnow() - started_at).total_seconds())
            if seconds > 0:
                db.add_voice_time(member.id, member.name, member.display_name, seconds)
            voice_sessions.pop(member.id, None)

    # 🚪 TempVoice Sistemi: "Otaq Yarat" kanalına qoşulma
    if after.channel is not None and (before.channel is None or before.channel.id != after.channel.id):
        temp_master_id = db.get_guild_setting(member.guild.id, "tempvoice_channel") or (TEMPVOICE_CHANNEL_ID if TEMPVOICE_CHANNEL_ID and str(TEMPVOICE_CHANNEL_ID) != "0" else None)
        if temp_master_id and str(after.channel.id) == str(temp_master_id):
            try:
                category = after.channel.category
                overwrites = {
                    member.guild.default_role: discord.PermissionOverwrite(connect=True, speak=True),
                    member: discord.PermissionOverwrite(
                        manage_channels=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                        priority_speaker=True
                    )
                }
                new_room = await member.guild.create_voice_channel(
                    name=f"{member.display_name} otağı",
                    category=category,
                    overwrites=overwrites,
                    user_limit=0,
                    reason=f"TempVoice: {member} tərəfindən yaradıldı"
                )
                db.add_temp_channel(new_room.id, member.guild.id, member.id)
                await member.move_to(new_room)

                # İstifadəçiyə otağı idarə etmək üçün düyməli idarəetmə paneli göndəririk
                try:
                    embed = discord.Embed(
                        title="🎙️ Şəxsi Səs Otağınız Yaradıldı",
                        description=(
                            f"Xoş gəldiniz, {member.mention}!\n\n"
                            "Aşağıdakı düymələrdən istifadə edərək səs otağınızı asanlıqla idarə edə bilərsiniz:\n"
                            "• **🔒 Kilidlə / Aç** — Otağa giriş icazəsini bağlayır / açır\n"
                            "• **✏️ Ad Dəyiş** — Otağın adını yeniləyir\n"
                            "• **👥 Limit Qoy** — İstifadəçi sayına limit qoyur\n"
                            "• **👢 İstifadəçi At** — İstenməyən şəxsi otaqdan çıxarır\n"
                            "• **👑 Liderlik Al** — Əsl sahib çıxıbsa, otaq rəhbərliyini ələ alır\n"
                        ),
                        color=0x57F287,
                        timestamp=datetime.utcnow()
                    )
                    embed.set_footer(text="Otaqdakı hər kəs çıxdıqda kanal avtomatik silinəcək • abi-bot")
                    await new_room.send(embed=embed, view=TempVoiceControlView())
                except Exception:
                    pass
            except Exception as err:
                logger.error(f"TempVoice yaradılarkən xəta: {err}")

    # 🚪 TempVoice Sistemi: Otaqdan çıxış və boş qalan otağın avtomatik silinməsi
    if before.channel is not None and (after.channel is None or before.channel.id != after.channel.id):
        temp_data = db.get_temp_channel(before.channel.id)
        if temp_data:
            # Əgər otaqda heç kim qalmayıbsa, kanalı silirik
            if len(before.channel.members) == 0:
                db.remove_temp_channel(before.channel.id)
                try:
                    await before.channel.delete(reason="TempVoice boşaldığı üçün silindi")
                except Exception:
                    pass


@bot.event
async def on_member_join(member: discord.Member):
    # 1. Auto-Role: Avtomatik rol təyin edilməsi
    autorole_id = db.get_guild_setting(member.guild.id, "autorole") or (AUTOROLE_ID if AUTOROLE_ID and str(AUTOROLE_ID) != "0" else None)
    if autorole_id:
        try:
            role = member.guild.get_role(int(autorole_id))
            if role:
                await member.add_roles(role, reason="Auto-Role: Yeni üzvə avtomatik rol verildi")
        except Exception as err:
            logger.warning(f"Auto-Role verilə bilmədi | user={member.id} role={autorole_id} err={err}")

    # 2. Welcome Image & Message: Gözəl vizual şəkillə qarşılama
    welcome_chan_id = db.get_guild_setting(member.guild.id, "welcome_channel") or (WELCOME_CHANNEL_ID if WELCOME_CHANNEL_ID and str(WELCOME_CHANNEL_ID) != "0" else None)
    if welcome_chan_id:
        try:
            chan = member.guild.get_channel(int(welcome_chan_id))
            if chan:
                try:
                    avatar_bytes = await member.display_avatar.read()
                except Exception:
                    avatar_bytes = None

                welcome_card = graphics.generate_welcome_card(
                    avatar_bytes=avatar_bytes,
                    username=str(member),
                    guild_name=member.guild.name,
                    member_count=member.guild.member_count
                )

                file = discord.File(welcome_card, filename="welcome.png")
                await chan.send(
                    content=f"🎉 Xoş gəldin {member.mention}! Serverimizə qatıldığın üçün şadıq!",
                    file=file
                )
        except Exception as err:
            logger.warning(f"Welcome mesajı göndərilə bilmədi | guild={member.guild.id} err={err}")



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

    # Mətn kanalında mesaj yazdıqca XP verilməsi (hər 60 saniyədən bir 5-12 XP)
    if message.guild and not message.content.startswith(PREFIX):
        now = datetime.utcnow()
        last_time = last_msg_xp.get(message.author.id)
        if not last_time or (now - last_time).total_seconds() >= 60:
            last_msg_xp[message.author.id] = now
            import random
            earned_xp = random.randint(5, 12)
            db.upsert_user_identity(message.author.id, message.author.name, message.author.display_name)
            new_xp, new_level, leveled_up = db.add_xp(message.author.id, earned_xp)
            if leveled_up:
                lvl_chan_id = db.get_guild_setting(message.guild.id, "levelup_channel") or (LEVEL_UP_CHANNEL_ID if LEVEL_UP_CHANNEL_ID != 0 else None)
                if lvl_chan_id:
                    try:
                        lvl_chan = message.guild.get_channel(int(lvl_chan_id))
                        if lvl_chan:
                            embed = discord.Embed(
                                title="🎉 Səviyyə Yüksəldi!",
                                description=f"Təbriklər {message.author.mention}! Səviyyə **{new_level}** oldunuz! 🚀",
                                color=0xFFD700,
                                timestamp=datetime.utcnow()
                            )
                            embed.set_thumbnail(url=message.author.display_avatar.url)
                            embed.set_footer(text="abi-bot Level Sistemi")
                            await lvl_chan.send(embed=embed)
                    except Exception:
                        pass

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
    except discord.NotFound:
        await send_error_card(ctx, "Tapılmadı", "Bu ID-yə uyğun istifadəçi tapılmadı və ya ban siyahısında deyil.")
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun ban qaldırmağa (unban) səlahiyyəti çatmır.")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")




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


@bot.command(name="seviyye", aliases=["rank", "level"])
async def seviyye(ctx, member: discord.Member = None):
    # İstifadəçinin level və XP kartını göstəririk
    target = member or ctx.author
    user = db.get_user(target.id)

    current_level = int(user.get("level") or 1) if user else 1
    current_xp = int(user.get("xp") or 0) if user else 0
    next_level_xp = db.xp_for_level(current_level + 1)
    level_start_xp = db.xp_for_level(current_level)
    rank_position = db.get_user_rank(target.id)
    streak_info = db.get_streak(target.id)
    streak = streak_info.get("streak", 0)

    try:
        avatar_bytes = await target.display_avatar.read()
    except Exception:
        avatar_bytes = None

    card_buf = graphics.generate_rank_card(
        avatar_bytes=avatar_bytes,
        username=target.name,
        display_name=target.display_name,
        level=current_level,
        xp=current_xp,
        current_level_xp=level_start_xp,
        next_level_xp=next_level_xp,
        rank_position=rank_position,
        streak=streak,
    )
    file = discord.File(card_buf, filename="rank.png")

    embed = discord.Embed(
        title=f"⭐ Səviyyə Kartı — {target.display_name}",
        color=BRAND_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url="attachment://rank.png")
    streak_note = f" • 🔥 Seriya: **{streak} gün**" if streak > 0 else ""
    embed.description = f"**İstifadəçi:** {target.mention}\n**Sıralama:** `🏆 #{rank_position}` | **Səviyyə:** `🏅 {current_level}`{streak_note}"
    embed.set_footer(text="Hər 5 dəqiqə səsdə qalmağa 10 XP • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await ctx.send(embed=embed, file=file)


@bot.command(name="qrafik", aliases=["chart", "aktivlik"])
async def qrafik(ctx, member: discord.Member = None):
    """İstifadəçinin son 7 günlük səs aktivliyi qrafikini göstərir."""
    target = member or ctx.author
    history = db.get_user_daily_history(target.id, days=7)

    chart_buf = graphics.generate_voice_chart(history, target.display_name)
    file = discord.File(chart_buf, filename="activity.png")

    total_sec = sum(d["seconds"] for d in history)
    embed = discord.Embed(
        title=f"📈 Həftəlik Səs Aktivliyi — {target.display_name}",
        description=f"{target.mention} üçün son 7 günün statistikası:\n**Ümumi Aktivlik:** `{format_time(total_sec)}`",
        color=BRAND_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url="attachment://activity.png")
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name} • abi-bot", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed, file=file)


@bot.command(name="streak", aliases=["seriya"])
async def streak(ctx, member: discord.Member = None):
    """İstifadəçinin gündəlik səs seriyasını (Streak) göstərir."""
    target = member or ctx.author
    streak_data = db.get_streak(target.id)
    cur = streak_data["streak"]
    highest = streak_data["highest_streak"]
    active_today = streak_data["active_today"]

    status_str = "🔥 Bu gün aktivdir (+bonus alınıb)" if active_today else "⏳ Bu gün hələ 15 dəqiqə tamamlanmayıb"

    embed = discord.Embed(
        title=f"🔥 Gündəlik Səs Seriyası — {target.display_name}",
        color=0xE67E22 if cur > 0 else 0x95A5A6,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.description = f"**İstifadəçi:** {target.mention}\n**Status:** `{status_str}`"
    embed.add_field(name="🔥 Cari Seriya", value=f"**`{cur} Gün`**", inline=True)
    embed.add_field(name="🏆 Rekord Seriya", value=f"**`{highest} Gün`**", inline=True)
    embed.add_field(
        name="💡 Seriya Qaydası",
        value="Hər gün ən azı **15 dəqiqə** səs kanalında vaxt keçir, seriyanı artır və hər gün üçün əlavə **XP bonusu** qazan!",
        inline=False
    )
    embed.set_footer(text="Seriyanı qoru, zirvəyə qalx! • abi-bot")
    await ctx.send(embed=embed)


@bot.command(name="streaktop", aliases=["seriyatop"])
async def streaktop(ctx, number: int = 10):
    """Serverdə ən yüksək səs seriyasına sahib istifadəçilər."""
    number = max(1, min(number, 25))
    rows = db.get_streak_leaderboard(number)
    if not rows:
        embed = discord.Embed(
            description="📭 Hələ heç bir aktiv səs seriyası qeydə alınmayıb.",
            color=0xE67E22
        )
        await ctx.send(embed=embed)
        return

    lines = []
    for idx, r in enumerate(rows, start=1):
        medal = get_medal(idx)
        name = r.get("display_name") or r.get("username") or "Naməlum"
        cur = r.get("current_streak") or 0
        high = r.get("highest_streak") or 0
        lines.append(f"{medal} **{name}** — 🔥 **`{cur} gün`** (Rekord: `{high}` gün)")

    embed = discord.Embed(
        title=f"🔥 Ən Yüksək Səs Seriyaları • Top {len(rows)}",
        description="\n".join(lines),
        color=0xE67E22,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Gündəlik səs aktivliyi liderləri • abi-bot")
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


# ==================== TEMPVOICE (ŞƏXSİ SƏS OTAĞI) PREFIX ƏHRAMLARI ====================

def get_user_temp_channel(member: discord.Member) -> discord.VoiceChannel | None:
    if not member.voice or not member.voice.channel:
        return None
    channel = member.voice.channel
    temp_data = db.get_temp_channel(channel.id)
    if temp_data and (temp_data["owner_id"] == member.id or member.guild_permissions.administrator):
        return channel
    return None


@bot.group(name="ses", invoke_without_command=True)
async def ses_group(ctx):
    embed = discord.Embed(
        title="🎙️ TempVoice (Şəxsi Səs Otağı) İdarəetmə Paneli",
        description=(
            "Şəxsi səs otağınızı aşağıdakı **düymələr** və ya komandalarla idarə edə bilərsiniz:\n\n"
            "• `abi ses ad [yeni ad]` — Otağın adını dəyişir\n"
            "• `abi ses limit [say]` — Otağın istifadəçi limitini təyin edir (0 = limitsiz)\n"
            "• `abi ses kilid` — Otağı kilidləyir (başqalarının qoşulmasını bağlayır)\n"
            "• `abi ses ac` — Otağın kilidini açır\n"
            "• `abi ses at @user` — Göstərilən şəxsi səs otağınızdan çıxarır\n"
            "• `abi ses devret @user` — Otaq sahibliyini başqa üzvə verir\n"
            "• `abi ses lider` — Əgər otaq sahibi çıxıbsa, otaq rəhbərliyini ələ alır\n"
        ),
        color=BRAND_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Düymələrdən istifadə edərək asanlıqla idarə edin • abi-bot")
    await ctx.send(embed=embed, view=TempVoiceControlView())


@ses_group.command(name="ad", aliases=["name"])
async def ses_ad(ctx, *, yeni_ad: str):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    yeni_ad = yeni_ad[:32]
    await chan.edit(name=yeni_ad)
    await send_success_card(ctx, "Otaq Adı Dəyişdirildi", f"✅ Otağın yeni adı: **{yeni_ad}**")


@ses_group.command(name="limit")
async def ses_limit(ctx, say: int):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    say = max(0, min(say, 99))
    await chan.edit(user_limit=say)
    limit_text = f"**{say} nəfər**" if say > 0 else "**Limitsiz**"
    await send_success_card(ctx, "Limit Yeniləndi", f"✅ Otağın istifadəçi limiti: {limit_text}")


@ses_group.command(name="kilid", aliases=["lock"])
async def ses_kilid(ctx):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    await chan.set_permissions(ctx.guild.default_role, connect=False)
    await send_success_card(ctx, "Otaq Kilidləndi", "🔒 Otaq kilidləndi! Artıq icazəsiz heç kim qoşula bilməz.")


@ses_group.command(name="ac", aliases=["unlock"])
async def ses_ac(ctx):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    await chan.set_permissions(ctx.guild.default_role, connect=True)
    await send_success_card(ctx, "Kilid Açıldı", "🔓 Otağın kilidi açıldı! Hər kəs qoşula bilər.")


@ses_group.command(name="at", aliases=["kick"])
async def ses_at(ctx, member: discord.Member):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    if member == ctx.author:
        await send_error_card(ctx, "Xəta", "Özünüzü otaqdan ata bilməzsiniz.")
        return
    if member.voice and member.voice.channel == chan:
        await member.move_to(None, reason=f"{ctx.author} tərəfindən temp otaqdan çıxarıldı")
        await chan.set_permissions(member, connect=False)
        await send_success_card(ctx, "İstifadəçi Çıxarıldı", f"👢 {member.mention} otaqdan çıxarıldı və təkrar girişi bağlandı.")
    else:
        await send_error_card(ctx, "Xəta", f"{member.mention} sizin otaqda deyil.")


@ses_group.command(name="devret", aliases=["transfer"])
async def ses_devret(ctx, member: discord.Member):
    chan = get_user_temp_channel(ctx.author)
    if not chan:
        await send_error_card(ctx, "İcazə Yoxdur", "Bu əmri istifadə etmək üçün özünüzə aid TempVoice səs otağında olmalısınız.")
        return
    if member == ctx.author or member.bot:
        await send_error_card(ctx, "Xəta", "Keçərsiz istifadəçi.")
        return
    if not member.voice or member.voice.channel != chan:
        await send_error_card(ctx, "Xəta", f"{member.mention} bu səs otağında olmalıdır.")
        return

    db.add_temp_channel(chan.id, ctx.guild.id, member.id)
    await chan.set_permissions(member, manage_channels=True, move_members=True, mute_members=True, deafen_members=True)
    await send_success_card(ctx, "Otaq Sahibliyi Verildi", f"👑 Otaq rəhbərliyi {member.mention} istifadəçisinə verildi.")


@ses_group.command(name="lider", aliases=["claim"])
async def ses_lider(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await send_error_card(ctx, "Xəta", "Səs kanalında deyilsiniz.")
        return
    chan = ctx.author.voice.channel
    temp_data = db.get_temp_channel(chan.id)
    if not temp_data:
        await send_error_card(ctx, "Xəta", "Bu kanal xüsusi TempVoice otağı deyil.")
        return

    owner_id = temp_data["owner_id"]
    owner_present = any(m.id == owner_id for m in chan.members)
    if owner_present and owner_id != ctx.author.id:
        await send_error_card(ctx, "Xəta", "Otağın əsl sahibi hələ də kanaldadır.")
        return

    db.add_temp_channel(chan.id, ctx.guild.id, ctx.author.id)
    await chan.set_permissions(ctx.author, manage_channels=True, move_members=True, mute_members=True, deafen_members=True)
    await send_success_card(ctx, "Otaq Liderliyi Alındı", f"👑 Təbriklər, artıq bu otağın rəhbəri {ctx.author.mention}!")


@bot.command(name="settempvoice")
@commands.has_permissions(administrator=True)
async def prefix_settempvoice(ctx, channel: discord.VoiceChannel):
    db.set_guild_setting(ctx.guild.id, "tempvoice_channel", str(channel.id))
    await send_success_card(
        ctx,
        "TempVoice Quraşdırıldı",
        f"✅ **Otaq Yarat** kanalı təyin edildi: {channel.mention}\nİstifadəçilər bu kanala daxil olduqda onlar üçün avtomatik şəxsi otaq açılacaq."
    )


@bot.command(name="setwelcome")
@commands.has_permissions(administrator=True)
async def prefix_setwelcome(ctx, channel: discord.TextChannel):
    db.set_guild_setting(ctx.guild.id, "welcome_channel", str(channel.id))
    await send_success_card(
        ctx,
        "Xoşgəldin Kanalı Təyin Edildi",
        f"✅ Yeni qoşulan üzvlər üçün vizual qarşılama kartı {channel.mention} kanalına göndəriləcək."
    )


@bot.command(name="setautorole")
@commands.has_permissions(administrator=True)
async def prefix_setautorole(ctx, role: discord.Role):
    db.set_guild_setting(ctx.guild.id, "autorole", str(role.id))
    await send_success_card(
        ctx,
        "Auto-Role Təyin Edildi",
        f"✅ Yeni qoşulan bütün üzvlərə avtomatik {role.mention} rolu veriləcək."
    )


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
            "• `abi qrafik [@user]` — Həftəlik səs aktivliyi diaqramı\n"
            "• `abi hesabat [gun/hefte/ay]` — Periodik səs hesabatı"
        ),
        inline=False
    )

    embed.add_field(
        name="⭐ Səviyyə & XP Sistemi",
        value=(
            "• `abi seviyye [@user]` — Vizual Rank kartı (Level, XP, Progress)\n"
            "• `abi xptop [say]` — Ən yüksək səviyyəli üzvlər"
        ),
        inline=False
    )

    embed.add_field(
        name="🔥 Seriya (Streak) Sistemi",
        value=(
            "• `abi streak [@user]` — Gündəlik səs aktivliyi seriyanız\n"
            "• `abi streaktop [say]` — Ən yüksək seriyaya sahib üzvlər"
        ),
        inline=False
    )

    embed.add_field(
        name="🔊 TempVoice (Şəxsi Səs Otağı)",
        value=(
            "• `abi ses ad [ad]` — Otağın adını dəyişir\n"
            "• `abi ses limit [say]` — İstifadəçi limiti\n"
            "• `abi ses kilid / ac` — Otağı kilidləyir / açır\n"
            "• `abi ses at @user` — İstifadəçini otaqdan atır\n"
            "• `abi ses devret @user` — Sahibliyi verir\n"
            "• `abi ses lider` — Otaq rəhbərliyini ələ alır"
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
        name="⚙️ Admin Quraşdırma",
        value=(
            "• `abi settempvoice #kanal` — TempVoice kanalı təyin edir\n"
            "• `abi setwelcome #kanal` — Xoşgəldin kartı kanalı\n"
            "• `abi setautorole @rol` — Avtomatik rol\n"
            "• `abi setlevelup #kanal` — Level bildiriş kanalı"
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


@bot.tree.command(name="seviyye", description="İstifadəçinin cari Level və XP kartını (Qrafik kartla) göstərir.")
@app_commands.describe(member="Levelinə baxmaq istədiyiniz üzv")
async def slash_seviyye(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    user = db.get_user(target.id)

    current_level = int(user.get("level") or 1) if user else 1
    current_xp = int(user.get("xp") or 0) if user else 0
    next_level_xp = db.xp_for_level(current_level + 1)
    level_start_xp = db.xp_for_level(current_level)
    rank_position = db.get_user_rank(target.id)
    streak_info = db.get_streak(target.id)
    streak = streak_info.get("streak", 0)

    try:
        avatar_bytes = await target.display_avatar.read()
    except Exception:
        avatar_bytes = None

    card_buf = graphics.generate_rank_card(
        avatar_bytes=avatar_bytes,
        username=target.name,
        display_name=target.display_name,
        level=current_level,
        xp=current_xp,
        current_level_xp=level_start_xp,
        next_level_xp=next_level_xp,
        rank_position=rank_position,
        streak=streak,
    )
    file = discord.File(card_buf, filename="rank.png")

    embed = discord.Embed(
        title=f"⭐ Səviyyə Kartı — {target.display_name}",
        color=BRAND_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url="attachment://rank.png")
    streak_note = f" • 🔥 Seriya: **{streak} gün**" if streak > 0 else ""
    embed.description = f"**İstifadəçi:** {target.mention}\n**Sıralama:** `🏆 #{rank_position}` | **Səviyyə:** `🏅 {current_level}`{streak_note}"
    embed.set_footer(text="Hər 5 dəqiqə səsdə qalmağa 10 XP • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)

    await interaction.followup.send(embed=embed, file=file)


@bot.tree.command(name="qrafik", description="Son 7 günün səs aktivliyini vizual diaqramla göstərir.")
@app_commands.describe(member="Qrafikini görmək istədiyiniz üzv (boş qoysanız özünüz)")
async def slash_qrafik(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer()
    target = member or interaction.user
    history = db.get_user_daily_history(target.id, days=7)

    chart_buf = graphics.generate_voice_chart(history, target.display_name)
    file = discord.File(chart_buf, filename="activity.png")

    total_sec = sum(d["seconds"] for d in history)
    embed = discord.Embed(
        title=f"📈 Həftəlik Səs Aktivliyi — {target.display_name}",
        description=f"{target.mention} üçün son 7 günün statistikası:\n**Ümumi Aktivlik:** `{format_time(total_sec)}`",
        color=BRAND_COLOR,
        timestamp=datetime.utcnow()
    )
    embed.set_image(url="attachment://activity.png")
    embed.set_footer(text=f"Sorğulayan: {interaction.user.display_name} • abi-bot", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed, file=file)


@bot.tree.command(name="streak", description="Gündəlik səs aktivliyi seriyanızı (Streak) göstərir.")
@app_commands.describe(member="Seriyasına baxmaq istədiyiniz üzv")
async def slash_streak(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    streak_data = db.get_streak(target.id)
    cur = streak_data["streak"]
    highest = streak_data["highest_streak"]
    active_today = streak_data["active_today"]

    status_str = "🔥 Bu gün aktivdir (+bonus alınıb)" if active_today else "⏳ Bu gün hələ 15 dəqiqə tamamlanmayıb"

    embed = discord.Embed(
        title=f"🔥 Gündəlik Səs Seriyası — {target.display_name}",
        color=0xE67E22 if cur > 0 else 0x95A5A6,
        timestamp=datetime.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.description = f"**İstifadəçi:** {target.mention}\n**Status:** `{status_str}`"
    embed.add_field(name="🔥 Cari Seriya", value=f"**`{cur} Gün`**", inline=True)
    embed.add_field(name="🏆 Rekord Seriya", value=f"**`{highest} Gün`**", inline=True)
    embed.add_field(
        name="💡 Seriya Qaydası",
        value="Hər gün ən azı **15 dəqiqə** səs kanalında vaxt keçir, seriyanı artır və hər gün üçün əlavə **XP bonusu** qazan!",
        inline=False
    )
    embed.set_footer(text="Seriyanı qoru, zirvəyə qalx! • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="streaktop", description="Serverdə ən yüksək səs seriyasına (Streak) sahib üzvlər.")
@app_commands.describe(say="Göstəriləcək üzv sayı (məs: 10)")
async def slash_streaktop(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 25))
    rows = db.get_streak_leaderboard(say)
    if not rows:
        await interaction.response.send_message("📭 Hələ heç bir aktiv səs seriyası qeydə alınmayıb.", ephemeral=True)
        return

    lines = []
    for idx, r in enumerate(rows, start=1):
        medal = get_medal(idx)
        name = r.get("display_name") or r.get("username") or "Naməlum"
        cur = r.get("current_streak") or 0
        high = r.get("highest_streak") or 0
        lines.append(f"{medal} **{name}** — 🔥 **`{cur} gün`** (Rekord: `{high}` gün)")

    embed = discord.Embed(
        title=f"🔥 Ən Yüksək Səs Seriyaları • Top {len(rows)}",
        description="\n".join(lines),
        color=0xE67E22,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Gündəlik səs aktivliyi liderləri • abi-bot")
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


@bot.tree.command(name="setchannel", description="Səviyyə (Level-Up) bildiriş kanalını təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Səviyyə bildirişlərinin göndəriləcəyi mətn kanalı")
async def slash_setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return

    db.set_guild_setting(interaction.guild.id, "levelup_channel", str(channel.id))
    embed = discord.Embed(
        title="⚙️ Kanal Quraşdırması Uğurlu",
        description=f"✅ Səviyyə (Level-Up) bildirişləri artıq {channel.mention} kanalına göndəriləcək.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.command(name="setlevelup", aliases=["setlevelchannel"])
@commands.has_permissions(administrator=True)
async def prefix_setlevelup(ctx, channel: discord.TextChannel):
    db.set_guild_setting(ctx.guild.id, "levelup_channel", str(channel.id))
    await send_success_card(
        ctx,
        "Level Bildiriş Kanalı Təyin Edildi",
        f"✅ Səviyyə (Level-Up) bildirişləri artıq {channel.mention} kanalına göndəriləcək."
    )


@bot.tree.command(name="settempvoice", description="TempVoice (Otaq Yarat) kanalını təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="İstifadəçilər qoşulduqda şəxsi otaq yaradılacaq səs kanalı")
async def slash_settempvoice(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return
    db.set_guild_setting(interaction.guild.id, "tempvoice_channel", str(channel.id))
    embed = discord.Embed(
        title="🔊 TempVoice Quraşdırıldı",
        description=f"✅ **Otaq Yarat** kanalı təyin edildi: {channel.mention}\nİstifadəçilər bu kanala daxil olduqda onlar üçün avtomatik şəxsi otaq açılacaq.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setwelcome", description="Yeni üzvlər üçün vizual qarşılama kartı kanalını təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Xoşgəldin mesajlarının göndəriləcəyi mətn kanalı")
async def slash_setwelcome(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return
    db.set_guild_setting(interaction.guild.id, "welcome_channel", str(channel.id))
    embed = discord.Embed(
        title="👋 Xoşgəldin Kanalı Təyin Edildi",
        description=f"✅ Yeni qoşulan üzvlər üçün vizual qarşılama kartı {channel.mention} kanalına göndəriləcək.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setautorole", description="Yeni üzvlərə avtomatik veriləcək rolu təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(role="Yeni qoşulan üzvlərə avtomatik veriləcək rol")
async def slash_setautorole(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return
    db.set_guild_setting(interaction.guild.id, "autorole", str(role.id))
    embed = discord.Embed(
        title="🎭 Auto-Role Təyin Edildi",
        description=f"✅ Yeni qoşulan bütün üzvlərə avtomatik {role.mention} rolu veriləcək.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="setupdatelog", description="Bot yenilənmə bildirişlərinin (Update Log) göndəriləcəyi kanalı təyin edir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Yenilənmə bildirişlərinin göndəriləcəyi mətn kanalı")
async def slash_setupdatelog(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return
    db.set_guild_setting(interaction.guild.id, "update_log_channel", str(channel.id))
    embed = discord.Embed(
        title="📢 Update Log Kanalı Təyin Edildi",
        description=f"✅ Bot yenilənmələri və yeni funksiyalar artıq {channel.mention} kanalına göndəriləcək.",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Quraşdıran: {interaction.user.display_name} • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.command(name="setupdatelog")
@commands.has_permissions(administrator=True)
async def prefix_setupdatelog(ctx, channel: discord.TextChannel):
    db.set_guild_setting(ctx.guild.id, "update_log_channel", str(channel.id))
    await send_success_card(
        ctx,
        "Update Log Kanalı Təyin Edildi",
        f"✅ Bot yenilənmələri və yeni funksiyalar artıq {channel.mention} kanalına göndəriləcək."
    )


@bot.tree.command(name="updatelog", description="Update Log kanalına xüsusi yenilənmə elanı göndərir.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(title="Yenilənmə başlığı", changes="Dəyişikliklər (vergül və ya | ilə ayırın)")
async def slash_updatelog(interaction: discord.Interaction, title: str, changes: str):
    if not interaction.guild:
        await interaction.response.send_message("❌ Bu əmr yalnız server daxilində işləyir.", ephemeral=True)
        return

    chan_id = db.get_guild_setting(interaction.guild.id, "update_log_channel") or (UPDATE_LOG_CHANNEL_ID if UPDATE_LOG_CHANNEL_ID and str(UPDATE_LOG_CHANNEL_ID) != "0" else None)
    if not chan_id:
        await interaction.response.send_message("❌ Update Log kanalı təyin edilməyib. Əvvəlcə `/setupdatelog #kanal` edin.", ephemeral=True)
        return

    chan = interaction.guild.get_channel(int(chan_id))
    if not chan:
        await interaction.response.send_message("❌ Təyin olunmuş kanal tapılmadı.", ephemeral=True)
        return

    change_lines = [c.strip() for c in changes.replace("|", "\n").split("\n") if c.strip()]
    embed = discord.Embed(
        title=f"📢 {title}",
        description="Serverimiz üçün botda aşağıdakı yeniliklər tətbiq edildi:\n\n" + "\n".join(f"• {c}" for c in change_lines),
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Yenilənməni paylaşan: {interaction.user.display_name} • abi-bot", icon_url=interaction.user.display_avatar.url)
    await chan.send(embed=embed)
    await interaction.response.send_message(f"✅ Yenilənmə elanı {chan.mention} kanalına uğurla göndərildi.", ephemeral=True)


@bot.command(name="updatelog")
@commands.has_permissions(administrator=True)
async def prefix_updatelog(ctx, *, text: str):
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if not parts:
        await ctx.send("❌ İstifadə: `abi updatelog Başlıq | Dəyişiklik 1 | Dəyişiklik 2 ...`")
        return

    title = parts[0]
    changes = parts[1:] if len(parts) > 1 else [parts[0]]

    chan_id = db.get_guild_setting(ctx.guild.id, "update_log_channel") or (UPDATE_LOG_CHANNEL_ID if UPDATE_LOG_CHANNEL_ID and str(UPDATE_LOG_CHANNEL_ID) != "0" else None)
    if not chan_id:
        await send_error_card(ctx, "Xəta", "Update Log kanalı təyin edilməyib. Əvvəlcə `abi setupdatelog #kanal` edin.")
        return

    chan = ctx.guild.get_channel(int(chan_id))
    if not chan:
        await send_error_card(ctx, "Xəta", "Təyin olunmuş kanal tapılmadı.")
        return

    embed = discord.Embed(
        title=f"📢 {title}",
        description="Serverimiz üçün botda aşağıdakı yeniliklər tətbiq edildi:\n\n" + "\n".join(f"• {c}" for c in changes),
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Yenilənməni paylaşan: {ctx.author.display_name} • abi-bot", icon_url=ctx.author.display_avatar.url)
    await chan.send(embed=embed)
    await send_success_card(ctx, "Update Log Göndərildi", f"✅ Yenilənmə elanı {chan.mention} kanalına uğurla göndərildi.")


# ==================== MUSİQİ SİSTEMİ (MUSIC COMMANDS) ====================

music_players: dict[int, GuildMusicPlayer] = {}

def get_music_player(guild: discord.Guild) -> GuildMusicPlayer:
    if guild.id not in music_players:
        music_players[guild.id] = GuildMusicPlayer(bot, guild)
    return music_players[guild.id]


async def _play_helper(ctx_or_interaction, query: str):
    """Mahnını axtarıb növbəyə əlavə edən və oxutmağa başlayan ümumi köməkçi funksiya."""
    is_slash = isinstance(ctx_or_interaction, discord.Interaction)
    user = ctx_or_interaction.user if is_slash else ctx_or_interaction.author
    guild = ctx_or_interaction.guild

    if not user.voice or not user.voice.channel:
        msg = "❌ Mahnı qoşmaq üçün əvvəlcə bir səs kanalında olmalısınız."
        if is_slash:
            await ctx_or_interaction.response.send_message(msg, ephemeral=True)
        else:
            await send_error_card(ctx_or_interaction, "Xəta", msg)
        return

    if is_slash:
        await ctx_or_interaction.response.defer()

    voice_channel = user.voice.channel
    player = get_music_player(guild)
    player.text_channel = ctx_or_interaction.channel

    # Bot səs kanalında deyilsə və ya başqa kanaldadırsa, qoşuluruq
    vc = guild.voice_client
    if vc is None:
        try:
            player.voice_client = await voice_channel.connect()
        except Exception as e:
            err_msg = f"Səs kanalına qoşularkən xəta: {e}"
            if is_slash:
                await ctx_or_interaction.followup.send(f"❌ {err_msg}")
            else:
                await send_error_card(ctx_or_interaction, "Xəta", err_msg)
            return
    else:
        player.voice_client = vc
        if vc.channel != voice_channel:
            await vc.move_to(voice_channel)

    loop = bot.loop or asyncio.get_event_loop()
    try:
        song_data = await search_song_info(query, loop=loop)
    except Exception as e:
        err_msg = f"Mahnı axtarılarkən xəta yarandı: {e}"
        if is_slash:
            await ctx_or_interaction.followup.send(f"❌ {err_msg}")
        else:
            await send_error_card(ctx_or_interaction, "Xəta", err_msg)
        return

    if not song_data:
        err_msg = "Mahnı tapılmadı. Zəhmət olmasa başqa bir axtarış sözü və ya link yoxlayın."
        if is_slash:
            await ctx_or_interaction.followup.send(f"❌ {err_msg}")
        else:
            await send_error_card(ctx_or_interaction, "Xəta", err_msg)
        return


    song = Song(song_data, user)
    player.queue.append(song)

    is_currently_playing = player._playing or (player.voice_client and player.voice_client.is_playing())

    if not is_currently_playing:
        asyncio.create_task(player.play_next_song())
        msg = f"🎶 **{song.title}** hazırlanır və oxudulur..."
    else:
        msg = f"✅ **[{song.title}]({song.webpage_url})** növbəyə əlavə edildi! (Mövqe: `#{len(player.queue)}`)"

    embed = discord.Embed(
        title="🎵 Musiqi Növbəsi",
        description=msg,
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=f"Müddət: {song.formatted_duration} • İstəyən: {user.display_name}")

    if is_slash:
        await ctx_or_interaction.followup.send(embed=embed)
    else:
        await ctx_or_interaction.send(embed=embed)


@bot.command(name="play", aliases=["p"])
async def prefix_play(ctx, *, query: str):
    """Mahnı oxudur: abi play <mahnı adı və ya link>"""
    await _play_helper(ctx, query)


@bot.tree.command(name="play", description="Səs kanalında mahnı oxudur (YouTube / SoundCloud / Link).")
@app_commands.describe(query="Mahnı adı və ya YouTube / SoundCloud linki")
async def slash_play(interaction: discord.Interaction, query: str):
    await _play_helper(interaction, query)


@bot.command(name="pause")
async def prefix_pause(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await send_success_card(ctx, "Pauza", "⏸️ Mahnı dayandırıldı.")
    else:
        await send_error_card(ctx, "Xəta", "Hazırda oxunan mahnı yoxdur.")


@bot.tree.command(name="pause", description="Oxunan mahnını müvəqqəti dayandırır (Pauza).")
async def slash_pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Mahnı dayandırıldı.")
    else:
        await interaction.response.send_message("❌ Hazırda oxunan mahnı yoxdur.", ephemeral=True)


@bot.command(name="resume")
async def prefix_resume(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await send_success_card(ctx, "Davam Edir", "▶️ Mahnı davam etdirilir.")
    else:
        await send_error_card(ctx, "Xəta", "Pauzada olan mahnı yoxdur.")


@bot.tree.command(name="resume", description="Pauzada olan mahnını davam etdirir.")
async def slash_resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Mahnı davam etdirilir.")
    else:
        await interaction.response.send_message("❌ Pauzada olan mahnı yoxdur.", ephemeral=True)


@bot.command(name="skip", aliases=["s", "next"])
async def prefix_skip(ctx):
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await send_success_card(ctx, "Mahnı Keçildi", "⏭️ Növbəti mahnıya keçildi.")
    else:
        await send_error_card(ctx, "Xəta", "Keçiləcək aktiv mahnı yoxdur.")


@bot.tree.command(name="skip", description="Növbəti mahnıya keçir.")
async def slash_skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Növbəti mahnıya keçildi.")
    else:
        await interaction.response.send_message("❌ Keçiləcək aktiv mahnı yoxdur.", ephemeral=True)


@bot.command(name="stop", aliases=["leave", "dc"])
async def prefix_stop(ctx):
    player = get_music_player(ctx.guild)
    player.queue.clear()
    player.is_looping = False
    vc = ctx.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
        await send_success_card(ctx, "Dayandırıldı", "⏹️ Musiqi dayandırıldı və bot kanaldan çıxdı.")
    else:
        await send_error_card(ctx, "Xəta", "Bot səs kanalında deyil.")


@bot.tree.command(name="stop", description="Musiqini dayandırır, növbəni təmizləyir və kanaldan çıxır.")
async def slash_stop(interaction: discord.Interaction):
    player = get_music_player(interaction.guild)
    player.queue.clear()
    player.is_looping = False
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Musiqi dayandırıldı və bot kanaldan çıxdı.")
    else:
        await interaction.response.send_message("❌ Bot səs kanalında deyil.", ephemeral=True)


@bot.command(name="queue", aliases=["q"])
async def prefix_queue(ctx):
    player = get_music_player(ctx.guild)
    if not player.queue and not player.current:
        await send_error_card(ctx, "Növbə Boşdur", "Hazırda musiqi növbəsində heç bir mahnı yoxdur.")
        return

    lines = []
    if player.current:
        lines.append(f"▶️ **İndi oxunur:** [{player.current.title}]({player.current.webpage_url}) (`{player.current.formatted_duration}`) — {player.current.requester.mention}\n")

    for i, s in enumerate(list(player.queue)[:10], start=1):
        lines.append(f"**{i}.** [{s.title}]({s.webpage_url}) (`{s.formatted_duration}`) — {s.requester.mention}")

    if len(player.queue) > 10:
        lines.append(f"\n*...və daha {len(player.queue) - 10} mahnı növbədədir*")

    loop_status = "Aktiv 🔁" if player.is_looping else "Deaktiv ⏹️"
    embed = discord.Embed(
        title=f"📜 Musiqi Növbəsi — {ctx.guild.name}",
        description="\n".join(lines),
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Təkrar rejimi: {loop_status} • Ümumi mahnı: {len(player.queue) + (1 if player.current else 0)}")
    await ctx.send(embed=embed)


@bot.tree.command(name="queue", description="Hazırkı mahnı növbəsini göstərir.")
async def slash_queue(interaction: discord.Interaction):
    player = get_music_player(interaction.guild)
    if not player.queue and not player.current:
        await interaction.response.send_message("📜 Hazırda musiqi növbəsində heç bir mahnı yoxdur.", ephemeral=True)
        return

    lines = []
    if player.current:
        lines.append(f"▶️ **İndi oxunur:** [{player.current.title}]({player.current.webpage_url}) (`{player.current.formatted_duration}`) — {player.current.requester.mention}\n")

    for i, s in enumerate(list(player.queue)[:10], start=1):
        lines.append(f"**{i}.** [{s.title}]({s.webpage_url}) (`{s.formatted_duration}`) — {s.requester.mention}")

    if len(player.queue) > 10:
        lines.append(f"\n*...və daha {len(player.queue) - 10} mahnı növbədədir*")

    loop_status = "Aktiv 🔁" if player.is_looping else "Deaktiv ⏹️"
    embed = discord.Embed(
        title=f"📜 Musiqi Növbəsi — {interaction.guild.name}",
        description="\n".join(lines),
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Təkrar rejimi: {loop_status} • Ümumi mahnı: {len(player.queue) + (1 if player.current else 0)}")
    await interaction.response.send_message(embed=embed)


@bot.command(name="loop", aliases=["repeat"])
async def prefix_loop(ctx):
    player = get_music_player(ctx.guild)
    player.is_looping = not player.is_looping
    status = "**Aktiv edildi** 🔁" if player.is_looping else "**Deaktiv edildi** ⏹️"
    await send_success_card(ctx, "Təkrar Rejimi", f"Mahnı təkrarı {status}.")


@bot.tree.command(name="loop", description="Hazırkı mahnının təkrar rejimini (Loop) açıb-bağlayır.")
async def slash_loop(interaction: discord.Interaction):
    player = get_music_player(interaction.guild)
    player.is_looping = not player.is_looping
    status = "**Aktiv edildi** 🔁" if player.is_looping else "**Deaktiv edildi** ⏹️"
    await interaction.response.send_message(f"Təkrar rejimi {status}.")


@bot.command(name="nowplaying", aliases=["np"])
async def prefix_nowplaying(ctx):
    player = get_music_player(ctx.guild)
    if not player.current:
        await send_error_card(ctx, "Xəta", "Hazırda heç bir mahnı oxunmur.")
        return

    song = player.current
    embed = discord.Embed(
        title="🎶 İndi Oxunur",
        description=f"[{song.title}]({song.webpage_url})\n\n"
                    f"👤 **İfaçı:** `{song.uploader}`\n"
                    f"⏱️ **Müddət:** `{song.formatted_duration}`\n"
                    f"🎧 **İstəyən:** {song.requester.mention}",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    loop_status = "Aktiv 🔁" if player.is_looping else "Deaktiv ⏹️"
    embed.set_footer(text=f"Təkrar: {loop_status} • Növbədə: {len(player.queue)} mahnı")
    await ctx.send(embed=embed, view=MusicControlView(player))


@bot.tree.command(name="nowplaying", description="Hazırda oxunan mahnı haqqında məlumatı və idarəetmə düymələrini göstərir.")
async def slash_nowplaying(interaction: discord.Interaction):
    player = get_music_player(interaction.guild)
    if not player.current:
        await interaction.response.send_message("❌ Hazırda heç bir mahnı oxunmur.", ephemeral=True)
        return

    song = player.current
    embed = discord.Embed(
        title="🎶 İndi Oxunur",
        description=f"[{song.title}]({song.webpage_url})\n\n"
                    f"👤 **İfaçı:** `{song.uploader}`\n"
                    f"⏱️ **Müddət:** `{song.formatted_duration}`\n"
                    f"🎧 **İstəyən:** {song.requester.mention}",
        color=0x57F287,
        timestamp=datetime.utcnow()
    )
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    loop_status = "Aktiv 🔁" if player.is_looping else "Deaktiv ⏹️"
    embed.set_footer(text=f"Təkrar: {loop_status} • Növbədə: {len(player.queue)} mahnı")
    await interaction.response.send_message(embed=embed, view=MusicControlView(player))


@bot.command(name="volume", aliases=["vol"])
async def prefix_volume(ctx, volume: int):
    player = get_music_player(ctx.guild)
    vc = ctx.guild.voice_client
    if not vc or not vc.is_playing():
        await send_error_card(ctx, "Xəta", "Hazırda oxunan mahnı yoxdur.")
        return

    vol = max(1, min(volume, 100))
    player.volume = vol / 100.0
    if vc.source:
        vc.source.volume = player.volume
    await send_success_card(ctx, "Səs Səviyyəsi", f"🔊 Səs səviyyəsi **{vol}%** olaraq təyin edildi.")


@bot.tree.command(name="volume", description="Musiqinin səs səviyyəsini tənzimləyir (1-100%).")
@app_commands.describe(percent="Səs səviyyəsi faizi (1 - 100)")
async def slash_volume(interaction: discord.Interaction, percent: int):
    player = get_music_player(interaction.guild)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("❌ Hazırda oxunan mahnı yoxdur.", ephemeral=True)
        return

    vol = max(1, min(percent, 100))
    player.volume = vol / 100.0
    if vc.source:
        vc.source.volume = player.volume
    await interaction.response.send_message(f"🔊 Səs səviyyəsi **{vol}%** təyin edildi.")


@bot.command(name="join")
async def prefix_join(ctx):
    if not ctx.author.voice or not ctx.author.voice.channel:
        await send_error_card(ctx, "Xəta", "Səs kanalında deyilsiniz.")
        return
    channel = ctx.author.voice.channel
    player = get_music_player(ctx.guild)
    vc = ctx.guild.voice_client
    if vc is None:
        player.voice_client = await channel.connect()
    else:
        player.voice_client = vc
        await vc.move_to(channel)
    await send_success_card(ctx, "Qoşuldu", f"🔊 {channel.mention} kanalına qoşuldum.")


@bot.tree.command(name="join", description="Botu olduğunuz səs kanalına çağırır.")
async def slash_join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("❌ Səs kanalında deyilsiniz.", ephemeral=True)
        return
    channel = interaction.user.voice.channel
    player = get_music_player(interaction.guild)
    vc = interaction.guild.voice_client
    if vc is None:
        player.voice_client = await channel.connect()
    else:
        player.voice_client = vc
        await vc.move_to(channel)
    await interaction.response.send_message(f"🔊 {channel.mention} kanalına qoşuldum.")


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
        name="🎵 Musiqi & Audio",
        value="• `/play [mahnı]` — Mahnı oxudur (YouTube / Link)\n• `/pause` / `/resume` — Pauza və davam\n• `/skip` — Növbəti mahnı\n• `/queue` — Mahnı növbəsi\n• `/loop` — Təkrar rejimi\n• `/volume [1-100]` — Səs səviyyəsi\n• `/nowplaying` — Cari mahnı və idarəetmə düymələri\n• `/stop` — Dayandırır və kanaldan çıxır",
        inline=False
    )
    embed.add_field(
        name="🎙️ Səs & Aktivlik Statistikası",
        value="• `/profil` — Səs aktivliyi və sıralama profili\n• `/top` — Ən çox səsdə qalanların lider cədvəli\n• `/qrafik` — Həftəlik səs aktivliyi diaqramı\n• `abi hesabat [gun/hefte/ay]` — Periodik hesabat",
        inline=False
    )
    embed.add_field(
        name="⭐ Səviyyə & XP Sistemi",
        value="• `/seviyye` — Vizual Rank kartı (Level, XP, Progress)\n• `/xptop` — Ən yüksək səviyyəli üzvlər",
        inline=False
    )
    embed.add_field(
        name="🔥 Seriya (Streak) Sistemi",
        value="• `/streak` — Gündəlik səs aktivliyi seriyanız\n• `/streaktop` — Ən yüksək seriyaya sahib üzvlər",
        inline=False
    )
    embed.add_field(
        name="🔊 TempVoice (Şəxsi Otaq)",
        value="• `abi ses ad / limit / kilid / ac / at / devret / lider` — Otaq idarəetməsi",
        inline=False
    )
    embed.add_field(
        name="🛡️ Moderasiya & Təhlükəsizlik",
        value="• `/kick` — Üzvü serverdən atır\n• `/ban` — Üzvü ban edir\n• `/mute` — Timeout verir\n• `/unmute` — Timeout-u qaldırır\n• `/sil` — Mesajları toplu silir\n• `abi warn` / `abi warnings` / `abi warnlar` — Xəbərdarlıq sistemi",
        inline=False
    )
    embed.add_field(
        name="⚙️ Admin Quraşdırma",
        value="• `/settempvoice` — TempVoice kanalı\n• `/setwelcome` — Xoşgəldin kartı kanalı\n• `/setautorole` — Avtomatik rol\n• `/setchannel` — Level bildiriş kanalı\n• `/setupdatelog` — Update log kanalı\n• `/updatelog` — Yenilənmə elanı göndərir",
        inline=False
    )
    embed.add_field(
        name="🧰 Köməkçi & Digər",
        value="• `/userinfo` — İstifadəçi haqqında məlumat\n• `/serverinfo` — Server statistikası\n• `/avatar` — Profil şəkli\n• `abi poll` — Sorğu\n• `abi sifirla` — Səs sıfırlama",
        inline=False
    )
    embed.set_footer(text="Developed for your server • abi-bot", icon_url=bot.user.display_avatar.url if bot.user else None)
    await interaction.response.send_message(embed=embed)



@tasks.loop(minutes=1)
async def xp_task():
    # Hər 1 dəqiqədə səsdə olan istifadəçilərə vaxt və XP veririk
    for guild in bot.guilds:
        channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        for channel in channels:
            # AFK kanalını ötürürük
            if guild.afk_channel and channel.id == guild.afk_channel.id:
                continue

            human_members = [m for m in channel.members if not m.bot]
            if len(human_members) < XP_MIN_MEMBERS_IN_VOICE:
                continue

            for member in human_members:
                # Səs vaxtını (60 saniyə) qeyd edirik
                db.add_voice_time(member.id, member.name, member.display_name, 60)
                db.upsert_user_identity(member.id, member.name, member.display_name)
                voice_sessions[member.id] = datetime.utcnow()

                # Hər dəqiqə 5 XP veririk
                new_xp, new_level, leveled_up = db.add_xp(member.id, 5)

                if leveled_up:
                    logger.info(f"Level artdı | user={member.id} level={new_level} xp={new_xp}")

                    # Level mükafat rolunu veririk (uyğun id varsa)
                    if LEVEL_ROLE_REWARDS:
                        eligible_levels = [lv for lv in LEVEL_ROLE_REWARDS.keys() if new_level >= lv]
                        if eligible_levels:
                            target_level = max(eligible_levels)
                            role_id = LEVEL_ROLE_REWARDS.get(target_level)
                            role = guild.get_role(role_id) if role_id else None
                            if role and role not in member.roles:
                                try:
                                    await member.add_roles(role, reason=f"Level reward: {new_level}")
                                except Exception as error:
                                    logger.warning(f"Level rolu verilə bilmədi | user={member.id} role={role_id} err={error}")

                    lvl_chan_id = db.get_guild_setting(guild.id, "levelup_channel") or (LEVEL_UP_CHANNEL_ID if LEVEL_UP_CHANNEL_ID != 0 else None)
                    if lvl_chan_id:
                        try:
                            lvl_chan = guild.get_channel(int(lvl_chan_id))
                            if lvl_chan:
                                embed = discord.Embed(
                                    title="🎉 Səviyyə Yüksəldi!",
                                    description=f"Təbriklər {member.mention}! Səviyyə **{new_level}** oldunuz! 🚀",
                                    color=0xFFD700,
                                    timestamp=datetime.utcnow()
                                )
                                embed.set_thumbnail(url=member.display_avatar.url)
                                embed.set_footer(text="abi-bot Level Sistemi")
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

