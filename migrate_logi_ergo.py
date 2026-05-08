#!/usr/bin/env python3
"""
migrate_logi_ergo.py
====================

Migrate Logi Options+ button configuration from a Logitech MX Ergo (model 6b01d)
to an MX Ergo S (model 2b03e), by editing the Logi Options+ settings.db.

Supports both macOS and Windows 11. The platform is auto-detected.

  macOS:   ~/Library/Application Support/LogiOptionsPlus/settings.db
  Windows: %LOCALAPPDATA%\\LogiOptionsPlus\\settings.db

What this does, in order:
  1. Discovers the live `settings.db` and prints a migration plan.
  2. Asks for confirmation, with per-item prompts for the non-obvious mappings.
  3. Cleanly stops Logi Options+ processes/services so they don't hold the DB.
  4. Waits until the DB is free for exclusive access. Aborts otherwise.
  5. Backs up settings.db (and -shm/-wal) plus macros.db to a timestamped folder.
  6. Edits settings.db (WAL-checkpointed before and after, for safety).
  7. Restarts Logi Options+ services and reopens the GUI.

Modes:
  python migrate_logi_ergo.py                    # interactive menu
  python migrate_logi_ergo.py --apply            # preset MX Ergo → MX Ergo S migration
  python migrate_logi_ergo.py --apply -y         # same, assume yes to all prompts
  python migrate_logi_ergo.py --restore PATH     # restore from a backup folder

Logs are written to stdout AND to <backup_dir>/migration.log when --apply runs.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Guard macOS-only import
if sys.platform == "darwin":
    import plistlib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROFILE_KEY = "profile-420fd454-0c36-499d-bde4-146823b16147"
OLD_PREFIX = "mx-ergo-6b01d"
NEW_PREFIX = "mx-ergo-s-2b03e"
BACKUP_DIR_PREFIX = "_migration_backup_"

# Human-readable device name lookup (best-effort)
DEVICE_NAMES: dict[str, str] = {
    "mx-ergo-6b01d": "MX Ergo",
    "mx-ergo-s-2b03e": "MX Ergo S",
    "m575-6b027": "M575 Trackball",
    "r500-6b505": "R500 Presenter",
    "c920-082d": "C920 Webcam",
    "c920-08e5": "C920 Webcam (2)",
    "radial-menu-virtual-device-10000000": "Radial Menu (virtual)",
}

# --- macOS-specific constants ---

# Plist search locations on macOS
MACOS_PLIST_LOCATIONS = [
    Path.home() / "Library" / "LaunchAgents",
    Path("/Library/LaunchAgents"),
    Path("/Library/LaunchDaemons"),
]
MACOS_PLIST_GLOB = "com.logi*.plist"

# Process names to pkill if any survive bootout. The driverkit extension
# (com.logi.optionsplus.hidfilter) is intentionally NOT in this list — it's a
# system extension that doesn't hold settings.db open and is awkward to restart.
MACOS_KILL_PATTERNS = [
    "logioptionsplus_agent",
    "Logi Options+",
    "LogiPluginService",
    "LogiRightSight",
    "logioptionsplus_updater",
    "logi_crashpad_handler",
]

# --- Windows-specific constants ---

# Windows process names for Logi Options+ (without .exe suffix)
WIN_PROCESS_NAMES = [
    "logioptionsplus",          # GUI
    "logioptionsplus_agent",
    "logioptionsplus_appbroker",
    "logioptionsplus_updater",
    "logi_crashpad_handler",
]

WIN_SERVICE_NAME = "OptionsPlusUpdaterService"
WIN_AGENT_EXE = Path(r"C:\Program Files\LogiOptionsPlus\logioptionsplus_agent.exe")
WIN_GUI_EXE = Path(r"C:\Program Files\LogiOptionsPlus\logioptionsplus.exe")

# ---------------------------------------------------------------------------
# Migration plan
# ---------------------------------------------------------------------------


@dataclass
class PlanItem:
    """One element of the migration plan.

    `mode` controls how the source slot is applied to the destination slot:
      - "full":         deep-copy the entire `card` from src to dst, rewrite slotId
      - "pointer_speed": copy only mouseSettings.pointerSpeed.value (preserve
                         everything else on the destination, e.g. cpsSlotId)
    `interactive` means the script must ask before applying this item.
    """

    src_button: str
    dst_button: str
    mode: str
    description: str
    interactive: bool = False


PLAN: list[PlanItem] = [
    PlanItem("c82", "c82", "full",
             "Middle button (your custom macro)"),
    PlanItem("c83", "c83", "full",
             "Thumb-back: your custom gesture (overrides default 'Back')"),
    PlanItem("c86", "c86", "full",
             "Thumb-forward: your custom gesture (overrides default 'Forward')"),
    PlanItem("c91", "c91", "full",
             "Wheel left tilt: your custom gesture (overrides default 'Scroll Left')"),
    PlanItem("c93", "c93", "full",
             "Wheel right tilt: your custom gesture (overrides default 'Scroll Right')"),
    # Same physical button (top-of-device), different control ID after firmware change.
    # Will replace the new device's "DPI cycle" with your gesture config.
    PlanItem("c237", "c253", "full",
             "Top-of-device button: copy old gesture config "
             "(REPLACES default DPI-cycle behavior)",
             interactive=True),
    # Pointer speed value migration only — preserve cpsSlotId on the new device.
    PlanItem("mouse_settings", "mouse_settings", "pointer_speed",
             "Pointer speed value only (preserves Ergo S's cpsSlotId link to c253)",
             interactive=True),
]

SKIP_NOTES = [
    ("virtual_precision_mode (old only)",
     "card body is empty (precisionMode: {}); nothing to migrate"),
    ("mouse_scroll_wheel_settings",
     "both devices have dir=STANDARD; new device has additional thumbwheel config "
     "we'd lose by overwriting"),
    ("thumb_wheel_adapter (new only)",
     "new physical hardware on the Ergo S; no source on the old device"),
]


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

log = logging.getLogger("migrate_logi_ergo")


def setup_logging(logfile: Optional[Path] = None) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    datefmt = "%H:%M:%S"
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    log.addHandler(sh)
    if logfile is not None:
        fh = logging.FileHandler(logfile)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        log.addHandler(fh)


# Tiny coloring helpers (no external deps)
def _c(code: str, msg: str) -> str:
    return f"\033[{code}m{msg}\033[0m" if sys.stdout.isatty() else msg


def green(m): return _c("32", m)
def yellow(m): return _c("33", m)
def red(m): return _c("31", m)
def bold(m): return _c("1", m)


# ---------------------------------------------------------------------------
# User prompts
# ---------------------------------------------------------------------------

def confirm(prompt: str, default_no: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        log.info("%s -> yes (auto)", prompt)
        return True
    suffix = "[y/N]" if default_no else "[Y/n]"
    while True:
        try:
            ans = input(f"{prompt} {suffix} ").strip().lower()
        except EOFError:
            return False
        if not ans:
            return not default_no
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


# ---------------------------------------------------------------------------
# Platform abstraction
# ---------------------------------------------------------------------------


class Platform(ABC):
    """Abstract interface for platform-specific operations."""

    @abstractmethod
    def get_lop_dir(self) -> Path:
        """Return the Logi Options+ data directory."""

    @property
    def settings_db(self) -> Path:
        return self.get_lop_dir() / "settings.db"

    @property
    def macros_db(self) -> Path:
        return self.get_lop_dir() / "macros.db"

    @abstractmethod
    def stop_logi_options(self, assume_yes: bool) -> Any:
        """Stop Logi Options+ processes/services. Returns context for restart."""

    @abstractmethod
    def start_logi_options(self, stop_context: Any) -> None:
        """Restart Logi Options+ processes/services using context from stop."""

    @abstractmethod
    def wait_db_free(self, db: Path, timeout_s: int = 20) -> None:
        """Wait until the DB file is not held by any process."""


def _run(cmd: list[str], *, capture: bool = True, check: bool = False) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


# ---------------------------------------------------------------------------
# macOS platform
# ---------------------------------------------------------------------------


@dataclass
class BootedOut:
    plist: Path
    label: str
    domain_target: str          # e.g. "gui/501/com.logi.optionsplus"
    bootstrap_domain: str       # e.g. "gui/501" or "system"
    needs_sudo: bool





class MacOSPlatform(Platform):

    def get_lop_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "LogiOptionsPlus"

    @staticmethod
    def _read_plist_label(path: Path) -> Optional[str]:
        try:
            with path.open("rb") as f:
                d = plistlib.load(f)
            v = d.get("Label")
            return str(v) if v else None
        except Exception as e:
            log.debug("could not read Label from %s: %s", path, e)
            return None

    @staticmethod
    def _find_logi_plists() -> list[Path]:
        found: list[Path] = []
        for d in MACOS_PLIST_LOCATIONS:
            if d.is_dir():
                try:
                    found.extend(sorted(d.glob(MACOS_PLIST_GLOB)))
                except PermissionError:
                    pass
        return found

    def stop_logi_options(self, assume_yes: bool) -> list[BootedOut]:
        """Quit GUI, bootout launchd jobs, kill stragglers. Returns booted-out jobs."""
        log.info("Quitting Logi Options+ GUI (osascript)…")
        _run(["osascript", "-e", 'tell application "Logi Options+" to quit'], check=False)

        plists = self._find_logi_plists()
        log.info("Found %d Logi launchd plist(s):", len(plists))
        for p in plists:
            log.info("  %s", p)

        if not plists:
            log.warning("No com.logi*.plist files found. The agent may not auto-respawn — proceeding.")

        booted_out: list[BootedOut] = []
        uid = os.getuid()

        for p in plists:
            label = self._read_plist_label(p) or p.stem
            location = str(p.parent)
            if location.startswith(str(Path.home())):
                domain_target = f"gui/{uid}/{label}"
                bootstrap_domain = f"gui/{uid}"
                needs_sudo = False
            elif location == "/Library/LaunchAgents":
                domain_target = f"gui/{uid}/{label}"
                bootstrap_domain = f"gui/{uid}"
                needs_sudo = True
            else:  # /Library/LaunchDaemons
                domain_target = f"system/{label}"
                bootstrap_domain = "system"
                needs_sudo = True

            cmd = ["launchctl", "bootout", domain_target]
            if needs_sudo:
                cmd = ["sudo"] + cmd
            cp = _run(cmd, check=False)
            if cp.returncode == 0:
                log.info("  %s bootout OK: %s", green("✓"), label)
            else:
                log.info("  %s bootout (rc=%d, may already be unloaded): %s",
                         yellow("•"), cp.returncode, label)
            booted_out.append(BootedOut(p, label, domain_target, bootstrap_domain, needs_sudo))

        # Belt-and-braces pkill for any survivors
        for pat in MACOS_KILL_PATTERNS:
            cp = _run(["pkill", "-f", pat], check=False)
            if cp.returncode == 0:
                log.info("  killed: %s", pat)

        return booted_out

    def start_logi_options(self, stop_context: Any) -> None:
        booted_out: list[BootedOut] = stop_context
        log.info("Bootstrapping launchd jobs back…")
        for bo in booted_out:
            cmd = ["launchctl", "bootstrap", bo.bootstrap_domain, str(bo.plist)]
            if bo.needs_sudo:
                cmd = ["sudo"] + cmd
            cp = _run(cmd, check=False)
            if cp.returncode == 0:
                log.info("  %s bootstrap OK: %s", green("✓"), bo.label)
            else:
                log.info("  %s bootstrap rc=%d (often means already loaded): %s",
                         yellow("•"), cp.returncode, bo.label)

        log.info("Opening Logi Options+…")
        cp = _run(["open", "-a", "Logi Options+"], check=False)
        if cp.returncode != 0:
            log.warning("`open -a 'Logi Options+'` failed (rc=%d). Launch it manually.",
                        cp.returncode)

    def wait_db_free(self, db: Path, timeout_s: int = 20) -> None:
        log.info("Waiting for %s to be released (lsof)…", db.name)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            cp = _run(["lsof", "--", str(db)], check=False)
            if cp.returncode != 0:
                log.info("  %s %s is free.", green("✓"), db.name)
                return
            time.sleep(1)
        cp = _run(["lsof", "--", str(db)], check=False)
        raise SystemExit(red(
            f"Timed out waiting for {db} to be released. Still held by:\n{cp.stdout}\n"
            "Stop the holding process and retry."
        ))


# ---------------------------------------------------------------------------
# Windows platform
# ---------------------------------------------------------------------------


def _is_admin() -> bool:
    """Check if the current process has admin privileges (Windows)."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
    except Exception:
        return False


class WindowsPlatform(Platform):

    def get_lop_dir(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA", "")) / "LogiOptionsPlus"

    def stop_logi_options(self, assume_yes: bool) -> dict:
        """Kill Logi Options+ processes and stop the updater service.

        Returns a dict with service state info for restart.
        """
        context: dict[str, Any] = {"service_was_running": False}

        # 1. Stop the Windows service (requires admin)
        if _is_admin():
            log.info("Stopping %s service…", WIN_SERVICE_NAME)
            cp = _run(["sc.exe", "query", WIN_SERVICE_NAME], check=False)
            if cp.returncode == 0 and "RUNNING" in (cp.stdout or ""):
                context["service_was_running"] = True
                cp = _run(["sc.exe", "stop", WIN_SERVICE_NAME], check=False)
                if cp.returncode == 0:
                    log.info("  %s service stop requested", green("✓"))
                    # Wait for the service to actually stop
                    for _ in range(10):
                        time.sleep(1)
                        cp2 = _run(["sc.exe", "query", WIN_SERVICE_NAME], check=False)
                        if "STOPPED" in (cp2.stdout or ""):
                            log.info("  %s service stopped", green("✓"))
                            break
                else:
                    log.warning("  sc.exe stop failed (rc=%d). Continuing anyway.", cp.returncode)
            else:
                log.info("  Service not running or not found — continuing.")
        else:
            log.warning("Not running as admin — cannot stop %s service.", WIN_SERVICE_NAME)
            log.warning("The service may auto-restart the agent. Consider running as admin.")

        # 2. Kill all Logi processes via taskkill
        time.sleep(1)  # brief pause after service stop
        for name in WIN_PROCESS_NAMES:
            cp = _run(["taskkill", "/F", "/IM", f"{name}.exe"], check=False)
            if cp.returncode == 0:
                log.info("  %s killed: %s", green("✓"), name)
            else:
                log.debug("  %s not running or already stopped: %s", yellow("•"), name)

        time.sleep(1)  # let file handles close
        return context

    def start_logi_options(self, stop_context: Any) -> None:
        context: dict = stop_context

        # 1. Restart the service if it was running
        if context.get("service_was_running"):
            if _is_admin():
                log.info("Starting %s service…", WIN_SERVICE_NAME)
                cp = _run(["sc.exe", "start", WIN_SERVICE_NAME], check=False)
                if cp.returncode == 0:
                    log.info("  %s service start requested", green("✓"))
                else:
                    log.warning("  sc.exe start failed (rc=%d).", cp.returncode)
            else:
                log.warning("Cannot restart service without admin. Start Logi Options+ manually.")
                return
        else:
            # Service wasn't running; just start the agent directly
            if WIN_AGENT_EXE.exists():
                log.info("Starting logioptionsplus_agent…")
                subprocess.Popen([str(WIN_AGENT_EXE)],
                                 creationflags=subprocess.DETACHED_PROCESS)  # type: ignore[attr-defined]

        # 2. Wait a moment then launch the GUI
        time.sleep(2)
        if WIN_GUI_EXE.exists():
            log.info("Opening Logi Options+ GUI…")
            subprocess.Popen([str(WIN_GUI_EXE)],
                             creationflags=subprocess.DETACHED_PROCESS)  # type: ignore[attr-defined]
        else:
            log.warning("Logi Options+ GUI not found at %s. Launch it manually.", WIN_GUI_EXE)

    def wait_db_free(self, db: Path, timeout_s: int = 20) -> None:
        log.info("Waiting for %s to be released…", db.name)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                # Try to open the DB with an exclusive lock
                con = sqlite3.connect(db)
                con.execute("PRAGMA locking_mode=EXCLUSIVE;")
                con.execute("BEGIN EXCLUSIVE;")
                con.rollback()
                con.close()
                log.info("  %s %s is free.", green("✓"), db.name)
                return
            except sqlite3.OperationalError:
                time.sleep(1)
        raise SystemExit(red(
            f"Timed out waiting for {db} to be released.\n"
            "Close all Logi Options+ processes and retry."
        ))


def get_platform() -> Platform:
    """Auto-detect and return the appropriate platform instance."""
    if sys.platform == "darwin":
        return MacOSPlatform()
    elif sys.platform == "win32":
        return WindowsPlatform()
    else:
        raise SystemExit(red(f"Unsupported platform: {sys.platform}"))


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def load_settings(db: Path) -> tuple[int, dict]:
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        row = con.execute("SELECT _id, file FROM data").fetchone()
        if row is None:
            raise RuntimeError("settings.db has no row in `data`")
        return row[0], json.loads(row[1])
    finally:
        con.close()


def save_settings(db: Path, row_id: int, data: dict) -> None:
    blob = json.dumps(data, separators=(",", ":"))
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("UPDATE data SET file = ? WHERE _id = ?", (blob, row_id))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        con.close()


def find_assignment(assignments: list[dict], slot_id: str) -> Optional[int]:
    for i, a in enumerate(assignments):
        if a.get("slotId") == slot_id:
            return i
    return None


def apply_plan(data: dict, plan: list[PlanItem]) -> tuple[list[str], list[tuple[str, str]]]:
    """Mutates `data` in place. Returns (applied_descriptions, skipped_with_reason)."""
    profile = data[PROFILE_KEY]
    assignments = profile["assignments"]
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []

    for item in plan:
        src_sid = f"{OLD_PREFIX}_{item.src_button}"
        dst_sid = f"{NEW_PREFIX}_{item.dst_button}"
        si = find_assignment(assignments, src_sid)
        di = find_assignment(assignments, dst_sid)

        if si is None:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"src slot {src_sid} not found"))
            continue
        if di is None:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"dst slot {dst_sid} not found"))
            continue

        if item.mode == "full":
            cloned = copy.deepcopy(assignments[si])
            cloned["slotId"] = dst_sid    # rewrite slotId to point at dst button
            assignments[di] = cloned
            applied.append(f"{src_sid}  ->  {dst_sid}  (full card replace)")

        elif item.mode == "pointer_speed":
            try:
                src_val = (assignments[si]["card"]["mouseSettings"]
                           ["pointerSpeed"]["active"]["value"])
            except (KeyError, TypeError):
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "src has no pointerSpeed.active.value"))
                continue
            try:
                dst_card = assignments[di]["card"]
                dst_card["mouseSettings"]["pointerSpeed"]["active"]["value"] = src_val
            except (KeyError, TypeError):
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "dst has unexpected mouseSettings shape"))
                continue
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (pointerSpeed value -> {src_val})"
            )

        else:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"unknown mode '{item.mode}'"))

    return applied, skipped


def describe_plan_item(data: dict, item: PlanItem) -> str:
    """One-line current state for display."""
    profile = data[PROFILE_KEY]
    src_sid = f"{OLD_PREFIX}_{item.src_button}"
    dst_sid = f"{NEW_PREFIX}_{item.dst_button}"
    si = find_assignment(profile["assignments"], src_sid)
    di = find_assignment(profile["assignments"], dst_sid)
    src_card = (profile["assignments"][si].get("cardId", "<missing>")
                if si is not None else "<missing>")
    dst_card = (profile["assignments"][di].get("cardId", "<missing>")
                if di is not None else "<missing>")
    return (f"  {item.src_button:>22s} -> {item.dst_button:<6s}  "
            f"[{item.mode}]  {src_card}  ->  {dst_card}\n"
            f"  {'':>22s}    {'':<6s}            {item.description}")


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

def make_backup(plat: Platform) -> Path:
    lop_dir = plat.get_lop_dir()
    settings_db = plat.settings_db
    macros_db = plat.macros_db
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = lop_dir / f"{BACKUP_DIR_PREFIX}{ts}"
    bdir.mkdir(parents=True, exist_ok=False)
    files_to_back_up = [
        settings_db,
        settings_db.with_name(settings_db.name + "-shm"),
        settings_db.with_name(settings_db.name + "-wal"),
        macros_db,
        macros_db.with_name(macros_db.name + "-shm"),
        macros_db.with_name(macros_db.name + "-wal"),
    ]
    for f in files_to_back_up:
        if f.exists():
            shutil.copy2(f, bdir / f.name)
            log.info("  backed up %s", f.name)
    return bdir


def restore_from_backup(plat: Platform, backup_dir: Path, assume_yes: bool) -> None:
    lop_dir = plat.get_lop_dir()
    if not backup_dir.is_dir():
        raise SystemExit(red(f"Backup dir not found: {backup_dir}"))
    setup_logging()
    log.info("Restoring from %s", backup_dir)
    files = list(backup_dir.glob("settings.db*")) + list(backup_dir.glob("macros.db*"))
    if not files:
        raise SystemExit(red(f"No settings.db / macros.db files in {backup_dir}"))
    for f in files:
        log.info("  will restore %s -> %s", f.name, lop_dir / f.name)
    if not confirm("Stop Logi Options+ and restore these files?",
                   default_no=True, assume_yes=assume_yes):
        log.info("Aborted.")
        return
    stop_ctx = plat.stop_logi_options(assume_yes)
    try:
        plat.wait_db_free(plat.settings_db)
        for f in files:
            shutil.copy2(f, lop_dir / f.name)
            log.info("  restored %s", f.name)
    finally:
        plat.start_logi_options(stop_ctx)
    log.info(green("Restore complete."))


# ---------------------------------------------------------------------------
# Device discovery & interactive helpers
# ---------------------------------------------------------------------------


def discover_devices(data: dict) -> dict[str, list[str]]:
    """Scan all slotId prefixes and group slot suffixes per device.

    Returns {prefix: [suffix, ...]} where suffix is e.g. "c82", "mouse_settings".
    """
    profile = data.get(PROFILE_KEY, {})
    assignments = profile.get("assignments", [])
    devices: dict[str, list[str]] = {}
    for a in assignments:
        sid = a.get("slotId", "")
        # Try to split on the last _ before a known suffix
        # Device prefixes can contain hyphens and digits
        # Slot IDs look like: "mx-ergo-6b01d_c82" or "mx-ergo-6b01d_mouse_settings"
        # Strategy: find the longest known prefix, or heuristically split
        matched = False
        for prefix in sorted(DEVICE_NAMES.keys(), key=len, reverse=True):
            if sid.startswith(prefix + "_"):
                suffix = sid[len(prefix) + 1:]
                devices.setdefault(prefix, []).append(suffix)
                matched = True
                break
        if not matched:
            # Heuristic: find the split point — look for _c\d+ or known settings suffixes
            import re
            m = re.match(r'^(.+?)_(c\d+|mouse_settings|mouse_scroll_wheel_settings|'
                         r'virtual_precision_mode|thumb_wheel_adapter|presenter_settings|'
                         r'webcam_\w+|camera_\w+)$', sid)
            if m:
                prefix, suffix = m.group(1), m.group(2)
                devices.setdefault(prefix, []).append(suffix)
            else:
                devices.setdefault(sid, []).append("")
    return devices


def device_display_name(prefix: str) -> str:
    return DEVICE_NAMES.get(prefix, prefix)


def print_device_list(devices: dict[str, list[str]]) -> None:
    print(bold("\n  Devices found in settings.db:\n"))
    for i, (prefix, slots) in enumerate(sorted(devices.items()), 1):
        name = device_display_name(prefix)
        buttons = [s for s in slots if s.startswith("c")]
        settings = [s for s in slots if not s.startswith("c") and s]
        print(f"    {i}. {name:<28s} ({prefix})")
        print(f"       {len(buttons)} button(s), {len(settings)} setting(s)")
    print()


def print_device_config(data: dict, prefix: str) -> None:
    """Print all button/setting assignments for a given device prefix."""
    profile = data.get(PROFILE_KEY, {})
    assignments = profile.get("assignments", [])
    name = device_display_name(prefix)
    print(bold(f"\n  Configuration for {name} ({prefix}):\n"))

    for a in assignments:
        sid = a.get("slotId", "")
        if not sid.startswith(prefix + "_"):
            continue
        suffix = sid[len(prefix) + 1:]
        card = a.get("card", {})
        card_id = a.get("cardId", "")

        # Extract a human-readable description from the card
        card_name = card.get("name", card_id)
        macro = card.get("macro", {})
        action = ""
        if macro:
            if macro.get("type") == "SYSTEM":
                action = macro.get("actionName", "")
            elif macro.get("type") == "MOUSE":
                mouse_act = macro.get("mouse", {}).get("action", "")
                action = mouse_act
            elif macro.get("type") == "KEYBOARD":
                keys = macro.get("actionName", "")
                action = keys
            else:
                action = f"macro:{macro.get('type', '?')}"

        # Check for gestures/nested cards
        if "nestedCards" in card:
            action = "gesture (multi-action)"
        if card.get("attribute") == "ADAPTER_4WAYS":
            action = "4-way gesture"

        desc = action if action else card_name
        print(f"    {suffix:<35s}  {desc}")
    print()


def compare_devices(data: dict, src_prefix: str, dst_prefix: str) -> None:
    """Show a side-by-side comparison of two devices' configs."""
    profile = data.get(PROFILE_KEY, {})
    assignments = profile.get("assignments", [])

    src_slots: dict[str, dict] = {}
    dst_slots: dict[str, dict] = {}
    for a in assignments:
        sid = a.get("slotId", "")
        if sid.startswith(src_prefix + "_"):
            suffix = sid[len(src_prefix) + 1:]
            src_slots[suffix] = a
        elif sid.startswith(dst_prefix + "_"):
            suffix = sid[len(dst_prefix) + 1:]
            dst_slots[suffix] = a

    all_suffixes = sorted(set(list(src_slots.keys()) + list(dst_slots.keys())))
    src_name = device_display_name(src_prefix)
    dst_name = device_display_name(dst_prefix)

    print(bold(f"\n  Comparison: {src_name} → {dst_name}\n"))
    print(f"    {'Slot':<30s}  {'Source':<30s}  {'Destination':<30s}  Match?")
    print(f"    {'─' * 30}  {'─' * 30}  {'─' * 30}  {'─' * 6}")

    for suffix in all_suffixes:
        src_a = src_slots.get(suffix)
        dst_a = dst_slots.get(suffix)
        src_desc = src_a.get("cardId", "—") if src_a else "—"
        dst_desc = dst_a.get("cardId", "—") if dst_a else "—"
        # Truncate long card IDs
        src_short = src_desc[:28] + ".." if len(src_desc) > 30 else src_desc
        dst_short = dst_desc[:28] + ".." if len(dst_desc) > 30 else dst_desc
        match = green("  ✓") if src_desc == dst_desc and src_a and dst_a else yellow("  ≠") if src_a and dst_a else red("  ✗")
        print(f"    {suffix:<30s}  {src_short:<30s}  {dst_short:<30s}{match}")
    print()


def build_dynamic_plan(data: dict, src_prefix: str, dst_prefix: str) -> list[PlanItem]:
    """Build a migration plan from matching slots between two devices."""
    profile = data.get(PROFILE_KEY, {})
    assignments = profile.get("assignments", [])

    src_buttons: set[str] = set()
    dst_buttons: set[str] = set()
    for a in assignments:
        sid = a.get("slotId", "")
        if sid.startswith(src_prefix + "_"):
            src_buttons.add(sid[len(src_prefix) + 1:])
        elif sid.startswith(dst_prefix + "_"):
            dst_buttons.add(sid[len(dst_prefix) + 1:])

    common = sorted(src_buttons & dst_buttons)
    plan: list[PlanItem] = []
    for suffix in common:
        if suffix.startswith("c"):
            plan.append(PlanItem(suffix, suffix, "full",
                                 f"Button {suffix} (full card copy)"))
        elif suffix == "mouse_settings":
            plan.append(PlanItem(suffix, suffix, "pointer_speed",
                                 "Pointer speed value only",
                                 interactive=True))
        # Skip settings slots like mouse_scroll_wheel_settings, thumb_wheel_adapter, etc.

    src_only = sorted(src_buttons - dst_buttons)
    dst_only = sorted(dst_buttons - src_buttons)

    if src_only:
        print(yellow(f"\n  Source-only slots (no target): {', '.join(src_only)}"))
    if dst_only:
        print(yellow(f"  Destination-only slots (will keep defaults): {', '.join(dst_only)}"))

    return plan


def list_backups(plat: Platform) -> list[Path]:
    """List available migration backups."""
    lop_dir = plat.get_lop_dir()
    backups = sorted(lop_dir.glob(f"{BACKUP_DIR_PREFIX}*"), reverse=True)
    if not backups:
        print(yellow("\n  No backups found.\n"))
        return []
    print(bold("\n  Available backups:\n"))
    for i, b in enumerate(backups, 1):
        # Count files in backup
        files = list(b.glob("*.db*"))
        has_log = (b / "migration.log").exists()
        extra = " (has migration.log)" if has_log else ""
        print(f"    {i}. {b.name}  — {len(files)} file(s){extra}")
        print(f"       {b}")
    print()
    return backups


def pick_number(prompt: str, max_val: int) -> Optional[int]:
    """Ask user to pick a number 1..max_val, or 0/empty to cancel."""
    while True:
        try:
            raw = input(f"  {prompt} (1-{max_val}, 0=cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw or raw == "0":
            return None
        try:
            n = int(raw)
            if 1 <= n <= max_val:
                return n
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {max_val}.")


def run_interactive(plat: Platform) -> None:
    """Interactive menu for device discovery, config viewing, migration, and backup."""
    setup_logging()
    settings_db = plat.settings_db

    if not settings_db.exists():
        raise SystemExit(red(f"settings.db not found at {settings_db}"))

    platform_name = "macOS" if sys.platform == "darwin" else "Windows"

    while True:
        print()
        print(bold("  ╔══════════════════════════════════════════════════╗"))
        print(bold("  ║  Logi Options+ Config Migration Tool            ║"))
        print(bold(f"  ║  Platform: {platform_name:<10s}  |  DB: settings.db     ║"))
        print(bold("  ╚══════════════════════════════════════════════════╝"))
        print()
        print("    1. List all devices & their button configs")
        print("    2. View detailed config for a device")
        print("    3. Compare two devices (source → destination)")
        print("    4. Migrate config (source → destination)")
        print("    5. Preset migration: MX Ergo → MX Ergo S")
        print("    6. Backup current settings")
        print("    7. Restore from backup")
        print("    8. List available backups")
        print("    0. Exit")
        print()

        try:
            choice = input("  Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0" or not choice:
            break

        _, data = load_settings(settings_db)
        devices = discover_devices(data)
        dev_list = sorted(devices.keys())

        if choice == "1":
            print_device_list(devices)

        elif choice == "2":
            print_device_list(devices)
            n = pick_number("Select device", len(dev_list))
            if n is not None:
                print_device_config(data, dev_list[n - 1])

        elif choice == "3":
            print_device_list(devices)
            print("  Select SOURCE device:")
            s = pick_number("Source", len(dev_list))
            if s is None:
                continue
            print("  Select DESTINATION device:")
            d = pick_number("Destination", len(dev_list))
            if d is None:
                continue
            compare_devices(data, dev_list[s - 1], dev_list[d - 1])

        elif choice == "4":
            print_device_list(devices)
            print("  Select SOURCE device:")
            s = pick_number("Source", len(dev_list))
            if s is None:
                continue
            print("  Select DESTINATION device:")
            d = pick_number("Destination", len(dev_list))
            if d is None:
                continue
            src_prefix = dev_list[s - 1]
            dst_prefix = dev_list[d - 1]
            if src_prefix == dst_prefix:
                print(red("  Source and destination must be different."))
                continue
            plan = build_dynamic_plan(data, src_prefix, dst_prefix)
            if not plan:
                print(yellow("  No matching buttons found between these devices."))
                continue
            # Show plan
            print(bold(f"\n  Migration plan: {device_display_name(src_prefix)} → "
                       f"{device_display_name(dst_prefix)}\n"))
            accepted: list[PlanItem] = []
            for item in plan:
                line = f"    {item.src_button:>20s} → {item.dst_button:<10s}  [{item.mode}]  {item.description}"
                print(line)
                if item.interactive:
                    if not confirm("      -> include this?", default_no=False):
                        print("      skipped.")
                        continue
                accepted.append(item)

            if not accepted:
                print(yellow("  Nothing to migrate."))
                continue

            if not confirm(bold(f"\n  Stop Logi Options+, apply {len(accepted)} change(s), "
                                "and restart? (a backup will be made)"),
                           default_no=True):
                print("  Aborted.")
                continue

            _execute_migration(plat, accepted, src_prefix, dst_prefix)

        elif choice == "5":
            # Preset migration
            _run_preset_migration(plat, data, assume_yes=False)

        elif choice == "6":
            if not confirm("  Create a backup of current settings?", default_no=False):
                continue
            log.info("Creating backup…")
            bdir = make_backup(plat)
            log.info("  %s backup saved to: %s", green("✓"), bdir)

        elif choice == "7":
            backups = list_backups(plat)
            if not backups:
                continue
            n = pick_number("Select backup to restore", len(backups))
            if n is not None:
                restore_from_backup(plat, backups[n - 1], assume_yes=False)

        elif choice == "8":
            list_backups(plat)

        else:
            print(yellow("  Invalid choice."))


def _execute_migration(plat: Platform, plan: list[PlanItem],
                       src_prefix: str, dst_prefix: str) -> None:
    """Execute a migration plan: stop app, backup, edit DB, restart."""
    settings_db = plat.settings_db
    stop_ctx = plat.stop_logi_options(False)
    try:
        plat.wait_db_free(settings_db)

        log.info("Creating backup…")
        bdir = make_backup(plat)
        log.info("  backup dir: %s", bdir)
        fh = logging.FileHandler(bdir / "migration.log")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)
        log.info("Backup complete.")

        row_id, data2 = load_settings(settings_db)
        applied, skipped = apply_plan_dynamic(data2, plan, src_prefix, dst_prefix)
        if not applied:
            log.warning("Nothing was applied. Skipping write.")
        else:
            save_settings(settings_db, row_id, data2)
            log.info(green(f"Wrote {len(applied)} change(s) to settings.db:"))
            for a in applied:
                log.info("  %s", a)
        for sid, why in skipped:
            log.warning("  skipped %s — %s", sid, why)

    finally:
        plat.start_logi_options(stop_ctx)

    log.info("")
    log.info(green("Done."))
    log.info("If anything looks wrong, restore with:")
    log.info("    python %s --restore '%s'", Path(__file__).name, bdir)


def apply_plan_dynamic(data: dict, plan: list[PlanItem],
                       src_prefix: str, dst_prefix: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Like apply_plan but uses dynamic prefixes instead of OLD_PREFIX/NEW_PREFIX."""
    profile = data[PROFILE_KEY]
    assignments = profile["assignments"]
    applied: list[str] = []
    skipped: list[tuple[str, str]] = []

    for item in plan:
        src_sid = f"{src_prefix}_{item.src_button}"
        dst_sid = f"{dst_prefix}_{item.dst_button}"
        si = find_assignment(assignments, src_sid)
        di = find_assignment(assignments, dst_sid)

        if si is None:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"src slot {src_sid} not found"))
            continue
        if di is None:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"dst slot {dst_sid} not found"))
            continue

        if item.mode == "full":
            cloned = copy.deepcopy(assignments[si])
            cloned["slotId"] = dst_sid
            assignments[di] = cloned
            applied.append(f"{src_sid}  ->  {dst_sid}  (full card replace)")

        elif item.mode == "pointer_speed":
            try:
                src_val = (assignments[si]["card"]["mouseSettings"]
                           ["pointerSpeed"]["active"]["value"])
            except (KeyError, TypeError):
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "src has no pointerSpeed.active.value"))
                continue
            try:
                dst_card = assignments[di]["card"]
                dst_card["mouseSettings"]["pointerSpeed"]["active"]["value"] = src_val
            except (KeyError, TypeError):
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "dst has unexpected mouseSettings shape"))
                continue
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (pointerSpeed value -> {src_val})"
            )
        else:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"unknown mode '{item.mode}'"))

    return applied, skipped


def _run_preset_migration(plat: Platform, data: dict, assume_yes: bool) -> None:
    """Run the hardcoded MX Ergo → MX Ergo S preset migration."""
    settings_db = plat.settings_db

    log.info(bold("=== Logi Options+ MX Ergo → MX Ergo S migration ==="))
    log.info("settings.db: %s", settings_db)
    log.info("")

    # Build the actual plan, asking about interactive items
    log.info(bold("Plan:"))
    accepted_plan: list[PlanItem] = []
    for item in PLAN:
        line = describe_plan_item(data, item)
        log.info(line)
        if item.interactive:
            if not confirm("    -> include this?", default_no=False, assume_yes=assume_yes):
                log.info("    skipped by user.")
                continue
        accepted_plan.append(item)

    log.info("")
    log.info(bold("Will skip (informational):"))
    for name, reason in SKIP_NOTES:
        log.info("  %s — %s", name, reason)
    log.info("")

    if not confirm(bold("Stop Logi Options+, edit settings.db, and restart? "
                        "(a backup will be made)"),
                   default_no=True, assume_yes=assume_yes):
        log.info("Aborted by user.")
        return

    # Execute
    stop_ctx = plat.stop_logi_options(assume_yes)
    try:
        plat.wait_db_free(settings_db)

        log.info("Creating backup…")
        bdir = make_backup(plat)
        log.info("  backup dir: %s", bdir)
        fh = logging.FileHandler(bdir / "migration.log")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(fh)
        log.info("Backup complete.")

        row_id, data2 = load_settings(settings_db)
        applied, skipped = apply_plan(data2, accepted_plan)
        if not applied:
            log.warning("Nothing was applied. Skipping write.")
        else:
            save_settings(settings_db, row_id, data2)
            log.info(green(f"Wrote {len(applied)} change(s) to settings.db:"))
            for a in applied:
                log.info("  %s", a)
        for sid, why in skipped:
            log.warning("  skipped %s — %s", sid, why)

    finally:
        plat.start_logi_options(stop_ctx)

    log.info("")
    log.info(green("Done."))
    log.info("If anything looks wrong, restore with:")
    log.info("    python %s --restore '%s'", Path(__file__).name, bdir)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true",
                   help="preset MX Ergo → MX Ergo S migration (asks for confirmations)")
    g.add_argument("--restore", metavar="BACKUP_DIR",
                   help="restore settings.db / macros.db from a backup folder")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="assume 'yes' to all confirmations")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    plat = get_platform()

    if args.restore:
        restore_from_backup(plat, Path(args.restore).expanduser(), assume_yes=args.yes)
        return

    settings_db = plat.settings_db
    if not settings_db.exists():
        raise SystemExit(red(f"settings.db not found at {settings_db}"))

    if args.apply:
        # Preset migration mode (backward compatible)
        setup_logging()
        _, data = load_settings(settings_db)
        _run_preset_migration(plat, data, assume_yes=args.yes)
    else:
        # Interactive menu mode (new default)
        run_interactive(plat)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.stderr.write(red("Interrupted.\n"))
        sys.exit(130)
