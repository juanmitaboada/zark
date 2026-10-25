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

## expected.json

| Field | Meaning |
|---|---|
| `now` | the reader's clock for this case: every orphan, stalled and stale rule is judged at this time |
| `progress_process` | `alive`: the reader under test treats the process `progress.json` names as alive (boot id, pid and start ticks match); `dead`: the real check applies and finds it gone; null: no `progress.json` |
| `progress` | `idle`, `running`, `stalled`, `orphan` or `unknown` (Orphan detection) |
| `status` | `ok`, `stale`, `newer`, `unreadable` or `absent` |
| `indicator` | the state of "Reading the files", Indicator state |
| `what` | the case in one line |

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
