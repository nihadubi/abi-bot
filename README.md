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

### Deploying on Render with Upstash

Render Free web services have an ephemeral filesystem: local SQLite data is removed when the service restarts, deploys, or spins down. Free web services cannot use Render persistent disks. Upstash Redis is used by this project to preserve **server settings** across those events; it does not store the SQLite log archive, moderation records, voice statistics, scheduled-post history, or uploaded post images.

#### 1. Create the Render web service

1. Push this project to a Git repository and create a **Web Service** in Render connected to that repository.
2. Set the build command to `pip install -r requirements.txt` and the start command to `python bot.py`.
3. Render supplies `PORT` automatically. Leave `HOST` unset (the app defaults to `0.0.0.0`) and do not set a fixed port in Render.

#### 2. Create Upstash Redis

1. Create a Redis database in the Upstash console.
2. Open the database details and copy its **REST URL** and **REST token**. Use the REST credentials, not the Redis TCP connection string or general Upstash account API key.
3. In Render, open the web service's **Environment** settings and add these variables:

   | Key | Value |
   | --- | --- |
   | `UPSTASH_REDIS_REST_URL` | The database's HTTPS REST URL |
   | `UPSTASH_REDIS_REST_TOKEN` | The database's REST token |

   Paste the values as provided; do not add quote characters. Keep the token private and do not commit it to `.env.example` or source control.

The bot copies log-channel selections and member settings (auto-role, welcome text, and welcome role-button configuration) to Upstash when saved, then restores them on startup. Both Upstash variables must be set together. If they are missing, settings are stored only in the local SQLite database and will be lost when Render replaces its filesystem. If the credentials are invalid or Upstash cannot be reached during startup, database initialization fails and the bot will not start.

#### 3. Add the other required environment variables

In the same Render Environment page, set `TOKEN` to the Discord bot token and `ADMIN_PASSWORD` to a long, unique password. You can add optional variables such as `GEMINI_API_KEY` there too. Do not upload your local `.env` file; enter its values in Render's Environment settings. Trigger a deploy after saving environment changes.

#### 4. Understand what persists

| Data | Upstash Redis | Render Free local SQLite |
| --- | --- | --- |
| Log channel routing | Yes | Temporary local copy |
| Auto-role and welcome/button settings | Yes | Temporary local copy |
| Discord log archive | No | Lost on restart/deploy/spin-down |
| Moderation records and voice statistics | No | Lost on restart/deploy/spin-down |
| Scheduled-post history and uploaded images | No | Lost on restart/deploy/spin-down |

If you need those SQLite records to survive, use a persistent database/service and migrate the application's storage to it, or run the bot on a host with persistent storage. Setting `DATABASE_PATH` to a Render disk path does **not** make it persistent on the Free plan because Free web services cannot attach disks.

Render Free web services can spin down after 15 minutes without inbound HTTP/WebSocket traffic and can restart at other times. The Discord gateway connection by itself does not guarantee that the service stays awake, so the Free plan does not guarantee continuous 24/7 bot availability. See [Render's Free instance limitations](https://render.com/docs/free) and [Upstash REST credentials](https://upstash.com/docs/redis).

Never commit `.env` or the SQLite database. If a token or key has been exposed, rotate it.

## Run

```bash
python bot.py
```

The admin panel is available at `http://localhost:8080/admin` on the same machine. For a deployed service, open its Render URL followed by `/admin` (for example, `https://your-service.onrender.com/admin`). The panel provides channel posting, scheduled posts, post history, moderation controls, and per-server event logging. Discord permissions and role hierarchy still apply.

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
