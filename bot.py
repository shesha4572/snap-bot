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
            res = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                if target in line and "\tdevice" in line:
                    return True
            subprocess.run([self.adb, "connect", target], capture_output=True, text=True, timeout=4)
            time.sleep(0.5)
            res = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=3)
            for line in res.stdout.splitlines():
                if target in line and "\tdevice" in line:
                    return True
        except Exception:
            pass
        return False

    def ensure_connected(self) -> bool:
        """Verify device connection."""
        if not self.device:
            try:
                res = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=3)
                for line in res.stdout.splitlines():
                    if "\tdevice" in line:
                        self.device = line.split()[0]
                        logger.info(f"Connected to device {self.device}")
                        return True
            except Exception as e:
                logger.debug(f"ADB devices check error: {e}")
            logger.error("No device configured in config.json and no connected ADB device found.")
            return False

        if self._test_adb_connect(self.device):
            logger.info(f"Connected to device {self.device}")
            return True

        logger.error(f"Could not establish ADB connection to {self.device}.")
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

    def _bounds_to_center(self, bounds: str) -> tuple[int, int] | None:
        """Parse bounds '[x1,y1][x2,y2]' into center (x, y)."""
        if bounds.startswith("[") and "][" in bounds:
            try:
                p1, p2 = bounds.strip("[]").split("][")
                x1, y1 = map(int, p1.split(","))
                x2, y2 = map(int, p2.split(","))
                return (x1 + x2) // 2, (y1 + y2) // 2
            except Exception:
                pass
        return None

    def get_ui_hierarchy(self) -> ET.Element | None:
        """Dump UI and parse into XML ElementTree."""
        try:
            self.run_adb("shell", "uiautomator", "dump", "/sdcard/ui_dump.xml", check=False)
            res = self.run_adb("shell", "cat", "/sdcard/ui_dump.xml", check=False)
            if not res.stdout or "<hierarchy" not in res.stdout:
                return None
            return ET.fromstring(res.stdout)
        except Exception as e:
            logger.debug(f"UI hierarchy dump failed: {e}")
            return None

    def find_center_by_desc(self, query: str) -> tuple[int, int] | None:
        """Dump UI and dynamically calculate center (x, y) of an element matching content-desc or text."""
        try:
            root = self.get_ui_hierarchy()
            if root is None:
                return None
            for node in root.iter("node"):
                desc = node.attrib.get("content-desc", "")
                text = node.attrib.get("text", "")
                if query.lower() in desc.lower() or query.lower() in text.lower():
                    bounds = node.attrib.get("bounds", "")
                    center = self._bounds_to_center(bounds)
                    if center:
                        logger.info(f"Dynamically resolved '{query}' to {center}")
                        return center
        except Exception as e:
            logger.debug(f"Dynamic lookup for '{query}' failed: {e}")
        return None

    def discard_active_draft(self, max_attempts: int = 5) -> bool:
        """
        Cleanly discard any active draft, dialog, or preview to restore
        Snapchat to a clean camera viewfinder state.
        """
        logger.info("Checking for drafts/dialogs to discard...")
        for attempt in range(1, max_attempts + 1):
            root = self.get_ui_hierarchy()
            if root is None:
                self.run_adb("shell", "input", "keyevent", "4", check=False)
                time.sleep(1.0)
                continue

            # 1. Check if already on clean camera screen
            for node in root.iter("node"):
                res_id = node.attrib.get("resource-id", "")
                desc = node.attrib.get("content-desc", "")
                if "camera_capture_button" in res_id or desc == "Camera Capture":
                    logger.info("Snapchat is on clean camera viewfinder.")
                    return True

            # 2. Check for Abandon/Discard confirmation dialog
            # Structure:
            # - dialog_content (LinearLayout)
            #   - child 0: alert_dialog_title ("Are you sure you want to abandon...")
            #   - child 1: discard_alert_dialog_discard_view ("Save & Close")
            #   - child 2: Abandon button (clickable View, text/desc empty)
            #   - child 3: cancel_button ("Cancel")
            abandon_target = None
            for parent in root.iter("node"):
                if "dialog_content" in parent.attrib.get("resource-id", ""):
                    children = list(parent)
                    for i, child in enumerate(children):
                        if "discard_alert_dialog_discard_view" in child.attrib.get("resource-id", ""):
                            if i + 1 < len(children):
                                abandon_target = self._bounds_to_center(children[i + 1].attrib.get("bounds", ""))
                            break
                    if not abandon_target and len(children) >= 3:
                        abandon_target = self._bounds_to_center(children[2].attrib.get("bounds", ""))
                    break

            if abandon_target:
                logger.info(f"Discarding draft: tapping 'Abandon' on dialog at {abandon_target}")
                self.tap(abandon_target[0], abandon_target[1], label="Abandon Dialog Button")
                time.sleep(1.0)
                continue

            # 3. Check for Preview screen top-left Discard button ('X')
            preview_discard = None
            for node in root.iter("node"):
                res_id = node.attrib.get("resource-id", "")
                desc = node.attrib.get("content-desc", "")
                if "preview_back_discard_button" in res_id or (desc == "Discard" and node.attrib.get("clickable") == "true"):
                    bounds = node.attrib.get("bounds", "")
                    center = self._bounds_to_center(bounds)
                    if center and center[1] < 400:  # Top area
                        preview_discard = center
                        break

            if preview_discard:
                logger.info(f"Preview screen detected: tapping top-left discard button at {preview_discard}")
                self.tap(preview_discard[0], preview_discard[1], label="Preview Discard Button")
                time.sleep(1.0)
                continue

            # 4. If in Send-To screen or text/sticker drawer, press BACK
            logger.info(f"Sub-screen detected (attempt {attempt}/{max_attempts}): pressing BACK...")
            self.run_adb("shell", "input", "keyevent", "4", check=False)
            time.sleep(1.0)

        # Check final camera state
        root = self.get_ui_hierarchy()
        if root:
            for node in root.iter("node"):
                if "camera_capture_button" in node.attrib.get("resource-id", ""):
                    return True
        return False

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
            self.discard_active_draft()
        except Exception as e:
            logger.debug(f"Startup clean note: {e}")

    def close_snapchat(self):
        """Discard active draft, return to home screen, and force stop Snapchat."""
        logger.info("Closing Snapchat cleanly and discarding any drafts...")
        try:
            self.discard_active_draft()
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
