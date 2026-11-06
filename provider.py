"""The `briq` terminal backend provider: classification, health checks and environment creation."""

from __future__ import annotations

import importlib.util
import logging
from typing import Any

from agent.secret_scope import get_secret
from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

KEY_ENV = "BRIQ_API_KEY"
URL_ENV = "BRIQ_API_URL"
SIZES = ("small", "medium", "large", "xlarge")
DEFAULT_SIZE = "medium"
DEFAULT_SLEEP = "1h"
_KEYS_URL = "https://app.briq.cloud"


def _terminal_config() -> dict:
    from hermes_cli.config import load_config_readonly

    terminal = load_config_readonly().get("terminal")
    return terminal if isinstance(terminal, dict) else {}


def _setting(key: str, default: str = "") -> str:
    """`terminal.<key>` from config.yaml, else `TERMINAL_<KEY>` (environment or profile .env)."""
    value = _terminal_config().get(key)
    if value is None or not str(value).strip():
        from hermes_cli.config import get_env_value

        value = get_env_value(f"TERMINAL_{key.upper()}")
    return str(value or "").strip() or default


def configured_image() -> str:
    """`terminal.briq_image`; empty means briq's default sandbox image."""
    return _setting("briq_image")


def configured_size() -> str:
    return _setting("briq_size", DEFAULT_SIZE)


def configured_sleep() -> str:
    return _setting("briq_sleep", DEFAULT_SLEEP)


def api_key() -> str | None:
    return get_secret(KEY_ENV) or None


def api_url() -> str | None:
    return get_secret(URL_ENV) or None


def client_available() -> bool:
    return importlib.util.find_spec("briq_cloud") is not None


class BriqProvider(TerminalEnvironmentProvider):
    """briq: a sandbox microVM with its own disk. It is not this machine and holds no host path."""

    is_remote = True
    is_container = True
    # No `exec_prefix`: briq has no client that opens a command with stdin and stdout kept open.
    supports_streams = False

    @property
    def name(self) -> str:
        return "briq"

    @property
    def display_name(self) -> str:
        return "briq"

    @property
    def description(self) -> str:
        return "A briq sandbox: a microVM with its own disk. It sleeps when idle."

    @property
    def env_description(self) -> str:
        return "a briq sandbox (Linux)"

    @property
    def default_cwd(self) -> str:
        # Each sandbox resolves its own home once it is up.
        return "~"

    @property
    def cache_path_base(self) -> str:
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        return frozenset({KEY_ENV})

    def is_available(self) -> bool:
        # No network: this runs while the UI paints.
        return client_available() and bool(api_key())

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if not client_available():
            logger.error(
                "briq backend selected but the briq-cloud package is not installed "
                "(pip install briq-cloud)"
            )
            return False
        if not api_key():
            logger.error(
                "briq backend selected but %s is not set. Create a key at %s and run "
                "`hermes config set %s <key>`",
                KEY_ENV,
                _KEYS_URL,
                KEY_ENV,
            )
            return False
        size = configured_size()
        if size not in SIZES:
            logger.error("terminal.briq_size is %r; it must be one of %s", size, ", ".join(SIZES))
            return False
        return True

    def probe(self) -> tuple[str, str]:
        if not client_available():
            return ("unavailable", "The briq-cloud package is not installed.")
        if not api_key():
            return ("needs_setup", f"{KEY_ENV} is not set.")
        return ("ready", "")

    def setup_instructions(self) -> list[str]:
        return [
            f"Create an API key at {_KEYS_URL} and set it: `hermes config set {KEY_ENV} <key>`.",
            f"Size: {configured_size()} (one of {', '.join(SIZES)}; "
            "change it with `hermes config set terminal.briq_size <size>`).",
            "Image: "
            + (configured_image() or "briq's default sandbox image")
            + " (change it with `hermes config set terminal.briq_image <image>`).",
        ]

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        has_client, has_key = client_available(), bool(api_key())
        return [
            (
                has_client,
                "briq-cloud package",
                "(installed)" if has_client else "(missing; pip install briq-cloud)",
            ),
            (
                has_key,
                f"briq {KEY_ENV}",
                "(set)" if has_key else f"(not set; create one at {_KEYS_URL})",
            ),
        ]

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: str | None = None,
        container_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        from .environment import BriqEnvironment, new_client

        size = configured_size()
        if size not in SIZES:
            raise ValueError(
                f"terminal.briq_size is {size!r}; it must be one of {', '.join(SIZES)}"
            )
        key = api_key()
        if not key:
            raise RuntimeError(f"{KEY_ENV} is not set")
        cc = container_config or {}
        return BriqEnvironment(
            cwd=cwd,
            timeout=timeout,
            task_id=task_id,
            image=(image or "").strip() or configured_image() or None,
            size=size,
            sleep=configured_sleep(),
            persistent=bool(cc.get("container_persistent", True)),
            client=new_client(key, api_url()),
        )
