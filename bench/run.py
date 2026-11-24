# Copyright 2026 Juanmi Taboada
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
One bench run: the steps in order, each timed, the leak check last.

This module starts virtual machines and talks to the network, so it holds
order and timing; the decisions live in tested modules (whether out/ may be
uploaded in bench/leak.py), and finish(), which needs no VM, has tests of
its own. It is excluded from coverage by name (sonar-project.properties).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bench import (
    RECORD_VERSION,
    console,
    drive,
    firmware,
    inspect_iso,
    iso,
    layout,
    leak,
    qemu,
    remaster,
    sbproof,
    seed,
    vm,
    zarkcheck,
)
from bench.qmp import Qmp, QmpError
from bench.report import Check, all_ok, summary, write_checks

SERIES = "26.04"
INSTALL_TIMEOUT_S = 3600  # Q2-8a: twice the longest install expected (25 min)
UNLOCK_TIMEOUT_S = 600
LOGIN_TIMEOUT_S = 600
SSH_TIMEOUT_S = 300
NEGATIVE_TIMEOUT_S = 180
SCREEN_EVERY_S = 60
# One line in the job's log this often during the install, so a run that
# stops shows where.
HEARTBEAT_EVERY_S = 300
POWER_OFF_SSH_TIMEOUT_S = 60
# QMP's per-read bound, and stop()'s waits: quit over QMP, then SIGTERM.
QMP_IO_TIMEOUT_S = 30.0
STOP_QUIT_WAIT_S = 30.0
STOP_TERM_WAIT_S = 10.0

# Mode full: zark built, installed, backed up and recovered (steps 4 to 6).
REPO = Path(__file__).resolve().parents[1]
USB_SERIAL = "ZB0Q3USB0001"  # invented, as every serial the bench gives
NEW_DISK_SERIAL = "ZB0Q3NEW0001"
BACKUP_DISK_SIZE = "32G"
NEW_DISK_SIZE = "40G"
APT_IDLE_TIMEOUT_S = 600
LIVE_SSH_TIMEOUT_S = 900
USB_WAIT_S = 120
MARKER_BYTES = 1 << 20
MARKER_PATHS = ("/root/zark-bench-marker", f"/home/{seed.USERNAME}/zark-bench-marker")
# Each zark command's limit, inside the steps' budget.
ZARK_LIMIT_S = {"setup": 900, "prepare": 2700, "backup": 1800, "recover": 3600, "finish": 1200}
# apt, apt-get, dpkg, apt's daily script and unattended-upgrade itself, not
# its shutdown helper, which waits for ever by design.
APT_BUSY = r"(^|/)(apt|apt-get|dpkg|apt\.systemd\.daily)( |$)|/unattended-upgrade( |$)"


class StepFailed(Exception):
    """A step's own verdict: the run stops here (the leak check still runs)."""


def log(message: str) -> None:
    """One line in the job's log."""
    print(message, flush=True)


def free_port() -> int:
    """A TCP port on the host's loopback that nothing listens on now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Run:  # pylint: disable=too-many-instance-attributes  # one run's whole state
    """The state of one run; only ``out`` is ever uploaded."""

    mode: str
    out: Path
    work: Path
    iso_wanted: str = ""
    steps: list[dict[str, object]] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    iso_path: Path | None = None
    remastered: Path | None = None
    fw: firmware.Firmware | None = None
    key: Path | None = None
    ssh_port: int = 0
    guest_up: bool = False
    # Ever up over SSH in this run; guest_up goes back to False at power-off.
    guest_seen: bool = False
    out_of_time: bool = False
    # Every QEMU started, with its QMP socket: none is left running at the end.
    vms: list[tuple[subprocess.Popen[bytes], Path]] = field(default_factory=list)
    # Mode full: secrets that are bytes (rpool's raw key), the other disk
    # images the host's search reads, the guests up over SSH after the first.
    byte_secrets: list[bytes] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    seen_stages: set[str] = field(default_factory=set)
    verdicts: list[Check] = field(default_factory=list)
    deb: Path | None = None
    tarball: Path | None = None
    zark_version: str = ""
    marker_sha256: str = ""
    # Step 4's backup point and its drive's pool GUID; how many datasets
    # recover anchored for that drive.
    point: str = ""
    drive_guid: str = ""
    anchored: int = 0

    def path(self, *parts: str) -> Path:
        """A file under the work directory (never uploaded)."""
        p = self.work.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def keep(self, *parts: str) -> Path:
        """A file under ``out`` (uploaded after the leak check)."""
        p = self.out.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def sh(
        self, argv: list[str], stdin: bytes | None = None, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """
        A command, logged by its arguments, which never carry a secret; a
        secret goes in ``stdin`` only. With ``timeout_s`` it is killed then,
        and ``subprocess.TimeoutExpired`` raised.
        """
        qemu.assert_no_secret(argv, self.secrets)
        with self.keep("commands.log").open("a", encoding="utf-8") as f:
            f.write(" ".join(argv) + "\n")
        return subprocess.run(
            argv, input=stdin, check=False, capture_output=True, timeout=timeout_s
        )

    def must(self, argv: list[str], stdin: bytes | None = None) -> str:
        """A command that has to succeed; its output as text."""
        proc = self.sh(argv, stdin)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[-400:]
            raise StepFailed(f"{argv[0]} exited {proc.returncode}: {err}")
        return proc.stdout.decode("utf-8", "replace")

    def step(self, name: str, fn: Callable[[], None]) -> bool:
        """Run one step, record its status and seconds; False stops the run."""
        log(f"== {name}")
        if self.out_of_time:
            return self._record(name, 1, 0.0, "not run: the steps' budget ran out")
        began = time.monotonic()
        status, detail = 0, ""
        try:
            fn()
        except vm.BudgetExpired as err:
            self.out_of_time = True
            status, detail = 1, f"BudgetExpired: {err}"
            log(f"FAIL {name}: {detail}")
        # A step that fails in any way is recorded as failed and stops the
        # run; the leak check and the upload decision still follow.
        except Exception as err:  # pylint: disable=broad-exception-caught
            status, detail = 1, f"{type(err).__name__}: {err}"
            log(f"FAIL {name}: {detail}")
        return self._record(name, status, round(time.monotonic() - began, 1), detail)

    def _record(self, name: str, status: int, seconds: float, detail: str) -> bool:
        self.steps.append({"step": name, "status": status, "seconds": seconds, "detail": detail})
        self.keep("steps.json").write_text(
            json.dumps({"record_version": RECORD_VERSION, "steps": self.steps}, indent=2) + "\n",
            encoding="utf-8",
        )
        log(f"   {name}: {'ok' if status == 0 else 'failed'} in {seconds} s")
        return status == 0

    def checks(self, name: str, checks: list[Check]) -> None:
        """Record named verdicts; any failure fails the step."""
        write_checks(self.keep(f"{name}.json"), checks)
        if self.mode == "full":
            self.verdicts.extend(Check(f"{name}/{c.name}", c.ok, c.detail) for c in checks)
            write_checks(self.keep("full.json"), self.verdicts)
        log(summary(checks))
        if not all_ok(checks):
            raise StepFailed(f"{name}: " + ", ".join(c.name for c in checks if not c.ok))


# ── the ISO ────────────────────────────────────────────────────────────────


def fetch_iso(run: Run) -> None:
    """Choose, fetch and verify the ISO."""
    base = iso.RELEASES.format(series=SERIES)
    with urllib.request.urlopen(base, timeout=60) as resp:
        listing = resp.read().decode("utf-8", "replace")
    choice = iso.choose(listing, SERIES, run.iso_wanted)
    log(f"   ISO: {choice.name}")
    sums, sig = run.path("SHA256SUMS"), run.path("SHA256SUMS.gpg")
    iso.download(base + "SHA256SUMS", sums)
    iso.download(base + "SHA256SUMS.gpg", sig)
    shutil.copyfile(sums, run.keep("iso", "SHA256SUMS"))
    status = iso.gpgv_status(sums, sig)
    run.keep("iso", "gpgv-status.txt").write_text(status, encoding="utf-8")
    error = iso.signature_error(status)
    if error:
        raise StepFailed(f"SHA256SUMS: {error}")
    expected = iso.parse_sha256sums(sums.read_text(encoding="utf-8")).get(choice.name)
    if not expected:
        raise StepFailed(f"{choice.name} has no line in SHA256SUMS")
    run.iso_path = run.path(choice.name)
    began = time.monotonic()
    iso.download(choice.url, run.iso_path)
    fetched_s = round(time.monotonic() - began, 1)
    actual = iso.sha256_file(run.iso_path)
    record = {
        "name": choice.name,
        "url": choice.url,
        "sha256": actual,
        "expected": expected,
        "bytes": run.iso_path.stat().st_size,
        "fetch_seconds": fetched_s,
        "signer": iso.CDIMAGE_FINGERPRINT,
    }
    run.keep("iso", "iso.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if actual != expected:
        raise StepFailed(f"{choice.name}: sha256 {actual}, SHA256SUMS says {expected}")


def remaster_iso(
    run: Run, args: tuple[str, ...] = remaster.INSTALL_ARGS, name: str = "remastered"
) -> None:
    """grub.cfg edited, boot records compared."""
    assert run.iso_path is not None
    original = run.path("grub.cfg.orig")
    original.unlink(missing_ok=True)
    run.must(remaster.extract_cmd(run.iso_path, remaster.GRUB_CFG, original))
    text = original.read_text(encoding="utf-8")
    edited = remaster.edit_grub_cfg(text, args)
    suffix = "" if name == "remastered" else f".{name}"
    shutil.copyfile(original, run.keep("iso", "grub.cfg.orig"))
    run.keep("iso", f"grub.cfg{suffix}").write_text(edited, encoding="utf-8")
    new_cfg = run.path(f"grub.cfg{suffix}")
    new_cfg.write_text(edited, encoding="utf-8")
    if run.remastered is not None:
        # One remastered ISO on the runner at a time (6 GB each).
        run.remastered.unlink(missing_ok=True)
    run.remastered = run.path(f"{name}.iso")
    run.remastered.unlink(missing_ok=True)
    run.must(remaster.remaster_cmd(run.iso_path, run.remastered, new_cfg))
    before = run.must(remaster.report_cmd(run.iso_path))
    after = run.must(remaster.report_cmd(run.remastered))
    run.keep("iso", "boot-records.orig.txt").write_text(before, encoding="utf-8")
    run.keep("iso", f"boot-records{suffix}.txt").write_text(after, encoding="utf-8")
    if remaster.boot_shape(before) != remaster.boot_shape(after):
        raise StepFailed("the remaster changed the ISO's boot records")


# ── firmware and the negative control ─────────────────────────────────────


def machine(run: Run, name: str, vars_file: Path, memory_mib: int = 8192) -> qemu.Machine:
    """The bench's machine shape, with this boot's sockets and serial log."""
    assert run.fw is not None
    return qemu.Machine(
        firmware=run.fw,
        vars_file=vars_file,
        serial_socket=run.path(f"{name}.serial.sock"),
        serial_log=run.keep(f"serial-{name}.log"),
        qmp_socket=run.path(f"{name}.qmp.sock"),
        # BENCH_ACCEL=tcg runs the bench without KVM, for checks by hand.
        accel=os.environ.get("BENCH_ACCEL", "kvm"),
        memory_mib=memory_mib,
    )


def start(run: Run, name: str, argv: list[str]) -> subprocess.Popen[bytes]:
    """Start QEMU; its own messages go to out/qemu-<name>.log."""
    qemu.assert_no_secret(argv, run.secrets)
    run.keep(f"qemu-{name}.argv").write_text(" ".join(argv) + "\n", encoding="utf-8")
    err = run.keep(f"qemu-{name}.log").open("wb")
    # Outlives this call by design: stop() or finish() ends it.
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        argv, stdin=subprocess.DEVNULL, stdout=err, stderr=err
    )
    run.vms.append((proc, run.path(f"{name}.qmp.sock")))
    return proc


def stop(proc: subprocess.Popen[bytes], qmp_socket: Path) -> None:
    """End QEMU, every wait bounded, and say how it ended."""
    how = vm.stop(
        proc,
        qmp_socket,
        qmp_timeout_s=min(QMP_IO_TIMEOUT_S, 5.0),
        quit_wait_s=STOP_QUIT_WAIT_S,
        term_wait_s=STOP_TERM_WAIT_S,
    )
    if how != "exited":
        log(f"   QEMU {how}")


def pick_firmware(run: Run) -> None:
    """The runner's Secure Boot firmware."""
    run.fw = firmware.find()
    log(f"   firmware {run.fw.code} with {run.fw.vars_template} ({run.fw.descriptor.name})")
    run.keep("firmware.json").write_text(
        json.dumps({k: str(v) for k, v in vars(run.fw).items()}, indent=2) + "\n",
        encoding="utf-8",
    )


def negative_control(run: Run) -> None:
    """An unsigned EFI binary must be refused."""
    unsigned = run.path("negative", "BOOTX64.EFI")
    run.must(["grub-mkimage", "-O", "x86_64-efi", "-o", str(unsigned), "-p", "/EFI/BOOT", "normal"])
    image = run.path("negative", "esp.img")
    image.unlink(missing_ok=True)
    run.must(["mkfs.vfat", "-C", str(image), "8192"])
    run.must(["mmd", "-i", str(image), "::/EFI", "::/EFI/BOOT"])
    run.must(["mcopy", "-i", str(image), str(unsigned), "::/EFI/BOOT/BOOTX64.EFI"])
    assert run.fw is not None
    # The firmware alone runs here: 1 GiB is plenty.
    vars_file = firmware.fresh_vars(run.fw, run.path("negative", "vars.fd"))
    m = machine(run, "negative", vars_file, memory_mib=1024)
    proc = start(run, "negative", qemu.negative_argv(m, image))
    deadline = time.monotonic() + NEGATIVE_TIMEOUT_S
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(2)
        if sbproof.negative_check(_text(m.serial_log)).ok:
            break
    stop(proc, m.qmp_socket)
    # Read once more: QEMU may have ended between two reads.
    run.checks("negative-control", [sbproof.negative_check(_text(m.serial_log))])


def _text(path: Path) -> str:
    try:
        return console.clean(path.read_bytes()).decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


# ── probe: the ISO read without booting it ─────────────────────────────────


def _squashfs_list(run: Run, layer: Path) -> list[str]:
    return inspect_iso.listed_paths(run.must(inspect_iso.unsquashfs_list_cmd(layer)))


def _extract(run: Run, image: Path, inner: str, dest: Path) -> None:
    run.must(inspect_iso.unsquashfs_extract_cmd(image, inner, dest))


def _layers(run: Run, iso_path: Path, chain: list[str]) -> dict[str, Path]:
    """The probe's files out of the live's layers; the top layer wins."""
    files: dict[str, Path] = {}
    for layer in reversed(chain):
        local = run.path("layers", layer)
        local.unlink(missing_ok=True)
        run.must(remaster.extract_cmd(iso_path, f"/casper/{layer}", local))
        dest = run.path("rootfs", layer)
        for inner in _squashfs_list(run, local):
            if inner not in files and inspect_iso.wanted_from_layer(inner):
                _extract(run, local, inner, dest)
                files[inner] = dest / inner
        local.unlink()
    return files


def _cloud(run: Run, files: dict[str, Path]) -> list[str]:
    lines: list[str] = []
    for inner, path in sorted(files.items()):
        if inner.startswith(inspect_iso.CLOUD_FILES) and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            shutil.copyfile(path, run.keep("probe", "cloud", inner.replace("/", "_")))
            lines.extend(f"{inner}: {ln}" for ln in inspect_iso.datasource_lines(text))
    return lines


def _snap(run: Run, snap: Path, report: dict[str, object]) -> None:
    listing = _squashfs_list(run, snap)
    for inner in [p for p in listing if inspect_iso.wanted_from_snap(p)]:
        _extract(run, snap, inner, run.path("snap"))
        shutil.copyfile(run.path("snap", inner), run.keep("probe", "snap", inner.replace("/", "_")))
    if inspect_iso.SNAP_YAML in listing:
        meta = run.path("snap", inspect_iso.SNAP_YAML).read_text(encoding="utf-8", errors="replace")
        report["installer-snap-version"] = inspect_iso.snap_version(meta)
    sources = [p for p in listing if p.endswith(inspect_iso.SUBIQUITY_FILE)]
    if sources:
        report["subiquity-filesystem-py"] = sources[0]
        data = run.path("snap", sources[0]).read_bytes()
        report["subiquity-filesystem-py-blob"] = inspect_iso.git_blob_id(data)


def inspect(run: Run) -> None:
    """What the live carries: installer snap, cloud-init, sshd."""
    assert run.iso_path is not None
    found = run.must(
        ["xorriso", "-indev", str(run.iso_path), "-find", "/casper", "-name", "*.squashfs"]
    )
    available = inspect_iso.squashfs_names(found)
    args = remaster.first_linux_args(run.path("grub.cfg.orig").read_text(encoding="utf-8"))
    chain = inspect_iso.layer_chain(inspect_iso.layerfs_path(args), available)
    chain = chain or inspect_iso.default_chain(available)
    report: dict[str, object] = {"layers": available, "chain": chain}
    files = _layers(run, run.iso_path, chain)
    status = files.get(inspect_iso.DPKG_STATUS)
    text = status.read_text(encoding="utf-8", errors="replace") if status else ""
    for package in ("openssh-server", "cloud-init"):
        report[package] = inspect_iso.package_installed(text, package) or "absent"
    report["recover-packages"] = {
        p: inspect_iso.package_installed(text, p) or "absent" for p in inspect_iso.RECOVER_PACKAGES
    }
    report["cloud-init-datasources"] = _cloud(run, files)
    snaps = [p for i, p in files.items() if i.endswith(".snap")]
    report["installer-snap"] = [p.name for p in snaps]
    for snap in snaps:
        _snap(run, snap, report)
    out = json.dumps(report, indent=2) + "\n"
    run.keep("probe", "probe.json").write_text(out, encoding="utf-8")
    log(out)


# ── install: secrets, seed, the installer, the installed boot ──────────────


def make_secrets(run: Run) -> tuple[str, str]:
    """
    The disk passphrase and the user's password, in memory, masked in the
    job's log (Q2-2a); the SSH key in the work directory.
    """
    passphrase = secrets.token_urlsafe(24)
    password = secrets.token_urlsafe(24)
    run.secrets.extend([passphrase, password])
    for value in (passphrase, password):
        # GitHub replaces a masked value with *** wherever the log shows it.
        log(f"::add-mask::{value}")
    keydir = run.work / "ssh"
    keydir.mkdir(mode=0o700, exist_ok=True)
    run.key = keydir / "id_ed25519"
    run.key.unlink(missing_ok=True)
    run.must(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "zark-bench", "-f", str(run.key)]
    )
    return passphrase, password


def make_seed(run: Run, passphrase: str, password: str) -> Path:
    """The CIDATA image, the one file holding the passphrase."""
    assert run.key is not None
    hashed = run.must(["openssl", "passwd", "-6", "-stdin"], stdin=password.encode()).strip()
    public = run.key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    doc = seed.autoinstall(passphrase, hashed, public)
    user_data, meta = run.path("seed", "user-data"), run.path("seed", "meta-data")
    user_data.write_text(seed.render_user_data(doc), encoding="utf-8")
    meta.write_text(seed.meta_data(f"zark-bench-{int(time.time())}"), encoding="utf-8")
    image = run.path("seed", "seed.img")
    run.must(seed.cloud_localds_cmd(image, user_data, meta))
    user_data.unlink()
    # What the seed held, with the secrets out, for the record.
    shown = seed.autoinstall("<passphrase>", "<hash>", public)
    run.keep("seed-user-data.redacted.json").write_text(
        json.dumps(shown, indent=2) + "\n", encoding="utf-8"
    )
    return image


def _screens(q: Qmp | None, run: Run, label: str) -> Qmp | None:
    """A screen into out/screens; the session, or None once QMP has failed."""
    if q is None:
        return None
    try:
        q.screendump(run.keep("screens", f"{label}-{int(time.time())}.png"))
    except (OSError, QmpError) as err:
        # Said once, then no more screens: every later call would wait again.
        log(f"   QMP stopped answering: {err}; no more screens")
        q.close()
        return None
    return q


def _vm_state(q: Qmp | None) -> str:
    if q is None:
        return "unknown (no QMP)"
    try:
        return q.status()
    except (OSError, QmpError) as err:
        return f"unknown ({err})"


def install(run: Run, seed_image: Path, disk: Path, vars_file: Path) -> None:
    """The installer runs from the remastered ISO and powers off."""
    assert run.remastered is not None
    run.must(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), "40G"])
    m = machine(run, "install", vars_file)
    run.ssh_port = free_port()
    proc = start(
        run, "install", qemu.install_argv(m, run.remastered, seed_image, disk, run.ssh_port)
    )
    q: Qmp | None = None
    try:
        try:
            q = Qmp.connect(m.qmp_socket, io_timeout_s=QMP_IO_TIMEOUT_S)
        except (OSError, QmpError) as err:
            log(f"   no QMP session: {err}; no screens")
        began = time.monotonic()
        deadline = began + INSTALL_TIMEOUT_S
        next_screen = next_beat = began
        while proc.poll() is None and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_screen:
                q = _screens(q, run, "install")
                next_screen = now + SCREEN_EVERY_S
            if now >= next_beat:
                log(f"   install: {int(now - began)} s, QEMU running, VM {_vm_state(q)}")
                next_beat = now + HEARTBEAT_EVERY_S
            time.sleep(5)
        if proc.poll() is None:
            # Kept for the upload: what the guest showed and where it stood.
            q = _screens(q, run, "install-timeout")
            state = _vm_state(q)
            df = run.sh(["df", "-h", str(run.work)], timeout_s=30)
            run.keep("install-timeout.txt").write_text(
                f"VM {state}\n{df.stdout.decode('utf-8', 'replace')}", encoding="utf-8"
            )
            raise StepFailed(f"the install did not end within {INSTALL_TIMEOUT_S} s (VM {state})")
        if proc.returncode != 0:
            raise StepFailed(f"QEMU exited {proc.returncode} during the install")
    finally:
        # On a timeout, a failure or the budget's end alike: no QEMU left behind.
        if q is not None:
            q.close()
        stop(proc, m.qmp_socket)


def ssh_argv(run: Run, command: str, tty: bool = False) -> list[str]:
    """
    SSH into the guest as the bench user; the command is a remote shell line.
    ``tty``: a pty in the guest (``-tt``), for a command that reads its terminal.
    """
    assert run.key is not None
    return [
        "ssh",
        *(["-tt"] if tty else []),
        "-i",
        str(run.key),
        "-p",
        str(run.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={run.path('ssh', 'known_hosts')}",
        f"{seed.USERNAME}@127.0.0.1",
        command,
    ]


def boot_installed(
    run: Run, passphrase: str, disk: Path, vars_file: Path
) -> tuple[subprocess.Popen[bytes], qemu.Machine]:
    """Boot the installed disk, answer the unlock prompt, reach SSH."""
    m = machine(run, "installed", vars_file)
    run.ssh_port = free_port()
    # Mode full: the USB port the backup drive is plugged into later.
    usb = qemu.usb_port(run.path("backup.qcow2")) if run.mode == "full" else []
    argv = qemu.installed_argv(m, disk, run.ssh_port, tuple(usb))
    proc = start(run, "installed", argv)
    run.keep("qemu-installed.argv.json").write_text(json.dumps(argv) + "\n", encoding="utf-8")
    unlock_to_ssh(run, passphrase, proc, m, "installed")
    run.guest_seen = True
    return proc, m


def unlock_to_ssh(
    run: Run, passphrase: str, proc: subprocess.Popen[bytes], m: qemu.Machine, name: str
) -> None:
    """Answer the unlock prompt on the serial console, then wait for SSH."""
    try:
        with run.keep(f"console-{name}.log").open("wb") as transcript:
            con = console.Console.connect(m.serial_socket, transcript)
            try:
                answered = console.unlock(con, passphrase, UNLOCK_TIMEOUT_S, LOGIN_TIMEOUT_S)
            except console.ConsoleError as err:
                raise StepFailed(str(err)) from err
            finally:
                con.close()
        log(f"   unlock prompt answered: {answered}")
        wait_ssh(run, SSH_TIMEOUT_S)
    except BaseException:
        # Not left running: the leak check reads the disk image next.
        stop(proc, m.qmp_socket)
        raise


def wait_ssh(run: Run, timeout_s: float) -> None:
    """SSH answers within ``timeout_s``, or the step fails."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if run.sh(ssh_argv(run, "true"), timeout_s=60).returncode == 0:
            run.guest_up = True
            return
        time.sleep(5)
    raise StepFailed(f"no SSH within {int(timeout_s)} s")


def guest(run: Run, command: str) -> bytes:
    """A read-only command in the guest, as root; its output, filed by name."""
    proc = run.sh(ssh_argv(run, f"sudo -n {command}"))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise StepFailed(f"guest: {command} exited {proc.returncode}: {err}")
    return proc.stdout


def guest_reading(run: Run, name: str, command: str, where: str = "guest") -> sbproof.Reading:
    """A read-only command in the guest, as root, kept under its name whatever its exit."""
    proc = run.sh(ssh_argv(run, f"sudo -n {command}"))
    err = proc.stderr.decode("utf-8", "replace")
    run.keep(where, f"{name}.out").write_bytes(proc.stdout)
    run.keep(where, f"{name}.status").write_text(
        f"{command}\nexit {proc.returncode}\n{err}", encoding="utf-8"
    )
    return sbproof.Reading(proc.returncode, proc.stdout, err)


def check_layout(run: Run, where: str = "guest", name: str = "layout") -> None:
    """The installer's layout; every listing kept for Q3."""
    outputs: dict[str, str] = {}
    for item, command in layout.COMMANDS.items():
        outputs[item] = guest(run, command).decode("utf-8", "replace")
        run.keep(where, f"{item}.txt").write_text(outputs[item], encoding="utf-8")
    run.checks(name, layout.check(outputs, seed.USERNAME))


def prove_secure_boot(
    run: Run, m: qemu.Machine, where: str = "guest", name: str = "secure-boot"
) -> None:
    """Secure Boot from the guest, the kernel's line and the host's command line."""
    assert run.fw is not None
    mokutil = guest_reading(run, "mokutil-sb-state", "mokutil --sb-state", where)
    efivar = guest_reading(run, "efivar-secureboot", f"cat {sbproof.EFIVAR}", where)
    for item, command in sbproof.EFIVAR_CONTEXT.items():
        guest_reading(run, item, command, where)
    kernel = guest(run, "journalctl -k -b --no-pager").decode("utf-8", "replace")
    run.keep(where, "journal-kernel.txt").write_text(kernel, encoding="utf-8")
    boot = m.serial_log.name.removeprefix("serial-").removesuffix(".log")
    argv = json.loads(run.keep(f"qemu-{boot}.argv.json").read_text(encoding="utf-8"))
    run.checks(
        name,
        [
            *sbproof.guest_checks(mokutil, efivar),
            sbproof.kernel_check(_text(m.serial_log)),
            sbproof.host_check(argv, run.fw, m.vars_file),
        ],
    )


def collect(run: Run) -> None:
    """The installer's logs and cloud-init's, as the installed system keeps them."""
    tar = guest(run, "tar -C /var/log -cf - installer")
    run.keep("guest", "var-log-installer.tar").write_bytes(tar)
    # cloud-init's own logs name the datasource it used; either may be absent.
    for name in ("cloud-init.log", "cloud-init-output.log"):
        guest_reading(run, f"var-log-{name}", f"cat /var/log/{name}")


def guest_leak_check(run: Run, where: str = "guest") -> None:
    """The guest searches itself, the secrets on stdin."""
    patterns = "\n".join(run.secrets).encode() + b"\n"
    proc = run.sh(ssh_argv(run, " ".join(leak.guest_grep_argv())), stdin=patterns)
    hits = proc.stdout.decode("utf-8", "replace").split()
    run.keep(where, "leak-check.txt").write_text(
        f"exit {proc.returncode}\n" + "\n".join(hits) + "\n", encoding="utf-8"
    )
    if hits:
        raise StepFailed(f"a secret is in the guest ({where}): " + ", ".join(hits[:20]))
    if proc.returncode not in {0, 1}:
        raise StepFailed(f"the guest's search exited {proc.returncode}")


def power_off(run: Run, proc: subprocess.Popen[bytes], m: qemu.Machine) -> None:
    """Shut the guest down so its disk is quiet before the image is read."""
    try:
        run.sh(ssh_argv(run, "sudo -n systemctl poweroff"), timeout_s=POWER_OFF_SSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log("   power-off over SSH did not return")
    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        stop(proc, m.qmp_socket)
    run.guest_up = False


# ── mode full: zark built, installed, backed up, recovered (Q3) ────────────


def build_zark(run: Run) -> None:
    """The package and the release tarball, from the commit checked out (Q3-1a)."""
    src = run.work / "src"
    run.sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(src)])
    shutil.rmtree(src, ignore_errors=True)
    for old in run.work.glob("zark_*"):
        old.unlink()
    run.must(["git", "-C", str(REPO), "worktree", "add", "--detach", str(src), "HEAD"])
    commit = run.must(["git", "-C", str(src), "rev-parse", "HEAD"]).strip()
    # make deb runs make dist; both write next to the tree (Makefile:281, :543).
    began = time.monotonic()
    run.must(["make", "-C", str(src), "deb"])
    debs = sorted(run.work.glob("zark_*_amd64.deb"))
    tarballs = sorted(p for p in src.glob("zark_*.tar.gz") if ".orig." not in p.name)
    if len(debs) != 1 or len(tarballs) != 1:
        raise StepFailed(f"make deb left {len(debs)} .deb and {len(tarballs)} tarball(s)")
    run.deb, run.tarball = debs[0], tarballs[0]
    fields = run.must(["dpkg-deb", "-f", str(run.deb), "Version", "Depends"])
    match = VERSION_RE.search((src / "lib" / "config.py").read_text(encoding="utf-8"))
    run.zark_version = match.group(1) if match else ""
    record = {
        "commit": commit,
        "version": run.zark_version,
        "seconds": round(time.monotonic() - began, 1),
        "control": fields.strip().splitlines(),
        **{
            kind: {"name": p.name, "bytes": p.stat().st_size, "sha256": iso.sha256_file(p)}
            for kind, p in (("deb", run.deb), ("tarball", run.tarball))
        },
    }
    run.keep("zark", "build.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


# zark's own version, read as debian/rules reads it.
VERSION_RE = re.compile(r'^VERSION = "([^"]+)"', re.M)


def scp_to_guest(run: Run, local: Path, remote: str) -> None:
    """A file into the guest, as the bench user."""
    assert run.key is not None
    run.must(
        [
            "scp",
            "-i",
            str(run.key),
            "-P",
            str(run.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={run.path('ssh', 'known_hosts')}",
            str(local),
            f"{seed.USERNAME}@127.0.0.1:{remote}",
        ]
    )


def keep_guest(run: Run, where: str, name: str, command: str) -> str:
    """A read-only command in the guest, as root, its output kept as out/<where>/<name>."""
    text = guest(run, command).decode("utf-8", "replace")
    run.keep(where, name).write_text(text, encoding="utf-8")
    return text


def apt_idle(run: Run) -> None:
    """apt's timers off, and apt idle, before zark runs (Q3-4a)."""
    guest(run, "systemctl disable --now apt-daily.timer apt-daily-upgrade.timer")
    began = time.monotonic()
    idle = 0
    while time.monotonic() - began < APT_IDLE_TIMEOUT_S:
        busy = run.sh(ssh_argv(run, f"pgrep -f '{APT_BUSY}'"), timeout_s=60)
        # pgrep exits 1 when nothing matches: two idle reads in a row.
        idle = idle + 1 if busy.returncode == 1 else 0
        if idle >= 2:
            waited = round(time.monotonic() - began, 1)
            run.keep("guest-installed", "apt-idle.txt").write_text(
                f"apt idle after {waited} s\n", encoding="utf-8"
            )
            return
        time.sleep(5)
    raise StepFailed(f"apt still busy after {APT_IDLE_TIMEOUT_S} s")


def system_key(run: Run) -> None:
    """rpool's raw key, into memory only, as a needle for the host's search (Q3-5a)."""
    key = guest(run, "cat /run/keystore/rpool/system.key")
    if len(key) < 16:
        raise StepFailed(f"the system key read back has {len(key)} bytes")
    run.byte_secrets.append(key)
    log(f"   rpool's key read: {len(key)} bytes, kept in memory as a needle")


def write_marker(run: Run) -> None:
    """Random data that must come back after the recover (Q3-7a); it opens nothing."""
    data = os.urandom(MARKER_BYTES)
    run.must(ssh_argv(run, "sudo -n tee " + " ".join(MARKER_PATHS) + " >/dev/null"), stdin=data)
    digest = hashlib.sha256(data).hexdigest()
    run.marker_sha256 = digest
    record = {"paths": list(MARKER_PATHS), "bytes": len(data), "sha256": digest}
    run.keep("zark", "marker.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )


def install_zark(run: Run) -> None:
    """The package into the installed system, as apt installs a release's .deb."""
    assert run.deb is not None
    scp_to_guest(run, run.deb, f"/tmp/{run.deb.name}")
    guest(run, "apt-get update -q")
    guest(run, f"env DEBIAN_FRONTEND=noninteractive apt-get install -y -q /tmp/{run.deb.name}")
    shown = keep_guest(run, "guest-installed", "zark-version.txt", "zark --version")
    keep_guest(run, "guest-installed", "zark-package.txt", "dpkg-query -W zark")
    run.checks(
        "zark-installed",
        [
            Check(
                "version",
                bool(run.zark_version) and run.zark_version in shown,
                (shown.strip().splitlines() or [""])[0],
            )
        ],
    )


def _progress(run: Run) -> Callable[[], str]:
    """Reads /run/zark/progress.json in the guest; "" when there is none."""

    def poll() -> str:
        try:
            got = run.sh(ssh_argv(run, "sudo -n cat /run/zark/progress.json"), timeout_s=30)
        except subprocess.TimeoutExpired:
            return ""
        return got.stdout.decode("utf-8", "replace") if got.returncode == 0 else ""

    return poll


def _ended(proc: subprocess.Popen[bytes]) -> int:
    """The SSH session's exit status (the command's), its input closed first."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        return proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def run_zark(  # pylint: disable=too-many-arguments
    run: Run, where: str, command: str, remote: str, passphrase: str, expect: drive.Expect
) -> tuple[bytes, Check]:
    """One zark command under a pty in the guest, answered from its table (Q3-3a)."""
    argv = ssh_argv(run, remote, tty=True)
    qemu.assert_no_secret(argv, run.secrets)
    with run.keep("commands.log").open("a", encoding="utf-8") as f:
        f.write(" ".join(argv) + "\n")
    path = run.keep("zark", f"{where}-{command}.transcript")
    # Outlives this call only until _ended() below.
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert proc.stdin is not None and proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    try:
        with path.open("wb") as transcript:
            outcome = drive.Session(
                drive.TABLES[command],
                expect,
                drive.pty_reader(proc.stdout.fileno()),
                drive.pipe_writer(proc.stdin),
                _progress(run),
                passphrase,
                transcript,
            ).run(ZARK_LIMIT_S[command])
    finally:
        status = _ended(proc)
    run.keep("zark", f"{where}-{command}.questions.json").write_text(
        json.dumps({"exit": status, "asked": outcome.asked}, indent=2) + "\n", encoding="utf-8"
    )
    output = path.read_bytes()
    return output, drive.verdict(command, status, output, outcome)


def plug_usb(run: Run, m: qemu.Machine, where: str) -> str:
    """The backup drive plugged in over QMP; its /dev/disk/by-id name in the guest."""
    q = Qmp.connect(m.qmp_socket, io_timeout_s=QMP_IO_TIMEOUT_S)
    try:
        q.execute("device_add", **qemu.usb_plug(USB_SERIAL))
    finally:
        q.close()
    deadline = time.monotonic() + USB_WAIT_S
    while True:
        names = guest(run, "ls -1 /dev/disk/by-id").decode("utf-8", "replace").splitlines()
        try:
            name = zarkcheck.by_id_name(names, USB_SERIAL)
        except zarkcheck.ReadBackError:
            if time.monotonic() > deadline:
                run.keep(where, "by-id.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
                raise
            time.sleep(3)
            continue
        run.keep(where, "by-id.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
        log(f"   backup drive: /dev/disk/by-id/{name}")
        return name


def zark_setup(run: Run, passphrase: str) -> None:
    """zark setup, then what it says it did."""
    _, check = run_zark(run, "installed", "setup", "sudo -n zark setup", passphrase, drive.Expect())
    conf = run.sh(ssh_argv(run, "test -s /etc/sanoid/sanoid.conf")).returncode == 0
    timer = run.sh(ssh_argv(run, "systemctl is-active sanoid.timer")).returncode == 0
    snaps = guest(run, "zfs list -H -t snapshot -o name -r rpool/ROOT").decode()
    autosnaps = sum(1 for ln in snaps.splitlines() if "@autosnap_" in ln)
    run.checks(
        "zark-setup",
        [
            check,
            Check("sanoid-conf", conf, "/etc/sanoid/sanoid.conf"),
            Check("sanoid-timer", timer, "sanoid.timer active"),
            Check(
                "first-snapshots", autosnaps > 0, f"{autosnaps} autosnap_ snapshot(s) in rpool/ROOT"
            ),
        ],
    )


def zark_prepare(run: Run, passphrase: str, m: qemu.Machine) -> None:
    """The backup drive plugged in, then zark prepare on its by-id name."""
    name = plug_usb(run, m, "guest-installed")
    remote = f"sudo -n zark prepare /dev/disk/by-id/{name}"
    _, check = run_zark(run, "installed", "prepare", remote, passphrase, drive.Expect())
    registry = guest(run, "cat /etc/zark/known_drives.json").decode("utf-8", "replace")
    run.checks("zark-prepare", [check, Check("registered", '"backup"' in registry, "backup")])


def status_record(run: Run, where: str) -> dict[str, object]:
    """status.json kept, and its backup drive's entry."""
    text = keep_guest(run, where, "status.json", "cat /var/lib/zark/status.json")
    return zarkcheck.status_drive(text, "backup")


def zark_backup(run: Run, passphrase: str, where: str, after: str = "") -> bytes:
    """zark backup on the plugged-in drive; its point recorded."""
    output, check = run_zark(
        run, where, "backup", "sudo -n zark backup", passphrase, drive.Expect()
    )
    entry = status_record(run, where)
    checks = [check, zarkcheck.last_run_check(entry, "last-run", after)]
    if where == "installed":
        run.point = str(entry.get("last_backup_point") or "")
        run.drive_guid = str(entry.get("guid") or "")
    else:
        text = console.clean(output).decode("utf-8", "replace")
        try:
            checks.append(zarkcheck.anchors_kept(zarkcheck.plan_states(text)))
        except zarkcheck.ReadBackError as err:
            checks.append(Check("anchors-kept", False, str(err)))
    run.checks(f"zark-backup-{where}", checks)
    return output


def collect_zark(run: Run, where: str) -> None:
    """What zark left in the guest, for the record."""
    for name, command in zarkcheck.SYSTEM_READS.items():
        guest_reading(run, name, command, f"guest-{where}")


def power_down(run: Run, booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]]) -> None:
    """The guest that is up, shut down; it is no longer the current one."""
    if booted:
        power_off(run, *booted.pop())


def installed_steps(
    run: Run, passphrase: str, booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]]
) -> bool:
    """Step 4: zark on the installed system, a drive prepared, a backup."""
    m = booted[-1][1]
    done = (
        run.step("system-key", lambda: system_key(run))
        and run.step("apt-idle", lambda: apt_idle(run))
        and run.step("marker", lambda: write_marker(run))
        and run.step("zark-install", lambda: install_zark(run))
        and run.step("zark-setup", lambda: zark_setup(run, passphrase))
        and run.step("zark-prepare", lambda: zark_prepare(run, passphrase, m))
        and run.step("zark-backup", lambda: _ignore(zark_backup(run, passphrase, "installed")))
    )
    # Whatever happened above, the guest searches itself before it goes.
    run.step("collect-installed", lambda: collect_zark(run, "installed"))
    searched = run.step("guest-leak-check", lambda: guest_leak_check(run, "guest-installed"))
    return bool(done) and searched


def _ignore(_value: object) -> None:
    """A step's function whose value is not the step's verdict."""


def make_live_seed(run: Run) -> Path:
    """The live's CIDATA image: the bench user, its key, the SSH server (Q3-6a)."""
    assert run.key is not None
    public = run.key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    text = seed.render_cloud_config(seed.live_user_data(public))
    user_data, meta = run.path("seed", "live-user-data"), run.path("seed", "live-meta-data")
    user_data.write_text(text, encoding="utf-8")
    meta.write_text(seed.meta_data(f"zark-bench-live-{int(time.time())}"), encoding="utf-8")
    image = run.path("seed", "live-seed.img")
    image.unlink(missing_ok=True)
    run.must(seed.cloud_localds_cmd(image, user_data, meta))
    # No secret in it: kept as it is.
    run.keep("live-seed-user-data.txt").write_text(text, encoding="utf-8")
    return image


def boot_live(
    run: Run, vars_file: Path, booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]]
) -> None:
    """The live with the backup drive's port and the new blank disk; SSH from its seed."""
    assert run.remastered is not None
    image = make_live_seed(run)
    new = run.path("new.qcow2")
    new.unlink(missing_ok=True)
    run.must(["qemu-img", "create", "-q", "-f", "qcow2", str(new), NEW_DISK_SIZE])
    run.images.append(new)
    m = machine(run, "live", vars_file)
    run.ssh_port = free_port()
    argv = qemu.live_argv(
        m, run.remastered, image, new, NEW_DISK_SERIAL, run.path("backup.qcow2"), run.ssh_port
    )
    proc = start(run, "live", argv)
    run.keep("qemu-live.argv.json").write_text(json.dumps(argv) + "\n", encoding="utf-8")
    booted.append((proc, m))
    q: Qmp | None = None
    try:
        q = Qmp.connect(m.qmp_socket, io_timeout_s=QMP_IO_TIMEOUT_S)
    except (OSError, QmpError) as err:
        log(f"   no QMP session: {err}; no screens")
    try:
        deadline = time.monotonic() + LIVE_SSH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise StepFailed(f"QEMU exited {proc.returncode} while the live started")
            if run.sh(ssh_argv(run, "true"), timeout_s=60).returncode == 0:
                run.guest_up = True
                run.seen_stages.add("live")
                return
            q = _screens(q, run, "live")
            time.sleep(15)
        raise StepFailed(f"no SSH in the live within {LIVE_SSH_TIMEOUT_S} s")
    finally:
        if q is not None:
            q.close()


def live_tools(run: Run) -> None:
    """What the live has of recover's tools, and whether apt has lists (recorded)."""
    guest_reading(run, "tools.txt", zarkcheck.LIVE_TOOLS, "guest-live")


def zark_recover(run: Run, passphrase: str, m: qemu.Machine) -> None:
    """The release tarball into /tmp, then zark recover onto the blank disk."""
    assert run.tarball is not None
    plug_usb(run, m, "guest-live")
    scp_to_guest(run, run.tarball, f"/tmp/{run.tarball.name}")
    guest(run, f"tar xzf /tmp/{run.tarball.name} -C /tmp")
    expect = drive.Expect(point=run.point, disk_serial=NEW_DISK_SERIAL)
    output, check = run_zark(
        run, "live", "recover", "sh -c 'cd /tmp/zark && sudo -n ./zark recover'", passphrase, expect
    )
    text = console.clean(output).decode("utf-8", "replace")
    run.anchored, not_anchored = zarkcheck.anchored_by_recover(text)
    run.checks(
        "zark-recover",
        [
            check,
            Check(
                "anchored",
                run.anchored > 0 and not not_anchored,
                f"{run.anchored} anchored; not anchored: {', '.join(not_anchored) or 'none'}",
            ),
        ],
    )


def collect_live(run: Run) -> None:
    """The live's own record of the recover."""
    for name, command in zarkcheck.LIVE_READS.items():
        guest_reading(run, name, command, "guest-live")


def live_steps(
    run: Run,
    vars_file: Path,
    passphrase: str,
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]],
) -> bool:
    """Step 5: the live, recover onto the new disk."""
    if not (
        run.step("live-remaster", lambda: remaster_iso(run, remaster.LIVE_ARGS, "live"))
        and run.step("live-boot", lambda: boot_live(run, vars_file, booted))
    ):
        return False
    m = booted[-1][1]
    if not (
        run.step(
            "live-secure-boot", lambda: prove_secure_boot(run, m, "guest-live", "live-secure-boot")
        )
        and run.step("live-tools", lambda: live_tools(run))
        and run.step("zark-recover", lambda: zark_recover(run, passphrase, m))
    ):
        run.step("collect-live", lambda: collect_live(run))
        run.step("guest-leak-check-live", lambda: guest_leak_check(run, "guest-live"))
        return False
    run.step("collect-live", lambda: collect_live(run))
    return run.step("guest-leak-check-live", lambda: guest_leak_check(run, "guest-live"))


def boot_recovered(
    run: Run,
    passphrase: str,
    vars_file: Path,
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]],
) -> None:
    """The recovered disk alone, the backup drive unplugged; one passphrase."""
    m = machine(run, "recovered", vars_file)
    run.ssh_port = free_port()
    argv = qemu.recovered_argv(
        m, run.path("new.qcow2"), NEW_DISK_SERIAL, run.path("backup.qcow2"), run.ssh_port
    )
    proc = start(run, "recovered", argv)
    run.keep("qemu-recovered.argv.json").write_text(json.dumps(argv) + "\n", encoding="utf-8")
    unlock_to_ssh(run, passphrase, proc, m, "recovered")
    run.seen_stages.add("recovered")
    booted.append((proc, m))


def recovered_data(run: Run) -> None:
    """The marker files back, and the drive's anchors on every restored dataset."""
    sums = guest(run, "sha256sum " + " ".join(MARKER_PATHS)).decode("utf-8", "replace")
    found: dict[str, str] = {}
    for line in sums.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            found[parts[1].strip()] = parts[0]
    bookmarks = keep_guest(
        run, "guest-recovered", "bookmarks.txt", "zfs list -H -t bookmark -o name -r rpool"
    )
    snaps = keep_guest(
        run, "guest-recovered", "bpool-snapshots.txt", "zfs list -H -t snapshot -o name -r bpool"
    )
    run.checks(
        "recovered-data",
        [
            *(
                zarkcheck.same_digest(f"marker {p}", run.marker_sha256, found.get(p, ""))
                for p in MARKER_PATHS
            ),
            zarkcheck.anchors_check(
                bookmarks.splitlines(), snaps.splitlines(), run.drive_guid, run.anchored
            ),
        ],
    )


def zark_finish(run: Run, passphrase: str) -> None:
    """zark finish, as the recovered system's first command (docs/GUIDE.md:258-263)."""
    _, check = run_zark(
        run, "recovered", "finish", "sudo -n zark finish", passphrase, drive.Expect()
    )
    run.checks("zark-finish", [check])


def recovered_steps(
    run: Run,
    vars_file: Path,
    passphrase: str,
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]],
) -> None:
    """Step 6: the recovered system boots, holds the data, backs up again."""
    if not run.step("recovered-boot", lambda: boot_recovered(run, passphrase, vars_file, booted)):
        return
    m = booted[-1][1]
    for name, fn in (
        (
            "recovered-secure-boot",
            lambda: prove_secure_boot(run, m, "guest-recovered", "recovered-secure-boot"),
        ),
        ("recovered-layout", lambda: check_layout(run, "guest-recovered", "recovered-layout")),
        ("recovered-data", lambda: recovered_data(run)),
    ):
        run.step(name, fn)

    def again() -> None:
        plug_usb(run, m, "guest-recovered")
        zark_backup(run, passphrase, "recovered", after=run.point)

    if run.step("zark-finish", lambda: zark_finish(run, passphrase)):
        run.step("zark-backup-again", again)
    run.step("collect-recovered", lambda: collect_zark(run, "recovered"))
    run.step("guest-leak-check-recovered", lambda: guest_leak_check(run, "guest-recovered"))


def make_backup_disk(run: Run) -> None:
    """The USB backup drive's blank image (Q3-2a)."""
    backup = run.path("backup.qcow2")
    backup.unlink(missing_ok=True)
    run.must(["qemu-img", "create", "-q", "-f", "qcow2", str(backup), BACKUP_DISK_SIZE])
    run.images.append(backup)


def _full_steps(
    run: Run, disk: Path, booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]]
) -> None:
    if not (
        run.step("build", lambda: build_zark(run))
        and _common(run)
        and run.step("inspect", lambda: inspect(run))
    ):
        return
    made: list[str] = []

    def secrets_step() -> None:
        made.extend(make_secrets(run))

    if not run.step("secrets", secrets_step):
        return
    passphrase, password = made[0], made[1]
    assert run.fw is not None
    vars_file = firmware.fresh_vars(run.fw, run.path("vars.fd"))
    image = run.path("seed", "seed.img")
    make_backup_disk(run)

    def boot_step() -> None:
        booted.append(boot_installed(run, passphrase, disk, vars_file))

    if not (
        run.step("seed", lambda: _ignore(make_seed(run, passphrase, password)))
        and run.step("install", lambda: install(run, image, disk, vars_file))
        and run.step("installed-boot", boot_step)
    ):
        return
    m = booted[-1][1]
    q2 = [
        run.step(name, fn)
        for name, fn in (
            ("secure-boot", lambda: prove_secure_boot(run, m)),
            ("layout", lambda: check_layout(run)),
            ("collect", lambda: collect(run)),
        )
    ]
    if not all(q2):
        # Steps 4 to 6 stand on Q2's checks; the guest still searches itself.
        run.step("guest-leak-check", lambda: guest_leak_check(run, "guest-installed"))
        return
    if not installed_steps(run, passphrase, booted):
        return
    power_down(run, booted)
    if not live_steps(run, vars_file, passphrase, booted):
        return
    power_down(run, booted)
    recovered_steps(run, vars_file, passphrase, booted)


def full_run(run: Run) -> int:
    """Mode full: Q2's install, then steps 4 to 6 (Q3)."""
    disk = run.path("target.qcow2")
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]] = []
    try:
        with vm.watchdog(vm.STEPS_BUDGET_S):
            _full_steps(run, disk, booted)
    except vm.BudgetExpired as err:
        log(f"FAIL {err}")
        run.out_of_time = True
    finally:
        try:
            power_down(run, booted)
        finally:
            run.sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(run.work / "src")])
            status = finish(run, disk)
    return status


# ── the end: the host's leak check, then the marker ────────────────────────


def host_leak_check(run: Run, disks: list[Path]) -> bool:
    """out/ and the disk images hold no secret."""
    needles = leak.needles_of(run.secrets)
    if run.key is not None and run.key.exists():
        needles.extend(leak.key_needles(run.key.read_text(encoding="utf-8")))
    needles.extend(run.byte_secrets)
    hits = [str(p.relative_to(run.out)) for p, _ in leak.scan_tree(run.out, needles)]
    # The images: the passphrase and rpool's raw key (Q2-3a, Q3-5a).
    in_images = needles[:1] + run.byte_secrets
    present = [d for d in disks if d.exists()]
    hits.extend(f"the disk image {d.name}" for d in present if leak.scan_file(d, in_images))
    for hit in hits:
        log(f"LEAK in {hit}")
    image = "".join(f" and {d.name}" for d in present)
    log(f"   leak check, host: {len(hits)} hit(s) in out/{image}")
    return not hits


def stages(run: Run) -> list[tuple[str, bool, int | None]]:
    """Each guest of the run: its name, up over SSH, its own search's status."""
    up = {"installed"} if run.guest_seen else set()
    return leak.stages_of(run.mode, up | run.seen_stages, run.steps)


def guest_leak_note(run: Run) -> str:
    """What the job log says of the guests' own searches, from the recorded steps."""
    up = {"installed"} if run.guest_seen else set()
    return leak.stage_notes(run.mode, up | run.seen_stages, run.steps)


def finish(run: Run, disk: Path | None) -> int:
    """Leak check, removal of every secret file, the upload marker, the exit status."""
    # A machine a failed or interrupted step left running would still hold
    # its disk image while the leak check reads it.
    for proc, qmp_socket in run.vms:
        stop(proc, qmp_socket)
    host_clean = True
    if run.mode in {"install", "full"}:
        log(guest_leak_note(run))
    try:
        host_clean = host_leak_check(run, [*([disk] if disk is not None else []), *run.images])
    except OSError as err:
        log(f"FAIL the leak check could not run: {err}")
        host_clean = False
    for path in (run.path("seed"), run.path("ssh")):
        shutil.rmtree(path, ignore_errors=True)
    may_upload, why = leak.stages_verdict(host_clean, stages(run))
    marker = run.out / leak.MARKER
    if may_upload:
        marker.write_text(why + "\n", encoding="utf-8")
        # The upload's if: reads this marker; the job log says so too.
        log(f"   leak check passed: {marker.name} written, out/ may be uploaded")
    else:
        log(f"the leak check failed: nothing is uploaded ({why})")
    ok = (
        may_upload
        and not run.out_of_time
        and bool(run.steps)
        and all(s["status"] == 0 for s in run.steps)
    )
    log(f"== {run.mode}: {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


def _common(run: Run) -> bool:
    return (
        run.step("iso", lambda: fetch_iso(run))
        and run.step("firmware", lambda: pick_firmware(run))
        and run.step("negative-control", lambda: negative_control(run))
        and run.step("remaster", lambda: remaster_iso(run))
    )


def probe(run: Run) -> int:
    """ISO, firmware, negative control, remaster and the ISO read."""
    try:
        with vm.watchdog(vm.STEPS_BUDGET_S):
            if _common(run):
                run.step("inspect", lambda: inspect(run))
    except vm.BudgetExpired as err:
        log(f"FAIL {err}")
        run.out_of_time = True
    finally:
        status = finish(run, None)
    return status


def _install_steps(
    run: Run, disk: Path, booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]]
) -> None:
    # The ISO read is cheap (seconds) and names the subiquity each install ran.
    if not (_common(run) and run.step("inspect", lambda: inspect(run))):
        return
    made: list[str] = []

    def secrets_step() -> None:
        made.extend(make_secrets(run))

    if not run.step("secrets", secrets_step):
        return
    passphrase, password = made[0], made[1]
    assert run.fw is not None
    vars_file = firmware.fresh_vars(run.fw, run.path("vars.fd"))
    image = run.path("seed", "seed.img")

    def seed_step() -> None:
        make_seed(run, passphrase, password)

    def boot_step() -> None:
        booted.append(boot_installed(run, passphrase, disk, vars_file))

    if not (
        run.step("seed", seed_step)
        and run.step("install", lambda: install(run, image, disk, vars_file))
        and run.step("installed-boot", boot_step)
    ):
        return
    m = booted[0][1]
    # All three only read the guest, so one failing does not stop the others:
    # each run then reports every reading it could take.
    for name, fn in (
        ("secure-boot", lambda: prove_secure_boot(run, m)),
        ("layout", lambda: check_layout(run)),
        ("collect", lambda: collect(run)),
    ):
        run.step(name, fn)
    if run.guest_up:
        run.step("guest-leak-check", lambda: guest_leak_check(run))


def install_run(run: Run) -> int:
    """The full Q2 run on one host."""
    disk = run.path("target.qcow2")
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]] = []
    try:
        # Steps only: power-off, the leak check and the upload get the time
        # kept back for them.
        with vm.watchdog(vm.STEPS_BUDGET_S):
            _install_steps(run, disk, booted)
    except vm.BudgetExpired as err:
        log(f"FAIL {err}")
        run.out_of_time = True
    finally:
        # finish() stops every QEMU, removes the secret files and runs the
        # leak check even when the power-off raises.
        try:
            if booted:
                power_off(run, *booted[0])
        finally:
            status = finish(run, disk)
    return status


def main(argv: list[str]) -> int:
    """``python3 -m bench {probe|install|full} --out DIR --work DIR [--iso NAME]``."""
    import argparse  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(prog="python3 -m bench")
    parser.add_argument("mode", choices=["probe", "install", "full"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--iso", default="", help="an ISO name; default the newest point release")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    if leak.drop_stale_marker(args.out):
        log(f"   removed {leak.MARKER} left in {args.out} by an earlier run")
    run = Run(mode=args.mode, out=args.out, work=args.work, iso_wanted=args.iso)
    if args.mode == "probe":
        return probe(run)
    return full_run(run) if args.mode == "full" else install_run(run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
