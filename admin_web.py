import hashlib
import asyncio
import hmac
from html.parser import HTMLParser
import io
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp
from aiohttp import web
import discord
from logging_system import LOG_EVENTS, LOG_EVENT_MAP

from admin_helpers import (
    ACTIVE_SESSIONS,
    ALLOWED_IMAGE_EXTS,
    ALLOWED_MIME_TYPES,
    SESSION_MAX_AGE_SECONDS,
    generate_image_description,
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
CHANNEL_PERMISSION_FLAGS = ("view_channel", "send_messages", "connect", "speak")
ROLE_PERMISSION_FLAGS = tuple(flag for flag in discord.Permissions.VALID_FLAGS)

BOT_GUIDES = [
    {"id": "carl", "name": "Carl-bot", "dashboard": "https://carl.gg/", "docs": "https://docs.carl.gg/", "source": "https://docs.carl.gg/", "format": "html"},
    {"id": "mee6", "name": "MEE6", "dashboard": "https://mee6.xyz/dashboard", "docs": "https://mee6bot.freshdesk.com/support/home", "source": "https://mee6bot.freshdesk.com/support/home", "format": "html"},
    {"id": "dyno", "name": "Dyno", "dashboard": "https://dyno.gg/", "docs": "https://docs.dyno.gg/en/commands", "source": "https://docs.dyno.gg/en/commands", "format": "html"},
    {"id": "tickettool", "name": "Ticket Tool", "dashboard": "https://dash.ticket-tool.app/", "docs": "https://ticket-tool.app/docs/commands", "source": "https://ticket-tool.app/docs/commands", "format": "html"},
    {"id": "giveawaybot", "name": "Giveaway Bot", "dashboard": "https://giveawaybot.party/", "docs": "https://github.com/jagrosh/GiveawayBot", "source": "https://raw.githubusercontent.com/jagrosh/GiveawayBot/master/README.md", "format": "markdown"},
    {"id": "statbot", "name": "Statbot", "dashboard": "https://statbot.net/me", "docs": "https://docs.statbot.net/docs/usage/commands/overview/", "source": "https://docs.statbot.net/docs/usage/commands/overview/", "format": "html"},
    {"id": "owo", "name": "OwO", "dashboard": "https://owobot.com/", "docs": "https://owobot.com/help", "source": "https://owobot.com/help", "format": "html"},
    {"id": "mudae", "name": "Mudae", "dashboard": "https://mudae.net/", "docs": "https://mudae.net/", "source": "https://mudae.net/", "format": "html"},
]

_BOT_COMMAND_CACHE = {}


class _CommandTableParser(HTMLParser):
    """Extract command rows from official documentation tables without executing scripts."""
    def __init__(self):
        super().__init__()
        self.rows = []
        self._in_table = False
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if self._in_table and tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif self._in_table and tag == "tr" and self._row:
            if len(self._row) >= 2:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self._in_table = False


def _parse_official_commands(content: str, fmt: str):
    commands = []
    if fmt == "markdown":
        in_commands = False
        for line in content.splitlines():
            if re.match(r"^\s{0,3}#{1,4}\s+Commands\s*$", line, re.I):
                in_commands = True
                continue
            if in_commands and re.match(r"^\s{0,3}#{1,4}\s+", line):
                break
            if in_commands:
                match = re.match(r"^\s*[-*]\s+(`?/?[\w$!<>.-]+[^`]*?)`?\s+-\s+(.+)$", line)
                if match:
                    commands.append({"name": match.group(1).strip(), "description": match.group(2).strip(), "usage": ""})
    else:
        parser = _CommandTableParser()
        parser.feed(content)
        for row in parser.rows:
            name = row[0].strip("` ")
            if len(name) > 60 or name.casefold() in {"command", "commands", "name", "category"}:
                continue
            if not re.match(r"^(?:[/!?$]|[a-z][\w.-]{0,35}$)", name, re.I):
                continue
            description = row[1] if len(row) > 1 else ""
            usage = row[2] if len(row) > 2 else ""
            if description and description.casefold() in {"description", "what it does"}:
                continue
            commands.append({"name": name, "description": description, "usage": usage})
    unique = {}
    for command in commands:
        key = command["name"].casefold()
        if key and key not in unique:
            unique[key] = command
    return list(unique.values())[:500]


def _manageable_role(guild, role):
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles and role and role != guild.default_role
                and not role.managed and role < me.top_role)


def _permission_overwrite_json(channel, guild):
    result = {}
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            result[str(target.id)] = {flag: getattr(overwrite, flag) for flag in CHANNEL_PERMISSION_FLAGS}
    result.setdefault(str(guild.default_role.id), {flag: None for flag in CHANNEL_PERMISSION_FLAGS})
    return result


def get_admin_password_hash() -> str:
    """Derive a slow password verifier for the configured admin password."""
    raw_pwd = os.getenv("ADMIN_PASSWORD", "").strip()
    if not raw_pwd or raw_pwd == "admin123":
        raise RuntimeError("ADMIN_PASSWORD təhlükəsiz parol kimi təyin edilməlidir.")
    return hashlib.pbkdf2_hmac(
        "sha256", raw_pwd.encode("utf-8"), b"abi-admin-panel-password-v1", 600_000
    ).hex()


def verify_password(provided_password: str) -> bool:
    expected_hash = get_admin_password_hash()
    provided_hash = hashlib.pbkdf2_hmac(
        "sha256", provided_password.strip().encode("utf-8"), b"abi-admin-panel-password-v1", 600_000
    ).hex()
    return hmac.compare_digest(expected_hash, provided_hash)


def get_session_data(request: web.Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token or token not in ACTIVE_SESSIONS:
        return None
    session = ACTIVE_SESSIONS[token]
    # Check expiry
    if (datetime.now(timezone.utc).timestamp() - session["created_at"]) > SESSION_MAX_AGE_SECONDS:
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
            "created_at": datetime.now(timezone.utc).timestamp(),
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
            secure=os.getenv("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
        )
        return resp

    # 3. Logout Action (CSRF-protected POST only)
    async def handle_logout(request: web.Request):
        try:
            data = await request.json()
            csrf_token = str(data.get("csrf_token", ""))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "Sorğu məlumatı düzgün deyil."}, status=400)
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        token = request.cookies.get(COOKIE_NAME)
        if token in ACTIVE_SESSIONS:
            ACTIVE_SESSIONS.pop(token, None)
        resp = web.json_response({"success": True, "redirect": "/admin/login"})
        resp.del_cookie(COOKIE_NAME)
        return resp

    # 4. Main Admin Dashboard Page
    @require_auth
    async def handle_admin_dashboard(request: web.Request):
        response = web.FileResponse(BASE_DIR / "templates" / "admin_dashboard.html")
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    # 5.1 API: System Overview & Stats
    @require_auth
    async def handle_api_system_stats(request: web.Request):
        import psutil
        guilds = list(bot.guilds)
        total_members = sum(g.member_count or len(g.members) for g in guilds)
        total_channels = sum(len(g.channels) for g in guilds)
        voice_members = 0
        for g in guilds:
            for vc in g.voice_channels:
                voice_members += len(vc.members)
            for sc in getattr(g, "stage_channels", []):
                voice_members += len(sc.members)

        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        mem_mb = round(mem_info.rss / (1024 * 1024), 1)
        cpu_usage = process.cpu_percent(interval=None)

        uptime_seconds = int((datetime.now(timezone.utc) - getattr(bot, "_start_time", datetime.now(timezone.utc))).total_seconds()) if hasattr(bot, "_start_time") else 0

        try:
            scheduled_posts = [p for p in db.get_admin_posts(limit=100) if p.get("status") == "scheduled"]
            posts_history = db.get_admin_posts(limit=5)
        except Exception:
            scheduled_posts = []
            posts_history = []

        return web.json_response({
            "success": True,
            "bot": {
                "name": bot.user.name if bot.user else "Abi Bot",
                "id": str(bot.user.id) if bot.user else "",
                "avatar": bot.user.display_avatar.url if bot.user else "",
                "latency_ms": round(bot.latency * 1000, 1) if bot.latency else 0,
                "guilds_count": len(guilds),
                "total_members": total_members,
                "total_channels": total_channels,
                "voice_members": voice_members,
                "memory_mb": mem_mb,
                "cpu_percent": cpu_usage,
                "uptime_seconds": uptime_seconds,
                "scheduled_count": len(scheduled_posts),
            },
            "guilds": [
                {
                    "id": str(g.id),
                    "name": g.name,
                    "icon": g.icon.url if g.icon else "",
                    "members": g.member_count or len(g.members),
                    "channels": len(g.channels),
                    "roles": len(g.roles),
                } for g in guilds
            ],
            "recent_posts": posts_history
        })

    # 5. API: Get Session & CSRF Token
    @require_auth
    async def handle_api_session_info(request: web.Request):
        session = get_session_data(request)
        return web.json_response({
            "success": True,
            "csrf_token": session.get("csrf_token") if session else ""
        })

    def moderation_guild(guild_id: int):
        return bot.get_guild(guild_id)

    @require_auth
    async def handle_api_moderation_guilds(request: web.Request):
        guilds = list(bot.guilds)
        return web.json_response({
            "success": True,
            "guilds": [{"id": str(guild.id), "name": guild.name} for guild in guilds],
        })

    @require_auth
    async def handle_api_command_catalog(request: web.Request):
        prefix_purposes = {
            "sil": ("Kanaldakı son mesajları toplu silir.", "Mesajları idarə et"),
            "mute": ("Üzvə göstərilən müddətə Discord timeout-u verir.", "Üzvləri moderasiya et"),
            "unmute": ("Üzvün aktiv timeout-unu qaldırır.", "Üzvləri moderasiya et"),
            "kick": ("Üzvü serverdən çıxarır.", "Üzvləri at"),
            "ban": ("Üzvü serverdən ban edir.", "Üzvləri ban et"),
            "unban": ("Discord ID-si ilə üzvün banını açır.", "Üzvləri ban et"),
            "userinfo": ("Üzvün profil və server məlumatlarını göstərir.", "Hər kəs"),
            "serverinfo": ("Serverin əsas məlumatlarını göstərir.", "Hər kəs"),
            "avatar": ("Üzvün avatarını böyük ölçüdə göstərir.", "Hər kəs"),
            "poll": ("Mesajda reaksiyalarla səsvermə yaradır.", "Mesajları idarə et"),
            "adminkomandalar": ("Administrator əmrlərinin izahını göstərir.", "Administrator"),
        }
        slash_permissions = {
            "sil": "Mesajları idarə et",
            "kick": "Üzvləri at",
            "ban": "Üzvləri ban et",
            "mute": "Üzvləri moderasiya et",
            "unmute": "Üzvləri moderasiya et",
        }
        commands_list = []
        for command in sorted(bot.commands, key=lambda item: item.name.casefold()):
            if command.hidden:
                continue
            purpose, permission = prefix_purposes.get(
                command.name, (command.help or "Əmr haqqında izah əlavə edilməyib.", "Əmr icazələrinə baxın"))
            usage = f"abi {command.qualified_name}"
            if command.signature:
                usage += f" {command.signature}"
            commands_list.append({
                "type": "Prefiks",
                "name": command.qualified_name,
                "usage": usage,
                "aliases": list(command.aliases),
                "purpose": purpose,
                "permission": permission,
            })

        for command in sorted(bot.tree.get_commands(), key=lambda item: item.name.casefold()):
            parameters = []
            for parameter in getattr(command, "parameters", ()):
                parameters.append(f"<{parameter.name}>" if parameter.required else f"[{parameter.name}]")
            usage = "/" + command.qualified_name
            if parameters:
                usage += " " + " ".join(parameters)
            commands_list.append({
                "type": "Slash",
                "name": command.qualified_name,
                "usage": usage,
                "aliases": [],
                "purpose": command.description or "Əmr haqqında izah əlavə edilməyib.",
                "permission": slash_permissions.get(command.name, "Hər kəs"),
            })

        return web.json_response({"success": True, "commands": commands_list})

    @require_auth
    async def handle_api_bots(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit():
            return web.json_response({"success": False, "error": "Server seçilməyib."}, status=400)
        guild = moderation_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        bots = sorted((member for member in guild.members if member.bot), key=lambda member: member.display_name.casefold())
        def provider_for(name):
            normalized = re.sub(r"[^a-z0-9]+", "", name.casefold())
            aliases = (("carl", "carl"), ("mee6", "mee6"), ("dyno", "dyno"),
                       ("tickettool", "tickettool"), ("giveawaybot", "giveawaybot"),
                       ("statbot", "statbot"), ("owo", "owo"), ("mudae", "mudae"))
            return next((provider_id for marker, provider_id in aliases if marker in normalized), None)
        return web.json_response({
            "success": True,
            "guild": {"id": str(guild.id), "name": guild.name},
            "bots": [{"id": str(member.id), "name": member.display_name, "username": member.name,
                      "avatar": member.display_avatar.url, "provider": provider_for(member.name) or provider_for(member.display_name)} for member in bots],
            "providers": BOT_GUIDES,
        })

    @require_auth
    async def handle_api_bot_commands(request: web.Request):
        provider_id = request.query.get("provider", "")
        provider = next((item for item in BOT_GUIDES if item["id"] == provider_id), None)
        if not provider:
            return web.json_response({"success": False, "error": "Bu bot üçün rəsmi mənbə tapılmadı."}, status=404)
        now = time.monotonic()
        cached = _BOT_COMMAND_CACHE.get(provider_id)
        cache_ttl = 900 if cached and cached["data"].get("fetched_at") else 120
        if cached and now - cached["time"] < cache_ttl:
            return web.json_response({"success": True, **cached["data"]})
        try:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "AbiBotAdmin/1.0 (official-docs-reader)"}) as session:
                async with session.get(provider["source"], allow_redirects=True) as response:
                    if response.status != 200:
                        raise ValueError(f"Rəsmi mənbə HTTP {response.status} qaytardı.")
                    raw = await response.content.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError("Rəsmi sənədlər gözləniləndən böyükdür.")
                    content = raw.decode(response.charset or "utf-8", errors="replace")
            commands = _parse_official_commands(content, provider["format"])
            result = {"provider": provider_id, "name": provider["name"], "source": provider["source"],
                      "docs": provider["docs"], "fetched_at": datetime.now(timezone.utc).isoformat(timespec="minutes") + "Z",
                      "commands": commands,
                      "message": "" if commands else "Rəsmi səhifə açıldı, amma orada maşınla oxuna bilən komanda siyahısı yoxdur. Rəsmi sənədləri açın və ya Discord-da botun slash/help menyusundan baxın."}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            result = {"provider": provider_id, "name": provider["name"], "source": provider["source"],
                      "docs": provider["docs"], "fetched_at": None, "commands": [],
                      "message": f"Rəsmi komanda səhifəsi hazırda oxuna bilmədi ({str(exc)[:160]}). Sənədləri yeni pəncərədə açın."}
        _BOT_COMMAND_CACHE[provider_id] = {"time": now, "data": result}
        return web.json_response({"success": True, **result})

    @require_auth
    async def handle_api_moderation_members(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit():
            return web.json_response({"success": False, "error": "Server seçilməyib."}, status=400)
        guild = moderation_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı və ya icazəli deyil."}, status=404)
        query = request.query.get("q", "").strip().casefold()
        members = [member for member in guild.members if not member.bot]
        if query:
            members = [member for member in members if query in member.display_name.casefold() or query in member.name.casefold() or query in str(member.id)]
        members.sort(key=lambda member: member.display_name.casefold())
        return web.json_response({
            "success": True,
            "members": [{"id": str(member.id), "name": member.display_name, "username": member.name,
                         "avatar": member.display_avatar.url} for member in members[:100]],
        })

    @require_auth
    async def handle_api_moderation_member(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        user_id = request.query.get("user_id", "")
        if not guild_id.isdigit() or not user_id.isdigit():
            return web.json_response({"success": False, "error": "Server və üzv seçilməlidir."}, status=400)
        guild = moderation_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Discord botu hazır deyil."}, status=503)
        member = guild.get_member(int(user_id))
        if not member:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.NotFound:
                return web.json_response({"success": False, "error": "Üzv bu serverdə tapılmadı."}, status=404)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Üzv məlumatı Discord-dan alına bilmədi."}, status=502)
        return web.json_response({
            "success": True,
            "member": {
                "id": str(member.id), "name": member.display_name, "username": member.name,
                "avatar": member.display_avatar.url, "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "created_at": member.created_at.isoformat(), "bot": member.bot,
                "roles": [{"id": str(role.id), "name": role.name, "color": f"#{role.color.value:06x}" if role.color.value else "#6b7280"}
                          for role in reversed(member.roles) if role != guild.default_role],
            },
        })

    @require_auth
    async def handle_api_moderation_roles(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        user_id = request.query.get("user_id", "")
        if not guild_id.isdigit() or not user_id.isdigit():
            return web.json_response({"success": False, "error": "Server və üzv seçilməlidir."}, status=400)
        guild = moderation_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Discord botu hazır deyil."}, status=503)
        member = guild.get_member(int(user_id))
        if not member:
            try:
                member = await guild.fetch_member(int(user_id))
            except discord.NotFound:
                return web.json_response({"success": False, "error": "Üzv bu serverdə tapılmadı."}, status=404)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Üzv məlumatı Discord-dan alına bilmədi."}, status=502)
        me = guild.me
        can_manage = bool(me and me.guild_permissions.manage_roles)
        roles = []
        for role in sorted(guild.roles, key=lambda item: item.position, reverse=True):
            if role == guild.default_role or role.managed:
                continue
            manageable = bool(can_manage and me.top_role > role)
            roles.append({"id": str(role.id), "name": role.name, "color": f"#{role.color.value:06x}" if role.color.value else "#6b7280",
                          "assigned": role in member.roles, "manageable": manageable})
        return web.json_response({"success": True, "roles": roles, "can_manage_roles": can_manage})

    @require_auth
    async def handle_api_moderation_role_action(request: web.Request):
        try:
            data = await request.json()
            guild_id, user_id, role_id = (int(data.get(key, 0)) for key in ("guild_id", "user_id", "role_id"))
            action = str(data.get("action", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()[:400] or "Admin Panel rol idarəetməsi"
            csrf_token = data.get("csrf_token", "")
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "Sorğu məlumatı düzgün deyil."}, status=400)
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        if action not in {"add", "remove"} or min(guild_id, user_id, role_id) <= 0:
            return web.json_response({"success": False, "error": "Server, üzv, rol və əməliyyat düzgün seçilməyib."}, status=400)
        guild = moderation_guild(guild_id)
        if not guild or not guild.me:
            return web.json_response({"success": False, "error": "Server və ya bot üzvlük məlumatı tapılmadı."}, status=404)
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return web.json_response({"success": False, "error": "Bota Manage Roles icazəsi verilməyib."}, status=403)
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            return web.json_response({"success": False, "error": "Üzv bu serverdə tapılmadı."}, status=404)
        except discord.HTTPException:
            return web.json_response({"success": False, "error": "Üzv məlumatı Discord-dan alına bilmədi."}, status=502)
        role = guild.get_role(role_id)
        if not role or role == guild.default_role or role.managed or me.top_role <= role:
            return web.json_response({"success": False, "error": "Bu rol mövcud deyil və ya bot onu idarə edə bilməz."}, status=403)
        if member.id == guild.owner_id or me.top_role <= member.top_role:
            return web.json_response({"success": False, "error": "Botun rolu üzvün ən yüksək rolundan yuxarı olmalıdır."}, status=403)
        if action == "add" and role in member.roles:
            return web.json_response({"success": False, "error": "Üzvdə bu rol artıq var."}, status=409)
        if action == "remove" and role not in member.roles:
            return web.json_response({"success": False, "error": "Üzvdə bu rol yoxdur."}, status=409)
        try:
            audit_reason = f"Admin Panel | {reason}"[:512]
            if action == "add":
                await member.add_roles(role, reason=audit_reason)
                message = f"{role.name} rolu {member.display_name} üzvünə verildi."
            else:
                await member.remove_roles(role, reason=audit_reason)
                message = f"{role.name} rolu {member.display_name} üzvündən alındı."
        except discord.Forbidden:
            return web.json_response({"success": False, "error": "Discord bu rol əməliyyatına icazə vermədi."}, status=403)
        except discord.HTTPException:
            return web.json_response({"success": False, "error": "Rol əməliyyatı icra edilə bilmədi."}, status=502)
        return web.json_response({"success": True, "message": message})

    @require_auth
    async def handle_api_moderation_warnings(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        user_id = request.query.get("user_id", "")
        if not guild_id.isdigit() or not user_id.isdigit():
            return web.json_response({"success": False, "error": "Server və üzv seçilməlidir."}, status=400)
        guild = moderation_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        return web.json_response({
            "success": True,
            "total": db.get_warning_count(guild.id, int(user_id)),
            "warnings": db.get_warnings(guild.id, int(user_id), limit=20),
        })

    @require_auth
    async def handle_api_moderation_action(request: web.Request):
        try:
            data = await request.json()
            guild_id = int(data.get("guild_id", 0))
            user_id = int(data.get("user_id", 0))
            action = str(data.get("action", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()[:500] or "Admin panel əməliyyatı"
            csrf_token = data.get("csrf_token", "")
            minutes = int(data.get("minutes", 0) or 0)
        except Exception:
            return web.json_response({"success": False, "error": "Sorğu məlumatı düzgün deyil."}, status=400)
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        if action not in {"timeout", "untimeout", "kick", "ban", "unban"} or guild_id <= 0 or user_id <= 0:
            return web.json_response({"success": False, "error": "Əməliyyat və ya istifadəçi ID-si düzgün deyil."}, status=400)
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Discord botu hazır deyil."}, status=503)
        guild = moderation_guild(guild_id)
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı və ya icazəli deyil."}, status=404)
        me = guild.me
        if not me:
            return web.json_response({"success": False, "error": "Botun server üzvlük məlumatı tapılmadı."}, status=503)
        audit_reason = f"Admin Panel | {reason}"[:512]

        if action == "unban":
            if not me.guild_permissions.ban_members:
                return web.json_response({"success": False, "error": "Bota Ban Members icazəsi verilməyib."}, status=403)
            try:
                ban_entry = await guild.fetch_ban(discord.Object(id=user_id))
                await guild.unban(ban_entry.user, reason=audit_reason)
            except discord.NotFound:
                return web.json_response({"success": False, "error": "Bu ID ilə banlı üzv tapılmadı."}, status=404)
            except discord.Forbidden:
                return web.json_response({"success": False, "error": "Discord botun banı açmasına icazə vermədi."}, status=403)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Discord banı aça bilmədi."}, status=502)
            return web.json_response({"success": True, "message": f"{user_id} ID-li üzvün banı açıldı."})

        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return web.json_response({"success": False, "error": "Üzv bu serverdə tapılmadı."}, status=404)
            except discord.Forbidden:
                return web.json_response({"success": False, "error": "Bot üzv məlumatını oxuya bilmədi."}, status=403)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Discord üzv məlumatını ala bilmədi."}, status=502)

        if action in {"timeout", "untimeout"}:
            if not me.guild_permissions.moderate_members:
                return web.json_response({"success": False, "error": "Bota Moderate Members icazəsi verilməyib."}, status=403)
            if member.id == guild.owner_id or me.top_role <= member.top_role:
                return web.json_response({"success": False, "error": "Botun rolu bu üzvün rolundan yuxarı olmalıdır."}, status=403)
            if action == "untimeout":
                until = None
                success_message = f"{member.display_name} üçün timeout qaldırıldı."
            else:
                if not 1 <= minutes <= 40320:
                    return web.json_response({"success": False, "error": "Timeout 1–40320 dəqiqə arasında olmalıdır."}, status=400)
                until = discord.utils.utcnow() + timedelta(minutes=minutes)
                success_message = f"{member.display_name} üçün {minutes} dəqiqəlik timeout tətbiq edildi."
            try:
                await member.timeout(until, reason=audit_reason)
            except discord.Forbidden:
                return web.json_response({"success": False, "error": "Discord bu timeout əməliyyatına icazə vermədi."}, status=403)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Discord timeout əməliyyatını icra edə bilmədi."}, status=502)
            return web.json_response({"success": True, "message": success_message})

        permission = "kick_members" if action == "kick" else "ban_members"
        if not getattr(me.guild_permissions, permission):
            return web.json_response({"success": False, "error": f"Bota {permission.replace('_', ' ').title()} icazəsi verilməyib."}, status=403)
        if member.id == guild.owner_id or me.top_role <= member.top_role:
            return web.json_response({"success": False, "error": "Botun rolu bu üzvün rolundan yuxarı olmalıdır."}, status=403)
        try:
            if action == "kick":
                await member.kick(reason=audit_reason)
                message = f"{member.display_name} serverdən çıxarıldı."
            else:
                await member.ban(reason=audit_reason, delete_message_seconds=0)
                message = f"{member.display_name} serverdən banlandı."
        except discord.Forbidden:
            return web.json_response({"success": False, "error": "Discord botun bu üzvə qarşı əməliyyatına icazə vermədi."}, status=403)
        except discord.HTTPException:
            return web.json_response({"success": False, "error": "Discord moderasiya əməliyyatını icra edə bilmədi."}, status=502)
        return web.json_response({"success": True, "message": message})

    # 6. API: Fetch channels available for admin posts
    @require_auth
    async def handle_api_channels(request: web.Request):
        if not bot.is_ready():
            return web.json_response({"success": False, "error": "Bot hələ Discord-a tam qoşulmayıb."}, status=503)

        channels = []
        for guild in bot.guilds:
            for ch in guild.text_channels:
                perms = ch.permissions_for(guild.me)
                if perms.send_messages and perms.embed_links and perms.attach_files:
                    channels.append({
                        "id": str(ch.id),
                        "name": ch.name,
                        "guild_id": str(guild.id),
                        "guild_name": guild.name,
                        "display": f"{guild.name} ➔ #{ch.name}"
                    })

        return web.json_response({
            "success": True,
            "channels": channels
        })

    @require_auth
    async def handle_api_log_settings(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit():
            return web.json_response({"success": False, "error": "Server seçilməlidir."}, status=400)
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        saved = db.get_log_settings(guild.id)
        channels = []
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel and permissions.send_messages and permissions.embed_links:
                channels.append({"id": str(channel.id), "name": channel.name,
                                 "category": channel.category.name if channel.category else ""})
        return web.json_response({"success": True,
            "events": [{**item, "enabled": saved.get(item["key"], {}).get("enabled", True),
                        "channel_id": str(saved[item["key"]]["channel_id"]) if saved.get(item["key"], {}).get("channel_id") else None}
                       for item in LOG_EVENTS],
            "channels": channels})

    @require_auth
    async def handle_api_save_log_settings(request: web.Request):
        try:
            data = await request.json()
            guild_id = int(data.get("guild_id", 0))
            entries = data.get("events", [])
            csrf_token = str(data.get("csrf_token", ""))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "Sorğu məlumatı düzgün deyil."}, status=400)
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        guild = bot.get_guild(guild_id) if guild_id > 0 else None
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        if not isinstance(entries, list) or len(entries) > len(LOG_EVENTS):
            return web.json_response({"success": False, "error": "Log hadisələrinin siyahısı yanlışdır."}, status=400)
        for item in entries:
            if not isinstance(item, dict) or item.get("key") not in LOG_EVENT_MAP:
                return web.json_response({"success": False, "error": "Naməlum log hadisəsi."}, status=400)
            if "enabled" in item and not isinstance(item["enabled"], bool):
                return web.json_response({"success": False, "error": "Hadisə aktivliyi boolean olmalıdır."}, status=400)
            raw_channel = item.get("channel_id")
            channel = None
            if raw_channel not in (None, "", 0, "0"):
                try:
                    channel = guild.get_channel(int(raw_channel))
                except (TypeError, ValueError):
                    channel = None
                if not isinstance(channel, discord.TextChannel):
                    return web.json_response({"success": False, "error": "Log üçün mətn kanalı seçilməlidir."}, status=400)
                permissions = channel.permissions_for(guild.me)
                if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                    return web.json_response({"success": False, "error": f"#{channel.name} kanalında botun baxmaq, mesaj və embed icazəsi olmalıdır."}, status=403)
            db.set_log_setting(guild.id, item["key"], item.get("enabled", True), channel.id if channel else None)
        return web.json_response({"success": True, "message": "Log sazlamaları yadda saxlanıldı."})

    @require_auth
    async def handle_api_server_settings(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        guild = bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        channels = [{"id": str(channel.id), "name": channel.name}
                    for channel in guild.text_channels
                    if guild.me and (perms := channel.permissions_for(guild.me)).view_channel and perms.send_messages]
        roles = [{"id": str(role.id), "name": role.name, "editable": _manageable_role(guild, role)}
                 for role in sorted(guild.roles, key=lambda item: item.position, reverse=True)
                 if role != guild.default_role]
        return web.json_response({
            "success": True,
            "channels": channels,
            "roles": roles,
            "autorole_id": db.get_guild_setting(guild.id, "autorole", ""),
            "welcome_channel_id": db.get_guild_setting(guild.id, "welcome_channel", ""),
            "welcome_message": db.get_guild_setting(guild.id, "welcome_message", ""),
            "welcome_button_remove_role": db.get_guild_setting(guild.id, "welcome_button_remove_role", ""),
            "welcome_button_add_role": db.get_guild_setting(guild.id, "welcome_button_add_role", ""),
            "welcome_button_label": db.get_guild_setting(guild.id, "welcome_button_label", "Rolu dəyiş"),
            "welcome_button_allowed_role": db.get_guild_setting(guild.id, "welcome_button_allowed_role", ""),
        })

    @require_auth
    async def handle_api_save_server_settings(request: web.Request):
        try:
            data = await request.json()
            guild_id = int(data.get("guild_id", 0))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "Sorğu məlumatları düzgün deyil."}, status=400)
        if not verify_csrf(request, str(data.get("csrf_token", ""))):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        guild = bot.get_guild(guild_id) if guild_id > 0 else None
        if not guild or not guild.me:
            return web.json_response({"success": False, "error": "Server və ya botun üzvlük məlumatı tapılmadı."}, status=404)

        role_id = str(data.get("autorole_id", "") or "").strip()
        role = None
        if role_id:
            if not role_id.isdigit():
                return web.json_response({"success": False, "error": "Avtomatik rol seçimi düzgün deyil."}, status=400)
            role = guild.get_role(int(role_id))
            if not _manageable_role(guild, role):
                return web.json_response({"success": False, "error": "Bu rol bot tərəfindən verilə bilməz. Botun rolunu yuxarı qaldırın."}, status=400)

        remove_role_id = str(data.get("welcome_button_remove_role", "") or "").strip()
        add_role_id = str(data.get("welcome_button_add_role", "") or "").strip()
        if bool(remove_role_id) != bool(add_role_id):
            return web.json_response({"success": False, "error": "Düyməni aktivləşdirmək üçün götürüləcək və veriləcək rolu birlikdə seçin."}, status=400)
        if remove_role_id and (not remove_role_id.isdigit() or not add_role_id.isdigit()):
            return web.json_response({"success": False, "error": "Düymə üçün rol seçimi düzgün deyil."}, status=400)
        if remove_role_id:
            remove_role = guild.get_role(int(remove_role_id))
            add_role = guild.get_role(int(add_role_id))
            if remove_role and add_role and remove_role.id == add_role.id:
                return web.json_response({"success": False, "error": "Götürüləcək və veriləcək rol fərqli olmalıdır."}, status=400)
            if not _manageable_role(guild, remove_role) or not _manageable_role(guild, add_role):
                return web.json_response({"success": False, "error": "Düymə üçün hər iki rol bot tərəfindən idarə edilə bilməlidir."}, status=400)
        button_label = str(data.get("welcome_button_label", "Rolu dəyiş")).strip()
        if remove_role_id and not button_label:
            return web.json_response({"success": False, "error": "Düymə yazısı boş qala bilməz."}, status=400)
        if len(button_label) > 80:
            return web.json_response({"success": False, "error": "Düymə yazısı maksimum 80 simvol ola bilər."}, status=400)
        allowed_role_id = str(data.get("welcome_button_allowed_role", "") or "").strip()
        if allowed_role_id:
            if not allowed_role_id.isdigit():
                return web.json_response({"success": False, "error": "Düymədən istifadə icazəsi olan rol düzgün deyil."}, status=400)
            allowed_role = guild.get_role(int(allowed_role_id))
            if not allowed_role or allowed_role == guild.default_role:
                return web.json_response({"success": False, "error": "Düymə üçün istifadəçi icazə rolu seçin."}, status=400)

        channel_id = str(data.get("welcome_channel_id", "") or "").strip()
        welcome_channel = None
        if channel_id:
            if not channel_id.isdigit():
                return web.json_response({"success": False, "error": "Qarşılama kanalı düzgün deyil."}, status=400)
            welcome_channel = guild.get_channel(int(channel_id))
            if not isinstance(welcome_channel, discord.TextChannel):
                return web.json_response({"success": False, "error": "Qarşılama üçün mətn kanalı seçin."}, status=400)
            permissions = welcome_channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.send_messages):
                return web.json_response({"success": False, "error": "Botun seçilən kanalda mesaj göndərmə icazəsi yoxdur."}, status=403)
        if remove_role_id and not channel_id:
            return web.json_response({"success": False, "error": "Rol düyməsini qarşılama kanalında göstərmək üçün kanal seçin."}, status=400)

        welcome_message = str(data.get("welcome_message", ""))
        if len(welcome_message) > 1800:
            return web.json_response({"success": False, "error": "Qarşılama mətni 1800 simvoldan uzun ola bilməz."}, status=400)

        try:
            db.set_guild_settings(guild.id, {
                "autorole": role_id,
                "welcome_channel": channel_id,
                "welcome_message": welcome_message,
                "welcome_button_remove_role": remove_role_id,
                "welcome_button_add_role": add_role_id,
                "welcome_button_label": button_label or "Rolu dəyiş",
                "welcome_button_allowed_role": allowed_role_id,
            })
        except Exception:
            logger.exception("Server sazlamaları yadda saxlanmadı | guild=%s", guild.id)
            return web.json_response({"success": False, "error": "Sazlamalar yadda saxlanmadı. Redis bağlantısını yoxlayın."}, status=503)
        return web.json_response({"success": True, "message": "Server sazlamaları saxlanıldı."})

    @require_auth
    async def handle_api_log_events(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit() or not bot.get_guild(int(guild_id)):
            return web.json_response({"success": False, "error": "Server seçilməlidir."}, status=400)
        event_key = request.query.get("event", "") or None
        if event_key and event_key not in LOG_EVENT_MAP:
            return web.json_response({"success": False, "error": "Log növü tapılmadı."}, status=400)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 100))
            offset = max(0, int(request.query.get("offset", "0")))
        except ValueError:
            return web.json_response({"success": False, "error": "Səhifə parametrləri yanlışdır."}, status=400)
        start = request.query.get("start", "") or None
        end = request.query.get("end", "") or None
        search = request.query.get("q", "").strip()[:100] or None
        try:
            total, rows = db.query_log_events(int(guild_id), start, end, event_key, search, limit, offset)
            for row in rows:
                row["details"] = json.loads(row.get("details") or "{}")
            return web.json_response({"success": True, "total": total, "events": rows})
        except Exception:
            logger.exception("Log arxivi oxunmadı | guild=%s", guild_id)
            return web.json_response({"success": False, "error": "Log arxivini oxumaq mümkün olmadı."}, status=500)

    @require_auth
    async def handle_api_log_analytics(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit() or not bot.get_guild(int(guild_id)):
            return web.json_response({"success": False, "error": "Server seçilməlidir."}, status=400)
        try:
            end = datetime.fromisoformat(request.query.get("end", "").replace("Z", "+00:00")) if request.query.get("end") else datetime.now(timezone.utc)
            start = datetime.fromisoformat(request.query.get("start", "").replace("Z", "+00:00")) if request.query.get("start") else end - timedelta(days=30)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if start > end or (end - start).days > 366:
                return web.json_response({"success": False, "error": "Tarix aralığı 366 gündən çox ola bilməz."}, status=400)
        except ValueError:
            return web.json_response({"success": False, "error": "Tarix formatı düzgün deyil."}, status=400)
        result = db.get_log_analytics(int(guild_id), start.isoformat(), end.isoformat())
        return web.json_response({"success": True, **result})

    @require_auth
    async def handle_api_structure(request: web.Request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id.isdigit():
            return web.json_response({"success": False, "error": "Server seçilməlidir."}, status=400)
        guild = bot.get_guild(int(guild_id))
        if not guild:
            return web.json_response({"success": False, "error": "Server tapılmadı."}, status=404)
        me = guild.me
        categories = [{"id": str(item.id), "name": item.name, "position": item.position,
                       "overwrites": _permission_overwrite_json(item, guild)} for item in guild.categories]
        channels = []
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            channel_type = "text" if isinstance(channel, discord.TextChannel) else "voice" if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) else None
            if channel_type:
                channels.append({"id": str(channel.id), "name": channel.name, "type": channel_type,
                                 "category_id": str(channel.category_id) if channel.category_id else "",
                                 "position": channel.position, "overwrites": _permission_overwrite_json(channel, guild)})
        roles = [{"id": str(role.id), "name": role.name, "color": f"#{role.color.value:06x}",
                  "position": role.position, "managed": role.managed, "editable": _manageable_role(guild, role),
                  "permissions": {flag: getattr(role.permissions, flag) for flag in ROLE_PERMISSION_FLAGS}}
                 for role in sorted(guild.roles, key=lambda item: item.position, reverse=True)]
        return web.json_response({"success": True, "guild": {"id": str(guild.id), "name": guild.name},
                                  "categories": categories, "channels": channels, "roles": roles,
                                  "permissions": {"manage_channels": bool(me and me.guild_permissions.manage_channels),
                                                  "manage_roles": bool(me and me.guild_permissions.manage_roles)},
                                  "channel_permission_flags": CHANNEL_PERMISSION_FLAGS,
                                  "role_permission_flags": ROLE_PERMISSION_FLAGS})

    @require_auth
    async def handle_api_structure_action(request: web.Request):
        try:
            data = await request.json()
            guild_id = int(data.get("guild_id", 0))
            action = str(data.get("action", "")).strip().lower()
            csrf_token = str(data.get("csrf_token", ""))
        except (TypeError, ValueError):
            return web.json_response({"success": False, "error": "Sorğu məlumatları düzgün deyil."}, status=400)
        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF yoxlaması uğursuz oldu."}, status=403)
        guild = bot.get_guild(guild_id) if guild_id > 0 else None
        if not guild or not guild.me:
            return web.json_response({"success": False, "error": "Server və ya botun üzvlük məlumatı tapılmadı."}, status=404)
        me = guild.me
        audit = f"Admin Panel | {str(data.get('reason', 'Server quruluşu redaktəsi'))[:400]}"[:512]

        if action in {"create_role", "edit_role", "delete_role"}:
            if not me.guild_permissions.manage_roles:
                return web.json_response({"success": False, "error": "Bota Manage Roles icazəsi verilməyib."}, status=403)
            try:
                role_id = int(data.get("role_id", 0) or 0)
            except (TypeError, ValueError):
                return web.json_response({"success": False, "error": "Rol ID-si düzgün deyil."}, status=400)
            role = guild.get_role(role_id) if role_id else None
            if action != "create_role" and not role:
                return web.json_response({"success": False, "error": "Rol tapılmadı."}, status=404)
            if action != "create_role" and not _manageable_role(guild, role):
                return web.json_response({"success": False, "error": "Bu rol bot tərəfindən idarə edilə bilməz (managed, @everyone və ya bot rolundan yuxarıdır)."}, status=403)
            try:
                if action == "delete_role":
                    await role.delete(reason=audit)
                    return web.json_response({"success": True, "message": f"{role.name} rolu silindi."})
                name = str(data.get("name", "")).strip()
                if not name or len(name) > 100:
                    return web.json_response({"success": False, "error": "Rol adı 1–100 simvol olmalıdır."}, status=400)
                color = str(data.get("color", "#000000"))
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    return web.json_response({"success": False, "error": "Rol rəngi #RRGGBB formatında olmalıdır."}, status=400)
                raw_permissions = data.get("permissions", {})
                if not isinstance(raw_permissions, dict) or any(key not in ROLE_PERMISSION_FLAGS for key in raw_permissions):
                    return web.json_response({"success": False, "error": "Rol icazələri siyahısı düzgün deyil."}, status=400)
                permission_values = {flag: bool(raw_permissions.get(flag, getattr(role.permissions, flag) if role else False)) for flag in ROLE_PERMISSION_FLAGS}
                permissions = discord.Permissions(**permission_values)
                if action == "create_role":
                    role = await guild.create_role(name=name, colour=discord.Colour(int(color[1:], 16)), permissions=permissions, reason=audit)
                    message = f"{role.name} rolu yaradıldı."
                else:
                    await role.edit(name=name, colour=discord.Colour(int(color[1:], 16)), permissions=permissions, reason=audit)
                    message = f"{role.name} rolu yeniləndi."
                return web.json_response({"success": True, "message": message, "role_id": str(role.id)})
            except discord.Forbidden:
                return web.json_response({"success": False, "error": "Discord rol əməliyyatına icazə vermədi; botun rol sırasını yoxla."}, status=403)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Discord rol əməliyyatını icra edə bilmədi."}, status=502)

        if action in {"create_category", "edit_category", "delete_category", "create_channel", "edit_channel", "delete_channel", "save_overwrites"}:
            if not me.guild_permissions.manage_channels:
                return web.json_response({"success": False, "error": "Bota Manage Channels icazəsi verilməyib."}, status=403)
            try:
                object_id = int(data.get("object_id", 0) or 0)
            except (TypeError, ValueError):
                return web.json_response({"success": False, "error": "Kanal/kateqoriya ID-si düzgün deyil."}, status=400)
            target = guild.get_channel(object_id) if object_id else None
            try:
                if action == "create_category":
                    name = str(data.get("name", "")).strip()
                    if not name or len(name) > 100:
                        return web.json_response({"success": False, "error": "Kateqoriya adı 1–100 simvol olmalıdır."}, status=400)
                    item = await guild.create_category(name, reason=audit)
                    return web.json_response({"success": True, "message": f"{item.name} kateqoriyası yaradıldı.", "object_id": str(item.id)})
                if action == "create_channel":
                    name = str(data.get("name", "")).strip()
                    channel_type = str(data.get("channel_type", "text"))
                    if not name or len(name) > 100 or channel_type not in {"text", "voice"}:
                        return web.json_response({"success": False, "error": "Kanal adı və tipi düzgün deyil."}, status=400)
                    category_id = int(data.get("category_id", 0) or 0)
                    category = guild.get_channel(category_id) if category_id else None
                    if category_id and not isinstance(category, discord.CategoryChannel):
                        return web.json_response({"success": False, "error": "Kateqoriya tapılmadı."}, status=404)
                    if channel_type == "text":
                        item = await guild.create_text_channel(name, category=category, reason=audit)
                    else:
                        item = await guild.create_voice_channel(name, category=category, reason=audit)
                    return web.json_response({"success": True, "message": f"#{item.name} kanalı yaradıldı.", "object_id": str(item.id)})
                if not target:
                    return web.json_response({"success": False, "error": "Kanal və ya kateqoriya tapılmadı."}, status=404)
                if action in {"delete_category", "delete_channel"}:
                    if action == "delete_category" and not isinstance(target, discord.CategoryChannel):
                        return web.json_response({"success": False, "error": "Seçilən obyekt kateqoriya deyil."}, status=400)
                    if action == "delete_channel" and isinstance(target, discord.CategoryChannel):
                        return web.json_response({"success": False, "error": "Kateqoriyanı silmək üçün kateqoriya əməliyyatını seç."}, status=400)
                    label = target.name
                    await target.delete(reason=audit)
                    return web.json_response({"success": True, "message": f"{label} silindi."})
                if action == "edit_category" and not isinstance(target, discord.CategoryChannel):
                    return web.json_response({"success": False, "error": "Seçilən obyekt kateqoriya deyil."}, status=400)
                if action in {"edit_channel", "save_overwrites"} and isinstance(target, discord.CategoryChannel) and action == "edit_channel":
                    return web.json_response({"success": False, "error": "Kateqoriyanı redaktə etmək üçün kateqoriya əməliyyatını seç."}, status=400)
                edits = {}
                if action in {"edit_category", "edit_channel"}:
                    name = str(data.get("name", "")).strip()
                    if not name or len(name) > 100:
                        return web.json_response({"success": False, "error": "Ad 1–100 simvol olmalıdır."}, status=400)
                    edits["name"] = name
                    if action == "edit_channel":
                        category_id = int(data.get("category_id", 0) or 0)
                        category = guild.get_channel(category_id) if category_id else None
                        if category_id and not isinstance(category, discord.CategoryChannel):
                            return web.json_response({"success": False, "error": "Kateqoriya tapılmadı."}, status=404)
                        edits["category"] = category
                if action == "save_overwrites" or "overwrites" in data:
                    raw_overwrites = data.get("overwrites", {})
                    if not isinstance(raw_overwrites, dict):
                        return web.json_response({"success": False, "error": "İcazə cədvəli düzgün deyil."}, status=400)
                    overwrites = dict(target.overwrites)
                    for principal_id, values in raw_overwrites.items():
                        principal = guild.get_role(int(principal_id))
                        if not principal or not isinstance(values, dict):
                            return web.json_response({"success": False, "error": "İcazə verilən rol serverdə tapılmadı."}, status=400)
                        current = overwrites.get(principal, discord.PermissionOverwrite())
                        for flag in CHANNEL_PERMISSION_FLAGS:
                            value = values.get(flag)
                            if value not in (True, False, None):
                                return web.json_response({"success": False, "error": "İcazə dəyəri inherit/allow/deny olmalıdır."}, status=400)
                            setattr(current, flag, value)
                        if current.is_empty():
                            overwrites.pop(principal, None)
                        else:
                            overwrites[principal] = current
                    edits["overwrites"] = overwrites
                if action == "save_overwrites" or edits:
                    await target.edit(reason=audit, **edits)
                    return web.json_response({"success": True, "message": f"{target.name} yeniləndi."})
                return web.json_response({"success": False, "error": "Əməliyyat tanınmadı."}, status=400)
            except (ValueError, TypeError):
                return web.json_response({"success": False, "error": "Kanal/kateqoriya məlumatları düzgün deyil."}, status=400)
            except discord.Forbidden:
                return web.json_response({"success": False, "error": "Discord kanal əməliyyatına icazə vermədi."}, status=403)
            except discord.HTTPException:
                return web.json_response({"success": False, "error": "Discord kanal əməliyyatını icra edə bilmədi."}, status=502)
        return web.json_response({"success": False, "error": "Əməliyyat tanınmadı."}, status=400)

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
            elif name in ("images", "files", "file", "image") or part.filename:
                filename = part.filename or f"image_{len(uploaded_files)+1}.png"
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

        # Find target channel and ensure it is an accessible text channel.
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                return web.json_response({"success": False, "error": "Kanal Discord-da tapılmadı."}, status=404)

        if not isinstance(channel, discord.TextChannel):
            return web.json_response({
                "success": False,
                "error": "Seçilmiş kanal mətn kanalı deyil."
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
                if sched_dt > datetime.now(timezone.utc):
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
                guild_id=channel.guild.id,
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
            guild_id=channel.guild.id,
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
                "guild_id": str(p["guild_id"]),
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

    # 11. API: Generate Image Description with Gemini
    @require_auth
    async def handle_api_generate_description(request: web.Request):
        try:
            reader = await request.multipart()
        except Exception:
            return web.json_response({"success": False, "error": "Sorğu oxunmadı."}, status=400)

        csrf_token = ""
        image_url = ""
        uploaded_bytes = None
        uploaded_mime = "image/jpeg"

        while True:
            part = await reader.next()
            if part is None:
                break
            name = part.name
            if name == "csrf_token":
                csrf_token = (await part.text()).strip()
            elif name == "image_url":
                image_url = (await part.text()).strip()
            elif name in ("file", "image", "files"):
                fname = part.filename or "image.jpg"
                uploaded_bytes = await part.read(decode=False)
                ext = Path(fname).suffix.lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
                uploaded_mime = mime_map.get(ext, "image/jpeg")

        if not verify_csrf(request, csrf_token):
            return web.json_response({"success": False, "error": "CSRF təhlükəsizlik xətası."}, status=403)

        target_bytes = uploaded_bytes
        target_mime = uploaded_mime

        if not target_bytes and image_url:
            async with aiohttp.ClientSession() as fetch_sess:
                bdata, fname, err = await safe_fetch_image_url(fetch_sess, image_url)
                if err:
                    return web.json_response({"success": False, "error": f"URL xətası: {err}"}, status=400)
                target_bytes = bdata
                ext = Path(fname or "").suffix.lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
                target_mime = mime_map.get(ext, "image/jpeg")

        if not target_bytes:
            return web.json_response({"success": False, "error": "Təsvir yaratmaq üçün zəhmət olmasa əvvəlcə bir şəkil faylı seçin və ya şəkil linki daxil edin."}, status=400)

        desc, err = await generate_image_description(target_bytes, target_mime)
        if err:
            return web.json_response({"success": False, "error": err}, status=500)

        return web.json_response({
            "success": True,
            "description": desc
        })

    # Register Routes
    app.router.add_get("/admin/login", handle_login_page)
    app.router.add_post("/admin/login", handle_login_action)
    app.router.add_post("/admin/logout", handle_logout)

    app.router.add_get("/admin", handle_admin_dashboard)
    app.router.add_get("/admin/api/session", handle_api_session_info)
    app.router.add_get("/admin/api/stats", handle_api_system_stats)

    app.router.add_get("/admin/api/moderation/guilds", handle_api_moderation_guilds)
    app.router.add_get("/admin/api/bots", handle_api_bots)
    app.router.add_get("/admin/api/bots/commands", handle_api_bot_commands)
    app.router.add_get("/admin/api/moderation/members", handle_api_moderation_members)
    app.router.add_get("/admin/api/moderation/member", handle_api_moderation_member)
    app.router.add_get("/admin/api/moderation/roles", handle_api_moderation_roles)
    app.router.add_post("/admin/api/moderation/roles", handle_api_moderation_role_action)
    app.router.add_post("/admin/api/moderation/action", handle_api_moderation_action)
    app.router.add_get("/admin/api/commands", handle_api_command_catalog)
    app.router.add_get("/admin/api/channels", handle_api_channels)
    app.router.add_get("/admin/api/logs/settings", handle_api_log_settings)
    app.router.add_post("/admin/api/logs/settings", handle_api_save_log_settings)
    app.router.add_get("/admin/api/server-settings", handle_api_server_settings)
    app.router.add_post("/admin/api/server-settings", handle_api_save_server_settings)
    app.router.add_get("/admin/api/logs/events", handle_api_log_events)
    app.router.add_get("/admin/api/logs/analytics", handle_api_log_analytics)
    app.router.add_get("/admin/api/structure", handle_api_structure)
    app.router.add_post("/admin/api/structure", handle_api_structure_action)
    app.router.add_post("/admin/api/posts", handle_api_create_post)
    app.router.add_get("/admin/api/history", handle_api_history)
    app.router.add_post("/admin/api/delete", handle_api_delete_post)
    app.router.add_post("/admin/api/resend", handle_api_resend_post)
    app.router.add_post("/admin/api/generate-description", handle_api_generate_description)


