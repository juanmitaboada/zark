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
order and timing only; every decision lives in a tested module. It is
excluded from coverage by name (sonar-project.properties).
"""

from __future__ import annotations

import json
import os
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
    firmware,
    inspect_iso,
    iso,
    layout,
    leak,
    qemu,
    remaster,
    sbproof,
    seed,
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

    def sh(self, argv: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        """
        A command, logged by its arguments, which never carry a secret; a
        secret goes in ``stdin`` only.
        """
        qemu.assert_no_secret(argv, self.secrets)
        with self.keep("commands.log").open("a", encoding="utf-8") as f:
            f.write(" ".join(argv) + "\n")
        return subprocess.run(argv, input=stdin, check=False, capture_output=True)

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
        began = time.monotonic()
        status, detail = 0, ""
        try:
            fn()
        # A step that fails in any way is recorded as failed and stops the
        # run; the leak check and the upload decision still follow.
        except Exception as err:  # pylint: disable=broad-exception-caught
            status, detail = 1, f"{type(err).__name__}: {err}"
            log(f"FAIL {name}: {detail}")
        seconds = round(time.monotonic() - began, 1)
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


def remaster_iso(run: Run) -> None:
    """grub.cfg edited, boot records compared."""
    assert run.iso_path is not None
    original = run.path("grub.cfg.orig")
    original.unlink(missing_ok=True)
    run.must(remaster.extract_cmd(run.iso_path, remaster.GRUB_CFG, original))
    text = original.read_text(encoding="utf-8")
    edited = remaster.edit_grub_cfg(text)
    shutil.copyfile(original, run.keep("iso", "grub.cfg.orig"))
    run.keep("iso", "grub.cfg").write_text(edited, encoding="utf-8")
    new_cfg = run.path("grub.cfg")
    new_cfg.write_text(edited, encoding="utf-8")
    run.remastered = run.path("remastered.iso")
    run.remastered.unlink(missing_ok=True)
    run.must(remaster.remaster_cmd(run.iso_path, run.remastered, new_cfg))
    before = run.must(remaster.report_cmd(run.iso_path))
    after = run.must(remaster.report_cmd(run.remastered))
    run.keep("iso", "boot-records.orig.txt").write_text(before, encoding="utf-8")
    run.keep("iso", "boot-records.txt").write_text(after, encoding="utf-8")
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
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=err, stderr=err)


def stop(proc: subprocess.Popen[bytes], qmp_socket: Path) -> None:
    """Ask QEMU to quit, then make sure it did."""
    if proc.poll() is None:
        try:
            q = Qmp.connect(qmp_socket, timeout_s=5)
            q.execute("quit")
            q.close()
        except (OSError, QmpError):
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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


def _screens(q: Qmp | None, run: Run, label: str) -> None:
    if q is None:
        return
    try:
        q.screendump(run.keep("screens", f"{label}-{int(time.time())}.png"))
    except (OSError, QmpError):
        pass


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
        q = Qmp.connect(m.qmp_socket)
    except OSError:
        q = None
    deadline = time.monotonic() + INSTALL_TIMEOUT_S
    next_screen = 0.0
    while proc.poll() is None and time.monotonic() < deadline:
        if time.monotonic() >= next_screen:
            _screens(q, run, "install")
            next_screen = time.monotonic() + SCREEN_EVERY_S
        time.sleep(5)
    if proc.poll() is None:
        _screens(q, run, "install-timeout")
        stop(proc, m.qmp_socket)
        raise StepFailed(f"the install did not end within {INSTALL_TIMEOUT_S} s")
    if proc.returncode != 0:
        raise StepFailed(f"QEMU exited {proc.returncode} during the install")


def ssh_argv(run: Run, command: str) -> list[str]:
    """SSH into the guest as the bench user; the command is a remote shell line."""
    assert run.key is not None
    return [
        "ssh",
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
    argv = qemu.installed_argv(m, disk, run.ssh_port)
    proc = start(run, "installed", argv)
    run.keep("qemu-installed.argv.json").write_text(json.dumps(argv) + "\n", encoding="utf-8")
    with run.keep("console-installed.log").open("wb") as transcript:
        con = console.Console.connect(m.serial_socket, transcript)
        try:
            answered = console.unlock(con, passphrase, UNLOCK_TIMEOUT_S, LOGIN_TIMEOUT_S)
        except console.ConsoleError as err:
            raise StepFailed(str(err)) from err
        finally:
            con.close()
    log(f"   unlock prompt answered: {answered}")
    deadline = time.monotonic() + SSH_TIMEOUT_S
    while time.monotonic() < deadline:
        if run.sh(ssh_argv(run, "true")).returncode == 0:
            run.guest_up = True
            return proc, m
        time.sleep(5)
    raise StepFailed(f"no SSH within {SSH_TIMEOUT_S} s")


def guest(run: Run, command: str) -> bytes:
    """A read-only command in the guest, as root; its output, filed by name."""
    proc = run.sh(ssh_argv(run, f"sudo -n {command}"))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise StepFailed(f"guest: {command} exited {proc.returncode}: {err}")
    return proc.stdout


def check_layout(run: Run) -> None:
    """The installer's layout; every listing kept for Q3."""
    outputs: dict[str, str] = {}
    for name, command in layout.COMMANDS.items():
        outputs[name] = guest(run, command).decode("utf-8", "replace")
        run.keep("guest", f"{name}.txt").write_text(outputs[name], encoding="utf-8")
    run.checks("layout", layout.check(outputs, seed.USERNAME))


def prove_secure_boot(run: Run, m: qemu.Machine) -> None:
    """Secure Boot from the guest, the kernel's line and the host's command line."""
    assert run.fw is not None
    mokutil = guest(run, "mokutil --sb-state").decode("utf-8", "replace")
    efivar = guest(run, f"cat {sbproof.EFIVAR}")
    kernel = guest(run, "journalctl -k -b --no-pager").decode("utf-8", "replace")
    run.keep("guest", "journal-kernel.txt").write_text(kernel, encoding="utf-8")
    argv = json.loads(run.keep("qemu-installed.argv.json").read_text(encoding="utf-8"))
    run.checks(
        "secure-boot",
        [
            *sbproof.guest_checks(mokutil, efivar),
            sbproof.kernel_check(_text(m.serial_log)),
            sbproof.host_check(argv, run.fw),
        ],
    )


def collect(run: Run) -> None:
    """The installer's logs, as the installed system keeps them."""
    tar = guest(run, "tar -C /var/log -cf - installer")
    run.keep("guest", "var-log-installer.tar").write_bytes(tar)


def guest_leak_check(run: Run) -> None:
    """The guest searches itself, the secrets on stdin."""
    patterns = "\n".join(run.secrets).encode() + b"\n"
    proc = run.sh(ssh_argv(run, " ".join(leak.guest_grep_argv())), stdin=patterns)
    hits = proc.stdout.decode("utf-8", "replace").split()
    run.keep("guest", "leak-check.txt").write_text(
        f"exit {proc.returncode}\n" + "\n".join(hits) + "\n", encoding="utf-8"
    )
    if hits:
        raise StepFailed("a secret is in the installed system: " + ", ".join(hits[:20]))
    if proc.returncode not in {0, 1}:
        raise StepFailed(f"the guest's search exited {proc.returncode}")


def power_off(run: Run, proc: subprocess.Popen[bytes], m: qemu.Machine) -> None:
    """Shut the guest down so its disk is quiet before the image is read."""
    run.sh(ssh_argv(run, "sudo -n systemctl poweroff"))
    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        stop(proc, m.qmp_socket)
    run.guest_up = False


# ── the end: the host's leak check, then the marker ────────────────────────


def host_leak_check(run: Run, disk: Path | None) -> bool:
    """out/ and the disk image hold no secret."""
    needles = leak.needles_of(run.secrets)
    if run.key is not None and run.key.exists():
        body = "".join(
            ln
            for ln in run.key.read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("-----")
        )
        needles.append(body.encode())
    hits = [str(p.relative_to(run.out)) for p, _ in leak.scan_tree(run.out, needles)]
    if disk is not None and disk.exists() and leak.scan_file(disk, needles[:1]):
        hits.append(f"the disk image {disk.name}")
    for hit in hits:
        log(f"LEAK in {hit}")
    return not hits


def finish(run: Run, disk: Path | None) -> int:
    """Leak check, removal of every secret file, the upload marker, the exit status."""
    clean = True
    try:
        clean = host_leak_check(run, disk)
    except OSError as err:
        log(f"FAIL the leak check could not run: {err}")
        clean = False
    for path in (run.path("seed"), run.path("ssh")):
        shutil.rmtree(path, ignore_errors=True)
    marker = run.out / "LEAK-CHECK-PASSED"
    if clean:
        marker.write_text("no secret found in out/ or the disk image\n", encoding="utf-8")
    else:
        log("the leak check failed: nothing is uploaded")
    ok = clean and bool(run.steps) and all(s["status"] == 0 for s in run.steps)
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
        if _common(run):
            run.step("inspect", lambda: inspect(run))
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
    for name, fn in (
        ("secure-boot", lambda: prove_secure_boot(run, m)),
        ("layout", lambda: check_layout(run)),
        ("collect", lambda: collect(run)),
    ):
        if not run.step(name, fn):
            break
    if run.guest_up:
        run.step("guest-leak-check", lambda: guest_leak_check(run))


def install_run(run: Run) -> int:
    """The full Q2 run on one host."""
    disk = run.path("target.qcow2")
    booted: list[tuple[subprocess.Popen[bytes], qemu.Machine]] = []
    try:
        _install_steps(run, disk, booted)
    finally:
        if booted:
            power_off(run, *booted[0])
        status = finish(run, disk)
    return status


def main(argv: list[str]) -> int:
    """``python3 -m bench {probe|install} --out DIR --work DIR [--iso NAME]``."""
    import argparse  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(prog="python3 -m bench")
    parser.add_argument("mode", choices=["probe", "install"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--iso", default="", help="an ISO name; default the newest point release")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    run = Run(mode=args.mode, out=args.out, work=args.work, iso_wanted=args.iso)
    return probe(run) if args.mode == "probe" else install_run(run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
