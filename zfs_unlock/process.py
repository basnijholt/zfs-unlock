"""Process runner abstractions."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .constants import COMMAND_STARTUP_ERROR_RETURNCODE, COMMAND_TIMEOUT_RETURNCODE

# After kill() the child is dead, but communicate() only returns at pipe EOF,
# which a grandchild (e.g. an ssh ProxyCommand helper) can hold open long past
# any configured command timeout. Bound the drain and fall back to wait().
_KILL_DRAIN_TIMEOUT = 5.0


async def _drain_killed_process(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Collect output from a killed child without blocking on inherited pipes."""
    try:
        return await asyncio.wait_for(process.communicate(), timeout=_KILL_DRAIN_TIMEOUT)
    except TimeoutError:
        await process.wait()
        return b"", b""


@dataclass(frozen=True)
class CommandResult:
    """Result from a local command runner."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Async command runner protocol."""

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and return its result."""


class SubprocessRunner:
    """Run commands through asyncio subprocesses."""

    async def run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        command_timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and capture stdout/stderr."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if input_text is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return CommandResult(returncode=COMMAND_STARTUP_ERROR_RETURNCODE, stdout="", stderr=f"{exc}\n")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(None if input_text is None else input_text.encode()),
                timeout=command_timeout,
            )
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            stdout, stderr = await _drain_killed_process(process)
            return CommandResult(
                returncode=COMMAND_TIMEOUT_RETURNCODE,
                stdout=stdout.decode(errors="replace"),
                stderr=f"command timed out after {command_timeout:g}s\n{stderr.decode(errors='replace')}",
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await _drain_killed_process(process)
            raise
        returncode = process.returncode if process.returncode is not None else 1
        return CommandResult(
            returncode=returncode,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )


class LocalCommandRunner(Protocol):
    """Synchronous local command runner protocol for the receiver."""

    def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a local command and return its result."""


class LocalSubprocessRunner:
    """Run local receiver commands through subprocess."""

    def run(self, args: list[str], *, input_text: str | None = None) -> CommandResult:
        """Run a local command and capture stdout/stderr."""
        # UTF-8 with replacement, matching SubprocessRunner: the root receiver
        # must degrade gracefully on non-UTF-8 child output, never traceback.
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def run_process(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local process and return the completed process."""
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=check)
