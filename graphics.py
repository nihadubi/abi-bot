import io
import math
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker


def _get_font(size: int, bold: bool = False):
    """Sistemdəki mövcud şriftləri sınaqdan keçirir və ya standart şrifti qaytarır."""
    font_names = [
        "segoeui.ttf" if not bold else "segoeuib.ttf",
        "arial.ttf" if not bold else "arialbd.ttf",
        "tahoma.ttf" if not bold else "tahomabd.ttf",
        "DejaVuSans.ttf" if not bold else "DejaVuSans-Bold.ttf",
    ]
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def generate_rank_card(
    avatar_bytes: bytes,
    username: str,
    display_name: str,
    level: int,
    xp: int,
    current_level_xp: int,
    next_level_xp: int,
    rank_position: int,
    streak: int = 0,
) -> io.BytesIO:
    """Müasir, tünd dizaynlı, qradiyentli və parlaq Rank/Profil kartı yaradır."""
    width, height = 900, 270
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    # 1. Arxa fon (Gradient)
    bg = Image.new("RGBA", (width, height), (15, 18, 26, 255))
    bg_draw = ImageDraw.Draw(bg)

    # Qradiyent effekti
    for y in range(height):
        ratio = y / height
        r = int(18 + ratio * 15)
        g = int(22 + ratio * 12)
        b = int(34 + ratio * 20)
        bg_draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # Dekorativ neon vurğular (Arxa fonda zərif işıq dairələri)
    accent = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    accent_draw = ImageDraw.Draw(accent)
    accent_draw.ellipse([(width - 250, -100), (width + 100, 250)], fill=(88, 101, 242, 45))
    accent_draw.ellipse([(100, height - 150), (450, height + 150)], fill=(0, 210, 255, 30))
    accent = accent.filter(ImageFilter.GaussianBlur(35))
    bg = Image.alpha_composite(bg, accent)

    # Kartın yuvarlaq kənarları üçün maska
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([(0, 0), (width, height)], radius=24, fill=255)
    card.paste(bg, (0, 0), mask)

    # 2. Xarici zərif parlaq haşiyə (Border)
    draw.rounded_rectangle([(1, 1), (width - 2, height - 2)], radius=24, outline=(88, 101, 242, 120), width=2)

    # 3. Avatarın yerləşdirilməsi
    avatar_size = 170
    avatar_x, avatar_y = 40, (height - avatar_size) // 2

    if avatar_bytes:
        try:
            avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        except Exception:
            avatar_img = Image.new("RGBA", (avatar_size, avatar_size), (88, 101, 242, 255))
    else:
        avatar_img = Image.new("RGBA", (avatar_size, avatar_size), (88, 101, 242, 255))

    # Dairəvi avatar maskası
    avatar_mask = Image.new("L", (avatar_size, avatar_size), 0)
    avatar_mask_draw = ImageDraw.Draw(avatar_mask)
    avatar_mask_draw.ellipse([(0, 0), (avatar_size, avatar_size)], fill=255)

    # Avatar ətrafı parlaq halqa
    ring_pad = 6
    draw.ellipse(
        [(avatar_x - ring_pad, avatar_y - ring_pad), (avatar_x + avatar_size + ring_pad, avatar_y + avatar_size + ring_pad)],
        outline=(87, 242, 135, 230),
        width=4,
    )
    card.paste(avatar_img, (avatar_x, avatar_y), avatar_mask)

    # 4. Mətnlər və Məlumatlar
    text_x = avatar_x + avatar_size + 40

    font_name = _get_font(32, bold=True)
    font_tag = _get_font(20, bold=False)
    font_badge = _get_font(18, bold=True)
    font_xp = _get_font(18, bold=True)

    # Display name və tag
    display_title = display_name if len(display_name) <= 18 else f"{display_name[:17]}…"
    draw.text((text_x, 42), display_title, font=font_name, fill=(255, 255, 255, 255))

    user_tag = f"@{username}" if len(username) <= 20 else f"@{username[:19]}…"
    draw.text((text_x, 82), user_tag, font=font_tag, fill=(160, 170, 195, 255))

    # Badgelər: Rank # və Level
    badge_y = 42
    # Rank Badge (Sağ tərəfdə)
    rank_text = f"#{rank_position}" if rank_position > 0 else "#-"
    level_text = f"LVL {level}"

    # Sağ tərəfdə Level və Rank
    draw.text((width - 50, badge_y), level_text, font=_get_font(30, bold=True), fill=(88, 101, 242, 255), anchor="ra")
    draw.text((width - 190, badge_y + 2), f"RANK {rank_text}", font=_get_font(24, bold=True), fill=(255, 215, 0, 255), anchor="ra")

    # Əgər streak varsa, alov ikonlu badge göstər
    if streak > 0:
        streak_str = f"🔥 {streak} GÜN SERİYA"
        streak_w = 160
        streak_box = [(width - 50 - streak_w, badge_y + 42), (width - 50, badge_y + 72)]
        draw.rounded_rectangle(streak_box, radius=8, fill=(240, 71, 71, 50), outline=(255, 90, 95, 180), width=1)
        draw.text((width - 50 - streak_w // 2, badge_y + 57), streak_str, font=_get_font(14, bold=True), fill=(255, 140, 140, 255), anchor="mm")

    # 5. Progress Bar (Tərəqqi Çubuğu)
    bar_x = text_x
    bar_y = 165
    bar_w = width - text_x - 50
    bar_h = 24

    range_xp = max(next_level_xp - current_level_xp, 1)
    current_progress = max(0, min(xp - current_level_xp, range_xp))
    progress_ratio = max(0.0, min(1.0, current_progress / range_xp))
    progress_pct = int(progress_ratio * 100)

    # Arxa fon çubuğu
    draw.rounded_rectangle([(bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h)], radius=12, fill=(35, 40, 58, 255))

    # Doldurulmuş qradiyent çubuq
    filled_w = int(bar_w * progress_ratio)
    if filled_w > 12:
        fill_img = Image.new("RGBA", (filled_w, bar_h), (0, 0, 0, 0))
        fill_draw = ImageDraw.Draw(fill_img)
        for fx in range(filled_w):
            fratio = fx / max(bar_w, 1)
            cr = int(88 - fratio * 60)
            cg = int(101 + fratio * 120)
            cb = int(242 + fratio * 13)
            fill_draw.line([(fx, 0), (fx, bar_h)], fill=(max(0, cr), min(255, cg), min(255, cb), 255))

        fill_mask = Image.new("L", (filled_w, bar_h), 0)
        fill_mask_draw = ImageDraw.Draw(fill_mask)
        fill_mask_draw.rounded_rectangle([(0, 0), (filled_w, bar_h)], radius=12, fill=255)
        card.paste(fill_img, (bar_x, bar_y), fill_mask)

    # Progress mətni (Barın altında)
    xp_text = f"{current_progress:,} / {range_xp:,} XP ({progress_pct}%)"
    draw.text((bar_x, bar_y + bar_h + 14), "Növbəti səviyyəyə tərəqqi", font=_get_font(16, bold=False), fill=(140, 150, 175, 255))
    draw.text((bar_x + bar_w, bar_y + bar_h + 14), xp_text, font=font_xp, fill=(87, 242, 135, 255), anchor="ra")

    output = io.BytesIO()
    card.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def generate_welcome_card(
    avatar_bytes: bytes,
    username: str,
    guild_name: str,
    member_count: int,
) -> io.BytesIO:
    """Yeni qatılan üzvlər üçün şık, vizual Welcome şəkli yaradır."""
    width, height = 900, 320
    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    # Arxa fon
    bg = Image.new("RGBA", (width, height), (14, 17, 24, 255))
    bg_draw = ImageDraw.Draw(bg)
    for y in range(height):
        ratio = y / height
        r = int(14 + ratio * 20)
        g = int(18 + ratio * 15)
        b = int(28 + ratio * 35)
        bg_draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # Neon işıq effektləri
    accent = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    accent_draw = ImageDraw.Draw(accent)
    accent_draw.ellipse([(width // 2 - 150, -80), (width // 2 + 150, 180)], fill=(88, 101, 242, 60))
    accent_draw.ellipse([(width - 200, height - 100), (width + 100, height + 100)], fill=(87, 242, 135, 40))
    accent = accent.filter(ImageFilter.GaussianBlur(40))
    bg = Image.alpha_composite(bg, accent)

    # Dairəvi kənarlar
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([(0, 0), (width, height)], radius=24, fill=255)
    card.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([(1, 1), (width - 2, height - 2)], radius=24, outline=(88, 101, 242, 140), width=2)

    # Mərkəzləşdirilmiş dairəvi Avatar
    avatar_size = 120
    avatar_x = (width - avatar_size) // 2
    avatar_y = 30

    if avatar_bytes:
        try:
            avatar_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar_img = avatar_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
        except Exception:
            avatar_img = Image.new("RGBA", (avatar_size, avatar_size), (88, 101, 242, 255))
    else:
        avatar_img = Image.new("RGBA", (avatar_size, avatar_size), (88, 101, 242, 255))

    avatar_mask = Image.new("L", (avatar_size, avatar_size), 0)
    avatar_mask_draw = ImageDraw.Draw(avatar_mask)
    avatar_mask_draw.ellipse([(0, 0), (avatar_size, avatar_size)], fill=255)

    # Parlaq xarici halqa
    draw.ellipse(
        [(avatar_x - 5, avatar_y - 5), (avatar_x + avatar_size + 5, avatar_y + avatar_size + 5)],
        outline=(88, 101, 242, 255),
        width=4,
    )
    card.paste(avatar_img, (avatar_x, avatar_y), avatar_mask)

    # Mətnlər
    draw.text((width // 2, 165), "XOŞ GƏLDİN!", font=_get_font(22, bold=True), fill=(87, 242, 135, 255), anchor="mm")
    
    clean_name = username if len(username) <= 24 else f"{username[:23]}…"
    draw.text((width // 2, 205), clean_name, font=_get_font(30, bold=True), fill=(255, 255, 255, 255), anchor="mm")

    guild_label = f"{guild_name} serverinə qatıldı" if len(guild_name) <= 30 else f"{guild_name[:29]}… serverinə qatıldı"
    draw.text((width // 2, 245), guild_label, font=_get_font(18, bold=False), fill=(160, 175, 205, 255), anchor="mm")

    # Üzv Sayı Badge
    badge_text = f"🎉 #{member_count:,}-ci Üzv"
    badge_w = 200
    badge_box = [(width // 2 - badge_w // 2, 272), (width // 2 + badge_w // 2, 302)]
    draw.rounded_rectangle(badge_box, radius=10, fill=(88, 101, 242, 45), outline=(88, 101, 242, 180), width=1)
    draw.text((width // 2, 287), badge_text, font=_get_font(16, bold=True), fill=(255, 215, 0, 255), anchor="mm")

    output = io.BytesIO()
    card.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def generate_voice_chart(history_data: list[dict], display_name: str) -> io.BytesIO:
    """Matplotlib ilə istifadəçinin son 7 günlük səs aktivliyi qrafikini çəkir."""
    labels = [d["day_label"] for d in history_data]
    seconds = [d["seconds"] for d in history_data]
    # Dəqiqəyə çeviririk
    minutes = [round(s / 60, 1) for s in seconds]

    fig, ax = plt.subplots(figsize=(9.5, 5.2), dpi=140)
    fig.patch.set_facecolor("#11131a")
    ax.set_facecolor("#181b26")

    # Bar rəngləri və dizaynı
    bars = ax.bar(
        range(len(labels)),
        minutes,
        width=0.55,
        color="#5865F2",
        edgecolor="#7983F5",
        linewidth=1.2,
        zorder=3,
    )

    # Ən yüksək aktivlik olan günü fərqli rənglə vurğulayırıq
    if any(m > 0 for m in minutes):
        max_idx = minutes.index(max(minutes))
        bars[max_idx].set_color("#57F287")
        bars[max_idx].set_edgecolor("#82F7A7")

    # Bar üzərində dəyər yazıları
    for idx, (bar, sec) in enumerate(zip(bars, seconds)):
        h = bar.get_height()
        if sec <= 0:
            text = "0 dəq"
        elif sec < 3600:
            text = f"{sec // 60} dəq"
        else:
            hrs = sec // 3600
            rem_m = (sec % 3600) // 60
            text = f"{hrs}s {rem_m}d" if rem_m > 0 else f"{hrs} saat"

        ax.annotate(
            text,
            xy=(bar.get_x() + bar.get_width() / 2, h),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            color="#FFFFFF",
            zorder=4,
        )

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, color="#A0ABC0", fontsize=11, fontweight="bold")
    ax.tick_params(axis="y", colors="#7E8BA5", labelsize=10)
    ax.tick_params(axis="x", colors="#7E8BA5", length=0)

    # Qrid xətləri
    ax.grid(axis="y", linestyle="--", alpha=0.18, color="#FFFFFF", zorder=0)

    # Kənar haşiyələri təmizləyirik
    for spine in ax.spines.values():
        spine.set_color("#2C3246")
        spine.set_linewidth(1)

    # Başlıq və alt başlıq
    total_sec = sum(seconds)
    if total_sec < 3600:
        tot_str = f"{total_sec // 60} dəqiqə"
    else:
        tot_hrs = total_sec // 3600
        tot_rem = (total_sec % 3600) // 60
        tot_str = f"{tot_hrs} saat {tot_rem} dəq" if tot_rem > 0 else f"{tot_hrs} saat"

    plt.title(
        f"{display_name} — Həftəlik Səs Aktivliyi\nSon 7 günün cəmi: {tot_str}",
        color="#FFFFFF",
        fontsize=14,
        fontweight="bold",
        pad=18,
        loc="center",
    )
    plt.ylabel("Aktivlik (Dəqiqə)", color="#7E8BA5", fontsize=11, labelpad=10)

    # Y-limit üçün bir az yuxarı boşluq
    max_m = max(minutes) if minutes else 0
    ax.set_ylim(0, max(max_m * 1.25, 30))

    plt.tight_layout()

    output = io.BytesIO()
    plt.savefig(output, format="PNG", facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    output.seek(0)
    return output
