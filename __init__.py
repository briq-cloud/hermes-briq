"""briq: a Hermes terminal backend that runs commands in a briq sandbox.

Select it with `terminal.backend: briq`. The key comes from `BRIQ_API_KEY`. See README.md.
"""

from __future__ import annotations

from .provider import BriqProvider


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(BriqProvider())
