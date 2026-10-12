import asyncio
import io
import ipaddress
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
import discord
from PIL import Image

logger = logging.getLogger("admin_panel")

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Login Rate Limiter: IP -> deque/list of timestamps
LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes lockout window

# Active admin sessions: session_token -> {"created_at": float, "csrf_token": str}
ACTIVE_SESSIONS: Dict[str, dict] = {}
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days


def clean_login_attempts(ip: str) -> List[float]:
    now = time.time()
    valid = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    LOGIN_ATTEMPTS[ip] = valid
    return valid


def is_rate_limited(ip: str) -> bool:
    attempts = clean_login_attempts(ip)
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def record_failed_login(ip: str):
    attempts = clean_login_attempts(ip)
    attempts.append(time.time())
    LOGIN_ATTEMPTS[ip] = attempts


def reset_login_attempts(ip: str):
    LOGIN_ATTEMPTS.pop(ip, None)


def get_client_ip(request: web.Request) -> str:
    # Check X-Forwarded-For if behind reverse proxy
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if peername:
        return peername[0]
    return "127.0.0.1"


# --- SSRF Protection ---
def is_safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False

        # Disallow loopback, private IP addresses
        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return False

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            # Domain name (e.g. i.imgur.com)
            pass

        return True
    except Exception:
        return False


async def safe_fetch_image_url(session: aiohttp.ClientSession, url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Safely fetches image bytes while preventing SSRF and checking content headers."""
    if not is_safe_url(url):
        return None, None, "Təhlükəsizlik xətası: Daxil edilən şəkil URL-i qadağandır və ya daxili şəbəkəyə aiddir."

    try:
        async with session.get(
            url,
            allow_redirects=True,
            max_redirects=3,
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AbiBotAdmin/1.0"}
        ) as resp:
            if resp.status != 200:
                return None, None, f"Şəkil yüklənmədi (HTTP Status: {resp.status})"

            # Final URL check after redirects
            if not is_safe_url(str(resp.url)):
                return None, None, "Təhlükəsizlik xətası: Yönləndirilən URL daxili şəbəkəyə aiddir."

            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type not in ALLOWED_MIME_TYPES:
                # Also check file extension in path
                path = urllib.parse.urlparse(str(resp.url)).path.lower()
                ext = Path(path).suffix
                if ext not in ALLOWED_IMAGE_EXTS:
                    return None, None, f"Dəstəklənməyən şəkil formatı ({content_type or 'naməlum'}). Yalnız PNG, JPG, WEBP və GIF dəstəklənir."

            data = await resp.read()
            # Determine filename
            url_path = urllib.parse.urlparse(str(resp.url)).path
            filename = Path(url_path).name or "image.png"
            if "." not in filename:
                ext = ALLOWED_MIME_TYPES.get(content_type, ".png")
                filename = f"image{ext}"

            return data, filename, None
    except asyncio.TimeoutError:
        return None, None, "Şəkil yüklənərkən vaxt bitdi (Timeout)."
    except Exception as e:
        return None, None, f"Şəkil yüklənərkən xəta: {e}"


# --- Upload Limits & Image Compression ---
def get_effective_max_upload_bytes(bot: discord.Client, channel: discord.TextChannel) -> int:
    """Calculates 90% effective maximum upload limit in bytes."""
    env_max_mb = os.getenv("MAX_UPLOAD_MB", "").strip()
    if env_max_mb:
        try:
            mb_val = float(env_max_mb)
            if mb_val > 0:
                raw_bytes = int(mb_val * 1024 * 1024)
                return int(raw_bytes * 0.90)
        except ValueError:
            pass

    # Read guild upload limit from bot/guild
    if channel and channel.guild:
        raw_bytes = getattr(channel.guild, "filesize_limit", 25 * 1024 * 1024)
    else:
        raw_bytes = 25 * 1024 * 1024

    return int(raw_bytes * 0.90)


def compress_image_if_needed(image_bytes: bytes, filename: str, max_allowed_bytes: int) -> bytes:
    """Compresses/resizes image with Pillow step by step if it exceeds size limit."""
    if len(image_bytes) <= max_allowed_bytes:
        return image_bytes

    ext = Path(filename).suffix.lower()
    # If GIF, compressing frames can be lossy and heavy, try basic resize if needed
    if ext == ".gif":
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            format_to_save = "JPEG" if ext in (".jpg", ".jpeg") else "PNG"
            if ext == ".webp":
                format_to_save = "WEBP"

            # Convert RGBA to RGB for JPEG
            if format_to_save == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            orig_w, orig_h = img.size
            current_bytes = image_bytes

            # Step 1: Quality reduction
            for quality in (85, 70, 55, 40):
                buf = io.BytesIO()
                if format_to_save in ("JPEG", "WEBP"):
                    img.save(buf, format=format_to_save, quality=quality, optimize=True)
                else:
                    img.save(buf, format=format_to_save, optimize=True)
                compressed = buf.getvalue()
                if len(compressed) <= max_allowed_bytes:
                    return compressed
                current_bytes = compressed

            # Step 2: Stepwise downscaling
            for scale in (0.8, 0.6, 0.45, 0.3):
                new_w = max(100, int(orig_w * scale))
                new_h = max(100, int(orig_h * scale))
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                if format_to_save in ("JPEG", "WEBP"):
                    resized.save(buf, format=format_to_save, quality=60, optimize=True)
                else:
                    resized.save(buf, format=format_to_save, optimize=True)
                compressed = buf.getvalue()
                if len(compressed) <= max_allowed_bytes:
                    return compressed
                current_bytes = compressed

            return current_bytes
    except Exception as e:
        logger.warning(f"Şəkil sıxılarkən xəta baş verdi: {e}")
        return image_bytes


class DiscordPostButtonView(discord.ui.View):
    def __init__(self, link_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Open", url=link_url, style=discord.ButtonStyle.link))


async def send_discord_post_payload(
    bot: discord.Client,
    channel_id: int,
    title: str,
    description: Optional[str],
    link_url: str,
    images: List[Tuple[str, bytes]]  # [(filename, bytes), ...]
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Posts modern embed(s) with button to an NSFW channel. Splits into multiple messages if needed."""
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            return False, None, f"Kanal tapılmadı və ya botun oxuma icazəsi yoxdur: {e}"

    if not isinstance(channel, discord.TextChannel):
        return False, None, "Seçilmiş kanal mətn kanalı deyil."

    # Strict NSFW verification
    if not channel.is_nsfw():
        return False, None, f"Təhlükəsizlik xətası: '#{channel.name}' kanalı yaş məhdudiyyətli (NSFW) deyil! Göndərilmə rədd edildi."

    # Check bot permissions
    perms = channel.permissions_for(channel.guild.me)
    if not perms.send_messages or not perms.embed_links or not perms.attach_files:
        return False, None, "Botun bu kanalda 'Mesaj göndər', 'Link yerləşdir' və ya 'Fayl əlavə et' icazələri yoxdur."

    max_allowed = get_effective_max_upload_bytes(bot, channel)

    # 1. Process and compress images
    processed_images: List[Tuple[str, bytes]] = []
    for idx, (fname, raw_bytes) in enumerate(images):
        clean_ext = Path(fname).suffix.lower()
        if clean_ext not in ALLOWED_IMAGE_EXTS:
            clean_ext = ".png"
        safe_fname = f"post_img_{idx + 1}{clean_ext}"
        compressed = compress_image_if_needed(raw_bytes, safe_fname, max_allowed)
        processed_images.append((safe_fname, compressed))

    # 2. Partition into message batches so each batch does not exceed max_allowed
    batches: List[List[Tuple[str, bytes]]] = []
    current_batch: List[Tuple[str, bytes]] = []
    current_batch_size = 0

    for item in processed_images:
        fname, bdata = item
        bsize = len(bdata)
        if bsize > max_allowed:
            return False, None, f"'{fname}' şəkli sıxıldıqdan sonra belə icazə verilən həddi aşır ({round(bsize / (1024*1024), 2)} MB > {round(max_allowed / (1024*1024), 2)} MB)."

        # If adding this image exceeds limit or exceeds 10 Discord attachments limit, start new batch
        if current_batch and (current_batch_size + bsize > max_allowed or len(current_batch) >= 10):
            batches.append(current_batch)
            current_batch = [item]
            current_batch_size = bsize
        else:
            current_batch.append(item)
            current_batch_size += bsize

    if current_batch or not processed_images:
        batches.append(current_batch)

    # 3. Send batches
    first_message_id = None
    first_jump_url = None

    try:
        for b_idx, batch in enumerate(batches):
            is_first = (b_idx == 0)
            embed = discord.Embed(
                title=title if is_first else f"{title} (Davamı #{b_idx + 1})",
                description=description if is_first else None,
                color=0x5865F2,
                url=link_url,
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="Abi Bot Admin Panel")

            files = []
            if batch:
                for fname, bdata in batch:
                    files.append(discord.File(io.BytesIO(bdata), filename=fname))
                # Set primary image of the embed to first file in batch
                embed.set_image(url=f"attachment://{batch[0][0]}")

            view = DiscordPostButtonView(link_url) if is_first else None

            # If batch has multiple images, create multi-embed gallery for Discord
            if len(batch) > 1:
                embeds = [embed]
                for extra_fname, _ in batch[1:]:
                    extra_embed = discord.Embed(url=link_url)
                    extra_embed.set_image(url=f"attachment://{extra_fname}")
                    embeds.append(extra_embed)
                msg = await channel.send(embeds=embeds, files=files, view=view)
            else:
                msg = await channel.send(embed=embed, files=files, view=view)

            if is_first:
                first_message_id = str(msg.id)
                first_jump_url = msg.jump_url

        return True, first_message_id, None
    except Exception as e:
        logger.error(f"Discord-a göndərilərkən xəta: {e}", exc_info=True)
        return False, None, f"Discord xətası: {e}"


# --- Scheduled Posts Background Worker ---
async def start_scheduler_worker(bot: discord.Client, db):
    """Background task running in the event loop checking for pending scheduled posts."""
    while True:
        try:
            await asyncio.sleep(15)
            if not bot.is_ready():
                continue

            due_posts = db.get_due_scheduled_posts()
            for post in due_posts:
                post_id = post["id"]
                logger.info(f"Planlaşdırılmış post göndərilir: ID {post_id} - '{post['title']}'")
                images_list: List[Tuple[str, bytes]] = []

                # Restore stored binary images or image URLs
                img_data_list = db.get_post_images(post_id)
                for item in img_data_list:
                    if item.get("image_bytes"):
                        images_list.append((item.get("filename", "image.png"), item["image_bytes"]))
                    elif item.get("image_url"):
                        async with aiohttp.ClientSession() as fetch_sess:
                            bdata, fname, err = await safe_fetch_image_url(fetch_sess, item["image_url"])
                            if bdata:
                                images_list.append((fname or "image.png", bdata))

                success, msg_id, err_msg = await send_discord_post_payload(
                    bot=bot,
                    channel_id=post["channel_id"],
                    title=post["title"],
                    description=post.get("description"),
                    link_url=post["link_url"],
                    images=images_list
                )

                if success:
                    db.update_scheduled_post_status(post_id, "sent", discord_message_id=msg_id)
                    logger.info(f"Planlaşdırılmış post uğurla göndərildi: ID {post_id}")
                else:
                    db.update_scheduled_post_status(post_id, "failed", error_message=err_msg)
                    logger.warning(f"Planlaşdırılmış post uğursuz oldu: ID {post_id} | {err_msg}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Scheduler worker xətası: {e}")
            await asyncio.sleep(10)

