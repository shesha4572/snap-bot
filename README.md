# Snapchat Shortcut Daily Automation Bot

Automates taking a daily snap and sending it to your friends shortcut (streak maintenance) via real Android device automation over ADB. No Snapchat API — runs on genuine hardware, zero ban risk.

## Features

- **Zero Ban Risk** — ADB touch simulation on a real Android device, indistinguishable from a human tap.
- **Gen Z AI News Captions** — Pulls the top 25 global headlines daily, picks the most relatable story for 20–25 year-olds, and generates a witty one-liner (≤ 75 chars) via OpenRouter.
- **Strict Fail-Safe** — If the AI quote fails or the API key is missing the bot aborts immediately. It will never send an uncaptioned snap.
- **Post-Run Email Report** — Win or fail, you get an email with the status, the generated quote, the verbose log, and a screenshot of the captioned snap. All files self-delete from disk after sending.
- **Dry-Run Mode** — Full simulation up to the final send tap. Nothing is actually sent.
- **systemd Timer Scheduling** — Frequency is controlled entirely by a systemd timer (not Python). Change it any time with `./deploy.sh --reconfigure`.
- **Pi Zero 2 W Ready** — < 35 MB RAM, ~15 s runtime per trigger.

---

## Requirements

- Linux host (Ubuntu / Debian / Raspberry Pi OS)
- Android phone with **Wireless Debugging** enabled (Android 11+)
- Python 3.11+
- ADB (`apt install adb`)
- An [OpenRouter](https://openrouter.ai) API key (free tier works)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) for email reports

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/<you>/snap-bot.git ~/snap-bot
cd ~/snap-bot
```

### 2. Enable Wireless Debugging on your phone

`Settings → Developer Options → Wireless Debugging → Enable`

Note the IP address shown there.

### 3. Run the deploy script

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will interactively ask for:

| Prompt | What to enter |
|---|---|
| **OpenRouter API key** | From [openrouter.ai/keys](https://openrouter.ai/keys) |
| **Gmail SMTP user** | Your Gmail address |
| **Gmail App Password** | From Google Account → Security → App Passwords |
| **Recipient email** | Where to send run reports |
| **Bot mode** | `dry-run` to test, `live` to actually send |
| **Schedule frequency** | A systemd `OnCalendar` expression (see examples below) |

Schedule examples:
```
*-*-* 09:00:00        every day at 09:00
*-*-* 00/4:00:00      every 4 hours
*-*-* 00/6:00:00      every 6 hours
Mon..Fri *-*-* 08:00  weekdays at 08:00
```

Credentials are saved to `.env` (chmod 600) and never committed to git.

---

## Reconfiguring

Change schedule, mode, or credentials at any time — no reinstall needed:

```bash
./deploy.sh --reconfigure
```

---

## Manual Commands

```bash
# Check phone is reachable + battery level
.venv/bin/python bot.py --status

# Full dry-run (no snap sent)
.venv/bin/python bot.py --now --dry-run

# Send a snap immediately (live)
.venv/bin/python bot.py --now

# Verbose debug output
.venv/bin/python bot.py --now --dry-run --debug
```

---

## Managing the Service

```bash
./deploy.sh --status      # next scheduled run + device status
./deploy.sh --run-now     # fire immediately outside the timer
./deploy.sh --logs        # live journal log tail
./deploy.sh --reconfigure # change schedule / env vars
```

Or via systemctl directly:

```bash
systemctl --user status snap-bot@$USER.timer
systemctl --user list-timers snap-bot@$USER.timer
journalctl --user -u snap-bot@$USER.service -f
```

---

## Configuration (`config.json`)

Credentials are managed by `deploy.sh` via `.env`. The only fields you may want to edit manually:

```json
{
  "device": "192.168.0.106:PORT",
  "shortcut_emoji": "🙂",
  "news_quote": {
    "enabled": true,
    "model": "openrouter/free"
  },
  "timings": {
    "launch_delay_sec": 4.0,
    "snap_capture_delay_sec": 2.0,
    "caption_delay_sec": 1.5,
    "send_to_menu_delay_sec": 2.5,
    "shortcut_select_delay_sec": 1.5,
    "send_confirm_delay_sec": 2.0
  },
  "coordinates": {
    "shutter":        [540, 1858],
    "caption_button": [1005, 163],
    "caption_dismiss": [540, 400],
    "send_to":        [878, 2148],
    "shortcut_pill":  [194, 337],
    "select_all":     [938, 689],
    "final_send":     [960, 2140]
  }
}
```

> **Note:** The device port is auto-discovered on each run — Android 11+ uses a random ephemeral port for Wireless Debugging. You only need the IP address to be correct.

> **Coordinates** are calibrated for a 1080×2400 display (Samsung Galaxy M52 5G). Re-calibrate `shortcut_pill` and other taps if your phone has a different resolution.

---

## Deploying to Raspberry Pi Zero 2 W

```bash
# From your main machine
scp -r ~/snap-bot pi@<PI_IP>:~/snap-bot

# SSH into the Pi
ssh pi@<PI_IP>
cd ~/snap-bot
chmod +x deploy.sh
./deploy.sh
```

That's it — `deploy.sh` handles venv, dependencies, and systemd on the Pi exactly the same as on any other Linux host.
