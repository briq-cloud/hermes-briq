from __future__ import annotations

import importlib
import json

import pytest
from briq_cloud import BriqError, ExecResult
from fakes import FakeBriq, info, make_sandbox, ok

env_module = importlib.import_module("hermes_briq.environment")
BriqEnvironment = env_module.BriqEnvironment


def make_env(
    client: FakeBriq,
    *,
    persistent: bool = True,
    task_id: str = "t1",
    cwd: str = "~",
    image: str | None = None,
) -> BriqEnvironment:
    return BriqEnvironment(
        cwd=cwd,
        timeout=60,
        task_id=task_id,
        image=image,
        size="medium",
        sleep="1h",
        persistent=persistent,
        client=client,
    )


def run(env: BriqEnvironment, command: str, **kwargs) -> tuple[str, int | None]:
    """One `_run_bash` call, as `(output, exit code)`, whichever process handle Hermes has."""
    handle = env._run_bash(command, **kwargs)
    code = handle.wait(timeout=5)
    output = handle.output if hasattr(handle, "output") else handle.stdout.read()
    return output, code


def last_exec(sandbox) -> str:
    return sandbox.exec.call_args.args[0]


# --- creation ---


def test_creates_a_sandbox_with_the_settings_and_never_sends_env() -> None:
    client = FakeBriq()
    make_env(client, image="python:3.12")
    (call,) = client.created
    assert call["size"] == "medium" and call["image"] == "python:3.12" and call["sleep"] == "1h"
    assert call["name"].startswith("hermes-") and call["idempotency_key"]
    assert set(call) == {"name", "size", "image", "sleep", "idempotency_key"}


def test_a_tilde_cwd_becomes_the_sandbox_home() -> None:
    env = make_env(FakeBriq())
    assert env.cwd == "/home/user"
    assert env._remote_home == "/home/user"


def test_an_absolute_cwd_is_kept() -> None:
    assert make_env(FakeBriq(), cwd="/work").cwd == "/work"


def test_creation_failures_are_named_and_the_degraded_ones_retry() -> None:
    client = FakeBriq()
    client.create_error = BriqError("auth", "the key was refused")
    with pytest.raises(RuntimeError, match="briq: the key was refused") as refused:
        make_env(client)
    assert type(refused.value) is RuntimeError

    client.create_error = BriqError("unavailable", "briq is unreachable")
    with pytest.raises(env_module.EnvironmentConnectionError):
        make_env(client)


# --- persistence ---


def test_a_persistent_sandbox_is_stopped_at_cleanup_and_found_again() -> None:
    client = FakeBriq()
    first = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    first.cleanup()
    sandbox.stop.assert_called_once()
    sandbox.destroy.assert_not_called()

    sandbox.info = info("bq_1", "stopped")
    second = make_env(client)
    assert len(client.created) == 1
    sandbox.start.assert_called_once()
    assert second._sandbox is sandbox


def test_a_recorded_sandbox_that_is_gone_is_replaced() -> None:
    client = FakeBriq()
    make_env(client).cleanup()
    client.sandboxes.clear()
    make_env(client)
    assert len(client.created) == 2


def test_a_recorded_sandbox_in_error_is_not_reused() -> None:
    client = FakeBriq()
    make_env(client).cleanup()
    client.sandboxes["bq_1"].info = info("bq_1", "error")
    make_env(client)
    assert len(client.created) == 2


def test_a_non_persistent_sandbox_is_destroyed_and_never_recorded(tmp_path) -> None:
    client = FakeBriq()
    env = make_env(client, persistent=False)
    sandbox = client.sandboxes["bq_1"]
    env.cleanup()
    sandbox.destroy.assert_called_once()
    sandbox.stop.assert_not_called()
    state = tmp_path / "sandboxes" / "briq-sandboxes.json"
    assert not state.exists() or json.loads(state.read_text()) == {}


def test_only_ids_are_recorded(tmp_path) -> None:
    client = FakeBriq()
    make_env(client, task_id="abc")
    stored = json.loads((tmp_path / "sandboxes" / "briq-sandboxes.json").read_text())
    assert stored == {"abc": "bq_1"}


def test_cleanup_twice_is_harmless() -> None:
    client = FakeBriq()
    env = make_env(client)
    env.cleanup()
    env.cleanup()
    client.sandboxes["bq_1"].stop.assert_called_once()


def test_cleanup_swallows_a_failed_stop() -> None:
    client = FakeBriq()
    env = make_env(client)
    client.sandboxes["bq_1"].stop.side_effect = BriqError("unavailable", "down")
    env.cleanup()


# --- commands ---


def test_a_command_runs_in_bash_with_both_streams_merged() -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    sandbox.exec.side_effect = lambda command, **kw: ok("out\n", 3, "err\n")
    output, code = run(env, "echo hi", login=False, timeout=30)
    assert (output, code) == ("out\nerr\n", 3)
    assert last_exec(sandbox) == "bash -c 'echo hi' 2>&1"
    assert sandbox.exec.call_args.kwargs == {"timeout": 30}


def test_a_login_shell_is_asked_for() -> None:
    client = FakeBriq()
    env = make_env(client)
    run(env, "true", login=True)
    assert last_exec(client.sandboxes["bq_1"]).startswith("bash -l -c ")


def test_a_command_that_outlives_its_timeout_exits_124() -> None:
    client = FakeBriq()
    env = make_env(client)
    client.sandboxes["bq_1"].exec.side_effect = lambda c, **kw: ExecResult(
        stdout="partial", stderr="", exit_code=None, timed_out=True
    )
    assert run(env, "sleep 99", timeout=1) == ("partial", 124)


def test_a_failed_call_is_an_exit_1_with_the_reason() -> None:
    client = FakeBriq()
    env = make_env(client)
    client.sandboxes["bq_1"].exec.side_effect = BriqError("quota", "out of credits")
    output, code = run(env, "ls")
    assert code == 1 and output == "briq: out of credits (quota)\n"


def test_a_long_script_is_written_to_a_file_and_run_from_there() -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    script = "echo " + "x" * 9000
    run(env, script)
    (path, data), kwargs = sandbox.write_file.call_args.args, sandbox.write_file.call_args.kwargs
    assert data == script.encode() and kwargs == {"mode": 0o700}
    command = last_exec(sandbox)
    assert len(command) < 500 and path in command and "rm -f --" in command


def test_stdin_is_staged_privately_and_read_from_the_file() -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    run(env, "cat", stdin_data="secret input")
    (path, data), kwargs = sandbox.write_file.call_args.args, sandbox.write_file.call_args.kwargs
    assert data == b"secret input" and kwargs == {"mode": 0o600}
    assert f"exec 0< {path}" in last_exec(sandbox)


# --- interrupts and recovery ---


def test_an_interrupt_stops_the_sandbox_and_the_next_command_starts_it() -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    handle = env._run_bash("sleep 99")
    handle.wait(timeout=5)
    handle.kill()
    sandbox.stop.assert_called_once()

    sandbox.info = info("bq_1", "stopped")
    env._before_execute()
    sandbox.start.assert_called_once()


def test_a_sandbox_removed_behind_our_back_is_replaced_and_flagged() -> None:
    client = FakeBriq()
    env = make_env(client)
    gone = client.sandboxes.pop("bq_1")
    gone.refresh.side_effect = BriqError("not_found", "gone")
    sessions = env.sessions if hasattr(env, "sessions") else None
    env._before_execute()
    assert len(client.created) == 2 and env._sandbox is client.sandboxes["bq_2"]
    if sessions is not None:
        assert env.recreated == 1 and env.sessions == sessions + 1
    else:
        assert env._recreated_notice_pending


def test_a_refresh_that_cannot_reach_briq_is_a_degraded_backend() -> None:
    client = FakeBriq()
    env = make_env(client)
    client.sandboxes["bq_1"].refresh.side_effect = BriqError("unavailable", "down")
    with pytest.raises(env_module.EnvironmentConnectionError):
        env._before_execute()


# --- files ---


def test_files_go_up_with_write_file_and_are_removed_with_rm(tmp_path) -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    local = tmp_path / "skill.md"
    local.write_bytes(b"hello")
    env._upload(str(local), "/home/user/.hermes/skills/skill.md")
    sandbox.write_file.assert_called_with("/home/user/.hermes/skills/skill.md", b"hello")
    env._delete(["/home/user/.hermes/old"])
    assert "rm" in last_exec(sandbox) and "/home/user/.hermes/old" in last_exec(sandbox)


def test_sync_back_downloads_the_sandbox_hermes_directory_as_a_tar(tmp_path) -> None:
    client = FakeBriq()
    env = make_env(client)
    sandbox = client.sandboxes["bq_1"]
    sandbox.read_file.return_value = b"TAR"
    dest = tmp_path / "back.tar"
    env._bulk_download(dest)
    assert dest.read_bytes() == b"TAR"
    tar_call = next(c.args[0] for c in sandbox.exec.call_args_list if "tar cf" in str(c.args[0]))
    assert "-C / home/user/.hermes" in tar_call and "--exclude='*.sock'" in tar_call


def test_the_double_is_a_real_sandbox_shape() -> None:
    """A guard on the fakes: they follow the client's own method names."""
    sandbox = make_sandbox()
    for name in ("exec", "write_file", "read_file", "stop", "start", "destroy", "refresh"):
        assert callable(getattr(sandbox, name))
