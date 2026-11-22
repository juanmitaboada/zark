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
Tests of the VM bench (bench/): everything that needs no virtual machine,
fed with text shaped like the real outputs and with local sockets.

``tests/test_unit.py`` imports every Test* class here by name, so its runner
finds them; run that file, not this one.
"""

import io
import json
import re
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

from bench import console, firmware, inspect_iso, iso, layout, leak, qemu, remaster, sbproof, seed
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

    def test_a_missing_socket_times_out(self):
        try:
            Qmp.connect(Path("/nonexistent/q.sock"), timeout_s=0.1)
        except OSError:
            return
        raise AssertionError("no error")


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

    def read(self, _wait_s: float) -> bytes | None:
        """The next chunk; never waits."""
        if self.chunks:
            return self.chunks.pop(0)
        return b"" if self.closes else None

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
        assert console.unlock(con, "s3cret", 5, 5) == "askpass"
        assert line.sent() == b"s3cret\r"
        assert b"s3cret" not in transcript.getvalue()
        assert b"Please unl" in transcript.getvalue()

    def test_a_second_prompt_fails(self):
        prompt = b"Please unlock disk keystore-rpool: "
        line = _Line([prompt, b"\r\nNo key available\r\n" + prompt])
        try:
            console.unlock(line.console(), "x", 5, 5)
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
        good = sbproof.guest_checks("SecureBoot enabled\n", b"\x06\x00\x00\x00\x01")
        assert all_ok(good)
        bad = sbproof.guest_checks("SecureBoot disabled\n", b"\x06\x00\x00\x00\x00")
        assert [c.ok for c in bad] == [False, False]
        assert "unreadable" in sbproof.guest_checks("", b"")[1].detail

    def test_kernel_and_negative_lines(self):
        assert sbproof.kernel_check("[0.0] secureboot: Secure boot enabled\n").ok
        assert not sbproof.kernel_check("secureboot: Secure boot disabled").ok
        log = 'BdsDxe: failed to load Boot0001 "UEFI Misc Device" from x: Access Denied\n'
        assert sbproof.negative_check(log).ok
        assert not sbproof.negative_check("BdsDxe: starting Boot0001").ok

    def test_host_argv(self):
        m = _machine(Path("/w"))
        argv = qemu.installed_argv(m, Path("/w/t"), 1)
        fw = firmware.Firmware(Path("/w/code,x.fd"), Path("/w/v"), Path("/w/d"))
        assert not sbproof.host_check(argv, fw).ok  # the comma is doubled in the option
        fw_plain = firmware.Firmware(Path("/w/code.fd"), Path("/w/v"), Path("/w/d"))
        m2 = qemu.Machine(fw_plain, m.vars_file, m.serial_socket, m.serial_log, m.qmp_socket)
        argv2 = qemu.installed_argv(m2, Path("/w/t"), 1)
        assert sbproof.host_check(argv2, fw_plain).ok
        no_smm = [a.replace("smm=on", "smm=off") for a in argv2]
        assert "smm" in sbproof.host_check(no_smm, fw_plain).detail


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
