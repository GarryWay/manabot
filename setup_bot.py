#!/usr/bin/env python3
"""
setup_bot.py — Bootstrap and upgrade helper for the manabot Discord bot.

First deployment (run once on the host machine):
    python setup_bot.py

Upgrade after pulling new code:
    python setup_bot.py upgrade

Setup steps:
  1. Verify Python >= 3.11
  2. Install bot dependencies  (pip install -e ".[bot]")
  3. Create data/ directories
  4. Create data/buylist.csv with header row (if absent)
  5. Interactively populate .env with credentials (if absent or incomplete)
  6. Create config.yaml template (if absent)
  7. Install and enable a startup service:
       Linux  → systemd user service  (~/.config/systemd/user/manabot.service)
       Windows → Task Scheduler task  (runs at logon)
       macOS  → launchd plist         (~/Library/LaunchAgents/com.manabot.bot.plist)

Upgrade steps (skips credential prompting, only reinstalls deps + restarts service):
  1. pip install -e ".[bot]"
  2. Restart the running service
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
    """Return a pip command that works with the current Python interpreter.

    Tries in order:
      1. sys.executable -m pip          (pip already installed for this Python)
      2. sys.executable -m ensurepip    (bootstrap pip, then retry)
      3. pip3 in PATH                   (system alias fallback)
    """
    if _probe(sys.executable, "-m", "pip", "--version"):
        return [sys.executable, "-m", "pip"]

    # Attempt to bootstrap pip via ensurepip (ships with CPython 3.4+)
    _run([sys.executable, "-m", "ensurepip", "--upgrade"], check=False)
    if _probe(sys.executable, "-m", "pip", "--version"):
        return [sys.executable, "-m", "pip"]

    # Last resort: system pip3 alias
    if shutil.which("pip3") and _probe("pip3", "--version"):
        _warn("pip module not found for this interpreter — falling back to system pip3.")
        return ["pip3"]

    _fail(
        "Could not find a working pip for this Python interpreter.\n"
        "  Try bootstrapping it manually:\n"
        f"    {sys.executable} -m ensurepip --upgrade\n"
        f"  Then re-run:  {sys.executable} setup_bot.py"
    )
    return []  # unreachable; satisfies type checker


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

    # Look for a newer Python in PATH and suggest it
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
    result = _run([*pip_cmd, "install", "-e", ".[bot]", "--quiet"], check=False)
    if result.returncode != 0:
        _fail(
            "pip install failed. Check the error above and retry.\n"
            "  Common fix: ensure you're running inside the manabot directory."
        )
    _ok("Dependencies installed.")


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
    # Merge with actual environment variables so we don't re-prompt for already-set vars
    for k in list(_REQUIRED_ENV) + list(_OPTIONAL_ENV):
        if os.getenv(k) and k not in existing:
            existing[k] = os.environ[k]

    missing_required = [k for k in _REQUIRED_ENV if not existing.get(k)]

    if not missing_required:
        _ok(f".env is complete — all required credentials present.")
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


# ── Auto-start helpers ─────────────────────────────────────────────────────────

def _setup_linux() -> None:
    """Install a systemd user service that starts at boot via loginctl linger."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_file = service_dir / "manabot.service"

    service_dir.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    env_file_line = f"EnvironmentFile={ENV_FILE}" if ENV_FILE.exists() else ""

    service_content = textwrap.dedent(f"""\
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

    service_file.write_text(service_content, encoding="utf-8")
    _ok(f"Service file written: {service_file}")

    # Reload and enable
    for cmd in [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "manabot.service"],
        ["systemctl", "--user", "start", "manabot.service"],
    ]:
        result = _run(cmd, check=False)
        if result.returncode != 0:
            _warn(f"Command failed: {' '.join(cmd)}")
            _warn("You may need to run it manually after verifying systemd is available.")

    # Enable linger so the service survives after the user logs out
    linger_result = _run(["loginctl", "enable-linger", getpass.getuser()], check=False)
    if linger_result.returncode == 0:
        _ok("loginctl linger enabled — service will start at boot without login.")
    else:
        _warn(
            "loginctl enable-linger failed (may require sudo).\n"
            "  Without linger the service only runs while you are logged in.\n"
            f"  Run manually:  sudo loginctl enable-linger {getpass.getuser()}"
        )

    print(
        "\n  Useful commands:\n"
        "    systemctl --user status manabot\n"
        "    systemctl --user restart manabot\n"
        "    journalctl --user -u manabot -f"
    )


def _setup_windows() -> None:
    """Register a Task Scheduler task that runs the bot at logon."""
    import tempfile

    python_exe = str(Path(sys.executable).resolve())
    task_name = "Manabot Discord Bot"

    # Build the XML for schtasks /create /xml
    xml = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>Manabot Discord slash-command bot — auto-starts at logon.</Description>
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
              <Arguments>-m manabot bot</Arguments>
              <WorkingDirectory>{PROJECT_DIR}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)

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
        _warn("Task Scheduler registration failed.")
        _warn(
            "You can register it manually by running:\n"
            f"  schtasks /create /tn \"{task_name}\" /tr \"{python_exe} -m manabot bot\" "
            f"/sc onlogon /f"
        )
        return

    _ok(f"Task Scheduler task '{task_name}' registered (runs at logon).")

    # Start it immediately as well
    start_result = _run(["schtasks", "/run", "/tn", task_name], check=False)
    if start_result.returncode == 0:
        _ok("Bot started.")
    else:
        _warn("Could not start task immediately — it will start on next logon.")

    print(
        "\n  Useful commands:\n"
        f"    schtasks /query /tn \"{task_name}\"\n"
        f"    schtasks /run /tn \"{task_name}\"\n"
        f"    schtasks /end /tn \"{task_name}\"\n"
        f"    schtasks /delete /tn \"{task_name}\" /f"
    )
    print(
        "\n  NOTE: The task runs when you log in. For a truly headless server\n"
        "  (bot runs even when no one is logged in), enable auto-login and set\n"
        "  the task trigger to 'At startup' with a service account instead."
    )


def _setup_macos() -> None:
    """Install a launchd user agent that starts at login."""
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plist_file = agents_dir / "com.manabot.bot.plist"

    agents_dir.mkdir(parents=True, exist_ok=True)

    python_exe = str(Path(sys.executable).resolve())
    log_file = str(PROJECT_DIR / "data" / "manabot.log")

    # Build env dict from .env for launchd EnvironmentVariables
    env_dict = _read_env_file()
    env_xml = "\n".join(
        f"\t\t<key>{k}</key>\n\t\t<string>{v}</string>"
        for k, v in env_dict.items()
        if v
    )

    plist = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
            "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.manabot.bot</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_exe}</string>
                <string>-m</string>
                <string>manabot</string>
                <string>bot</string>
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

    plist_file.write_text(plist, encoding="utf-8")
    _ok(f"plist written: {plist_file}")

    for cmd in [
        ["launchctl", "unload", str(plist_file)],   # unload first (ignore failure)
        ["launchctl", "load", "-w", str(plist_file)],
    ]:
        result = _run(cmd, check=False)
        if result.returncode != 0 and "load" in cmd:
            _warn(f"launchctl load failed — try: launchctl load -w {plist_file}")

    print(
        "\n  Useful commands:\n"
        "    launchctl list com.manabot.bot\n"
        f"    launchctl stop com.manabot.bot\n"
        f"    launchctl start com.manabot.bot\n"
        f"    tail -f {log_file}"
    )


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
            "  Start the bot manually:  python -m manabot bot\n"
            "  Or add the above command to your system's startup mechanism."
        )


def restart_service() -> None:
    """Restart the running bot service (OS-specific)."""
    system = platform.system()
    if system == "Linux":
        result = _run(["systemctl", "--user", "restart", "manabot"], check=False)
        if result.returncode != 0:
            _warn("Restart failed. Run manually:  systemctl --user restart manabot")
        else:
            _ok("Service restarted.")
    elif system == "Windows":
        import time
        task_name = "Manabot Discord Bot"
        _run(["schtasks", "/end", "/tn", task_name], check=False)
        time.sleep(2)
        result = _run(["schtasks", "/run", "/tn", task_name], check=False)
        if result.returncode != 0:
            _warn(f"Restart failed. Run manually:  schtasks /run /tn \"{task_name}\"")
        else:
            _ok("Task restarted.")
    elif system == "Darwin":
        _run(["launchctl", "stop", "com.manabot.bot"], check=False)
        result = _run(["launchctl", "start", "com.manabot.bot"], check=False)
        if result.returncode != 0:
            _warn("Restart failed. Run manually:  launchctl start com.manabot.bot")
        else:
            _ok("Service restarted.")
    else:
        _warn(f"Unknown OS ({system}) — restart manually:  python -m manabot bot")


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


# ── Main ───────────────────────────────────────────────────────────────────────

def _run_setup() -> None:
    print("=" * 60)
    print("  Manabot Discord Bot — Setup")
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
        "  2. Invite the bot to your Discord server if you haven't already:\n"
        "       https://discord.com/developers/applications\n"
        "       → Your app → OAuth2 → URL Generator\n"
        "         Scopes: bot + applications.commands\n"
        "         Permissions: Send Messages, Attach Files, Embed Links,\n"
        "                      Use Application Commands\n"
        "       Open the generated URL in your browser and add it to your server.\n"
        "  3. Verify slash commands appear in Discord (may take up to 1 hour\n"
        "     for global sync; use --guild <ID> on first run for instant sync).\n"
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
    print("  Manabot Discord Bot — Upgrade")
    print(f"  Project: {PROJECT_DIR}")
    print(f"  Python:  {sys.executable}")
    print("=" * 60)

    check_python()
    install_deps()

    _step("Restarting service")
    restart_service()

    print("\n" + "=" * 60)
    print("  Upgrade complete!")
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
