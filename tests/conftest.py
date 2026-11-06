"""Loads the plugin as Hermes does (a package named after its directory) and, when Hermes itself
is not importable, stands in for the few core modules it imports.

With a Hermes checkout on `PYTHONPATH` the tests run against the real classes; without one the
stand-ins below keep the same names and signatures, so the suite runs anywhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
# `python -m pytest` from the plugin directory puts it on `sys.path`, where `environment.py` could
# shadow a module of Hermes.
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PLUGIN_DIR]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _have_hermes() -> bool:
    try:
        return importlib.util.find_spec("tools.environments.base") is not None
    except (ImportError, ValueError):
        return False


HERMES_IS_REAL = _have_hermes()


def _stub(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _install_stand_ins() -> None:
    import abc
    import shlex

    class ProviderBase(abc.ABC):
        @property
        @abc.abstractmethod
        def name(self) -> str: ...

        @property
        def display_name(self) -> str:
            return self.name

    class TerminalEnvironmentProvider(ProviderBase):
        is_remote = True
        is_container = True
        supports_streams = False

        @abc.abstractmethod
        def is_available(self) -> bool: ...

        @abc.abstractmethod
        def create_environment(self, **kwargs: object): ...

    class EnvironmentConnectionError(RuntimeError):
        def __init__(self, reason: str, *, retry_hint: str = ""):
            super().__init__(reason)

    class BaseEnvironment:
        _stdin_mode = "pipe"

        def __init__(
            self, cwd: str, timeout: int, env: dict | None = None, persist_env: bool = True
        ):
            self.cwd, self.timeout = cwd, timeout
            self.recreated = 0
            self.sessions = 0

        def get_temp_dir(self) -> str:
            return "/tmp"

        def init_session(self) -> None:
            self.sessions += 1

        def _mark_recreated(self) -> None:
            self.recreated += 1

        @staticmethod
        def _redirect_stdin_from_file(command: str, path: str) -> str:
            quoted = shlex.quote(path)
            return f"exec 0< {quoted} || exit $?\nrm -f -- {quoted} || exit $?\n{command}"

    class _ThreadedProcessHandle:
        def __init__(self, exec_fn, cancel_fn=None):
            self._cancel_fn, self.returncode, self.output = cancel_fn, None, ""
            self._done = threading.Event()

            def work() -> None:
                self.output, self.returncode = exec_fn()
                self._done.set()

            threading.Thread(target=work, daemon=True).start()

        def wait(self, timeout: float | None = None) -> int | None:
            self._done.wait(timeout=timeout)
            return self.returncode

        def kill(self) -> None:
            if self._cancel_fn:
                self._cancel_fn()

    class FileSyncManager:
        def __init__(self, get_files_fn, upload_fn, delete_fn, **kwargs):
            self.upload_fn, self.delete_fn = upload_fn, delete_fn
            self.synced = self.pulled = 0

        def sync(self, *, force: bool = False) -> None:
            self.synced += 1

        def sync_back(self) -> None:
            self.pulled += 1

    for package in ("agent", "tools", "tools.environments", "hermes_cli"):
        _stub(package).__path__ = []  # type: ignore[attr-defined]
    _stub("agent.provider_base", ProviderBase=ProviderBase)
    _stub("agent.terminal_env_provider", TerminalEnvironmentProvider=TerminalEnvironmentProvider)
    _stub("agent.secret_scope", get_secret=lambda name, default=None: os.environ.get(name, default))
    _stub(
        "tools.environments.base",
        BaseEnvironment=BaseEnvironment,
        EnvironmentConnectionError=EnvironmentConnectionError,
        get_sandbox_dir=lambda: Path(os.environ["TERMINAL_SANDBOX_DIR"]),
    )
    _stub("tools.environments.base_output", _ThreadedProcessHandle=_ThreadedProcessHandle)
    _stub(
        "tools.environments.file_sync",
        FileSyncManager=FileSyncManager,
        iter_sync_files=lambda base="/root/.hermes": [],
        quoted_rm_command=lambda paths: "rm -f " + " ".join(shlex.quote(p) for p in paths),
    )
    _stub(
        "hermes_cli.config",
        load_config_readonly=lambda: {},
        get_env_value=lambda name: os.environ.get(name),
    )


if not HERMES_IS_REAL:
    _install_stand_ins()


def _load_plugin() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "hermes_briq", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_briq"] = module
    spec.loader.exec_module(module)
    return module


_load_plugin()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No key, no config, and a sandbox directory of its own."""
    for name in (
        "BRIQ_API_KEY",
        "BRIQ_API_URL",
        "TERMINAL_BRIQ_IMAGE",
        "TERMINAL_BRIQ_SIZE",
        "TERMINAL_BRIQ_SLEEP",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(tmp_path / "sandboxes"))
    (tmp_path / "sandboxes").mkdir()
    provider = importlib.import_module("hermes_briq.provider")
    monkeypatch.setattr(provider, "_terminal_config", lambda: {})


class _PluginDirIsNotATestPackage:
    """The plugin directory is itself a package (`__init__.py` with relative imports), so pytest
    would collect it as a test package and import that file on its own, where the relative imports
    cannot resolve. Collect it as a plain directory."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == PLUGIN_DIR:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_PluginDirIsNotATestPackage(), "briq-plugin-dir")
