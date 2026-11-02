import asyncio
from contextlib import closing
import logging
import os
import re
import sqlite3
from types import SimpleNamespace
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
import graphics
from logging_system import emit_log, invite_snapshot



load_dotenv()

PREFIX = "abi "
TOKEN = os.getenv("TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
AI_CHAT_HISTORY = defaultdict(lambda: deque(maxlen=10))  # Son 10 mesajı yadda saxlayır
BASE_DIR = Path(__file__).resolve().parent

# Bot sahibi — random ilişmə siyahısına düşmür, AI heç vaxt ona söyüş söyməz
BOT_OWNER_USERNAMES = {"nihadubi"}  # Discord username və ya display name

# Moderasiya və anti-spam ayarları
ANTI_SPAM_ENABLED = False
SPAM_WINDOW_SECONDS = 8
SPAM_MESSAGE_THRESHOLD = 5
SPAM_TIMEOUT_MINUTES = 2


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.moderation = True
intents.invites = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None, max_messages=10000)
# Baza faylını layihə qovluğunda saxlayırıq. SQLite backup açıq DB-dən də tutarlı
# snapshot götürür; xam DB/WAL/SHM fayllarını os.replace ilə daşımaq olmaz.
configured_db_path = os.getenv("DATABASE_PATH", "").strip()
DB_FILE = configured_db_path or str(BASE_DIR / "abi_bot.db")
OLD_DB_FILE = str(BASE_DIR / "voice_stats.db")
if not os.path.exists(DB_FILE) and os.path.exists(OLD_DB_FILE):
    migration_file = DB_FILE + ".migrating"
    try:
        with closing(sqlite3.connect(OLD_DB_FILE, timeout=30)) as source, closing(sqlite3.connect(migration_file, timeout=30)) as destination:
            source.backup(destination)
        os.replace(migration_file, DB_FILE)
    except (OSError, sqlite3.Error) as error:
        # Keep the existing database usable if Windows or another process prevents a snapshot.
        try:
            if os.path.exists(migration_file):
                os.remove(migration_file)
        except OSError:
            pass
        if configured_db_path:
            raise
        DB_FILE = OLD_DB_FILE
        logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
        logging.getLogger("abi_bot").warning(
            "DB migration to abi_bot.db did not complete; continuing with voice_stats.db: %s", error
        )
db = Database(DB_FILE)
bot.db = db

# Log sistemini aktivləşdiririk
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("abi_bot")

# Səsdə olan istifadəçilərin aktiv sessiya başlanğıc vaxtını yadda saxlayırıq

# Mesaj spamını izləmək üçün istifadəçi vaxtlarını saxlayırıq
spam_tracker = defaultdict(deque)

invite_cache = {}
invite_locks = defaultdict(asyncio.Lock)

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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Sorğulayan: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


async def send_success_card(ctx, title: str, description: str, color: int = SUCCESS_COLOR):
    # Gözəl yaşıl uğur kartı
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"İcra edən: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


async def initialize_invite_cache(guild):
    """Establish a fresh invite baseline so offline use is never attributed to a later join."""
    try:
        invites = [invite_snapshot(invite) for invite in await guild.invites()]
        vanity = getattr(guild, "vanity_invite", None)
        if vanity:
            try:
                invite = await vanity()
                if invite:
                    item = invite_snapshot(invite)
                    item["code"] = f"vanity:{item['code']}"
                    invites.append(item)
            except (discord.Forbidden, discord.HTTPException):
                pass
        invite_cache[guild.id] = {item["code"]: item for item in invites}
        db.save_invite_snapshots(guild.id, invites)
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.info("Invite cache əlçatan deyil | guild=%s: %s", guild.id, error)


async def identify_join_invite(guild):
    """Return an inviter only when exactly one invite use-count increased."""
    async with invite_locks[guild.id]:
        before = invite_cache.get(guild.id, {})
        current_items = []
        try:
            current_items = [invite_snapshot(invite) for invite in await guild.invites()]
        except (discord.Forbidden, discord.HTTPException) as error:
            return {"known": False, "kind": "unknown", "reason": f"Invite siyahısı oxunmadı: {error}"}
        vanity = getattr(guild, "vanity_invite", None)
        if vanity:
            try:
                invite = await vanity()
                if invite:
                    item = invite_snapshot(invite)
                    item["code"] = f"vanity:{item['code']}"
                    current_items.append(item)
            except (discord.Forbidden, discord.HTTPException):
                pass

        increased = []
        current = {item["code"]: item for item in current_items}
        for code, item in current.items():
            old_uses = int(before.get(code, {}).get("uses") or 0)
            new_uses = int(item.get("uses") or 0)
            if new_uses > old_uses:
                increased.append((item, new_uses - old_uses))
        invite_cache[guild.id] = current
        db.save_invite_snapshots(guild.id, current_items)
        if len(increased) != 1:
            return {"known": False, "kind": "unknown", "reason": "Bir dəvət linkini dəqiq ayırmaq mümkün olmadı."}
        item, delta = increased[0]
        if delta != 1:
            return {"known": False, "kind": "unknown", "reason": "Dəvət istifadələri üst-üstə düşdü; inviter təxmin edilmədi."}
        is_vanity = str(item["code"]).startswith("vanity:")
        return {"known": True, "kind": "vanity" if is_vanity else "invite",
                "code": str(item["code"]).removeprefix("vanity:"), "url": item["url"],
                "inviter_id": item.get("inviter_id"), "inviter_name": item.get("inviter_name"),
                "channel_id": item.get("channel_id"), "uses": item.get("uses")}


COMMAND_GUIDANCE = {
    "warn": ("`abi warn @istifadəçi [səbəb]`", "`abi warn @Nihad Spam paylaşdı`"),
    "warnings": ("`abi warnings [@istifadəçi]`", "`abi warnings @Nihad`"),
    "warnlar": ("`abi warnlar [say]`", "`abi warnlar 10`"),
    "delwarn": ("`abi delwarn [warn ID]`", "`abi delwarn 12`"),
    "clearwarn": ("`abi clearwarn @istifadəçi`", "`abi clearwarn @Nihad`"),
    "sil": ("`abi sil [say]`", "`abi sil 25`"),
    "temizle": ("`abi sil [say]`", "`abi sil 25`"),
    "mute": ("`abi mute @istifadəçi|ID [dəqiqə] [səbəb]`", "`abi mute 123456789012345678 30 Təhqir`"),
    "unmute": ("`abi unmute @istifadəçi|ID`", "`abi unmute 123456789012345678`"),
    "kick": ("`abi kick @istifadəçi|ID [səbəb]`", "`abi kick 123456789012345678 Qaydaları pozdu`"),
    "ban": ("`abi ban @istifadəçi|ID [səbəb]`", "`abi ban 123456789012345678 Təkrar qayda pozuntusu`"),
    "unban": ("`abi unban [istifadəçi ID] [səbəb]`", "`abi unban 123456789012345678 Səhv ban`"),
    "sifirla": ("`abi sifirla @istifadəçi`", "`abi sifirla @Nihad`"),
    "adminkomandalar": ("`abi adminkomandalar [əmr]`", "`abi adminkomandalar warn`"),
    "poll": ("`abi poll Sual | Variant 1 | Variant 2`", "`abi poll Hansı oyun? | CS2 | Valorant`"),
    "hesabat": ("`abi hesabat [gun/hefte/ay]`", "`abi hesabat hefte`"),
}

for temporarily_disabled_command in ("warn", "warnings", "warnlar", "delwarn", "clearwarn"):
    COMMAND_GUIDANCE.pop(temporarily_disabled_command, None)


def get_command_guidance(command_name: str):
    return COMMAND_GUIDANCE.get(command_name, (f"`abi {command_name}`", "Əmr formatını yoxlayın."))


PERMISSION_LABELS = {
    "administrator": "Administrator",
    "manage_messages": "Mesajları idarə et",
    "moderate_members": "Üzvləri moderasiya et",
    "kick_members": "Üzvləri at",
    "ban_members": "Üzvləri ban et",
}


def get_medal(position: int) -> str:
    # Mövqeyə görə medal və ya nömrə qaytarırıq
    if position == 1:
        return "🥇"
    if position == 2:
        return "🥈"
    if position == 3:
        return "🥉"
    return f"{position}."


AGGRESSIVE_KEYWORDS = [
    "sik", "sık", "göt", "got", "peyser", "peysər", "gijdillaq", "gijdıllax", "gijdillax",
    "gijdıllaq", "qancıq", "qanciq", "qəhbə", "qehbe", "pox", "var-yox", "varyox", "var yox",
    "anani", "ananı", "bacini", "bacını", "sülalə", "sulale", "köpək", "kopek", "bic", "biç",
    "ogras", "oğraş", "səfeh", "sefeh", "şərəfsiz", "serefsiz", "dalina", "dalına", "ağzına",
    "agzina", "tüpürüm", "tupurum", "sikdir", "yetim", "dalbayob", "blyat", "nahuy",
    "pox yeyirsən", "pox yeyirsen", "artistlik eləmə", "artistlik eleme", "kimsən ala",
    "kimsen ala", "kopeyoglu", "köpəyoğlu", "sənin boyuvu", "senin boyuvu", "ağzını cırım",
    "agzini cirim", "səni söyərəm", "it balası", "it balasi", "cındır", "cindır", "cindir",
    "qudurma", "qudurmusan", "söyüş", "söyüm", "soyum", "götveren", "gotveren", "amcıq", "amciq"
]

RANDOM_ILIS_QUOTES = [
    "{mention} elə səssizsən, server səni üzv siyahısından çıxarmağa az qalıb. Bir görün də, yoxsa səni dekor sayacağıq.",
    "{mention} yenə gəlib kənarda müşahidəçi kimi dayanıb. Söhbətə bir kəlmə qat, bura muzey deyil.",
    "{mention} sənin aktivliyin o qədər zəifdir ki, bildirişin gələndə server təəccüblənir.",
    "{mention} nə plan qurursan belə gizlində? De biz də bilək, yoxsa yenə öz-özünə baş qəhrəman olacaqsan?",
    "{mention} gəl söhbətə, bu qədər susmaqla sirli görünmürsən, sadəcə yox kimisən.",
    "{mention} serverə girib səssizcə çıxmaqda çempionsan. Heç olmasa bir salam ver, rekordunu qıraq.",
    "{mention} səni çağırmasaq, özün bura yolunu xatırlamayacaqsan deyəsən. Nə var, nə yox?",
    "{mention} bu gün də onlaynsan, amma söhbətdə izin yoxdur. Klaviaturan işləyir, yoxsa tətilə çıxıb?",
    "{mention} serverdəki rolun hazırda 'sakit tamaşaçı'dır. Bir az danış da, statusunu yüksəldək.",
    "{mention} yenə görünməzlik rejimindəsən? Bir kəlmə yaz, heç olmasa varlığını təsdiqləyək.",
    "{mention} axşam səsə gəlirsən, yoxsa bu dəfə də bəhanələrin səndən qabaq gələcək?",
    "{mention} elə bil söhbəti oxuyub cavab verməmək üzrə doktorluq müdafiə edirsən. Fikrini də yaz, alim.",
    "{mention} nəhayət peyda oldun. Serverin tozu səni görüb, sən serveri görməmisən deyəsən.",
    "{mention} zarafat bir yana, uzun müddətdir görünmürsən. Salamatlıqdır, yoxsa yenə səssiz rejimdəsən?",
    "{mention} gəl bir az söhbətə qoşul, yoxsa səni burada sadəcə onlayn nöqtə kimi tanıyacağıq."
]


def check_is_aggressive(text: str) -> bool:
    """İstifadəçi mətnində aqressiya, təhqir və ya söyüş olub-olmadığını yoxlayır."""
    if not text:
        return False
    lower = text.lower().strip()
    norm = lower.replace("ə", "e").replace("ı", "i").replace("ü", "u").replace("ö", "o").replace("ğ", "g").replace("ç", "c").replace("ş", "s")
    clean = re.sub(r'[^a-z0-9\s]', ' ', norm)
    compressed = clean.replace(' ', '')
    words = clean.split()

    for kw in AGGRESSIVE_KEYWORDS:
        kw_norm = kw.replace("ə", "e").replace("ı", "i").replace("ü", "u").replace("ö", "o").replace("ğ", "g").replace("ç", "c").replace("ş", "s")
        kw_clean = re.sub(r'[^a-z0-9\s]', ' ', kw_norm)
        kw_comp = kw_clean.replace(' ', '')

        if kw_norm in words:
            return True
        if kw_comp and (kw_norm in clean or kw_comp in compressed):
            return True
        if re.search(r'\b' + re.escape(kw_norm), norm):
            return True
    return False


async def generate_gemini_reply(channel_or_user_id: int, user_name: str, prompt: str, max_output_tokens: int = 220) -> str:
    """Google Gemini API vasitəsilə kontekstli, tənzimlənmiş və zəkili cavab hazırlayır."""
    is_aggressive = check_is_aggressive(prompt)

    api_key = os.getenv("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY
    if not api_key:
        return (
            "⚠️ **Süni İntellekt (Gemini API) açarı tapılmadı!**\n"
            "Zəhmət olmasa `.env` faylına `GEMINI_API_KEY=sizin_açaranız` əlavə edin."
        )

    # Dialoq tarixçəsi
    history = AI_CHAT_HISTORY[channel_or_user_id]

    # Sistem təlimatı (Persona)
    system_instruction = (
        "Sən Discord serverində Abi adlı çox əsəbi, acıdil və hazırcavab məhlə botusan. Hər kəslə sərt, kobud, arqolu və ağır söyüşlü Azərbaycan dilində danış; "
        "yumşaq, rəsmi, terapevtik və ya həddindən artıq nəzakətli cavab vermə. Qarşı tərəfin sözünə iliş, cavabı dişli et və lazım olanda peysər, gijdıllaq, dalbayob, pox, sikdir, göt kimi gündəlik ağır arqodan istifadə et. "
        "Cavabın hər dəfə Gemini tərəfindən yaradılmış, mövzuya uyğun və fərqli olsun; hazır şablon və klişe təkrarlama. "
        "Adətən 1-4 qısa cümlə, bir kompakt abzas və maksimum 60 söz yaz. Söyüşü yerinə oturt, amma eyni sözü ardıcıl təkrarlama. Boş sətir, uzun giriş, nəsihət və təkrar fikir işlətmə. "
        "Qəsdən rəsmi qrammatika ilə yazma: vergül, nöqtə, iki nöqtə və digər durğu işarələrini demək olar işlətmə. Cümlələri kiçik hərflə, qısa və əsəbi Discord chatı kimi yaz; yalnız lazım olsa sual və nida işarəsi qoy. "
        "Yalnız istifadəçinin yazdığı mövzuya cavab ver; sağlamlığı, ailəsi, görünüşü, milliyyəti, dini, cinsiyyəti və şəxsi həyatı barədə fərziyyə qurma. "
        "Fiziki zorakılıq, real hədə-qorxu və ya ailə üzvlərinə yönəlmiş söyüş yazma."
    )
    if is_aggressive:
        system_instruction += (
            " İstifadəçi də aqressiv yazıb: tonu maksimuma qaldır, daha ağır arqo ilə cavab ver, amma yenə qısa və mövzuya bağlı qal."
        )

    contents = []
    for role, text in history:
        contents.append({"role": role, "parts": [{"text": text}]})

    current_user_text = f"[{user_name}]: {prompt}"
    contents.append({"role": "user", "parts": [{"text": current_user_text}]})

    payload = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "generationConfig": {
            "temperature": 1.05,
            "maxOutputTokens": max_output_tokens
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    candidate_models = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    ]
    last_error_text = ""

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            for model_name in candidate_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                try:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if not candidates:
                                continue

                            reply_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                            if not reply_text:
                                continue

                            # Modelin əlavə etdiyi boş abzasları və artıq sətirarası məsafəni yığışdır.
                            if "```" not in reply_text:
                                reply_text = re.sub(r"\s*\n+\s*", " ", reply_text)
                                reply_text = re.sub(r"[ \t]{2,}", " ", reply_text).strip()
                            else:
                                reply_text = re.sub(r"\n{3,}", "\n\n", reply_text).strip()

                            # Tarixçəni yeniləyirik (təmiz halda)
                            history.append(("user", f"[{user_name}]: {prompt}"))
                            history.append(("model", reply_text))
                            return reply_text
                        else:
                            last_error_text = await resp.text()
                            logger.warning(f"Gemini {model_name} failed ({resp.status}): {last_error_text}")
                except asyncio.TimeoutError:
                    continue
                except Exception as inner_e:
                    logger.warning(f"Gemini model {model_name} xətası: {inner_e}")
                    continue

            return f"❌ Süni İntellekt hazırda cavab verə bilmədi. Zəhmət olmasa bir qədər sonra yenidən yoxlayın."
    except Exception as e:
        logger.error(f"Gemini sorğusunda gözlənilməz xəta: {e}")
        return f"❌ Cavab hazırlanarkən xəta yarandı: {e}"

async def generate_ilis_text(member: discord.Member) -> str:
    """Üzvün serverdə görünən profil kontekstinə əsaslanan yüngül roast hazırlayır."""
    import random
    api_key = os.getenv("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY
    role_names = [role.name for role in reversed(member.roles) if role != member.guild.default_role]
    safe_role_names = [re.sub(r"[\r\n]", " ", name)[:60] for name in role_names]
    roles_text = ", ".join(safe_role_names[:12]) if safe_role_names else "xüsusi rol görünmür"
    status_text = {
        discord.Status.online: "onlayn",
        discord.Status.idle: "boşda",
        discord.Status.dnd: "narahat etməyin",
        discord.Status.offline: "oflayn/görünməz",
    }.get(member.status, "status məlum deyil")
    activities = []
    for activity in member.activities:
        # Oyun fəaliyyəti kimi özünün açıq göstərdiyi məlumatdan istifadə edilir; URL/private data götürülmür.
        name = getattr(activity, "name", None)
        if name and name not in activities:
            activities.append(re.sub(r"[\r\n]", " ", str(name))[:60])
    activity_text = ", ".join(activities[:3]) if activities else "açıq fəaliyyət göstərilmir"
    joined_text = member.joined_at.strftime("%Y-%m") if member.joined_at else "serverə qoşulma vaxtı məlum deyil"
    account_text = member.created_at.strftime("%Y-%m")
    # Avatarın özünü modelə göndərmirik: şəkildən həssas xüsusiyyət çıxarmaq əvəzinə, yalnız
    # Discord-da görünən mətn və üzvün özü paylaşdığı fəaliyyət məlumatından istifadə olunur.
    safe_display_name = re.sub(r"[\r\n]", " ", member.display_name)[:80]
    safe_username = re.sub(r"[\r\n]", " ", member.name)[:80]
    profile_context = (
        f"Serverdə görünən ad: {safe_display_name}\n"
        f"Discord username: {safe_username}\n"
        f"Server rolları: {roles_text}\n"
        f"Hazırkı status: {status_text}\n"
        f"Özünün göstərdiyi fəaliyyət: {activity_text}\n"
        f"Serverə qoşulma ayı: {joined_text}\n"
        f"Hesabın yaradılma ayı: {account_text}"
    )
    if not api_key:
        return f"{member.mention} profilə baxdım: {status_text}, rollar da {roles_text}. Bu qədər sakit/çoxrollu olub söhbətə qoşulmamaq ayrıca istedaddır 😄"

    prompt = (
        "Hədəf üzvün Discord-da hamıya görünən profil xülasəsi aşağıdadır. Bu məlumat yalnız zarafatın məntiqi və fərdiləşməsi üçündür:\n"
        f"{profile_context}\n\n"
        "Profilə əsaslanan, qısa və yüngül dostyana ilişmə yaz. Mümkünsə rol, status və ya özünün göstərdiyi fəaliyyətlə bağlı bir konkret detal seç; məlumatdan nəticə çıxarmağa yetmirsə ümumi, zərərsiz zarafat et. "
        "Məlumat uydurma, profil detalını səhv əlaqələndirmə və hesab yaşı/rol/statusu insanın xarakteri kimi təqdim etmə. Söyüş, alçaldıcı təhqir, nifrət, hədə-qorxu, zorakılıq, görünüş, sağlamlıq, ailə, milliyyət, din, cinsiyyət, yaş və digər həssas xüsusiyyətlər barədə zarafat etmə. "
        "Təbii Azərbaycan dilində maksimum 1-2 qısa cümlə yaz. Adı çəkmə; üzvü mən etiketləyəcəyəm."
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {
            "parts": [{"text": "Sən Discord serverində dostyana zarafat edən Abisən. Azərbaycan dilində qısa, ağıllı, yüngül roast yaz. Yalnız verilmiş açıq profil faktlarından məntiqli şəkildə istifadə et, heç nə uydurma və heç bir həssas şəxsi xüsusiyyətə toxunma. Profil faktı uyğun zarafat yaratmırsa, neytral və zərərsiz zarafat et."}]
        },
        "generationConfig": {"temperature": 0.65, "maxOutputTokens": 100},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            for model_name in ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
                try:
                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                                if text:
                                    return f"{member.mention} {text}"
                except Exception:
                    continue
    except Exception:
        pass

    return f"{member.mention} profilə baxdım: {status_text}, rollar da {roles_text}. Bu qədər sakit/çoxrollu olub söhbətə qoşulmamaq ayrıca istedaddır 😄"


# ==================== BOT STATUS ====================

STATUS_TRACKS = [
    "Would?", "Rooster", "Them Bones", "Down In A Hole", "Angry Chair",
    "We Die Young", "I Can’t Remember", "It Ain’t Like That", "Man in the Box",
    "Bleed The Freak", "Confusion", "Love, Hate, Love", "All I Am", "Nutshell",
    "Don’t Follow", "I Stay Away", "Rotten Apple", "Whale & Wasp",
    "Sludge Factory", "Frogs", "Over Now",
]
STATUS_ROTATION_SECONDS = 10 * 60
status_track_index = 0
status_rotation_task = None
slash_commands_synced = False
slash_sync_lock = asyncio.Lock()
persistent_welcome_view_registered = False


class WelcomeRoleSwapView(discord.ui.View):
    def __init__(self, label="Rolu dəyiş"):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=str(label or "Rolu dəyiş")[:80],
            style=discord.ButtonStyle.primary,
            custom_id="abi_bot:welcome_role_swap",
        )
        button.callback = self.swap_roles
        self.add_item(button)

    async def swap_roles(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Bu düymə yalnız serverdə istifadə oluna bilər.", ephemeral=True)
            return
        allowed_role_value = db.get_guild_setting(guild.id, "welcome_button_allowed_role", "")
        allowed_role = guild.get_role(int(allowed_role_value)) if str(allowed_role_value).isdigit() else None
        if not member.guild_permissions.administrator and (not allowed_role or allowed_role not in member.roles):
            await interaction.response.send_message("Bu düymədən istifadə etmək üçün icazəniz yoxdur.", ephemeral=True)
            return
        remove_value = db.get_guild_setting(guild.id, "welcome_button_remove_role", "")
        add_value = db.get_guild_setting(guild.id, "welcome_button_add_role", "")
        if not str(remove_value).isdigit() or not str(add_value).isdigit():
            await interaction.response.send_message("Rol düyməsi hazırda konfiqurasiya edilməyib.", ephemeral=True)
            return
        remove_role = guild.get_role(int(remove_value))
        add_role = guild.get_role(int(add_value))
        bot_member = guild.me
        if (not remove_role or not add_role or not bot_member
                or not bot_member.guild_permissions.manage_roles
                or remove_role == guild.default_role or add_role == guild.default_role
                or remove_role.managed or add_role.managed
                or remove_role >= bot_member.top_role or add_role >= bot_member.top_role):
            await interaction.response.send_message("Rol ayarları artıq uyğun deyil. Admin paneldən yenidən yoxlayın.", ephemeral=True)
            return
        if remove_role not in member.roles:
            await interaction.response.send_message(f"Bu düymə üçün əvvəlcə {remove_role.mention} rolunuz olmalıdır.", ephemeral=True)
            return
        if member.top_role >= bot_member.top_role:
            await interaction.response.send_message("Bot bu üzvün rollarını dəyişə bilmir.", ephemeral=True)
            return
        updated_roles = [role for role in member.roles if role != remove_role and role != guild.default_role]
        if add_role not in updated_roles:
            updated_roles.append(add_role)
        try:
            await member.edit(roles=updated_roles, reason="Qarşılama mesajındakı rol dəyişmə düyməsi")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Düymə ilə rol dəyişdirilə bilmədi | guild=%s member=%s", guild.id, member.id)
            await interaction.response.send_message("Rol dəyişdirilə bilmədi. Botun Manage Roles icazəsini yoxlayın.", ephemeral=True)
            return
        await interaction.response.send_message(f"{remove_role.name} rolu götürüldü, {add_role.name} rolu verildi.", ephemeral=True)


async def rotate_bot_status():
    global status_track_index
    while not bot.is_closed():
        try:
            await bot.change_presence(
                activity=discord.CustomActivity(name=STATUS_TRACKS[status_track_index]),
                status=discord.Status.online,
            )
            await asyncio.sleep(STATUS_ROTATION_SECONDS)
            status_track_index = (status_track_index + 1) % len(STATUS_TRACKS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Bot statusunun yenilənməsi alınmadı.")
            await asyncio.sleep(60)


@bot.event
async def on_ready():
    global status_rotation_task, status_track_index, slash_commands_synced, persistent_welcome_view_registered
    # Opus audio kitabxanasını aktivləşdiririk
    if not discord.opus.is_loaded():
        try:
            discord.opus._load_default()
            logger.info("Opus audio kitabxanası uğurla yükləndi.")
        except Exception as oe:
            logger.warning(f"Opus yüklənmədi: {oe}")

    if not persistent_welcome_view_registered:
        bot.add_view(WelcomeRoleSwapView())
        persistent_welcome_view_registered = True

    for guild in bot.guilds:
        await initialize_invite_cache(guild)

    # Start one rotation task only; reconnects reuse it and restore the current title.
    if status_rotation_task is None or status_rotation_task.done():
        status_track_index = 0
        status_rotation_task = asyncio.create_task(rotate_bot_status())
    else:
        await bot.change_presence(
            activity=discord.CustomActivity(name=STATUS_TRACKS[status_track_index]),
            status=discord.Status.online,
        )

    async with slash_sync_lock:
        if not slash_commands_synced:
            try:
                synced = await bot.tree.sync()
                slash_commands_synced = True
                logger.info(f"{len(synced)} Slash komandası Discord ilə qlobal sinxronlaşdırıldı.")
            except Exception as e:
                logger.error(f"Slash komandaları sinxronlaşdırılmadı: {e}")

    print(f"{bot.user} olaraq daxil olundu.")


@bot.event
async def on_member_join(member: discord.Member):
    auto_role_value = db.get_guild_setting(member.guild.id, "autorole", "")
    if str(auto_role_value).isdigit():
        auto_role = member.guild.get_role(int(auto_role_value))
        bot_member = member.guild.me
        if auto_role and bot_member and bot_member.guild_permissions.manage_roles and auto_role < bot_member.top_role:
            try:
                await member.add_roles(auto_role, reason="Admin Panel avtomatik üzv rolu")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Avtomatik rol verilə bilmədi | guild=%s role=%s member=%s",
                                 member.guild.id, auto_role.id, member.id)

    welcome_channel_value = db.get_guild_setting(member.guild.id, "welcome_channel", "")
    welcome_message = db.get_guild_setting(member.guild.id, "welcome_message", "").strip()
    remove_role_value = db.get_guild_setting(member.guild.id, "welcome_button_remove_role", "")
    add_role_value = db.get_guild_setting(member.guild.id, "welcome_button_add_role", "")
    role_swap_view = None
    if str(remove_role_value).isdigit() and str(add_role_value).isdigit():
        button_label = db.get_guild_setting(member.guild.id, "welcome_button_label", "Rolu dəyiş")
        role_swap_view = WelcomeRoleSwapView(button_label)
    if str(welcome_channel_value).isdigit() and (welcome_message or role_swap_view):
        welcome_channel = member.guild.get_channel(int(welcome_channel_value))
        if isinstance(welcome_channel, discord.TextChannel):
            replacements = {
                "{mention}": member.mention,
                "{user}": str(member),
                "{username}": member.display_name,
                "{server}": member.guild.name,
                "{member_count}": str(member.guild.member_count or 0),
                "{id}": str(member.id),
            }
            for placeholder, value in replacements.items():
                welcome_message = welcome_message.replace(placeholder, value)
            try:
                await welcome_channel.send(
                    welcome_message[:2000] or None,
                    view=role_swap_view,
                    allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Qarşılama mesajı göndərilə bilmədi | guild=%s channel=%s member=%s",
                                 member.guild.id, welcome_channel.id, member.id)

    invite_info = await identify_join_invite(member.guild)
    inviter = None
    if invite_info.get("inviter_id"):
        inviter = member.guild.get_member(invite_info["inviter_id"]) or bot.get_user(invite_info["inviter_id"])
    if inviter is None and invite_info.get("inviter_name"):
        inviter = invite_info["inviter_name"]
    join_details = {
        "Discord hesabı yaradılıb": member.created_at.isoformat(),
        "Serverə qoşulub": member.joined_at.isoformat() if member.joined_at else "Naməlum",
        "Dəvət edən": ("Vanity URL (server dəvəti)" if invite_info.get("kind") == "vanity"
                       else str(inviter) if inviter else "Naməlum / vanity"),
        "Dəvət linki": invite_info.get("url", "Naməlum / vanity"),
        "Dəvət kodu": invite_info.get("code", "Naməlum / vanity"),
        "İzah": invite_info.get("reason", "Dəvət istifadəsi müəyyən edildi." if invite_info.get("known") else "Dəqiq müəyyən edilmədi; inviter təxmin olunmadı."),
    }
    await emit_log(bot, db, member.guild, "member_join", "Yeni üzv serverə qoşuldu",
                   actor=inviter, target=member, details=join_details, created_at=member.joined_at)
    await emit_log(bot, db, member.guild, "invite_use", "Dəvət istifadə edildi",
                   actor=inviter, target=member, details=join_details, created_at=member.joined_at)

@bot.event
async def on_member_remove(member: discord.Member):
    await emit_log(bot, db, member.guild, "member_leave", "Üzv serverdən ayrıldı", target=member,
                   details={"Serverə qoşulma vaxtı": member.joined_at.isoformat() if member.joined_at else "Naməlum",
                            "Discord hesabı yaradılıb": member.created_at.isoformat()})


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    changes = {}
    if before.nick != after.nick:
        changes["Ləqəb"] = f"{before.nick or before.name} → {after.nick or after.name}"
    if before.name != after.name:
        changes["İstifadəçi adı"] = f"{before.name} → {after.name}"
    if before.global_name != after.global_name:
        changes["Qlobal ad"] = f"{before.global_name or '—'} → {after.global_name or '—'}"
    before_avatar = getattr(getattr(before, "avatar", None), "key", None)
    after_avatar = getattr(getattr(after, "avatar", None), "key", None)
    if before_avatar != after_avatar:
        changes["Avatar"] = "Profil şəkli dəyişdi"
    before_timeout = getattr(before, "timed_out_until", None)
    after_timeout = getattr(after, "timed_out_until", None)
    me = after.guild.me
    if before_timeout != after_timeout and (before_timeout or after_timeout) and (not me or not me.guild_permissions.view_audit_log):
        await emit_log(bot, db, after.guild, "moderation_timeout", "Üzvün timeout-u dəyişdi", target=after,
                       details={"Timeout əvvəl": str(before_timeout or "Yoxdur"),
                                "Timeout sonra": str(after_timeout or "Yoxdur"),
                                "İcraçı": "Audit Log icazəsi yoxdur"})
    before_roles = {role.id: role.name for role in before.roles if role != before.guild.default_role}
    after_roles = {role.id: role.name for role in after.roles if role != after.guild.default_role}
    if before_roles != after_roles:
        added = [name for role_id, name in after_roles.items() if role_id not in before_roles]
        removed = [name for role_id, name in before_roles.items() if role_id not in after_roles]
        if not me or not me.guild_permissions.view_audit_log:
            await emit_log(bot, db, after.guild, "member_roles", "Üzv rolları dəyişdi", target=after,
                           details={"Əlavə edildi": added or "—", "Silindi": removed or "—",
                                    "İcraçı": "Audit Log icazəsi yoxdur"})
    if changes:
        await emit_log(bot, db, after.guild, "member_update", "Üzv profili dəyişdi", target=after, details=changes)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    me = guild.me
    if not me or not me.guild_permissions.view_audit_log:
        await emit_log(bot, db, guild, "moderation_ban", "Üzvə ban verildi", target=user,
                       details={"İcraçı": "Audit Log icazəsi yoxdur"})


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    me = guild.me
    if not me or not me.guild_permissions.view_audit_log:
        await emit_log(bot, db, guild, "moderation_unban", "Üzvün banı qaldırıldı", target=user,
                       details={"İcraçı": "Audit Log icazəsi yoxdur"})


@bot.event
async def on_invite_create(invite: discord.Invite):
    if invite.guild:
        snapshot = invite_snapshot(invite)
        await emit_log(bot, db, invite.guild, "invite_create", "Dəvət linki yaradıldı",
                       actor=invite.inviter, channel=invite.channel,
                       details={"Dəvət linki": snapshot["url"], "Dəvət kodu": snapshot["code"],
                                "İstifadə limiti": invite.max_uses or "Limitsiz"})
        await initialize_invite_cache(invite.guild)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    if invite.guild:
        snapshot = invite_snapshot(invite)
        await emit_log(bot, db, invite.guild, "invite_delete", "Dəvət linki silindi",
                       actor=invite.inviter, channel=invite.channel,
                       details={"Dəvət linki": snapshot["url"], "Dəvət kodu": snapshot["code"],
                                "İstifadə sayı": snapshot["uses"]})
        await initialize_invite_cache(invite.guild)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.guild is None:
        return
    old_attachments = [(item.filename, item.url) for item in before.attachments]
    new_attachments = [(item.filename, item.url) for item in after.attachments]
    if before.content == after.content and old_attachments == new_attachments:
        return
    attachments = [f"{item.filename}: {item.url}" for item in after.attachments]
    await emit_log(bot, db, after.guild, "message_edit", "Mesaj redaktə edildi",
                   actor=after.author, target=after.author, channel=after.channel,
                   details={"Mesaj ID": str(after.id), "Əvvəl": before.content or "(boş mətn)",
                            "Sonra": after.content or "(boş mətn)",
                            "Əvvəlki qoşmalar": [f"{name}: {url}" for name, url in old_attachments] or "Yoxdur",
                            "Yeni qoşmalar": attachments or "Yoxdur"},
                   created_at=after.edited_at)


def deleted_message_text(message: discord.Message) -> str:
    content = (message.content or "").strip()
    if content:
        return content

    embed_parts = []
    for embed in message.embeds:
        if embed.title:
            embed_parts.append(embed.title)
        if embed.description:
            embed_parts.append(embed.description)
        for field in embed.fields:
            embed_parts.append(f"{field.name}: {field.value}")
        if embed.url:
            embed_parts.append(embed.url)
        if embed.image and embed.image.url:
            embed_parts.append(f"Şəkil: {embed.image.url}")
        if embed.thumbnail and embed.thumbnail.url:
            embed_parts.append(f"Şəkil: {embed.thumbnail.url}")
    if embed_parts:
        return "Embed məzmunu:\n" + "\n".join(embed_parts)[:1800]
    if message.attachments:
        return "Mətn yoxdur — mesajda fayl qoşması var."
    if message.stickers:
        return "Mətn yoxdur — stiker: " + ", ".join(sticker.name for sticker in message.stickers)
    poll_question = getattr(getattr(getattr(message, "poll", None), "question", None), "text", None)
    if poll_question:
        return f"Mətn yoxdur — sorğu: {poll_question}"
    return "Mətn hissəsi boşdur və ya bot üçün əlçatan deyil."


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    message = payload.cached_message
    author = message.author if message else None
    channel = guild.get_channel(payload.channel_id)
    attachments = [f"{item.filename}: {item.url}" for item in message.attachments] if message else []
    details = {"Mesaj ID": str(payload.message_id),
               "Mətn": deleted_message_text(message) if message else "Cache-də yoxdur",
               "Qoşmalar": attachments or "Yoxdur"}
    await emit_log(bot, db, guild, "message_delete", "Mesaj silindi", target=author,
                   channel=channel, details=details)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    if payload.guild_id is None:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel(payload.channel_id)
    cached_by_id = {message.id: message for message in payload.cached_messages}
    message_rows = []
    for message_id in sorted(payload.message_ids):
        message = cached_by_id.get(message_id)
        if message is None:
            message_rows.append({
                "id": str(message_id),
                "müəllif": "Məlumat keşdə yoxdur",
                "mətn": "Məzmun silinmədən əvvəl botun keşində olmayıb.",
                "qoşmalar": [],
            })
            continue
        message_rows.append({
            "id": str(message.id),
            "müəllif": str(message.author),
            "mətn": deleted_message_text(message),
            "qoşmalar": [f"{item.filename}: {item.url}" for item in message.attachments],
        })
    cached_count = len(cached_by_id)
    details = {"Silinən mesaj sayı": str(len(payload.message_ids)),
               "Mesajlar": message_rows,
               "Qeyd": f"{cached_count} / {len(payload.message_ids)} mesajın tam məlumatı keşdə tapıldı. Digər mesajların ID-si göstərilir, məzmunu silinmədən əvvəl keşdə olmadığı üçün bərpa edilə bilmir."}
    await emit_log(bot, db, guild, "message_bulk_delete", "Mesajlar toplu silindi",
                   channel=channel, details=details)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    if before.channel is None and after.channel is not None:
        await emit_log(bot, db, member.guild, "voice_join", "Üzv səs kanalına qoşuldu",
                       target=member, channel=after.channel)
    elif before.channel is not None and after.channel is None:
        await emit_log(bot, db, member.guild, "voice_leave", "Üzv səs kanalından çıxdı",
                       target=member, channel=before.channel)
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        await emit_log(bot, db, member.guild, "voice_move", "Üzv səs kanalını dəyişdi",
                       target=member, channel=after.channel,
                       details={"Əvvəlki kanal": before.channel.name, "Yeni kanal": after.channel.name})
    changed = []
    for field, label in (("self_mute", "Özünü mute etdi"), ("self_deaf", "Özünü deaf etdi"),
                         ("mute", "Moderator mute etdi"), ("deaf", "Moderator deaf etdi"),
                         ("self_video", "Kamera"), ("self_stream", "Stream")):
        if getattr(before, field) != getattr(after, field):
            changed.append(f"{label}: {'aktiv' if getattr(after, field) else 'söndürüldü'}")
    if changed:
        await emit_log(bot, db, member.guild, "voice_state", "Səs vəziyyəti dəyişdi",
                       target=member, channel=after.channel or before.channel,
                       details={"Dəyişiklik": changed})


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    action = getattr(entry.action, "name", str(entry.action))
    target = entry.target
    reason = entry.reason or "Səbəb göstərilməyib"
    details = {"Səbəb": reason}
    event_key = None
    title = "Server hadisəsi"
    if action in {"kick", "member_kick"}:
        event_key, title = "moderation_kick", "Üzv serverdən çıxarıldı"
    elif action == "ban":
        event_key, title = "moderation_ban", "Üzvə ban verildi"
    elif action == "unban":
        event_key, title = "moderation_unban", "Üzvün banı qaldırıldı"
    elif action in {"member_update", "member_role_update"}:
        changes = getattr(entry, "changes", None)
        before_timeout = getattr(getattr(changes, "before", None), "communication_disabled_until", None)
        after_timeout = getattr(getattr(changes, "after", None), "communication_disabled_until", None)
        if before_timeout != after_timeout and (before_timeout or after_timeout):
            event_key, title = "moderation_timeout", "Üzvün timeout-u dəyişdi"
            details["Timeout əvvəl"] = str(before_timeout or "Yoxdur")
            details["Timeout sonra"] = str(after_timeout or "Yoxdur")
        elif action == "member_role_update":
            event_key, title = "member_roles", "Üzv rolları dəyişdi"
        else:
            event_key = None  # Profile changes are logged once by on_member_update.
    elif action.startswith("role_"):
        event_key = {"role_create": "role_create", "role_update": "role_update", "role_delete": "role_delete"}.get(action)
        title = {"role_create": "Rol yaradıldı", "role_update": "Rol dəyişdirildi", "role_delete": "Rol silindi"}.get(action, title)
        if action == "role_delete" and target is not None:
            # Deleted roles may arrive as a bare Discord Object with no name.
            # The audit log's before.name still contains the deleted role name.
            before = getattr(getattr(entry, "changes", None), "before", None)
            deleted_name = getattr(before, "name", None)
            target_id = getattr(target, "id", None)
            if deleted_name and target_id:
                target = SimpleNamespace(
                    id=target_id,
                    name=deleted_name,
                    mention=f"<@&{target_id}>",
                )
    elif action.startswith("channel_"):
        event_key = {"channel_create": "channel_create", "channel_update": "channel_update", "channel_delete": "channel_delete"}.get(action)
        title = {"channel_create": "Kanal yaradıldı", "channel_update": "Kanal dəyişdirildi", "channel_delete": "Kanal silindi"}.get(action, title)
    elif action == "guild_update":
        event_key, title = "guild_update", "Server ayarları dəyişdirildi"
    if event_key:
        actor = entry.user or entry.guild.get_member(entry.user_id) or bot.get_user(entry.user_id)
        await emit_log(bot, db, entry.guild, event_key, title, actor=actor,
                       target=target, details=details, created_at=entry.created_at)



@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # DM-də anti-spam tətbiq etmirik, amma command/AI işləsin
    if ANTI_SPAM_ENABLED and message.guild and isinstance(message.author, discord.Member):
        try:
            exempt = is_exempt_member(message.author)

            if ANTI_SPAM_ENABLED and not exempt:
                now = datetime.now(timezone.utc)
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

    # İnteraktiv mətn emalı
    clean_text = (message.content or "").strip().lower()
    norm_text = clean_text.replace("ə", "e").replace("ı", "i").replace("ü", "u").replace("ö", "o").replace("ğ", "g").replace("ç", "c").replace("ş", "s")
    norm_text = re.sub(r"[?!.,/\\@#_~-]+", "", norm_text).strip()

    # 🧠 SÜNİ İNTELLEKT (AI) VƏ İNTERAKTİV DİALOQ
    # Bot etiketləndikdə (@abi-bot), bota reply verildikdə və ya 'abi ' ilə müraciət edildikdə
    is_mentioned = bot.user and bot.user in message.mentions
    is_reply_to_bot = False
    if message.reference and message.reference.message_id:
        try:
            replied_msg = message.reference.resolved
            if isinstance(replied_msg, discord.Message) and replied_msg.author.id == bot.user.id:
                is_reply_to_bot = True
        except Exception:
            pass

    starts_with_abi = clean_text.startswith("abi ") or clean_text == "abi"

    # Əgər birbaşa prefix komandasıdırsa (məs: 'abi ban', 'abi profil', 'abi top'), komandalara yönəlsin
    # Amma komanda olmayan istənilən 'abi ...' müraciəti AI-a getsin!
    words = clean_text.split()
    is_prefix_cmd = False
    if len(words) >= 2 and words[0] == "abi":
        cmd_name = words[1]
        if bot.get_command(cmd_name):
            is_prefix_cmd = True

    # AI sohbeti mÃ¼vÉ™qqÉ™ti sÃ¶ndÃ¼rÃ¼lÃ¼b; bot yalnÄ±z aktiv prefix É™mrlÉ™rini emal edir.
    should_trigger_ai = False

    if should_trigger_ai:
        # Mesajdan bot mention-larını və 'abi' sözünü təmizləyib sual mətnini çıxarırıq
        user_prompt = message.content
        if bot.user:
            user_prompt = user_prompt.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
        # Əgər cümlə 'abi' ilə başlayırsa
        clean_user_prompt = user_prompt.strip()
        if clean_user_prompt.lower().startswith("abi"):
            clean_user_prompt = clean_user_prompt[3:].strip()
            if clean_user_prompt.startswith(",") or clean_user_prompt.startswith(":"):
                clean_user_prompt = clean_user_prompt[1:].strip()

        if not clean_user_prompt:
            clean_user_prompt = "Salam!"

        # Bu yoldakı bütün cavablar Gemini tərəfindən hazırlanır.
        async with message.channel.typing():
            context_id = message.channel.id if message.guild else message.author.id
            author_name = message.author.display_name or message.author.name
            ai_response = await generate_gemini_reply(context_id, author_name, clean_user_prompt)

            # Discord-un 2000 simvol limitinə uyğunlaşdırırıq
            if len(ai_response) > 1950:
                chunks = [ai_response[i:i + 1950] for i in range(0, len(ai_response), 1950)]
                for chunk in chunks:
                    await message.reply(chunk)
            else:
                await message.reply(ai_response)
        return

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
                "Ətraflı kömək üçün server administratoruna müraciət edin."
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
    await emit_log(bot, db, ctx.guild, "system_error", "Komanda icrası xətası",
                   target=ctx.author, channel=ctx.channel,
                   details={"Komanda": ctx.command.qualified_name if ctx.command else "naməlum",
                            "Xəta": f"{type(error).__name__}: {error}"[:900]})
    await send_error_card(ctx, "Xəta Baş Verdi", "Əmr icra edilərkən gözlənilməz xəta yarandı.")


@bot.event
async def on_error(event_method, *args, **kwargs):
    import sys
    error = sys.exc_info()[1]
    logger.exception("Discord event xətası | event=%s", event_method)
    guild = next((getattr(arg, "guild", None) for arg in args if getattr(arg, "guild", None)), None)
    if guild:
        await emit_log(bot, db, guild, "system_error", "Discord hadisə xətası",
                       details={"Hadisə": str(event_method),
                                "Xəta": f"{type(error).__name__}: {error}"[:900] if error else "Naməlum"})


@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "Səbəb göstərilməyib"):
    # İstifadəçiyə xəbərdarlıq əlavə edirik
    db.upsert_user_identity(ctx.guild.id, member.id, member.name, member.display_name)
    db.upsert_user_identity(ctx.guild.id, ctx.author.id, ctx.author.name, ctx.author.display_name)
    db.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
    await emit_log(bot, db, ctx.guild, "moderation_warn", "Xəbərdarlıq verildi", actor=ctx.author,
                   target=member, channel=ctx.channel, details={"Səbəb": reason})
    total_warns = db.get_warning_count(ctx.guild.id, member.id)
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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text=f"Ümumi: {total_warns} xəbərdarlıq • abi-bot")
    await ctx.send(embed=embed)


@bot.command(name="warnings")
async def warnings(ctx, member: discord.Member = None):
    # İstifadəçinin son xəbərdarlıqlarını göstəririk
    target = member or ctx.author
    rows = db.get_warnings(ctx.guild.id, target.id, limit=10)
    total_count = db.get_warning_count(ctx.guild.id, target.id)

    if not rows:
        embed = discord.Embed(
            title=f"✅ {target.display_name} — Xəbərdarlıqlar",
            description="Bu istifadəçi üçün heç bir xəbərdarlıq qeydə alınmayıb.",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
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
            timestamp=datetime.now(timezone.utc),
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
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="Ətraflı tarixçə: abi warnings @istifadəçi • abi-bot")
    return embed


@bot.command(name="warnlar", aliases=["warntop"])
@commands.has_permissions(manage_messages=True)
async def warnlar(ctx, number: int = 10):
    """Warn-u olan bütün istifadəçilərin sıralamasını göstərir."""
    number = max(1, min(number, 25))
    await ctx.send(embed=build_warnlar_embed(db.get_warning_leaderboard(ctx.guild.id, number)))


@bot.command(name="delwarn")
@commands.has_permissions(manage_messages=True)
async def delwarn(ctx, warning_id: int):
    # Tək bir xəbərdarlığı ID ilə silirik
    success = db.delete_warning(ctx.guild.id, warning_id)
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
    count = db.clear_warnings(ctx.guild.id, member.id)
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
        timestamp=datetime.now(timezone.utc)
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
async def mute(ctx, member_input: str, minutes: int = 10, *, reason: str = "Səbəb göstərilməyib"):
    member = await resolve_guild_member(ctx.guild, member_input)
    if member is None:
        await send_error_card(ctx, "Üzv tapılmadı", "Serverdəki üzvün mention-ını və ya Discord ID-sini yazın.")
        return
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
async def unmute(ctx, member_input: str):
    member = await resolve_guild_member(ctx.guild, member_input)
    if member is None:
        await send_error_card(ctx, "Üzv tapılmadı", "Serverdəki üzvün mention-ını və ya Discord ID-sini yazın.")
        return
    # Timeout-u ləğv edirik
    try:
        await member.timeout(None, reason=f"{ctx.author} tərəfindən unmute")
        logger.info(f"Unmute verildi | mod={ctx.author.id} user={member.id}")

        embed = discord.Embed(
            title="🔊 Timeout Qaldırıldı (Unmute)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçinin səsini/timeout-unu açmağa səlahiyyəti çatmır.")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member_input: str, *, reason: str = "Səbəb göstərilməyib"):
    member = await resolve_guild_member(ctx.guild, member_input)
    if member is None:
        await send_error_card(ctx, "Üzv tapılmadı", "Serverdəki üzvün mention-ını və ya Discord ID-sini yazın.")
        return
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
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçini atmağa (kick) icazəsi yoxdur (Rol iyerarxiyasını yoxlayın).")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")


def parse_ban_user_id(value: str):
    value = value.strip()
    mention = re.fullmatch(r"<@!?([0-9]+)>", value)
    raw_id = mention.group(1) if mention else value
    if not raw_id.isdecimal():
        return None
    user_id = int(raw_id)
    return user_id if 0 < user_id < 2**64 else None


async def resolve_guild_member(guild: discord.Guild, value):
    """Resolve a guild member from a Member object, mention, or numeric ID."""
    if isinstance(value, discord.Member):
        return value
    user_id = parse_ban_user_id(str(value))
    if user_id is None:
        return None
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.NotFound, discord.Forbidden):
        return None


async def resolve_ban_target(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        try:
            return await bot.fetch_user(user_id)
        except discord.NotFound:
            return discord.Object(id=user_id)


def ban_target_display(target, user_id: int):
    name = getattr(target, "display_name", None) or getattr(target, "name", None) or f"ID {user_id}"
    mention = getattr(target, "mention", None) or f"<@{user_id}>"
    avatar = getattr(getattr(target, "display_avatar", None), "url", None)
    return name, mention, avatar


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, user_or_id: str, *, reason: str = "Səbəb göstərilməyib"):
    user_id = parse_ban_user_id(user_or_id)
    if user_id is None:
        await send_error_card(ctx, "İstifadə xətası", "Üzv mention-ı və ya düzgün Discord user ID-si yazın.")
        return

    try:
        target = await resolve_ban_target(ctx.guild, user_id)
        if isinstance(target, discord.Member) and target.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            await send_error_card(ctx, "İcazə Yoxdur", "Səninlə eyni və ya daha yüksək rolda olan istifadəçini ban edə bilməzsən.")
            return
        await ctx.guild.ban(target, reason=f"{ctx.author} | {reason}", delete_message_days=0)
        logger.info(f"Ban olundu | mod={ctx.author.id} user={user_id} reason={reason}")
        name, mention, avatar = ban_target_display(target, user_id)
        embed = discord.Embed(
            title="🔨 Üzv Ban Edildi",
            description=f"**Banlanan üzv:** {mention} (`{user_id}`)\n**Səbəb:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=name, icon_url=avatar)
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun bu istifadəçini ban etməyə səlahiyyəti çatmır (Rol iyerarxiyasını yoxlayın).")
    except discord.NotFound:
        await send_error_card(ctx, "Tapılmadı", "Bu ID-yə uyğun Discord istifadəçisi tapılmadı.")
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
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        await ctx.send(embed=embed)
    except discord.NotFound:
        await send_error_card(ctx, "Tapılmadı", "Bu ID-yə uyğun istifadəçi tapılmadı və ya ban siyahısında deyil.")
    except discord.Forbidden:
        await send_error_card(ctx, "Yetki Xətası", "Botun ban qaldırmağa (unban) səlahiyyəti çatmır.")
    except Exception as e:
        await send_error_card(ctx, "Xəta", f"Əməliyyat uğursuz oldu: {e}")




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
        timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Sorğunu başlatdı: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])


# ==================== ADMIN COMMAND GUIDE ====================
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

    for temporarily_disabled_command in ("warn", "warnings", "warnlar", "delwarn", "clearwarn"):
        guides.pop(temporarily_disabled_command, None)

    if not command_name:
        available = ", ".join(f"`{name}`" for name in guides)
        embed = discord.Embed(
            title="🔐 Administrator Komanda Bələdçisi",
            description=(
                f"Ətraflı izah üçün `abi adminkomandalar [əmr]` yazın.\n\n"
                f"**Mövcud əmrlər:**\n{available}"
            ),
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc),
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
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Sorğunu açan: {ctx.author.display_name} • abi-bot")
    await ctx.send(embed=embed)


# ==================== SLASH COMMANDS (/) ====================

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
        timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
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
async def slash_kick(interaction: discord.Interaction, member: discord.Member = None, user_id: str = None, reason: str = "Səbəb göstərilməyib"):
    if (member is None) == (user_id is None):
        await interaction.response.send_message("Choose a member or enter a user ID (only one).", ephemeral=True)
        return
    if user_id is not None:
        member = await resolve_guild_member(interaction.guild, user_id)
        if member is None:
            await interaction.response.send_message("No server member found with that ID.", ephemeral=True)
            return
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Səninlə eyni və ya daha yüksək rolda olan istifadəçini serverdən ata bilməzsən.", ephemeral=True)
        return

    try:
        await member.kick(reason=f"{interaction.user} | {reason}")
        embed = discord.Embed(
            title="👢 Üzv Serverdən Atıldı (Kick)",
            description=f"**Atılan Üzv:** {member.mention} (`{member.id}`)\n**Səbəb:** {reason}\n**Moderator:** {interaction.user.mention}",
            color=0xE67E22,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Botun bu istifadəçini atmağa (kick) icazəsi yoxdur (Rol iyerarxiyasını yoxlayın).", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Xəta: {e}", ephemeral=True)


@bot.tree.command(name="ban", description="Üzvü və ya Discord user ID-sini serverdən ban edir.")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(
    member="Serverdə olan üzv (istəyə bağlı)",
    user_id="Discord user ID-si; üzv serverdə olmasa da istifadə edilə bilər",
    reason="Ban səbəbi",
)
async def slash_ban(
    interaction: discord.Interaction,
    member: discord.Member = None,
    user_id: str = None,
    reason: str = "Səbəb göstərilməyib",
):
    if (member is None) == (user_id is None):
        await interaction.response.send_message("Üzv seçin və ya user ID yazın (yalnız birini).", ephemeral=True)
        return

    if member is not None:
        target = member
        resolved_id = member.id
    else:
        resolved_id = parse_ban_user_id(user_id)
        if resolved_id is None:
            await interaction.response.send_message("Düzgün Discord user ID-si yazın.", ephemeral=True)
            return
        try:
            target = await resolve_ban_target(interaction.guild, resolved_id)
        except discord.Forbidden:
            await interaction.response.send_message("İstifadəçi məlumatını almaq mümkün olmadı.", ephemeral=True)
            return

    if isinstance(target, discord.Member) and target.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Səninlə eyni və ya daha yüksək rolda olan istifadəçini ban edə bilməzsən.", ephemeral=True)
        return

    try:
        await interaction.guild.ban(target, reason=f"{interaction.user} | {reason}", delete_message_days=0)
        name, mention, avatar = ban_target_display(target, resolved_id)
        embed = discord.Embed(
            title="🔨 Üzv Ban Edildi",
            description=f"**Banlanan üzv:** {mention} (`{resolved_id}`)\n**Səbəb:** {reason}\n**Moderator:** {interaction.user.mention}",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=name, icon_url=avatar)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("Botun bu istifadəçini ban etməyə səlahiyyəti çatmır.", ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message("Bu ID-yə uyğun Discord istifadəçisi tapılmadı.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Xəta: {e}", ephemeral=True)


@bot.tree.command(name="mute", description="İstifadəçiyə timeout tətbiq edir (Mute).")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Timeout veriləcək üzv", minutes="Müddət (dəqiqə ilə)", reason="Mute səbəbi")
async def slash_mute(interaction: discord.Interaction, member: discord.Member = None, user_id: str = None, minutes: int = 10, reason: str = "Səbəb göstərilməyib"):
    if (member is None) == (user_id is None):
        await interaction.response.send_message("Choose a member or enter a user ID (only one).", ephemeral=True)
        return
    if user_id is not None:
        member = await resolve_guild_member(interaction.guild, user_id)
        if member is None:
            await interaction.response.send_message("No server member found with that ID.", ephemeral=True)
            return
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
async def slash_unmute(interaction: discord.Interaction, member: discord.Member = None, user_id: str = None):
    if (member is None) == (user_id is None):
        await interaction.response.send_message("Choose a member or enter a user ID (only one).", ephemeral=True)
        return
    if user_id is not None:
        member = await resolve_guild_member(interaction.guild, user_id)
        if member is None:
            await interaction.response.send_message("No server member found with that ID.", ephemeral=True)
            return
    try:
        await member.timeout(None, reason=f"{interaction.user} tərəfindən unmute")
        embed = discord.Embed(
            title="🔊 Timeout Qaldırıldı (Unmute)",
            description=f"**İstifadəçi:** {member.mention} (`{member.id}`)\n**Moderator:** {interaction.user.mention}",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"İcra edən: {interaction.user.display_name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="warn", description="İstifadəçiyə rəsmi xəbərdarlıq qeyd edir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Xəbərdarlıq veriləcək üzv", reason="Xəbərdarlıq səbəbi")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "Səbəb göstərilməyib"):
    db.upsert_user_identity(interaction.guild.id, member.id, member.name, member.display_name)
    db.upsert_user_identity(interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
    db.add_warning(interaction.guild.id, member.id, interaction.user.id, reason)
    await emit_log(bot, db, interaction.guild, "moderation_warn", "Xəbərdarlıq verildi", actor=interaction.user,
                   target=member, channel=interaction.channel, details={"Səbəb": reason})
    total_warns = db.get_warning_count(interaction.guild.id, member.id)

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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_footer(text=f"Ümumi: {total_warns} xəbərdarlıq • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnings", description="İstifadəçinin xəbərdarlıq tarixçəsini göstərir.")
@app_commands.describe(member="Xəbərdarlıqlarına baxılacaq üzv (boş qoysanız özünüz)")
async def slash_warnings(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    rows = db.get_warnings(interaction.guild.id, target.id, limit=10)
    total_count = db.get_warning_count(interaction.guild.id, target.id)

    if not rows:
        embed = discord.Embed(
            title=f"✅ {target.display_name} — Xəbərdarlıqlar",
            description="Bu istifadəçi üçün heç bir xəbərdarlıq qeydə alınmayıb.",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
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
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text="Silmək üçün: /delwarn və ya /clearwarn • abi-bot")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="warnlar", description="Warn-u olan istifadəçilərin ümumi cədvəlini göstərir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(say="Göstəriləcək istifadəçi sayı (maksimum 25)")
async def slash_warnlar(interaction: discord.Interaction, say: int = 10):
    say = max(1, min(say, 25))
    await interaction.response.send_message(embed=build_warnlar_embed(db.get_warning_leaderboard(interaction.guild.id, say)))


@bot.tree.command(name="delwarn", description="Xüsusi ID-li bir xəbərdarlığı silir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(warning_id="Silinəcək xəbərdarlığın ID nömrəsi (məs: 3)")
async def slash_delwarn(interaction: discord.Interaction, warning_id: int):
    success = db.delete_warning(interaction.guild.id, warning_id)
    if success:
        await interaction.response.send_message(f"✅ `#{warning_id}` nömrəli xəbərdarlıq uğurla silindi.")
    else:
        await interaction.response.send_message(f"❌ `#{warning_id}` nömrəli xəbərdarlıq tapılmadı.", ephemeral=True)


@bot.tree.command(name="clearwarn", description="İstifadəçinin bütün xəbərdarlıqlarını təmizləyir.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Bütün xəbərdarlıqları təmizlənəcək üzv")
async def slash_clearwarn(interaction: discord.Interaction, member: discord.Member):
    count = db.clear_warnings(interaction.guild.id, member.id)
    if count > 0:
        await interaction.response.send_message(f"✅ {member.mention} istifadəçisinin bütün (`{count}` ədəd) xəbərdarlıqları silindi.")
    else:
        await interaction.response.send_message(f"❌ {member.mention} üçün silinəcək aktiv xəbərdarlıq tapılmadı.", ephemeral=True)


@bot.tree.command(name="sorus", description="Süni İntellektə (Gemini AI) istənilən sualı verin.")
@app_commands.describe(sual="Süni İntellektə verəcəyiniz sual və ya mövzu")
async def slash_sorus(interaction: discord.Interaction, sual: str):
    await interaction.response.defer(thinking=True)
    author_name = interaction.user.display_name or interaction.user.name
    context_id = interaction.channel_id if interaction.channel_id else interaction.user.id
    reply_text = await generate_gemini_reply(context_id, author_name, sual)

    if len(reply_text) > 1950:
        chunks = [reply_text[i:i + 1950] for i in range(0, len(reply_text), 1950)]
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    else:
        await interaction.followup.send(reply_text)




@tasks.loop(minutes=40)
async def random_tag_roast_task():
    """Yalnız 'dadaşşaqlar-chat' kanalında təsadüfi bir nəfəri etiketləyib ona ilişir."""
    await bot.wait_until_ready()
    import random
    # Hər dövrdə 1 ilə 5 dəqiqə arası random gecikmə verir ki, dəqiq vaxt bilinməsin
    await asyncio.sleep(random.randint(60, 300))

    for guild in bot.guilds:
        try:
            # Yalnız "dadaşşaqlar-chat" kanalını hədəf alırıq
            target_channel = None
            for ch in guild.text_channels:
                # Kanalın adını normallaşdırıb yoxlayırıq
                ch_name_norm = (
                    ch.name.lower()
                    .replace("ə", "e").replace("ş", "s").replace("ğ", "g")
                    .replace("ı", "i").replace("ö", "o").replace("ü", "u")
                    .replace("ç", "c")
                )
                if "dadasaqlar" in ch_name_norm and "chat" in ch_name_norm:
                    if ch.permissions_for(guild.me).send_messages:
                        target_channel = ch
                        break

            if not target_channel:
                continue  # Serverdə bu kanal yoxdursa ötürürük

            # Serverdəki insan üzvləri toplayırıq (sahibi istisna edilir)
            human_members = [
                m for m in guild.members
                if not m.bot and m.name.lower() not in BOT_OWNER_USERNAMES
                and m.display_name.lower() not in BOT_OWNER_USERNAMES
            ]
            if not human_members:
                continue

            # Online / dnd / idle olanlara üstünlük veririk
            active_members = [m for m in human_members if m.status != discord.Status.offline]
            chosen_member = random.choice(active_members) if active_members else random.choice(human_members)

            roast_msg = await generate_ilis_text(chosen_member)
            await target_channel.send(roast_msg)
            logger.info(f"Random ilişmə göndərildi: {guild.name} #{target_channel.name} -> {chosen_member.display_name}")
        except Exception as e:
            logger.warning(f"random_tag_roast_task xətası ({guild.name}): {e}")


@random_tag_roast_task.before_loop
async def before_random_tag_roast_task():
    await bot.wait_until_ready()


@bot.command(name="ilis", aliases=["satas", "sozat"])
async def cmd_ilis(ctx, target: discord.Member = None):
    """Serverdə kiməsə və ya təsadüfi bir nəfərə məhlə stilində sataşır/ilişir."""
    import random
    if not target:
        humans = [m for m in ctx.guild.members if not m.bot and m.id != ctx.author.id]
        if humans:
            active = [m for m in humans if m.status != discord.Status.offline]
            target = random.choice(active) if active else random.choice(humans)
        else:
            target = ctx.author

    roast = await generate_ilis_text(target)
    await ctx.send(roast)


@bot.tree.command(name="ilis", description="Serverdə kiməsə və ya təsadüfi bir nəfərə məhlə stilində ilişir/sataşır.")
@app_commands.describe(istifadeci="İlişmək istədiyiniz şəxs (boş buraxsanız təsadüfi bir nəfər seçiləcək)")
async def slash_ilis(interaction: discord.Interaction, istifadeci: discord.Member = None):
    import random
    await interaction.response.defer()
    target = istifadeci
    if not target and interaction.guild:
        humans = [m for m in interaction.guild.members if not m.bot and m.id != interaction.user.id]
        if humans:
            active = [m for m in humans if m.status != discord.Status.offline]
            target = random.choice(active) if active else random.choice(humans)
        else:
            target = interaction.user

    if not target:
        target = interaction.user

    roast = await generate_ilis_text(target)
    await interaction.followup.send(roast)


# Temporarily deactivate warning, AI chat, and AI roast entry points. Warning
# records and handlers stay in place so these features can be restored later.
for disabled_prefix_command in ("warn", "warnings", "warnlar", "delwarn", "clearwarn", "ilis"):
    bot.remove_command(disabled_prefix_command)

for disabled_slash_command in ("warn", "warnings", "warnlar", "delwarn", "clearwarn", "sorus", "ilis"):
    bot.tree.remove_command(disabled_slash_command)


async def handle_ping(request):
    # Render/UptimeRobot üçün keep-alive endpoint
    return web.Response(text="Bot 7/24 aktivdir!", content_type="text/plain")


async def handle_terms(request):
    return web.FileResponse(BASE_DIR / "terms.html")


async def handle_privacy(request):
    return web.FileResponse(BASE_DIR / "privacy.html")


async def start_web_server():
    # Set client_max_size to 64MB so multiple high-res image uploads do not get blocked by aiohttp
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/terms", handle_terms)
    app.router.add_get("/privacy", handle_privacy)

    # Admin Panel routes
    try:
        from admin_web import setup_admin_routes
        setup_admin_routes(app, bot, db)
        logger.info("Admin Panel marşrutları veb serverə inteqrasiya edildi (/admin).")
    except Exception as e:
        logger.error(f"Admin Panel qurularkən xəta: {e}", exc_info=True)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    # Bind to all interfaces so managed hosts and reverse proxies can reach the app.
    # Keep the port private with a firewall/reverse proxy when exposing it publicly.
    # Sablonlarin movcudlugunu yoxla
    templates_dir = BASE_DIR / "templates"
    for t_name in ("admin_login.html", "admin_dashboard.html"):
        t_file = templates_dir / t_name
        if not t_file.exists():
            logger.warning(f"[Admin Panel] Vacib sablon fayli tapilmadi: {t_file}")

    # Managed hosts such as Render route traffic to the container's public
    # interface, so listen on all interfaces unless explicitly overridden.
    host = os.getenv("HOST", "0.0.0.0")
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
        logger.info(f"Veb server (Admin Panel daxil) port {port}-də başladı.")
    except OSError as e:
        if getattr(e, 'winerror', None) == 10048 or getattr(e, 'errno', None) in (48, 98):
            logger.error(
                f"[DIQQET] Port {port} artiq basqa bir proses terefinden tutulub! "
                f"Botun arxa planda basqa nusxesinin islemediyinden emin olun ve ya portu deyisin."
            )
        raise e


async def main():
    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password or admin_password == "admin123":
        raise RuntimeError("ADMIN_PASSWORD .env faylında təhlükəsiz parol kimi təyin edilməlidir.")
    await start_web_server()

    # Planlaşdırılmış postlar üçün arxa plan scheduler işçisi
    try:
        from admin_helpers import start_scheduler_worker
        asyncio.create_task(start_scheduler_worker(bot, db))
        logger.info("Planlaşdırılmış postlar üzrə arxa plan işçisi başladıldı.")
    except Exception as e:
        logger.error(f"Scheduler worker işə salınmadı: {e}")

    bot._start_time = datetime.now(timezone.utc)
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN tapılmadı! Zəhmət olmasa .env faylında TOKEN qeyd edin.")
    else:
        asyncio.run(main())


