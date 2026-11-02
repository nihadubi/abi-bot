# Abi Discord Bot

Discord bot for moderation, voice event logging, and an authenticated web admin panel with post scheduling and server controls.

## Requirements

- Python 3.10 or newer
- A Discord bot token, with Message Content and Server Members intents enabled in the Discord Developer Portal when using features that need them

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in at least `TOKEN` and a strong `ADMIN_PASSWORD`. `GEMINI_API_KEY` is optional and enables image descriptions. The web server binds to `HOST=0.0.0.0` and `PORT=8080` by default so managed hosts such as Render can reach it; Render supplies `PORT` automatically. For a reverse proxy on the same host, set `HOST=127.0.0.1`; set `COOKIE_SECURE=true` when serving the panel over HTTPS and set `TRUSTED_PROXY_CIDRS` only to the proxy's IP/CIDR.

On Render, SQLite data is lost on deploys unless the service has a persistent disk. Attach a disk mounted at `/var/data` and set `DATABASE_PATH=/var/data/abi_bot.db` in the service's environment. Without a persistent disk, log-channel selections, log history, and other database state reset when Render replaces the instance.

For a Render Free service, server settings can be kept in Upstash Redis instead: create a free Redis database and set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` from its HTTPS REST credentials in Render's environment. The bot restores log channels, auto-role, welcome-message settings, and log appearance templates on startup. The SQLite log archive, moderation records, and other database data still need persistent storage.

Never commit `.env` or the SQLite database. If a token or key has been exposed, rotate it.

## Run

```bash
python bot.py
```

The admin panel is available at `http://localhost:8080/admin` on the same machine. If the bot runs on a remote host, put it behind HTTPS with a reverse proxy and restrict direct access to its port. The panel provides channel posting, scheduled posts, post history, moderation controls, and per-server event logging. Discord permissions and role hierarchy still apply.

### Event logging setup

In the Discord Developer Portal, enable the privileged **Server Members Intent** and **Message Content Intent**. Voice-state, moderation, and invite gateway intents are enabled in the bot code. Give the bot **Manage Server** to read invite usage, **Manage Channels** for invite-created/deleted gateway events, and **View Audit Log** to attribute moderator actions. In the admin panel's **Loglar** tab, choose a server, enable each event, and optionally choose its destination channel. If no channel is selected, enabled events are stored only in the guild-scoped SQLite archive. Message text is retained in that archive; attachment files are not copied, but their Discord links and filenames are logged.

When deploying behind Nginx, proxy to the host's listening address and port, for example:

```nginx
server {
    server_name panel.example.com;
    client_max_body_size 64M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Legal pages

The bot serves `/terms` and `/privacy` for the corresponding Discord Developer Portal URLs.
