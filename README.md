# Abi Discord Bot

A feature-rich Discord bot built with `discord.py` for voice activity tracking, XP/level progression, moderation, anti-spam protection, and utility commands.

## ✨ Features

- 🎙️ Voice activity tracking (total/daily/weekly/monthly)
- 📊 Leaderboards and periodic reports
- ⭐ XP + level system for voice participation
- 🛡️ Moderation commands (`warn`, `warnings`, `temizle`, `mute`, `unmute`, `kick`, `ban`, `unban`)
- 📜 Audit & Mod Log system (message delete/edit, voice events, moderation actions, member join/leave)
- 🚫 Anti-link and anti-spam protection
- 🧰 Utility commands (`userinfo`, `serverinfo`, `avatar`, `poll`)
- 💾 SQLite persistence (`users`, `sessions`, `warnings`)

## 📁 Project Structure

```text
.
├─ bot.py
├─ database.py
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
REPORT_CHANNEL_ID=123456789012345678
LEVEL_UP_CHANNEL_ID=123456789012345678
MOD_LOG_CHANNEL_ID=123456789012345678
```

## 🚀 Run the Bot

```bash
python bot.py
```

## 🧪 Quick Check (Optional)

```bash
python -m py_compile bot.py database.py
```

## 🛠️ Main Commands

### Stats & XP
- `abi profil [@user]`
- `abi top [number]`
- `abi hesabat [gun/hefte/ay]`
- `abi seviyye [@user]`
- `abi xptop [number]`

### Moderation
- `abi warn @user [reason]`
- `abi warnings [@user]`
- `abi temizle [count]`
- `abi mute @user [minutes] [reason]`
- `abi unmute @user`
- `abi kick @user [reason]`
- `abi ban @user [reason]`
- `abi unban [userID] [reason]`

### Utility
- `abi userinfo [@user]`
- `abi serverinfo`
- `abi avatar [@user]`
- `abi poll Sual | Variant 1 | Variant 2 | ...`

### Help
- `abi komandalar`

## 🔒 Security Notes

- Never commit real secrets in `.env`
- `.env` and `voice_stats.db` are ignored via `.gitignore`
- If keys were exposed before, rotate them immediately

## 📄 License

Use, modify, and adapt for your own server needs.
