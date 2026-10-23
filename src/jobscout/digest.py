"""One-off digest of the roles that already match, for the first usable session.

This is deliberately *not* part of the alert pipeline. Baselined roles never generate a
notification row (that is what stops a launch flood), so a new user would otherwise wait
days for the first message. The digest reads the current matches and sends them once,
without writing to the database, so deduplication, baselining, and partial-scan safety are
untouched: a role listed here can still never be alerted twice, because it is not queued
at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import StoredJob

# Telegram rejects messages over 4096 characters; leave room for the header.
MESSAGE_CHAR_LIMIT = 3500


@dataclass(frozen=True)
class DigestMessage:
    """One Telegram-ready message for one source."""

    source_id: str
    employer: str
    role_count: int
    text: str


def build_digest(
    entries: list[tuple[str, str, list[StoredJob]]],
    *,
    generated_at: str,
) -> list[DigestMessage]:
    """Format one message per source, splitting when a source has too many roles."""
    messages: list[DigestMessage] = []
    for source_id, employer, jobs in entries:
        if not jobs:
            continue
        header = (
            f"Job-Scout: {len(jobs)} matching role(s) at {employer}\n"
            f"Already known as of {generated_at} - these are current, not new."
        )
        for index, chunk in enumerate(_chunks(jobs, header)):
            continuation = f" (continued {index + 1})" if index else ""
            body = "\n\n".join(_format_role(position, job) for position, job in chunk)
            messages.append(
                DigestMessage(
                    source_id=source_id,
                    employer=employer,
                    role_count=len(jobs),
                    text=f"{header}{continuation}\n\n{body}",
                )
            )
    return messages


def _chunks(jobs: list[StoredJob], header: str) -> list[list[tuple[int, StoredJob]]]:
    """Greedily pack formatted roles into messages that stay under the Telegram limit."""
    chunks: list[list[tuple[int, StoredJob]]] = []
    current: list[tuple[int, StoredJob]] = []
    size = len(header)
    for position, job in enumerate(jobs, start=1):
        block = _format_role(position, job)
        if current and size + len(block) + 2 > MESSAGE_CHAR_LIMIT:
            chunks.append(current)
            current = []
            size = len(header) + len(" (continued 9)")
        current.append((position, job))
        size += len(block) + 2
    if current:
        chunks.append(current)
    return chunks


def _format_role(position: int, job: StoredJob) -> str:
    """Apply link first, then the essentials. Missing detail is omitted, never empty."""
    job_view = job.job
    lines = [f"{position}. {job_view.title.strip()}"]
    detail = [part for part in (job_view.location_raw.strip(),) if part]
    if detail:
        lines.append(f"   {' - '.join(detail)}")
    lines.append(f"   {job_view.url.strip()}")
    return "\n".join(lines)


__all__ = ["MESSAGE_CHAR_LIMIT", "DigestMessage", "build_digest"]
