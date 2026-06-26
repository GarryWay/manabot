#!/usr/bin/env python3
"""
setup_bot.py — Bootstrap and upgrade helper for manabot.

First deployment (run once on the host machine):
    python setup_bot.py

Upgrade after pulling new code:
    python setup_bot.py upgrade

Setup steps:
  1. Verify Python >= 3.11
  2. Install all dependencies  (pip install -e ".[full]")
  3. Create data/ directories
  4. Create data/buylist.csv with header row (if absent)
  5. Interactively populate .env with credentials (if absent or incomplete)
  6. Create config.yaml template (if absent)
  7. Install and enable startup services:
       Linux  → two systemd user services
                  manabot.service        (Discord bot, starts at boot)
                  manabot-pricer.service (daily price updater, runs 2 AM Central)
       Windows → two Task Scheduler tasks (same services, run at logon)
       macOS  → two launchd plists        (same services)
  8. Download Scryfall oracle card data

Upgrade steps (skips credential prompting):
  1. pip install -e ".[full]"
  2. Rewrite service files to pick up any changes
  3. Restart both running services
"""

from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
BUYLIST_CSV = DATA_DIR / "buylist.csv"
CONFIG_YAML = PROJECT_DIR / "config.yaml"
ENV_FILE = PROJECT_DIR / ".env"

BUYLIST_HEADER = (
    "card_name,scryfall_id,target_quantity,max_price_usd,"
    "min_condition,foil,allowed_sets,in_universe_only,tags,notes\n"
)

CONFIG_YAML_TEMPLATE = """\
# Manabot configuration
# Values here are overridden by matching environment variables in .env

manapool:
  email: ""      # or set MANAPOOL_EMAIL in .env
  token: ""      # or set MANAPOOL_TOKEN in .env

discord:
  webhook_url: ""  # optional — webhook for run/optimize notifications
  bot_token: ""    # or set DISCORD_BOT_TOKEN in .env
  # guild_id: 123456789  # uncomment for dev: syncs slash commands instantly

paths:
  buylist: data/buylist.csv
  db: data/manabot.db
  reports_dir: data/reports

optimizer:
  over_budget_pct: 0
  max_iterations: 5
  destination: US
  min_market_price_usd: 2.00

pricer:
  schedule_hour: 2                    # local hour for daily inventory reprice
  schedule_timezone: America/Chicago  # IANA timezone — handles DST automatically

# behavior:
#   trend_window_days: 7
#   trend_threshold_pct: 5.0
"""

_REQUIRED_ENV = {
    "MANAPOOL_EMAIL":    "ManaPool account email",
    "MANAPOOL_TOKEN":    "ManaPool API access token",
    "DISCORD_BOT_TOKEN": "Discord bot token (from Discord Developer Portal)",
}
_OPTIONAL_ENV = {
    "DISCORD_WEBHOOK_URL": "Discord webhook URL (optional — for run/optimize alerts)",
    "DISCORD_GUILD_ID":    "Discord guild/server ID (optional — for instant slash-command sync during dev)",
}

# ── Systemd / Task Scheduler names ────────────────────────────────────────────

_LINUX_BOT_UNIT    = "manabot.service"
_LINUX_PRICER_UNIT = "manabot-pricer.service"
_WIN_TASK_BOT      = "Manabot Discord Bot"
_WIN_TASK_PRICER   = "Manabot Price Updater"
_MAC_LABEL_BOT     = "com.manabot.bot"
_MAC_LABEL_PRICER  = "com.manabot.pricer"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _step(msg: str) -> None:
    print(f"\n{'─' * 60}\n{msg}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _fail(msg: str, *, fatal: bool = True) -> None:
    print(f"\n  ✗ ERROR: {msg}", file=sys.stderr)
    if fatal:
        sys.exit(1)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def _probe(*cmd: str) -> bool:
    """Return True if cmd exits 0. Output is suppressed — used for silent capability checks."""
    try:
        subprocess.run(list(cmd), check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _find_pip() -> list[str]:
    """Return a pip command that works with the current Python interpreter."""
    if _probe(sys.executable, "-m", "pip", "--version"):
        return [sys.executable, "-m", "pip"]

    if _probe(sys.executable, "-m", "ensurepip", "--upgrade"):
        if _probe(sys.executable, "-m", "pip", "--version"):
            return [sys.executable, "-m", "pip"]

    import tempfile
    import urllib.request

    _warn("pip not found for this interpreter — downloading get-pip.py to bootstrap...")
    tmp_path = Path(tempfile.mktemp(suffix=".py"))
    try:
        urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", tmp_path)
        _run([sys.executable, str(tmp_path)], check=False)
    except Exception as exc:
        _warn(f"get-pip.py download failed: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)

    if _probe(sys.executable, "-m", "pip", "--version"):
        return [sys.executable, "-m", "pip"]

    v = f"{sys.version_info.major}.{sys.version_info.minor}"
    _fail(
        "Could not install pip for this Python interpreter.\n"
        "  Install it manually, then re-run setup_bot.py:\n"
        f"    curl -sS https://bootstrap.pypa.io/get-pip.py | {sys.executable}\n"
        "  Or on Debian/Ubuntu:\n"
        f"    sudo apt install python{v}-pip"
    )
    return []


def _read_env_file() -> dict[str, str]:
    """Parse key=value pairs from .env (ignores comments and blank lines)."""
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_env_file(values: dict[str, str]) -> None:
    lines = []
    for k, v in values.items():
        lines.append(f'{k}="{v}"\n')
    ENV_FILE.write_text("".join(lines), encoding="utf-8")


# ── Setup steps ────────────────────────────────────────────────────────────────

def check_python() -> None:
    _step("Step 1 — Verify Python version")
    info = sys.version_info
    if info >= (3, 11):
        _ok(f"Python {info.major}.{info.minor}.{info.micro}")
        return

    suggestion = ""
    for minor in range(14, 10, -1):
        candidate = f"python3.{minor}"
        if shutil.which(candidate):
            suggestion = f"\n  Found {candidate} — re-run with:  {candidate} setup_bot.py"
            break

    _fail(
        f"Python 3.11+ required; found {info.major}.{info.minor}."
        f"{suggestion}"
    )


def install_deps() -> None:
    _step("Step 2 — Install dependencies")
    pip_cmd = _find_pip()
    # [full] includes bot (discord.py) + scheduler (apscheduler) extras
    result = _run([*pip_cmd, "install", "-e", ".[full]", "--quiet"], check=False)
    if result.returncode != 0:
        _fail(
            "pip install failed. Check the error above and retry.\n"
            "  Common fix: ensure you're running inside the manabot directory."
        )
    _ok("Dependencies installed (bot + scheduler).")


def create_directories() -> None:
    _step("Step 3 — Create data directories")
    for d in [DATA_DIR, DATA_DIR / "reports"]:
        d.mkdir(parents=True, exist_ok=True)
        _ok(f"{d.relative_to(PROJECT_DIR)}/")


def create_buylist() -> None:
    _step("Step 4 — Initialise buy list")
    if BUYLIST_CSV.exists():
        _ok(f"Buy list already exists: {BUYLIST_CSV.relative_to(PROJECT_DIR)}")
        return
    BUYLIST_CSV.write_text(BUYLIST_HEADER, encoding="utf-8-sig")
    _ok(f"Created {BUYLIST_CSV.relative_to(PROJECT_DIR)} with header row.")


def configure_env() -> None:
    _step("Step 5 — Configure credentials (.env)")

    existing = _read_env_file()
    for k in list(_REQUIRED_ENV) + list(_OPTIONAL_ENV):
        if os.getenv(k) and k not in existing:
            existing[k] = os.environ[k]

    missing_required = [k for k in _REQUIRED_ENV if not existing.get(k)]

    if not missing_required:
        _ok(".env is complete — all required credentials present.")
        return

    print(
        "\n  The following credentials are needed to run the bot.\n"
        "  Values are written to .env in the project directory.\n"
        "  Press Enter to skip optional entries.\n"
    )

    updated = dict(existing)
    for key, description in {**_REQUIRED_ENV, **_OPTIONAL_ENV}.items():
        if updated.get(key):
            _ok(f"{key} already set — skipping.")
            continue
        required = key in _REQUIRED_ENV
        prompt = f"  {'*' if required else ' '} {key} ({description}): "
        value = getpass.getpass(prompt) if "TOKEN" in key else input(prompt)
        value = value.strip()
        if not value and required:
            _fail(f"{key} is required.")
        if value:
            updated[key] = value

    _write_env_file({k: v for k, v in updated.items() if v})
    _ok(f".env written to {ENV_FILE}")


def create_config_yaml() -> None:
    _step("Step 6 — Create config.yaml template")
    if CONFIG_YAML.exists():
        _ok("config.yaml already exists — skipping.")
        return
    CONFIG_YAML.write_text(CONFIG_YAML_TEMPLATE, encoding="utf-8")
    _ok(f"Created {CONFIG_YAML.name} — edit it to customise further.")


# ── Service file builders ──────────────────────────────────────────────────────

def _linux_bot_service() -> str:
    python_exe = sys.executable
    env_file_line = f"EnvironmentFile={ENV_FILE}" if ENV_FILE.exists() else ""
    return textwrap.dedent(f"""\
        [Unit]
        Description=Manabot Discord Bot
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        WorkingDirectory={PROJECT_DIR}
        ExecStart={python_exe} -m manabot bot
        Restart=on-failure
        RestartSec=15
        {env_file_line}

        [Install]
        WantedBy=default.target
    """)


def _linux_pricer_service() -> str:
    python_exe = sys.executable
    env_file_line = f"EnvironmentFile={ENV_FILE}" if ENV_FILE.exists() else ""
    return textwrap.dedent(f"""\
        [Unit]
        Description=Manabot Daily Price Updater
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        WorkingDirectory={PROJECT_DIR}
        ExecStart={python_exe} -m manabot pricer-scheduler
        Restart=on-failure
        RestartSec=60
        {env_file_line}

        [Install]
        WantedBy=default.target
    """)


def _windows_task_xml(task_name: str, module_args: str) -> str:
    python_exe = str(Path(sys.executable).resolve())
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>{task_name}</Description>
          </RegistrationInfo>
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
            </LogonTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>LeastPrivilege</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <RestartCount>3</RestartCount>
            <RestartInterval>PT1M</RestartInterval>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{python_exe}</Command>
              <Arguments>{module_args}</Arguments>
              <WorkingDirectory>{PROJECT_DIR}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)


def _mac_plist(label: str, module_cmd: str) -> str:
    python_exe = str(Path(sys.executable).resolve())
    log_file = str(PROJECT_DIR / "data" / "manabot.log")
    env_dict = _read_env_file()
    env_xml = "\n".join(
        f"\t\t<key>{k}</key>\n\t\t<string>{v}</string>"
        for k, v in env_dict.items()
        if v
    )
    args_xml = "\n".join(
        f"\t\t<string>{arg}</string>" for arg in module_cmd.split()
    )
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_exe}</string>
        {args_xml}
            </array>
            <key>WorkingDirectory</key>
            <string>{PROJECT_DIR}</string>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{log_file}</string>
            <key>StandardErrorPath</key>
            <string>{log_file}</string>
            <key>EnvironmentVariables</key>
            <dict>
        {env_xml}
            </dict>
        </dict>
        </plist>
    """)


# ── Platform-specific setup ────────────────────────────────────────────────────

def _write_linux_service_files() -> tuple[Path, Path]:
    """Write both service files to ~/.config/systemd/user/. Returns their paths."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    bot_file = service_dir / _LINUX_BOT_UNIT
    pricer_file = service_dir / _LINUX_PRICER_UNIT

    bot_file.write_text(_linux_bot_service(), encoding="utf-8")
    _ok(f"Service file written: {bot_file}")

    pricer_file.write_text(_linux_pricer_service(), encoding="utf-8")
    _ok(f"Service file written: {pricer_file}")

    return bot_file, pricer_file


def _setup_linux() -> None:
    """Install both systemd user services and enable linger for boot-time start."""
    _write_linux_service_files()

    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", _LINUX_BOT_UNIT],
        ["systemctl", "--user", "start",  _LINUX_BOT_UNIT],
        ["systemctl", "--user", "enable", _LINUX_PRICER_UNIT],
        ["systemctl", "--user", "start",  _LINUX_PRICER_UNIT],
    ]:
        result = _run(cmd, check=False)
        if result.returncode != 0:
            _warn(f"Command failed: {' '.join(cmd)}")

    linger_result = _run(["loginctl", "enable-linger", getpass.getuser()], check=False)
    if linger_result.returncode == 0:
        _ok("loginctl linger enabled — services start at boot without login.")
    else:
        _warn(
            "loginctl enable-linger failed (may require sudo).\n"
            "  Without linger the services only run while you are logged in.\n"
            f"  Run manually:  sudo loginctl enable-linger {getpass.getuser()}"
        )

    print(
        "\n  Useful commands:\n"
        f"    systemctl --user status {_LINUX_BOT_UNIT}\n"
        f"    systemctl --user status {_LINUX_PRICER_UNIT}\n"
        f"    systemctl --user restart {_LINUX_BOT_UNIT}\n"
        f"    systemctl --user restart {_LINUX_PRICER_UNIT}\n"
        f"    journalctl --user -u manabot -f\n"
        f"    journalctl --user -u manabot-pricer -f"
    )


def _restart_linux() -> None:
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    for unit in [_LINUX_BOT_UNIT, _LINUX_PRICER_UNIT]:
        result = _run(["systemctl", "--user", "restart", unit], check=False)
        if result.returncode != 0:
            _warn(f"Restart failed for {unit}. Run manually:  systemctl --user restart {unit}")
        else:
            _ok(f"{unit} restarted.")


def _register_windows_task(task_name: str, module_args: str) -> None:
    import tempfile
    xml = _windows_task_xml(task_name, module_args)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(xml)
        tmp_path = tmp.name
    try:
        result = _run(
            ["schtasks", "/create", "/tn", task_name, "/xml", tmp_path, "/f"],
            check=False,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if result.returncode != 0:
        _warn(f"Task Scheduler registration failed for '{task_name}'.")
    else:
        _ok(f"Task '{task_name}' registered.")


def _setup_windows() -> None:
    _register_windows_task(_WIN_TASK_BOT, "-m manabot bot")
    _register_windows_task(_WIN_TASK_PRICER, "-m manabot pricer-scheduler")

    for task_name in [_WIN_TASK_BOT, _WIN_TASK_PRICER]:
        result = _run(["schtasks", "/run", "/tn", task_name], check=False)
        if result.returncode != 0:
            _warn(f"Could not start '{task_name}' immediately — starts at next logon.")
        else:
            _ok(f"'{task_name}' started.")

    print(
        f"\n  Useful commands:\n"
        f"    schtasks /query /tn \"{_WIN_TASK_BOT}\"\n"
        f"    schtasks /query /tn \"{_WIN_TASK_PRICER}\"\n"
        f"    schtasks /run   /tn \"{_WIN_TASK_PRICER}\"\n"
        f"    schtasks /end   /tn \"{_WIN_TASK_PRICER}\""
    )


def _restart_windows() -> None:
    import time
    for task_name in [_WIN_TASK_BOT, _WIN_TASK_PRICER]:
        _run(["schtasks", "/end", "/tn", task_name], check=False)
    time.sleep(2)
    for task_name in [_WIN_TASK_BOT, _WIN_TASK_PRICER]:
        result = _run(["schtasks", "/run", "/tn", task_name], check=False)
        if result.returncode != 0:
            _warn(f"Could not restart '{task_name}'.")
        else:
            _ok(f"'{task_name}' restarted.")


def _setup_macos() -> None:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    services = [
        (_MAC_LABEL_BOT,    "-m manabot bot"),
        (_MAC_LABEL_PRICER, "-m manabot pricer-scheduler"),
    ]

    for label, module_cmd in services:
        plist_file = agents_dir / f"{label}.plist"
        plist_file.write_text(_mac_plist(label, module_cmd), encoding="utf-8")
        _ok(f"plist written: {plist_file}")
        _run(["launchctl", "unload", str(plist_file)], check=False)
        result = _run(["launchctl", "load", "-w", str(plist_file)], check=False)
        if result.returncode != 0:
            _warn(f"launchctl load failed — try: launchctl load -w {plist_file}")
        else:
            _ok(f"{label} loaded.")

    log_file = PROJECT_DIR / "data" / "manabot.log"
    print(
        "\n  Useful commands:\n"
        f"    launchctl list {_MAC_LABEL_BOT}\n"
        f"    launchctl list {_MAC_LABEL_PRICER}\n"
        f"    launchctl stop {_MAC_LABEL_PRICER}\n"
        f"    launchctl start {_MAC_LABEL_PRICER}\n"
        f"    tail -f {log_file}"
    )


def _restart_macos() -> None:
    for label in [_MAC_LABEL_BOT, _MAC_LABEL_PRICER]:
        _run(["launchctl", "stop", label], check=False)
        result = _run(["launchctl", "start", label], check=False)
        if result.returncode != 0:
            _warn(f"Restart failed for {label}.")
        else:
            _ok(f"{label} restarted.")


def setup_autostart() -> None:
    _step("Step 7 — Configure auto-start at boot")
    system = platform.system()
    if system == "Linux":
        _setup_linux()
    elif system == "Windows":
        _setup_windows()
    elif system == "Darwin":
        _setup_macos()
    else:
        _warn(
            f"Unsupported OS: {system}.\n"
            "  Start services manually:\n"
            "    python -m manabot bot\n"
            "    python -m manabot pricer-scheduler"
        )


def update_service_files() -> None:
    """Rewrite service files on disk without enabling/starting them."""
    system = platform.system()
    if system == "Linux":
        _write_linux_service_files()
    elif system == "Windows":
        _register_windows_task(_WIN_TASK_BOT, "-m manabot bot")
        _register_windows_task(_WIN_TASK_PRICER, "-m manabot pricer-scheduler")
    elif system == "Darwin":
        agents_dir = Path.home() / "Library" / "LaunchAgents"
        for label, cmd in [
            (_MAC_LABEL_BOT, "-m manabot bot"),
            (_MAC_LABEL_PRICER, "-m manabot pricer-scheduler"),
        ]:
            plist_file = agents_dir / f"{label}.plist"
            plist_file.write_text(_mac_plist(label, cmd), encoding="utf-8")
            _ok(f"plist updated: {plist_file}")


def restart_service() -> None:
    """Restart both running services (OS-specific)."""
    system = platform.system()
    if system == "Linux":
        _restart_linux()
    elif system == "Windows":
        _restart_windows()
    elif system == "Darwin":
        _restart_macos()
    else:
        _warn(
            f"Unknown OS ({system}) — restart manually:\n"
            "  python -m manabot bot\n"
            "  python -m manabot pricer-scheduler"
        )


def download_oracle_data() -> None:
    _step("Step 8 — Download Scryfall oracle card data (~170 MB)")
    oracle_path = PROJECT_DIR / "data" / "scryfall_oracle.json"
    meta_path = oracle_path.with_name("scryfall_oracle.meta.json")

    if oracle_path.exists() and meta_path.exists():
        _ok("Scryfall oracle data already present — skipping initial download.")
        print("       (The running bot will check for updates weekly.)")
        return

    try:
        from manabot.api.scryfall_bulk import download_oracle_cards
        download_oracle_cards(oracle_path)
        _ok(f"Downloaded to {oracle_path}")
    except Exception as exc:
        _warn(
            f"Download failed: {exc}\n"
            "  The bot will retry automatically on first startup.\n"
            "  If this keeps failing, check your internet connection and try:\n"
            f"    python -c \"from manabot.api.scryfall_bulk import download_oracle_cards; "
            f"download_oracle_cards()\""
        )


# ── Main flows ─────────────────────────────────────────────────────────────────

def _run_setup() -> None:
    print("=" * 60)
    print("  Manabot — Setup")
    print(f"  Project: {PROJECT_DIR}")
    print(f"  Python:  {sys.executable}")
    print("=" * 60)

    check_python()
    install_deps()
    create_directories()
    create_buylist()
    configure_env()
    create_config_yaml()
    setup_autostart()
    download_oracle_data()

    print("\n" + "=" * 60)
    print("  Setup complete!")
    print(
        "\n  Next steps:\n"
        "  1. Review config.yaml and fill in any values you skipped.\n"
        "  2. Test the price updater before going live:\n"
        "       python -m manabot price-update --dry-run\n"
        "  3. Invite the Discord bot to your server:\n"
        "       https://discord.com/developers/applications\n"
        "       → Your app → OAuth2 → URL Generator\n"
        "         Scopes: bot + applications.commands\n"
        "         Permissions: Send Messages, Attach Files, Embed Links,\n"
        "                      Use Application Commands\n"
        "  4. Verify slash commands appear in Discord (up to 1 hour for\n"
        "     global sync; set discord.guild_id in config.yaml for instant).\n"
        "\n"
        "  Daily pricing runs at 2 AM Central by default. To change:\n"
        "    Edit pricer.schedule_hour / pricer.schedule_timezone in config.yaml\n"
        "\n"
        "  To upgrade after pulling new code:\n"
        "    python setup_bot.py upgrade\n"
        "\n"
        "  To update credentials, edit .env directly:\n"
        f"    {ENV_FILE}"
    )
    print("=" * 60)


def _run_upgrade() -> None:
    print("=" * 60)
    print("  Manabot — Upgrade")
    print(f"  Project: {PROJECT_DIR}")
    print(f"  Python:  {sys.executable}")
    print("=" * 60)

    check_python()
    install_deps()

    _step("Updating service files")
    update_service_files()

    _step("Restarting services")
    restart_service()

    print("\n" + "=" * 60)
    print("  Upgrade complete!")
    print(
        "\n  Both services have been restarted:\n"
        f"    Discord bot     ({_LINUX_BOT_UNIT if platform.system() == 'Linux' else _WIN_TASK_BOT})\n"
        f"    Price updater   ({_LINUX_PRICER_UNIT if platform.system() == 'Linux' else _WIN_TASK_PRICER})\n"
        "\n  Database schema migrations apply automatically on first run.\n"
        "  ManaPool catalog cache refreshes on the next price-update run.\n"
        "\n  To check service status:\n"
    )
    if platform.system() == "Linux":
        print(
            f"    journalctl --user -u manabot -f\n"
            f"    journalctl --user -u manabot-pricer -f"
        )
    elif platform.system() == "Windows":
        print(
            f"    schtasks /query /tn \"{_WIN_TASK_BOT}\"\n"
            f"    schtasks /query /tn \"{_WIN_TASK_PRICER}\""
        )
    print("=" * 60)


def main() -> None:
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "setup"
    if cmd == "upgrade":
        _run_upgrade()
    elif cmd == "setup":
        _run_setup()
    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        print("Usage:  python setup_bot.py [setup|upgrade]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
