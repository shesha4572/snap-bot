#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — Snap-Bot full setup & reconfigure script
#
#  Usage:
#    ./deploy.sh               First-time install (installs deps, sets env, sets schedule)
#    ./deploy.sh --reconfigure Change schedule frequency or env vars without reinstalling
#    ./deploy.sh --status      Show timer status and next run time
#    ./deploy.sh --run-now     Fire the bot immediately (outside the timer)
#    ./deploy.sh --logs        Tail live journal logs
# =============================================================================
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
header()  { echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

prompt_with_default() {
    # prompt_with_default "Label" "default_value" [secret]
    local label="$1" default="$2" secret="${3:-}"
    if [[ -n "$secret" ]]; then
        read -r -s -p "  ${label} [leave blank to keep existing]: " value; echo
    else
        read -r -p "  ${label} [default: ${default}]: " value
    fi
    echo "${value:-$default}"
}

prompt_secret() {
    # Prompts for a secret; shows "****" hint if a current value exists
    local label="$1" current="$2"
    local hint=""
    [[ -n "$current" ]] && hint=" [current: ****]" || hint=" [not set]"
    read -r -s -p "  ${label}${hint}: " value; echo
    echo "${value:-$current}"
}

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
SERVICE_NAME="snap-bot@${USER}"
TIMER_NAME="snap-bot@${USER}.timer"
SERVICE_SRC="${SCRIPT_DIR}/snap-bot.service"
TIMER_SRC="${SCRIPT_DIR}/snap-bot.timer"
SYSTEMD_DIR="/etc/systemd/system"

# ── Load existing .env if present ────────────────────────────────────────────
load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        # shellcheck disable=SC1090
        set +e
        set -o allexport
        source "$ENV_FILE" 2>/dev/null
        set +o allexport
        set -e
    fi
}

# ── Mode flags ────────────────────────────────────────────────────────────────
MODE_INSTALL=true
MODE_RECONFIG=false
MODE_STATUS=false
MODE_RUN_NOW=false
MODE_LOGS=false

for arg in "$@"; do
    case "$arg" in
        --reconfigure) MODE_INSTALL=false; MODE_RECONFIG=true ;;
        --status)      MODE_INSTALL=false; MODE_STATUS=true ;;
        --run-now)     MODE_INSTALL=false; MODE_RUN_NOW=true ;;
        --logs)        MODE_INSTALL=false; MODE_LOGS=true ;;
        --help|-h)
            echo "Usage: $0 [--reconfigure|--status|--run-now|--logs]"
            exit 0 ;;
    esac
done

# ══════════════════════════════════════════════════════════════════════════════
#  --status
# ══════════════════════════════════════════════════════════════════════════════
if $MODE_STATUS; then
    header "Snap-Bot Timer Status"
    systemctl status "$TIMER_NAME" --no-pager 2>/dev/null \
        || warn "Timer not installed yet. Run ./deploy.sh to install."
    echo ""
    info "Next scheduled runs:"
    systemctl list-timers "$TIMER_NAME" --no-pager 2>/dev/null || true
    echo ""
    info "Device status:"
    "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/bot.py" --status 2>&1 || true
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
#  --logs
# ══════════════════════════════════════════════════════════════════════════════
if $MODE_LOGS; then
    exec journalctl -u "${SERVICE_NAME}.service" -f
fi

# ══════════════════════════════════════════════════════════════════════════════
#  --run-now  (fire immediately outside timer)
# ══════════════════════════════════════════════════════════════════════════════
if $MODE_RUN_NOW; then
    load_env
    header "Running snap-bot immediately"
    info "Flags: ${SNAP_BOT_FLAGS:-none (live mode)}"
    sudo systemctl start "${SERVICE_NAME}.service" \
        && info "Done! Check logs: journalctl -u ${SERVICE_NAME}.service -n 50" \
        || err "Service start failed — is it installed? Run ./deploy.sh first."
    exit 0
fi

# ══════════════════════════════════════════════════════════════════════════════
#  INSTALL / RECONFIGURE
# ══════════════════════════════════════════════════════════════════════════════
load_env   # pre-load existing values so prompts can show them as defaults

# ── 1. Install system + Python dependencies (install only) ───────────────────
if $MODE_INSTALL; then
    header "Step 1 — Installing System Dependencies"
    sudo apt-get update -qq
    sudo apt-get install -y adb python3 python3-pip python3-venv
    info "System packages OK."

    header "Step 2 — Python Virtual Environment & Packages"
    python3 -m venv "${SCRIPT_DIR}/.venv"
    "${SCRIPT_DIR}/.venv/bin/pip" install --upgrade pip -q
    "${SCRIPT_DIR}/.venv/bin/pip" install requests -q
    info "Python venv ready."
fi

# ── 2. Environment Variables ──────────────────────────────────────────────────
header "$(if $MODE_RECONFIG; then echo "Reconfigure — Environment Variables"; else echo "Step 3 — Environment Variables"; fi)"
echo "  These are saved permanently to ${ENV_FILE} and loaded by the service."
echo "  Press Enter to keep the current value."
echo ""

NEW_OPENROUTER_KEY=$(prompt_secret "OpenRouter API Key" "${OPENROUTER_API_KEY:-}")
NEW_SMTP_USER=$(prompt_with_default "Gmail SMTP user (your Gmail address)" "${SMTP_USER:-}")
NEW_SMTP_PASS=$(prompt_secret "Gmail App Password" "${SMTP_PASS:-}")
NEW_RECIPIENT=$(prompt_with_default "Recipient email for reports" "${RECIPIENT_EMAIL:-$NEW_SMTP_USER}")

# ── 3. Bot Mode (dry-run or live) ─────────────────────────────────────────────
header "$(if $MODE_RECONFIG; then echo "Reconfigure — Bot Mode"; else echo "Step 4 — Bot Mode"; fi)"
echo "  Dry-run:  Runs the full flow but skips the final send tap. Safe for testing."
echo "  Live:     Sends the actual snap."
echo ""
CURRENT_FLAGS="${SNAP_BOT_FLAGS:-}"
if [[ "$CURRENT_FLAGS" == *"--dry-run"* ]]; then
    CURRENT_MODE="dry-run"
else
    CURRENT_MODE="live"
fi

read -r -p "  Mode? [dry-run / live] (current: ${CURRENT_MODE}): " input_mode
input_mode="${input_mode:-$CURRENT_MODE}"
case "$input_mode" in
    dry*) NEW_FLAGS="--dry-run" ;;
    live) NEW_FLAGS="" ;;
    *)    warn "Unrecognised input '${input_mode}', keeping current mode: ${CURRENT_MODE}"; NEW_FLAGS="$CURRENT_FLAGS" ;;
esac

# ── 4. Schedule Frequency ─────────────────────────────────────────────────────
header "$(if $MODE_RECONFIG; then echo "Reconfigure — Schedule Frequency"; else echo "Step 5 — Schedule Frequency"; fi)"
cat <<'EOF'
  Enter a systemd OnCalendar expression. Examples:
    daily                     → once a day at midnight
    *-*-* 09:00:00            → every day at 09:00
    *-*-* 00/4:00:00          → every 4 hours
    *-*-* 00/6:00:00          → every 6 hours
    Mon..Fri *-*-* 08:00:00   → weekdays at 08:00

  Press Enter to keep the current value.
EOF
echo ""

# Read current timer OnCalendar value from installed timer (if any) or the template
CURRENT_CALENDAR=$(systemctl cat "$TIMER_NAME" 2>/dev/null \
    | grep '^OnCalendar=' | head -1 | cut -d= -f2 || echo "")
[[ -z "$CURRENT_CALENDAR" ]] && CURRENT_CALENDAR="daily"

read -r -p "  OnCalendar [current: ${CURRENT_CALENDAR}]: " input_calendar
NEW_CALENDAR="${input_calendar:-$CURRENT_CALENDAR}"

# Validate with systemd-analyze if available
if command -v systemd-analyze &>/dev/null; then
    if ! systemd-analyze calendar "$NEW_CALENDAR" &>/dev/null; then
        warn "systemd-analyze reports '${NEW_CALENDAR}' may be invalid — double-check the syntax."
        read -r -p "  Continue anyway? [y/N]: " cont
        [[ "${cont,,}" == "y" ]] || err "Aborted."
    fi
fi

# ── 5. Write .env file and update config.json ─────────────────────────────────
header "Writing .env & Updating config.json"

PYTHON="${SCRIPT_DIR}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi

"$PYTHON" - "$SCRIPT_DIR" "$NEW_OPENROUTER_KEY" "$NEW_SMTP_USER" "$NEW_SMTP_PASS" "$NEW_RECIPIENT" "$NEW_FLAGS" <<'PYEOF'
import sys
import json
import pathlib

script_dir = pathlib.Path(sys.argv[1]).resolve()
api_key   = sys.argv[2].strip()
smtp_user = sys.argv[3].strip()
smtp_pass = sys.argv[4].strip()
recip     = sys.argv[5].strip()
flags     = sys.argv[6].strip()

# Safely write .env (wrap in quotes to preserve spaces/special chars)
env_file = script_dir / ".env"
with open(env_file, "w", encoding="utf-8") as f:
    f.write("# snap-bot environment variables — managed by deploy.sh\n")
    f.write("# Edit manually or run: ./deploy.sh --reconfigure\n")
    f.write(f'OPENROUTER_API_KEY="{api_key}"\n')
    f.write(f'SMTP_USER="{smtp_user}"\n')
    f.write(f'SMTP_PASS="{smtp_pass}"\n')
    f.write(f'RECIPIENT_EMAIL="{recip}"\n')
    f.write(f'SNAP_BOT_FLAGS="{flags}"\n')

# Patch config.json
cfg_path = script_dir / "config.json"
if cfg_path.exists():
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
else:
    cfg = {}

email = cfg.setdefault("email_notifications", {})
nr    = cfg.setdefault("news_quote", {})

if smtp_user: email["smtp_user"] = smtp_user
if smtp_pass: email["smtp_password"] = smtp_pass
if recip:     email["recipient_email"] = recip
if api_key:   nr["openrouter_api_key"] = api_key

with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)

print("  config.json updated.")
PYEOF

chmod 600 "$ENV_FILE"
info "Saved to ${ENV_FILE} (mode 600)."

# ── 6. Install / update systemd units ────────────────────────────────────────
header "Installing systemd Service + Timer"

# Template the actual directory into the service unit
SERVICE_TMP=$(mktemp)
sed "s|__DIR__|${SCRIPT_DIR}|g" "$SERVICE_SRC" > "$SERVICE_TMP"

# Stamp the chosen calendar into the timer unit on disk
TIMER_TMP=$(mktemp)
sed "s|^OnCalendar=.*|OnCalendar=${NEW_CALENDAR}|" "$TIMER_SRC" > "$TIMER_TMP"

sudo install -m 644 "$SERVICE_TMP" "${SYSTEMD_DIR}/snap-bot@.service"
sudo install -m 644 "$TIMER_TMP"   "${SYSTEMD_DIR}/${TIMER_NAME}"
rm -f "$SERVICE_TMP" "$TIMER_TMP"

sudo systemctl daemon-reload

# Enable & (re)start the timer; leave the service to be triggered by the timer
sudo systemctl enable --now "${TIMER_NAME}" 2>/dev/null \
    || { sudo systemctl enable "${TIMER_NAME}"; sudo systemctl restart "${TIMER_NAME}"; }

info "Timer enabled and running."

# ── 7. Summary ────────────────────────────────────────────────────────────────
header "✅ All Done!"
echo ""
echo "  Schedule  : ${NEW_CALENDAR}"
echo "  Bot mode  : ${input_mode}"
echo "  Env file  : ${ENV_FILE}"
echo ""
echo "  Useful commands:"
echo "    ./deploy.sh --status        → next run time + device status"
echo "    ./deploy.sh --run-now       → fire immediately"
echo "    ./deploy.sh --logs          → live journal log tail"
echo "    ./deploy.sh --reconfigure   → change schedule / env vars"
echo ""
systemctl list-timers "${TIMER_NAME}" --no-pager 2>/dev/null || true
