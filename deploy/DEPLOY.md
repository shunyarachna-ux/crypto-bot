# Deploying to an Ubuntu cloud server (paper mode)

Use a small server with a fixed IP (Oracle free tier, or any 1 vCPU / 1–2 GB VPS).
Paper mode needs no exchange keys. Do not add trading keys until Phase 4 and a reviewed `LiveExecutor`.

## 1. Harden the server (once)

```bash
# as root, first login
adduser --disabled-password --gecos "" deployer && usermod -aG sudo deployer
mkdir -p /home/deployer/.ssh && cp ~/.ssh/authorized_keys /home/deployer/.ssh/ && chown -R deployer: /home/deployer/.ssh
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
ufw default deny incoming && ufw allow OpenSSH && ufw --force enable
apt-get update && apt-get install -y unattended-upgrades python3-venv git && dpkg-reconfigure -plow unattended-upgrades
```

Open a second terminal and confirm you can log in as `deployer` with your key before closing the first one.

## 2. Install the bot

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin cryptobot
sudo mkdir -p /opt/crypto-bot && sudo chown deployer: /opt/crypto-bot
git clone https://github.com/<you>/crypto-bot.git /opt/crypto-bot   # private repo: use a read-only deploy key
cd /opt/crypto-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
mkdir -p state reports data && sudo chown -R cryptobot: state reports data
```

## 3. Secrets (server only)

```bash
sudo cp .env.example .env && sudo chown cryptobot: .env && sudo chmod 600 .env
sudo -e .env        # add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Leave exchange keys empty for paper mode.
```

Create the Telegram bot with @BotFather, send it a message, and get your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`. The bot only obeys commands from that chat.

## 4. Start it

```bash
sudo cp deploy/crypto-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now crypto-bot
journalctl -u crypto-bot -f          # watch the log; Telegram should say "bot started"
```

`Restart=always` restarts it after a crash; `enable` starts it on reboot. State is saved after
every candle in `state/`, so a restart carries on where it left off.

## Operating

| Task | Command |
| --- | --- |
| Status | `systemctl status crypto-bot` or `/status` in Telegram |
| Emergency stop | `/stop` in Telegram (closes all paper positions, blocks new entries) |
| Clear a stop / kill switch | `sudo systemctl stop crypto-bot && sudo -u cryptobot .venv/bin/python -m bot.main --reset-kill && sudo systemctl start crypto-bot` |
| Update | `git pull && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m pytest -q && sudo systemctl restart crypto-bot` |
| Download history for backtests | `.venv/bin/python -m bot.data download --years 3` (needs an exchange reachable from the server) |

A heartbeat arrives on Telegram every few hours. If it stops, the server is down.
