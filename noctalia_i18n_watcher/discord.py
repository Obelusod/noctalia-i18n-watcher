"""Discord application messages."""

from __future__ import annotations

from noctalia_i18n_core import SOURCE_LOCALE, DiscordSender, Embed, StateSummary

from .config import MonitorConfig, WebhookConfig

_BASELINE_COLOR = 0x5865F2
_STATUS_COLOR = 0x57F287


def _duration(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _locales(webhook: WebhookConfig) -> str:
    if "*" in webhook.locales:
        return "`*`"
    return " · ".join(
        f"`{locale}`"
        for locale in sorted(
            webhook.locales,
            key=lambda locale: (locale != SOURCE_LOCALE, locale.casefold()),
        )
    )


def _actions(webhook: WebhookConfig) -> str:
    return " · ".join(
        f"`{action}`"
        for action in ("added", "modified", "deleted")
        if action in webhook.actions
    )


def _delivery(webhook: WebhookConfig) -> str:
    policy = webhook.delivery
    details = [
        (
            "No inactivity delay"
            if policy.quiet_seconds == 0
            else f"After `{_duration(policy.quiet_seconds)}` inactivity"
        ),
        f"`{_duration(policy.max_wait_seconds)}` maximum",
        ("Latest change per locale and key" if policy.fold_changes else "Every change"),
    ]
    if policy.merge_threshold is None:
        details.append("Individual messages")
    elif policy.merge_threshold == 0:
        details.append("Merge every locale batch")
    else:
        details.append(f"Merge locale batches above `{policy.merge_threshold}`")
    return " · ".join(details)


def baseline_embed(
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    changes: int,
    source_texts: int,
) -> Embed:
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
                "value": (
                    f"{changes:,} changes recorded\n\n"
                    f"**SUBSCRIBED LOCALES**\n{_locales(webhook)}"
                ),
                "inline": True,
            },
            {
                "name": "ENGLISH SNAPSHOT",
                "value": (
                    f"{source_texts:,} keys\n\n**WATCHED ACTIONS**\n{_actions(webhook)}"
                ),
                "inline": True,
            },
            {
                "name": "DELIVERY",
                "value": _delivery(webhook),
            },
        ],
        "footer": {
            "text": (
                f"Monitor: {monitor.id} · Project: {monitor.project} · "
                f"Route: {webhook.id}"
            )
        },
    }


def status_embed(
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    summary: StateSummary,
) -> Embed:
    deliveries = (
        f"{summary.pending_deliveries:,} "
        f"{'delivery' if summary.pending_deliveries == 1 else 'deliveries'}"
    )
    routes = (
        f"{summary.pending_routes:,} "
        f"{'route' if summary.pending_routes == 1 else 'routes'}"
    )
    return {
        "title": "Watcher Status",
        "description": "Current persisted monitoring state and route configuration.",
        "color": _STATUS_COLOR,
        "fields": [
            {
                "name": "MONITORING STATE",
                "value": (
                    f"{'Ready' if summary.initialized else 'Baseline not created'}\n\n"
                    f"**SUBSCRIBED LOCALES**\n{_locales(webhook)}\n\n"
                    f"**DELIVERY RECEIPTS**\n"
                    f"{summary.delivery_receipts:,} recorded"
                ),
                "inline": True,
            },
            {
                "name": "ENGLISH SNAPSHOT",
                "value": (
                    f"{summary.source_texts:,} keys\n\n"
                    f"**WATCHED ACTIONS**\n{_actions(webhook)}\n\n"
                    f"**PENDING QUEUE**\n{deliveries} across {routes}"
                ),
                "inline": True,
            },
            {
                "name": "DELIVERY",
                "value": _delivery(webhook),
            },
        ],
        "footer": {
            "text": (
                f"Monitor: {monitor.id} · Project: {monitor.project} · "
                f"Route: {webhook.id}"
            )
        },
    }


def send_status(
    sender: DiscordSender,
    monitor: MonitorConfig,
    webhook: WebhookConfig,
    summary: StateSummary,
) -> None:
    """Send one application-owned status notification."""

    payload: dict[str, object] = {
        "allowed_mentions": {"parse": []},
        "embeds": [status_embed(monitor, webhook, summary)],
    }
    if webhook.username:
        payload["username"] = webhook.username
    if webhook.avatar_url:
        payload["avatar_url"] = webhook.avatar_url
    sender.send(webhook.id, payload)
