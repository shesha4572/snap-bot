#!/usr/bin/env python3
"""
Snapchat Shortcut Daily Automation Bot
Includes:
- Gen Z (20-25 yo) AI News Quote Generation (<= 70 chars) via OpenRouter
- Ultra-verbose file logging to disk
- Post-run email delivery (pass or fail) with quote, snap status, and attached log file
- Automatic log file deletion after email delivery
- Automatic device wake, live capture, shortcut recipient selection, and sleep
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from mailer import send_run_email
from news_quote import fetch_top_news, generate_news_quote, sanitize_for_adb

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

# Root logger setup
logger = logging.getLogger("snap-bot")
logger.setLevel(logging.DEBUG)


def load_persistent_env():
    """
    Auto-load persistent environment variables from env.sh, ~/.snapbot_env, or .env
    if present, so credentials persist across sessions without manual re-export.
    """
    candidates = [
        BASE_DIR / "env.sh",
        Path.home() / ".snapbot_env",
        BASE_DIR / ".env",
    ]
    for env_file in candidates:
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        if k and k not in os.environ:
                            os.environ[k] = v
                logger.debug(f"Loaded persistent environment variables from {env_file.name}")
                break
            except Exception as e:
                logger.debug(f"Could not parse {env_file}: {e}")


def find_adb_path() -> str:
    """Locate adb binary (local platform-tools or system PATH)."""
    local_adb = BASE_DIR / "platform-tools" / "adb"
    if local_adb.is_file() and os.access(local_adb, os.X_OK):
        return str(local_adb)
    which_adb = shutil.which("adb")
    if which_adb:
        return which_adb
    raise FileNotFoundError("Could not find 'adb' binary in platform-tools or system PATH.")


class SnapchatBot:
    def __init__(self, config_path: Path = CONFIG_PATH):
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.adb = find_adb_path()
        self.device = self.config.get("device", "").strip()
        self.timings = self.config.get("timings", {})
        self.coords = self.config.get("coordinates", {})
        self.news_cfg = self.config.get("news_quote", {})
        self.email_cfg = self.config.get("email_notifications", {})

    def run_adb(self, *args, check: bool = True) -> subprocess.CompletedProcess:
        """Execute an adb command with full debug logging."""
        cmd = [self.adb]
        if self.device:
            cmd.extend(["-s", self.device])
        cmd.extend(args)
        logger.debug(f"[EXEC] {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout.strip():
            logger.debug(f"[STDOUT] {result.stdout.strip()}")
        if result.stderr.strip():
            logger.debug(f"[STDERR] {result.stderr.strip()}")

        if check and result.returncode != 0:
            logger.error(f"ADB command failed: {' '.join(cmd)}\nError: {result.stderr.strip()}")
            raise RuntimeError(f"ADB command failed: {result.stderr.strip()}")
        return result

    def _test_adb_connect(self, target: str) -> bool:
        """Attempt 'adb connect target' and check if device is authorized and online."""
        try:
            subprocess.run([self.adb, "connect", target], capture_output=True, text=True, timeout=4)
            time.sleep(0.5)
            res = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                if target in line and "\tdevice" in line:
                    return True
        except Exception:
            pass
        return False

    def auto_discover_adb_port(self, target_ip: str) -> int | None:
        """
        Auto-discover the active Wireless Debugging port on target_ip:
        1. Checks currently attached active devices in adb.
        2. Queries mDNS services via 'adb mdns services'.
        3. Runs high-speed socket scan across ephemeral port range (32000-47000).
        """
        logger.info(f"[Auto-Discovery] Searching for active ADB port on {target_ip}...")

        # Step 1: Check existing active devices in ADB
        try:
            res = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                if target_ip in line and "\tdevice" in line:
                    port = int(line.split()[0].split(":")[-1])
                    logger.info(f"[Auto-Discovery] Reusing active connected port: {target_ip}:{port}")
                    return port
        except Exception as e:
            logger.debug(f"[Auto-Discovery] Device check note: {e}")

        # Step 2: Query mDNS services via ADB
        try:
            logger.info("[Auto-Discovery] Querying mDNS services...")
            mdns = subprocess.run([self.adb, "mdns", "services"], capture_output=True, text=True, timeout=4)
            for line in mdns.stdout.splitlines():
                if target_ip in line:
                    parts = line.split()
                    for p in parts:
                        if ":" in p and target_ip in p:
                            port = int(p.split(":")[-1])
                            logger.info(f"[Auto-Discovery] Discovered candidate port via mDNS: {target_ip}:{port}")
                            if self._test_adb_connect(f"{target_ip}:{port}"):
                                logger.info(f"[Auto-Discovery] Handshake confirmed on mDNS port: {port}")
                                return port
        except Exception as e:
            logger.debug(f"[Auto-Discovery] mDNS note: {e}")

        # Step 3: Fast multi-threaded socket scan across Android 11+ dynamic port range
        import concurrent.futures
        import socket

        logger.info(f"[Auto-Discovery] Scanning dynamic port range on {target_ip}...")
        # Prioritize 35000-46000 where Android dynamic ports typically land
        ports_to_scan = [5555] + list(range(35000, 46000)) + list(range(30000, 35000)) + list(range(46000, 61001))

        def _check_socket(port):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex((target_ip, port)) == 0:
                    return port
            return None

        open_ports = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=200) as executor:
            futures = {executor.submit(_check_socket, p): p for p in ports_to_scan}
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    open_ports.append(res)

        if open_ports:
            logger.info(f"[Auto-Discovery] Detected open candidate ports: {open_ports}")
            for p in open_ports:
                target_cand = f"{target_ip}:{p}"
                logger.info(f"[Auto-Discovery] Testing ADB handshake on {target_cand}...")
                if self._test_adb_connect(target_cand):
                    logger.info(f"[Auto-Discovery] SUCCESS: Handshake verified on {target_cand}!")
                    return p

        logger.error(f"[Auto-Discovery] Failed to locate any active ADB port on {target_ip}.")
        return None

    def ensure_connected(self) -> bool:
        """Verify device connection, auto-discovering new port if needed."""
        target_ip = self.device.split(":")[0] if ":" in self.device else self.device

        # Check if current configured port works directly
        if self.device and self._test_adb_connect(self.device):
            logger.info(f"Connected to device {self.device}")
            return True

        # If disconnected or port changed, trigger auto-discovery
        logger.warning(f"Connection to {self.device} failed or port changed. Starting auto-discovery...")
        discovered_port = self.auto_discover_adb_port(target_ip)
        if discovered_port:
            new_target = f"{target_ip}:{discovered_port}"
            self.device = new_target
            self.config["device"] = new_target
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=2)
                logger.info(f"[Auto-Discovery] Persisted new active target in config.json: {new_target}")
            except Exception as e:
                logger.debug(f"Could not persist new port to config.json: {e}")
            return True

        logger.error(f"Could not establish ADB connection to {target_ip}.")
        return False

    def get_status(self) -> dict:
        """Query device battery and display status."""
        if not self.ensure_connected():
            return {
                "device": self.device,
                "connected": False,
                "error": "Device unreachable. Ensure Wireless Debugging is enabled.",
            }
        battery_res = self.run_adb("shell", "dumpsys", "battery")
        power_res = self.run_adb("shell", "dumpsys", "power")

        level = "Unknown"
        for line in battery_res.stdout.splitlines():
            if "level:" in line:
                level = line.split(":")[-1].strip() + "%"
                break

        screen_on = "mWakefulness=Awake" in power_res.stdout
        return {
            "device": self.device,
            "connected": True,
            "battery": level,
            "screen_awake": screen_on,
        }

    def wake_device(self):
        """Wake up display and swipe/dismiss keyguard."""
        logger.info("Waking up device...")
        self.run_adb("shell", "input", "keyevent", "224")  # KEYCODE_WAKEUP
        time.sleep(0.5)
        self.run_adb("shell", "input", "keyevent", "82")   # KEYCODE_MENU (swipe unlock)
        time.sleep(1.0)

    def sleep_device(self):
        """Lock and turn off the screen."""
        logger.info("Putting device back to sleep...")
        self.run_adb("shell", "input", "keyevent", "223")  # KEYCODE_SLEEP

    def take_screenshot(self, output_path: Path | str) -> bool:
        """Capture screen via ADB exec-out screencap and save to local path."""
        logger.info(f"Capturing pre-send screenshot to {output_path}...")
        cmd = [self.adb]
        if self.device:
            cmd.extend(["-s", self.device])
        cmd.extend(["exec-out", "screencap", "-p"])
        res = subprocess.run(cmd, capture_output=True)
        if res.returncode == 0 and res.stdout:
            with open(output_path, "wb") as f:
                f.write(res.stdout)
            return True
        else:
            # Fallback via shell screencap
            remote_tmp = "/sdcard/snap_bot_tmp.png"
            self.run_adb("shell", "screencap", "-p", remote_tmp, check=False)
            self.run_adb("pull", remote_tmp, str(output_path), check=False)
            self.run_adb("shell", "rm", "-f", remote_tmp, check=False)
            return Path(output_path).exists()

    def tap(self, x: int, y: int, label: str = ""):
        """Tap screen coordinates."""
        logger.info(f"Tapping {label} at ({x}, {y})")
        self.run_adb("shell", "input", "tap", str(x), str(y))

    def clear_active_input(self, count: int = 120):
        """
        Aggressively clear the active text field before typing.
        Samsung routes 'adb input text' through the clipboard, so a stale
        clipboard from a previous run can re-appear in the caption box.
        We do two full select-all + delete passes with a focus delay between them.
        """
        try:
            logger.info("Clearing active input field (double-pass)...")
            time.sleep(0.4)   # let the field fully focus before we start

            # Pass 1: Ctrl+A → DEL, then END + batch backspaces
            self.run_adb("shell", "input", "keycombination", "113", "29", check=False)  # Ctrl+A
            time.sleep(0.1)
            self.run_adb("shell", "input", "keyevent", "67", check=False)               # DEL
            self.run_adb("shell", "input", "keyevent", "123", check=False)              # KEYCODE_MOVE_END
            del_keys = ["67"] * min(count, 120)
            self.run_adb("shell", "input", "keyevent", *del_keys, check=False)
            time.sleep(0.3)

            # Pass 2: repeat — catches any residual text left after pass 1
            self.run_adb("shell", "input", "keycombination", "113", "29", check=False)  # Ctrl+A
            time.sleep(0.1)
            self.run_adb("shell", "input", "keyevent", "67", check=False)               # DEL
            self.run_adb("shell", "input", "keyevent", "123", check=False)              # KEYCODE_MOVE_END
            self.run_adb("shell", "input", "keyevent", *del_keys, check=False)
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"Clear input note: {e}")

    def _prime_clipboard(self, text: str):
        """
        Pre-load the clipboard with the target text before typing.
        On Samsung, 'adb input text' internally writes to the clipboard first,
        so priming it ensures that even if a paste event fires, it pastes the
        correct new caption rather than the previous run's stale one.
        """
        try:
            logger.info("Priming clipboard with new caption text...")
            self.run_adb(
                "shell",
                "am", "broadcast",
                "-a", "clipper.set",
                "--es", "text", text,
                check=False,
            )
        except Exception as e:
            logger.debug(f"Clipboard prime note (non-fatal): {e}")

    def type_text(self, text: str):
        """Clear existing text, prime the clipboard, then type the caption."""
        sanitized = sanitize_for_adb(text)
        self._prime_clipboard(sanitized)   # pre-load clipboard with new text
        self.clear_active_input()          # wipe whatever is in the box
        logger.info(f"Typing caption via ADB: '{sanitized}'")
        self.run_adb("shell", "input", "text", sanitized)

    def find_center_by_desc(self, query: str) -> tuple[int, int] | None:
        """Dump UI and dynamically calculate center (x, y) of an element matching content-desc or text."""
        try:
            self.run_adb("shell", "uiautomator", "dump", "/sdcard/ui_dump.xml", check=False)
            res = self.run_adb("shell", "cat", "/sdcard/ui_dump.xml", check=False)
            if not res.stdout or "<hierarchy" not in res.stdout:
                return None
            root = ET.fromstring(res.stdout)
            for node in root.iter("node"):
                desc = node.attrib.get("content-desc", "")
                text = node.attrib.get("text", "")
                if query.lower() in desc.lower() or query.lower() in text.lower():
                    bounds = node.attrib.get("bounds", "")
                    if bounds.startswith("[") and "][" in bounds:
                        p1, p2 = bounds.strip("[]").split("][")
                        x1, y1 = map(int, p1.split(","))
                        x2, y2 = map(int, p2.split(","))
                        center_x = (x1 + x2) // 2
                        center_y = (y1 + y2) // 2
                        logger.info(f"Dynamically resolved '{query}' to ({center_x}, {center_y})")
                        return center_x, center_y
        except Exception as e:
            logger.debug(f"Dynamic lookup for '{query}' failed: {e}")
        return None

    def launch_snapchat(self):
        """Launch Snapchat Landing Page and ensure clean camera state."""
        logger.info("Launching Snapchat...")
        self.run_adb("shell", "am", "force-stop", "com.snapchat.android", check=False)
        time.sleep(1.0)
        self.run_adb(
            "shell",
            "am",
            "start",
            "-n",
            "com.snapchat.android/com.snapchat.android.LandingPageActivity",
            check=False,
        )
        delay = self.timings.get("launch_delay_sec", 4.0)
        logger.info(f"Waiting {delay}s for camera to initialize...")
        time.sleep(delay)

        # Check for any residual draft or discard dialog on launch
        try:
            discard_btn = self.find_center_by_desc("Discard")
            if discard_btn:
                logger.info(f"Discarding residual draft at {discard_btn}")
                self.tap(discard_btn[0], discard_btn[1], label="Discard Dialog")
                time.sleep(1.0)
        except Exception as e:
            logger.debug(f"Startup clean note: {e}")

    def close_snapchat(self):
        """Discard active draft, return to home screen, and force stop Snapchat."""
        logger.info("Closing Snapchat cleanly and discarding any drafts...")
        try:
            # Press BACK 3 times to back out of Send-To / Editor and dismiss any draft
            for _ in range(3):
                self.run_adb("shell", "input", "keyevent", "4", check=False)  # KEYCODE_BACK
                time.sleep(0.3)
            # Check if a 'Discard' prompt appeared and tap it
            center = self.find_center_by_desc("Discard")
            if center:
                self.tap(center[0], center[1], label="Discard Dialog")
                time.sleep(0.3)
        except Exception as e:
            logger.debug(f"Discard draft note: {e}")

        self.run_adb("shell", "input", "keyevent", "3", check=False)  # HOME
        time.sleep(0.3)
        self.run_adb("shell", "am", "force-stop", "com.snapchat.android", check=False)

    def run_flow(self, dry_run: bool = False) -> bool:
        """
        Execute the full automated streak sending flow with:
        1. Pre-generated AI quote from top 25 news stories
        2. Verbose file logging
        3. Post-run email report with attached log file
        4. Automatic log file cleanup after email dispatch
        """
        mode_str = "[DRY-RUN]" if dry_run else "[LIVE]"
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = BASE_DIR / f"run_{timestamp_str}.log"

        # Attach verbose FileHandler to logger
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
        )
        logger.addHandler(file_handler)

        status_summary = "FAILED (Unknown)"
        caption_text = None
        error_details = None
        screenshot_path = None
        success = False

        logger.info(f"=== Starting Snapchat Shortcut Routine {mode_str} ===")
        logger.info(f"Logging verbose output to: {log_file.name}")

        try:
            # Step 1: Pre-generate AI News Quote
            if self.news_cfg.get("enabled", True):
                logger.info("Fetching top 25 news stories and requesting AI quote for Gen Z...")
                api_key = (
                    self.news_cfg.get("openrouter_api_key", "").strip()
                    or os.getenv("OPENROUTER_API_KEY", "").strip()
                )
                model = self.news_cfg.get("model", "openrouter/free")
                headlines = fetch_top_news(count=25)
                caption_text = generate_news_quote(api_key=api_key, model=model, headlines=headlines)
                logger.info(f"AI Quote ready: \"{caption_text}\" ({len(caption_text)} chars)")

            # Step 2: Ensure device connection
            if not self.ensure_connected():
                raise ConnectionError(f"Could not connect to target device: {self.device}")

            # Step 3: Wake device
            self.wake_device()

            # Step 4: Open Snapchat
            self.launch_snapchat()

            # Step 5: Take photo (Shutter)
            shutter_x, shutter_y = self.coords.get("shutter", [540, 1858])
            self.tap(shutter_x, shutter_y, label="Shutter Button")
            snap_delay = self.timings.get("snap_capture_delay_sec", 2.0)
            time.sleep(snap_delay)

            # Step 6: Type Caption
            if caption_text:
                cap_btn_x, cap_btn_y = self.coords.get("caption_button", [1005, 163])
                self.tap(cap_btn_x, cap_btn_y, label="Caption Tool")
                time.sleep(1.0)

                self.type_text(caption_text)
                time.sleep(1.0)

                dismiss_x, dismiss_y = self.coords.get("caption_dismiss", [540, 400])
                self.tap(dismiss_x, dismiss_y, label="Confirm Caption")
                time.sleep(1.0)

            # Step 7: Capture Screenshot showing the Snap with the typed Caption
            screenshot_path = BASE_DIR / f"snap_caption_{timestamp_str}.png"
            try:
                if self.take_screenshot(screenshot_path):
                    logger.info(f"Captured snap caption screenshot: {screenshot_path.name}")
                else:
                    logger.warning("Could not take snap caption screenshot.")
                    screenshot_path = None
            except Exception as ss_err:
                logger.warning(f"Failed to capture snap caption screenshot: {ss_err}")
                screenshot_path = None

            # Step 8: Tap "Send to"
            send_to_x, send_to_y = self.coords.get("send_to", [878, 2148])
            self.tap(send_to_x, send_to_y, label="'Send to' Button")
            menu_delay = self.timings.get("send_to_menu_delay_sec", 2.5)
            time.sleep(menu_delay)

            # Step 9: Tap Shortcut Pill
            pill_x, pill_y = self.coords.get("shortcut_pill", [194, 337])
            self.tap(pill_x, pill_y, label=f"Shortcut '{self.config.get('shortcut_emoji')}'")
            pill_delay = self.timings.get("shortcut_select_delay_sec", 1.5)
            time.sleep(pill_delay)

            # Step 10: Tap "Select All"
            sel_coords = self.find_center_by_desc("Select All")
            if not sel_coords:
                sel_coords = self.coords.get("select_all", [938, 689])
            self.tap(sel_coords[0], sel_coords[1], label="'Select All'")
            time.sleep(1.0)

            # Step 11: Final Send
            final_x, final_y = self.coords.get("final_send", [960, 2140])
            if dry_run:
                logger.info(
                    f"[DRY-RUN SAFETY] Would tap Final Send at ({final_x}, {final_y}). "
                    "Skipping tap: NO SNAPS WERE SENT."
                )
                status_summary = "SUCCESS (DRY-RUN: Simulation Completed, No Snap Sent)"
            else:
                self.tap(final_x, final_y, label="Final Send Button")
                logger.info("Snap sent successfully!")
                time.sleep(self.timings.get("send_confirm_delay_sec", 2.0))
                status_summary = "SUCCESS (Snap Delivered to Shortcut)"

            success = True

        except Exception as e:
            error_details = str(e)
            status_summary = f"FAILED: {e}"
            logger.error(f"Routine encountered critical error: {e}", exc_info=True)
            success = False

        finally:
            # Clean up device UI
            try:
                self.close_snapchat()
                self.sleep_device()
            except Exception as clean_err:
                logger.debug(f"Device sleep cleanup note: {clean_err}")

            logger.info(f"=== Execution Summary: {status_summary} ===")

            # Flush and detach FileHandler before email delivery and deletion
            file_handler.flush()
            file_handler.close()
            logger.removeHandler(file_handler)

            # Send Email Report with status, quote, attached log file, and screenshot
            try:
                send_run_email(
                    email_cfg=self.email_cfg,
                    status_summary=status_summary,
                    quote=caption_text or "N/A (Generation Failed or Disabled)",
                    error_details=error_details,
                    log_file_path=log_file,
                    screenshot_path=screenshot_path,
                )
            except Exception as mail_err:
                print(f"Error during email delivery: {mail_err}")

            # Safe cleanup: Delete temporary log file and screenshot from disk
            try:
                if log_file.exists():
                    log_file.unlink()
                    print(f"Log file '{log_file.name}' deleted from disk.")
                if screenshot_path and Path(screenshot_path).exists():
                    Path(screenshot_path).unlink()
                    print(f"Screenshot '{Path(screenshot_path).name}' deleted from disk.")
            except Exception as del_err:
                print(f"Could not delete temporary files: {del_err}")

        return success


def main():
    load_persistent_env()

    parser = argparse.ArgumentParser(description="Snapchat Shortcut Automation Bot")
    parser.add_argument("--now", action="store_true", help="Run the sending routine immediately")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run through all steps up to recipient selection without sending",
    )
    parser.add_argument("--status", action="store_true", help="Check device status and battery")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug console output")

    args = parser.parse_args()

    # Console handler setup
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if args.debug else logging.INFO)
    console_handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(console_handler)

    bot = SnapchatBot()

    if args.status:
        status = bot.get_status()
        print("\nDevice Status:")
        print(f"  Target:    {status.get('device')}")
        print(f"  Connected: {status.get('connected', True)}")
        if not status.get("connected", True):
            print(f"  Error:     {status.get('error')}\n")
            sys.exit(1)
        print(f"  Battery:   {status.get('battery')}")
        print(f"  Awake:     {status.get('screen_awake')}\n")
        return

    if args.now:
        success = bot.run_flow(dry_run=args.dry_run)
        sys.exit(0 if success else 1)

    parser.print_help()


if __name__ == "__main__":
    main()
