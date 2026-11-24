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
# pylint: disable=too-many-lines
"""
Tests of the VM bench (bench/): everything that needs no virtual machine,
fed with text shaped like the real outputs and with local sockets.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

from bench import (
    console,
    drive,
    firmware,
    inspect_iso,
    iso,
    layout,
    leak,
    qemu,
    remaster,
    run as bench_run,
    sbproof,
    seed,
    vm,
    zarkcheck,
)
from bench.qmp import Qmp, QmpError
from bench.report import Check, all_ok, summary, write_checks

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "bench"

LISTING = """
<a href="ubuntu-26.04-desktop-amd64.iso">ubuntu-26.04-desktop-amd64.iso</a>
<a href="ubuntu-26.04-desktop-amd64.iso.torrent">torrent</a>
<a href="ubuntu-26.04.1-desktop-amd64.iso">ubuntu-26.04.1-desktop-amd64.iso</a>
<a href="ubuntu-26.04.1-desktop-amd64.iso.zsync">zsync</a>
<a href="ubuntu-26.04.1-live-server-amd64.iso">server</a>
"""

# The shape of an Ubuntu desktop ISO's grub.cfg (first entry, then others).
GRUB_CFG = """set timeout=30

loadfont unicode

menuentry "Try or Install Ubuntu" {
\tset gfxpayload=keep
\tlinux\t/casper/vmlinuz  layerfs-path=minimal.standard.live.squashfs --- quiet splash
\tinitrd\t/casper/initrd
}
menuentry "Ubuntu (safe graphics)" {
\tlinux\t/casper/vmlinuz  nomodeset --- quiet splash
\tinitrd\t/casper/initrd
}
"""

VALIDSIG = (
    "[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG D94AA3F0EFE21092 Ubuntu CD Image\n"
    "[GNUPG:] VALIDSIG {fpr} 2026-08-27 1787000000 0 4 0 1 8 00 {primary}\n"
)


class TestBenchIso:  # pylint: disable=missing-function-docstring
    """The ISO chosen from the listing and trusted only through its signed sums."""

    def test_the_newest_point_release_is_chosen(self):
        assert iso.newest_desktop_iso(LISTING, "26.04") == "ubuntu-26.04.1-desktop-amd64.iso"
        choice = iso.choose(LISTING, "26.04")
        assert choice.url == "https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso"

    def test_a_named_iso_must_be_a_bare_name_in_the_listing(self):
        assert iso.choose(LISTING, "26.04", "ubuntu-26.04-desktop-amd64.iso").name.endswith(
            "26.04-desktop-amd64.iso",
        )
        for bad in ("../x.iso", "ubuntu-26.04.9-desktop-amd64.iso", "a b"):
            try:
                iso.choose(LISTING, "26.04", bad)
            except iso.IsoError:
                continue
            raise AssertionError(bad)

    def test_no_iso_in_the_listing_is_an_error(self):
        try:
            iso.newest_desktop_iso("nothing here", "26.04")
        except iso.IsoError as err:
            assert "26.04" in str(err)
        else:
            raise AssertionError("no error")

    def test_sha256sums_parse_both_markers(self):
        text = f"{'a' * 64} *ubuntu-26.04.1-desktop-amd64.iso\n{'B' * 64}  other.iso\njunk\n"
        sums = iso.parse_sha256sums(text)
        assert sums == {"ubuntu-26.04.1-desktop-amd64.iso": "a" * 64, "other.iso": "b" * 64}

    def test_only_a_valid_signature_by_the_pinned_key_passes(self):
        fpr = iso.CDIMAGE_FINGERPRINT
        assert iso.signature_error(VALIDSIG.format(fpr=fpr, primary=fpr)) == ""
        # A subkey signs, the primary is the pinned key: accepted.
        assert iso.signature_error(VALIDSIG.format(fpr="0" * 40, primary=fpr)) == ""
        other = VALIDSIG.format(fpr="1" * 40, primary="1" * 40)
        assert "not " + fpr in iso.signature_error(other)
        assert iso.signature_error("[GNUPG:] BADSIG D94AA3F0EFE21092 x\n") == "gpgv reported BADSIG"
        assert iso.signature_error("") == "gpgv reported no valid signature"
        assert "ERRSIG" in iso.signature_error(
            "[GNUPG:] ERRSIG 1 2 3\n" + VALIDSIG.format(fpr=fpr, primary=fpr)
        )

    def test_gpgv_is_run_with_the_status_on_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "gpgv"
            fake.write_text('#!/bin/sh\necho "[GNUPG:] ARGS $*"\n', encoding="utf-8")
            fake.chmod(0o755)
            out = iso.gpgv_status(Path("S"), Path("S.gpg"), Path("k.gpg"), gpgv=str(fake))
        assert out.strip() == "[GNUPG:] ARGS --status-fd 1 --keyring k.gpg S.gpg S"

    def test_sha256_and_download_from_a_file_url(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.iso"
            src.write_bytes(b"x" * (3 * (1 << 20) + 7))
            dest = Path(td) / "dest.iso"
            iso.download(src.as_uri(), dest)
            assert iso.sha256_file(dest) == iso.sha256_file(src)
            assert not (Path(td) / "dest.iso.part").exists()

    def test_a_failed_download_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "dest.iso"
            missing = (Path(td) / "missing.iso").as_uri()
            try:
                iso.download(missing, dest, attempts=2, pause_s=0)
            except iso.IsoError:
                pass
            else:
                raise AssertionError("no error")
            assert not list(Path(td).iterdir())


class TestBenchRemaster:  # pylint: disable=missing-function-docstring
    """grub.cfg's first entry edited before ---, nothing else on the ISO."""

    def test_the_args_go_before_the_separator_and_splash_goes(self):
        out = remaster.edit_grub_cfg(GRUB_CFG)
        args = remaster.first_linux_args(out)
        cut = args.index("---")
        assert args[cut:] == ["---", "quiet"]
        for arg in remaster.INSTALL_ARGS:
            assert arg in args[:cut]
        assert "splash" not in args
        # The second entry is untouched.
        assert "\tlinux\t/casper/vmlinuz  nomodeset --- quiet splash" in out
        assert out.startswith("set timeout=1\n")

    def test_an_arg_already_there_is_not_doubled(self):
        out = remaster.edit_grub_cfg(GRUB_CFG.replace("layerfs", "autoinstall layerfs"))
        assert remaster.first_linux_args(out).count("autoinstall") == 1

    def test_no_separator_appends_and_no_timeout_adds_one(self):
        text = 'menuentry "x" {\n  linux /vmlinuz quiet splash\n}\n'
        out = remaster.edit_grub_cfg(text, ("a=1",), timeout_s=2)
        assert out.splitlines()[0] == "set timeout=2"
        assert "  linux /vmlinuz quiet a=1" in out

    def test_no_entry_is_refused(self):
        for text in ("set timeout=3\n", 'menuentry "x" {\n  initrd /i\n}\nlinux /v\n'):
            try:
                remaster.edit_grub_cfg(text)
            except remaster.RemasterError:
                continue
            raise AssertionError(text)
        assert remaster.first_linux_args("nothing") == []

    def test_xorriso_command_lines(self):
        iso_path, out, cfg = Path("/w/in.iso"), Path("/w/out.iso"), Path("/w/grub.cfg")
        cmd = remaster.remaster_cmd(iso_path, out, cfg)
        assert cmd[:5] == ["xorriso", "-indev", "/w/in.iso", "-outdev", "/w/out.iso"]
        assert cmd[-3:] == ["any", "replay"][:0] + ["-boot_image", "any", "replay"][-3:]
        assert ["-map", "/w/grub.cfg", "/boot/grub/grub.cfg"] == cmd[5:8]
        assert remaster.extract_cmd(iso_path, "/x", out)[-3:] == ["-extract", "/x", "/w/out.iso"]
        assert "-report_el_torito" in remaster.report_cmd(iso_path)

    def test_boot_shape_ignores_addresses_guids_and_gaps(self):
        before = (FIXTURES / "boot-records.txt").read_text(encoding="utf-8")
        shape = remaster.boot_shape(before)
        assert "eltorito UEFI y none" in shape
        assert not any("Gap" in line for line in shape)
        moved = re.sub(r"\b\d{2,}\b", "4242", before).replace("e0cc9e38", "deadbeef")
        moved += "GPT partname local :   9  Gap7\nGPT type GUID      :   9  a2a0\n"
        assert remaster.boot_shape(moved) == shape
        changed = before.replace("UEFI  y", "UEFI  n")
        assert remaster.boot_shape(changed) != shape


class TestBenchSeed:  # pylint: disable=missing-function-docstring
    """The autoinstall document and the files of the CIDATA seed."""

    def _doc(self) -> dict[str, object]:
        return seed.autoinstall("PASS", "$6$hash", "ssh-ed25519 AAAA bench")

    def test_the_user_data_is_cloud_config_json(self):
        text = seed.render_user_data(self._doc())
        first, rest = text.split("\n", 1)
        assert first == "#cloud-config"
        doc = json.loads(rest)["autoinstall"]
        assert doc["storage"] == {"layout": {"name": "zfs", "password": "PASS"}}
        assert doc["ssh"]["install-server"] is True and doc["ssh"]["allow-pw"] is False
        assert doc["identity"]["username"] == seed.USERNAME
        assert doc["shutdown"] == "poweroff"
        assert doc["refresh-installer"] == {"update": False}
        assert "interactive-sections" not in doc

    def test_late_commands_set_the_serial_console_and_sudo_only(self):
        cmds = seed.late_commands()
        flat = "\n".join(" ".join(c) for c in cmds)
        assert "update-grub" in flat and "visudo" in flat
        assert "/target/etc/default/grub.d/90-zark-bench.cfg" in flat
        assert "console=ttyS0,115200n8" in flat and "splash" not in flat
        for word in ("zpool", "zfs ", "cryptsetup", "shim", "grub-install", "keystore"):
            assert word not in flat, word

    def test_the_written_files_survive_the_shell(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            (target / "etc" / "sudoers.d").mkdir(parents=True)
            for cmd in seed.late_commands():
                if cmd[0] in {"sh", "mkdir"}:
                    subprocess.run([a.replace("/target", td) for a in cmd], check=True)
            assert (target / "etc/sudoers.d/bench").read_text() == seed.SUDOERS_LINE + "\n"
            dropin = (target / "etc/default/grub.d/90-zark-bench.cfg").read_text()
            assert dropin == seed.GRUB_DROPIN + "\n"
            assert oct((target / "etc/sudoers.d/bench").stat().st_mode & 0o777) == "0o440"

    def test_meta_data_and_cloud_localds(self):
        assert seed.meta_data("i-1") == "instance-id: i-1\nlocal-hostname: bench\n"
        cmd = seed.cloud_localds_cmd(Path("s.img"), Path("u"), Path("m"))
        assert cmd == ["cloud-localds", "s.img", "u", "m"]
        assert "ttyS0" in seed.error_commands()[0][2]


def _descriptor(td: Path, name: str, **over: object) -> Path:
    doc = json.loads((FIXTURES / "40-edk2-x86_64-secure-enrolled.json").read_text())
    code, template = td / f"{name}.code", td / f"{name}.vars"
    code.write_bytes(b"c")
    template.write_bytes(b"v")
    doc["mapping"]["executable"]["filename"] = str(code)
    doc["mapping"]["nvram-template"]["filename"] = str(template)
    doc.update(over)
    path = td / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class TestBenchFirmware:  # pylint: disable=missing-function-docstring
    """The Secure Boot firmware picked from QEMU's descriptors by what they declare."""

    def test_the_noble_descriptors_offer_exactly_one(self):
        fits = [
            p.name
            for p in sorted(FIXTURES.glob("*-edk2-*.json"))
            if firmware.offers_secure_boot(json.loads(p.read_text()))
        ]
        assert fits == ["40-edk2-x86_64-secure-enrolled.json"]

    def test_the_first_fitting_descriptor_with_its_files_wins(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            plain = _descriptor(t, "10-plain", features=["acpi-s3"])
            good = _descriptor(t, "40-good")
            later = _descriptor(t, "50-later")
            gone = _descriptor(t, "30-gone")
            (t / "30-gone.code").unlink()
            fw = firmware.pick([later, gone, plain, good])
            assert fw.descriptor == good and fw.code == t / "40-good.code"
            copy = firmware.fresh_vars(fw, t / "mine.fd")
            assert copy.read_bytes() == b"v"
            assert firmware.find(t).descriptor == good

    def test_none_fitting_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            bad = [
                _descriptor(t, "a", **{"interface-types": ["bios"]}),
                _descriptor(
                    t, "b", targets=[{"architecture": "x86_64", "machines": ["pc-i440fx-*"]}]
                ),
                _descriptor(t, "c", mapping={"device": "memory"}),
                _descriptor(t, "d", targets="x"),
            ]
            (t / "e.json").write_text("not json", encoding="utf-8")
            try:
                firmware.pick([*bad, t / "e.json"])
            except firmware.FirmwareError:
                return
            raise AssertionError("no error")


def _machine(t: Path) -> qemu.Machine:
    fw = firmware.Firmware(t / "code,x.fd", t / "vars.fd", t / "d.json")
    return qemu.Machine(fw, t / "my vars.fd", t / "s.sock", t / "s.log", t / "q.sock")


class TestBenchQemu:  # pylint: disable=missing-function-docstring
    """QEMU's command lines: Secure Boot's machine shape, boot order, no secret."""

    def test_the_machine_has_smm_a_secure_flash_and_the_firmware(self):
        argv = qemu.base_argv(_machine(Path("/w")), "x")
        assert "q35,smm=on,accel=kvm" in argv and "host" in argv
        assert "driver=cfi.pflash01,property=secure,value=on" in argv
        assert "if=pflash,format=raw,unit=0,readonly=on,file=/w/code,,x.fd" in argv
        assert any(a.endswith("logfile=/w/s.log") for a in argv)

    def test_tcg_uses_a_generic_cpu(self):
        m = _machine(Path("/w"))
        tcg = qemu.Machine(
            m.firmware, m.vars_file, m.serial_socket, m.serial_log, m.qmp_socket, accel="tcg"
        )
        assert "max" in qemu.base_argv(tcg, "x")

    def test_the_install_boots_the_cd_then_the_disk(self):
        argv = qemu.install_argv(
            _machine(Path("/w")), Path("/w/r.iso"), Path("/w/seed.img"), Path("/w/t.qcow2"), 2222
        )
        joined = " ".join(argv)
        assert "ide-cd,drive=cd0,bus=ide.0,bootindex=1" in joined
        assert "ide-cd,drive=seed,bus=ide.1 " in joined
        assert "virtio-blk-pci,drive=target,bootindex=2" in joined
        assert "hostfwd=tcp:127.0.0.1:2222-:22" in joined

    def test_the_installed_boot_has_no_cd_and_the_negative_no_net(self):
        installed = " ".join(qemu.installed_argv(_machine(Path("/w")), Path("/w/t"), 1))
        assert "ide-cd" not in installed and "bootindex=1" in installed
        negative = qemu.negative_argv(_machine(Path("/w")), Path("/w/esp.img"))
        assert negative[-2:] == ["-net", "none"]

    def test_a_secret_in_any_argument_stops_the_machine(self):
        argv = qemu.installed_argv(_machine(Path("/w")), Path("/w/disk-SECRET"), 1)
        try:
            qemu.assert_no_secret(argv, ["SECRET"])
        except qemu.SecretInArgv as err:
            assert "SECRET" not in str(err)
        else:
            raise AssertionError("no error")
        qemu.assert_no_secret(argv, ["", "absent"])


def _qmp_server(path: Path, answers: list[str]) -> threading.Thread:
    """
    A fake QEMU answering one line per command it reads. After its answers it
    still reads one more command before it closes, so the client's last send
    always reaches an open socket and the read that follows always finds it
    closed: no step depends on which thread runs first.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve() -> None:
        conn, _ = srv.accept()
        f = conn.makefile("rw", encoding="utf-8")
        f.write('{"QMP": {"version": {}}}\n')
        f.flush()
        for answer in answers:
            if not f.readline():
                break
            f.write(answer + "\n")
            f.flush()
        f.readline()  # the command it will not answer
        f.close()
        conn.close()
        srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


class TestBenchQmp:  # pylint: disable=missing-function-docstring
    """The QMP client against a local fake."""

    def test_commands_events_and_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            thread = _qmp_server(
                path,
                [
                    '{"return": {}}',
                    '{"event": "STOP"}\n{"return": {"status": "ok"}}',
                    '{"error": {"desc": "no such thing"}}',
                    '{"return": {}}',
                ],
            )
            q = Qmp.connect(path, timeout_s=5)
            assert q.execute("query-status") == {"status": "ok"}
            assert [e["event"] for e in q.events] == ["STOP"]
            try:
                q.execute("bogus", x=1)
            except QmpError as err:
                assert "no such thing" in str(err)
            else:
                raise AssertionError("no error")
            q.screendump(Path(td) / "s.png")
            try:
                q.execute("quit")
            except QmpError as err:
                assert str(err) == "QMP socket closed", str(err)
            else:
                raise AssertionError("no error")
            q.close()
            thread.join(5)
            assert not thread.is_alive()

    def test_a_send_to_a_closed_peer_is_a_closed_socket(self):
        ours, theirs = socket.socketpair()
        theirs.sendall(b'{"QMP": {}}\n{"return": {}}\n')
        q = Qmp(ours)
        theirs.close()  # QEMU gone before the next command: the send fails
        try:
            q.execute("quit")
        except QmpError as err:
            assert str(err).startswith("QMP socket closed sending quit: "), str(err)
        else:
            raise AssertionError("no error")
        q.close()

    def test_a_reply_that_is_not_qmp(self):
        for first in (b"[1]\n", b'{"greeting": 1}\n'):
            ours, theirs = socket.socketpair()
            theirs.sendall(first)
            try:
                Qmp(ours)
            except QmpError:
                pass
            else:
                raise AssertionError(first)
            ours.close()
            theirs.close()

    def test_a_line_that_is_not_json_or_not_utf8_is_a_qmp_error(self):
        # vm.stop catches QmpError and OSError: a ValueError would escape it.
        for first in (b"{not json\n", b"\xff\xfe\n"):
            ours, theirs = socket.socketpair()
            theirs.sendall(first)
            try:
                Qmp(ours)
            except QmpError as err:
                assert str(err).startswith("not a QMP message: "), str(err)
            else:
                raise AssertionError(first)
            ours.close()
            theirs.close()

    def test_a_missing_socket_times_out(self):
        try:
            Qmp.connect(Path("/nonexistent/q.sock"), timeout_s=0.1)
        except OSError:
            return
        raise AssertionError("no error")


def _mute_qmp(path: Path, greet: bool = True) -> tuple[socket.socket, threading.Event]:
    """
    A fake QEMU whose monitor stops answering: it accepts, sends the greeting
    (or not), then reads and never replies, as in run 45147805605's hang.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(4)
    done = threading.Event()

    def serve() -> None:
        conns = []
        srv.settimeout(0.1)
        while not done.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conns.append(conn)
            if greet:
                conn.sendall(b'{"QMP": {"version": {}}}\n')
        for conn in conns:
            conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, done


def _garbage_qmp(path: Path) -> threading.Thread:
    """A fake QEMU whose monitor answers with a line that is not JSON."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve() -> None:
        conn, _ = srv.accept()
        conn.sendall(b"{garbage\n")
        conn.recv(1)  # until the client closes
        conn.close()
        srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


class TestBenchQmpBounded:  # pylint: disable=missing-function-docstring
    """Every QMP wait ends: run 45147805605 waited on a monitor for 2 hours."""

    def test_a_command_with_no_answer_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            _, done = _mute_qmp(path)
            began = time.monotonic()
            try:
                Qmp.connect(path, timeout_s=5, io_timeout_s=0.3)
            except QmpError as err:
                # The greeting came, qmp_capabilities got no answer.
                assert str(err) == "QMP: no answer within 0.3 s", str(err)
            else:
                raise AssertionError("no error")
            finally:
                done.set()
            assert time.monotonic() - began < 3

    def test_a_greeting_that_never_comes_is_an_error_not_a_retry(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            _, done = _mute_qmp(path, greet=False)
            began = time.monotonic()
            try:
                Qmp.connect(path, timeout_s=30, io_timeout_s=0.3)
            except QmpError as err:
                assert "no answer within 0.3 s" in str(err)
            else:
                raise AssertionError("no error")
            finally:
                done.set()
            assert time.monotonic() - began < 3

    def test_a_send_nobody_reads_times_out(self):
        ours, theirs = socket.socketpair()
        theirs.sendall(b'{"QMP": {}}\n{"return": {}}\n')
        q = Qmp(ours, io_timeout_s=0.3)
        try:
            # Far more than a socket buffer, and the peer never reads.
            q.execute("human-monitor-command", command_line="x" * (64 << 20))
        except QmpError as err:
            assert str(err) == "QMP: human-monitor-command not taken within 0.3 s", str(err)
        else:
            raise AssertionError("no error")
        q.close()
        theirs.close()

    def test_status_and_a_timeout_after_answers(self):
        ours, theirs = socket.socketpair()
        theirs.sendall(b'{"QMP": {}}\n{"return": {}}\n{"return": {"status": "paused"}}\n')
        q = Qmp(ours, io_timeout_s=0.3)
        assert q.status() == "paused"
        try:
            q.screendump(Path("/x.png"))  # read by the fake, never answered
        except QmpError as err:
            assert "no answer within 0.3 s" in str(err)
        else:
            raise AssertionError("no error")
        q.close()
        theirs.close()


def _child(code: str) -> subprocess.Popen[bytes]:
    # Ended by the test, through vm.stop(): a with block would wait for it.
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        ["python3", "-c", code], stdout=subprocess.PIPE
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == b"ready\n"  # its handlers are in place
    return proc


class _NoQemuImg(bench_run.Run):
    """A run whose ``must`` (only qemu-img, in install()) does nothing."""

    def must(self, argv: list[str], stdin: bytes | None = None) -> str:
        return ""


class TestBenchVm:  # pylint: disable=missing-function-docstring
    """QEMU always ends, and the steps always leave time for the leak check."""

    def test_stop_an_exited_process(self):
        with subprocess.Popen(["true"]) as proc:
            proc.wait()
        assert vm.stop(proc, Path("/nonexistent/q.sock"), qmp_timeout_s=0.1) == "exited"

    def test_stop_with_no_monitor_terminates(self):
        proc = _child("import time; print('ready', flush=True); time.sleep(60)")
        how = vm.stop(proc, Path("/nonexistent/q.sock"), qmp_timeout_s=0.1, quit_wait_s=0.2)
        assert how == "terminated" and proc.returncode == -15

    def test_stop_with_a_mute_monitor_and_sigterm_ignored_kills(self):
        code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            _, done = _mute_qmp(path)
            proc = _child(code)
            began = time.monotonic()
            try:
                how = vm.stop(proc, path, qmp_timeout_s=0.3, quit_wait_s=0.2, term_wait_s=0.2)
            finally:
                done.set()
            assert how == "killed" and proc.returncode == -9
            assert time.monotonic() - began < 5

    def test_stop_with_a_monitor_that_sends_no_json_terminates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            thread = _garbage_qmp(path)
            proc = _child("import time; print('ready', flush=True); time.sleep(60)")
            how = vm.stop(proc, path, qmp_timeout_s=2, quit_wait_s=0.2)
            assert how == "terminated" and proc.returncode == -15
            thread.join(5)

    def test_stop_by_quit(self):
        # A fake QEMU that ends when it is told to quit.
        code = (
            "import socket, sys; s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); "
            "s.listen(1); print('ready', flush=True); c, _ = s.accept(); "
            "f = c.makefile('rw'); f.write('{\"QMP\": {}}\\n'); f.flush(); "
            "[(f.write('{\"return\": {}}\\n'), f.flush()) for _ in range(2) if f.readline()]"
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sock"
            proc = subprocess.Popen(  # pylint: disable=consider-using-with
                ["python3", "-c", code, str(path)], stdout=subprocess.PIPE
            )
            assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
            assert vm.stop(proc, path, qmp_timeout_s=2, quit_wait_s=10) == "quit"
            assert proc.returncode == 0

    def test_the_watchdog_interrupts_a_blocking_read(self):
        ours, theirs = socket.socketpair()
        began = time.monotonic()
        try:
            with vm.watchdog(1):
                ours.recv(1)  # nothing ever comes
        except vm.BudgetExpired as err:
            assert "budget of 1 s" in str(err)
        else:
            raise AssertionError("no error")
        assert time.monotonic() - began < 3
        assert signal.alarm(0) == 0  # nothing left armed
        assert signal.getsignal(signal.SIGALRM) is not None
        ours.close()
        theirs.close()

    def test_the_watchdog_disarms_when_the_steps_end_in_time(self):
        previous = signal.getsignal(signal.SIGALRM)
        with vm.watchdog(30):
            pass
        assert signal.alarm(0) == 0
        assert signal.getsignal(signal.SIGALRM) is previous

    def test_a_step_cut_by_the_budget_stops_the_rest(self):
        ours, theirs = socket.socketpair()
        ran: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            run = bench_run.Run(mode="install", out=Path(td) / "out", work=Path(td) / "w")
            with vm.watchdog(1):

                def blocked() -> None:
                    ours.recv(1)  # nothing ever comes

                assert not run.step("blocked", blocked)
            assert run.out_of_time
            assert not run.step("next", lambda: ran.append("next"))
        assert not ran
        assert [s["status"] for s in run.steps] == [1, 1]
        assert str(run.steps[0]["detail"]).startswith("BudgetExpired: ")
        assert run.steps[1]["detail"] == "not run: the steps' budget ran out"
        ours.close()
        theirs.close()

    def test_the_guest_leak_note(self):
        with tempfile.TemporaryDirectory() as td:
            run = bench_run.Run(mode="install", out=Path(td) / "out", work=Path(td) / "w")
            assert bench_run.guest_leak_note(run).endswith("the guest never came up over SSH")
            run.guest_seen = True
            assert bench_run.guest_leak_note(run).endswith("not run, the guest was up")
            # Cut by the budget: the step is recorded, and its reason is what the log says.
            run.out_of_time = True
            run.step("guest-leak-check", lambda: None)
            note = bench_run.guest_leak_note(run)
            assert note.endswith("guest: not run: the steps' budget ran out"), note
            run.steps.clear()
            run.out_of_time = False
            run.step("guest-leak-check", lambda: None)
            assert bench_run.guest_leak_note(run).endswith("guest: no hit")

    def test_the_budget_fits_the_job(self):
        wf = (ROOT / ".github" / "workflows" / "bench.yml").read_text(encoding="utf-8")
        found = re.search(r"timeout-minutes: (\d+)", wf)
        assert found is not None
        minutes = int(found.group(1))
        assert vm.JOB_TIMEOUT_S == minutes * 60
        assert vm.STEPS_BUDGET_S + vm.PREPARE_RESERVE_S + vm.FINISH_RESERVE_S == vm.JOB_TIMEOUT_S
        # The longest path through the steps' own timeouts still fits.
        waits = (
            bench_run.NEGATIVE_TIMEOUT_S
            + bench_run.INSTALL_TIMEOUT_S
            + bench_run.UNLOCK_TIMEOUT_S
            + bench_run.LOGIN_TIMEOUT_S
            + bench_run.SSH_TIMEOUT_S
        )
        assert waits < vm.STEPS_BUDGET_S, (waits, vm.STEPS_BUDGET_S)

    def test_an_install_whose_monitor_hangs_still_ends(self):  # pylint: disable=too-many-locals
        # install() as run 45147805605 met it: QEMU running, its monitor mute.
        # The bench's deadline must fire and leave no QEMU behind.
        names = (
            "INSTALL_TIMEOUT_S",
            "QMP_IO_TIMEOUT_S",
            "STOP_QUIT_WAIT_S",
            "STOP_TERM_WAIT_S",
            "start",
            "machine",
        )
        saved = {n: getattr(bench_run, n) for n in names}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "work" / "install.qmp.sock"
            path.parent.mkdir()
            _, done = _mute_qmp(path)
            procs: list[subprocess.Popen[bytes]] = []

            def start(_run, _name, _argv):
                procs.append(_child("import time; print('ready', flush=True); time.sleep(60)"))
                return procs[-1]

            def machine(_run, _name, _vars):
                return qemu.Machine(
                    firmware=firmware.Firmware(Path("/c"), Path("/v"), Path("/d")),
                    vars_file=Path("/v"),
                    serial_socket=root / "s.sock",
                    serial_log=root / "serial.log",
                    qmp_socket=path,
                )

            run = _NoQemuImg(mode="install", out=root / "out", work=root / "work")
            run.remastered = Path("/iso")
            fakes = (1, 0.3, 0.2, 1.0, start, machine)
            for name, fake in zip(names, fakes, strict=True):
                setattr(bench_run, name, fake)
            began = time.monotonic()
            try:
                ok = run.step(
                    "install",
                    lambda: bench_run.install(run, root / "seed", root / "d", root / "v"),
                )
            finally:
                for name, value in saved.items():
                    setattr(bench_run, name, value)
                done.set()
            assert not ok and time.monotonic() - began < 20
            assert "did not end within 1 s" in str(run.steps[-1]["detail"])
            assert procs[0].poll() == -15  # SIGTERM: the mute monitor never took quit
            note = (root / "out" / "install-timeout.txt").read_text(encoding="utf-8")
            assert note.startswith("VM unknown (no QMP)")


class _Line:
    """
    The guest's side of a serial line, in a fixed order: each read returns the
    next chunk, then nothing (None) once they are used up, or b"" (closed)
    when asked to. The bench's sends go to a socket pair, read back after.
    """

    def __init__(self, chunks: list[bytes], closes: bool = False) -> None:
        self.chunks = list(chunks)
        self.closes = closes
        self.ours, self.guest = socket.socketpair()
        self.reads_left_at_send: list[int] = []

    def read(self, wait_s: float) -> bytes | None:
        """The next chunk at once; with none left, a quiet line for a moment."""
        if self.chunks:
            self.reads_left_at_send.append(len(self.chunks))
            return self.chunks.pop(0)
        if self.closes:
            return b""
        time.sleep(min(wait_s, 0.02))  # a quiet line: time passes, nothing comes
        return None

    def console(self, transcript: io.BytesIO | None = None) -> console.Console:
        """A console reading this line."""
        return console.Console(self.ours, transcript or io.BytesIO(), reader=self.read)

    def sent(self) -> bytes:
        """What the bench typed into the line."""
        self.ours.close()
        return self.guest.recv(1000)


class TestBenchConsole:  # pylint: disable=missing-function-docstring
    """The passphrase driver against recorded console text."""

    def test_a_split_prompt_is_answered_once_then_login(self):
        boot = b"\x1b[2J\r\nBegin: Mounting\r\nPlease unl"
        line = _Line([boot, b"ock disk keystore-rpool: ", b"\r\nbench login: "])
        transcript = io.BytesIO()
        con = line.console(transcript)
        assert console.unlock(con, "s3cret", 5, 5, settle_s=0.1) == "askpass"
        assert line.sent() == b"s3cret\r"
        assert b"s3cret" not in transcript.getvalue()
        assert b"Please unl" in transcript.getvalue()

    def test_a_second_prompt_fails(self):
        prompt = b"Please unlock disk keystore-rpool: "
        line = _Line([prompt, b"\r\nNo key available\r\n" + prompt])
        try:
            console.unlock(line.console(), "x", 5, 5, settle_s=0.1)
        except console.ConsoleError as err:
            assert "refused" in str(err)
        else:
            raise AssertionError("no error")
        assert line.sent() == b"x\r"

    def test_no_prompt_times_out_and_a_closed_console_fails(self):
        con = _Line([b"nothing"]).console()
        try:
            con.expect(console.UNLOCK_PROMPTS, 0.2)
        except console.ConsoleError as err:
            assert "within" in str(err)
        else:
            raise AssertionError("no error")
        closed = _Line([b"nothing"], closes=True).console()
        try:
            closed.expect(console.UNLOCK_PROMPTS, 5)
        except console.ConsoleError as err:
            assert "closed" in str(err)
        else:
            raise AssertionError("no error")

    def test_the_earliest_prompt_wins_and_escapes_are_ignored(self):
        text = b"Enter passphrase for /dev/zd0-keystore: \x1b[0m and later bench login: "
        con = _Line([text]).console()
        assert con.expect((console.LOGIN, *console.UNLOCK_PROMPTS), 5).name == "cryptsetup"
        assert con.expect((console.LOGIN,), 5) is console.LOGIN
        assert console.clean(b"a\x1b[01;01Hb\r\n") == b"ab\n"

    def test_the_26_04_dracut_prompt_then_login(self):
        # Bytes as run 45120463779 recorded them: systemd-cryptsetup's prompt
        # under 26.04's dracut, then agetty's login among systemd's OSC 3008
        # markers, a DCS query and a soft reset.
        prompt = (
            b"         Starting \x1b[0;1;39mdracut-pre-mount.service\x1b[0m - dracut pre-mount"
            b" hook...\r\r\n\x1b[0;1;39mPlease enter passphrase for disk keystore-rpool: (pr"
        )
        login = (
            b"invocationid=48a1ca7490e4416e927d7ecd7b2de558;type=service\x1b\\\x1bP+q6E616D6"
            b"5\x1b\\\r\r\nUbuntu 26.04.1 LTS bench ttyS0\r\n\r\nbench login: \x1b[!p\x1b]104"
            b"\x1b\\\x1b[0m\x1b[?7h\x1b[1G\x1b[0J\x1b[6n"
        )
        line = _Line([prompt, b"ess TAB for no echo) \x1b[0m", b"************\r\n", login])
        assert console.unlock(line.console(), "s3cret", 5, 5, settle_s=0.1) == "systemd"
        assert line.sent() == b"s3cret\r"
        again = b"Please enter passphrase for disk keystore-rpool: (press TAB for no echo) "
        refused = _Line([again, b"***\r\n" + again])
        try:
            console.unlock(refused.console(), "x", 5, 5, settle_s=0.1)
        except console.ConsoleError as err:
            assert "asked again (systemd)" in str(err)
        else:
            raise AssertionError("no error")

    def test_the_passphrase_waits_for_the_whole_prompt_and_a_quiet_line(self):
        # Run 45215706030: the passphrase went out on "keystore-rpool: ", before
        # systemd had printed "(press TAB for no echo) " and turned echo off,
        # and the tty echoed it in clear. Now nothing is sent until the whole
        # prompt is in and the line has been quiet for the settle time.
        parts = [
            b"\x1b[0;1;39mPlease enter passphrase for disk keystore-rpool: ",
            b"\x1b[0;90m(press TAB ",
            b"for no echo) ",
            b"\x1b[0m",
        ]
        line = _Line([*parts, b"bench login: "])
        con = line.console()
        sent_at: list[float] = []
        send = con.send

        def timed_send(data: bytes) -> None:
            sent_at.append(time.monotonic())
            line.reads_left_at_send.append(-1)  # marks the send among the reads
            send(data)

        con.send = timed_send  # type: ignore[method-assign]
        began = time.monotonic()
        assert console.unlock(con, "s3cret", 5, 5, settle_s=0.3) == "systemd"
        # Every part of the prompt, its closing escape too, was read before the send.
        marks = line.reads_left_at_send
        assert marks.index(-1) >= len(parts), marks
        assert sent_at[0] - began >= 0.3
        assert line.sent() == b"s3cret\r"

    def test_the_systemd_prompt_alone_is_not_enough(self):
        # Without its "(press TAB for no echo) " the prompt is not taken as
        # asked: echo may still be on.
        con = _Line([b"Please enter passphrase for disk keystore-rpool: "]).console()
        try:
            con.expect(console.UNLOCK_PROMPTS, 0.2)
        except console.ConsoleError as err:
            assert "within" in str(err)
        else:
            raise AssertionError("no error")
        assert console.UNLOCK_SETTLE_S >= 1.0

    def test_settle_resets_on_new_bytes_and_has_a_limit(self):
        line = _Line([b"a", b"b", b"c"])
        con = line.console()
        con.settle(0.1, 5)
        assert not line.chunks  # every chunk was read before quiet
        busy = console.Console(socket.socketpair()[0], io.BytesIO(), reader=lambda _w: b"x")
        try:
            busy.settle(0.1, 0.3)
        except console.ConsoleError as err:
            assert "did not settle within" in str(err)
        else:
            raise AssertionError("no error")
        closed = _Line([], closes=True).console()
        try:
            closed.settle(0.1, 5)
        except console.ConsoleError as err:
            assert "closed" in str(err)
        else:
            raise AssertionError("no error")

    def test_clean_strips_strings_and_full_csi(self):
        raw = b"a\x1b]3008;start=1;type=service\x1b\\b\x1bP+q6E\x1b\\c\x1b[!pd\x1b]104\x07e\x1b[6n"
        assert console.clean(raw) == b"abcde"

    def test_the_socket_reader(self):
        # Everything is in the socket before the reads: nothing to wait for.
        ours, guest = socket.socketpair()
        guest.sendall(b"x\r\nbench login: ")
        con = console.Console(ours, io.BytesIO())
        assert con.expect((console.LOGIN,), 5) is console.LOGIN
        guest.close()
        try:
            con.expect((console.LOGIN,), 5)
        except console.ConsoleError as err:
            assert "closed" in str(err)
        else:
            raise AssertionError("no error")
        con.close()

    def test_connect_to_a_socket_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ser.sock"
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            srv.listen(1)
            con = console.Console.connect(path, io.BytesIO(), timeout_s=5)
            peer, _ = srv.accept()
            con.send(b"hi")
            assert peer.recv(10) == b"hi"
            con.close()
            peer.close()
            srv.close()
        try:
            console.Console.connect(Path("/nonexistent/s"), io.BytesIO(), timeout_s=0.1)
        except OSError:
            return
        raise AssertionError("no error")


def _outputs() -> dict[str, str]:
    return {
        name: (FIXTURES / "guest" / f"{name}.txt").read_text(encoding="utf-8")
        for name in layout.COMMANDS
    }


class TestBenchLayout:  # pylint: disable=missing-function-docstring
    """The installer's layout read from the guest's outputs."""

    def test_an_installer_layout_passes(self):
        checks = layout.check(_outputs(), "bench")
        assert all_ok(checks), summary(checks)
        assert len(checks) == 12

    def _fails(self, name: str, **changes: str) -> None:
        outputs = _outputs()
        for key, (old, new) in ((k, v.split("=>")) for k, v in changes.items()):
            assert old in outputs[key], (key, old)
            outputs[key] = outputs[key].replace(old, new)
        failed = [c.name for c in layout.check(outputs, "bench") if not c.ok]
        assert failed == [name], failed

    def test_each_check_fails_alone(self):
        self._fails("pools-online", **{"zpool-list": "bpool\tONLINE=>bpool\tDEGRADED"})
        self._fails("esp", findmnt='"vfat"=>"ext4"')
        self._fails("userdata /root", findmnt='"rpool/USERDATA/root_def456"=>"rpool/ROOT/x"')
        self._fails("keystore-mapping", cryptsetup="is active=>is inactive")
        self._fails("swap-encrypted", crypttab="cryptoswap=>otherswap")
        self._fails(
            "keystore-zvol", **{"zfs-list": "rpool/keystore\tvolume=>rpool/keystore\tfilesystem"}
        )
        self._fails(
            "rpool-key-from-keystore",
            **{"zfs-list": "file:///run/keystore/rpool/system.key=>prompt"},
        )
        self._fails(
            "rpool-encrypted",
            **{
                "zfs-list": "rpool/var\tfilesystem\t/var\trpool\tavailable=>"
                "rpool/var\tfilesystem\t/var\t-\tunavailable"
            },
        )

    def test_a_boot_environment_mismatch_fails(self):
        outputs = _outputs()
        outputs["findmnt"] = outputs["findmnt"].replace(
            "bpool/BOOT/ubuntu_abc123", "bpool/BOOT/ubuntu_zzz999"
        )
        failed = [c.name for c in layout.check(outputs, "bench") if not c.ok]
        assert failed == ["boot-environment-pair"], failed

    def test_the_commands_only_read(self):
        for command in layout.COMMANDS.values():
            assert command.split()[0] in {"zpool", "zfs", "findmnt", "lsblk", "cryptsetup", "cat"}
            assert not re.search(r"\b(set|create|destroy|import|export|open|close)\b", command)


class TestBenchSecureBoot:  # pylint: disable=missing-function-docstring
    """Every reading must hold; each missing one fails on its own."""

    def test_guest_readings(self):
        r = sbproof.Reading
        good = sbproof.guest_checks(r(0, b"SecureBoot enabled\n"), r(0, b"\x06\x00\x00\x00\x01"))
        assert all_ok(good)
        bad = sbproof.guest_checks(r(0, b"SecureBoot disabled\n"), r(0, b"\x06\x00\x00\x00\x00"))
        assert [c.ok for c in bad] == [False, False]
        assert "unreadable" in sbproof.guest_checks(r(0, b""), r(0, b""))[1].detail

    def test_a_failed_read_says_why(self):
        # Run 45134324398: mokutil read the variable, cat found no such file.
        r = sbproof.Reading
        missing = r(1, b"", "cat: /sys/firmware/efi/efivars/SecureBoot-8be4: No such file\n")
        checks = sbproof.guest_checks(r(0, b"SecureBoot enabled\n"), missing)
        assert [c.ok for c in checks] == [True, False]
        assert checks[1].detail.startswith("exit 1: cat: ") and "No such file" in checks[1].detail
        # A good-looking output does not count when the command failed.
        assert not sbproof.guest_checks(r(2, b"SecureBoot enabled\n", "x"), missing)[0].ok
        assert sbproof.Reading(0, b"").failure() == ""
        assert set(sbproof.EFIVAR_CONTEXT) == {"efivarfs-mount", "efivars-secureboot"}

    def test_kernel_and_negative_lines(self):
        assert sbproof.kernel_check("[0.0] secureboot: Secure boot enabled\n").ok
        assert not sbproof.kernel_check("secureboot: Secure boot disabled").ok
        log = 'BdsDxe: failed to load Boot0001 "UEFI Misc Device" from x: Access Denied\n'
        assert sbproof.negative_check(log).ok
        assert not sbproof.negative_check("BdsDxe: starting Boot0001").ok

    def test_host_argv(self):
        m = _machine(Path("/w"))
        fw = m.firmware
        argv = qemu.installed_argv(m, Path("/w/t"), 1)
        good = sbproof.host_check(argv, fw, m.vars_file)
        assert good.ok, good.detail
        assert good.detail.startswith(f"code {fw.code} read-only; vars {m.vars_file}, a copy of ")
        # _machine's code path has a comma: matched as QEMU's option writes it
        # (doubled). Another firmware's code is not this one.
        assert ",," in " ".join(argv)
        other = firmware.Firmware(Path("/w/code.fd"), fw.vars_template, fw.descriptor)
        assert sbproof.host_check(argv, other, m.vars_file).detail == "missing: code"
        no_smm = [a.replace("smm=on", "smm=off") for a in argv]
        assert sbproof.host_check(no_smm, fw, m.vars_file).detail == "missing: smm"
        # The template itself as the machine's NVRAM is not a copy.
        m3 = qemu.Machine(fw, fw.vars_template, m.serial_socket, m.serial_log, m.qmp_socket)
        argv3 = qemu.installed_argv(m3, Path("/w/t"), 1)
        assert sbproof.host_check(argv3, fw, fw.vars_template).detail == "missing: vars copy"

    def test_the_efivar_is_the_global_variable(self):
        # Run 45182632398: efivarfs listed SecureBoot-8be4df61-93ca-11d2-...;
        # the bench asked for ...-11d0-... and found no such file.
        assert sbproof.EFIVAR.endswith("/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c")


class TestBenchFinish:  # pylint: disable=missing-function-docstring
    """
    finish() decides what may be uploaded: the marker is written only when
    the host's search is clean and, once the guest was up, its own search
    passed. Run in a temporary directory, with no VM.
    """

    SECRET = "s3cret-passphrase-for-the-test"

    def _run(self, td: str) -> bench_run.Run:
        root = Path(td)
        run = bench_run.Run(mode="install", out=root / "out", work=root / "work")
        run.out.mkdir()
        run.work.mkdir()
        run.secrets = [self.SECRET]
        run.steps.append({"step": "install", "status": 0, "seconds": 1.0, "detail": ""})
        return run

    def _disk(self, td: str, data: bytes = b"\0" * 4096) -> Path:
        disk = Path(td) / "target.qcow2"
        disk.write_bytes(data)
        return disk

    def _guest(self, run: bench_run.Run, status: int, detail: str = "") -> None:
        run.guest_seen = True
        run.steps.append(
            {"step": "guest-leak-check", "status": status, "seconds": 1.0, "detail": detail}
        )

    def _marker(self, run: bench_run.Run) -> bool:
        return (run.out / "LEAK-CHECK-PASSED").exists()

    def test_clean_with_the_guest_search_passed_writes_the_marker(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            self._guest(run, 0)
            assert bench_run.finish(run, self._disk(td)) == 0
            assert self._marker(run)
            text = (run.out / "LEAK-CHECK-PASSED").read_text(encoding="utf-8")
            assert "the guest's own search" in text

    def test_clean_with_the_guest_never_up_writes_the_marker(self):
        # Decision 3a: nothing of the guest's encrypted rpool reached out/.
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            bench_run.finish(run, self._disk(td))
            assert self._marker(run)
            assert "never came up" in (run.out / "LEAK-CHECK-PASSED").read_text(encoding="utf-8")

    def test_a_hit_in_out_holds_the_upload(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            self._guest(run, 0)
            (run.out / "serial.log").write_text(f"x {self.SECRET} y", encoding="utf-8")
            assert bench_run.finish(run, self._disk(td)) == 1
            assert not self._marker(run)

    def test_a_hit_in_the_disk_image_holds_the_upload(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            self._guest(run, 0)
            disk = self._disk(td, b"\0" * 100 + self.SECRET.encode() + b"\0" * 100)
            assert bench_run.finish(run, disk) == 1
            assert not self._marker(run)

    def test_a_host_search_that_cannot_run_holds_the_upload(self):
        # The disk image is a directory: reading it raises an OSError.
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            self._guest(run, 0)
            disk = Path(td) / "target.qcow2"
            disk.mkdir()
            assert bench_run.finish(run, disk) == 1
            assert not self._marker(run)

    def test_a_guest_search_that_failed_or_found_a_hit_holds_the_upload(self):
        for detail in ("guest: grep exited 2", "a secret in /var/log/x"):
            with tempfile.TemporaryDirectory() as td:
                run = self._run(td)
                self._guest(run, 1, detail)
                assert bench_run.finish(run, self._disk(td)) == 1
                assert not self._marker(run), detail

    def test_a_guest_search_cut_by_the_budget_holds_the_upload(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            run.guest_seen = True
            run.out_of_time = True
            run.step("guest-leak-check", lambda: None)  # recorded as not run
            assert bench_run.finish(run, self._disk(td)) == 1
            assert not self._marker(run)

    def test_a_guest_up_with_no_search_recorded_holds_the_upload(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            run.guest_seen = True
            assert bench_run.finish(run, self._disk(td)) == 1
            assert not self._marker(run)

    @staticmethod
    def _key_shaped(salt: int) -> str:
        # The shape of ssh-keygen's file: a header, base64 broken at 70
        # columns, a shorter last line, a footer.
        body = "".join(chr(65 + (salt * 7 + i * 13) % 26) for i in range(312))
        lines = [body[i : i + 70] for i in range(0, len(body), 70)]
        return "\n".join(
            ["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----", ""]
        )

    def _with_key(self, run: bench_run.Run, text: str) -> Path:
        keydir = run.path("ssh")
        keydir.mkdir()
        run.key = keydir / "id_ed25519"
        run.key.write_text(text, encoding="utf-8")
        return run.key

    def test_a_copy_of_the_key_file_in_out_holds_the_upload(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            self._guest(run, 0)
            key = self._with_key(run, self._key_shaped(1))
            shutil.copy(key, run.out / "copied-key")
            assert bench_run.finish(run, self._disk(td)) == 1
            assert not self._marker(run)

    def test_each_body_line_and_the_joined_body_are_needles(self):
        text = self._key_shaped(2)
        body = text.splitlines()[1:-1]
        assert [len(ln) for ln in body] == [70, 70, 70, 70, 32]
        needles = leak.key_needles(text)
        assert needles == [ln.encode() for ln in body] + ["".join(body).encode()]
        # One line of the file is enough; so is the body with its newlines gone.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "one-line").write_text(f"x {body[2]} y", encoding="utf-8")
            (root / "joined").write_text("".join(body), encoding="utf-8")
            hits = {p.name for p, _ in leak.scan_tree(root, needles)}
            assert hits == {"one-line", "joined"}
        # A short line names nothing; the header and footer are not needles.
        short = "-----BEGIN X-----\n" + "A" * 40 + "\nAw==\n-----END X-----\n"
        assert leak.key_needles(short) == [b"A" * 40, b"A" * 40 + b"Aw=="]
        assert not leak.key_needles("-----BEGIN X-----\n-----END X-----\n")

    def test_a_real_key_is_found_in_out_and_its_public_half_is_not(self):
        if shutil.which("ssh-keygen") is None:
            return  # the shape test above still runs
        with tempfile.TemporaryDirectory() as td:
            key = Path(td) / "k"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(key)],
                check=True,
                stdin=subprocess.DEVNULL,
            )
            needles = leak.key_needles(key.read_text(encoding="utf-8"))
            assert len(needles) >= 2
            assert leak.scan_file(key, needles)
            assert not leak.scan_file(key.with_suffix(".pub"), needles)

    def test_finish_runs_when_the_power_off_raises(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(td)
            run.path("seed").mkdir()
            (run.out / "serial.log").write_text(f"x {self.SECRET} y", encoding="utf-8")

            def steps(_run, _disk, booted):
                booted.append((None, None))

            with (
                mock.patch.object(bench_run, "_install_steps", steps),
                mock.patch.object(bench_run, "power_off", side_effect=OSError("disk full")),
                mock.patch.object(bench_run, "finish", wraps=bench_run.finish) as fin,
            ):
                try:
                    bench_run.install_run(run)
                except OSError:
                    pass
                else:
                    raise AssertionError("the power-off's error was swallowed")
            assert fin.call_count == 1
            assert not run.path("seed").exists()
            assert not self._marker(run)

    def test_a_marker_from_an_earlier_run_is_removed_at_the_start(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            out.mkdir()
            (out / "LEAK-CHECK-PASSED").write_text("old\n", encoding="utf-8")
            seen: list[bool] = []

            def probe(run):
                seen.append((run.out / "LEAK-CHECK-PASSED").exists())
                return 1

            with mock.patch.object(bench_run, "probe", probe):
                status = bench_run.main(["probe", "--out", str(out), "--work", str(Path(td) / "w")])
            assert status == 1 and seen == [False]
            assert leak.MARKER == "LEAK-CHECK-PASSED"
            assert not leak.drop_stale_marker(out)

    def test_the_verdict(self):
        v = leak.upload_verdict
        assert v(True, True, 0)[0] and v(True, False, None)[0]
        assert not v(False, False, None)[0] and not v(False, True, 0)[0]
        assert v(True, True, None) == (False, "the guest was up but its own search did not run")
        assert v(True, True, 1) == (False, "the guest's own search found a secret or failed")
        # A guest never up: its step's status does not matter (decision 3a).
        assert v(True, False, 1)[0]


class TestBenchLeak:  # pylint: disable=missing-function-docstring
    """The leak check finds a secret anywhere, never prints it."""

    def test_a_match_across_two_reads(self):
        block = 1 << 20
        data = b"x" * (block - 3) + b"SECRET" + b"y" * 10
        assert leak.found_in_stream(io.BytesIO(data), [b"SECRET", b"other", b""]) == {0}
        assert leak.found_in_stream(io.BytesIO(b"abc"), []) == set()

    def test_a_tree_and_a_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a" / "b").mkdir(parents=True)
            (root / "a" / "b" / "log.txt").write_bytes(b"line pass-1 here")
            (root / "clean.txt").write_bytes(b"nothing")
            (root / "link").symlink_to(root / "a" / "b" / "log.txt")
            hits = leak.scan_tree(root, leak.needles_of(["pass-1", "", "zzz"]))
            assert hits == [(root / "a" / "b" / "log.txt", 0)]
            assert leak.scan_file(root / "clean.txt", [b"pass-1"]) == set()

    def test_the_guest_search_reads_its_patterns_from_stdin(self):
        argv = leak.guest_grep_argv()
        assert argv[argv.index("-f") + 1] == "-"
        for path in ("/var/log", "/etc", "/var", "/root", "/home", "/boot", "/tmp"):
            assert path in argv


class TestBenchInspect:  # pylint: disable=missing-function-docstring
    """Reading the live ISO's layers without booting it."""

    def test_the_layer_chain(self):
        layers = [
            "minimal.squashfs",
            "minimal.standard.squashfs",
            "minimal.standard.live.squashfs",
            "minimal.en.squashfs",
        ]
        args = remaster.first_linux_args(GRUB_CFG)
        chain = inspect_iso.layer_chain(inspect_iso.layerfs_path(args), layers)
        assert chain == layers[:3]
        assert inspect_iso.default_chain(layers) == layers[:3]
        assert inspect_iso.default_chain(["a.squashfs", "a.b.squashfs"]) == [
            "a.squashfs",
            "a.b.squashfs",
        ]
        assert inspect_iso.default_chain([]) == []
        assert inspect_iso.layerfs_path(["quiet"]) == ""

    def test_listing_status_and_datasources(self):
        listing = "Parallel unsquashfs\nsquashfs-root\nsquashfs-root/etc/cloud/cloud.cfg\n"
        assert inspect_iso.listed_paths(listing) == ["etc/cloud/cloud.cfg"]
        status = (
            "Package: openssh-server\nStatus: install ok installed\nVersion: 1:9.9p1-1\n\n"
            "Package: cloud-init\nStatus: deinstall ok config-files\nVersion: 25.1\n"
            "Description: x\n multi-line\n"
        )
        assert inspect_iso.package_installed(status, "openssh-server") == "1:9.9p1-1"
        assert inspect_iso.package_installed(status, "cloud-init") == ""
        cfg = "datasource_list: [ NoCloud, None ]\nother: 1\n"
        assert inspect_iso.datasource_lines(cfg) == ["datasource_list: [ NoCloud, None ]"]

    def test_what_the_probe_reads(self):
        found = "'/casper/minimal.squashfs'\n'/casper/minimal.standard.squashfs'\n/casper/x.gpg\n"
        assert inspect_iso.squashfs_names(found) == [
            "minimal.squashfs",
            "minimal.standard.squashfs",
        ]
        snap = "var/lib/snapd/seed/snaps/ubuntu-desktop-bootstrap_412.snap"
        for path in (snap, "var/lib/dpkg/status", "etc/cloud/cloud.cfg.d/90.cfg"):
            assert inspect_iso.wanted_from_layer(path), path
        assert not inspect_iso.wanted_from_layer("var/lib/snapd/seed/snaps/firefox_1.snap")
        assert inspect_iso.wanted_from_snap("meta/snap.yaml")
        assert not inspect_iso.wanted_from_snap("snap/snap.yaml")
        assert inspect_iso.wanted_from_snap(
            "bin/subiquity/subiquity/server/controllers/filesystem.py"
        )
        assert not inspect_iso.wanted_from_snap("snap/meta")

    def test_snap_version(self):
        meta = "name: ubuntu-desktop-bootstrap\nversion: '0+git.4d1f2a3'\nsummary: x\n"
        assert inspect_iso.snap_version(meta) == "0+git.4d1f2a3"
        assert inspect_iso.snap_version('version: "26.04.1"\n') == "26.04.1"
        assert inspect_iso.snap_version("name: x\n  version: nested\n") == ""

    def test_unsquashfs_writes_no_xattrs(self):
        # The live's layers carry trusted.overlay.* attributes that only root
        # may write: without -no-xattrs, unsquashfs exits 2 as the runner user.
        cmd = inspect_iso.unsquashfs_extract_cmd(Path("/w/l.squashfs"), "etc/x", Path("/w/out"))
        assert cmd[0] == "unsquashfs" and "-no-xattrs" in cmd
        assert cmd[-3:] == ["/w/out", "/w/l.squashfs", "etc/x"]
        assert inspect_iso.unsquashfs_list_cmd(Path("/w/l.squashfs")) == [
            "unsquashfs",
            "-l",
            "-no-progress",
            "/w/l.squashfs",
        ]

    def test_the_blob_id_is_git_s(self):
        assert inspect_iso.git_blob_id(b"x") == "c1b0730e0133447badcfd47fd144e254807b06e1"


class TestBenchReport:  # pylint: disable=missing-function-docstring
    """Verdicts written failures first."""

    def test_write_and_summary(self):
        checks = [Check("b", True, "fine"), Check("a", False, "broken")]
        assert not all_ok(checks) and not all_ok([])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "c.json"
            write_checks(path, checks)
            doc = json.loads(path.read_text())
        assert [c["name"] for c in doc["checks"]] == ["a", "b"]
        assert summary(checks).splitlines()[1] == "FAIL a: broken"

    def test_one_check_alone(self):
        assert all_ok([Check("x", True)]) and summary([Check("x", True)]) == "OK   x: "


class TestBenchSuites:  # pylint: disable=missing-function-docstring,too-few-public-methods
    """
    make fulltest, CI and tox -e cov run only the suites tests/test_unit.py
    names: a Test class here that is not named there never runs.
    """

    def test_every_class_here_is_registered(self):
        here = set(
            re.findall(r"^class (Test\w+)", Path(__file__).read_text(encoding="utf-8"), re.M)
        )
        unit = (ROOT / "tests" / "test_unit.py").read_text(encoding="utf-8")
        for start in ("from tests.test_bench import (", "\nBENCH_SUITES = ("):
            block = unit[unit.index(start) :]
            named = set(re.findall(r"^    (Test\w+),$", block[: block.index("\n)\n")], re.M))
            assert here, "no Test class found"
            assert here - named == set(), (start, sorted(here - named))


class TestBenchWorkflow:  # pylint: disable=missing-function-docstring
    """The bench's workflow: by hand and weekly, read-only, uploads only after the check."""

    _WF = ROOT / ".github" / "workflows" / "bench.yml"

    def test_triggers_permissions_and_hosts(self):
        text = self._WF.read_text(encoding="utf-8")
        on = text[text.index("\non:\n") : text.index("\npermissions:")]
        assert "workflow_dispatch:" in on and "schedule:" in on
        assert not re.search(r"^  (push|pull_request\w*):", on, re.M)
        assert "\npermissions:\n  contents: read\n\n" in text
        assert "secrets." not in text
        assert not re.search(r"-latest\b", text)
        hosts = set(re.findall(r"ubuntu-\d\d\.\d\d", text))
        assert hosts == {"ubuntu-24.04", "ubuntu-26.04"}, hosts
        assert '- cron: "17 3 * * 1"' in text

    def test_the_upload_waits_for_the_leak_check(self):
        text = self._WF.read_text(encoding="utf-8")
        upload = text[text.index("- name: Upload") :]
        assert "if: always() && hashFiles('out/LEAK-CHECK-PASSED') != ''" in upload
        assert "uses: actions/upload-artifact@v6" in upload
        assert "retention-days: 14" in upload
        # Free text from the dispatch reaches the run through the environment only.
        assert "${{ inputs.iso }}" not in text.split("run: python3 -m bench", 1)[1].split("\n")[0]

    def test_the_preparation_script_is_executable(self):
        assert (ROOT / "bench" / "prepare-runner.sh").stat().st_mode & 0o111

    def test_the_schedules_fallbacks_and_the_forks(self):
        # S6: on schedule `inputs` is empty, so every use of an input has its fallback.
        text = self._WF.read_text(encoding="utf-8")
        assert "github.event_name == 'schedule' || inputs.host == 'both'" in text
        assert text.count("${{ inputs.mode || 'full' }}") == 3, "prepare, run and artifact name"
        assert "inputs.mode || 'install'" not in text
        assert (
            'python3 -m bench "$BENCH_MODE" --out out --work "$BENCH_WORK" --iso "$BENCH_ISO"'
            in (text)
        )
        assert "BENCH_ISO: ${{ inputs.iso }}" in text
        # N8: a fork that enables Actions does not inherit the weekly run.
        job = text[text.index("\njobs:") :]
        assert (
            "if: github.repository == 'juanmitaboada/zark' || github.event_name != 'schedule'"
            in job.split("strategy:", 1)[0]
        )

    def test_mode_full_is_offered_and_prepared(self):
        text = self._WF.read_text(encoding="utf-8")
        assert "options: [install, full, probe]" in text and "default: install" in text
        prep = text[text.index("- name: Prepare the runner") : text.index("- name: Run the bench")]
        assert "BENCH_MODE: ${{ inputs.mode || 'full' }}" in prep
        script = (ROOT / "bench" / "prepare-runner.sh").read_text(encoding="utf-8")
        assert 'if [ "${BENCH_MODE:-}" = full ]; then' in script
        for tool in ("devscripts", "debhelper", "dh-python", "pandoc"):
            assert tool in script


def _progress_doc(waiting_for: str | None, question: dict[str, object] | None = None) -> str:
    return json.dumps(
        {
            "kind": "zark-progress",
            "waiting_for_input": waiting_for is not None,
            "waiting_for": waiting_for,
            "question": question,
        }
    )


def _q(kind: str, **fields: object) -> dict[str, object]:
    return {
        "kind": kind,
        "default": fields.get("default"),
        "options": fields.get("options"),
        "word": fields.get("word"),
        "timeout_s": None,
        "destructive": False,
    }


class _Pty:
    """A scripted pty for drive.Session: each read takes the next event."""

    def __init__(self, events: list[bytes | None]):
        self.events = list(events)
        self.t = 100.0
        self.written: list[bytes] = []

    def clock(self) -> float:
        """The script's time."""
        return self.t

    def read(self, timeout_s: float) -> bytes | None:
        """The next event; a quiet read takes its whole timeout."""
        if not self.events:
            return b""
        event = self.events.pop(0)
        self.t += timeout_s if event is None else 0.01
        return event

    def write(self, data: bytes) -> None:
        """What the session typed."""
        self.written.append(data)


QUIET = [None] * 12  # 3 s of a quiet line at 0.25 s a read


class TestBenchDrive:  # pylint: disable=missing-function-docstring
    """zark's reads answered from the command's table, through progress.json and the pty."""

    def test_every_row_is_found_by_kind_and_text(self):
        table = drive.TABLES["recover"]
        assert (
            drive.find_row(table, "Passphrase for backup", "passphrase").answer == drive.PASSPHRASE
        )
        row = drive.find_row(table, "Type YES to erase /dev/vda", "word")
        assert row.answer == "YES"
        for kind, text in (("yes_no", "Passphrase for backup"), ("choice", "Type IUNDERSTAND")):
            try:
                drive.find_row(table, text, kind)
            except drive.Unexpected:
                continue
            raise AssertionError((kind, text))
        # Every table's rows have a kind zark writes, and a prompt to wait for.
        for rows in drive.TABLES.values():
            for r in rows:
                assert r.kind in drive.PROMPT_TAILS, r

    def test_the_restore_point_is_the_default_only_when_it_is_step_4s(self):
        point = "zark_2026-11-24_06:03:02Z"
        good = "2026-11-24 06:03:03 UTC  backup point  (40/40 datasets)"
        options = ["2026-11-24 05:50:00 UTC  backup point  (40/40 datasets)", good]
        q = _q("choice", default=1, options=options)
        expect = drive.Expect(point=point)
        row, text = drive.decide(
            drive.TABLES["recover"], "Restore point (newest is the default):", q, expect, []
        )
        assert row.answer == drive.POINT and text == drive.ENTER
        for bad in (
            "2026-11-24 06:03:03 UTC  backup point, INCOMPLETE  (39/40 datasets)",
            "2026-11-24 06:03:03 UTC    carried: hourly  (40/40 datasets)",
            "2026-11-24 07:03:03 UTC  backup point  (40/40 datasets)",
        ):
            q = _q("choice", default=1, options=[options[0], bad])
            try:
                drive.decide(drive.TABLES["recover"], "Restore point", q, expect, [])
            except drive.Unexpected:
                continue
            raise AssertionError(bad)
        assert not drive.point_option_ok(good, "not-a-point")

    def test_the_disk_is_chosen_by_its_serial_and_only_then_erased(self):
        table = drive.TABLES["recover"]
        expect = drive.Expect(disk_serial="ZB0Q3NEW0001")
        options = [
            "/dev/vda  40G    serial ZB0Q3NEW0001  ",
            "/dev/sdb  8G  Stick  serial ZB0Q3NEW00012  usb  ⚠ USB",
        ]
        q = _q("choice", default=0, options=options)
        disk_row, text = drive.decide(
            table, "Internal disk to ERASE and restore to:", q, expect, []
        )
        assert text == "1"
        word = _q("word", word="YES")
        try:
            drive.decide(table, "Type YES to erase /dev/vda", word, expect, [])
        except drive.Unexpected as err:
            assert "before the disk" in str(err)
        else:
            raise AssertionError("YES before the disk")
        assert (
            drive.decide(table, "Type YES to erase /dev/vda", word, expect, [disk_row])[1] == "YES"
        )
        for wrong in ([options[1]], [options[0], options[0]], []):
            try:
                drive.decide(
                    table, "Internal disk", _q("choice", default=0, options=wrong), expect, []
                )
            except drive.Unexpected:
                continue
            raise AssertionError(wrong)
        try:
            drive.decide(
                table, "Type YES to erase /dev/vda", _q("word", word="ERASE"), expect, [disk_row]
            )
        except drive.Unexpected as err:
            assert "ERASE" in str(err)
        else:
            raise AssertionError("another word")

    def test_a_row_is_answered_once_but_a_wait_may_come_back(self):
        table = drive.TABLES["backup"]
        row, text = drive.decide(
            table, "Passphrase for backup", _q("passphrase"), drive.Expect(), []
        )
        assert text == drive.PASSPHRASE
        try:
            drive.decide(table, "Passphrase for backup", _q("passphrase"), drive.Expect(), [row])
        except drive.Unexpected as err:
            assert "asked again" in str(err)
        else:
            raise AssertionError("asked twice")
        wait, _ = drive.decide(
            table, "Skip the read-back of backup now?", _q("wait"), drive.Expect(), []
        )
        again, _ = drive.decide(
            table, "Skip the read-back of backup now?", _q("wait"), drive.Expect(), [wait]
        )
        assert again is wait
        assert drive.missing(table, [row]) == ["Eject drive 'backup' now?"]

    def test_progress_json_is_read_only_while_zark_waits(self):
        assert drive.parse_progress(_progress_doc("Q?", _q("yes_no"))) == ("Q?", _q("yes_no"))
        assert drive.parse_progress(_progress_doc(None)) is None
        assert drive.parse_progress("") is None and drive.parse_progress("[1]") is None
        assert (
            drive.parse_progress(json.dumps({"waiting_for_input": True, "question": None})) is None
        )

    def test_a_session_answers_each_read_when_its_prompt_is_on_the_screen(self):
        table = (
            drive.Row("yes_no", "Go on?", "y", required=True),
            drive.Row("passphrase", "Passphrase for backup", drive.PASSPHRASE, required=True),
        )
        pty = _Pty(
            [
                b"\x1b[1;33m?\x1b[0m Go on?\r\n    [y/N]: ",
                *QUIET,
                b"y\r\n",
                b"\xf0\x9f\x94\x91 Passphrase for backup\r\n",
                b"    Passphrase: ",
                *QUIET,
                b"\r\n",
                b"BACKUP COMPLETED\r\n",
            ]
        )
        docs = [
            _progress_doc("Go on?", _q("yes_no", default=False)),
            _progress_doc("Passphrase for backup", _q("passphrase")),
        ]
        transcript = io.BytesIO()
        session = drive.Session(
            table,
            drive.Expect(),
            pty.read,
            pty.write,
            lambda: docs[len(pty.written)],
            "s3cret",
            transcript,
            clock=pty.clock,
        )
        out = session.run(600)
        assert pty.written == [b"y\n", b"s3cret\n"]
        assert [a["answer"] for a in out.asked] == ["y", drive.PASSPHRASE]
        assert b"s3cret" not in transcript.getvalue() and not out.unexpected
        check = drive.verdict("setup", 0, b"SETUP COMPLETE", drive.Outcome())
        assert check.ok, check

    def test_a_stale_progress_file_is_not_answered(self):
        # The screen shows the passphrase prompt; progress.json still shows the yes/no before it.
        table = (drive.Row("passphrase", "Passphrase for backup", drive.PASSPHRASE),)
        polls = [_progress_doc("Go on?", _q("yes_no"))] * 2 + [
            _progress_doc("Passphrase for backup", _q("passphrase"))
        ]
        pty = _Pty([b"    Passphrase: ", *QUIET, *QUIET])
        session = drive.Session(
            table,
            drive.Expect(),
            pty.read,
            pty.write,
            lambda: polls.pop(0) if len(polls) > 1 else polls[0],
            "pw",
            io.BytesIO(),
            clock=pty.clock,
        )
        session.run(600)
        assert pty.written == [b"pw\n"]

    def test_nothing_is_typed_before_the_line_is_quiet(self):
        pty = _Pty([b"    Passphrase: ", None, None, b"more\r\n", None, None])
        table = (drive.Row("passphrase", "Passphrase for backup", drive.PASSPHRASE),)
        session = drive.Session(
            table,
            drive.Expect(),
            pty.read,
            pty.write,
            lambda: _progress_doc("Passphrase for backup", _q("passphrase")),
            "pw",
            io.BytesIO(),
            clock=pty.clock,
        )
        session.run(600)
        assert not pty.written

    def test_an_unexpected_read_ends_the_command_with_eof(self):
        pty = _Pty([b"  Type IUNDERSTAND to continue anyway: ", *QUIET, b"\r\n", None, None])
        session = drive.Session(
            drive.TABLES["recover"],
            drive.Expect(),
            pty.read,
            pty.write,
            lambda: _progress_doc(
                "Type IUNDERSTAND to continue anyway:", _q("word", word="IUNDERSTAND")
            ),
            "pw",
            io.BytesIO(),
            clock=pty.clock,
        )
        out = session.run(600)
        assert pty.written == [b"\x04"] and "IUNDERSTAND" in out.unexpected
        assert out.asked[-1]["answer"] == "EOF"
        check = drive.verdict("recover", 1, b"", out)
        assert not check.ok and "unexpected read" in check.detail and "never asked" in check.detail

    def test_a_command_that_never_ends_is_cut(self):
        pty = _Pty([None] * 100)
        out = drive.Session(
            (), drive.Expect(), pty.read, pty.write, lambda: "", "pw", io.BytesIO(), clock=pty.clock
        ).run(5)
        assert out.timed_out
        assert "did not end in time" in drive.verdict("finish", 0, b"FINISH COMPLETE", out).detail

    def test_the_verdict_reads_the_banners(self):
        none = drive.Outcome()
        assert drive.verdict("finish", 0, b"\x1b[32m FINISH COMPLETE \x1b[0m", none).ok
        assert not drive.verdict("finish", 0, b"FINISH INCOMPLETE", none).ok
        bad = drive.verdict("recover", 0, b"RECOVERY COMPLETED WITH ERRORS", none)
        assert not bad.ok and "WITH ERRORS" in bad.detail
        assert "exit 2" in drive.verdict("finish", 2, b"FINISH COMPLETE", none).detail
        assert "no FINISH COMPLETE" in drive.verdict("finish", 0, b"", none).detail

    def test_the_pty_reader_and_writer(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        read = drive.pty_reader(r)
        assert read(0.01) is None
        os.write(w, b"abc")
        assert read(1.0) == b"abc"
        os.close(w)
        assert read(1.0) == b""
        os.close(r)
        with tempfile.TemporaryFile() as f:
            drive.pipe_writer(f)(b"xyz")
            f.seek(0)
            assert f.read() == b"xyz"
        assert drive.tail_line(b"a\r\nb\x1b[0m: ") == "b: "
        assert drive.on_prompt(b"x\n    Select [1-2, default=1]: ")
        assert not drive.on_prompt(b"copying 3 of 7\n")


class TestBenchZarkCheck:  # pylint: disable=missing-function-docstring
    """What steps 4 to 6 read back, as verdicts."""

    def test_the_backup_drive_is_found_by_its_serial(self):
        names = [
            "usb-QEMU_QEMU_HARDDISK_ZB0Q3USB0001-0:0",
            "usb-QEMU_QEMU_HARDDISK_ZB0Q3USB0001-0:0-part1",
            "virtio-ZB0Q3NEW0001",
        ]
        assert zarkcheck.by_id_name(names, "ZB0Q3USB0001") == names[0]
        for wrong in (names[1:], [names[0], "usb-OTHER_ZB0Q3USB0001-0:0"]):
            try:
                zarkcheck.by_id_name(wrong, "ZB0Q3USB0001")
            except zarkcheck.ReadBackError:
                continue
            raise AssertionError(wrong)

    def test_the_plan_line_and_the_anchors_kept(self):
        text = "x\nPlan: 3 at point, 1 new\nPlan: 30 incremental, 2 via bookmark, 1 excluded\n"
        states = zarkcheck.plan_states(text)
        assert states == {"incremental": 30, "via bookmark": 2, "excluded": 1}
        assert zarkcheck.anchors_kept(states).ok
        for lost in ("1 new", "2 rollback", "1 diverged"):
            assert not zarkcheck.anchors_kept(
                zarkcheck.plan_states(f"Plan: 3 incremental, {lost}")
            ).ok
        for bad in ("nothing", "Plan: three"):
            try:
                zarkcheck.plan_states(bad)
            except zarkcheck.ReadBackError:
                continue
            raise AssertionError(bad)

    def test_recovers_anchors_against_the_restored_system(self):
        text = "✓ 3 rpool dataset(s) anchored for backup ✓\n"
        assert zarkcheck.anchored_by_recover(text) == (3, [])
        assert zarkcheck.anchored_by_recover(
            "  rpool/ROOT/ubuntu_ab/var: not anchored for backup: "
            "its next backup of it resends it in full"
        ) == (0, ["rpool/ROOT/ubuntu_ab/var"])
        guid = "1234"
        stamp = "2026-11-24_06:03:02Z"
        marks = [
            f"rpool/ROOT/ubuntu_ab#zark_{guid}_{stamp}",
            f"rpool/USERDATA/home_cd#zark_{guid}_{stamp}",
            f"rpool/USERDATA/root_cd#zark_{guid}_2026-11-24_06:03:03Z",
            "rpool/x#zark_999_" + stamp,
        ]
        boot = [f"bpool/BOOT/ubuntu_ab@zark_{guid}_{stamp}"]
        assert zarkcheck.anchors_check(marks, boot, guid, 3).ok
        assert not zarkcheck.anchors_check(marks, boot, guid, 4).ok
        assert not zarkcheck.anchors_check(marks, [], guid, 3).ok
        assert not zarkcheck.anchors_check(marks + marks[:1], boot, guid, 4).ok  # one dataset twice
        assert not zarkcheck.anchors_check([], boot, guid, 0).ok

    def test_the_status_file(self):
        doc = {
            "drives": [
                {"name": "other"},
                {
                    "name": "backup",
                    "guid": "1",
                    "last_backup_point": "zark_2026-11-24_06:03:02Z",
                    "last_run": {"result": "completed"},
                },
            ]
        }
        entry = zarkcheck.status_drive(json.dumps(doc), "backup")
        assert zarkcheck.last_run_check(entry, "r").ok
        assert zarkcheck.last_run_check(entry, "r", after="zark_2026-11-24_06:00:00Z").ok
        assert not zarkcheck.last_run_check(entry, "r", after="zark_2026-11-24_06:03:02Z").ok
        entry["last_run"] = {"result": "not_verified"}
        assert not zarkcheck.last_run_check(entry, "r").ok
        for bad in ("{", json.dumps({"drives": []}), json.dumps([1])):
            try:
                zarkcheck.status_drive(bad, "backup")
            except zarkcheck.ReadBackError:
                continue
            raise AssertionError(bad)
        assert zarkcheck.same_digest("m", "ab", "ab").ok
        assert (
            not zarkcheck.same_digest("m", "", "").ok
            and not zarkcheck.same_digest("m", "ab", "cd").ok
        )


class TestBenchFullShapes:  # pylint: disable=missing-function-docstring
    """Mode full's machines, seed and live media, and its three guests' searches."""

    def test_the_usb_drive_is_a_node_plugged_in_later(self):
        port = qemu.usb_port(Path("/w/back,up.qcow2"))
        assert port[:2] == ["-device", "qemu-xhci,id=xhci"]
        assert port[3] == (
            "driver=qcow2,node-name=usbdisk,file.driver=file,file.filename=/w/back,,up.qcow2"
        )
        assert qemu.usb_plug("ZB0Q3USB0001") == {
            "driver": "usb-storage",
            "bus": "xhci.0",
            "drive": "usbdisk",
            "serial": "ZB0Q3USB0001",
            "id": "usbdrive",
        }
        installed = qemu.installed_argv(_machine(Path("/w")), Path("/w/t"), 1, tuple(port))
        assert installed[-4:] == port and "usb-storage" not in " ".join(installed)

    def test_the_live_and_the_recovered_machines(self):
        m = _machine(Path("/w"))
        live = " ".join(
            qemu.live_argv(
                m, Path("/w/l.iso"), Path("/w/s.img"), Path("/w/n.qcow2"), "SN", Path("/w/b"), 5
            )
        )
        assert "ide-cd,drive=cd0,bus=ide.0,bootindex=1" in live
        assert "virtio-blk-pci,drive=newdisk,serial=SN,bootindex=2" in live
        assert "drive=target" not in live and "qemu-xhci" in live
        rec = " ".join(qemu.recovered_argv(m, Path("/w/n.qcow2"), "SN", Path("/w/b"), 5))
        assert "virtio-blk-pci,drive=newdisk,serial=SN,bootindex=1" in rec and "ide-cd" not in rec
        assert "hostfwd=tcp:127.0.0.1:5-:22" in rec

    def test_the_live_gets_ssh_and_never_autoinstall(self):
        doc = seed.live_user_data("ssh-ed25519 AAAA bench")
        text = seed.render_cloud_config(doc)
        first, rest = text.split("\n", 1)
        assert first == "#cloud-config" and json.loads(rest) == doc
        assert "autoinstall" not in text
        user = doc["users"][0]
        assert user["name"] == seed.USERNAME and user["lock_passwd"] is True
        assert user["ssh_authorized_keys"] == ["ssh-ed25519 AAAA bench"]
        # The live's cloud.cfg runs no packages module: runcmd installs sshd.
        assert "packages" not in doc and "package_update" not in doc
        assert doc["runcmd"] == [["bash", "-c", seed.LIVE_SSH_SCRIPT]]
        script = seed.LIVE_SSH_SCRIPT
        assert "apt-get -o DPkg::Lock::Timeout=600 install -y openssh-server" in script
        assert 'echo "Suites: $VERSION_CODENAME $VERSION_CODENAME-updates"' in script
        assert script.startswith("exec > >(tee -a /var/log/zark-bench-live.log > /dev/ttyS0) 2>&1")
        assert subprocess.run(["bash", "-n"], input=script.encode(), check=False).returncode == 0
        assert doc["output"] == {"all": "| tee -a /var/log/cloud-init-output.log /dev/ttyS0"}
        assert "/dev/ttyS0" in doc["bootcmd"][0][2]
        out = remaster.edit_grub_cfg(GRUB_CFG, remaster.LIVE_ARGS)
        args = remaster.first_linux_args(out)
        assert (
            "autoinstall" not in args and "ds=nocloud" in args and "console=ttyS0,115200n8" in args
        )
        assert "zfsutils-linux" in inspect_iso.RECOVER_PACKAGES

    def test_each_guest_that_came_up_must_have_searched_itself(self):
        v = leak.stages_verdict
        assert v(True, [("installed", True, 0)]) == leak.upload_verdict(True, True, 0)
        three = [("installed", True, 0), ("live", True, 0), ("recovered", True, 0)]
        ok, why = v(True, three)
        assert ok and "(installed, live, recovered)" in why
        assert not v(False, three)[0]
        ok, why = v(True, [three[0], ("live", True, None), ("recovered", False, None)])
        assert not ok and why.startswith("live: ")
        ok, why = v(True, [three[0], ("live", True, 1), ("recovered", False, None)])
        assert not ok and why == "live: the guest's own search found a secret or failed"
        assert v(True, [("installed", False, None), ("live", False, None)])[0]
        steps = [
            {"step": "guest-leak-check", "status": 0, "detail": ""},
            {"step": "guest-leak-check-live", "status": 1, "detail": "a secret in /tmp"},
        ]
        assert leak.stages_of("install", {"installed"}, steps) == [("installed", True, 0)]
        assert leak.stages_of("full", {"installed", "live"}, steps) == [
            ("installed", True, 0),
            ("live", True, 1),
            ("recovered", False, None),
        ]
        notes = leak.stage_notes("full", {"installed", "live"}, steps).splitlines()
        assert notes == [
            "   leak check, guest (installed): no hit",
            "   leak check, guest (live): a secret in /tmp",
            "   leak check, guest (recovered): not run, the guest never came up over SSH",
        ]

    def _full(self, td: str) -> bench_run.Run:
        root = Path(td)
        run = bench_run.Run(mode="full", out=root / "out", work=root / "work")
        run.out.mkdir()
        run.work.mkdir()
        run.secrets = ["s3cret-passphrase-for-the-test"]
        run.guest_seen = True
        run.steps.append({"step": "guest-leak-check", "status": 0, "seconds": 1.0, "detail": ""})
        return run

    def test_finish_in_mode_full_waits_for_every_guest(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._full(td)
            run.seen_stages = {"live"}
            assert bench_run.finish(run, None) == 1
            assert not (run.out / leak.MARKER).exists()
        with tempfile.TemporaryDirectory() as td:
            run = self._full(td)
            run.seen_stages = {"live", "recovered"}
            for name in ("guest-leak-check-live", "guest-leak-check-recovered"):
                run.steps.append({"step": name, "status": 0, "seconds": 1.0, "detail": ""})
            assert bench_run.finish(run, None) == 0
            assert "live, recovered" in (run.out / leak.MARKER).read_text(encoding="utf-8")

    def test_rpools_key_in_any_image_holds_the_upload(self):
        key = bytes(range(32))  # holds a newline byte: never a text pattern
        with tempfile.TemporaryDirectory() as td:
            run = self._full(td)
            run.byte_secrets = [key]
            backup = Path(td) / "backup.qcow2"
            backup.write_bytes(b"\0" * 1000 + key + b"\0" * 1000)
            run.images = [backup]
            assert bench_run.finish(run, Path(td) / "absent.qcow2") == 1
            assert not (run.out / leak.MARKER).exists()
        with tempfile.TemporaryDirectory() as td:
            run = self._full(td)
            run.byte_secrets = [key]
            (run.out / "zark").mkdir()
            (run.out / "zark" / "x.transcript").write_bytes(b"a" + key + b"b")
            assert bench_run.finish(run, None) == 1

    def test_the_lives_cloud_init_modules_and_apt_sources_read_offline(self):
        cfg = (
            "users:\n   - default\n"
            "cloud_init_modules:\n - bootcmd\n - users-groups\n - ssh\n"
            "cloud_config_modules:\n# a comment\n - set-passwords\n - runcmd\n"
            "cloud_final_modules:\n - scripts-user\n - [final_message, always]\n"
            "system_info:\n   distro: ubuntu\n"
        )
        lists = inspect_iso.module_lists([cfg])
        assert lists == {
            "cloud_init_modules": ["bootcmd", "users-groups", "ssh"],
            "cloud_config_modules": ["set-passwords", "runcmd"],
            "cloud_final_modules": ["scripts-user", "final-message"],
        }
        assert not inspect_iso.runs_packages(lists)
        later = "cloud_final_modules:\n - package-update-upgrade-install\n"
        both = inspect_iso.module_lists([cfg, later])
        assert both["cloud_final_modules"] == ["package-update-upgrade-install"]
        assert inspect_iso.runs_packages(both)
        assert inspect_iso.wanted_from_layer("etc/apt/sources.list.d/ubuntu.sources")
        assert inspect_iso.wanted_from_layer("etc/apt/sources.list")
        sources = (
            "# comment\n\ndeb cdrom:[Ubuntu 26.04]/ resolute main\n"
            "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\n"
        )
        lines = inspect_iso.source_lines(sources)
        assert lines == [
            "deb cdrom:[Ubuntu 26.04]/ resolute main",
            "Types: deb",
            "URIs: http://archive.ubuntu.com/ubuntu",
        ]
        assert inspect_iso.network_sources(lines) == ["URIs: http://archive.ubuntu.com/ubuntu"]
        assert not inspect_iso.network_sources(lines[:1])

    def test_a_live_without_ssh_leaves_its_cloud_init_lines(self):
        serial = (
            "[  OK  ] Started cloud-init.service\r\nzark-bench: cloud-init read the live seed\r\n"
            "noise\nGet:1 http://archive.ubuntu.com/ubuntu resolute InRelease\n"
            "E: Unable to locate package openssh-server\nmore noise\n"
        )
        lines = zarkcheck.live_serial_lines(serial.encode())
        assert lines == [
            "[  OK  ] Started cloud-init.service",
            "zark-bench: cloud-init read the live seed",
            "Get:1 http://archive.ubuntu.com/ubuntu resolute InRelease",
            "E: Unable to locate package openssh-server",
        ]
        assert zarkcheck.live_serial_lines(serial.encode(), 1) == lines[-1:]

    def test_the_iso_reads_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            files = {}
            for inner, text in (
                ("etc/cloud/cloud.cfg.d/90_x.cfg", "cloud_final_modules:\n - final-message\n"),
                ("etc/cloud/cloud.cfg", "cloud_final_modules:\n - scripts-user\n"),
                ("etc/cloud/cloud.cfg.d/README", "cloud_final_modules:\n - nope\n"),
                ("etc/apt/sources.list.d/cdrom.sources", "Types: deb\nURIs: file:/cdrom\n"),
                ("etc/apt/sources.list", "# none\n"),
            ):
                path = root / inner.replace("/", "_")
                path.write_text(text, encoding="utf-8")
                files[inner] = path
            files["etc/apt/sources.list.d/gone.sources"] = root / "missing"
            cloud = inspect_iso.cloud_modules_report(files)
            assert cloud == {
                "cloud-init-modules": {"cloud_final_modules": ["final-message"]},
                "cloud-init-runs-packages": False,
            }
            apt = inspect_iso.apt_sources_report(files)
            assert apt == {
                "apt-sources": {
                    "etc/apt/sources.list": [],
                    "etc/apt/sources.list.d/cdrom.sources": ["Types: deb", "URIs: file:/cdrom"],
                },
                "apt-network-sources": [],
            }
