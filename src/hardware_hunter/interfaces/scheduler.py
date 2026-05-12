"""``Scheduler`` ABC — Story 3.2 (FR8, NFR-I1).

The port through which the poll loop registers per-marketplace polling
jobs. The v1 concrete implementation is ``adapters/hermes_scheduler``
which wraps the running Hermes service's scheduler primitive (Hermes
runs as a remote Proxmox VM service — not embedded as a Python lib).

A pure in-process implementation may be added later for tests that
don't want to talk to the real Hermes, but the contract here is what
the orchestration layer composes against either way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

#: A registered job: a no-arg async callable that runs one cycle of work.
SchedulerTask = Callable[[], Awaitable[None]]


class Scheduler(ABC):
    """Port for periodic-job registration + lifecycle."""

    @abstractmethod
    async def register(
        self,
        job_name: str,
        cadence_minutes: int,
        task: SchedulerTask,
    ) -> None:
        """Register ``task`` to run every ``cadence_minutes`` minutes.

        ``job_name`` is the human-readable label that surfaces in
        ``health`` output and the ``job_started`` / ``job_finished``
        structured-log records.

        Calling ``register`` after ``shutdown`` is undefined; concrete
        adapters may either no-op or raise.
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Stop accepting new work and let in-flight jobs drain.

        SIGTERM handling in the daemon entry point (Story 4.8) calls
        this with a bounded timeout per FR50.
        """


class SchedulerError(RuntimeError):
    """The scheduler could not register or run a job. Cause in ``__cause__``."""
