"""Guild-scoped event logging, embeds, and event catalog."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord

logger = logging.getLogger("abi_bot.logs")

LOG_EVENTS = [
    {"key": "member_join", "label": "Üzv qoşuldu", "group": "Üzvlər"},
    {"key": "member_leave", "label": "Üzv ayrıldı", "group": "Üzvlər"},
    {"key": "member_update", "label": "Üzv profili dəyişdi", "group": "Üzvlər"},
    {"key": "member_roles", "label": "Üzv rolları dəyişdi", "group": "Üzvlər"},
    {"key": "invite_create", "label": "Dəvət yaradıldı", "group": "Dəvətlər"},
    {"key": "invite_delete", "label": "Dəvət silindi", "group": "Dəvətlər"},
    {"key": "invite_use", "label": "Dəvət istifadə edildi", "group": "Dəvətlər"},
    {"key": "message_edit", "label": "Mesaj redaktə edildi", "group": "Mesajlar"},
    {"key": "message_delete", "label": "Mesaj silindi", "group": "Mesajlar"},
    {"key": "message_bulk_delete", "label": "Mesajlar toplu silindi", "group": "Mesajlar"},
    {"key": "moderation_warn", "label": "Xəbərdarlıq verildi", "group": "Moderasiya"},
    {"key": "moderation_timeout", "label": "Timeout tətbiq edildi/qaldırıldı", "group": "Moderasiya"},
    {"key": "moderation_kick", "label": "Üzv serverdən çıxarıldı", "group": "Moderasiya"},
    {"key": "moderation_ban", "label": "Ban verildi/qaldırıldı", "group": "Moderasiya"},
    {"key": "moderation_unban", "label": "Ban qaldırıldı", "group": "Moderasiya"},
    {"key": "role_create", "label": "Rol yaradıldı", "group": "Rollar"},
    {"key": "role_update", "label": "Rol dəyişdirildi", "group": "Rollar"},
    {"key": "role_delete", "label": "Rol silindi", "group": "Rollar"},
    {"key": "channel_create", "label": "Kanal yaradıldı", "group": "Kanallar"},
    {"key": "channel_update", "label": "Kanal dəyişdirildi", "group": "Kanallar"},
    {"key": "channel_delete", "label": "Kanal silindi", "group": "Kanallar"},
    {"key": "guild_update", "label": "Server ayarları dəyişdi", "group": "Server"},
    {"key": "voice_join", "label": "Səs kanalına qoşuldu", "group": "Səs"},
    {"key": "voice_leave", "label": "Səs kanalından çıxdı", "group": "Səs"},
    {"key": "voice_move", "label": "Səs kanalı dəyişdi", "group": "Səs"},
    {"key": "voice_state", "label": "Səs vəziyyəti dəyişdi", "group": "Səs"},
    {"key": "system_error", "label": "Bot xətası", "group": "Sistem"},
    {"key": "log_delivery_error", "label": "Log kanala çatmadı", "group": "Sistem"},
]
LOG_EVENT_MAP = {item["key"]: item for item in LOG_EVENTS}


def _display(value):
    if value is None:
        return None, None
    ident = getattr(value, "id", None)
    name = getattr(value, "display_name", None) or getattr(value, "name", None)
    if not name:
        name = f"ID {ident}" if ident is not None else str(value)
    return ident, str(name)[:200]


def make_embed(event_key, title, actor=None, target=None, channel=None, details=None, created_at=None):
    info = LOG_EVENT_MAP.get(event_key, {"label": event_key, "group": "Sistem"})
    color = {
        "Üzvlər": 0x57F287, "Dəvətlər": 0x5865F2, "Mesajlar": 0x3498DB,
        "Moderasiya": 0xED4245, "Rollar": 0x9B59B6, "Kanallar": 0x1ABC9C,
        "Server": 0xF1C40F, "Səs": 0x2ECC71, "Sistem": 0x95A5A6,
    }.get(info["group"], 0x5865F2)
    embed = discord.Embed(title=f"{info['label']} · {title}"[:256], color=color,
                          timestamp=created_at or datetime.now(timezone.utc))
    _, actor_name = _display(actor)
    _, target_name = _display(target)
    if actor_name:
        embed.add_field(name="İcraçı", value=actor_name, inline=True)
    if target_name:
        embed.add_field(name="Hədəf", value=target_name, inline=True)
    if channel is not None:
        channel_name = getattr(channel, "mention", None) or getattr(channel, "name", None) or str(channel)
        embed.add_field(name="Kanal", value=str(channel_name)[:1024], inline=True)
    remaining = max(0, 5800 - len(embed.title or "") - sum(len(field.name) + len(field.value) for field in embed.fields))
    for key, raw in (details or {}).items():
        value = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        value = str(value).strip() or "—"
        field_name = str(key)[:256]
        value_limit = min(1024, remaining - len(field_name))
        if value_limit <= 0:
            break
        value = value[:value_limit]
        embed.add_field(name=field_name, value=value, inline=False)
        remaining -= len(field_name) + len(value)
        if len(embed.fields) >= 20 or remaining <= 0:
            break
    embed.set_footer(text=f"{info['group']} · abi-bot")
    return embed


async def emit_log(bot, db, guild, event_key, title, *, actor=None, target=None,
                   channel=None, details=None, created_at=None, deliver=True):
    """Persist every enabled event and deliver it only to its configured channel."""
    if guild is None or event_key not in LOG_EVENT_MAP:
        return None
    try:
        config = db.get_log_settings(guild.id).get(event_key, {"enabled": True, "channel_id": None})
        if not config["enabled"]:
            return None
        actor_id, actor_name = _display(actor)
        target_id, target_name = _display(target)
        channel_id = getattr(channel, "id", None)
        channel_name = getattr(channel, "name", None)
        event_id = db.add_log_event(guild.id, event_key, title, actor_id, actor_name,
                                    target_id, target_name, channel_id, channel_name,
                                    details or {}, created_at.isoformat() if created_at else None)
    except Exception:
        logger.exception("Log arxivə yazılmadı | guild=%s event=%s", getattr(guild, "id", None), event_key)
        return None

    destination_id = config.get("channel_id")
    if not deliver or not destination_id:
        return event_id
    destination = guild.get_channel(int(destination_id))
    if not isinstance(destination, discord.TextChannel):
        return event_id
    try:
        await destination.send(embed=make_embed(event_key, title, actor, target, channel, details, created_at))
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("Log kanala göndərilmədi | guild=%s event=%s channel=%s: %s",
                       guild.id, event_key, destination_id, exc)
        try:
            error_config = db.get_log_settings(guild.id).get("log_delivery_error", {"enabled": True})
            if error_config.get("enabled", True):
                db.add_log_event(guild.id, "log_delivery_error", "Log mesajı göndərilmədi",
                                 target_id=destination_id, target_name=getattr(destination, "name", None),
                                 details={"event": event_key, "error": str(exc)[:500]})
        except Exception:
            logger.exception("Log çatdırılma xətası arxivə yazılmadı | guild=%s", guild.id)
    return event_id


def invite_snapshot(invite):
    inviter = getattr(invite, "inviter", None)
    channel = getattr(invite, "channel", None)
    expires = getattr(invite, "expires_at", None)
    return {
        "code": str(invite.code), "uses": int(getattr(invite, "uses", 0) or 0),
        "inviter_id": getattr(inviter, "id", None),
        "inviter_name": (getattr(inviter, "display_name", None) or str(inviter)) if inviter else None,
        "channel_id": getattr(channel, "id", None),
        "url": str(getattr(invite, "url", f"https://discord.gg/{invite.code}")),
        "expires_at": expires.isoformat() if expires else None,
    }
