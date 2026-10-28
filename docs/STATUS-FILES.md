# Status files

Schema version 1.

zark is not a daemon: it lives only while a command runs. So that other
programs (a GNOME Shell extension, `zark monitor`, a panel applet, a script)
can show what zark is doing and how the backups stand, zark writes two JSON
files. Readers watch them with inotify; nothing is served over a socket or
D-Bus, and nothing needs root to read them.

| File | Filesystem | Exists | Written by |
|---|---|---|---|
| `/run/zark/progress.json` | tmpfs | only while a command that changes state runs | that command, at most once a second, with a heartbeat at least every 5 s |
| `/var/lib/zark/status.json` | persistent | from the first write on | every command that changes state, at its end; `zark status` run as root, hourly from the alerts timer |

Both are 0644, owned by root, in 0755 directories, and hold nothing secret.
Machine-readable schemas: `docs/schema/zark-progress.schema.json` and
`docs/schema/zark-status.schema.json` (JSON Schema 2020-12). Example files for
every state a reader has to show, with the state expected for each:
`tests/fixtures/status/`. The Debian package installs this document as
`/usr/share/doc/zark/STATUS-FILES.md.gz` and the schemas, uncompressed, in
`/usr/share/doc/zark/schema/`; they are documentation, which dpkg can be told
to leave out, so a reader must not need them at run time.

## Who keeps them current

zark itself, with no resident process.

- The running command writes `progress.json`: when it takes the lock, at
  every event listed under "Write cadence", and from a heartbeat inside the
  same process while it lives. It removes the file when it ends.
- `status.json` is written by every command that changes state, at its end,
  whatever its result, and by `zark status` when it runs as root: hourly from
  `zark-status.timer` (`zark status --refresh --notify`, installed by
  `zark setup` or `zark alerts install`) and when run by hand.
- Nothing is written from a live session (its `/var/lib` is thrown away at
  shutdown), except `progress.json` while a command runs there.
- Between two writes nothing refreshes the files. Figures that change on
  their own (a pool filling up, the age of a backup) are as old as
  `measured_at` or `generated_at` says. A reader that needs live pool figures
  reads `zfs list` and `zpool list` itself, which works unprivileged; a reader
  never imports a pool (see "Reading the files").

## Common rules

- Encoding: UTF-8 JSON, one object, no BOM. Non-ASCII characters may appear
  escaped (`\uXXXX`).
- Timestamps: ISO 8601 UTC, second precision, `Z` suffix
  (`2026-10-24T09:15:02Z`), the format of `last_backup_at` in
  `known_drives.json`.
- Sizes: integers in bytes; readers choose the units they show (zark itself
  shows binary units, KiB to TiB, as `zfs list` and `zpool list` do).
  Percentages: integers 0-100.
- Dataset names: full names in their pool (`rpool/USERDATA/home_abc123` in
  a source pool, `black/rpool/USERDATA/home_abc123` on a drive).
- The top-level object of each file carries `kind` and `schema_version`.
- Some strings are for people only and carry no meaning a program may parse:
  `step.label`, `waiting_for`, `last_run.message` and a notice's `text`. They
  are English, may change in any version, and carry no colour codes.

### Versioning

`schema_version` is an integer, the major version.

- Adding an optional field, or a new value to an enumeration documented as
  open, does not change it. Readers ignore fields they do not know, and treat
  an unknown value of an open enumeration as the generic case this document
  names for it.
- Removing or renaming a field, changing its type or meaning, making an
  optional field required, or adding a value to a closed enumeration
  increments it.
- A reader that finds a `schema_version` higher than the one it supports does
  not interpret the file: it keeps its last good state, if any, and says it
  needs an update.
- A writer never writes over a `status.json` whose `schema_version` is higher
  than its own (a newer zark wrote it, and an older one, run from a stick,
  would drop what it does not know). It logs a warning and leaves the file
  alone. `progress.json` is always replaced: it belongs to the running
  command.

### Atomic write

1. `tempfile.mkstemp(prefix=".<name>.", dir=<the file's directory>)`: the
   temporary name starts with a dot, so a reader watching the directory never
   takes it for the real file.
2. Write the whole text, `fchmod` to 0644, and, for `status.json` only,
   `fsync` the file (tmpfs needs none).
3. `os.replace(tmp, path)`: a reader sees the old file or the new one, never
   a partial one.
4. For `status.json` only, `fsync` the directory.
5. On any error the temporary file is removed and the error is logged as a
   warning: a failed status write never fails a command.

### Directories

- `/run/zark` is created at boot by the package's `tmpfiles.d` entry, 0755
  root root, so a reader can watch it before zark first runs. A zark run
  from a stick or a checkout creates it when missing.
- `/var/lib/zark` is 0755 root root; the writer creates it when missing. It
  also holds `selftest/`, 0700, which readers do not need.
- Whoever creates either directory gives it 0755 explicitly, whatever the
  umask: a reader must be able to reach the files.

### Concurrency

- `progress.json` has one writer at a time: only a command that holds
  `/run/zark/zark.lock` writes it, and the lock admits one.
- `status.json` has writers that do not share that lock: `zark status`, which
  the timer runs, is read-only for zark's state and runs without it, so it can
  run while a backup ends. Every `status.json` write is a read-merge-write
  under `flock(LOCK_EX)` on `/run/zark/status.lock` (0600, never `zark.lock`),
  waited for at most 10 s; on timeout the write is skipped with a warning.
  Without it the timer could read the file before the backup's write and
  replace it afterwards, losing the run's result.

## /run/zark/progress.json

Present while a command that takes the lock runs; absent otherwise.

```json
{
  "kind": "zark-progress",
  "schema_version": 1,
  "zark_version": "2.1.0",
  "command": "backup",
  "pid": 48213,
  "pid_start_ticks": 2961734,
  "boot_id": "6f1c0b8e-3d52-4a8e-9c1f-2b7d4e5a9c03",
  "started_at": "2026-10-24T09:02:11Z",
  "updated_at": "2026-10-24T09:19:41Z",
  "waiting_for_input": false,
  "waiting_for": null,
  "step": {"index": 7, "total": 8, "label": "Replicating (this may take a while)..."},
  "drive": {"name": "black", "guid": "...", "drive_id": "...", "space": {"...": "..."}},
  "point": "zark_2026-10-24_09:03:02Z",
  "transfer": {"...": "..."}
}
```

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"zark-progress"` | |
| `schema_version` | integer | see Versioning |
| `zark_version` | string | the writer's version |
| `command` | string, open enumeration | the command as typed (`backup`, `prepare`, `recover`, `prune`, `purge`, `finish`, `setup`, `exclude`, ...). Generic case: "zark is busy" |
| `pid` | integer | pid of the zark process, the one holding the lock |
| `pid_start_ticks` | integer | field 22 (`starttime`) of `/proc/<pid>/stat`: clock ticks since boot; tells a reused pid apart |
| `boot_id` | string | `/proc/sys/kernel/random/boot_id` when the command started |
| `started_at` | timestamp | when the command started |
| `updated_at` | timestamp | last write: the heartbeat (see Orphan detection) |
| `waiting_for_input` | boolean | the command is waiting for an answer in its terminal |
| `waiting_for` | string or null | the question or prompt shown, for people only; null when not waiting |
| `step` | object or null | the command's last numbered step: `index` and `total` (integers from 1), `label` (for people only); null before the first one or for a command without steps |
| `drive` | object or null | the backup drive the command works on, once chosen |
| `point` | string or null | the backup point of this run (`zark_YYYY-MM-DD_HH:MM:SSZ`) once taken; backup only |
| `transfer` | object or null | non-null only while a backup moves data (step 7); null otherwise and for every other command |

`waiting_for_input` is true during every read of the terminal: yes/no
questions (timed ones and the eject prompt included), choices, text and
number entries, the passphrase, "Press Enter" after a physical step, and the
pause after a fatal error. It goes back to false as soon as the answer, or
end of input, arrives.

`drive`:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | pool name, the registry key |
| `guid` | string | pool GUID, decimal |
| `drive_id` | string | registry `drive_id`, a `/dev/disk/by-id` name |
| `space` | Space object or null | the drive's space while zark has it imported; null before the first read |

`transfer`:

| Field | Type | Meaning |
|---|---|---|
| `datasets_total` | integer | dataset transfers planned so far in this run: one per dataset per round; a dataset resumed in one round and finished in the next counts twice. It may grow while the run goes on |
| `datasets_done` | integer | of those, how many ended (sent or failed) |
| `datasets_failed` | integer | of `datasets_done`, how many failed |
| `dataset` | string or null | the dataset being sent now; null between two |
| `dataset_state` | string or null, open enumeration | its plan state: `new`, `incremental`, `resume`, `via bookmark`. Generic case: "sending" |
| `bytes_total` | integer or null | sum of the stream sizes `zfs send -nvP` estimated for every send planned so far; null when no send has an estimate. It may grow with `datasets_total` |
| `bytes_total_partial` | boolean | true when at least one planned send has no estimate, so `bytes_total` is a floor |
| `bytes_done` | integer or null | stream bytes sent so far in this run, every send included |
| `rate_bytes_per_s` | integer or null | `bytes_done`'s rate over the last 30 s; null until two measurements exist |

#### Byte counters

- A writer of schema 1 fills `bytes_done` and `rate_bytes_per_s` during every
  send, measured outside the data path, and updates them at least every 5 s.
  `bytes_done` is cumulative over the run, so it stays non-null between two
  sends.
- Both counters measure the send stream, the same unit as `bytes_total`:
  neither the datasets' size nor what lands on the drive (a raw send of
  compressed data is smaller than the data it holds).
- `bytes_done` may exceed `bytes_total`: estimates are approximate, and a
  resumed send repeats part of a stream.
- Null means "not measured": the writer could not read the counter. A reader
  then shows no byte bar, rate or ETA, and falls back to the dataset counts
  and the current dataset. A null is never a zero.
- A reader computes an ETA only when `bytes_total` and `rate_bytes_per_s` are
  non-null, `rate_bytes_per_s` is above 0 and `bytes_total_partial` is false,
  and keeps a byte bar below 100 % while `transfer` is non-null.

### Write cadence

- At start, right after the lock is taken; at every numbered step; when a
  drive is chosen or its space is read; when the point is taken; when a
  dataset transfer starts or ends; when `waiting_for_input` changes.
- A heartbeat at least every 5 s, from the same process, so that `updated_at`
  keeps moving while the command waits on a `zfs send` or on the terminal;
  the byte counters are refreshed with it.
- Never more than one write a second: events closer together are coalesced
  into the next write.

### Lifecycle

- The file is created when the command takes the lock and removed when the
  command ends, after the run's result is in `status.json`, so a reader that
  sees it disappear already finds the result.
- A backup records its result in `status.json` when the result is known (the
  banner that says it), before the eject prompt. During that prompt
  `progress.json` still exists with `waiting_for_input` true and `transfer`
  null. A signal after the result was recorded does not change it.

### Orphan detection

A `progress.json` is an orphan when the process that wrote it no longer runs
(SIGKILL, an OOM kill, a crash nothing could clean up after). A reboot
removes it, since `/run` is tmpfs. A reader classifies it, first match wins:

1. Absent: `idle`.
2. Not parseable, not an object, `kind` other than `zark-progress`, or
   `schema_version` higher than supported: `unknown`.
3. `boot_id` differs from `/proc/sys/kernel/random/boot_id`: `orphan`.
4. `/proc/<pid>` does not exist: `orphan`.
5. `/proc/<pid>/stat` is readable and its field 22 differs from
   `pid_start_ticks`: `orphan` (the pid was reused). Fields are counted after
   the last `)` of the line, since the process name may contain spaces or
   parentheses: field 22 is the 20th after it. If the file is not readable
   (`hidepid`), this check is skipped.
6. `updated_at` more than 30 s before the reader's clock: `stalled`. The
   process exists but the heartbeat stopped. A stalled run is still a run:
   it is shown as running, its figures marked as not current, never as an
   orphan.
7. Otherwise: `running`.

On a default Ubuntu every one of these reads works unprivileged.

A command that takes the lock knows any `progress.json` it finds is an
orphan, since the lock admits one run. Before replacing it, if the orphan's
`command` is `backup` and it names a `drive`, the new command records in
`status.json` a `last_run` for that drive with result `interrupted` and
`ended_at` set to the orphan's `updated_at`.

## /var/lib/zark/status.json

The state between runs: the source pools, every registered drive, and what
the MOTD shows.

```json
{
  "kind": "zark-status",
  "schema_version": 1,
  "zark_version": "2.1.0",
  "generated_at": "2026-10-24T08:58:12Z",
  "generated_by": "timer",
  "registry": "/etc/zark/known_drives.json",
  "thresholds": {
    "capacity_warning_pct": 80,
    "capacity_critical_pct": 90,
    "rpo_warning_days": 7,
    "stale_after_s": 10800
  },
  "source_pools": [],
  "drives": [],
  "notices": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `kind` | `"zark-status"` | |
| `schema_version` | integer | see Versioning |
| `zark_version` | string | the writer's version |
| `generated_at` | timestamp | when this file was written |
| `generated_by` | string, open enumeration | `timer` for `zark status --refresh`, `status` for `zark status` by hand, else the command that wrote it (`backup`, `prepare`, `registry`, `prune`, ...). For people and logs |
| `registry` | string | the `known_drives.json` the writer used (`$ZARK_CONFIG_DIR`, the portable tree's `etc/`, or `/etc/zark/`) |
| `thresholds` | object | the writer's limits, so readers judge as zark does: `capacity_warning_pct`, `capacity_critical_pct`, `rpo_warning_days`, and `stale_after_s`, after which a reader treats this file as stale (three refresh intervals of the alerts timer) |
| `source_pools` | array | one entry per source pool present, rpool then bpool |
| `drives` | array | one entry per drive in the writer's registry, sorted by name |
| `notices` | array | the MOTD's lines, structured |

### Space object

Describes a whole pool: each source pool, each drive, and `progress.json`'s
`drive.space`. Every field is read in the same pass.

| Field | Type | Source |
|---|---|---|
| `used_bytes` | integer | `zfs list -Hp -o used <pool>` |
| `available_bytes` | integer | `zfs list -Hp -o avail <pool>` (reservations and the slop reserve already taken out) |
| `pool_size_bytes` | integer | `zpool list -Hp -o size <pool>` |
| `pool_alloc_bytes` | integer | `zpool list -Hp -o alloc <pool>` |
| `pool_free_bytes` | integer | `zpool list -Hp -o free <pool>` |
| `capacity_pct` | integer | `zpool list -Hp -o capacity <pool>`: what the MOTD and `zark status` judge |
| `measured_at` | timestamp | when it was read |

`zfs` and `zpool` account differently (`used_bytes` can exceed
`pool_alloc_bytes`), so neither set can be derived from the other. Readers
compute:

- reserved by ZFS: `max(0, pool_free_bytes - available_bytes)`;
- the total of a usage chart: `used_bytes + available_bytes + reserved`;
- the level: from `capacity_pct` and `thresholds`, as zark does.

### Source pool entry

| Field | Type | Meaning |
|---|---|---|
| `name` | string | `rpool` or `bpool` |
| `space` | Space object or null | null when the pool could not be read this time |
| `level` | `ok`, `warning`, `critical`, or null | from `capacity_pct`; null with `space` |
| `datasets` | array, optional | every dataset of the pool, sorted by name; absent when `space` is null or the listing failed (a `read_error` notice then says so) |

A `datasets` entry, from
`zfs list -Hp -t filesystem,volume -r -o name,type,used,usedbydataset,usedbysnapshots,usedbychildren,usedbyrefreservation <pool>`
in the same pass as `space`:

| Field | Type |
|---|---|
| `name` | string |
| `type` | `filesystem` or `volume` |
| `used_bytes` | integer |
| `usedbydataset_bytes` | integer |
| `usedbysnapshots_bytes` | integer |
| `usedbychildren_bytes` | integer |
| `usedbyrefreservation_bytes` | integer |

A dataset's own share is `usedbydataset + usedbysnapshots +
usedbyrefreservation`. The shares of all datasets do not overlap, so a chart
built on them counts nothing twice. Their sum is a little less than the pool
root's `used_bytes`: the root's `usedbychildren` also holds what ZFS keeps
under the root in directories `zfs list` never shows (`$MOS`, the pool's own
metadata, among them), and a dataset's `usedbychildren` holds the part of its
children's `reservation` they do not use. A reader that shows the whole of
`used_bytes` takes the rest, `used_bytes` minus the sum of the shares, as
pool metadata (and reservations, where any is set). Grouping (homes, system,
other) is the reader's.

A drive entry may carry the same listing for the drive's pool (see "Drive
contents").

### Drive entry

| Field | Type | Meaning |
|---|---|---|
| `name` | string | pool name, the registry key |
| `guid` | string | pool GUID, decimal |
| `drive_id` | string | registry `drive_id`; readers match it against `/dev/disk/by-id` to tell whether the drive is connected |
| `autoeject` | boolean | registry `autoeject` |
| `retention` | object | the effective policy: `window_days`, `max_age_days` (0: off), `margin_pct` |
| `last_backup_at` | timestamp or null | the last backup that completed and was verified; null if none |
| `age_days` | integer or null | whole days since `last_backup_at` at `generated_at`; readers showing a live age compute it from `last_backup_at` |
| `last_backup_point` | string or null | the point of that backup |
| `last_run` | object or null | the last backup run to this drive, whatever its result; null if none recorded |
| `space` | Space object or null | last measured while zark had the drive imported; null if never |
| `datasets` | array, optional | every dataset of the drive's pool (see "Drive contents") |
| `points` | array, optional | every backup point on the drive, oldest first |
| `non_point_snapshots` | object, optional | the drive's other snapshots, counted |
| `retention_next` | object, optional | what the drive's policy would remove at a backup a few days later |
| `reclaim_by_age` | array, optional | per month, what removing every older point zark would remove frees |

`last_run`:

| Field | Type | Meaning |
|---|---|---|
| `command` | `"backup"` | only backup runs are recorded in schema 1 |
| `result` | closed enumeration | see below |
| `started_at` | timestamp | |
| `ended_at` | timestamp | |
| `point` | string or null | the run's point; null if none was taken |
| `datasets_missing` | integer | expected datasets not at the point; non-zero only for `incomplete`. Datasets left out by design (zvols, `zark exclude`) are not expected |
| `message` | string | one line for people, as the terminal said it; empty for `completed` |

`result`:

| Value | When | The drive afterwards |
|---|---|---|
| `completed` | `BACKUP COMPLETED`: every expected dataset at the point, the drive exported and read back | `last_backup_at` and `last_backup_point` updated |
| `incomplete` | `BACKUP INCOMPLETE`: read back, but some datasets did not reach the point | earlier points intact; the next backup resumes or retries |
| `not_verified` | `BACKUP NOT VERIFIED`: the export or the read-back failed | not to be relied on until a backup completes |
| `aborted` | stopped before the first byte was sent: a fatal error or a question declined before replicating, a failed survey, no space, or a signal before the first transfer | unchanged; the point was undone |
| `interrupted` | a signal during or after the transfers, before the result was known; or an orphan `progress.json` found by the next command | partial receives kept for the next run |

A run that ends before a drive is chosen records nothing: `last_run` belongs
to a drive. Only `incomplete`, `not_verified` and `interrupted` call for
attention; `aborted` changed nothing.

### Carried-forward fields

`space`, `last_run` and `last_backup_point` are only known while zark has
the drive imported or at the end of a backup. A writer that did not measure
them (the timer, and any command other than backup; `zark prune` on a
connected drive measures `space` too, with the drive contents) copies them
from the previous `status.json`, matching drives by `guid`, not by name: a drive
prepared again gets a new GUID and starts empty. A drive no longer in the
writer's registry is dropped. A writer whose registry
(`known_drives.json`) cannot be read or parsed leaves `status.json` as it is
and logs a warning that names the cause; a registry that does not exist is
an empty one. If the previous file is unreadable or of another `kind`,
nothing is carried forward and a warning is logged; if it is absent, this is
a first write and nothing is logged.

The drive contents (`datasets`, `points`, `non_point_snapshots`,
`retention_next`, `reclaim_by_age`) are carried with `space` as one group: a writer either
measures them in the same pass as the `space` it writes, or copies them
together with the `space` they were measured with, or leaves them out. It
never pairs them with a `space` measured at another time. A writer that does
not know these fields leaves them out when it rewrites the entry.

### Drive contents

Five optional fields of a drive entry describe what the drive holds, what
its retention policy removes next, and what removing its oldest points
would free. They are measured while zark has the
drive imported: at the end of a backup, after its retention and before the
export, and when `zark prune` works on a connected drive. They are dated by
the `space` they come with (`space.measured_at`) and are never present
without it. Each field may be absent on its own: a writer that could not
read it leaves it out, and writers of schema 1 that predate these fields
never write them.

#### `datasets`

The same listing and fields as a source pool's `datasets`, run on the
drive's pool: names start with the drive's pool name
(`black/rpool/USERDATA/home_abc123`), the pool's root first, every dataset
sorted by name, archived lineages and kept orphans included. As for a
source pool, the own shares do not overlap and sum to a little less than
the drive's `space.used_bytes`: the rest is the drive's pool metadata
(`$MOS` and the other directories `zfs list` never shows, charged to the
root's `usedbychildren`), and, where a dataset on the drive has a
`reservation`, the part of it its children do not use, which stays in
their parent's `usedbychildren`. A reader that shows the whole of
`used_bytes` takes the rest as pool metadata.

#### `points`

One entry per backup point on the drive (snapshots named
`zark_YYYY-MM-DD_HH:MM:SSZ`), oldest first.

| Field | Type | Meaning |
|---|---|---|
| `name` | string | the point |
| `created_at` | timestamp | the `creation` of the point's newest snapshot on the drive: the time retention judges it by, its month included |
| `complete` | boolean | its run reached every expected dataset: the run's mark is on its snapshots (or, on a drive with no mark at all, the legacy rule) |
| `whole` | boolean | complete, and no dataset skipped it since; retention prefers a whole point as its month's point. Never true when `complete` is false |
| `datasets` | integer | how many datasets hold it (an archived lineage counts as its dataset) |
| `used_bytes` | integer | the sum of `used` of its snapshots: what they hold alone |

A snapshot's `used` counts only the blocks no other snapshot shares, so
removing several neighbouring points frees more than the sum of their
`used_bytes`. Readers label the figure as "held by this point alone" and use
`retention_next.reclaim_bytes` or `reclaim_by_age` for what a removal
frees.

#### `non_point_snapshots`

The drive's snapshots that are not backup points, as retention sorts them,
each kind as `{"count": <integer>, "used_bytes": <integer>}`:

- `intermediate`: snapshots that travelled with the points (sanoid's
  `autosnap_*`, another drive's bpool anchors), removed by the policy once
  outside the window;
- `other`: names neither zark nor sanoid wrote (`syncoid_*` from zark 1.x
  among them), which the policy never removes.

#### `retention_next`

What the drive's policy would remove at a complete backup `horizon_days`
after `space.measured_at`. It is the policy run on the drive as measured,
with the clock moved forward; nothing is removed to compute it.

| Field | Type | Meaning |
|---|---|---|
| `horizon_days` | integer | how far the clock was moved; 7 |
| `points` | array of strings | the points of which it would remove at least one snapshot, oldest first |
| `snapshots` | integer | every snapshot it would remove, intermediates included |
| `datasets_whole` | integer | archived lineages and kept orphans it would destroy whole |
| `reclaim_bytes` | integer or null | what ZFS says the removal frees (`zfs destroy -nvp` over those snapshots, as zark measures a removal); null when not measured |

- The figure assumes no other change: the next backup's own point is not in
  it. When `window_days` is at least `horizon_days` it is what that backup's
  retention removes; with a shorter window it is a lower bound.
- After `space.measured_at + horizon_days` it is still a lower bound for the
  next backup's retention, and readers say so.
- It says nothing about the space plan a backup may also propose (the oldest
  monthly points, asked at that backup): that depends on the size of the
  next transfer, which is not known in advance.

#### `reclaim_by_age`

What removing the drive's oldest points frees, month by month, as zark
itself would remove them: the list `zark prune` offers beyond the policy on
a connected drive ("the oldest monthly backup points", each with what it
frees "with the ones above"), which is also what a backup's space plan
offers.

The points it counts, the candidates, are fixed by zark's rules at
`space.measured_at`:

- each month's point that the policy keeps outside the window (the month's
  newest complete point, a whole one preferred), oldest first, so there is
  at most one candidate per month;
- never the drive's newest complete point, and nothing the policy itself
  removes at that time;
- never a point inside the window: no zark command removes those by age,
  so no figure is given for them;
- only a point of which at least one snapshot can go.

Removing a candidate removes its snapshots by exact name, except:

- the newest snapshot of a live dataset (the base of its next incremental);
- a snapshot with a `zfs hold`, or that is the origin of a clone (ZFS
  refuses to destroy either).

An archived lineage or a kept orphan is not live, so its snapshots of the
point go too; one that is left with no snapshot is destroyed whole, and its
`used` counts in the figure. Intermediates (sanoid's) and the history from
before the drive's first backup point are not counted: the policy removes
the first, and `zark prune` offers the second on its own.

One entry per candidate, oldest first. Entry *k* is the removal of the *k*
oldest candidates together:

| Field | Type | Meaning |
|---|---|---|
| `before` | string, `YYYY-MM` | the UTC month after the newest point this removal takes: removing it leaves no candidate created before this month |
| `points` | integer | how many points it removes (*k*) |
| `last_point` | string | the newest point it removes |
| `reclaim_bytes` | integer or null | bytes: what ZFS says the whole removal frees (`zfs destroy -nvp` over its snapshots, one dataset at a time, plus `used` of what goes whole), cumulative; null when not measured |

- The figure is cumulative and only grows from one entry to the next. It is
  at least the sum of what the snapshots it removes hold alone, and usually
  more; a pinned snapshot, or the newest of a live dataset, stays, and its
  `used` is not in the figure.
- ZFS frees destroyed blocks in the background (`zpool get freeing`), so
  `available_bytes` reaches the figure some time after a removal.
- It describes the drive as it was at `space.measured_at`. Any removal, a
  backup, or a month leaving the window changes it; the next measurement
  replaces it, and until then it is carried with `space`, like every drive
  content.
- When a writer stops measuring part way (a time limit), the rest of the
  entries have `reclaim_bytes` null; entries are never left out for that
  reason, so `points` and `last_point` still name every candidate.

#### Reading the drive contents

- Margin: `(space.used_bytes + space.available_bytes) * retention.margin_pct
  / 100`, rounded down: the free space a backup tries to leave on the
  drive, as zark computes it.
- After the policy: `space.available_bytes + retention_next.reclaim_bytes`,
  shown next to the margin; when `reclaim_bytes` is null, nothing is
  computed.
- Points by month: group `points` by the UTC month of `created_at`.
- What removing older points frees: on the row of month M, the entry whose
  `before` is M. A month with no entry gets no figure: it holds the oldest
  point, or a point inside the window, or no candidate. A null
  `reclaim_bytes` is shown as not measured, never as zero.
- Without `reclaim_by_age`, the month table shows only what each month holds
  alone (`points[].used_bytes` summed by month) and says that those figures
  do not add up: removing several months frees more than their sum.
- When all five fields are absent, a reader shows what it shows without
  them: the drive's space bar and its margin. When one is absent, only what
  it feeds is left out (the months without `points`, the next removal
  without `retention_next`, the largest datasets without `datasets`, the
  figure per month without `reclaim_by_age`).

### Notice entry

The lines the MOTD shows, with a level and a code, so a reader picks a
colour and an icon without parsing text.

| Field | Type | Meaning |
|---|---|---|
| `level` | `info`, `warning`, `critical` | as the MOTD colours them: plain, yellow, red |
| `code` | string, open enumeration | below. Generic case: show `text` at `level` |
| `subject` | string or null | the pool or drive it is about, when there is one |
| `text` | string | for people only: the MOTD line without colour codes and without the `zark:` prefix |

| `code` | What | `level` |
|---|---|---|
| `capacity` | a source pool at or above a threshold, one per pool | `warning` or `critical` |
| `backup_age` | days since each drive's last backup | `warning` when the newest is older than `rpo_warning_days`, else `info` |
| `no_anchor` | a drive with no anchor in the source, one per drive | `warning` |
| `read_error` | the source pools, or their datasets, could not be read | `warning` |
| `origin_residues` | snapshots in the source no registered drive needs | `info` |
| `selftest` | a subscribed self-test warning | `warning` |

## Reading the files

### Indicator state

One state for a panel icon, a monitor's header or a script's exit code,
from both files. First match wins:

| # | State | When |
|---|---|---|
| 1 | `waiting` | `progress.json` is `running` or `stalled` and `waiting_for_input` is true |
| 2 | `running` | `progress.json` is `running` or `stalled` |
| 3 | `attention` | `status.json` is readable and has a `critical` notice, or a drive whose `last_run.result` is `incomplete`, `not_verified` or `interrupted` |
| 4 | `edge` | `status.json` has a newer `schema_version`, is unreadable, or is stale; or `progress.json` is `orphan` or `unknown` |
| 5 | `overdue` | the `backup_age` notice has level `warning` |
| 6 | `none` | there is no `status.json` |
| 7 | `ok` | otherwise |

- Stale: `generated_at` more than `thresholds.stale_after_s` seconds before
  the reader's clock. A stale file is still shown, labelled with its date:
  the timer is not installed, or the zark it runs is gone (a stick).
- Unreadable: a `status.json` that exists but does not parse, is not an
  object or has another `kind`. An absent one is state 6, not unreadable.
- An orphan `progress.json` stays until the next zark command or a reboot;
  once a command runs, the drive's `last_run` says `interrupted` and the
  state moves to `attention`.

### Drives and live figures

- A drive is connected when a `/dev/disk/by-id/<drive_id>` link exists.
- A reader never imports a pool: an imported backup pool trips zark's GRUB
  and apt guards and blocks kernel upgrades. A drive's live fill comes from
  `progress.json`'s `drive.space` while zark has it imported, and from the
  last measured `space` otherwise, labelled with its `measured_at`.
- Live figures of the source pools come from running the commands of the
  Space object and `datasets` (`zfs list`, `zpool list`, unprivileged). A
  reader that does so labels them as live and keeps the files' figures
  labelled with their dates.

## For third-party readers

What any program may rely on, and how to read the files safely.

### Guarantees

- Paths: `/run/zark/progress.json` and `/var/lib/zark/status.json`. They do
  not move within schema version 1.
- Within a `schema_version`, fields are only added, and added fields are
  optional; enumerations marked open may gain values, closed ones do not
  (`last_run.result`, a notice's `level`, a source pool's `level`, a
  dataset's `type`). Every other change increments `schema_version`.
- An optional field may be absent from any file: a reader works without it.
  A writer drops the optional fields it does not know when it rewrites an
  entry (an older zark run from a stick, say), so a field present once may
  be gone from the next file.
- Both files are replaced atomically: a reader never sees a partial file.
- Nothing in them is secret, and nothing needs root: files 0644, directories
  0755, and every read the orphan rules need works unprivileged.
- The JSON Schemas in `docs/schema/` describe exactly what a schema 1 writer
  writes. Fixtures for every state, with the state expected for each, are in
  `tests/fixtures/status/`.

### Watching

- Watch the file path, not an open file: every write replaces the file with
  a new one, so a watch on the old inode hears nothing more.
- With GIO, `Gio.File.monitor_file()` on the path does this: GLib's inotify
  backend watches the parent directory for that name, reports a rename into
  place (`IN_MOVED_TO`), and rescans a missing directory every 4 s
  (`gio/inotify/inotify-helper.c`, `inotify-missing.c`). Treat any event as
  "read again"; an absent file after the event means deleted.
- With raw inotify, watch the directory for `IN_MOVED_TO`, `IN_CREATE`,
  `IN_CLOSE_WRITE`, `IN_DELETE` and `IN_MOVED_FROM`, and ignore names that
  start with a dot (the writer's temporary files).
- Polling also works: a `stat` every second, reading again when the inode,
  mtime or size changes.

### Reading

1. Open the path, read it whole, close it, parse it.
2. Check `kind`, then `schema_version`, before any other field.
3. If it is absent, it is absent: for `progress.json` that is "idle". Do not
   retry in a loop.
4. If it does not parse, it is unreadable (state 4); read it again on the
   next event. It should not happen, since writes are atomic.
5. Apply the orphan rules to `progress.json` and the stale rule to
   `status.json` with the reader's own clock, on every redraw, not only when
   a file changes: both depend on time passing.
6. Never parse the people-only strings (`step.label`, `waiting_for`,
   `last_run.message`, a notice's `text`): use `code`, `level`, `result` and
   the numbers.

### What a reader must not do

- Write either file, or create anything in `/run/zark` or `/var/lib/zark`.
- Take or test `zark.lock` or `status.lock`.
- Import, export or otherwise change a pool. Reading `zfs list` and
  `zpool list` is fine.

### Reference reader

`lib/status_reader.py` in zark's source tree applies every rule of this
document: it reads both files as "Reading" says, classifies them with the
orphan, stalled and stale rules, gives the indicator state, and computes the
figures of "Space object", "Source pool entry" (with what the root's
`used_bytes` holds beyond the datasets' shares, `pool_metadata_bytes`) and
"Byte counters". `zark monitor` is built on it.

- It depends on the Python standard library only. Copy the file into your
  program, or import it from an installed zark by putting `/usr/share/zark`
  on the module path (`import lib.status_reader`).
- The names in its `__all__` keep their meaning and signature within schema
  version 1; new names may be added. `read_all(root, now, alive)` gives both
  files and their classes in one call; `root`, `now` and `alive` let a test
  point it at a fixture, fix the clock and decide liveness, as the fixtures'
  README describes.
- It also reads a drive's optional contents, when a writer records them
  (`drive_contents`, `drive_margin_bytes`, `retention_next_is_floor`); a
  file without them gives None.
- It never writes, takes no lock and runs no command. The fixtures in
  `tests/fixtures/status/` with their `expected.json` are its tests, and the
  tests of any other reader.
