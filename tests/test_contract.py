"""Against a real Hermes: the environment through `BaseEnvironment.execute`, with a sandbox double
that runs each command in a local bash. Skipped when no Hermes checkout is on `PYTHONPATH`."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path

import pytest
from conftest import HERMES_IS_REAL
from fakes import FakeBriq, make_sandbox, ok

pytestmark = pytest.mark.skipif(not HERMES_IS_REAL, reason="needs a Hermes checkout on PYTHONPATH")

env_module = importlib.import_module("hermes_briq.environment")


class LocalBashSandbox:
    """Just enough of a sandbox: `exec` runs bash on this machine, `write_file` writes here."""

    def __init__(self, home: Path):
        self.id, self.home = "bq_local", home
        self.stopped = False

    def exec(self, command, *, timeout=60.0, **kwargs):
        done = subprocess.run(
            ["bash", "-c", command] if isinstance(command, str) else list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(self.home)},
            check=False,
        )
        return ok(done.stdout, done.returncode, done.stderr)

    def write_file(self, path, data, *, mode=None):
        Path(path).write_bytes(data)
        if mode:
            Path(path).chmod(mode)

    def read_file(self, path):
        return Path(path).read_bytes()

    def refresh(self):
        return make_sandbox().info

    def stop(self):
        self.stopped = True

    def destroy(self):
        pass


@pytest.fixture
def env(tmp_path):
    client = FakeBriq()
    client.add(sandbox := LocalBashSandbox(tmp_path))
    client.create_sandbox = lambda **kw: sandbox  # type: ignore[method-assign]
    created = env_module.BriqEnvironment(
        cwd=str(tmp_path),
        timeout=30,
        task_id="contract",
        image=None,
        size="medium",
        sleep="1h",
        persistent=False,
        client=client,
    )
    yield created
    for leftover in (created._snapshot_path, created._cwd_file):
        Path(leftover).unlink(missing_ok=True)


def test_output_exit_code_and_directory_follow_the_command(env, tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    first = env.execute("cd sub && echo hello")
    assert first["output"].strip() == "hello" and first["returncode"] == 0
    assert env.cwd == str(tmp_path / "sub")
    assert env.execute("pwd")["output"].strip() == str(tmp_path / "sub")
    assert env.execute("exit 7")["returncode"] == 7


def test_exported_variables_and_stderr_carry_over(env) -> None:
    env.execute("export BRIQ_TEST_VALUE=kept")
    result = env.execute('echo "$BRIQ_TEST_VALUE"; echo oops >&2')
    assert "kept" in result["output"] and "oops" in result["output"]


def test_stdin_reaches_the_command(env) -> None:
    assert env.execute("cat", stdin_data="from stdin").get("output", "").strip() == "from stdin"


def test_hermes_accepts_the_registration_and_builds_the_environment(tmp_path, monkeypatch) -> None:
    """`register()` against Hermes' own registry, then Hermes' own factory."""
    from agent import terminal_env_registry as registry
    from hermes_briq import register
    from tools.terminal_tool_backends import _create_environment

    class Context:
        def register_terminal_environment_provider(self, provider):
            registry.register_provider(provider)

    register(Context())
    assert "briq" in registry.plugin_backend_names()
    assert registry.provider_flag("briq", "default_cwd") == "~"
    assert "BRIQ_API_KEY" in registry.plugin_strip_env_keys()

    client = FakeBriq()
    client.add(sandbox := LocalBashSandbox(tmp_path))
    client.create_sandbox = lambda **kw: sandbox  # type: ignore[method-assign]
    monkeypatch.setenv("BRIQ_API_KEY", "bk_test")
    monkeypatch.setattr(env_module, "new_client", lambda key, url: client)
    env = _create_environment(
        "briq", "", "~", 30, container_config={"container_persistent": False}, task_id="factory"
    )
    try:
        assert env.cwd == str(tmp_path) and env.execute("echo ok")["output"].strip() == "ok"
    finally:
        for leftover in (env._snapshot_path, env._cwd_file):
            Path(leftover).unlink(missing_ok=True)
