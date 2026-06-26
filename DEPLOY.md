# Deployment Guide

Steps to get manabot running on a Linux server with automatic daily price updates.

---

## Prerequisites

```bash
# Python 3.11+ (Ubuntu/Debian)
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Verify
python3.11 --version
```

---

## 1. Clone and install

```bash
# Pick a home for the bot (adjust path as needed)
cd /opt
sudo git clone <repo-url> manabot
sudo chown -R $USER:$USER /opt/manabot
cd /opt/manabot

# Create a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install with scheduler + Discord extras
pip install -e ".[full]"
```

---

## 2. Configure credentials

```bash
cp .env.example .env
nano .env   # or vim, etc.
```

Set at minimum:

```
MANAPOOL_EMAIL=you@example.com
MANAPOOL_TOKEN=your-access-token
```

For Discord bot support also add `DISCORD_BOT_TOKEN` and optionally `DISCORD_GUILD_ID`.

---

## 3. Initialize the database and warm up caches

```bash
source .venv/bin/activate

# Create the data directory
mkdir -p data

# Smoke test — fetch live inventory and preview prices (no writes)
python -m manabot price-update --dry-run
```

This also downloads the ManaPool catalog cache (`data/manapool_catalog.json.gz`) and TCGTracking data into `data/tcgtracking/`. The first run takes a few minutes; subsequent runs use the disk cache.

---

## 4. Set up the price-update scheduler as a systemd service

Create the service file:

```bash
sudo nano /etc/systemd/system/manabot-pricer.service
```

Paste (update `User` and `WorkingDirectory` if you used a different path):

```ini
[Unit]
Description=manabot daily price update scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/manabot
EnvironmentFile=/opt/manabot/.env
ExecStart=/opt/manabot/.venv/bin/python -m manabot pricer-scheduler
Restart=on-failure
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable manabot-pricer
sudo systemctl start manabot-pricer

# Verify it's running
sudo systemctl status manabot-pricer

# Watch live logs
sudo journalctl -u manabot-pricer -f
```

The scheduler runs at **2:00 AM Central** by default (see `PRICER_SCHEDULE_HOUR` / `PRICER_SCHEDULE_TIMEZONE` in `.env` to change).

---

## 5. (Optional) Discord bot as a separate service

```bash
sudo nano /etc/systemd/system/manabot-discord.service
```

```ini
[Unit]
Description=manabot Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/manabot
EnvironmentFile=/opt/manabot/.env
ExecStart=/opt/manabot/.venv/bin/python -m manabot discord-bot
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable manabot-discord
sudo systemctl start manabot-discord
sudo systemctl status manabot-discord
```

---

## Updating

```bash
cd /opt/manabot
git pull
source .venv/bin/activate
pip install -e ".[full]"

sudo systemctl restart manabot-pricer
sudo systemctl restart manabot-discord   # if running
```

---

## Useful commands

```bash
# Check scheduler is alive and see last run time
sudo journalctl -u manabot-pricer --since "24 hours ago"

# Run a manual price update immediately (outside the schedule)
source .venv/bin/activate && python -m manabot price-update

# Preview prices without writing
source .venv/bin/activate && python -m manabot price-update --dry-run

# View margin report
source .venv/bin/activate && python -m manabot margin-report
```
