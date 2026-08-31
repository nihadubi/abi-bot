import asyncio
import base64
import functools
import os
import tempfile
from collections import deque
from datetime import datetime
import discord
from discord import ui
import yt_dlp

# Cookie dəstəyi: Render env-dən YTDLP_COOKIES (base64) və ya YTDLP_COOKIES_FILE (yol)
_cookie_file = None

def _setup_cookies():
    global _cookie_file
    # 1) Əgər YTDLP_COOKIES_FILE env var varsa, birbaşa həmin yolu istifadə et
    cookie_path = os.getenv("YTDLP_COOKIES_FILE")
    if cookie_path and os.path.isfile(cookie_path):
        _cookie_file = cookie_path
        return
    # 2) Əgər YTDLP_COOKIES env var (base64-encoded Netscape cookie faylı) varsa, müvəqqəti fayla yaz
    cookie_b64 = os.getenv("YTDLP_COOKIES")
    if cookie_b64:
        try:
            raw = base64.b64decode(cookie_b64)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", prefix="ytcookies_")
            tmp.write(raw)
            tmp.close()
            _cookie_file = tmp.name
        except Exception:
            pass

_setup_cookies()

# yt-dlp konfiqurasiyası
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "extractaudio": True,
    "audioformat": "mp3",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    # YouTube bot bloklama problemini azaltmaq üçün player_client
    "extractor_args": {"youtube": {"player_client": ["ios", "mweb"]}},
}

# Əgər cookie faylı varsa, yt-dlp-yə ötürürük
if _cookie_file:
    YTDL_OPTIONS["cookiefile"] = _cookie_file

# SoundCloud fallback üçün ayrıca konfiqurasiya (YouTube bloklandıqda)
YTDL_OPTIONS_SC = {**YTDL_OPTIONS, "default_search": "scsearch"}

# ffmpeg stream parametrləri
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_sc = yt_dlp.YoutubeDL(YTDL_OPTIONS_SC)



class Song:
    """Növbədə olan tək bir mahnı obyekti."""

    def __init__(self, data: dict, requester: discord.Member):
        self.data = data
        self.requester = requester
        self.title = data.get("title", "Naməlum Mahnı")
        self.url = data.get("url")
        self.webpage_url = data.get("webpage_url", "")
        self.duration = int(data.get("duration") or 0)
        self.thumbnail = data.get("thumbnail", "")
        self.uploader = data.get("uploader", "Naməlum İfaçı")

    @property
    def formatted_duration(self) -> str:
        if self.duration <= 0:
            return "Canlı Yayım"
        minutes = self.duration // 60
        seconds = self.duration % 60
        return f"{minutes:02d}:{seconds:02d}"


class YTDLSource(discord.PCMVolumeTransformer):
    """Discord səs axını üçün mənbə."""

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title")
        self.url = data.get("url")

    @classmethod
    async def create_source(cls, song: Song, *, loop=None, volume=0.5):
        loop = loop or asyncio.get_event_loop()

        # Əgər birbaşa audio url yoxdursa və ya vaxtı keçibsə, yenidən ekstrakt edirik
        data = song.data
        if not data.get("url"):
            partial = functools.partial(ytdl.extract_info, song.webpage_url or song.title, download=False)
            data = await loop.run_in_executor(None, partial)
            if "entries" in data:
                data = data["entries"][0]

        filename = data["url"]
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data, volume=volume)


class GuildMusicPlayer:
    """Hər server üçün ayrıca musiqi pleyeri və növbə idarəçisi."""

    def __init__(self, bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue = deque()
        self.current: Song | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.is_looping: bool = False
        self.volume: float = 0.5
        self.text_channel: discord.TextChannel | None = None
        self._next = asyncio.Event()
        self._playing = False  # play_next_song loop aktiv olub-olmadığını izləyir

    async def play_next_song(self):
        """Növbəti mahnını başladır."""
        self._playing = True
        try:
            await self._play_loop()
        finally:
            self._playing = False

    async def _play_loop(self):
        """Daxili oxutma döngəsi."""
        while True:
            self._next.clear()

            if not self.is_looping or self.current is None:
                if not self.queue:
                    self.current = None
                    if self.text_channel:
                        try:
                            embed = discord.Embed(
                                title="🎵 Musiqi Növbəsi Bitdi",
                                description="Növbədə başqa mahnı qalmadı. Yeni mahnı qoşmaq üçün `/play` və ya `abi play` istifadə edin.",
                                color=0x5865F2,
                            )
                            await self.text_channel.send(embed=embed)
                        except Exception:
                            pass
                    return
                self.current = self.queue.popleft()

            try:
                source = await YTDLSource.create_source(self.current, loop=self.bot.loop, volume=self.volume)
            except Exception as e:
                if self.text_channel:
                    try:
                        await self.text_channel.send(f"❌ **{self.current.title}** mahnısı oxunarkən xəta yarandı: {e}")
                    except Exception:
                        pass
                self.current = None
                continue

            if not self.voice_client or not self.voice_client.is_connected():
                return

            try:
                self.voice_client.play(
                    source,
                    after=lambda _: self.bot.loop.call_soon_threadsafe(self._next.set)
                )
            except Exception as e:
                if self.text_channel:
                    try:
                        await self.text_channel.send(f"❌ Audio oxutma xətası (ffmpeg tapılmadı?): {e}")
                    except Exception:
                        pass
                self.current = None
                # ffmpeg yoxdursa disconnect olub qayıdırıq
                if self.voice_client and self.voice_client.is_connected():
                    await self.voice_client.disconnect()
                return

            # Mahnı başladıqda gözəl embed və idarəetmə düymələri göndəririk
            if self.text_channel:
                try:
                    embed = discord.Embed(
                        title="🎶 İndi Oxunur",
                        description=f"[{self.current.title}]({self.current.webpage_url})\n\n"
                                    f"👤 **İfaçı:** `{self.current.uploader}`\n"
                                    f"⏱️ **Müddət:** `{self.current.formatted_duration}`\n"
                                    f"🎧 **İstəyən:** {self.current.requester.mention}",
                        color=0x57F287,
                        timestamp=datetime.utcnow()
                    )
                    if self.current.thumbnail:
                        embed.set_thumbnail(url=self.current.thumbnail)
                    
                    loop_status = "Aktiv 🔁" if self.is_looping else "Deaktiv ⏹️"
                    embed.set_footer(text=f"Təkrar: {loop_status} • Növbədə: {len(self.queue)} mahnı • abi-bot")
                    
                    view = MusicControlView(self)
                    await self.text_channel.send(embed=embed, view=view)
                except Exception:
                    pass

            await self._next.wait()


class MusicControlView(ui.View):
    """Musiqi pleyeri üçün interaktiv Discord UI düymələri."""

    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=180)
        self.player = player

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        if not interaction.user.voice or not self.player.voice_client:
            await interaction.response.send_message("❌ Musiqini idarə etmək üçün bot ilə eyni səs kanalında olmalısınız.", ephemeral=True)
            return False
        if interaction.user.voice.channel != self.player.voice_client.channel:
            await interaction.response.send_message("❌ Musiqini idarə etmək üçün bot ilə eyni səs kanalında olmalısınız.", ephemeral=True)
            return False
        return True

    @ui.button(label="Pauza / Davam", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def toggle_play(self, interaction: discord.Interaction, button: ui.Button):
        vc = self.player.voice_client
        if not vc:
            await interaction.response.send_message("❌ Bot səs kanalında deyil.", ephemeral=True)
            return

        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Mahnı dayandırıldı (Pauza).", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Mahnı davam etdirilir.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Hazırda heç bir mahnı oxunmur.", ephemeral=True)

    @ui.button(label="Keç (Skip)", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_playing():
            await interaction.response.send_message("❌ Keçiləcək aktiv mahnı yoxdur.", ephemeral=True)
            return

        vc.stop()
        await interaction.response.send_message("⏭️ Növbəti mahnıya keçildi.", ephemeral=True)

    @ui.button(label="Təkrar (Loop)", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def toggle_loop(self, interaction: discord.Interaction, button: ui.Button):
        self.player.is_looping = not self.player.is_looping
        status = "**Aktiv edildi** 🔁" if self.player.is_looping else "**Deaktiv edildi** ⏹️"
        await interaction.response.send_message(f"Təkrar rejimi {status}", ephemeral=True)

    @ui.button(label="Növbə", style=discord.ButtonStyle.secondary, emoji="📜")
    async def show_queue(self, interaction: discord.Interaction, button: ui.Button):
        if not self.player.queue and not self.player.current:
            await interaction.response.send_message("📜 Növbə hazırda boşdur.", ephemeral=True)
            return

        lines = []
        if self.player.current:
            lines.append(f"▶️ **İndi oxunur:** {self.player.current.title} (`{self.player.current.formatted_duration}`)")

        for i, s in enumerate(list(self.player.queue)[:10], start=1):
            lines.append(f"**{i}.** {s.title} (`{s.formatted_duration}`) — {s.requester.mention}")

        if len(self.player.queue) > 10:
            lines.append(f"\n*...və daha {len(self.player.queue) - 10} mahnı*")

        embed = discord.Embed(
            title="📜 Musiqi Növbəsi",
            description="\n".join(lines),
            color=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(label="Dayandır & Çıx", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction: discord.Interaction, button: ui.Button):
        self.player.queue.clear()
        self.player.is_looping = False
        vc = self.player.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("⏹️ Musiqi dayandırıldı və bot kanaldan çıxdı.", ephemeral=True)

