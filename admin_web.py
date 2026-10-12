import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp
from aiohttp import web
import discord

from admin_helpers import (
    ACTIVE_SESSIONS,
    ALLOWED_IMAGE_EXTS,
    ALLOWED_MIME_TYPES,
    SESSION_MAX_AGE_SECONDS,
    get_client_ip,
    is_rate_limited,
    record_failed_login,
    reset_login_attempts,
    safe_fetch_image_url,
    send_discord_post_payload,
)

logger = logging.getLogger("admin_panel")

BASE_DIR = Path(__file__).resolve().parent
COOKIE_NAME = "abi_admin_session"


def get_admin_password_hash() -> str:
    """Gets SHA-256 hash of ADMIN_PASSWORD from .env or fallback."""
    raw_pwd = os.getenv("ADMIN_PASSWORD", "admin123").strip()
    return hashlib.sha256(raw_pwd.encode("utf-8")).hexdigest()


def verify_password(provided_password: str) -> bool:
    expected_hash = get_admin_password_hash()
    provided_hash = hashlib.sha256(provided_password.strip().encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected_hash, provided_hash)


def get_session_data(request: web.Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or token not in ACTIVE_SESSIONS:
        return None
    session = ACTIVE_SESSIONS[token]
    # Check expiry
    if (datetime.utcnow().timestamp() - session["created_at"]) > SESSION_MAX_AGE_SECONDS:
        ACTIVE_SESSIONS.pop(token, None)
        return None
    return session


def require_auth(handler):
    """Decorator to enforce admin authentication for endpoints."""
    async def middleware(request: web.Request):
        session = get_session_data(request)
        if not session:
            # If AJAX / JSON request
            if request.headers.get("Accept") == "application/json" or request.path.startswith("/admin/api/"):
                return web.json_response({"success": False, "error": "İcazə verilmədi. Zəhmət olmasa daxil olun."}, status=401)
            # Redirect to login page
            raise web.HTTPFound("/admin/login")
        return await handler(request)
    return middleware


def verify_csrf(request: web.Request, token: str) -> bool:
    session = get_session_data(request)
    if not session:
        return False
    return hmac.compare_digest(session.get("csrf_token", ""), token or "")


def setup_admin_routes(app: web.Application, bot: discord.Client, db):
    """Mounts all admin panel routes to the aiohttp application."""

    # 1. Login Page
    async def handle_login_page(request: web.Request):
        if get_session_data(request):
            raise web.HTTPFound("/admin")
        return web.FileResponse(BASE_DIR / "templates" / "admin_login.html")

    # 2. Login Action (POST)
    async def handle_login_action(request: web.Request):
        ip = get_client_ip(request)
        if is_rate_limited(ip):
            return web.json_response({
                "success": False,
                "error": "Çox sayda uğursuz cəhd! Zəhmət olmasa 5 dəqiqə gözləyib yenidən cəhd edin."
            }, status=429)

        try:
            data = await request.post()
            password = data.get("password", "")
        except Exception:
            return web.json_response({"success": False, "error": "Sorğu oxunmadı."}, status=400)

        if not password or not verify_password(password):
            record_failed_login(ip)
            return web.json_response({
                "success": False,
                "error": "Şifrə yalnışdır! Daxil edilən məlumatı yoxlayın."
            }, status=401)

        # Successful login
        reset_login_attempts(ip)
        session_token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        ACTIVE_SESSIONS[session_token] = {
            "created_at": datetime.utcnow().timestamp(),
            "csrf_token": csrf_token,
            "ip": ip
        }

        resp = web.json_response({"success": True, "redirect": "/admin"})
        resp.set_cookie(
            name=COOKIE_NAME,
            value=session_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="Lax",
            secure=False  # Set to True if strictly serving over HTTPS
        )
        return resp

    # 3. Logout Action (POST or GET)
    async def handle_logout(request: web.Request):
        token = request.cookies.get(COOKIE_NAME)
        if token in ACTIVE_SESSIONS:
            ACTIVE_SESSIONS.pop(token, None)
        resp = web.HTTPFound("/admin/login")
        resp.del_cookie(COOKIE_NAME)
        return resp

    # 4. Main Admin Dashboard Page
    @require_auth
    async def handle_admin_dashboard(request: web.Request):
        return web.FileResponse(BASE_DIR / "templates" / "admin_dashboard.html")

    # 5. API: Get Session & CSRF Token
    @require_auth
    async def handle_api_session_info(request: web.Request):
        session = get_session_data(request)
        return web.json_response({
            "success": True,
            "csrf_token": session.get("csrf_token") if session else ""
        })

    # 6. API: Fetch NSFW Channels
    @require_auth
    async def handle_api_channels(request: web.Request):
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Bot hələ Discord-a tam qoşulmayıb."}, status=503)

        nsfw_channels = []
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.is_nsfw():
                    perms = ch.permissions_for(guild.me)
                    if perms.send_messages and perms.embed_links:
                        nsfw_channels.append({
                            "id": str(ch.id),
                            "name": ch.name,
                            "guild_id": str(guild.id),
                            "guild_name": guild.name,
                            "display": f"{guild.name} ➔ #{ch.name}"
                        })

        return web.json_response({
            "success": True,
            "channels": nsfw_channels
        })

    # 7. API: Send or Schedule Post
    @require_auth
    async def handle_api_create_post(request: web.Request):
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Bot Discord-a qoşulmayıb."}, status=503)

        # Parse multipart form data
        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response({"success": False, "error": "Multipart form məlumatı oxunmadı."}, status=400)

        title = ""
        description = ""
        link_url = ""
        channel_id_str = ""
        csrf_token = ""
        image_urls_raw = ""
        scheduled_time = ""

        uploaded_files: List[Tuple[str, bytes]] = []

        while True:
            part = await reader.next()
            if part is None:
                break

            name = part.name
            if name == "csrf_token":
                csrf_token = (await part.text()).strip()
            elif name == "title":
                title = (await part.text()).strip()
            elif name == "description":
                description = (await part.text()).strip()
            elif name == "link_url":
                link_url = (await part.text()).strip()
            elif name == "channel_id":
                channel_id_str = (await part.text()).strip()
            elif name == "image_urls":
                image_urls_raw = (await part.text()).strip()
            elif name == "scheduled_time":
                scheduled_time = (await part.text()).strip()
            elif name in ("images", "files"):
                filename = part.filename or "image.png"
                file_bytes = await part.read(decode=False)
                if file_bytes:
                    uploaded_files.append((filename, file_bytes))

        # CSRF check
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF təhlükəsizlik xətası (Invalid token)."}, status=403)

        # Validations
        if not title:
            return web.json_response({"success": False, "error": "Başlıq (Title) mütləq daxil edilməlidir."}, status=400)

        if not link_url:
            return web.json_response({"success": False, "error": "Link URL mütləq daxil edilməlidir."}, status=400)

        # Link URL format validation
        parsed_link = urllib.parse.urlparse(link_url)
        if parsed_link.scheme not in ("http", "https") or not parsed_link.netloc:
            return web.json_response({"success": False, "error": "Keçərsiz Link URL formatı. 'http://' və ya 'https://' ilə başlamalıdır."}, status=400)

        if not channel_id_str:
            return web.json_response({"success": False, "error": "Kanal seçilməlidir."}, status=400)

        try:
            channel_id = int(channel_id_str)
        except ValueError:
            return web.json_response({"success": False, "error": "Keçərsiz kanal ID-si."}, status=400)

        # Find target channel and check NSFW
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                return web.json_response({"success": False, "error": "Kanal Discord-da tapılmadı."}, status=404)

        if not isinstance(channel, discord.TextChannel) or not channel.is_nsfw():
            return web.json_response({
                "success": False,
                "error": "Təhlükəsizlik xətası: Seçilmiş kanal yaş məhdudiyyətli (NSFW) deyil! Göndərilmə qadağandır."
            }, status=400)

        # Collect images (uploaded + image URLs)
        all_images: List[Tuple[str, bytes]] = []

        # 1. Process uploaded files
        for fname, fbytes in uploaded_files:
            ext = Path(fname).suffix.lower()
            if ext not in ALLOWED_IMAGE_EXTS:
                return web.json_response({"success": False, "error": f"'{fname}' dəstəklənməyən şəkil formatıdır. Yalnız PNG, JPG, WEBP, GIF icazəlidir."}, status=400)
            all_images.append((fname, fbytes))

        # 2. Process image URLs if provided (safely with SSRF protection)
        if image_urls_raw:
            url_list = [u.strip() for u in image_urls_raw.replace("\r", "").split("\n") if u.strip()]
            async with aiohttp.ClientSession() as fetch_session:
                for img_url in url_list:
                    img_bytes, fname, err = await safe_fetch_image_url(fetch_session, img_url)
                    if err:
                        return web.json_response({"success": False, "error": f"URL xətası ({img_url}): {err}"}, status=400)
                    all_images.append((fname or "image.png", img_bytes))

        # Multiple images validation (Max 10)
        if len(all_images) > 10:
            return web.json_response({"success": False, "error": f"Maksimum 10 ədəd şəkil seçilə bilər. Hal-hazırda: {len(all_images)}"}, status=400)

        # Determine if scheduling or immediate send
        is_scheduled = False
        sched_iso = None
        if scheduled_time:
            try:
                # Expecting 'YYYY-MM-DDTHH:MM' or similar from datetime-local input
                sched_dt = datetime.fromisoformat(scheduled_time.replace("Z", ""))
                sched_iso = sched_dt.strftime("%Y-%m-%d %H:%M:%S")
                # If scheduled in future
                if sched_dt > datetime.utcnow():
                    is_scheduled = True
            except Exception as e:
                return web.json_response({"success": False, "error": f"Keçərsiz planlaşdırma vaxtı: {e}"}, status=400)

        # Format image objects for SQLite
        db_images = []
        for fname, bdata in all_images:
            db_images.append({
                "filename": fname,
                "image_bytes": bdata,
                "image_url": None
            })

        if is_scheduled:
            post_id = db.create_admin_post(
                title=title,
                description=description,
                link_url=link_url,
                channel_id=channel_id,
                channel_name=f"#{channel.name}",
                status="scheduled",
                scheduled_time=sched_iso,
                images=db_images
            )
            return web.json_response({
                "success": True,
                "post_id": post_id,
                "message": f"Post uğurla planlaşdırıldı! ({sched_iso} tarixində göndəriləcək)",
                "scheduled": True
            })

        # Immediate sending
        success, msg_id, err_msg = await send_discord_post_payload(
            bot=bot,
            channel_id=channel_id,
            title=title,
            description=description,
            link_url=link_url,
            images=all_images
        )

        status_val = "sent" if success else "failed"
        post_id = db.create_admin_post(
            title=title,
            description=description,
            link_url=link_url,
            channel_id=channel_id,
            channel_name=f"#{channel.name}",
            status=status_val,
            discord_message_id=msg_id,
            error_message=err_msg,
            images=db_images
        )

        if not success:
            return web.json_response({"success": False, "error": err_msg or "Discord-a göndərilərkən xəta yarandı."}, status=500)

        return web.json_response({
            "success": True,
            "post_id": post_id,
            "message": f"Post uğurla #{channel.name} kanalına göndərildi!",
            "discord_message_id": msg_id
        })

    # 8. API: Post History List
    @require_auth
    async def handle_api_history(request: web.Request):
        posts = db.get_admin_posts(limit=50)
        # Format response
        result = []
        for p in posts:
            img_count = len(db.get_post_images(p["id"]))
            result.append({
                "id": p["id"],
                "title": p["title"],
                "description": p.get("description") or "",
                "link_url": p["link_url"],
                "channel_id": str(p["channel_id"]),
                "channel_name": p.get("channel_name") or f"ID: {p['channel_id']}",
                "status": p["status"],
                "discord_message_id": p.get("discord_message_id"),
                "scheduled_time": p.get("scheduled_time"),
                "created_at": p["created_at"],
                "error_message": p.get("error_message"),
                "image_count": img_count
            })
        return web.json_response({"success": True, "posts": result})

    # 9. API: Delete Post (and Discord message)
    @require_auth
    async def handle_api_delete_post(request: web.Request):
        try:
            data = await request.json()
            post_id = int(data.get("post_id", 0))
            csrf_token = data.get("csrf_token", "")
        except Exception:
            return web.json_response({"success": False, "error": "Keçərsiz JSON sorğusu."}, status=400)

        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF təhlükəsizlik xətası."}, status=403)

        post = db.get_admin_post(post_id)
        if not post:
            return web.json_response({"success": False, "error": "Post tapılmadı."}, status=404)

        # If discord_message_id exists, attempt to delete from Discord
        discord_msg_id = post.get("discord_message_id")
        channel_id = post.get("channel_id")
        if discord_msg_id and channel_id and bot.is_ready():
            try:
                ch = bot.get_channel(int(channel_id))
                if ch:
                    msg = await ch.fetch_message(int(discord_msg_id))
                    if msg:
                        await msg.delete()
            except Exception as e:
                logger.warning(f"Discord mesajı silinərkən xəta (ola bilsin artıq silinib): {e}")

        db.delete_admin_post(post_id)
        return web.json_response({"success": True, "message": f"Post #{post_id} bazadan və Discord-dan silindi."})

    # 10. API: Resend Post
    @require_auth
    async def handle_api_resend_post(request: web.Request):
        try:
            data = await request.json()
            post_id = int(data.get("post_id", 0))
            csrf_token = data.get("csrf_token", "")
        except Exception:
            return web.json_response({"success": False, "error": "Keçərsiz JSON sorğusu."}, status=400)

        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF təhlükəsizlik xətası."}, status=403)

        post = db.get_admin_post(post_id)
        if not post:
            return web.json_response({"success": False, "error": "Post tapılmadı."}, status=404)

        images_list: List[Tuple[str, bytes]] = []
        for img in db.get_post_images(post_id):
            if img.get("image_bytes"):
                images_list.append((img.get("filename", "image.png"), img["image_bytes"]))
            elif img.get("image_url"):
                async with aiohttp.ClientSession() as fetch_sess:
                    bdata, fname, err = await safe_fetch_image_url(fetch_sess, img["image_url"])
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
            return web.json_response({"success": True, "message": "Post uğurla yenidən göndərildi!", "discord_message_id": msg_id})
        else:
            db.update_scheduled_post_status(post_id, "failed", error_message=err_msg)
            return web.json_response({"success": False, "error": err_msg or "Post yenidən göndərilərkən xəta baş verdi."}, status=500)

    # Register Routes
    app.router.add_get("/admin/login", handle_login_page)
    app.router.add_post("/admin/login", handle_login_action)
    app.router.add_get("/admin/logout", handle_logout)
    app.router.add_post("/admin/logout", handle_logout)

    app.router.add_get("/admin", handle_admin_dashboard)
    app.router.add_get("/admin/api/session", handle_api_session_info)
    app.router.add_get("/admin/api/channels", handle_api_channels)
    app.router.add_post("/admin/api/posts", handle_api_create_post)
    app.router.add_get("/admin/api/history", handle_api_history)
    app.router.add_post("/admin/api/delete", handle_api_delete_post)
    app.router.add_post("/admin/api/resend", handle_api_resend_post)

