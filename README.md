# Abi Discord Bot

A feature-rich Discord bot built with `discord.py` for voice activity tracking, XP/level progression, moderation, anti-spam protection, and utility commands.

## ✨ Features

- 🎙️ Voice activity tracking (total/daily/weekly/monthly)
- 📊 Leaderboards & Həftəlik Səs Aktivliyi Qrafiki (Matplotlib)
- ⭐ XP + level system with visual Rank Card (Pillow)
- 🔥 Voice Streak (Gündəlik Səs Seriyası) — günlük bonus XP
- 🚪 TempVoice — Join to Create şəxsi səs otaqları (ad, limit, kilid, kick, devret, lider)
- 👋 Welcome Image & Auto-Role — vizual xoşgəldin kartı + avtomatik rol
- 🛡️ Moderation commands (`warn`, `warnings`, `sil`, `mute`, `unmute`, `kick`, `ban`, `unban`)
- 🚫 Anti-link and anti-spam protection
- 🧰 Utility commands (`userinfo`, `serverinfo`, `avatar`, `poll`)
- 💾 SQLite persistence (`users`, `sessions`, `warnings`, `temp_channels`, `guild_settings`)

## 📁 Project Structure

```text
.
├─ bot.py          # Botun əsas faylı (komandalar, event-lər)
├─ database.py     # SQLite verilənlər bazası əməliyyatları
├─ graphics.py     # Pillow (Rank/Welcome kartları) & Matplotlib (Aktivlik qrafiki)
├─ requirements.txt
├─ .env
├─ .env.example
├─ .gitignore
├─ privacy.html
└─ terms.html
```

## ⚙️ Requirements

- Python 3.10+
- Discord bot token

Install dependencies:

```bash
pip install -r requirements.txt
```

## 🔐 Environment Variables

Create a `.env` file in the project root:

```env
TOKEN=your_discord_bot_token
GUILD_ID=123456789012345678
LEVEL_UP_CHANNEL_ID=123456789012345678
```

## 🚀 Run the Bot

```bash
python bot.py
```

## 📄 Terms and Privacy URLs

When deployed on Render, use these URLs in the Discord Developer Portal:

```text
https://your-render-service.onrender.com/terms
https://your-render-service.onrender.com/privacy
```

## 🧪 Quick Check (Optional)

```bash
python -m py_compile bot.py database.py graphics.py
```

## 🛠️ Main Commands

### Stats & XP
- `abi profil [@user]` — Səs aktivliyi və sıralama profili
- `abi top [number]` — Ən çox səsdə qalanların lider cədvəli
- `abi qrafik [@user]` — Həftəlik səs aktivliyi diaqramı
- `abi seviyye [@user]` — Vizual Rank kartı (Level, XP, Progress)
- `abi xptop [number]` — XP lider cədvəli
- `abi hesabat [gun/hefte/ay]` — Periodik səs hesabatı

### Voice Streak (Seriya)
- `abi streak [@user]` — Gündəlik səs seriyası
- `abi streaktop [say]` — Ən yüksək seriyaya sahib üzvlər

### TempVoice (Şəxsi Səs Otağı)
- `abi ses ad [ad]` — Otağın adını dəyişir
- `abi ses limit [say]` — İstifadəçi limiti (0 = limitsiz)
- `abi ses kilid / ac` — Otağı kilidləyir / açır
- `abi ses at @user` — İstifadəçini otaqdan atır
- `abi ses devret @user` — Sahibliyi verir
- `abi ses lider` — Otaq rəhbərliyini ələ alır

### Moderation
- `abi warn @user [reason]`
- `abi warnings [@user]`
- `abi warnlar [count]`
- `abi sil [count]`
- `abi mute @user [minutes] [reason]`
- `abi unmute @user`
- `abi kick @user [reason]`
- `abi ban @user [reason]`
- `abi unban [userID] [reason]`

### Admin Setup
- `abi settempvoice #kanal` — TempVoice generator kanalını təyin edir
- `abi setwelcome #kanal` — Xoşgəldin kartı kanalını təyin edir
- `abi setautorole @rol` — Yeni üzvlərə avtomatik rol
- `abi setlevelup #kanal` — Level-Up bildiriş kanalı

### 🧠 Süni İntellekt (AI Chat)
- Bota mention ataraq (`@abi-bot salam necəsən?`), cavab (reply) verərək və ya birbaşa `abi <sual>` yazaraq istənilən kanalda canlı söhbət
- `/sorus <sual>` — Süni İntellektə Slash komandası ilə sual vermək
- Dialoq konteksti və yaddaş sistemi

### Utility & Help
- `abi userinfo [@user]`
- `abi serverinfo`
- `abi avatar [@user]`
- `abi poll Sual | Variant 1 | Variant 2 | ...`
- `abi komandalar`
- `abi adminkomandalar [əmr]`

## 🌐 Web Admin Paneli

Bota daxil edilmiş veb idarəetmə paneli vasitəsilə NSFW (18+) mətn kanallarına müasir Discord Embed formatında şəkil və keçid linkləri olan postlar paylaşa, onları canlı önbaxışda (Live Preview) izləyə və planlaşdıra (Schedule) bilərsiniz.

### Panel İmkanları
- 🔒 **Təhlükəsiz Giriş**: Tək-admin şifrəli giriş (`ADMIN_PASSWORD`), Rate-limiting (5 səhv cəhddən sonra bloklanma), `HttpOnly` + `SameSite` sessiya kukiləri və CSRF qorunması.
- 🔞 **Yalnız NSFW Kanallar**: Kanal siyahısı yalnız Discord-da `is_nsfw()` olan mətn kanallarını göstərir və arxa planda göndərilmə anında təkrar yoxlanılır.
- 🖼️ **Çoxsaylı Şəkillər (10-dək)**: Həm fayl yükləmə (upload), həm də birbaşa URL ilə şəkil dəstəyi (PNG, JPG, WEBP, GIF). SSRF hücumlarına qarşı daxili IP/localhost filtri.
- 📐 **Avtomatik Həcm & Sıxılma**: Discord serverinin real yükləmə limitinin 90%-i effektiv limit kimi qəbul edilir (`MAX_UPLOAD_MB` ilə tənzimlənə bilir). Şəkillər limiti aşdıqda Pillow ilə keyfiyyət və ölçü proporsional sıxılır.
- 👁️ **Canlı Discord Önbaxışı**: Formu doldurarkən sağ tərəfdə mesajın Discord-da necə görünəcəyi canlı əks olunur.
- 🕒 **Planlaşdırma (Scheduler)**: Post üçün vaxt təyin edildikdə SQLite bazasında saxlanılır və bot yenidən başlasa belə vaxtı çatanda avtomatik göndərilir.
- 📜 **Tarixçə & Silmə**: Paylaşılan postlar tarixçədə saxlanılır; "Sil" düyməsi ilə həm bazadan, həm də Discord kanalından mesaj dərhal silinir.

### Paneli İşə Salma & Giriş
1. Botu işə saldıqdan sonra brauzerdə daxil olun:
   ```
   http://localhost:8080/admin
   ```
2. Giriş səhifəsində `.env` faylında təyin etdiyiniz `ADMIN_PASSWORD` şifrəsini yazın (defolt: `admin123`).

### Təhlükəsiz Hostinq (Nginx Reverse Proxy & HTTPS)
Admin panelini internet üzərindən təhlükəsiz idarə etmək üçün Nginx arxasında HTTPS (SSL) ilə işlətmək tövsiyə olunur:
```nginx
server {
    server_name panel.domaininiz.com;

    client_max_body_size 64M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    listen 443 ssl;
    # ssl_certificate /etc/letsencrypt/live/.../fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;
}
```

## 🔒 Security Notes

- Never commit real secrets in `.env`
- `.env` and `voice_stats.db` are ignored via `.gitignore`
- If keys were exposed before, rotate them immediately

## 📄 License

Use, modify, and adapt for your own server needs.

## Local Admin Panel

Open `http://localhost:8080/admin` while the bot is running. The panel listens on localhost only and includes the existing post tools plus member warnings, warning history, timeouts, kick, ban, and unban controls. Discord bot permissions and role hierarchy still apply.

