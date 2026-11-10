<p align="center">
  <img src="assets/logo.png" alt="tiktok-streak-saver" width="160">
</p>

# tiktok-streak-saver

headless script that keeps tiktok streaks alive without spamming. checks when the last message was sent and only texts during the 18–22h window so streaks don't break. runs 24/7 on linux vps (oracle cloud, raspberry pi) or windows.

## how it works

tiktok streaks break if 24 hours pass without a message from either person. instead of spamming on a dumb timer, this script:

1. opens the chat and reads the last message timestamp
2. calculates elapsed time:
   - under 18h: does nothing, streak is safe
   - 18 to 22h: sends a streak message to reset the 24h timer
   - over 22h: streak is about to die, sends immediately
3. sleeps 30 minutes and checks again

## setup

```bash
git clone https://github.com/whileLost/tiktok-streak-saver.git
cd tiktok-streak-saver

python3 -m venv venv
source venv/bin/activate  # windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium  # linux only
```

## config

copy example config:

```bash
cp config.example.json config.json
```

edit `config.json`:

```json
{
  "recipients": [
    "username_or_display_name"
  ],
  "messages": [
    "🔥 streak",
    "streak 🔥",
    "🔥"
  ],
  "randomize_message": true,
  "headless": true,
  "browser_channel": "auto",
  "daemon_interval_minutes": 30,
  "streak_trigger_elapsed_hours": 18.0,
  "streak_critical_elapsed_hours": 22.0,
  "alert_recipient_expiring": false,
  "recipient_critical_elapsed_hours": 21.0,
  "recipient_reminder_messages": [
    "⚠️ your streak is expiring soon! send a message to keep it alive 🔥"
  ],
  "discord": {
    "webhook_url": "",
    "avatar_url": "https://raw.githubusercontent.com/whileLost/tiktok-streak-saver/main/assets/logo.png",
    "notify_on_send": true,
    "notify_on_warning": true,
    "notify_on_error": true,
    "notify_on_check": false
  }
}
```

## auth

tiktok blocks headless logins with slider captchas, so the easiest way is copying your `sessionid` cookie from your browser:

1. open tiktok.com in your browser while logged in
2. press f12 to open devtools
3. go to application (storage on firefox) -> cookies -> `https://www.tiktok.com`
4. copy the value of `sessionid`
5. save it:
   ```bash
   python streak.py --set-cookie "your_sessionid_here"
   ```

check if it works:
```bash
python streak.py --status
```

## usage

test run (checks timestamps and navigates without sending):
```bash
python streak.py --dry-run
```

single check:
```bash
python streak.py
```

force send right now:
```bash
python streak.py --force
```

run daemon:
```bash
python streak.py --daemon
```

## systemd service (linux)

to run it 24/7 in the background with auto restart on boot:

1. copy service file:
   ```bash
   sudo cp tiktok-streak-saver.service /etc/systemd/system/
   ```
2. adjust user and working directory in `/etc/systemd/system/tiktok-streak-saver.service` if your path is different
3. enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now tiktok-streak-saver
   ```
4. view logs:
   ```bash
   sudo journalctl -u tiktok-streak-saver -f
   ```

## cli options

- `--daemon`: run continuous monitoring loop
- `--dry-run`: check timestamps and open chat without sending
- `--force`: send message immediately regardless of elapsed time
- `--status`: check if saved session is valid
- `--set-cookie <id>`: save sessionid cookie
- `--login`: open browser window for manual login
- `--export-session`: export session to tiktok_session.json
- `--import-session <file>`: import session file
- `--user <name>`: check only a specific recipient
- `--test-discord`: send test webhook to discord
- `--headful`: run with visible browser window

## license

mit
