# Logi Options+ config migration

`migrate_logi_ergo.py` copies button assignments, gestures, and pointer speed from one
Logitech device to another inside the Logi Options+ database. It runs on macOS and on
Windows 11. It is a single Python file, and it uses the standard library only.

Logi Options+ has no built-in way to move a profile from an old device to a new one. This
tool does that edit directly in the database, with a backup and a one-command restore.

---

## Contents

- [What the tool changes](#what-the-tool-changes)
- [Before you run it](#before-you-run-it)
- [Install and run](#install-and-run)
- [Interactive menu](#interactive-menu)
- [Command line modes](#command-line-modes)
- [What happens during a write](#what-happens-during-a-write)
- [The preset plan](#the-preset-plan)
- [Sharing a backup](#sharing-a-backup)
- [Restore from a backup](#restore-from-a-backup)
- [Code structure](#code-structure)
- [Platform differences in the stored configuration](#platform-differences-in-the-stored-configuration)
- [Layouts: export, edit, import](#layouts-export-edit-import)
- [Translating between Windows and macOS](#translating-between-windows-and-macos)
- [Running against Windows from WSL](#running-against-windows-from-wsl)
- [Known limits](#known-limits)
- [Development notes](#development-notes)

---

## What the tool changes

Logi Options+ keeps every device setting in one SQLite database:

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/LogiOptionsPlus/settings.db` |
| Windows | `%LOCALAPPDATA%\LogiOptionsPlus\settings.db` |

The database holds one table, `data`, with one row. The `file` column of that row holds the
whole configuration as one JSON document. The tool reads the document, edits it in memory,
and writes it back.

Inside the document, a profile object holds an `assignments` list. Each assignment carries a
`slotId` and a `card`. A `slotId` joins a device prefix and a slot suffix with an
underscore:

```
mx-ergo-6b01d_c82              # device prefix "mx-ergo-6b01d", button slot "c82"
mx-ergo-s-2b03e_mouse_settings # device prefix "mx-ergo-s-2b03e", settings slot
```

The `card` holds the behavior of that slot: a system action, a keyboard macro, a mouse
action, or a nested gesture. A migration copies cards from the slots of a source device onto
the slots of a destination device, and rewrites the `slotId` of each copy.

## Before you run it

**Do not edit `settings.db` while Logi Options+ runs. The application overwrites your edit
on exit, and a concurrent write can corrupt the file.** The tool handles this for you: it
stops the application, waits for the file lock to clear, and restarts the application after
the write.

- Run the tool as the user who owns the Logi Options+ profile. The database lives in that
  user's home directory.
- On Windows, open the terminal as Administrator. The tool must stop a Windows service.
- On macOS, the tool can ask for `sudo` when a Logitech job lives in a system domain.
- Pair the new device in Logi Options+ first. The tool can only copy onto slots that
  already exist in the database.

## Install and run

Requirements: Python 3.9 or later. No third-party packages.

```bash
git clone https://github.com/pepsi133/logi_options_config_copy.git
cd logi_options_config_copy
python3 migrate_logi_ergo.py
```

On Windows, use an Administrator terminal:

```powershell
py migrate_logi_ergo.py
```

The command with no arguments opens the interactive menu. Menu items 1, 2, 3, and 8 are
read-only, so you can explore your configuration before you change anything.

## Interactive menu

| Item | Action | Writes? |
|:--:|---|:--:|
| 1 | List every device in `settings.db`, with a button count and a setting count | no |
| 2 | Show every slot of one device, with a readable action name | no |
| 3 | Compare two devices slot by slot | no |
| 4 | Migrate from any source device to any destination device | **yes** |
| 5 | Run the preset MX Ergo to MX Ergo S migration | **yes** |
| 6 | Create a backup of `settings.db` and `macros.db` | writes a backup folder |
| 7 | Restore from a backup folder | **yes** |
| 8 | List the backup folders that exist | no |
| 0 | Exit | no |

Item 4 builds the plan from the slots that both devices share. The tool prints the plan
first, asks about each risky item, and asks once more before it stops the application.

## Command line modes

| Command | Meaning |
|---|---|
| `python3 migrate_logi_ergo.py` | Interactive menu. This is the default. |
| `python3 migrate_logi_ergo.py --apply` | Preset MX Ergo to MX Ergo S migration, with prompts. |
| `python3 migrate_logi_ergo.py --apply -y` | The same preset, with every prompt answered yes. |
| `python3 migrate_logi_ergo.py --restore PATH` | Restore `settings.db` and `macros.db` from a backup folder. |
| `python3 migrate_logi_ergo.py --db PATH` | Work on the database at `PATH` instead of the live one. |
| `python3 migrate_logi_ergo.py --dry-run` | Build and print the plan, then stop. Writes nothing. |
| `… --export-layout FILE --device PREFIX` | Write one device's buttons and gestures to a portable JSON file. |
| `… --import-layout FILE --device PREFIX` | Apply a layout file to a device. |
| `… --import-layout FILE --map c237=c253` | Rename a slot on import, for a button whose control id changed. |
| `… --import-layout FILE --translate` | Substitute the closest action of the destination platform. |
| `… --apply -y --elevate` | From WSL, run against the live Windows install behind a UAC prompt. |
| `… --archive` | Also pack every backup this run makes into one file (7z by default). |
| `--archive-backup DIR [--archive FORMAT]` | Pack a backup folder that already exists, then exit. |

`--apply` and `--restore` exclude each other. `-y` also applies to `--restore`.
`--db` and `--dry-run` combine with any mode.

### Work without a Logi Options+ install

`--db PATH` opens any copy: a backup, a database taken from another machine, or a test
fixture. Nothing is stopped and nothing is started, because nothing is running. This is the
only mode that works on Linux and on WSL, where Logi Options+ does not exist. It is also the
right way to inspect a database while the application runs: copy the file first, sidecars
included, and open the copy.

```bash
python3 migrate_logi_ergo.py --db ./copy/settings.db          # menu, on the copy
python3 migrate_logi_ergo.py --db ./copy/settings.db --apply --dry-run
```

## What happens during a write

Every write path follows the same order:

1. The tool prints the plan and asks for confirmation.
2. The tool stops the Logi Options+ processes and services.
   - macOS: `launchctl bootout` for each `com.logi*` job, then `pkill` for survivors.
   - Windows: `taskkill` for each process, then `sc stop` for `OptionsPlusUpdaterService`.
3. The tool proves that the database is free, and it retries for 20 seconds. macOS asks
   `lsof` which process holds the file. Windows opens an exclusive SQLite transaction. A
   file that stays busy aborts the run, and the tool names the holder.
4. The tool copies `settings.db`, `macros.db`, and their `-wal` and `-shm` sidecar files
   into a timestamped folder named `_migration_backup_YYYYmmdd-HHMMSS`, next to the
   database.
5. The tool checkpoints the write-ahead log, edits the JSON document, writes it back, and
   checkpoints again.
6. The tool restarts every process and service that it stopped.
7. The tool prints the exact `--restore` command for the backup that it just made.

Step 6 runs in a `finally` block, so the application restarts even after a failed edit.
Every line of output also goes to `migration.log` inside the backup folder.

## The preset plan

Menu item 5 and the `--apply` flag run one fixed plan, from `mx-ergo-6b01d` to
`mx-ergo-s-2b03e`:

| Source slot | Destination slot | Mode | Meaning |
|---|---|---|---|
| `c82` | `c82` | full | Middle button |
| `c83` | `c83` | full | Thumb back |
| `c86` | `c86` | full | Thumb forward |
| `c91` | `c91` | full | Wheel tilt left |
| `c93` | `c93` | full | Wheel tilt right |
| `c237` | `c253` | full | Top button. The control id changed with the firmware. This replaces the default DPI cycle, so the tool asks first. |
| `mouse_settings` | `mouse_settings` | pointer_speed | Pointer speed only, and only when both sides use the same schema. The tool asks first. |

The two copy modes are:

- **full**: deep-copy the whole `card` and rewrite the `slotId`.
- **pointer_speed**: copy the active pointer-speed value only, and keep every other field
  of the destination. This preserves the `cpsSlotId` link of the new device.
- **merge**: for a settings slot. Source values win, and every field the destination has
  and the source does not survives. This is what lets a newer device receive an older
  device's settings without losing its own hardware, such as the thumbwheel block on the
  MX Ergo S.

A `full` copy keeps what belongs to the destination: its `slotId`, and its `icons`, `tags`,
and `name` when the source card does not carry them. It also rewrites every nested slot
reference, so a copied card never points back at the source device.

The preset skips three slots on purpose:

- `virtual_precision_mode`, because the source card body is empty.
- `mouse_scroll_wheel_settings`, because both devices already use `STANDARD`, and the new
  device carries extra thumbwheel data that a copy destroys.
- `thumb_wheel_adapter`, because the hardware is new and no source slot exists.

## Sharing a backup

A backup is a folder, which is awkward to copy to a drive or a phone. `--archive` packs
each one into a single file as well, and `--archive-backup` packs a folder that already
exists:

```bash
python3 migrate_logi_ergo.py --apply --archive                     # backup, and a .7z beside it
python3 migrate_logi_ergo.py --archive-backup ./backup-dir         # 7z by default
python3 migrate_logi_ergo.py --archive-backup ./backup-dir --archive zip
```

| Format | Needs | Size of one real backup |
|---|---|---|
| `7z` (default) | a 7-Zip binary | 35 KB |
| `zip` | nothing | 174 KB |
| `tgz` | nothing | 166 KB |

The folder itself is 1.4 MB, so 7z is worth the dependency for anything that has to travel.
It is found on `PATH` (`7z`, `7za`, `7zz`, `7zr`) or at the standard Windows install
location, and a Windows `7z.exe` called from WSL gets Windows-form paths. Without a binary
the tool says so and points at `zip`, which needs nothing.

`zip` opens natively in Windows Explorer and macOS Finder. `tgz` is written in **GNU tar
format on purpose**: Python defaults to POSIX PAX, whose extended headers 7-Zip cannot
read, which is exactly how a macOS `tar` archive fails to open on Windows.

Every backup carries a `MANIFEST.sha256`, so a copy can be checked after it travels:

```bash
cd extracted-backup && sha256sum -c MANIFEST.sha256
```

The manifest excludes itself. A manifest that lists its own checksum can never verify,
because writing the line changes the file it describes.

## Restore from a backup

```bash
python3 migrate_logi_ergo.py --restore "~/Library/Application Support/LogiOptionsPlus/_migration_backup_20260904-011500"
```

Menu item 7 does the same work without a typed path, and menu item 8 lists the folders that exist. The restore stops the application,
copies the files back, and restarts the application.

## Code structure

The whole tool is `migrate_logi_ergo.py`, about 1200 lines, in seven blocks.

| Block | Key names | Purpose |
|---|---|---|
| Constants | `PROFILE_KEY`, `OLD_PREFIX`, `NEW_PREFIX`, `DEVICE_NAMES` | Identifiers of the profile, the preset devices, and the readable names. |
| Plan model | `PlanItem`, `PLAN`, `SKIP_NOTES` | One dataclass per change, plus the preset plan and its documented skips. |
| Platform layer | `Platform`, `MacOSPlatform`, `WindowsPlatform`, `get_platform` | Every operating system difference sits behind one abstract class. |
| Database layer | `load_settings`, `save_settings`, `find_assignment` | JSON in and out of the single blob row, with WAL checkpoints on both sides. |
| Plan engine | `apply_plan`, `apply_plan_dynamic`, `build_dynamic_plan` | Turns plan items into edits of the in-memory document. |
| Discovery and report | `discover_devices`, `print_device_config`, `compare_devices` | Read-only views of the document. |
| Entry points | `main`, `run_interactive`, `_execute_migration`, `_run_preset_migration` | Argument parsing, menu loop, and the two write paths. |

Three ideas carry most of the design:

**The `Platform` abstract class is the only place that knows the operating system.**
It exposes `get_lop_dir`, `settings_db`, `macros_db`, `stop_logi_options`,
`start_logi_options`, and `wait_db_free`. `stop_logi_options` returns a context object, and
`start_logi_options` consumes it, so the restart replays exactly what the stop did. macOS
returns a list of `BootedOut` records with the domain of each job. Windows returns a
dictionary with the process list and the service state. Nothing else in the file branches
on `sys.platform`, except the guarded `plistlib` import and the message in `get_platform`.

**Discovery is textual, not schema-driven.** `discover_devices` splits each `slotId` on the
longest known prefix from `DEVICE_NAMES`. When no name matches, a regular expression splits
on a known suffix shape, such as `c\d+` or `mouse_settings`. This is why an unknown device
still appears in the menu.

**The preset path and the dynamic path share everything except the prefixes.** `apply_plan`
uses the `OLD_PREFIX` and `NEW_PREFIX` constants. `apply_plan_dynamic` takes the two
prefixes as arguments. Both produce the same pair of result lists: the applied descriptions
and the skipped items with a reason.

## Layouts: export, edit, import

A layout file is the portable form of one device's buttons and gestures. It is plain JSON,
meant to be read and edited by hand:

```json
{
  "version": 1,
  "device": "mx-ergo-6b01d",
  "slots": {
    "c86": {
      "mode": "custom_gesture",
      "directions": {
        "click": {"kind": "system", "action": "SWITCH_APPS"},
        "up":    {"kind": "keystroke", "modifiers": ["ctrl", "shift"], "key": "6"},
        "down":  {"kind": "keystroke", "modifiers": ["shift", "win"], "key": "s"},
        "left":  {"kind": "mouse", "action": "WIN_BACK"}
      }
    }
  }
}
```

A keystroke is written from scratch, because the shape is known: the generated card is
byte-identical to one Logi Options+ writes for the same chord. Every other action is
**harvested** from a card the destination database already contains, because the icons,
tags and `taskId` belong to Logitech's vocabulary and cannot be invented. If the
destination has no card for an action, the import says so and skips that direction rather
than writing something the application may reject:

```
! c83.left — this database has no card for system 'SWITCH_BETWEEN_DESKTOPS_LEFT';
             assign it once in Logi Options+, then re-import.
```

Two switches handle the two kinds of mismatch:

- `--map OLD=NEW` renames a slot. The same physical top button is `c237` on the MX Ergo and
  `c253` on the MX Ergo S, so `--map c237=c253` is what carries a layout between the two
  generations.
- `--translate` substitutes the closest action of the destination platform, and reports
  every substitution. Without it, an action that does not exist is skipped, not guessed.

A slot that currently holds a single action, such as a Smart Action or plain scroll, has no
gesture structure at all. The import borrows a gesture card of the right mode from elsewhere
in the same database rather than inventing one, and says so.

## Translating between Windows and macOS

`--translate` uses this table, measured from a Windows database and a macOS database of the
same two devices:

| Intent | macOS | Windows |
|---|---|---|
| All windows overview | `MISSION_CONTROL`, `APP_EXPOSE` (QUICK_LAUNCH) | `TASK_VIEW` |
| Previous / next desktop | `SWITCH_BETWEEN_DESKTOPS_LEFT` / `_RIGHT` | `Ctrl+Win+Left` / `Right` keystroke |
| Back / forward | `OSX_GESTURE_BACK` / `_FORWARD` | `WIN_BACK` / `WIN_FORWARD` |

A card whose id carries the other platform's infix (`_osx_`, `_win_`) is never written
verbatim. The destination platform is judged from the database's own vocabulary, not from
the operating system the tool happens to be running on, because it routinely reads a
database copied from the other machine.

## Running against Windows from WSL

Logi Options+ has no Linux build, and stopping its service needs Administrator. `--elevate`
copies this script to the Windows temp directory, runs it there through a UAC prompt, and
**verifies the outcome from a result file the elevated run writes**:

```bash
python3 migrate_logi_ergo.py --apply -y --dry-run --elevate   # safe rehearsal
python3 migrate_logi_ergo.py --apply -y --elevate             # the real thing
```

`-y` is required: the elevated process runs in its own console with its output redirected,
so it cannot ask you anything.

The result file exists because `Start-Process -Verb RunAs` reports only whether the UAC
prompt was **accepted**, never what the elevated process then did. A run that exits zero but
writes no result is reported as unconfirmed, and the configuration is treated as unchanged.
The script is copied rather than run over `\\wsl.localhost\…`, because an elevated process
does not reliably see the WSL share.

## Platform differences in the stored configuration

These are measured from a Windows database and a macOS database of the same two devices,
not assumed. They are why a copy between operating systems is a translation, not a copy.

| Field | Windows | macOS |
|---|---|---|
| `pointerSpeed.active` | `dpiLevel`, an index into the device DPI steps | `value`, a float from 0 to 1 |
| Preset card ids | `card_global_presets_win_back`, `..._win_forward`, `..._win_horizontal_scroll` | `..._osx_back`, `..._osx_forward`, `..._osx_horizontal_scroll` |
| System actions | `SWITCH_APPS`, `TASK_VIEW`, `MAXIMIZE`, `MINIMIZE`, `WIN_BACK`, `WIN_FORWARD`, `SCROLL_LEFT`, `SCROLL_RIGHT` | `MISSION_CONTROL`, `APP_EXPOSE`, `LAUNCHPAD`, `SWITCH_BETWEEN_DESKTOPS_LEFT`, `SWITCH_BETWEEN_DESKTOPS_RIGHT`, `OSX_GESTURE_BACK`, `OSX_GESTURE_FORWARD` |
| `actionName` | display text such as `"Left Windows + D"` | absent on most cards |

`BUTTON` is the only action symbol the two platforms share. Keystrokes are the portable
part: `code` and `modifiers` are raw USB HID usage ids on both. The catch is that usage
`227` is the Windows key on Windows and Command on macOS, so a chord can port by number and
still mean something else.

The tool refuses the pointer-speed copy when the two sides use different schemas, instead of
writing a DPI index into a float field.

## Smart Actions

A Smart Action is a recorded input sequence. It does **not** live in `settings.db`. The
button assignment is a card whose `attribute` is `MACRO_REF` and whose `id` is a UUID; the
sequence itself lives in `macros.db` under `macro_infos.macroInfos`, tagged with the
platform it was recorded on.

Before any copy, the tool reports every Smart Action involved: one the source card needs,
saying whether `macros.db` actually defines it, and one the destination already has, saying
that the copy will replace it. Both files are backed up and restored together, so a restore
never leaves a reference pointing at nothing.

## Known limits

1. **A Smart Action cannot yet travel between machines.** Within one installation both
   devices read the same `macros.db`, so a copied reference stays valid. Moving a
   configuration to another computer needs the macro carried too, which waits on export and
   import.
2. **The Windows restart uses fixed install paths** under `C:\Program Files\LogiOptionsPlus`.
   A custom install location breaks the restart, but not the edit.
3. **Settings slots migrate by merge, and the tool asks before each one.** Button slots
   are copied whole. Every other shared slot uses `merge`, so a newer device keeps its own
   fields. Slots only one device has are reported and left alone.
4. **Only slots that both devices already have can receive a copy.** Pair and open the new
   device in Logi Options+ before you migrate. A missing destination slot is reported and
   skipped, never created.
5. **Nothing translates between operating systems yet.** The table above is the evidence for
   that work, not an implemented feature.
6. **A layout carries buttons and gestures, not everything.** Settings slots migrate
   device to device inside one database with `merge`, but the layout file covers button
   slots only.
7. **The macOS driverkit extension `com.logi.optionsplus.hidfilter` stays loaded** on
   purpose. It does not hold the database open, and a restart of it is awkward.

## Development notes

There is no build step, no dependency file, no linter configuration, and no test suite in
this repository. The useful commands are short:

```bash
python3 -m py_compile migrate_logi_ergo.py   # syntax check
python3 migrate_logi_ergo.py --help          # argument summary
python3 migrate_logi_ergo.py --db ./copy/settings.db   # menu, against a copy
python3 migrate_logi_ergo.py --db ./copy/settings.db --dry-run --apply
```

Test against a copy, never against the live database. `--db` with `--dry-run` exercises
every code path with no write and no process control, on any operating system.

To inspect a database without the application and without a write, copy it first and read
the copy:

```bash
python3 - <<'PY'
import json, sqlite3, shutil, tempfile, pathlib
src = pathlib.Path.home() / "Library/Application Support/LogiOptionsPlus/settings.db"
tmp = pathlib.Path(tempfile.mkdtemp()) / "settings.db"
shutil.copy2(src, tmp)
row = sqlite3.connect(tmp).execute("SELECT _id, file FROM data").fetchone()
doc = json.loads(row[1])
for key, value in doc.items():
    slots = [a.get("slotId") for a in value.get("assignments", [])] if isinstance(value, dict) else []
    print(key, len(slots), "slots")
    for slot in slots:
        print("   ", slot)
PY
```

That snippet also prints the real profile key of your installation, which is the value that
limit 1 above describes.

Two rules for changes to this file:

- Keep every operating system difference inside a `Platform` subclass. A new platform is a
  new subclass plus one branch in `get_platform`.
- Keep the plan phase free of writes. `build_dynamic_plan` and `describe_plan_item` read the
  document. Only `_execute_migration` and `_run_preset_migration` call `save_settings`.

**For AI agents: menu items 4 to 7, `--apply`, and `--restore` need explicit consent.**
Ask the user in the current session before you run one of them. Those paths stop the
software of the user and write to live application state. The read-only paths need no
consent.

## License

No license file is present yet. Ask the repository owner before you redistribute this code.
