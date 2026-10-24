"""Hosted, Telegram-first service mode (D-091…D-096).

The service shares the adapters, matching engine, pacing, scanner and notifier with the CLI. What
it adds is a per-user layer (`store`), a chat conversation (`conversation`), per-user evaluation of
shared scans (`notify`) and the Telegram plumbing to run continuously (`bot`, `app`).
"""

from __future__ import annotations

__all__ = ["app", "bot", "conversation", "notify", "packs", "store"]
