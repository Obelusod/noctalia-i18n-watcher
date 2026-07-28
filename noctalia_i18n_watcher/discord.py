"""Discord application messages."""

from __future__ import annotations

from noctalia_i18n_core import DiscordSender, Embed, StateSummary

_BASELINE_COLOR = 0x5865F2
_STATUS_COLOR = 0x57F287


def baseline_embed(changes: int, source_texts: int) -> Embed:
    """Render the application-owned baseline notification."""

    return {
        "title": "Baseline Created",
        "description": (
            "Monitoring starts from this point; existing history will not be replayed."
        ),
        "color": _BASELINE_COLOR,
        "fields": [
            {
                "name": "RECENT HISTORY",
                "value": f"{changes:,} changes recorded",
                "inline": True,
            },
            {
                "name": "ENGLISH SNAPSHOT",
                "value": f"{source_texts:,} keys",
                "inline": True,
            },
        ],
    }


def _status_embed(summary: StateSummary) -> Embed:
    return {
        "title": "Watcher Status",
        "description": (
            "Ready" if summary.initialized else "Baseline has not been created."
        ),
        "color": _STATUS_COLOR,
        "fields": [
            {
                "name": "ENGLISH SNAPSHOT",
                "value": f"{summary.source_texts:,} keys",
                "inline": True,
            },
            {
                "name": "PENDING QUEUE",
                "value": (
                    f"{summary.pending_deliveries:,} deliveries across "
                    f"{summary.pending_routes:,} routes"
                ),
                "inline": True,
            },
            {
                "name": "DELIVERY RECEIPTS",
                "value": f"{summary.delivery_receipts:,} recorded",
            },
        ],
    }


def send_status(
    sender: DiscordSender,
    target_ref: str,
    summary: StateSummary,
    *,
    username: str = "",
    avatar_url: str = "",
) -> None:
    """Send one application-owned status notification."""

    payload: dict[str, object] = {
        "allowed_mentions": {"parse": []},
        "embeds": [_status_embed(summary)],
    }
    if username:
        payload["username"] = username
    if avatar_url:
        payload["avatar_url"] = avatar_url
    sender.send(target_ref, payload)
