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

# Hər qrup üçün rəng + kiçik emoji — embed başlığı və sərhədini fərqləndirir.
NEUTRAL = 0x262624  # avenox.io referansındakı isti qara — Discord fonunda demək olar görünməz xətt
GROUP_STYLE = {
    "Üzvlər":     {"color": NEUTRAL, "emoji": "✦"},
    "Dəvətlər":   {"color": NEUTRAL, "emoji": "✦"},
    "Mesajlar":   {"color": NEUTRAL, "emoji": "✦"},
    "Moderasiya": {"color": 0xB4443C, "emoji": "✦"},
    "Rollar":     {"color": NEUTRAL, "emoji": "✦"},
    "Kanallar":   {"color": NEUTRAL, "emoji": "✦"},
    "Server":     {"color": NEUTRAL, "emoji": "✦"},
    "Səs":        {"color": NEUTRAL, "emoji": "✦"},
    "Sistem":     {"color": 0xB4443C, "emoji": "✦"},
}

DEFAULT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"


def _display(value):
    if value is None:
        return None, None
    ident = getattr(value, "id", None)
    name = getattr(value, "display_name", None) or getattr(value, "name", None)
    if not name:
        name = f"ID {ident}" if ident is not None else str(value)
    return ident, str(name)[:200]


def _mention(value):
    """Return a clickable mention when possible, else just the display name.

    Discord's own `.mention` is used so each object type gets the right syntax:
    members/users -> <@id>, roles -> <@&id>, channels -> <#id>. Always using
    <@id> (the old behaviour) breaks roles and channels — Discord shows the
    raw bracket text instead of the coloured name chip.
    """
    ident, name = _display(value)
    if ident is not None:
        native_mention = getattr(value, "mention", None)
        return (str(native_mention) if native_mention else f"<@{ident}>"), name
    return name, name


def _avatar_url(value):
    avatar = getattr(value, "display_avatar", None) or getattr(value, "avatar", None)
    url = getattr(avatar, "url", None)
    return str(url) if url else None


def _guild_icon_url(guild):
    icon = getattr(guild, "icon", None)
    url = getattr(icon, "url", None)
    return str(url) if url else None


import re

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

INVITE_ROWS = {"Dəvət edən": "dəvət edən", "Dəvət linki": "link", "Dəvət kodu": "kod"}
INVITE_EVENTS = {"member_join", "invite_use", "invite_create", "invite_delete"}

# Giriş/dəvət logunun dizaynı: əsas sətirlər (icraçı/hədəf...) heç vaxt sükutla
# gizlədilmir — məlumat yoxdursa belə, mənalı etiket və "müəyyən edilmədi" ilə
# görünür. Bütün digər hadisə növləri də eyni prinsiplə işləsin deyə, hər event
# üçün ayrıca (actor_label, target_label) cütü təyin olunur. Qrupun ümumi
# mənasına uyğun gəlməyən generik "icraçı/hədəf" sözləri əvəzinə hər qrup üçün
# ən təbii Azərbaycan sözü seçilib (üzv, rol, kanal, müəllif, istifadəçi...).
NOT_SET = "müəyyən edilmədi"
EVENT_ROLE_LABELS = {
    # Üzvlər
    "member_leave":        {"actor": None,     "target": "üzv"},
    "member_update":       {"actor": None,     "target": "üzv"},
    "member_roles":        {"actor": "icraçı", "target": "üzv"},
    # Moderasiya
    "moderation_warn":     {"actor": "icraçı", "target": "hədəf"},
    "moderation_timeout":  {"actor": "icraçı", "target": "hədəf"},
    "moderation_kick":     {"actor": "icraçı", "target": "hədəf"},
    "moderation_ban":      {"actor": "icraçı", "target": "hədəf"},
    "moderation_unban":    {"actor": "icraçı", "target": "hədəf"},
    # Rollar — target_kind: real Role obyekti yoxdursa (məs. rol silinib), mention
    # yenə də <@&id> formatında qurulur, generik <@id> (istifadəçi) yox.
    "role_create":         {"actor": "icraçı", "target": "rol", "target_kind": "role"},
    "role_update":         {"actor": "icraçı", "target": "rol", "target_kind": "role"},
    "role_delete":         {"actor": "icraçı", "target": "rol", "target_kind": "role"},
    # Kanallar
    "channel_create":      {"actor": "icraçı", "target": "kanal", "target_kind": "channel"},
    "channel_update":      {"actor": "icraçı", "target": "kanal", "target_kind": "channel"},
    "channel_delete":      {"actor": "icraçı", "target": "kanal", "target_kind": "channel"},
    # Server
    "guild_update":        {"actor": "icraçı", "target": None},
    # Səs
    "voice_join":          {"actor": None,     "target": "üzv"},
    "voice_leave":         {"actor": None,     "target": "üzv"},
    "voice_move":          {"actor": None,     "target": "üzv"},
    "voice_state":         {"actor": None,     "target": "üzv"},
    # Mesajlar
    "message_edit":        {"actor": None,     "target": "müəllif"},
    "message_delete":      {"actor": None,     "target": "müəllif"},
    "message_bulk_delete": {"actor": None,     "target": None},
    # Sistem
    "system_error":        {"actor": None,     "target": "istifadəçi"},
    "log_delivery_error":  {"actor": None,     "target": "kanal", "target_kind": "channel"},
}
DEFAULT_ROLE_LABELS = {"actor": "icraçı", "target": "hədəf"}


def _kind_mention(value, ident, kind):
    """Discord obyektinin öz .mention-u yoxdursa (məs. silinmiş rol yalnız ID
    kimi qalır), hadisənin məlum növünə görə düzgün mention sintaksisini özümüz
    qururuq: rol -> <@&id>, kanal -> <#id>, əks halda <@id>.
    """
    native = getattr(value, "mention", None)
    if native:
        return str(native)
    if ident is None:
        return None
    if kind == "role":
        return f"<@&{ident}>"
    if kind == "channel":
        return f"<#{ident}>"
    return f"<@{ident}>"

ROW_LABELS = {
    "Discord hesabı yaradılıb": "hesab", "Serverə qoşulub": "qoşuldu", "Serverə qoşulma vaxtı": "qoşulmuşdu",
    "Səbəb": "səbəb", "İzah": "qeyd", "Mesaj ID": "mesaj id", "Əvvəlki kanal": "əvvəl", "Yeni kanal": "indi",
    "Əməliyyat": "əməliyyat", "Ləqəb": "ləqəb", "İstifadəçi adı": "ad", "Avatar": "avatar",
    "İstifadə limiti": "limit", "İstifadə sayı": "say", "Komanda": "komanda", "Xəta": "xəta", "İcraçı": "icraçı",
    "Dəyişikliklər": "dəyişiklik", "Silinən mesaj sayı": "silinən", "Mesajlar": "mesajlar", "Qeyd": "qeyd",
    "Əlavə edildi": "əlavə", "Silindi": "silindi", "Əvvəlki qoşmalar": "köhnə qoşma", "Yeni qoşmalar": "yeni qoşma",
    "Timeout əvvəl": "əvvəl", "Timeout sonra": "sonra",
}
_EMPTY_VALUES = {"", "—", "naməlum", "naməlum / vanity", "yoxdur"}
RULE = "┈" * 14


def _fmt_value(raw):
    """Pretty-print detail values: Discord timestamps, lists."""
    if isinstance(raw, (list, tuple)):
        items = [str(x) if not isinstance(x, (dict, list)) else json.dumps(x, ensure_ascii=False, default=str) for x in raw]
        return "\n".join(f"• {i}" for i in items) if items else "—"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False, default=str)
    value = str(raw).strip()
    if _ISO_RE.match(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = int(dt.timestamp())
            return f"<t:{ts}:d> · <t:{ts}:R>"
        except ValueError:
            pass
    return value or "—"


def _row(label, value):
    return f"`{label:<12}` {value}"


def format_audit_changes(changes):
    """Turn a discord.AuditLogChanges before/after pair into readable
    'sahə: köhnə → yeni' lines instead of the raw <AuditLogDiff ...> repr.
    Call this in bot.py before putting the value into details — the result
    is a plain string, so it still stores fine as JSON in the log archive.
    """
    if not changes:
        return ""
    before, after = getattr(changes, "before", None), getattr(changes, "after", None)
    try:
        before_map = dict(iter(before)) if before else {}
    except TypeError:
        before_map = {}
    try:
        after_map = dict(iter(after)) if after else {}
    except TypeError:
        after_map = {}
    keys = list(dict.fromkeys(list(before_map.keys()) + list(after_map.keys())))
    lines = []
    for key in keys:
        b, a = before_map.get(key), after_map.get(key)
        if b == a:
            continue
        label = str(key).replace("_", " ")
        b_text, a_text = _stringify_change_value(b), _stringify_change_value(a)
        if b_text == a_text:
            continue
        lines.append(f"{label}: {b_text} → {a_text}")
    return "\n".join(lines)[:2000]


def _stringify_change_value(value):
    """A role/channel LIST (e.g. roles=[<Role ...>]) str()'s to each item's
    repr, not its name — that's the '<Role id=... name=...>' junk seen in
    role-list changes. Render each item with str() instead so a role list
    shows plain names like 'Coffee Guest, Moderator'.
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)[:200] if value else "—"
    return str(value)[:120]


def make_embed(event_key, title, actor=None, target=None, channel=None, details=None,
               created_at=None, bot_avatar_url=None, guild_icon_url=None):
    info = LOG_EVENT_MAP.get(event_key, {"label": event_key, "group": "Sistem"})
    style = GROUP_STYLE.get(info["group"], {"color": NEUTRAL, "emoji": "✦"})
    if event_key in {"moderation_ban", "moderation_unban"}:
        style = GROUP_STYLE[LOG_EVENT_MAP["member_join"]["group"]]
    details = dict(details or {})

    actor_mention, actor_name = _mention(actor)
    target_mention, target_name = _mention(target)
    target_id, _ = _display(target)
    actor_is_obj = actor is not None and not isinstance(actor, str)

    rows = []
    if event_key in INVITE_EVENTS:
        # Əsas hədəf (kim qoşuldu / kim yaratdı)
        if target_name:
            rows.append(_row("üzv", target_mention))
        # DƏVƏT BLOKU — həmişə göstərilir: kim dəvət edib, hansı link, hansı kod
        inviter = actor_mention if actor_name else (details.get("Dəvət edən") or "")
        if str(inviter).strip().lower() in _EMPTY_VALUES:
            inviter = "müəyyən edilmədi"
        rows.append(_row("dəvət edən", inviter))
        link = str(details.get("Dəvət linki", "")).strip()
        code = str(details.get("Dəvət kodu", "")).strip()
        rows.append(_row("link", link if link.lower() not in _EMPTY_VALUES else "müəyyən edilmədi"))
        rows.append(_row("kod", f"`{code}`" if code.lower() not in _EMPTY_VALUES else "müəyyən edilmədi"))
        for k in INVITE_ROWS:
            details.pop(k, None)
        if channel is not None:
            rows.append(_row("kanal", getattr(channel, "mention", None) or getattr(channel, "name", None) or str(channel)))
    else:
        # Digər bütün hadisələr də giriş/dəvət logunun eyni prinsipi ilə işləyir:
        # mənalı etiket + sətir həmişə görünür, məlumat yoxdursa yer tutucu yazılır.
        role_labels = EVENT_ROLE_LABELS.get(event_key, DEFAULT_ROLE_LABELS)
        actor_label = role_labels.get("actor")
        target_label = role_labels.get("target")
        target_kind = role_labels.get("target_kind", "user")

        if actor_label:
            if actor_name:
                actor_value = actor_mention
            else:
                # Audit Log icazəsi olmadıqda details-ə yazılan "İcraçı" qeydini
                # əsas sətrə köçürürük ki, iki dəfə görünməsin (dəvət edən sətri kimi).
                fallback = str(details.pop("İcraçı", "") or "").strip()
                actor_value = fallback if fallback and fallback.lower() not in _EMPTY_VALUES else NOT_SET
            rows.append(_row(actor_label, actor_value))

        if target_label:
            if target_name:
                target_value = _kind_mention(target, target_id, target_kind) or target_name
            else:
                target_value = NOT_SET
            rows.append(_row(target_label, target_value))

        if channel is not None:
            rows.append(_row("kanal", getattr(channel, "mention", None) or getattr(channel, "name", None) or str(channel)))

    # Qısa dəyərlər sətir kimi, uzunlar ayrıca sahə kimi
    long_fields = []
    for key, raw in details.items():
        if str(key).strip().casefold() in {"dəyişiklik", "dəyişikliklər"}:
            continue
        if event_key == "message_bulk_delete" and key == "Mesajlar" and isinstance(raw, (list, tuple)):
            for position, message in enumerate(raw, 1):
                if not isinstance(message, dict):
                    continue
                index = message.get("_index", position)
                author = str(message.get("müəllif") or "Müəllif məlum deyil")
                message_id = str(message.get("id") or "")
                content = str(message.get("mətn") or "(boş mətn)").strip()
                attachments = message.get("qoşmalar") or []
                if attachments:
                    content += "\n\nQoşmalar:\n" + "\n".join(str(item) for item in attachments)
                if len(content) > 900:
                    content = content[:897].rstrip() + "..."
                message_label = f"{int(index):02d} · {author}"
                if message_id:
                    message_label += f" · ID {message_id}"
                long_fields.append((message_label[:256], content or "(boş mətn)"))
            continue
        value = _fmt_value(raw)
        if value.strip().lower() in _EMPTY_VALUES:
            continue
        label = ROW_LABELS.get(str(key), str(key).lower())
        if len(value) <= 70 and "\n" not in value and not value.startswith("•") and len(label) <= 12:
            rows.append(_row(label, value))
        else:
            long_fields.append((label, value))

    body = [f"{style['emoji']} **{title}**", RULE]
    body.extend(rows)
    embed = discord.Embed(description="\n".join(body)[:4000], color=style["color"],
                          timestamp=created_at or datetime.now(timezone.utc))
    # Böyük başlıq onsuz da hadisəni tam yazır (məs. "Rol silindi"); yuxarıdakı
    # kiçik yazını eyni mətnin təkrarı etmək əvəzinə qrup adını göstəririk.
    embed.set_author(name=info["group"], icon_url=guild_icon_url or DEFAULT_AVATAR)

    thumb = _avatar_url(target) or _avatar_url(actor)
    if thumb:
        embed.set_thumbnail(url=thumb)

    remaining = max(0, 5400 - len(embed.description or ""))
    for label, value in long_fields:
        if value.startswith("•"):
            shown = value
        elif event_key == "message_bulk_delete":
            shown = value
        elif len(value) > 120 or "\n" in value:
            shown = f"```{value[:900]}```"
        else:
            shown = "\n".join(f"> {ln}" for ln in value.splitlines())
        limit = min(1024, remaining - len(label))
        if limit <= 0 or len(embed.fields) >= 20:
            break
        shown = shown[:limit]
        embed.add_field(name=label, value=shown, inline=False)
        remaining -= len(label) + len(shown)

    footer = info["group"].lower()
    if target_id:
        footer = f"{footer}  ·  id {target_id}"
    embed.set_footer(text=footer, icon_url=bot_avatar_url or DEFAULT_AVATAR)
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
        bot_user = getattr(bot, "user", None)
        bot_avatar_url = _avatar_url(bot_user)
        guild_icon_url = _guild_icon_url(guild)
        if event_key == "message_bulk_delete" and isinstance((details or {}).get("Mesajlar"), list):
            messages = [dict(message, _index=index) for index, message in enumerate(details["Mesajlar"], 1)]
            # Pack up to ten short message fields per post, while keeping each
            # embed comfortably below Discord's aggregate 6000-character limit.
            batches = []
            batch = []
            batch_chars = 0
            for message in messages:
                text = str(message.get("mətn") or "(boş mətn)").strip()
                attachments = message.get("qoşmalar") or []
                if attachments:
                    text += "\n\nQoşmalar:\n" + "\n".join(str(item) for item in attachments)
                author = str(message.get("müəllif") or "Müəllif məlum deyil")
                field_cost = min(900, len(text)) + min(256, len(author) + 8)
                if batch and (len(batch) >= 10 or batch_chars + field_cost > 4500):
                    batches.append(batch)
                    batch = []
                    batch_chars = 0
                batch.append(message)
                batch_chars += field_cost
            if batch:
                batches.append(batch)
            if not batches:
                batches = [[]]
            for page_number, batch in enumerate(batches, 1):
                start = batch[0]["_index"] if batch else 0
                end = start + len(batch) - 1
                page_details = dict(details)
                page_details["Mesajlar"] = batch
                page_title = title if len(batches) == 1 else f"{title} · {start}–{end}/{len(messages)}"
                embed = make_embed(event_key, page_title, actor, target, channel, page_details, created_at,
                                   bot_avatar_url=bot_avatar_url, guild_icon_url=guild_icon_url)
                await destination.send(embed=embed)
        else:
            embed = make_embed(event_key, title, actor, target, channel, details, created_at,
                               bot_avatar_url=bot_avatar_url, guild_icon_url=guild_icon_url)
            await destination.send(embed=embed)
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
