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


### Utility & Help
- `abi userinfo [@user]`
- `abi serverinfo`
- `abi avatar [@user]`
- `abi poll Sual | Variant 1 | Variant 2 | ...`
- `abi komandalar`
- `abi adminkomandalar [əmr]`

## 🔒 Security Notes

- Never commit real secrets in `.env`
- `.env` and `voice_stats.db` are ignored via `.gitignore`
- If keys were exposed before, rotate them immediately

## 📄 License

Use, modify, and adapt for your own server needs.
