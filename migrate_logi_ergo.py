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
import re
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
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

# Fallback only. The profile key is resolved from the document at runtime
# (see resolve_profile); this constant is used when the document names no profile.
PROFILE_KEY = "profile-420fd454-0c36-499d-bde4-146823b16147"

# Set by --dry-run. When true, nothing is written and no process is stopped.
DRY_RUN = False

# Set by --archive. When set, every backup is also packed into one file.
ARCHIVE_FORMAT: Optional[str] = None
OLD_PREFIX = "mx-ergo-6b01d"
NEW_PREFIX = "mx-ergo-s-2b03e"
BACKUP_DIR_PREFIX = "_migration_backup_"

# Backups from two machines end up side by side in the same folder once they are
# copied around, and a macOS backup restored onto Windows would write the wrong
# schema. The name says where it came from.
OS_TAG = {"darwin": "macos", "win32": "windows", "linux": "linux"}

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


class FilePlatform(Platform):
    """A settings.db given on the command line, with no running application.

    This is what makes the tool usable on a machine that has no Logi Options+
    install: a copy of someone else's database, a backup, or a test fixture.
    It also makes Linux and WSL usable, where the application does not exist.
    Nothing is stopped and nothing is started, because nothing is running.
    """

    def __init__(self, db: Path):
        self._db = db.expanduser().resolve()

    def get_lop_dir(self) -> Path:
        return self._db.parent

    @property
    def settings_db(self) -> Path:
        return self._db

    def stop_logi_options(self, assume_yes: bool) -> None:
        log.info("offline mode (--db): no process is stopped")
        return None

    def start_logi_options(self, stop_context: Any) -> None:
        log.info("offline mode (--db): no process is started")

    def wait_db_free(self, db: Path, timeout_s: int = 20) -> None:
        if not db.exists():
            raise SystemExit(red(f"{db} does not exist"))
        log.info("  offline mode: assuming %s is free", db.name)


def get_platform(db: Optional[Path] = None) -> Platform:
    """Return the platform implementation for this run."""
    if db is not None:
        return FilePlatform(db)
    if sys.platform == "darwin":
        return MacOSPlatform()
    elif sys.platform == "win32":
        return WindowsPlatform()
    else:
        raise SystemExit(red(
            f"Unsupported platform: {sys.platform}.\n"
            "Logi Options+ has no build for it. To read or edit a database copied "
            "from another machine, pass --db PATH."))


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def load_settings(db: Path, for_write: bool = True) -> tuple[int, dict]:
    """Read the configuration document out of the single blob row.

    `for_write=False` opens the file read-only and skips the checkpoint, so the
    menu's inspection items never touch a database the application is using.
    A checkpoint is a write: it rewrites the main file from the log.
    """
    if not for_write:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT _id, file FROM data").fetchone()
                if row is None:
                    raise RuntimeError("settings.db has no row in `data`")
                return row[0], json.loads(row[1])
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            # A database with an unread write-ahead log cannot always be opened
            # read-only. Fall through and say so rather than fail.
            log.debug("read-only open failed (%s); falling back", e)
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
    if DRY_RUN:
        log.info(yellow(f"dry run: not writing {len(blob)} bytes to {db}"))
        return
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("UPDATE data SET file = ? WHERE _id = ?", (blob, row_id))
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        con.close()


def profile_keys(data: dict) -> list[str]:
    """Every profile key in the document, most-likely-first.

    `profile_keys` is the document's own index. Fall back to a scan of the
    top-level keys, then to the historical constant. The default profile carries
    the same identifier on independent installations, but a configuration can
    hold several profiles, one per application, so the index is what decides.
    """
    keys: list[str] = []
    for k in data.get("profile_keys", []) or []:
        if isinstance(k, str) and k in data:
            keys.append(k)
    for k in data:
        if k.startswith("profile-") and k not in keys:
            keys.append(k)
    if PROFILE_KEY in data and PROFILE_KEY not in keys:
        keys.append(PROFILE_KEY)
    return keys


def resolve_profile(data: dict, key: Optional[str] = None) -> dict:
    """Return the profile object that holds `assignments`.

    Raises SystemExit with an actionable message when the document names none,
    instead of the bare KeyError the caller used to get after the app was
    already stopped.
    """
    if key is not None:
        if key not in data:
            raise SystemExit(red(f"No profile {key!r} in this settings.db"))
        return data[key]
    for k in profile_keys(data):
        prof = data.get(k)
        if isinstance(prof, dict) and isinstance(prof.get("assignments"), list):
            return prof
    raise SystemExit(red(
        "No profile with an `assignments` list found in this settings.db.\n"
        f"Top-level keys that look like profiles: {[k for k in data if k.startswith('profile')] or 'none'}"))


def find_assignment(assignments: list[dict], slot_id: str) -> Optional[int]:
    for i, a in enumerate(assignments):
        if a.get("slotId") == slot_id:
            return i
    return None


# Presentation fields that belong to the destination slot, not to the copied
# behaviour. A newer device carries icons and preset tags that an older source
# does not have, and a blind whole-card replace silently drops them.
PRESENTATION_FIELDS = ("icons", "tags", "name")


def retarget_refs(node: Any, src_prefix: str, dst_prefix: str) -> Any:
    """Rewrite every nested slot reference from the source device to the destination.

    A copied card can name other slots of its own device, for example
    `mouseSettings.cpsSlotId`. Rewriting only the top-level slotId leaves those
    pointing back at the source device.
    """
    if isinstance(node, dict):
        return {k: retarget_refs(v, src_prefix, dst_prefix) for k, v in node.items()}
    if isinstance(node, list):
        return [retarget_refs(v, src_prefix, dst_prefix) for v in node]
    if isinstance(node, str) and node.startswith(src_prefix + "_"):
        return dst_prefix + "_" + node[len(src_prefix) + 1:]
    return node


def copy_card(src_assignment: dict, dst_assignment: dict,
              src_prefix: str, dst_prefix: str, dst_sid: str) -> dict:
    """Build the new destination assignment from the source, keeping what is the
    destination's own: its slot id and its presentation fields."""
    cloned = copy.deepcopy(src_assignment)
    cloned = retarget_refs(cloned, src_prefix, dst_prefix)
    cloned["slotId"] = dst_sid
    dst_card = dst_assignment.get("card", {})
    new_card = cloned.get("card")
    if isinstance(new_card, dict) and isinstance(dst_card, dict):
        for field in PRESENTATION_FIELDS:
            if field not in new_card and field in dst_card:
                new_card[field] = copy.deepcopy(dst_card[field])
    return cloned


# A Smart Action assignment is a card whose attribute is MACRO_REF; its `id` is
# the UUID of the macro that lives in macros.db. Ordinary cards also carry UUID
# shaped ids, so the attribute is what identifies the reference, not the shape.
MACRO_REF_ATTRIBUTE = "MACRO_REF"


def macro_refs(node: Any) -> set:
    """Every Smart Action UUID a card references.

    The macro itself lives in macros.db. A copy of the card without the macro
    leaves a reference to nothing.
    """
    found = set()
    if isinstance(node, dict):
        if node.get("attribute") == MACRO_REF_ATTRIBUTE and isinstance(node.get("id"), str):
            found.add(node["id"])
        for v in node.values():
            found |= macro_refs(v)
    elif isinstance(node, list):
        for v in node:
            found |= macro_refs(v)
    return found


def load_macro_ids(macros_db: Path) -> set:
    """UUIDs of the Smart Actions defined in a macros.db."""
    if not macros_db.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{macros_db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        con = sqlite3.connect(macros_db)
    try:
        row = con.execute("SELECT file FROM data").fetchone()
    finally:
        con.close()
    if row is None:
        return set()
    blob = row[0]
    if isinstance(blob, (bytes, bytearray)):
        blob = blob.decode("utf-8", "replace")
    doc = json.loads(blob)
    return {m["id"] for m in doc.get("macro_infos", {}).get("macroInfos", [])
            if isinstance(m, dict) and isinstance(m.get("id"), str)}


def deep_merge(src: Any, dst: Any) -> Any:
    """Source values win, destination-only keys survive.

    This is what makes a settings slot safe to migrate. A whole-card replace
    drops the fields a newer device has and an older one never had, such as the
    thumbwheel block on the MX Ergo S.
    """
    if isinstance(src, dict) and isinstance(dst, dict):
        out = copy.deepcopy(dst)
        for k, v in src.items():
            out[k] = deep_merge(v, dst[k]) if k in dst else copy.deepcopy(v)
        return out
    return copy.deepcopy(src)


def pointer_speed_key(card: dict) -> Optional[str]:
    """Which pointer-speed schema this card uses.

    macOS stores `active.value`, a float from 0 to 1. Windows stores
    `active.dpiLevel`, an index into the device's DPI steps. They are different
    quantities, so a value from one platform must never be written to the other.
    """
    active = (card.get("mouseSettings", {})
                  .get("pointerSpeed", {})
                  .get("active", {}))
    if not isinstance(active, dict):
        return None
    for key in ("value", "dpiLevel"):
        if key in active:
            return key
    return None


def apply_plan(data: dict, plan: list[PlanItem]) -> tuple[list[str], list[tuple[str, str]]]:
    """Mutates `data` in place. Returns (applied_descriptions, skipped_with_reason)."""
    profile = resolve_profile(data)
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
            assignments[di] = copy_card(assignments[si], assignments[di],
                                        OLD_PREFIX, NEW_PREFIX, dst_sid)
            applied.append(f"{src_sid}  ->  {dst_sid}  (full card replace)")

        elif item.mode == "pointer_speed":
            src_card = assignments[si].get("card", {})
            dst_card = assignments[di].get("card", {})
            src_key = pointer_speed_key(src_card)
            dst_key = pointer_speed_key(dst_card)
            if src_key is None:
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "src card has no pointerSpeed.active"))
                continue
            if dst_key is None:
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "dst card has no pointerSpeed.active"))
                continue
            if src_key != dst_key:
                skipped.append((
                    f"{item.src_button}->{item.dst_button}",
                    f"pointer speed schema differs: src uses {src_key!r}, "
                    f"dst uses {dst_key!r}. macOS stores a 0..1 float, Windows a "
                    f"DPI step index. Set it by hand in Logi Options+."))
                continue
            src_val = src_card["mouseSettings"]["pointerSpeed"]["active"][src_key]
            dst_card["mouseSettings"]["pointerSpeed"]["active"][dst_key] = src_val
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (pointerSpeed {dst_key} -> {src_val})"
            )

        elif item.mode == "merge":
            merged = deep_merge(assignments[si].get("card", {}),
                                assignments[di].get("card", {}))
            assignments[di]["card"] = retarget_refs(merged, OLD_PREFIX, NEW_PREFIX)
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (merged, destination-only fields kept)")

        else:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"unknown mode '{item.mode}'"))

    return applied, skipped


def describe_plan_item(data: dict, item: PlanItem) -> str:
    """One-line current state for display."""
    profile = resolve_profile(data)
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

def document_host(data: dict, os_type: str) -> Optional[str]:
    """The name of the host running `os_type`, as the device itself recorded it.

    Easy-Switch stores one entry per paired computer, each with the operating
    system of that host. That is the right source for labelling a backup: the
    machine the configuration belongs to, not the machine reading the file.
    """
    easy = data.get("easy_switch", {})
    for entry in (easy.get("devices", []) if isinstance(easy, dict) else []):
        device = entry.get("device", entry) if isinstance(entry, dict) else {}
        for host in device.get("hosts", []) if isinstance(device, dict) else []:
            if not isinstance(host, dict):
                continue
            if host.get("os", {}).get("type") == os_type and host.get("name"):
                return str(host["name"])
    return None


def machine_tag(db: Optional[Path] = None) -> str:
    """`<os>-<host>`, safe for a filename, for labelling a backup.

    The operating system is the one the *configuration* was written on, not the
    one the tool happens to run on: a Windows database copied to a Linux box and
    backed up there is still a Windows backup. The card vocabulary in the
    document decides; the running platform is only the fallback.
    """
    host = ""
    try:
        import platform as _platform          # stdlib; no network, no socket import
        host = _platform.node() or ""
    except Exception:                          # noqa: BLE001 - a label is never worth failing for
        host = ""
    host = re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-").lower()[:24]
    os_name = OS_TAG.get(sys.platform, sys.platform)
    if db is not None and db.exists():
        try:
            _, doc = load_settings(db, for_write=False)
            detected = document_platform(doc)
            if detected:
                os_name = OS_TAG.get({"WINDOWS": "win32", "MACOS": "darwin"}[detected], os_name)
                recorded = document_host(doc, detected)
                if recorded:
                    host = re.sub(r"[^A-Za-z0-9]+", "-", recorded).strip("-").lower()[:24]
                elif OS_TAG.get(sys.platform) != os_name:
                    host = ""                  # do not label foreign data with this host
        except Exception:                      # noqa: BLE001 - a label never fails a backup
            pass
    return f"{os_name}-{host}" if host else os_name


def make_backup(plat: Platform) -> Path:
    lop_dir = plat.get_lop_dir()
    settings_db = plat.settings_db
    macros_db = plat.macros_db
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = machine_tag(settings_db)
    bdir = lop_dir / f"{BACKUP_DIR_PREFIX}{tag}_{ts}"
    # Two runs inside one second would otherwise collide, and the failure lands
    # after the application has been stopped, leaving it stopped.
    for attempt in range(1, 100):
        try:
            bdir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            bdir = lop_dir / f"{BACKUP_DIR_PREFIX}{tag}_{ts}-{attempt}"
    else:
        raise SystemExit(red(f"could not create a backup directory under {lop_dir}"))
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
    # A checksum list travels with the data, so a copy can be verified later.
    manifest = bdir / "MANIFEST.sha256"
    lines = []
    for f in sorted(bdir.rglob("*")):
        if f.is_file() and f != manifest:
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            lines.append(f"{digest}  {f.relative_to(bdir)}")
    manifest.write_text("\n".join(lines) + "\n")
    if ARCHIVE_FORMAT:
        archive_backup(bdir, ARCHIVE_FORMAT)
    return bdir


ARCHIVE_FORMATS = ("7z", "zip", "tgz")
DEFAULT_ARCHIVE_FORMAT = "7z"

# Where a 7-Zip binary is usually found. The tool needs no dependency for zip or
# tgz, which are standard library; 7z is the one format that needs a program.
SEVENZIP_CANDIDATES = ("7z", "7za", "7zz", "7zr")
SEVENZIP_WINDOWS = (
    Path("/mnt/c/Program Files/7-Zip/7z.exe"),
    Path("/mnt/c/Program Files (x86)/7-Zip/7z.exe"),
    Path("C:/Program Files/7-Zip/7z.exe"),
)


def find_7zip() -> Optional[str]:
    """A 7-Zip binary, from PATH or a standard Windows install."""
    for name in SEVENZIP_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in SEVENZIP_WINDOWS:
        if candidate.exists():
            return str(candidate)
    return None


def _arg_for(binary: str, path: Path) -> str:
    """Render a path the way the chosen binary expects it.

    A Windows 7z.exe called from WSL cannot read `/home/...`; it needs the
    Windows form of the same location.
    """
    if binary.lower().endswith(".exe") and sys.platform != "win32":
        cp = _run(["wslpath", "-w", str(path)])
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    return str(path)


def archive_backup(bdir: Path, fmt: str) -> Path:
    """Pack a backup directory into one file, for copying to a drive or a phone.

    `tgz` is written in GNU format on purpose. Python defaults to PAX, and
    7-Zip cannot read the PAX extended headers, which is exactly how a macOS
    `tar` archive fails to open on Windows.
    """
    if fmt not in ARCHIVE_FORMATS:
        raise SystemExit(red(f"unknown archive format {fmt!r}; use one of {ARCHIVE_FORMATS}"))
    if not bdir.is_dir():
        raise SystemExit(red(f"not a directory: {bdir}"))
    files = sorted(f for f in bdir.rglob("*") if f.is_file())
    if fmt == "7z":
        binary = find_7zip()
        if binary is None:
            raise SystemExit(red(
                "no 7-Zip binary found. Install p7zip (`sudo apt install p7zip-full`) "
                "or 7-Zip for Windows, or choose --archive zip, which needs nothing."))
        out = bdir.with_suffix(bdir.suffix + ".7z")
        out.unlink(missing_ok=True)
        cp = _run([binary, "a", "-t7z", "-mx=9", "-bso0", "-bsp0",
                   _arg_for(binary, out), _arg_for(binary, bdir)])
        if cp.returncode != 0 or not out.exists():
            raise SystemExit(red(
                f"7-Zip failed (exit {cp.returncode}).\n  {cp.stdout.strip()}\n  {cp.stderr.strip()}"))
    elif fmt == "zip":
        import zipfile
        out = bdir.with_suffix(bdir.suffix + ".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, arcname=str(Path(bdir.name) / f.relative_to(bdir)))
    else:
        import tarfile
        out = bdir.with_suffix(bdir.suffix + ".tar.gz")
        with tarfile.open(out, "w:gz", format=tarfile.GNU_FORMAT) as tar:
            for f in files:
                tar.add(f, arcname=str(Path(bdir.name) / f.relative_to(bdir)))
    total = sum(f.stat().st_size for f in files)
    log.info("  %s archive: %s (%d file(s), %.1f MB -> %.1f MB)",
             green("✓"), out, len(files), total / 1e6, out.stat().st_size / 1e6)
    return out


def restore_from_backup(plat: Platform, backup_dir: Path, assume_yes: bool) -> None:
    lop_dir = plat.get_lop_dir()
    if not backup_dir.is_dir():
        raise SystemExit(red(f"Backup dir not found: {backup_dir}"))
    setup_logging()
    log.info("Restoring from %s", backup_dir)
    # Glob on the real file names. With --db the database need not be called
    # settings.db, and a hardcoded pattern silently restores nothing.
    names = (plat.settings_db.name, plat.macros_db.name)
    files = [f for name in names for f in sorted(backup_dir.glob(name + "*"))]
    if not files:
        raise SystemExit(red(
            f"No {' / '.join(names)} files in {backup_dir}"))
    for f in files:
        log.info("  will restore %s -> %s", f.name, lop_dir / f.name)
    if not confirm("Stop Logi Options+ and restore these files?",
                   default_no=True, assume_yes=assume_yes):
        log.info("Aborted.")
        return
    stop_ctx = None if DRY_RUN else plat.stop_logi_options(assume_yes)
    try:
        if not DRY_RUN:
            plat.wait_db_free(plat.settings_db)
        # Clear the live write-ahead logs first. SQLite replays a -wal left over
        # from a later state on top of the restored file, which silently undoes
        # the restore.
        restored = {f.name for f in files}
        for name in names:
            for suffix in ("-wal", "-shm", "-journal"):
                stale = lop_dir / (name + suffix)
                if stale.exists() and stale.name not in restored:
                    stale.unlink()
                    log.info("  cleared stale %s", stale.name)
        for f in files:
            if DRY_RUN:
                log.info("  dry run: would restore %s", f.name)
                continue
            shutil.copy2(f, lop_dir / f.name)
            log.info("  restored %s", f.name)
    finally:
        if not DRY_RUN:
            plat.start_logi_options(stop_ctx)
    log.info(green("Restore complete."))


# ---------------------------------------------------------------------------
# Device discovery & interactive helpers
# ---------------------------------------------------------------------------


# Slot suffixes seen in real databases. The fallback splitter uses these when a
# device prefix is not in the document's own lists.
SLOT_SUFFIX_RE = re.compile(
    r"^(?P<prefix>.+?)_(?P<suffix>c\d+"
    r"|mouse_settings|mouse_scroll_wheel_settings|virtual_precision_mode"
    r"|thumb_wheel_adapter|backlighting_settings|presenter_settings"
    r"|webcam_\w+_settings|camera_\w+|[a-z][a-z0-9]*(?:_[a-z0-9]+)*_settings)$"
)


def known_prefixes(data: dict) -> list[str]:
    """Device prefixes the document names itself, longest first.

    `slot_prefixes_ever_seen` and `ever_connected_devices` are Logi Options+'s
    own registries. They beat any guess made from the shape of a slot id.
    """
    found: set[str] = set(DEVICE_NAMES)
    for pfx in data.get("slot_prefixes_ever_seen", []) or []:
        if isinstance(pfx, str):
            found.add(pfx)
    ecd = data.get("ever_connected_devices", {})
    for dev in (ecd.get("devices", []) if isinstance(ecd, dict) else []):
        pfx = dev.get("slotPrefix") if isinstance(dev, dict) else None
        if isinstance(pfx, str):
            found.add(pfx)
    return sorted(found, key=len, reverse=True)


def split_slot_id(slot_id: str, prefixes: list[str]) -> tuple[str, str]:
    """Split a slotId into (device prefix, slot suffix)."""
    for pfx in prefixes:
        if slot_id.startswith(pfx + "_"):
            return pfx, slot_id[len(pfx) + 1:]
    m = SLOT_SUFFIX_RE.match(slot_id)
    if m:
        return m.group("prefix"), m.group("suffix")
    return slot_id, ""


def is_button_slot(suffix: str) -> bool:
    """True for a physical control slot such as `c82`, false for a settings slot."""
    return bool(re.fullmatch(r"c\d+", suffix))


def discover_devices(data: dict) -> dict[str, list[str]]:
    """Scan all slotId prefixes and group slot suffixes per device.

    Returns {prefix: [suffix, ...]} where suffix is e.g. "c82", "mouse_settings".
    """
    profile = resolve_profile(data)
    assignments = profile.get("assignments", [])
    prefixes = known_prefixes(data)
    devices: dict[str, list[str]] = {}
    for a in assignments:
        sid = a.get("slotId", "")
        if not sid:
            continue
        prefix, suffix = split_slot_id(sid, prefixes)
        devices.setdefault(prefix, []).append(suffix)
    return devices


def device_display_name(prefix: str) -> str:
    return DEVICE_NAMES.get(prefix, prefix)


def print_device_list(devices: dict[str, list[str]]) -> None:
    print(bold("\n  Devices found in settings.db:\n"))
    for i, (prefix, slots) in enumerate(sorted(devices.items()), 1):
        name = device_display_name(prefix)
        buttons = [s for s in slots if is_button_slot(s)]
        settings = [s for s in slots if s and not is_button_slot(s)]
        print(f"    {i}. {name:<28s} ({prefix})")
        print(f"       {len(buttons)} button(s), {len(settings)} setting(s)")
    print()


def print_device_config(data: dict, prefix: str) -> None:
    """Print all button/setting assignments for a given device prefix."""
    profile = resolve_profile(data)
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
    profile = resolve_profile(data)
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
    profile = resolve_profile(data)
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
        if is_button_slot(suffix):
            plan.append(PlanItem(suffix, suffix, "full",
                                 f"Button {suffix} (full card copy)"))
        elif suffix == "mouse_settings":
            plan.append(PlanItem(suffix, suffix, "pointer_speed",
                                 "Pointer speed value only",
                                 interactive=True))
        else:
            # Every other settings slot the two devices share. `merge` keeps the
            # fields the destination has and the source does not, so a newer
            # device never loses a capability the older one lacks.
            plan.append(PlanItem(suffix, suffix, "merge",
                                 f"{suffix} (merge, destination keeps its own fields)",
                                 interactive=True))

    src_only = sorted(src_buttons - dst_buttons)
    dst_only = sorted(dst_buttons - src_buttons)

    if src_only:
        print(yellow(f"\n  Source-only slots (no target): {', '.join(src_only)}"))
    if dst_only:
        print(yellow(f"  Destination-only slots (will keep defaults): {', '.join(dst_only)}"))

    return plan


def check_macro_refs(data: dict, plan: list[PlanItem], src_prefix: str,
                     dst_prefix: str, macros_db: Path) -> list[tuple[str, str]]:
    """Report every planned copy whose card needs a Smart Action.

    Within one machine both devices read the same macros.db, so the reference
    stays valid. Across machines it does not, which is why this is reported
    rather than assumed.
    """
    profile = resolve_profile(data)
    assignments = profile.get("assignments", [])
    known = load_macro_ids(macros_db)
    problems: list[tuple[str, str]] = []
    for item in plan:
        idx = find_assignment(assignments, f"{src_prefix}_{item.src_button}")
        if idx is None:
            continue
        for mid in sorted(macro_refs(assignments[idx])):
            state = "present in macros.db" if mid in known else red("MISSING from macros.db")
            problems.append((item.src_button, f"needs Smart Action {mid} ({state})"))
    for item in plan:
        idx = find_assignment(assignments, f"{dst_prefix}_{item.dst_button}")
        if idx is None:
            continue
        for mid in sorted(macro_refs(assignments[idx])):
            problems.append((item.dst_button,
                             yellow(f"destination currently holds Smart Action {mid}; "
                                    "this copy replaces it")))
    return problems


def select_items(plan: list[PlanItem], assume_yes: bool = False) -> list[PlanItem]:
    """Let the user drop any item, not only the ones marked interactive."""
    if assume_yes:
        return list(plan)
    print()
    if confirm(f"  Include all {len(plan)} item(s)?", default_no=False):
        return list(plan)
    accepted: list[PlanItem] = []
    for item in plan:
        line = f"    {item.src_button:>22s} -> {item.dst_button:<12s} [{item.mode}]  {item.description}"
        print(line)
        if confirm("      include?", default_no=False):
            accepted.append(item)
        else:
            print("      skipped.")
    return accepted


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
        origin = b.name[len(BACKUP_DIR_PREFIX):].rsplit("_", 1)[0] if b.name.startswith(BACKUP_DIR_PREFIX) else "?"
        print(f"    {i}. {b.name}  — from {origin}, {len(files)} file(s){extra}")
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

        _, data = load_settings(settings_db, for_write=False)
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
            for item in plan:
                print(f"    {item.src_button:>20s} → {item.dst_button:<12s}"
                      f"  [{item.mode}]  {item.description}")
            for pfx in (src_prefix, dst_prefix):
                seen_note = device_presence(data, pfx)
                if seen_note and "NO record" in seen_note:
                    print(yellow(f"    ! {seen_note}"))
            for button, note in check_macro_refs(data, plan, src_prefix, dst_prefix, plat.macros_db):
                print(yellow(f"    ! {button}: {note}"))
            accepted = select_items(plan)

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
    RESULT["action"] = "migrate"
    settings_db = plat.settings_db
    stop_ctx = None if DRY_RUN else plat.stop_logi_options(False)
    try:
        if not DRY_RUN:
            plat.wait_db_free(settings_db)

        if DRY_RUN:
            bdir = plat.get_lop_dir() / "(dry run: no backup created)"
        else:
            log.info("Creating backup…")
            bdir = make_backup(plat)
        log.info("  backup dir: %s", bdir)
        fh = (logging.NullHandler() if DRY_RUN
              else logging.FileHandler(bdir / "migration.log"))
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
            verb = "Would write" if DRY_RUN else "Wrote"
            log.info(green(f"{verb} {len(applied)} change(s) to settings.db:"))
            RESULT.update(ok=True, applied=list(applied), backup=str(bdir),
                          skipped=[f"{a}: {b}" for a, b in skipped])
            for a in applied:
                log.info("  %s", a)
        for sid, why in skipped:
            log.warning("  skipped %s — %s", sid, why)

    finally:
        if not DRY_RUN:
            plat.start_logi_options(stop_ctx)

    log.info("")
    log.info(green("Done."))
    log.info("If anything looks wrong, restore with:")
    log.info("    python %s --restore '%s'", Path(__file__).name, bdir)


def apply_plan_dynamic(data: dict, plan: list[PlanItem],
                       src_prefix: str, dst_prefix: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Like apply_plan but uses dynamic prefixes instead of OLD_PREFIX/NEW_PREFIX."""
    profile = resolve_profile(data)
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
            assignments[di] = copy_card(assignments[si], assignments[di],
                                        src_prefix, dst_prefix, dst_sid)
            applied.append(f"{src_sid}  ->  {dst_sid}  (full card replace)")

        elif item.mode == "pointer_speed":
            src_card = assignments[si].get("card", {})
            dst_card = assignments[di].get("card", {})
            src_key = pointer_speed_key(src_card)
            dst_key = pointer_speed_key(dst_card)
            if src_key is None:
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "src card has no pointerSpeed.active"))
                continue
            if dst_key is None:
                skipped.append((f"{item.src_button}->{item.dst_button}",
                                "dst card has no pointerSpeed.active"))
                continue
            if src_key != dst_key:
                skipped.append((
                    f"{item.src_button}->{item.dst_button}",
                    f"pointer speed schema differs: src uses {src_key!r}, "
                    f"dst uses {dst_key!r}. macOS stores a 0..1 float, Windows a "
                    f"DPI step index. Set it by hand in Logi Options+."))
                continue
            src_val = src_card["mouseSettings"]["pointerSpeed"]["active"][src_key]
            dst_card["mouseSettings"]["pointerSpeed"]["active"][dst_key] = src_val
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (pointerSpeed {dst_key} -> {src_val})"
            )

        elif item.mode == "merge":
            merged = deep_merge(assignments[si].get("card", {}),
                                assignments[di].get("card", {}))
            assignments[di]["card"] = retarget_refs(merged, src_prefix, dst_prefix)
            applied.append(
                f"{src_sid}  ->  {dst_sid}  (merged, destination-only fields kept)")

        else:
            skipped.append((f"{item.src_button}->{item.dst_button}",
                            f"unknown mode '{item.mode}'"))

    return applied, skipped


def _run_preset_migration(plat: Platform, data: dict, assume_yes: bool) -> None:
    """Run the hardcoded MX Ergo → MX Ergo S preset migration."""
    RESULT["action"] = "preset-migrate"
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
    stop_ctx = None if DRY_RUN else plat.stop_logi_options(assume_yes)
    try:
        if not DRY_RUN:
            plat.wait_db_free(settings_db)

        if DRY_RUN:
            bdir = plat.get_lop_dir() / "(dry run: no backup created)"
        else:
            log.info("Creating backup…")
            bdir = make_backup(plat)
        log.info("  backup dir: %s", bdir)
        fh = (logging.NullHandler() if DRY_RUN
              else logging.FileHandler(bdir / "migration.log"))
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
            verb = "Would write" if DRY_RUN else "Wrote"
            log.info(green(f"{verb} {len(applied)} change(s) to settings.db:"))
            RESULT.update(ok=True, applied=list(applied), backup=str(bdir),
                          skipped=[f"{a}: {b}" for a, b in skipped])
            for a in applied:
                log.info("  %s", a)
        for sid, why in skipped:
            log.warning("  skipped %s — %s", sid, why)

    finally:
        if not DRY_RUN:
            plat.start_logi_options(stop_ctx)

    log.info("")
    log.info(green("Done."))
    log.info("If anything looks wrong, restore with:")
    log.info("    python %s --restore '%s'", Path(__file__).name, bdir)


# ---------------------------------------------------------------------------
# Portable layout: export, edit, import
# ---------------------------------------------------------------------------

LAYOUT_VERSION = 1

# USB HID keyboard usage ids. The database stores these raw on every platform,
# which is why a keystroke is the one part of a configuration that ports by
# number. What the number *means* still differs: 227 is the Windows key on
# Windows and Command on macOS.
MODIFIER_IDS = {
    "ctrl": 224, "shift": 225, "alt": 226, "win": 227,
    "rctrl": 228, "rshift": 229, "ralt": 230, "rwin": 231,
}
MODIFIER_NAMES = {v: k for k, v in MODIFIER_IDS.items()}
# macOS names for the same two usages, accepted on input, never emitted.
MODIFIER_IDS.update({"cmd": 227, "rcmd": 231, "opt": 226, "ropt": 230})

HID_KEYS: dict[str, int] = {}


def _build_hid_keys() -> None:
    """Fill HID_KEYS without leaking loop variables into the module namespace."""
    for index, char in enumerate("abcdefghijklmnopqrstuvwxyz"):
        HID_KEYS[char] = 4 + index
    for index, char in enumerate("123456789"):
        HID_KEYS[char] = 30 + index
    for index in range(1, 13):
        HID_KEYS[f"f{index}"] = 57 + index


_build_hid_keys()
HID_KEYS.update({
    "0": 39, "enter": 40, "esc": 41, "backspace": 42, "tab": 43, "space": 44,
    "-": 45, "=": 46, "[": 47, "]": 48, "\\": 49, ";": 51, "'": 52, "`": 53,
    ",": 54, ".": 55, "/": 56, "capslock": 57,
    "printscreen": 70, "scrolllock": 71, "pause": 72, "insert": 73,
    "home": 74, "pageup": 75, "delete": 76, "end": 77, "pagedown": 78,
    "right": 79, "left": 80, "down": 81, "up": 82,
})
HID_CODES = {v: k for k, v in HID_KEYS.items()}

GESTURE_DIRECTIONS = ("click", "up", "down", "left", "right", "horizontal", "vertical")


def _display_character(key: str) -> str:
    named = {"left": "Left", "right": "Right", "up": "Up", "down": "Down",
             "home": "Home", "end": "End", "tab": "Tab", "space": "Space",
             "enter": "Enter", "esc": "Esc", "delete": "Delete",
             "pageup": "PageUp", "pagedown": "PageDown"}
    return named.get(key, key.upper())


def _virtual_key_id(key: str) -> str:
    named = {"left": "VK_LEFT", "right": "VK_RIGHT", "up": "VK_UP", "down": "VK_DOWN",
             "home": "VK_HOME", "end": "VK_END", "tab": "VK_TAB", "space": "VK_SPACE",
             "enter": "VK_RETURN", "esc": "VK_ESCAPE", "delete": "VK_DELETE",
             "`": "VK_GRAVE"}
    return named.get(key, f"VK_{key.upper()}")


def describe_action(card: dict) -> dict:
    """One direction's card, as a portable action.

    Anything the vocabulary below cannot express is kept verbatim under `raw`,
    so an export never silently loses a card it did not understand.
    """
    macro = card.get("macro") or {}
    kind = macro.get("type")
    if card.get("attribute") == MACRO_REF_ATTRIBUTE and isinstance(card.get("id"), str):
        return {"kind": "smart_action", "id": card["id"]}
    if kind == "SYSTEM":
        return {"kind": "system", "action": macro.get("system", {}).get("action")}
    if kind == "MOUSE":
        return {"kind": "mouse", "action": macro.get("mouse", {}).get("action"),
                "hidUsage": macro.get("mouse", {}).get("hidUsage")}
    if kind == "MEDIA":
        return {"kind": "media", "usage": macro.get("media", {}).get("usage")}
    if kind == "QUICK_LAUNCH":
        return {"kind": "quick_launch",
                "action": macro.get("quickLaunch", {}).get("action")}
    if kind == "KEYSTROKE":
        ks = macro.get("keystroke", {})
        code = ks.get("code")
        action = {"kind": "keystroke",
                  "modifiers": [MODIFIER_NAMES.get(m, m) for m in ks.get("modifiers", [])],
                  "key": HID_CODES.get(code, code) if code is not None else None}
        if code is None and ks.get("displayCharacter"):
            # A modifier held on its own, such as the Alt hold a horizontal
            # gesture uses. There is no key code, only a label.
            action["display"] = ks["displayCharacter"]
        return action
    if macro.get("doNothing") is not None or str(card.get("id", "")).endswith("do_nothing"):
        return {"kind": "nothing"}
    if not macro and str(card.get("id", "")).startswith("card_global_presets_"):
        # A preset card carries no macro; the id is the action. Several are
        # platform specific (`_osx_`, `_win_`), so this must not be copied blind.
        return {"kind": "preset", "id": card["id"]}
    return {"kind": "raw", "card": copy.deepcopy(card)}


def keystroke_card(modifiers: list, key: Optional[str],
                   display: Optional[str] = None) -> dict:
    """A user-defined keyboard shortcut, in the shape the application writes.

    Verified byte-identical against a card Logi Options+ produced for the same
    chord: modifiers ascending, `virtualKeyId` present, four tags. `key` may be
    None, which is how a gesture holds a modifier on its own.
    """
    # Keep the order the layout gives. The application records the order the
    # keys were pressed, and a chord is a set, so order carries no behaviour --
    # but preserving it makes an export and re-import byte-identical.
    seen: list = []
    for name in modifiers:
        code = MODIFIER_IDS[name.lower()]
        if code not in seen:
            seen.append(code)
    mods = seen
    if key is None:
        keystroke: dict = {"modifiers": mods}
        if display:
            keystroke["displayCharacter"] = display
    else:
        code = HID_KEYS[key.lower()] if isinstance(key, str) else int(key)
        keystroke = {"code": code,
                     "displayCharacter": _display_character(str(key).lower()),
                     "modifiers": mods,
                     "virtualKeyId": _virtual_key_id(str(key).lower())}
    return {
        "attribute": "MACRO_PLAYBACK",
        "icons": {"icons": ["Shortcut.png", "Shortcut.svg"],
                  "uri": "pipeline://system_actions/"},
        "id": "card_global_presets_keyboard_shortcut",
        "macro": {"actionName": "keyboard_none",
                  "keystroke": keystroke,
                  "type": "KEYSTROKE"},
        "name": "ASSIGNMENT_NAME_KEYBOARD_SHORTCUT",
        "tags": ["PRESET_TAG_KEY_OR_BUTTON", "PRESET_TAG_MACROS_UNSUPPORTED",
                 "PRESET_TAG_PRESENTER_BUTTON", "PRESET_KEYBOARD_FUNCTIONS"],
        "taskId": 65536,
    }


def action_catalogue(data: dict) -> dict:
    """Every non-keystroke card the document already contains, by action.

    Logi Options+ owns the vocabulary: icons, tags and taskId belong to the
    action and cannot be invented. So a system, mouse or media action can only
    be written if this database already holds a card for it somewhere.
    """
    catalogue: dict = {}

    def visit(node):
        if isinstance(node, dict):
            action = describe_action(node) if "macro" in node or "attribute" in node else None
            if action and action["kind"] in ("system", "mouse", "media", "nothing",
                                              "preset", "quick_launch"):
                key = (action["kind"],
                       action.get("action") or action.get("usage") or action.get("id"))
                catalogue.setdefault(key, copy.deepcopy(node))
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(resolve_profile(data))
    return catalogue


# Cross-platform action equivalences, from the two databases measured side by
# side. Only `BUTTON` is shared verbatim between the platforms; everything here
# is a judgement about intent, so translation is opt-in (--translate) and every
# substitution is reported.
ACTION_ALIASES: dict = {
    # macOS -> Windows
    ("system", "MISSION_CONTROL"): {"kind": "system", "action": "TASK_VIEW"},
    ("system", "APP_EXPOSE"): {"kind": "system", "action": "TASK_VIEW"},
    ("system", "SWITCH_BETWEEN_DESKTOPS_LEFT"):
        {"kind": "keystroke", "modifiers": ["ctrl", "win"], "key": "left"},
    ("system", "SWITCH_BETWEEN_DESKTOPS_RIGHT"):
        {"kind": "keystroke", "modifiers": ["ctrl", "win"], "key": "right"},
    ("mouse", "OSX_GESTURE_BACK"): {"kind": "mouse", "action": "WIN_BACK"},
    ("mouse", "OSX_GESTURE_FORWARD"): {"kind": "mouse", "action": "WIN_FORWARD"},
    # Windows -> macOS
    ("system", "TASK_VIEW"): {"kind": "system", "action": "MISSION_CONTROL"},
    ("mouse", "WIN_BACK"): {"kind": "mouse", "action": "OSX_GESTURE_BACK"},
    ("mouse", "WIN_FORWARD"): {"kind": "mouse", "action": "OSX_GESTURE_FORWARD"},
    # Preset cards, which carry the action in the id and no macro block.
    ("preset", "card_global_presets_osx_mission_control"):
        {"kind": "system", "action": "TASK_VIEW"},
    ("preset", "card_global_presets_osx_back"): {"kind": "mouse", "action": "WIN_BACK"},
    ("preset", "card_global_presets_osx_forward"): {"kind": "mouse", "action": "WIN_FORWARD"},
    ("preset", "card_global_presets_win_back"): {"kind": "mouse", "action": "OSX_GESTURE_BACK"},
    ("preset", "card_global_presets_win_forward"):
        {"kind": "mouse", "action": "OSX_GESTURE_FORWARD"},
    # QUICK_LAUNCH actions: macOS window overviews against the Windows one.
    ("quick_launch", "MISSION_CONTROL"): {"kind": "system", "action": "TASK_VIEW"},
    ("quick_launch", "APP_EXPOSE"): {"kind": "system", "action": "TASK_VIEW"},
    ("system", "TASK_VIEW_MAC"): {"kind": "quick_launch", "action": "MISSION_CONTROL"},
}


def document_platform(data: dict) -> Optional[str]:
    """Which platform wrote this configuration, judged by its own vocabulary.

    Preset card ids carry a `_win_` or `_osx_` infix. Counting them is more
    reliable than the running operating system, because the tool routinely
    reads a database copied from the other machine.
    """
    raw = json.dumps(resolve_profile(data))
    wins, macs = raw.count("_win_"), raw.count("_osx_")
    if wins == macs:
        return None
    return "WINDOWS" if wins > macs else "MACOS"


def gesture_wrapper(data: dict, mode: str) -> Optional[dict]:
    """A gesture card for `mode`, taken from any slot in this document that has one.

    A slot currently holding a single action, such as a Smart Action or plain
    scroll, has no gesture structure at all. The application owns that structure,
    so it is borrowed rather than invented.
    """
    found: list = []

    def visit(node):
        if found:
            return
        if isinstance(node, dict):
            nested = node.get("nestedCards")
            if isinstance(nested, dict) and mode in nested and node.get("attribute") == "ONE_OF":
                found.append(node)
                return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(resolve_profile(data))
    return copy.deepcopy(found[0]) if found else None


def render_action(action: dict, catalogue: dict, translate: bool = False,
                  platform_name: Optional[str] = None) -> tuple:
    """Turn a portable action back into a card. Returns (card, reason_if_none)."""
    kind = action.get("kind")
    if kind == "keystroke":
        try:
            return keystroke_card(action.get("modifiers", []), action.get("key"),
                                  action.get("display")), None
        except (KeyError, TypeError, AttributeError) as e:
            return None, f"cannot build this keystroke: {e}"
    if kind == "raw":
        card_id = str(action.get("card", {}).get("id", ""))
        foreign = {"WINDOWS": "_osx_", "MACOS": "_win_"}.get(platform_name or "")
        if foreign and foreign in card_id:
            return None, (f"card {card_id!r} belongs to the other platform and has no "
                          "known equivalent; assign this action once in Logi Options+, "
                          "then re-import")
        return copy.deepcopy(action["card"]), None
    if kind == "smart_action":
        return {"attribute": MACRO_REF_ATTRIBUTE, "id": action["id"]}, None
    if kind in ("system", "mouse", "media", "nothing", "preset", "quick_launch"):
        key = (kind, action.get("action") or action.get("usage") or action.get("id"))
        card = catalogue.get(key)
        if card is not None:
            return copy.deepcopy(card), None
        if translate and key in ACTION_ALIASES:
            alias = ACTION_ALIASES[key]
            card, why = render_action(alias, catalogue, False, platform_name)
            if card is not None:
                label = alias.get("action") or (
                    "+".join(alias.get("modifiers", [])) + "+" + str(alias.get("key")))
                return card, f"translated {key[1]} -> {label}"
            return None, f"{key[1]} translates to {alias}, which this database also lacks"
        hint = ("" if translate else
                "  Pass --translate to substitute the closest action on this platform.")
        what = action.get("action") or action.get("usage") or action.get("id")
        return None, (f"this database has no card for {kind} {what!r}; "
                      f"assign it once in Logi Options+, then re-import.{hint}")
    return None, f"unknown action kind {kind!r}"


def device_presence(data: dict, prefix: str) -> Optional[str]:
    """Warn if this installation has no record of the device.

    Nothing in the file says whether a device is connected right now, so this
    reports what can actually be known: whether the installation has ever seen
    it. An edit for an absent device is still valid, and Logi Options+ picks it
    up the next time the device connects.
    """
    seen = set(data.get("slot_prefixes_ever_seen", []) or [])
    ecd = data.get("ever_connected_devices", {})
    for dev in (ecd.get("devices", []) if isinstance(ecd, dict) else []):
        if isinstance(dev, dict) and isinstance(dev.get("slotPrefix"), str):
            seen.add(dev["slotPrefix"])
    if prefix in seen:
        return (f"{prefix}: this installation has seen this device before. It does not "
                "need to be connected now; Logi Options+ applies the change the next "
                "time it sees it.")
    return (f"{prefix}: this installation has NO record of ever seeing this device. "
            "Its slots may be stale or incomplete, so check the result in Logi "
            "Options+ once the device is connected.")


def export_layout(data: dict, prefix: str) -> dict:
    """The gesture and button layout of one device, as a portable document."""
    profile = resolve_profile(data)
    prefixes = known_prefixes(data)
    slots: dict = {}
    for a in profile.get("assignments", []):
        sid = a.get("slotId", "")
        pfx, suffix = split_slot_id(sid, prefixes)
        if pfx != prefix or not is_button_slot(suffix):
            continue
        card = a.get("card", {})
        mode = card.get("selectedNestedCard")
        if mode and mode in card.get("nestedCards", {}):
            inner = card["nestedCards"][mode].get("nestedCards", {})
            slots[suffix] = {
                "mode": mode,
                "directions": {d: describe_action(inner[d])
                               for d in GESTURE_DIRECTIONS if d in inner},
            }
        else:
            slots[suffix] = {"mode": None, "action": describe_action(card)}
    # JSON has no comments, so the guidance rides in `_comment` keys, which the
    # reader ignores. `target` and `slot_aliases` make the file self-describing:
    # with them set, an import needs no flags at all.
    return {
        "version": LAYOUT_VERSION,
        "_comment": (
            "Layout for one device. Edit `target` to the destination slot prefix and "
            "`slot_aliases` to rename slots whose control id differs between device "
            "generations, then import with no flags: --import-layout FILE"
        ),
        "exported": {
            "from_device": prefix,
            "platform": document_platform(data) or "UNKNOWN",
            "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "device": prefix,
        "target": None,
        "_comment_target": (
            "Destination slot prefix. null means the destination must be given with "
            "--device. Set it here to make the file stand alone."
        ),
        "slot_aliases": {},
        "_comment_slot_aliases": (
            "Source slot -> destination slot, for a button whose control id changed. "
            "Example: {\"c237\": \"c253\"} for the MX Ergo top button on an MX Ergo S. "
            "--map on the command line is merged over whatever is set here."
        ),
        "slots": slots,
    }


def import_layout(data: dict, layout: dict, prefix: str,
                  slot_map: Optional[dict] = None, translate: bool = False) -> tuple:
    """Apply a portable layout to a device. Returns (applied, skipped).

    `slot_map` renames a source slot to the destination's control id, which is
    what a firmware change between device generations requires: the same
    physical button is c237 on the MX Ergo and c253 on the MX Ergo S.
    """
    if layout.get("version") != LAYOUT_VERSION:
        raise SystemExit(red(f"layout version {layout.get('version')} is not supported "
                             f"(this tool writes version {LAYOUT_VERSION})"))
    profile = resolve_profile(data)
    assignments = profile.get("assignments", [])
    catalogue = action_catalogue(data)
    platform_name = document_platform(data)
    aliases = dict(layout.get("slot_aliases") or {})
    aliases.update(slot_map or {})
    applied: list = []
    skipped: list = []
    notes: list = []

    for source_suffix, spec in sorted(layout.get("slots", {}).items()):
        suffix = aliases.get(source_suffix, source_suffix)
        sid = f"{prefix}_{suffix}"
        idx = find_assignment(assignments, sid)
        if idx is None:
            skipped.append((suffix, f"destination has no slot {sid}"))
            continue
        card = assignments[idx].get("card", {})
        mode = spec.get("mode")

        if mode is None:
            new_card, why = render_action(spec.get("action", {}), catalogue, translate, platform_name)
            if new_card is None:
                skipped.append((suffix, why))
                continue
            assignments[idx]["card"] = new_card
            applied.append(f"{sid}  (single action)")
            continue

        nested = card.get("nestedCards", {})
        if mode not in nested:
            borrowed = gesture_wrapper(data, mode)
            if borrowed is None:
                skipped.append((suffix, f"no gesture card for mode {mode!r} exists "
                                        "anywhere in this database"))
                continue
            borrowed = retarget_refs(borrowed, layout.get("device", prefix), prefix)
            assignments[idx]["card"] = borrowed
            assignments[idx]["cardId"] = borrowed.get("id", assignments[idx].get("cardId"))
            card = borrowed
            nested = card.get("nestedCards", {})
            notes.append(f"{suffix}: slot had no gesture structure, borrowed a {mode!r} card")
        target = nested[mode].setdefault("nestedCards", {})
        wrote = []
        for direction, action in spec.get("directions", {}).items():
            new_card, why = render_action(action, catalogue, translate, platform_name)
            if new_card is None:
                skipped.append((f"{suffix}.{direction}", why))
                continue
            if why:
                notes.append(f"{suffix}.{direction}: {why}")
            target[direction] = new_card
            wrote.append(direction)
        if wrote:
            card["selectedNestedCard"] = mode
            applied.append(f"{sid}  [{mode}]  {', '.join(wrote)}")
    for note in notes:
        skipped.append(("note", note))
    return applied, skipped


def _execute_layout_import(plat: Platform, layout: dict, prefix: str,
                           assume_yes: bool, slot_map: Optional[dict] = None,
                           translate: bool = False) -> None:
    """Apply a layout through the same stop, backup, write, restart lifecycle."""
    RESULT["action"] = "import-layout"
    settings_db = plat.settings_db
    row_id, preview = load_settings(settings_db, for_write=False)
    applied, skipped = import_layout(copy.deepcopy(preview), layout, prefix,
                                     slot_map, translate)

    note = device_presence(preview, prefix)
    log.info(bold(f"Layout import: {layout.get('device','?')} -> {prefix}"))
    if note:
        log.warning("  %s %s", yellow("!"), note)
    for line in applied:
        log.info("  %s %s", green("+"), line)
    for slot, why in skipped:
        if slot == "note":
            log.info("  %s %s", yellow("~"), why)
        else:
            log.warning("  %s %s — %s", yellow("!"), slot, why)
    if not applied:
        raise SystemExit(yellow("Nothing to apply."))
    if not confirm(bold(f"Apply {len(applied)} slot change(s)?"),
                   default_no=True, assume_yes=assume_yes):
        log.info("Aborted.")
        return

    stop_ctx = None if DRY_RUN else plat.stop_logi_options(assume_yes)
    try:
        if not DRY_RUN:
            plat.wait_db_free(settings_db)
        if DRY_RUN:
            bdir = plat.get_lop_dir() / "(dry run: no backup created)"
        else:
            log.info("Creating backup…")
            bdir = make_backup(plat)
        log.info("  backup dir: %s", bdir)
        fh = (logging.NullHandler() if DRY_RUN
              else logging.FileHandler(bdir / "migration.log"))
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                          datefmt="%H:%M:%S"))
        log.addHandler(fh)
        try:
            row_id, data = load_settings(settings_db)
            applied, skipped = import_layout(data, layout, prefix, slot_map, translate)
            save_settings(settings_db, row_id, data)
            verb = "Would write" if DRY_RUN else "Wrote"
            log.info(green(f"{verb} {len(applied)} slot change(s)."))
            RESULT.update(ok=True, applied=list(applied), backup=str(bdir),
                          skipped=[f"{a}: {b}" for a, b in skipped])
        finally:
            log.removeHandler(fh)
            fh.close()
    finally:
        if not DRY_RUN:
            plat.start_logi_options(stop_ctx)
    log.info("Restore with:  python3 %s --restore '%s'", Path(__file__).name, bdir)


# ---------------------------------------------------------------------------
# Running against a live Windows install from WSL
# ---------------------------------------------------------------------------

# What the elevated run reports back. Never infer success from an exit code:
# `Start-Process -Verb RunAs` returns whether UAC was accepted, not what the
# elevated process did, so the parent reads this file instead.
RESULT: dict = {"ok": False, "action": None, "applied": [], "skipped": [], "backup": None}

_RC_RE = re.compile(r"^(\S+)_rc=(-?\d+)$")


def is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _powershell(command: str) -> subprocess.CompletedProcess:
    return _run(["powershell.exe", "-NoProfile", "-Command", command])


def _to_windows_path(p: Path) -> str:
    cp = _run(["wslpath", "-w", str(p)])
    if cp.returncode != 0 or not cp.stdout.strip():
        raise SystemExit(red(f"could not convert {p} to a Windows path"))
    return cp.stdout.strip()


def _windows_temp_dir() -> tuple:
    """(path usable from WSL, same path in Windows form)."""
    cp = _powershell("$env:TEMP")
    win_dir = cp.stdout.strip()
    if cp.returncode != 0 or not win_dir:
        raise SystemExit(red("could not read the Windows TEMP directory"))
    cp = _run(["wslpath", "-u", win_dir])
    if cp.returncode != 0 or not cp.stdout.strip():
        raise SystemExit(red(f"could not convert {win_dir} to a WSL path"))
    return Path(cp.stdout.strip()), win_dir


def _find_windows_python() -> Optional[str]:
    """The Windows Python that will run the elevated copy."""
    for probe in (["py.exe", "-3", "-c", "import sys;print(sys.executable)"],
                  ["python.exe", "-c", "import sys;print(sys.executable)"]):
        cp = _run(probe)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    return None


def _read_windows_text(path: Path) -> str:
    """Read a file the elevated Windows console wrote.

    A redirected Python process on Windows writes the ANSI codepage, not UTF-8.
    """
    if not path.exists():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_elevated_on_windows(passthrough: list) -> int:
    """Re-run this script on the Windows side, elevated, and verify by artefact.

    The tool must stop a Windows service and edit a file the running application
    owns, and neither is possible from WSL: `sys.platform` is linux here, and
    the service control needs Administrator. So the same script is copied to the
    Windows temp directory and launched through a UAC prompt.
    """
    if not is_wsl():
        raise SystemExit(red("--elevate is only for running from WSL against Windows"))
    py_exe = _find_windows_python()
    if py_exe is None:
        raise SystemExit(red(
            "no Windows Python found. Install it from python.org or the Microsoft "
            "Store so that `py.exe` or `python.exe` resolves from WSL."))

    write_dir, win_dir = _windows_temp_dir()
    nonce = uuid.uuid4().hex[:12]
    script_copy = write_dir / f"migrate_logi_ergo-{nonce}.py"
    cmd_path = write_dir / f"migrate_logi_ergo-{nonce}.cmd"
    log_path = write_dir / f"migrate_logi_ergo-{nonce}.log"
    result_path = write_dir / f"migrate_logi_ergo-{nonce}.json"

    # Copy rather than run over the WSL network share: an elevated process does not
    # reliably see the WSL share, and the script has no imports outside stdlib.
    shutil.copy2(Path(__file__).resolve(), script_copy)

    # Input files named on the command line live on the Linux side, which the
    # elevated Windows process cannot read. Copy them across and rewrite the
    # argument to the Windows path.
    copied_inputs: list = []
    passthrough = list(passthrough)
    for flag in ("--import-layout",):
        if flag in passthrough:
            i = passthrough.index(flag) + 1
            if i >= len(passthrough):
                raise SystemExit(red(f"{flag} needs a path"))
            source = Path(passthrough[i]).expanduser()
            if not source.is_file():
                raise SystemExit(red(f"{flag}: {source} is not a file"))
            target = write_dir / f"migrate_logi_ergo-{nonce}-{source.name}"
            shutil.copy2(source, target)
            copied_inputs.append(target)
            passthrough[i] = f"{win_dir}\\{target.name}"
    for flag in ("--export-layout", "--restore", "--db"):
        if flag in passthrough:
            raise SystemExit(red(
                f"{flag} cannot be combined with --elevate yet: the elevated process "
                "writes on the Windows side and this would need the result copied back. "
                "Run that command directly from a Windows shell instead."))

    win = {p: f"{win_dir}\\{p.name}" for p in (script_copy, cmd_path, log_path, result_path)}
    argv = " ".join(f'"{a}"' if " " in a else a for a in passthrough)
    lines = [
        f'"{py_exe}" "{win[script_copy]}" {argv} --result-file "{win[result_path]}"'
        f' >> "{win[log_path]}" 2>&1',
        f'echo run_rc=%ERRORLEVEL% >> "{win[log_path]}"',
    ]
    # CRLF, and newline="" so Python does not double the \r already written.
    cmd_path.write_text("\r\n".join(lines) + "\r\n", newline="")

    log.info("Requesting elevation. Approve the UAC prompt on the Windows desktop…")
    ps_out = write_dir / f"migrate_logi_ergo-{nonce}.ps.txt"
    try:
        # Real file handles, never pipes. The elevated run restarts Logi Options+,
        # and a long-lived child that inherits a pipe keeps it open, so
        # capture_output would block here long after the work has finished.
        with open(ps_out, "wb") as sink:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"Start-Process -Verb RunAs -Wait -FilePath '{win[cmd_path]}'"],
                stdin=subprocess.DEVNULL, stdout=sink, stderr=sink)
        if proc.returncode != 0:
            detail = ps_out.read_text(errors="replace").strip() if ps_out.exists() else ""
            raise SystemExit(red(
                "the UAC prompt was declined or could not be shown.\n"
                f"  {detail}"))
        log_text = _read_windows_text(log_path)
        result = json.loads(result_path.read_text()) if result_path.exists() else None
    finally:
        for f in [script_copy, cmd_path, log_path, result_path, ps_out, *copied_inputs]:
            try:
                f.unlink()
            except OSError:
                pass

    for line in log_text.splitlines():
        print("   " + line)

    codes = dict(m.groups() for m in
                 (_RC_RE.match(l.strip()) for l in log_text.splitlines()) if m)
    if "run" not in codes:
        raise SystemExit(red(
            "the elevated run reported no result code. The command file most "
            "likely failed before it could write its log."))
    if result is None:
        raise SystemExit(red(
            f"the elevated run exited with code {codes['run']} but wrote no result "
            "file, so nothing can be confirmed. Treat the configuration as unchanged "
            "and check the log above."))
    if not result.get("ok"):
        log.error(red(f"the elevated run reported failure: {result.get('error')}"))
        return 1

    log.info(green(f"elevated run confirmed: {result.get('action')}"))
    for line in result.get("applied", []):
        log.info("    %s", line)
    if result.get("backup"):
        log.info("  backup: %s", result["backup"])
    return 0


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
    ap.add_argument("--db", metavar="PATH",
                    help="operate on this settings.db instead of the live one. "
                         "No process is stopped or started. Works on any OS, "
                         "including Linux, and on a copy or a backup.")
    g.add_argument("--export-layout", metavar="FILE",
                   help="write one device's button and gesture layout to a "
                        "portable JSON file")
    g.add_argument("--import-layout", metavar="FILE",
                   help="apply a layout JSON file to a device")
    ap.add_argument("--device", metavar="PREFIX",
                    help="device slot prefix for --export-layout / --import-layout, "
                         "for example mx-ergo-s-2b03e")
    ap.add_argument("--map", metavar="OLD=NEW", action="append", default=[],
                    help="rename a slot on import, for example --map c237=c253 "
                         "when a firmware change moved the same physical button. "
                         "Repeatable.")
    ap.add_argument("--translate", action="store_true",
                    help="on import, substitute the closest action of the "
                         "destination platform when the source action does not "
                         "exist there. Every substitution is reported.")
    ap.add_argument("--elevate", action="store_true",
                    help="from WSL, re-run this script on the Windows side behind a "
                         "UAC prompt, then confirm the outcome from the result file "
                         "it writes. Requires -y.")
    ap.add_argument("--result-file", metavar="PATH", help=argparse.SUPPRESS)
    ap.add_argument("--archive", metavar="FORMAT", nargs="?",
                    choices=ARCHIVE_FORMATS, const=DEFAULT_ARCHIVE_FORMAT,
                    help=f"also pack each backup into one file for sharing: "
                         f"{', '.join(ARCHIVE_FORMATS)}. Defaults to "
                         f"{DEFAULT_ARCHIVE_FORMAT} when the flag is given with no value. "
                         f"7z needs a 7-Zip binary; zip and tgz need nothing.")
    ap.add_argument("--archive-backup", metavar="DIR",
                    help="pack an existing backup folder and exit. Use with --archive "
                         f"to choose the format; the default is {DEFAULT_ARCHIVE_FORMAT}.")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print the plan, then stop. Writes nothing "
                         "and stops no process.")
    return ap.parse_args()


def main() -> None:
    global DRY_RUN
    args = parse_args()
    setup_logging()
    DRY_RUN = args.dry_run
    global ARCHIVE_FORMAT
    ARCHIVE_FORMAT = args.archive
    if args.archive_backup:
        archive_backup(Path(args.archive_backup).expanduser(),
                       args.archive or DEFAULT_ARCHIVE_FORMAT)
        return

    if args.elevate:
        if not args.yes:
            raise SystemExit(red(
                "--elevate needs -y. The elevated run happens in its own console "
                "with its output redirected to a log, so it cannot ask you anything."))
        passthrough = [a for a in sys.argv[1:] if a != "--elevate"]
        raise SystemExit(run_elevated_on_windows(passthrough))

    if is_wsl() and not args.db and not args.elevate:
        log.warning(yellow(
            "This is WSL, where Logi Options+ does not run. Use --db PATH to work on "
            "a copy, or --elevate -y to drive the live Windows install."))
    db_override = Path(args.db).expanduser() if args.db else None
    if db_override is not None and not db_override.exists():
        raise SystemExit(red(f"--db path does not exist: {db_override}"))
    plat = get_platform(db_override)
    if DRY_RUN:
        log.info(yellow("dry run: nothing will be written and nothing stopped"))

    if args.restore:
        RESULT["action"] = "restore"
        restore_from_backup(plat, Path(args.restore).expanduser(), assume_yes=args.yes)
        RESULT["ok"] = True
        return

    settings_db = plat.settings_db
    if not settings_db.exists():
        raise SystemExit(red(f"settings.db not found at {settings_db}"))

    if args.import_layout and not args.device:
        # A layout that names its own target needs no flags.
        peek = json.loads(Path(args.import_layout).expanduser().read_text())
        if peek.get("target"):
            args.device = peek["target"]
            log.info("target from the layout file: %s", args.device)

    if args.export_layout or args.import_layout:
        if not args.device:
            _, peek = load_settings(settings_db, for_write=False)
            names = ", ".join(sorted(discover_devices(peek)))
            raise SystemExit(red(
                f"--device is required, or set \"target\" in the layout file.\n"
                f"  Devices in this database: {names}"))

    if args.export_layout:
        _, data = load_settings(settings_db, for_write=False)
        layout = export_layout(data, args.device)
        if not layout["slots"]:
            raise SystemExit(red(f"no button slots found for device {args.device!r}"))
        out = Path(args.export_layout).expanduser()
        out.write_text(json.dumps(layout, indent=2) + "\n")
        log.info(green(f"wrote {len(layout['slots'])} slot(s) to {out}"))
        return

    if args.import_layout:
        layout = json.loads(Path(args.import_layout).expanduser().read_text())
        slot_map = {}
        for pair in args.map:
            if "=" not in pair:
                raise SystemExit(red(f"--map expects OLD=NEW, got {pair!r}"))
            old, new = pair.split("=", 1)
            slot_map[old.strip()] = new.strip()
        _execute_layout_import(plat, layout, args.device, assume_yes=args.yes,
                               slot_map=slot_map, translate=args.translate)
        return

    if args.apply:
        # Preset migration mode (backward compatible)
        _, data = load_settings(settings_db, for_write=False)
        _run_preset_migration(plat, data, assume_yes=args.yes)
    else:
        # Interactive menu mode (new default)
        run_interactive(plat)


def _write_result(path: str) -> None:
    try:
        Path(path).write_text(json.dumps(RESULT, indent=2) + "\n")
    except OSError as e:
        log.error("could not write the result file %s: %s", path, e)


if __name__ == "__main__":
    _result_file = None
    for _i, _a in enumerate(sys.argv):
        if _a == "--result-file" and _i + 1 < len(sys.argv):
            _result_file = sys.argv[_i + 1]
    try:
        main()
    except KeyboardInterrupt:
        RESULT["error"] = "interrupted"
        if _result_file:
            _write_result(_result_file)
        print()
        sys.stderr.write(red("Interrupted.\n"))
        sys.exit(130)
    except SystemExit as e:
        if _result_file and not RESULT.get("ok"):
            RESULT["error"] = str(e) or f"exit {e.code}"
            _write_result(_result_file)
        raise
    except Exception as e:                       # noqa: BLE001 - reported, then re-raised
        RESULT["error"] = f"{type(e).__name__}: {e}"
        if _result_file:
            _write_result(_result_file)
        raise
    else:
        if _result_file:
            _write_result(_result_file)
