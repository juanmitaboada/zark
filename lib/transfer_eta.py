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
A backup's time left, from the run's own pace (M8F-8).

The contract's ETA (bytes left over the last 30 s's rate) holds while one
large dataset is being sent. A daily incremental is mostly small datasets,
each of which costs a few seconds to start and close its send and receive
whatever it moves; between them the 30 s rate falls towards zero and that
ETA jumps from seconds to days (eli, 2026-10-26: 8 s to 6 min within one
run). This model times the run as it goes:

- ``R``, the throughput while bytes are actually moving: bytes over the
  seconds in which ``bytes_done`` grew, leaving out the seconds that moved
  less than a tenth of the best one (a tiny send fills a fraction of its
  second; counted whole, three of them made R a few KiB/s and the first
  ETA 1h02m with 40 s left, eli 2026-10-26, M8F-10);
- ``c``, the fixed cost of one transfer: for every transfer that ended, its
  time less its bytes over ``R``, averaged;
- time left = bytes left over ``R`` + transfers left times ``c`` - the time
  the current transfer has already taken, at most ``c``.

The figure is shown only where the contract allows an ETA (byte counts
measured, an estimate that is not partial, a rate above 0), once two
transfers have ended and once 64 MiB have moved (half the estimate when
that is less), and it is smoothed: it counts down by the time that
passed and moves a third of the way towards each new estimate.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lib import status_reader as sr

MIN_ENDED = 2  # transfers ended before a figure is shown
BLEND = 1 / 3  # how far each new estimate moves the shown one
MIN_MOVED = 64 << 20  # bytes seen moving before R is trusted
SLOW_SHARE = 0.1  # a second below this share of the best one is a partial second


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class TransferEta:  # pylint: disable=too-many-instance-attributes
    """Feed it every reading of ``transfer`` with the reader's clock (seconds,
    any monotonic origin); :meth:`seconds` gives the time left or None."""

    moved: int = 0  # bytes seen moving in this run
    steps: list[tuple[float, int]] = field(default_factory=list)  # (seconds, bytes) per reading
    ended: list[tuple[float, int]] = field(default_factory=list)  # (seconds, bytes) per transfer
    readings: int = 0  # readings fed in this run
    _last_t: float | None = None
    _last_bytes: int | None = None
    _last_done: int | None = None
    _start_t: float | None = None  # when the current transfer started
    _start_bytes: int = 0
    _shown: float | None = None
    _shown_t: float | None = None
    _eta: float | None = None

    def feed(self, now: float, transfer: Mapping[str, Any] | None) -> None:
        """One reading; a null ``transfer`` ends the model's run."""
        if transfer is None:
            self.__dict__.update(TransferEta().__dict__)  # the next run starts from nothing
            return
        self.readings += 1
        done_bytes = _int(transfer.get("bytes_done"))
        done = _int(transfer.get("datasets_done")) or 0
        if self._last_t is not None and done_bytes is not None and self._last_bytes is not None:
            if done_bytes > self._last_bytes:
                self.moved += done_bytes - self._last_bytes
                self.steps.append((now - self._last_t, done_bytes - self._last_bytes))
        if self._last_done is None:
            self._start_t, self._start_bytes = now, done_bytes or 0
        elif done > self._last_done and self._start_t is not None:
            # Every transfer that ended since the last reading shares the time.
            n = done - self._last_done
            span = (now - self._start_t) / n
            moved = max(0, (done_bytes or 0) - self._start_bytes) // n
            self.ended += [(span, moved)] * n
            self._start_t, self._start_bytes = now, done_bytes or 0
        self._last_t, self._last_bytes, self._last_done = now, done_bytes, done
        self._update(now, transfer)

    def _rate(self) -> float | None:
        if not self.steps:
            return None
        best = max(moved / secs for secs, moved in self.steps if secs > 0)
        full = [(secs, moved) for secs, moved in self.steps if moved >= SLOW_SHARE * best * secs]
        secs = sum(s for s, _m in full)
        return sum(m for _s, m in full) / secs if secs > 0 else None

    def _fixed_cost(self, rate: float) -> float:
        costs = [max(0.0, secs - moved / rate) for secs, moved in self.ended]
        return sum(costs) / len(costs) if costs else 0.0

    def _update(self, now: float, transfer: Mapping[str, Any]) -> None:
        rate = self._rate()
        total = _int(transfer.get("bytes_total")) or 0
        if (
            sr.eta_seconds(transfer) is None
            or rate is None
            or len(self.ended) < MIN_ENDED
            or self.moved < min(MIN_MOVED, total // 2)
        ):
            self._eta = None
            return
        done_bytes = _int(transfer.get("bytes_done")) or 0
        left = max(0, (_int(transfer.get("datasets_total")) or 0) - (self._last_done or 0))
        cost = self._fixed_cost(rate)
        running = min(cost, now - self._start_t) if left and self._start_t is not None else 0.0
        raw = max(0.0, max(0, total - done_bytes) / rate + left * cost - running)
        if self._shown is None or self._shown_t is None:
            self._shown = raw
        else:
            counted = max(0.0, self._shown - (now - self._shown_t))
            self._shown = counted + BLEND * (raw - counted)
        self._shown_t = now
        self._eta = self._shown

    def seconds(self) -> float | None:
        """The time left, smoothed; None until the model can say."""
        return self._eta
