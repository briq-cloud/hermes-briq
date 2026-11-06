"""A briq client double. Its sandboxes are autospecced on the real `briq_cloud.Sandbox`, so a call
the real class would refuse (a wrong name, a wrong argument) fails here too."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

from briq_cloud import BriqError, ExecResult, Sandbox
from briq_cloud._models import SandboxInfo


def info(sandbox_id: str, state: str = "running") -> SandboxInfo:
    return SandboxInfo(
        id=sandbox_id,
        name="hermes",
        image="img",
        size="medium",
        state=state,
        sleep="1h",
        created_at=datetime.now(UTC),
        sleeps_at=None,
        cost_cents_so_far=0.0,
        error=None,
    )


def ok(stdout: str = "", exit_code: int = 0, stderr: str = "") -> ExecResult:
    return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)


def make_sandbox(sandbox_id: str = "bq_1", state: str = "running", home: str = "/home/user") -> Any:
    sandbox = create_autospec(Sandbox, instance=True)
    sandbox.id = sandbox_id
    sandbox.info = info(sandbox_id, state)

    def refresh() -> SandboxInfo:
        return sandbox.info

    def exec_(command: Any, **kwargs: Any) -> ExecResult:
        return ok(home + "\n") if command == "echo $HOME" else ok("")

    sandbox.refresh.side_effect = refresh
    sandbox.exec.side_effect = exec_
    return sandbox


class FakeBriq:
    """`create_sandbox` makes numbered sandboxes; `sandbox(id)` finds one made or added before."""

    def __init__(self) -> None:
        self.sandboxes: dict[str, Any] = {}
        self.created: list[dict[str, Any]] = []
        self.create_error: BriqError | None = None

    def add(self, sandbox: Any) -> Any:
        self.sandboxes[sandbox.id] = sandbox
        return sandbox

    def create_sandbox(self, **kwargs: Any) -> Any:
        if self.create_error:
            raise self.create_error
        self.created.append(kwargs)
        return self.add(make_sandbox(f"bq_{len(self.created)}"))

    def sandbox(self, sandbox_id: str) -> Any:
        if sandbox_id not in self.sandboxes:
            raise BriqError("not_found", "no such sandbox")
        return self.sandboxes[sandbox_id]


def ctx() -> SimpleNamespace:
    registered: list[Any] = []
    return SimpleNamespace(
        registered=registered,
        register_terminal_environment_provider=registered.append,
    )
