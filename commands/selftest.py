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
zark selftest: check that this platform behaves as zark relies on (M4s).

  zark selftest [run] [--stick <device>] [--only <ID>[,<ID>...]]
  zark selftest status
  zark selftest report [<report file>]
  zark selftest certificate [--into <file>] <report file>...
  zark selftest warnings on|off

``run`` checks level 1 on file-backed pools (no disk is touched) and, with
``--stick``, level 2 on a blank scratch stick that it erases. Exit status:
0 when no check failed (SKIPs allowed), 1 when one did; a signal stops the
run with zark's usual status after its cleanup.
"""

import json
from pathlib import Path
from typing import Any

from lib import certificates, lock, platform_facts, sh
from lib.certificates import RunInfo
from lib.cleanup import Cleanup
from lib.config import Config, zark_command
from lib.drives import validate_external_block_device
from lib.identity import DiskIdentity, match_registry, usb_ids
from lib.log import Log
from lib.selftest import Bench, Check, Result, RunState, SetupError, all_checks, run_checks
from lib.selftest_l2 import StickBench

USAGE = (
    "Usage: zark selftest [run] [--stick <device>] [--only <ID>[,<ID>...]] | status | "
    + "report [<file>] | certificate [--into <file>] <file>... | warnings on|off"
)


def run(args: list[str]) -> None:
    """Dispatch the subcommand."""
    log = Log()
    sub = lock.selftest_subcommand(args)
    rest = args[1:] if args and not args[0].startswith("-") else args
    handlers = {
        "run": _run,
        "status": _status,
        "report": _report,
        "certificate": _certificate,
        "warnings": _warnings,
    }
    handler = handlers.get(sub)
    if handler is None:
        log.error(f"Unknown selftest subcommand '{sub}'")
        log.info(USAGE)
        raise SystemExit(2)
    handler(rest, log)


# ── run ──────────────────────────────────────────────────────────────────


def _parse_run(args: list[str], log: Log) -> tuple[str, list[str]]:
    stick, only = "", []
    i = 0
    while i < len(args):
        if args[i] == "--stick" and i + 1 < len(args):
            stick, i = args[i + 1].strip(), i + 2
            if not stick:
                # An unset variable in `--stick "$DEV"` would run level 1 only (M4dF-2).
                log.error("--stick needs a device")
                log.info(USAGE)
                raise SystemExit(2)
        elif args[i] == "--only" and i + 1 < len(args):
            only, i = [x.strip().upper() for x in args[i + 1].split(",") if x.strip()], i + 2
        elif args[i] == "--debug":
            i += 1
        else:
            log.error(f"Unknown option '{args[i]}'")
            log.info(USAGE)
            raise SystemExit(2)
    return stick, only


def _select(only: list[str], stick: str, log: Log) -> list[Check]:
    checks = [c for c in all_checks() if c.level == 1 or stick]
    if not only:
        return checks
    unknown = sorted(set(only) - {c.cid for c in all_checks()})
    if unknown:
        log.error(f"Unknown check(s): {', '.join(unknown)}")
        raise SystemExit(2)
    picked = [c for c in checks if c.cid in only]
    if not picked:
        log.error("--only names level-2 checks: give --stick too")
        raise SystemExit(2)
    return picked


def _stick_identity(dev: str, log: Log) -> DiskIdentity:
    """The stick, refused unless it is a safe external disk, unregistered and
    blank, and the operator types its kernel name (M4s-4)."""
    ident = validate_external_block_device(dev, log, command="selftest run --stick")
    if not ident.by_id:
        log.fatal(
            f"{dev} has no stable /dev/disk/by-id name",
            causes=["zark checks the stick by that name before every write"],
            solutions=[
                "Use a stick or enclosure that exposes a by-id name",
                "Check: ls -l /dev/disk/by-id/",
            ],
        )
    cfg = Config.load()
    names = match_registry(ident, cfg.known_drives)
    if names:
        log.fatal(
            f"{dev} is the registered backup drive {', '.join(names)}: "
            + "the self-test erases its disk",
            solutions=["Use a scratch stick that zark does not know"],
        )
    parts = [ln for ln in sh.run(f"lsblk -nr -o NAME,TYPE {ident.disk}").lines if " part" in ln]
    probe = sh.run(f"blkid -p {ident.disk}")
    if parts or probe.returncode != 2:
        log.fatal(
            f"{dev} is not blank: the self-test only erases a disk that holds nothing",
            causes=[f"{len(parts)} partition(s)" if parts else f"blkid -p found: {probe.output}"],
            solutions=[f"Wipe it first: {zark_command()} purge {ident.by_id_path or ident.disk}"],
        )
    log.info("Level 2 erases this disk:")
    log.info(f"  {ident.disk}  {ident.model}  {ident.size}  serial {ident.serial or '?'}")
    log.info(f"  {ident.by_id_path}")
    answer = log.ask_text(f"  Type {ident.name} to erase it: ", accept=(ident.name,))
    if answer != ident.name:
        log.info("Aborted: nothing was touched")
        raise SystemExit(1)
    return ident


def _run(args: list[str], log: Log) -> None:
    stick_dev, only = _parse_run(args, log)
    checks = _select(only, stick_dev, log)
    log.banner("SELF-TEST", "Does this platform behave as zark relies on?")
    stick = _stick_identity(stick_dev, log) if stick_dev else None
    facts = _show_facts(log)
    cleanup = Cleanup(log)
    cleanup.register()
    state = RunState()
    level = 2 if stick else 1
    bridge = ":".join(usb_ids(stick.name)).strip(":") if stick else ""
    # Both benches and their masks before the first check, so a stopped
    # run's report is masked as a complete one's (M4sR-1).
    benches: list[Bench] = [Bench(log, cleanup)]
    if stick:
        benches.append(StickBench(log, cleanup, stick))
    state.masks = {k: v for b in benches for k, v in b.masks().items()}
    before = _system_pools()
    try:
        _run_levels(checks, benches, log, state)
    except BaseException:
        # A signal or a dead stdout: the record says the run was stopped,
        # which covers nothing; the cleanup still runs, from atexit.
        _save(facts, RunInfo(level, True, bridge), state.results, state.masks, log, stopped=True)
        raise
    after = _system_pools()
    if after != before:
        log.warn(f"The system's pools changed during the run: {before} -> {after}")
    path = _save(facts, RunInfo(level, bool(only), bridge), state.results, state.masks, log)
    if not _summary(state.results, path, log):
        raise SystemExit(1)


def _show_facts(log: Log) -> platform_facts.Facts:
    facts = platform_facts.read()
    for key in ("ubuntu", "kernel", "zfs", "zfs_kmod", "util_linux", "systemd", "python"):
        log.info(f"{key:<11} {facts.exact.get(key) or '?'}")
    log.info(f"{'where':<11} {facts.where}" + (f" ({facts.iso})" if facts.iso else ""))
    return facts


def _summary(results: list[Result], path: Path | None, log: Log) -> bool:
    """The closing banner; whether no check failed."""
    fails = [r.key for r in results if r.result == "FAIL"]
    skips = len([r for r in results if r.result == "SKIP"])
    log.blank()
    summary = f"{len(results) - len(fails) - skips} PASS, {len(fails)} FAIL, {skips} SKIP"
    if fails:
        log.banner_error("SELF-TEST: FAILURES", [summary, *fails])
    else:
        log.banner_ok("SELF-TEST PASSED", [summary])
    log.info(f"Report: {path}")
    log.info(f"To share it: {zark_command()} selftest report")
    return not fails


def _run_levels(checks: list[Check], benches: list[Bench], log: Log, state: RunState) -> None:
    for n, bench in enumerate(benches, 1):
        _run_level(checks, n, bench, log, state)


def _run_level(checks: list[Check], level: int, bench: Bench, log: Log, state: RunState) -> None:
    mine = [c for c in checks if c.level == level]
    if not mine:
        return
    log.info(f"Level {level}: {len(mine)} check(s)")
    try:
        bench.setup()
    except SetupError as e:
        log.fatal(f"The self-test bench could not be built: {e}", solutions=["Nothing was checked"])
    if level == 1:
        log.ok(f"Bench ready: pools {bench.a} and {bench.b} in {bench.dir}")
    _ = run_checks(mine, bench, log, state)
    bench.teardown()


def _system_pools() -> list[str]:
    r = sh.run("zpool list -H -o name")
    return sorted(p for p in r.lines if not p.startswith("zst")) if r.ok else []


def _save(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    facts: platform_facts.Facts,
    info: RunInfo,
    results: list[Result],
    masks: dict[str, str],
    log: Log,
    stopped: bool = False,
) -> Path | None:
    report = certificates.build_report(facts, info, results, masks)
    if stopped:
        report["certificate"]["stopped"] = True
    try:
        return certificates.write_report(report)
    except OSError as e:
        log.warn(f"Could not write the self-test record: {e}")
        return None


# ── status, report, certificate, warnings ────────────────────────────────


def _status(args: list[str], log: Log) -> None:
    if args:
        log.error("zark selftest status takes no options")
        raise SystemExit(2)
    facts = platform_facts.read()
    checks = all_checks()
    record = certificates.read_record()
    shipped = certificates.read_certificates()
    log.banner("SELF-TEST STATUS", "Coverage of this platform")
    log.info("Platform: " + ", ".join(f"{k} {v}" for k, v in facts.series.items()))
    log.info(f"Where: {facts.where}" + (f" ({facts.iso})" if facts.iso else ""))
    for label, entries in (("certificates shipped with zark", shipped), ("this machine", record)):
        have = certificates.covered(facts, entries)
        counts = [
            f"{len([c for c in checks if c.level == n and c.key in have])}/"
            + f"{len([c for c in checks if c.level == n])} level {n}"
            for n in (1, 2)
        ]
        log.info(f"Covered by {label}: {', '.join(counts)}")
    for e in record:
        fails = ", ".join(f["id"] for f in e.get("failed", [])) or "none failed"
        log.info(
            f"Last run here: {e.get('date', '?')} {e.get('where', '?')} level {e.get('level')}"
            + f"{' (partial)' if e.get('partial') else ''}: {fails}"
        )
    on = certificates.subscribed(log)
    log.info(f"Warnings: {'on' if on else 'off'} (zark selftest warnings on|off)")
    lines = certificates.warning_lines(facts, checks, record, shipped)
    for line in lines:
        log.warn(line)
    if not lines:
        log.ok("Nothing to warn about")


def _report(args: list[str], log: Log) -> None:
    path = Path(args[0]) if args else certificates.latest_report()
    if path is None or not path.is_file():
        log.error(f"No self-test report yet: run {zark_command()} selftest")
        raise SystemExit(1)
    report = _load(path, log)
    link, whole = certificates.issue_link(report)
    log.info(f"Report: {path}")
    if whole:
        log.info("Open this link to file it on GitHub (nothing is sent until you submit):")
    else:
        log.warn("The report is too long for a link: open it, then paste the file's content")
    print(link)


def _load(path: Path, log: Log) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.error(f"{path}: {e}")
        raise SystemExit(1) from e
    if not isinstance(data, dict):
        log.error(f"{path}: not a self-test report")
        raise SystemExit(1)
    return data


def _certificate(args: list[str], log: Log) -> None:
    into = ""
    if args[:1] == ["--into"] and len(args) > 1:
        into, args = args[1], args[2:]
    if not args:
        log.error("Usage: zark selftest certificate [--into <file>] <report file>...")
        raise SystemExit(2)
    entries: list[dict[str, Any]] = []
    for name in args:
        try:
            entries.append(certificates.certificate_entry(_load(Path(name), log)))
        except ValueError as e:
            log.error(f"{name}: {e}")
            raise SystemExit(1) from e
    if not into:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return
    target = Path(into)
    current = certificates.read_certificates(target) if target.exists() else []
    keyed = {_cert_key(old): old for old in current}
    for new in entries:
        keyed[_cert_key(new)] = new
    merged = sorted(keyed.values(), key=_cert_key)
    target.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.ok(f"{len(entries)} new, {len(merged)} in all: {target}")


def _cert_key(e: dict[str, Any]) -> str:
    series = json.dumps(e.get("series", {}), sort_keys=True)
    return f"{e.get('zark')} {e.get('where')} {e.get('level')} {series}"


def _warnings(args: list[str], log: Log) -> None:
    if args not in (["on"], ["off"]):
        log.error("Usage: zark selftest warnings on|off")
        raise SystemExit(2)
    path = certificates.set_subscribed(args[0] == "on")
    log.ok(f"Self-test warnings {args[0]} ({path})")
