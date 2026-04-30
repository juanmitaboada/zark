"""
zark setup — Install dependencies and configure sanoid snapshots.
"""

from pathlib import Path

from lib import sh

# from lib.config import Config
from lib.log import Log
from lib.zfs import ZFS

DEPS = {
    "syncoid": "sanoid",
    "zfs": "zfsutils-linux",
    "zpool": "zfsutils-linux",
    "sgdisk": "gdisk",
    "bc": "bc",
    "pv": "pv",
    "mbuffer": "mbuffer",
    "lzop": "lzop",
    "sanoid": "sanoid",
}


def _detect_ubuntu_ds() -> str:
    r = sh.run(
        "zfs list -H -o name -r rpool/ROOT "
        + "| grep -v '^rpool/ROOT$' | grep -v '@' | awk -F/ 'NF==3' | head -1",
    )
    return r.output.replace("rpool/ROOT/", "") if r.ok and r.output else ""


def _generate_sanoid_conf(ubuntu_name: str) -> str:
    lines = [
        f"[rpool/ROOT/{ubuntu_name}]",
        "use_template = production",
        "recursive = yes",
        "",
        "[rpool/USERDATA]",
        "use_template = production",
        "recursive = yes",
        "",
        "[rpool]",
        "use_template = minimal",
        "recursive = no",
        "",
        "[rpool/ROOT]",
        "use_template = minimal",
        "recursive = no",
        "",
        "[template_production]",
        "frequently = 0",
        "hourly = 24",
        "daily = 7",
        "weekly = 4",
        "monthly = 3",
        "yearly = 0",
        "autoprune = yes",
        "autosnap = yes",
        "",
        "[template_minimal]",
        "frequently = 0",
        "hourly = 0",
        "daily = 2",
        "weekly = 0",
        "monthly = 0",
        "yearly = 0",
        "autoprune = yes",
        "autosnap = yes",
    ]
    return "\n".join(lines) + "\n"


def run(
    args: list[str],
):  # pylint: disable=too-many-statements,too-many-branches,too-many-locals
    """Run the setup process to install dependencies and configure sanoid."""
    del args  # Unused
    log = Log()
    # cfg = Config.load()
    zfs = ZFS(log)

    log.banner("BACKUP SYSTEM SETUP", "Install dependencies and configure sanoid")

    # ── 1. Dependencies ──────────────────────────────────────────────────
    log.step(1, 4, "Checking dependencies...")
    missing: list[str] = []
    for cmd, pkg in DEPS.items():
        if sh.run(f"command -v {cmd}").ok:
            log.ok(f"  {cmd} — installed")
        else:
            missing.append(pkg)
            log.warn(f"  {cmd} — MISSING (package: {pkg})")

    if missing:
        pkgs = " ".join(sorted(set(missing)))
        log.info(f"Installing: {pkgs}")
        r = sh.run(f"apt-get install -y {pkgs}", log=log)
        if r.ok:
            log.ok("All packages installed")
        else:
            log.fatal(
                "Package installation failed",
                solutions=[f"Try: apt-get update && apt-get install -y {pkgs}"],
            )
    else:
        log.ok("All dependencies present")

    # ── 2. Sanoid configuration ──────────────────────────────────────────
    log.step(2, 4, "Configuring sanoid...")

    ubuntu_name = _detect_ubuntu_ds()
    if not ubuntu_name:
        log.warn("Could not detect rpool/ROOT/ubuntu_XXXXXX dataset")
        ubuntu_name = log.ask_input("Enter ubuntu dataset name", "ubuntu_XXXXXX")

    conf_path = "/etc/sanoid/sanoid.conf"
    _ = sh.run("mkdir -p /etc/sanoid")

    conf_content = _generate_sanoid_conf(ubuntu_name)

    if Path(conf_path).exists():
        log.info(f"sanoid.conf exists at {conf_path}")
        if log.ask("Replace with default configuration?"):
            _ = sh.run(f"cp {conf_path} {conf_path}.backup.$(date +%Y%m%d_%H%M%S)")
            _ = Path(conf_path).write_text(conf_content, encoding="utf-8")
            log.ok(f"sanoid.conf updated for {ubuntu_name}")
    else:
        _ = Path(conf_path).write_text(conf_content, encoding="utf-8")
        log.ok(f"Created {conf_path} for {ubuntu_name}")

    # ── 3. Enable sanoid timer ───────────────────────────────────────────
    log.step(3, 4, "Enabling sanoid timer...")
    _ = sh.run("systemctl enable sanoid.timer", timeout=10, log=log)
    _ = sh.run("systemctl start sanoid.timer", timeout=10, log=log)
    log.ok("sanoid.timer enabled and started")

    # ── 4. Initial snapshots ─────────────────────────────────────────────
    log.step(4, 4, "Checking snapshots...")
    snap_count = len(zfs.list_snapshots("rpool", "autosnap"))

    if snap_count == 0:
        if log.ask("No autosnap snapshots found. Take first snapshots now?", default=True):
            r = sh.run("sanoid --take-snapshots", log=log)
            if r.ok:
                snap_count = len(zfs.list_snapshots("rpool", "autosnap"))
                log.ok(f"Created {snap_count} snapshots")
            else:
                log.warn("sanoid --take-snapshots had errors")
    else:
        log.ok(f"{snap_count} autosnap snapshots exist")

    log.banner_ok(
        "SETUP COMPLETE",
        [
            "sanoid installed and running",
            "Snapshots will be taken automatically",
            "",
            f"Next: connect a backup drive and run: {log.W}sudo ./zark backup{log.N}",
        ],
    )
