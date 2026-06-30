"""Process runner abstractions."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .constants import COMMAND_STARTUP_ERROR_RETURNCODE, COMMAND_TIMEOUT_RETURNCODE


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
            stdout, stderr = await process.communicate()
            return CommandResult(
                returncode=COMMAND_TIMEOUT_RETURNCODE,
                stdout=stdout.decode(errors="replace"),
                stderr=f"command timed out after {command_timeout:g}s\n{stderr.decode(errors='replace')}",
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.communicate()
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
        result = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def run_process(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local process and return the completed process."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)
