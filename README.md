# Noctalia i18n Watcher

[简体中文](README.zh-CN.md)

Noctalia i18n Watcher is an unofficial change monitor for localization projects on [Noctalia Translate](https://i18n.noctalia.dev/projects). It supports delivering additions, modifications, and deletions of source text and translations to independent Discord webhook routes.

By default, the built-in web adapter monitors `noctalia`. Other projects such as `official-plugins` can be selected through configuration, while a systemd timer handles scheduled runs.

## Features

- Run multiple monitors in one installation, each selecting its own project, source, and Discord routes.
- Configure multiple routes per monitor with independent locale, action, message, and delivery settings.
- Use detailed, compact, or merged messages, with Discord embed content and layout defined in YAML.
- Wait for continuous activity to settle while enforcing a maximum delay, accommodating both isolated and concentrated changes.
- Fold repeated changes to the same locale/key and merge locale batches above a configurable threshold.
- Persist baselines, source cursors, pending deliveries, and delivery receipts so processing can resume after a restart.
- Continue with later monitors after one fails; retain pending Discord deliveries without repeating successful ones.
- Disable Discord mention parsing so webhook messages cannot unexpectedly notify users, roles, or `@everyone`.
- Manage immediate runs, read-only previews, status inspection, history pages, and baseline resets from the command line.
- Add another upstream through a source adapter without changing monitoring, messages, or Discord delivery.

## Message format

The default detailed messages use green, yellow, and red for additions, modifications, and deletions. Merged messages highlight changed tokens with bold red and green ANSI text. Detailed source and target messages link editor profiles when available; Discord localizes timestamps.

| Scope | Messages | Layout |
| --- | --- | --- |
| `source` | `english`, `chinese`, `compact` | English source-text changes; detailed built-ins show the key and values |
| `target` | `english`, `chinese`, `compact` | Target-language changes; detailed built-ins show the key, English, and target values |
| `merge` | `english`, `chinese` | Locale batches; entries list each key and value change |

```text
Obelusod
[zh-Hans] Modified 1 translation

KEY
`settings.schema.notifications.border.description`

ENGLISH
`Draw an outline around notification toasts`

OLD
`在 Toast 通知周围绘制边框`

NEW
`在 Toast 通知周围绘制轮廓`

Today at 5:51 PM
```

## Project structure

```text
.
├── config/
│   ├── config.json           # Default configuration
│   └── messages/             # Discord messages
│       ├── source/           # Source-language messages
│       ├── target/           # Target-language messages
│       └── merge/            # Merged delivery messages
├── noctalia_i18n_watcher/    # Application source
│   ├── sources/              # Source adapters
│   │   └── web.py            # Web scraping adapter
│   ├── app.py                # Application operations and composition
│   ├── cli.py                # Program entry and command-line handling
│   ├── config.py             # Configuration loading/validation
│   ├── diff.py               # Multilingual value differences
│   ├── discord.py            # Discord message rendering and delivery
│   ├── messages.py           # YAML message loading and rendering
│   ├── models.py             # Shared data models
│   ├── monitor.py            # Change polling and delivery
│   └── state.py              # SQLite state storage
├── scripts/                  # Deployment and management scripts
├── systemd/                  # systemd scheduling
├── tests/                    # Automated tests
└── pyproject.toml            # Development configuration
```

## Configuration

Runtime configuration lives in `/etc/noctalia-i18n-watcher/config.json`. Process-wide fields are:

| Field | Initial value | Purpose |
| --- | --- | --- |
| `settings.http.proxy_url` | `""` | HTTP(S) or SOCKS proxy URL with a port; empty connects directly |
| `settings.http.request_timeout` | `30` | HTTP request timeout from 0.001 to 300 seconds |
| `settings.retention_days` | `180` | Delivery-receipt retention from 1 to 3650 days |
| `settings.log_level` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`, or `NOTSET` |

Each `monitors[]` entry contains:

| Field | Purpose |
| --- | --- |
| `id` | Stable monitor ID, unique across the configuration |
| `source.project` | Localization project identifier, such as `noctalia` or `community-plugins` |
| `source.adapter` | Data-source adapter under `noctalia_i18n_watcher.sources` |
| `source.options` | Options owned and validated by the selected adapter |
| `webhooks` | One or more Discord routes owned by this monitor |

The built-in web adapter accepts `source.options.max_pages`, initially `20`, to bound the history read during cursor recovery; its range is 1–100. The project identifier comes from its URL, such as `/projects/community-plugins`.

To add a project, copy one monitor object in [`config/config.json`](config/config.json), then assign a unique monitor `id`, select its source, and configure its routes. Route IDs are local to a monitor, and the same Discord webhook URL may be used by different monitors.

Each `webhooks[]` entry contains:

| Field | Purpose |
| --- | --- |
| `id` | Stable route ID, unique within its monitor |
| `url` | Secret Discord webhook URL |
| `username` | Per-send display-name override; empty uses the Discord webhook name |
| `avatar_url` | Per-send avatar override; empty uses the Discord webhook avatar |
| `locales` | Locale list, or a list containing only `"*"` |
| `actions` | Non-empty subset of `added`, `modified`, and `deleted` |
| `messages` | Source-language, target-language, and merged delivery message names |
| `delivery.quiet_seconds` | Required inactivity before automatic delivery, from 0 to 86400 seconds |
| `delivery.max_wait_seconds` | Maximum wait from the oldest item, from 0 to 604800 seconds and not less than `quiet_seconds` |
| `delivery.latest_only` | Whether to fold queued changes for each locale and key into their final result |
| `delivery.merge.enabled` | Whether to merge locale batches that exceed the threshold |
| `delivery.merge.threshold` | Locale batch threshold; when enabled, `0` merges every non-empty batch, from 0 to 100 |
| `notify_baseline` | Whether to send the route's baseline and subscription summary |

All listed fields are required. At least one monitor and one route per monitor must be configured; validation errors prevent startup. After changing a configured monitor's project or adapter, run `sudo noctalia-i18n-watcher reset state --monitor ID` to establish fresh state for the new source.

`locales` matches `Change.locale` in the normalized change, which represents a selected project locale such as `de`, `zh-Hans`, or `pt-BR`. Adapters only acquire and normalize upstream changes; the selected localization project determines its supported locales. `["*"]` matches every normalized locale identifier, while explicit lists match exactly and must be updated when the project adds or renames an identifier. `"*"` cannot be combined with explicit identifiers.

The `default` route in the initial configuration subscribes to English source text and Simplified Chinese, using English messages for both detailed and merged delivery. Batches above five changes for one locale are merged. The route waits for four minutes of inactivity with a fifteen-minute maximum.

### Check interval

The default timer starts its first collection after two minutes and schedules the next one two minutes after the previous run ends. Request volume grows linearly with the number of monitors; use a longer interval when watching many projects.

Change the interval through a systemd drop-in:

```bash
sudo systemctl edit noctalia-i18n-watcher.timer
```

For example, keep the first check at two minutes and use a five-minute interval:

```ini
[Timer]
OnActiveSec=
OnActiveSec=2min
OnUnitInactiveSec=
OnUnitInactiveSec=5min
```

Restart the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl restart noctalia-i18n-watcher.timer
```

The empty assignments clear the installed timer values before replacing them. Delete the drop-in, reload systemd, and restart the timer to restore the default schedule.

## Messages

Messages live under `/etc/noctalia-i18n-watcher/messages/<scope>/<name>.yaml`. Noctalia's source language is fixed to English.

| Subscription | `locales` | `messages` |
| --- | --- | --- |
| English source only | `["en"]` | `{"source": "english", "target": "english", "merge": "english"}` |
| Simplified Chinese only | `["zh-Hans"]` | `{"source": "chinese", "target": "chinese", "merge": "chinese"}` |
| English source and German | `["en", "de"]` | `{"source": "english", "target": "english", "merge": "english"}` |
| Every locale | `["*"]` | `{"source": "compact", "target": "compact", "merge": "english"}` |

`messages` must explicitly provide `source`, `target`, and `merge`. `locales` determines whether the source and target messages are loaded, while `delivery.merge.enabled` determines whether the merge message is loaded. Selections in an inactive scope are not validated against message files until that scope is enabled.

Source and target files define `added`, `modified`, and `deleted`; see [`target/english.yaml`](config/messages/target/english.yaml) for a detailed message. Merge files define `source` and `target` embeds, `entries.separator`, and one entry for each action. Built-in messages render value changes as:

````yaml
diff:
  old: {color: red, bold: true, underline: false}
  new: {color: green, bold: true, underline: false}
modified:
  fields:
    - name: CHANGE
      value: |-
        ```ansi
        − {old_diff}
        + {new_diff}
        ```
````

Merge entries use the same placeholders. `diff` is required when a file uses `{old_diff}` or `{new_diff}`. `color` accepts `gray`, `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`, `white`, or `null`; `bold` and `underline` independently control emphasis.

Embed definitions may use `title`, `description`, `fields`, `footer`, `author`, `image`, `thumbnail`, `url`, `timestamp`, and `color`. Components and fields appear only when written and in the listed order. Discord renders `timestamp` for each viewer; omit it to hide time.

YAML strings support these placeholders:

| Placeholder | Value |
| --- | --- |
| `{key}` | Translation key |
| `{key_link}` | Code-styled key linked to the change detail when available |
| `{monitor_id}` | Monitor ID owning the change |
| `{project}` | Localization project selected by the monitor |
| `{source}` | English source text |
| `{old_value}` | Value before modification or deletion |
| `{new_value}` | Value after addition or modification |
| `{old_diff}` / `{new_diff}` | Old/new value with changed tokens using the configured ANSI style |
| `{locale}` | Change locale, such as `zh-Hans` |
| `{actor}` | Editor |
| `{actor_url}` | Editor profile URL, when available |
| `{actor_avatar_url}` | Editor avatar URL, when available |
| `{action}` | `added`, `modified`, or `deleted` |
| `{change_url}` | Change-detail URL provided by the data source, when available |
| `{timestamp}` | UTC ISO 8601 change time, suitable for Discord's native `timestamp` |
| `{unix_time}` | Unix seconds for the change time, suitable for Discord timestamp markup |

Merge entries may use every placeholder above. The merged embed supports `{monitor_id}`, `{project}`, `{locale}`, and these batch placeholders:

| Placeholder | Value |
| --- | --- |
| `{count}` | Changes contained in the current message |
| `{actors}` / `{actor_count}` | Unique editors and their count |
| `{actor_avatar_url}` | Single editor's avatar URL; empty for multiple or unavailable editors |
| `{added_count}` / `{modified_count}` / `{deleted_count}` | Final action counts |
| `{first_timestamp}` / `{last_timestamp}` | First and last change times in UTC ISO 8601 form |
| `{first_unix_time}` / `{last_unix_time}` | Unix seconds for the first and last change times |
| `{entries}` | Complete entries joined by `entries.separator` |

Use `truncate=N` to shorten `{key}`, `{source}`, `{old_value}`, `{new_value}`, and `{actor}`; merged embeds also support `{actors}` and `{entries}`. `N` includes the ellipsis, and wide characters count as two columns:

```yaml
value: "`{new_value:truncate=160}`"
```

Use `fallback=...` to render a replacement for an unavailable string value:

```yaml
icon_url: "{actor_avatar_url:fallback=https://github.com/noctalia-dev.png}"
```

Unavailable author URLs are omitted. YAML supports Discord Markdown and integer `color` values; oversized merges split at entry boundaries.

## Installation

Requirements: Debian or Ubuntu with systemd and `apt-get`, Python 3.12+, root access, and one or more Discord webhooks.

```bash
sudo bash scripts/install.sh
sudoedit /etc/noctalia-i18n-watcher/config.json
sudo noctalia-i18n-watcher check
sudo noctalia-i18n-watcher run
sudo systemctl enable --now noctalia-i18n-watcher.timer
```

Installation creates `config.json` and the built-in message files only when absent. Configuration and messages remain under `/etc/noctalia-i18n-watcher`; the application stores state in the systemd-managed `/var/lib/noctalia-i18n-watcher` directory.

### Update

Update the repository checkout, then run:

```bash
sudo bash scripts/update.sh
```

The update preserves the configuration, existing messages, and monitoring state. New built-in message files are installed only when their target path is absent.

### Uninstall

```bash
sudo bash scripts/uninstall.sh
```

The uninstall preserves configuration and state while removing the application and systemd units. Remove everything with:

```bash
sudo bash scripts/uninstall.sh --purge
```

## Operations

Run `noctalia-i18n-watcher --help` to display the installed command summary. Operational commands require `sudo`.

| Command | Purpose |
| --- | --- |
| `sudo noctalia-i18n-watcher run [--monitor ID] [--dry-run]` | Collect immediately and flush the current queue; `--dry-run` only previews without sending or writing state |
| `sudo noctalia-i18n-watcher reset baseline [--monitor ID] [--notify]` | Rebuild the source cursor and English source-text snapshot; `--notify` sends a baseline summary |
| `sudo noctalia-i18n-watcher reset state [--monitor ID] [--notify]` | Clear monitoring state and create a new baseline; `--notify` sends a baseline summary |
| `sudo noctalia-i18n-watcher check [--monitor ID]` | Validate configuration, messages, and state without polling sources or contacting Discord |
| `sudo noctalia-i18n-watcher status [--monitor ID] [--notify]` | Show the timer state, last service result, and persisted monitoring summaries; `--notify` sends status summaries |
| `sudo noctalia-i18n-watcher changes 1 [--monitor ID]` | Display the first source-native history page without sending or writing state |

Without `--monitor`, `run`, `reset`, `check`, and `status` operate on every configured monitor. `changes` requires `--monitor` when more than one exists. Its page number starts at 1. Reset operations pause the timer and restore its previous running state.

### Editing configuration

Edit process settings, monitors, sources, and webhook routes:

```bash
sudoedit /etc/noctalia-i18n-watcher/config.json
```

Edit Discord message content and layout in the YAML file selected by `messages`:

```bash
sudoedit /etc/noctalia-i18n-watcher/messages/target/chinese.yaml
```

Validate all runtime configuration after editing:

```bash
sudo noctalia-i18n-watcher check
```

Changes load on the next run; edit the schedule under [Check interval](#check-interval).

## Development

Python 3.12 or later and uv are required. Create or update the environment from `uv.lock`:

```bash
uv sync --locked
```

Run the complete test suite, or target one module while developing:

```bash
uv run python -m unittest discover -v
uv run python -m unittest tests.test_web -v
```

The default suite uses fixed HTTP responses and temporary databases to verify configuration, sources, the monitoring pipeline, SQLite state, and Discord delivery without accessing external services.

Explicitly verify a source against its live upstream:

```bash
NOCTALIA_LIVE_CONFIG=config/config.json \
  uv run python -m unittest tests.live.LiveSourceTests -v
```

Send one real test notification through a temporary Discord webhook:

```bash
NOCTALIA_TEST_WEBHOOK_URL='https://discord.com/api/webhooks/...' \
  uv run python -m unittest tests.live.LiveDiscordTests -v
```

Check formatting, lint rules, and static types:

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
```

### Sources

Add `noctalia_i18n_watcher/sources/<adapter>.py` with the same name as `monitors[].source.adapter`. Every adapter receives the selected project and owns its `source.options`.

| Member | Contract |
| --- | --- |
| `create(options, *, project, timeout, proxy)` | Construct the selected project source without network I/O after validating adapter options |
| `poll(cursor)` | Return a `PollResult` for the adapter-owned JSON cursor |
| `close()` | Release every resource owned by the adapter |
| `page(number)` | Optional one-based historical page used by the `changes` command |

`PollResult` contains the changes, an adapter-owned JSON cursor, the scanned count, and `source_texts`. Initial polls and polls with changes include the complete English source-text mapping; an unchanged poll may omit `source_texts`. Keep IDs stable, order changes oldest-first, include timezones, and reject cursors from another adapter or project.

Add `tests/test_<adapter>.py` using the shared [`PollScenario`](tests/contracts.py) contract with deterministic upstream responses.
