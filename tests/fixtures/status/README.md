# Status file fixtures

Example `progress.json` and `status.json` files (`docs/STATUS-FILES.md`), one
directory per state a reader has to show. Each directory mirrors the real
paths, so a reader under test can point its root at it:

    <case>/run/zark/progress.json      (only while a command runs)
    <case>/var/lib/zark/status.json
    <case>/expected.json               (what a reader must conclude)

Every `status.json` and `progress.json` validates against `docs/schema/`,
except where the case says otherwise (`newer-schema`, `progress-unreadable`).
Pool figures are carmen's of 2026-10-24 (rpool at 89 %, bpool at 14 %); the
dataset names under them are illustrative. The drives' figures are made up.

As on a real pool, each `datasets` listing has a hidden part above zero:
the root's `usedbychildren` minus its listed children's `used`, what ZFS
keeps in `$MOS` and the other directories `zfs list` never shows (rpool
437.8 MiB, bpool 3.0 MiB, black 1.2 GiB; the sizes are illustrative). So
the own shares sum to a little less than the pool's `used_bytes`, and the
shares plus the hidden part make it.

In the drive contents cases, black's points come from daily backups from
November 2025 to its last backup, passed through zark's retention policy
(`lib/retention.py`, `plan_prune`) with black's settings (30-day window):
one point a month outside the window, every point inside it, one
incomplete run. `retention_next` is the same policy with the clock moved
7 days forward. `reclaim_by_age` lists the candidates zark's own rules
give (`space_candidates`, `point_removals`): the 10 monthly points
outside the window, November 2025 to August 2026. The bytes are made up;
no `reclaim_bytes` is a ZFS figure.

## expected.json

| Field | Meaning |
|---|---|
| `now` | the reader's clock for this case: every orphan, stalled and stale rule is judged at this time |
| `progress_process` | `alive`: the reader under test treats the process `progress.json` names as alive (boot id, pid and start ticks match); `dead`: the real check applies and finds it gone; null: no `progress.json` |
| `progress` | `idle`, `running`, `stalled`, `orphan` or `unknown` (Orphan detection) |
| `status` | `ok`, `stale`, `newer`, `unreadable` or `absent` |
| `indicator` | the state of "Reading the files", Indicator state |
| `what` | the case in one line |
| `drives` | only in the drive contents cases: per drive, what its tab shows. `margin_bytes`, and `contents`: null when the drive carries none of the optional contents (the tab keeps its space bar and margin), else `points`, `months` (count per UTC month), `next_points`, `next_reclaim_bytes`, `available_after_policy_bytes`, `datasets` and `frees_if_older` (per `before` month, what removing older points frees), each null when the field it comes from is absent |

## Cases

| Case | progress.json | status.json | indicator |
|---|---|---|---|
| `idle` | absent | hourly refresh; black 1 day, blue 13 days; rpool `warning` notice | `ok` |
| `running` | backup to black, step 7, 9 of 21 transfers, 7.0 of 17.0 GiB at 107 MiB/s | as `idle` | `running` |
| `stalled` | as `running`, heartbeat 95 s old | as `idle` | `running` |
| `waiting` | backup at step 5, asking for the passphrase | as `idle` | `waiting` |
| `waiting-eject` | backup at step 8, the eject prompt | already `completed` | `waiting` |
| `completed` | absent | black `completed`: new point, new space | `ok` |
| `incomplete` | absent | black `incomplete`, 2 datasets missing | `attention` |
| `not-verified` | absent | black `not_verified` | `attention` |
| `aborted` | absent | black `aborted` before the first byte | `ok` |
| `orphan` | `running`'s file with pid 4194304 | as `idle` | `edge` |
| `interrupted` | absent | the orphan recorded by the next command (`zark mount`) | `attention` |
| `no-byte-counts` | `bytes_done` and `rate_bytes_per_s` null | as `idle` | `running` |
| `partial-estimate` | `bytes_total_partial` true | as `idle` | `running` |
| `newer-schema` | absent | `schema_version` 2 and an unknown field; fails the v1 schema on `schema_version` only | `edge` |
| `stale` | absent | 6 h old (`stale_after_s` 10800) | `edge` |
| `progress-unreadable` | truncated JSON | as `idle` | `edge` |
| `overdue` | absent | newest backup 9 days old | `overdue` |
| `no-status` | absent | absent | `none` |
| `drive-contents` | absent | as `idle`, black with `datasets`, `points`, `non_point_snapshots`, `retention_next` and `reclaim_by_age`; blue without them | `ok` |
| `drive-contents-partial` | absent | as `idle`, black with `points`, `retention_next` (`reclaim_bytes` null) and `reclaim_by_age` measured for its 6 oldest entries only, no `datasets` | `ok` |

## Liveness

A fixture cannot name a live process or the current boot, so with the real
checks every `progress.json` here is an orphan. A reader under test injects
its clock (`now`) and, for `alive` cases, its liveness check. The `orphan`
case uses pid 4194304, which no Linux process can have (`PID_MAX_LIMIT` on
64-bit; pids are allocated below `pid_max`, whose maximum is that value), so
the real check finds it gone on any machine.

To see the running state against the real kernel, copy a case and point it
at a live process of your own:

    mkdir -p /tmp/zark-dev/run/zark
    cp -r running/var /tmp/zark-dev/
    sleep 600 &
    pid=$!
    ticks=$(sed 's/.*) //' /proc/$pid/stat | cut -d' ' -f20)
    now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    jq --argjson pid "$pid" --argjson ticks "$ticks" \
       --arg boot "$(cat /proc/sys/kernel/random/boot_id)" --arg now "$now" \
       '.pid = $pid | .pid_start_ticks = $ticks | .boot_id = $boot | .updated_at = $now' \
       running/run/zark/progress.json > /tmp/zark-dev/run/zark/progress.json

`starttime` is field 22 of `/proc/<pid>/stat`, the 20th after the `) ` that
closes the process name. Run the `jq` line again within 30 s to keep the
heartbeat fresh; stop it to see `stalled`, and `kill $pid` to see `orphan`.
