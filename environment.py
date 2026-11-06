"""A Hermes execution environment on a briq sandbox.

Each command is one `exec` call to the sandbox. The session snapshot, the working directory and
the exported variables are kept by `BaseEnvironment`, as for every other backend. With
`container_persistent` the sandbox is stopped at cleanup (its disk stays, nothing is billed) and
found again by the id recorded for the task; otherwise it is destroyed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import threading
import uuid
from pathlib import Path
from typing import Any

from briq_cloud import Briq, BriqError
from tools.environments.base import BaseEnvironment, EnvironmentConnectionError, get_sandbox_dir
from tools.environments.base_output import _ThreadedProcessHandle
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_rm_command,
)

logger = logging.getLogger(__name__)

#: briq takes a command and its directory up to 8,192 bytes. A longer script is written to a file.
_INLINE_LIMIT = 6000
_STATE_FILE = "briq-sandboxes.json"
#: States in which a recorded sandbox can be used again. `sleeping` wakes on the next call.
_REUSABLE = {"running", "sleeping", "starting", "stopped", "stopping"}
_TRANSIENT = {"unavailable", "timeout", "outcome_unknown"}


def new_client(key: str, base_url: str | None) -> Briq:
    return Briq(api_key=key, base_url=base_url)


class _IdStore:
    """`task -> sandbox id`, in a small file under Hermes' sandbox directory.

    Only ids are kept here, never a key. A damaged file counts as empty.
    """

    def __init__(self) -> None:
        self._path = get_sandbox_dir() / _STATE_FILE

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return (
            {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
        )

    def get(self, key: str) -> str | None:
        return self._read().get(key)

    def put(self, key: str, sandbox_id: str | None) -> None:
        data = self._read()
        if sandbox_id is None:
            data.pop(key, None)
        else:
            data[key] = sandbox_id
        tmp = self._path.with_name(f"{_STATE_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


def _failure(error: BriqError) -> RuntimeError:
    """The error a failed briq call becomes. A transient one marks the backend degraded."""
    message = f"briq: {error}"
    if error.code in _TRANSIENT:
        return EnvironmentConnectionError(message)
    return RuntimeError(message)


class BriqEnvironment(BaseEnvironment):
    """Commands run in a briq sandbox, one `exec` call each."""

    _stdin_mode = "payload"
    # A sandbox that slept needs a moment to wake.
    _snapshot_timeout = 60

    def __init__(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str,
        image: str | None,
        size: str,
        sleep: str,
        persistent: bool,
        client: Briq,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self._client, self._task_id = client, task_id
        self._image, self._size, self._sleep, self._persistent = image, size, sleep, persistent
        self._lock = threading.RLock()
        self._store = _IdStore()
        self._store_key = task_id
        self._sandbox: Any = None
        self._remote_home = "/root"
        self._start_cwd = cwd

        try:
            self._sandbox = self._reuse() or self._create()
        except BriqError as error:
            raise _failure(error) from error
        self._resolve_home()
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._upload,
            delete_fn=self._delete,
            bulk_download_fn=self._bulk_download,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    # --- sandbox lifecycle ---

    def _reuse(self) -> Any:
        """The sandbox recorded for this task, started again, or None."""
        if not self._persistent:
            return None
        sandbox_id = self._store.get(self._store_key)
        if not sandbox_id:
            return None
        try:
            sandbox = self._client.sandbox(sandbox_id)
        except BriqError as error:
            if error.code != "not_found":
                raise
            self._store.put(self._store_key, None)
            return None
        if sandbox.info.state not in _REUSABLE:
            return None
        if sandbox.info.state in {"stopped", "stopping"}:
            sandbox.start()
        logger.info("briq: resumed sandbox %s for task %s", sandbox.id, self._task_id)
        return sandbox

    def _create(self) -> Any:
        sandbox = self._client.create_sandbox(
            name=f"hermes-{uuid.uuid4().hex[:8]}",
            size=self._size,
            image=self._image,
            sleep=self._sleep,
            idempotency_key=uuid.uuid4().hex,
        )
        if self._persistent:
            self._store.put(self._store_key, sandbox.id)
        logger.info("briq: created sandbox %s for task %s", sandbox.id, self._task_id)
        return sandbox

    def _resolve_home(self) -> None:
        with contextlib.suppress(Exception):
            home = self._sandbox.exec("echo $HOME").stdout.strip()
            if home.startswith("/"):
                self._remote_home = home
        if self._start_cwd in {"~", ""}:
            self.cwd = self._remote_home
        logger.info("briq: home %s, cwd %s", self._remote_home, self.cwd)

    def _ensure_ready(self) -> bool:
        """Start the sandbox if it was stopped (an interrupt does that); replace it if it is gone.

        Returns True when it made a new one, which then needs its home, files and shell session.
        """
        try:
            state = self._sandbox.refresh().state
        except BriqError as error:
            if error.code != "not_found":
                raise _failure(error) from error
            return self._replace_sandbox()
        if state in {"stopped", "stopping"}:
            try:
                self._sandbox.start()
            except BriqError as error:
                raise _failure(error) from error
            logger.info("briq: restarted sandbox %s", self._sandbox.id)
        elif state not in _REUSABLE:
            return self._replace_sandbox()
        return False

    def _replace_sandbox(self) -> bool:
        """The sandbox was removed behind our back (or failed): start over and say so."""
        logger.warning("briq: sandbox %s is gone, creating a new one", self._sandbox.id)
        try:
            self._sandbox = self._create()
        except BriqError as error:
            raise _failure(error) from error
        self._mark_recreated()
        return True

    def _before_execute(self) -> None:
        with self._lock:
            replaced = self._ensure_ready()
        if replaced:
            # Not under the lock: the session snapshot runs a command, and a command takes it.
            self._resolve_home()
            self._sync_manager.sync(force=True)
            self.init_session()
        self._sync_manager.sync()

    # --- file transfer (the sync manager's transport) ---

    def _upload(self, host_path: str, remote_path: str) -> None:
        self._sandbox.write_file(remote_path, Path(host_path).read_bytes())

    def _delete(self, remote_paths: list[str]) -> None:
        self._sandbox.exec(quoted_rm_command(remote_paths))

    def _bulk_download(self, dest: Path) -> None:
        """The sandbox's `.hermes/` as one tar, written to `dest`."""
        base = f"{self._remote_home}/.hermes".lstrip("/")
        remote_tar = f"{self.get_temp_dir()}/.hermes_sync.{os.getpid()}.tar"
        # Live sockets cannot be archived and must not fail the download.
        self._sandbox.exec(
            f"tar cf {shlex.quote(remote_tar)} --exclude='*.sock' -C / {shlex.quote(base)}"
        )
        dest.write_bytes(self._sandbox.read_file(remote_tar))
        with contextlib.suppress(Exception):
            self._sandbox.exec(f"rm -f {shlex.quote(remote_tar)}")

    # --- commands ---

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        sandbox, lock = self._sandbox, self._lock
        # Guarded by `lock`, so cancel() and the dispatch agree on whether the shell has taken the
        # staged files (it opens and removes them before the command runs).
        state: dict[str, Any] = {"cancelled": False, "staged": [], "dispatched": False}

        def stage(data: str, mode: int = 0o600) -> str:
            path = f"{self.get_temp_dir().rstrip('/')}/.hermes-{uuid.uuid4().hex}"
            sandbox.write_file(path, data.encode("utf-8", "surrogateescape"), mode=mode)
            with lock:
                state["staged"].append(path)
            return path

        def scrub() -> None:  # the caller holds `lock`
            leftovers, state["staged"] = state["staged"], []
            if leftovers and not state["dispatched"]:
                with contextlib.suppress(Exception):
                    sandbox.exec(quoted_rm_command(leftovers))

        def cancel() -> None:
            # briq cannot interrupt one call, so the sandbox is stopped (disk kept); the next
            # command starts it again.
            with lock:
                state["cancelled"] = True
                scrub()
                with contextlib.suppress(Exception):
                    sandbox.stop()

        def exec_fn() -> tuple[str, int]:
            flags = "-l " if login else ""
            command = cmd_string
            try:
                if stdin_data:  # empty stdin is no stdin, as on every other backend
                    with lock:
                        if state["cancelled"]:
                            return ("", 130)
                    command = self._redirect_stdin_from_file(cmd_string, stage(stdin_data))
                shell = f"bash {flags}-c {shlex.quote(command)} 2>&1"
                if len(shell.encode("utf-8", "surrogateescape")) > _INLINE_LIMIT:
                    script = stage(command, mode=0o700)
                    quoted = shlex.quote(script)
                    shell = f"{{ bash {flags}{quoted}; rc=$?; rm -f -- {quoted}; exit $rc; }} 2>&1"
                with lock:
                    if state["cancelled"]:
                        scrub()
                        return ("", 130)
                    state["dispatched"] = True
                result = sandbox.exec(shell, timeout=timeout)
            except BriqError as error:
                with lock:
                    if state["cancelled"]:
                        return ("", 130)
                    scrub()
                return (f"briq: {error}\n", 1)
            if result.timed_out:
                return (result.stdout + result.stderr, 124)
            return (
                result.stdout + result.stderr,
                result.exit_code if result.exit_code is not None else 1,
            )

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    # --- cleanup ---

    def cleanup(self) -> None:
        with self._lock:
            sandbox = self._sandbox
            if sandbox is None:
                return
            logger.info("briq: syncing files from sandbox...")
            try:
                self._sync_manager.sync_back()
            except Exception as error:
                logger.warning("briq: sync_back failed: %s", error)
            self._sandbox = None
            try:
                if self._persistent:
                    sandbox.stop()
                    logger.info("briq: stopped sandbox %s (disk kept)", sandbox.id)
                else:
                    sandbox.destroy()
                    self._store.put(self._store_key, None)
                    logger.info("briq: destroyed sandbox %s", sandbox.id)
            except Exception as error:
                logger.warning("briq: cleanup failed: %s", error)
