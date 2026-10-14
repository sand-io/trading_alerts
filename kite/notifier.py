import json
import os
import time
import platform
import shutil
import subprocess
import queue
import threading
from datetime import datetime


CONFIG_FILE = "config.json"

class Notifier:
    def __init__(self):
        self.last_alert_times = {}  # Format: {(symbol, condition_str): timestamp}
        self.cooldown_seconds = 300  # Default 5 minutes
        self.load_cooldown()
        self._desktop_queue = queue.Queue(maxsize=1000)
        self._desktop_stop = threading.Event()
        self._desktop_worker = None

    def start_background(self):
        if self._desktop_worker is not None and self._desktop_worker.is_alive():
            return
        self._desktop_stop.clear()
        self._desktop_worker = threading.Thread(target=self._deliver_pending, name='kite-desktop-notifications', daemon=True)
        self._desktop_worker.start()

    def stop_background(self, timeout=5):
        self._desktop_stop.set()
        if self._desktop_worker is not None:
            self._desktop_worker.join(timeout)

    def _deliver_pending(self):
        while not self._desktop_stop.is_set() or not self._desktop_queue.empty():
            try:
                title, message = self._desktop_queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._send_desktop(title, message)
            finally:
                self._desktop_queue.task_done()

    def load_cooldown(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    config = json.load(f)
                    self.cooldown_seconds = config.get("cooldown_seconds", 300)
            except Exception:
                pass

    def send_alert(self, symbol, is_bullish, condition_str, price, cooldown_key=None):
        current_time = time.time()
        # Use custom cooldown_key if provided to prevent key variation (e.g. changing sub-condition count) from resetting the cooldown
        key = cooldown_key if cooldown_key is not None else condition_str
        alert_key = (symbol, key)
        
        # Check cooldown
        if alert_key in self.last_alert_times:
            elapsed = current_time - self.last_alert_times[alert_key]
            if elapsed < self.cooldown_seconds:
                # Still in cooldown
                return False

        # Update last alert time
        self.last_alert_times[alert_key] = current_time

        # Format details
        direction = "BULLISH 🟢" if is_bullish else "BEARISH 🔴"
        title = f"{direction} Alert: {symbol}"
        
        # If condition_str is multiline, make a clean single line for the macOS notification bubble
        first_line = condition_str.split('\n')[0]
        message = f"Price: {price} | Condition: {first_line}"
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Print to terminal with clean, vibrant formatting
        color_code = "\033[92m" if is_bullish else "\033[91m"  # Green or Red
        reset_code = "\033[0m"
        print(f"\n[{timestamp_str}] {color_code}ALERT: {symbol} is {direction}!{reset_code}")
        print(f"  └─ Condition: '{condition_str}'")
        print(f"  └─ Current Price: {price}")

        if self._desktop_worker is not None and self._desktop_worker.is_alive():
            try:
                self._desktop_queue.put_nowait((title, message))
            except queue.Full:
                print('  └─ Desktop notification queue is full; terminal and saved alert retained')
        else:
            self._send_desktop(title, message)
        return True

    def _send_desktop(self, title, message):
        # Send desktop notification based on operating system
        system = platform.system()
        try:
            # Escape double quotes for shell execution
            escaped_msg = message.replace('"', '\\"')
            escaped_title = title.replace('"', '\\"')
            
            if system == "Darwin":  # macOS
                applescript = f'display notification "{escaped_msg}" with title "{escaped_title}" sound name "Glass"'
                subprocess.run(["osascript", "-e", applescript], check=True, timeout=5)
                
            elif system == "Windows":  # Windows
                # Powershell script to trigger native Windows balloon/toast notification
                ps_cmd = (
                    f'[void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms"); '
                    f'$toast = New-Object System.Windows.Forms.NotifyIcon; '
                    f'$toast.Icon = [System.Drawing.SystemIcons]::Information; '
                    f'$toast.BalloonTipText = "{escaped_msg}"; '
                    f'$toast.BalloonTipTitle = "{escaped_title}"; '
                    f'$toast.Visible = $True; '
                    f'$toast.ShowBalloonTip(5000)'
                )
                subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=5)

            elif system == "Linux":
                # notify-send is provided by libnotify on most desktop Linux
                # distributions. Terminal alerts remain the fallback on a
                # headless machine or when libnotify is not installed.
                notify_send = shutil.which("notify-send")
                if notify_send:
                    subprocess.run([
                        notify_send,
                        "--app-name=Kite Strategy Monitor",
                        "--urgency=critical",
                        "--expire-time=10000",
                        title,
                        message,
                    ], check=True, timeout=5)
        except Exception as e:
            print(f"  └─ Failed to send {system} notification: {e}")

# Singleton instance
notifier = Notifier()
