# Noctalia i18n Watcher

[English](README.md)

Noctalia i18n Watcher 是面向 [Noctalia Translate](https://i18n.noctalia.dev/projects) 本地化项目的非官方变更监控工具，支持将原文和译文的新增、修改、删除投递到多个独立 Discord Webhook 路由。

默认通过内置网页适配器监控 `noctalia`，修改配置即可监控 `official-plugins` 等其他项目；systemd timer 负责定时运行。

## 功能特点

- 在同一实例中运行多个监听器，分别选择项目、数据源和 Discord 路由。
- 每个监听器可配置多条路由，独立选择 locale、操作类型、文案和投递策略。
- 提供详细、紧凑和合并投递文案，支持通过 YAML 自定义 Embed 内容与排版。
- 可等待连续变更趋于平静后投递，也可限制最长等待时间，兼顾零散修改和集中更新。
- 可折叠同一 locale 和键的重复变更，并按阈值合并同一 locale 的批量消息。
- 持久保存基线、数据源游标、待投递消息和投递回执，重启后可继续处理。
- 单个监听器失败不会阻止后续监听器；Discord 路由失败时保留待投递消息，已成功投递不会重复。
- Webhook 消息禁用 Discord 提及解析，不会意外通知用户、角色或 `@everyone`。
- 提供立即运行、只读预览、状态查询、历史查看和基线重建等管理命令。
- 数据采集通过适配器接入，新增上游无需修改监控、文案或 Discord 投递模块。

## 消息格式

默认详细文案使用绿色、黄色和红色区分新增、修改和删除。详细原文语言和目标语言文案会在有编辑者主页时显示链接；时间由 Discord 本地化显示。

| 范围 | 文案 | 布局 |
| --- | --- | --- |
| `source` | `english`、`chinese`、`compact` | 英文原文变更；详细内置文案显示键名和值 |
| `target` | `english`、`chinese`、`compact` | 目标语言变更；详细内置文案显示键名、英文和译文值 |
| `merge` | `english`、`chinese` | 同一 locale 的批量变更；逐条列出键和值变化 |

```text
Obelusod
[zh-Hans] 修改了 1 条译文

键名
`settings.schema.notifications.border.description`

英文
`Draw an outline around notification toasts`

旧值
`在 Toast 通知周围绘制边框`

新值
`在 Toast 通知周围绘制轮廓`

今天 17:51
```

## 项目结构

```text
.
├── config/
│   ├── config.json           # 默认配置
│   └── messages/             # Discord 文案
│       ├── source/           # 原文语言文案
│       ├── target/           # 目标语言文案
│       └── merge/            # 合并投递文案
├── noctalia_i18n_watcher/    # 程序源码
│   ├── sources/              # 数据源适配器
│   │   └── web.py            # 网页抓取适配器
│   ├── app.py                # 应用操作与组件装配
│   ├── cli.py                # 程序入口与命令行处理
│   ├── config.py             # 配置加载/校验
│   ├── discord.py            # Discord 消息渲染、投递
│   ├── messages.py           # YAML 文案加载、渲染
│   ├── models.py             # 共享数据模型
│   ├── monitor.py            # 变更轮询与投递
│   └── state.py              # SQLite 状态存储
├── scripts/                  # 部署与管理脚本
├── systemd/                  # systemd 定时调度
├── tests/                    # 自动化测试
└── pyproject.toml            # 开发环境配置
```

## 配置

运行配置位于 `/etc/noctalia-i18n-watcher/config.json`。进程级字段如下：

| 字段 | 初始值 | 用途 |
| --- | --- | --- |
| `settings.http.proxy_url` | `""` | 带端口的 HTTP(S) 或 SOCKS 代理 URL；空字符串表示直连 |
| `settings.http.request_timeout` | `30` | 单次 HTTP 请求超时，范围为 0.001–300 秒 |
| `settings.retention_days` | `180` | 投递回执的保留天数，范围为 1–3650 |
| `settings.log_level` | `INFO` | `CRITICAL`、`ERROR`、`WARNING`、`INFO`、`DEBUG` 或 `NOTSET` |

每个 `monitors[]` 项包含：

| 字段 | 用途 |
| --- | --- |
| `id` | 稳定且全局唯一的监听器 ID |
| `source.project` | 本地化项目标识，例如 `noctalia` 或 `community-plugins` |
| `source.adapter` | `noctalia_i18n_watcher.sources` 中的数据源适配器 |
| `source.options` | 由所选适配器定义并校验的选项 |
| `webhooks` | 该监听器拥有的一条或多条 Discord 路由 |

内置网页适配器支持 `source.options.max_pages`，初始值为 `20`，用于限制恢复游标时读取的历史页数，范围为 1–100。项目标识取自 URL，例如 `/projects/community-plugins`。

添加项目时，复制 [`config/config.json`](config/config.json) 中的一个监听器对象，为其设置唯一 `id`、数据源和路由。路由 ID 只需在所属监听器内唯一，不同监听器可以使用同一个 Discord Webhook URL。

每个 `webhooks[]` 项包含：

| 字段 | 用途 |
| --- | --- |
| `id` | 稳定且在所属监听器内唯一的路由 ID |
| `url` | Discord Webhook URL |
| `username` | 发送时覆盖的显示名；空字符串使用 Discord Webhook 的名称 |
| `avatar_url` | 发送时覆盖的头像；空字符串使用 Discord Webhook 的头像 |
| `locales` | locale 列表，或仅包含 `"*"` 的列表 |
| `actions` | `added`、`modified`、`deleted` 的非空子集 |
| `messages` | 选择原文语言、目标语言和合并投递文案 |
| `delivery.quiet_seconds` | 自动投递前需要连续无新变更的秒数，范围为 0–86400 |
| `delivery.max_wait_seconds` | 最早记录的最长等待秒数，范围为 0–604800，且不小于 `quiet_seconds` |
| `delivery.latest_only` | 是否将同一 locale 和键的待投递变更折叠为最终结果 |
| `delivery.merge.enabled` | 是否合并超过阈值的 locale 批次 |
| `delivery.merge.threshold` | locale 批次合并阈值；启用时，`0` 合并任意非空批次，范围为 0–100 |
| `notify_baseline` | 是否发送路由的基线与订阅摘要 |

所有列出的字段均须填写。至少配置一个监听器，且每个监听器至少包含一条路由；校验错误会阻止启动。修改已有监听器的项目或适配器后，运行 `sudo noctalia-i18n-watcher reset state --monitor ID` 建立匹配的新状态。

`locales` 匹配规范化变更中的 `Change.locale`，它表示所选项目的 locale 标识，例如 `de`、`zh-Hans` 或 `pt-BR`。适配器只负责采集和规范化上游变更，所选本地化项目决定支持哪些语言。`["*"]` 匹配全部规范化 locale 标识，显式列表则精确匹配，项目新增或更改标识后需要相应修改配置。`"*"` 不能与其他标识并用。

默认配置中的 `default` 路由同时订阅英文原文和简体中文，逐条消息与合并投递均使用英文文案。同一 locale 超过五条时合并；路由等待四分钟无新变更，最长等待十五分钟。

### 检查周期

默认 timer 在启动两分钟后首次采集，此后在上一次运行结束两分钟后再次采集。请求量随监听器数量线性增加；同时监控较多项目时应相应延长周期。

通过 systemd drop-in 修改周期：

```bash
sudo systemctl edit noctalia-i18n-watcher.timer
```

例如保留首次两分钟，将后续间隔改为五分钟：

```ini
[Timer]
OnActiveSec=
OnActiveSec=2min
OnUnitInactiveSec=
OnUnitInactiveSec=5min
```

重新启动 timer：

```bash
sudo systemctl daemon-reload
sudo systemctl restart noctalia-i18n-watcher.timer
```

空赋值用于先清除已安装的 timer 值。删除 drop-in、重新加载 systemd 并重启 timer 即可恢复默认周期。

## 文案

文案位于 `/etc/noctalia-i18n-watcher/messages/<scope>/<name>.yaml`。Noctalia 的原文语言固定为英文。

| 订阅范围 | `locales` | `messages` |
| --- | --- | --- |
| 仅英文原文 | `["en"]` | `{"source": "english", "target": "english", "merge": "english"}` |
| 仅简体中文 | `["zh-Hans"]` | `{"source": "chinese", "target": "chinese", "merge": "chinese"}` |
| 英文原文和德语 | `["en", "de"]` | `{"source": "english", "target": "english", "merge": "english"}` |
| 所有 locale | `["*"]` | `{"source": "compact", "target": "compact", "merge": "english"}` |

`messages` 必须显式填写 `source`、`target` 和 `merge`。`locales` 决定是否加载 source 和 target 文案，`delivery.merge.enabled` 决定是否加载 merge 文案；未启用范围的选择在启用前不会根据文案文件进行校验。

source 和 target 文件定义 `added`、`modified`、`deleted`，逐条文案可参考 [`target/chinese.yaml`](config/messages/target/chinese.yaml)。merge 文件使用 `source`、`target` Embed 和 `entries` 下的三种动作排版，例如 [`merge/chinese.yaml`](config/messages/merge/chinese.yaml)：

```yaml
source:
  title: "[English] 原文变更 · {count} 条"
  description: "{entries}"
target:
  title: "[{locale}] 译文变更 · {count} 条"
  description: "{entries}"
entries:
  separator: "\n"
  added: |-
    **[`{key}`]({change_url})**
    > `∅` → `{new_value}`
  modified: |-
    **[`{key}`]({change_url})**
    > `{old_value}` → `{new_value}`
  deleted: |-
    **[`{key}`]({change_url})**
    > `{old_value}` → `∅`
```

Embed 可使用 `title`、`description`、`fields`、`footer`、`author`、`image`、`thumbnail`、`url`、`timestamp` 和 `color`。组件和字段只在写出时显示，并保持配置顺序。Discord 按查看者设置渲染 `timestamp`，省略即可隐藏时间。

YAML 字符串支持以下占位符：

| 占位符 | 内容 |
| --- | --- |
| `{key}` | 翻译键 |
| `{monitor_id}` | 当前变更所属的监听器 ID |
| `{project}` | 监听器选择的本地化项目 |
| `{source}` | 英文原文 |
| `{old_value}` | 修改前或删除前的值 |
| `{new_value}` | 新增后或修改后的值 |
| `{locale}` | 变更的 locale，例如 `zh-Hans` |
| `{actor}` | 编辑者 |
| `{actor_url}` | 编辑者主页 URL（如有） |
| `{actor_avatar_url}` | 编辑者头像 URL（如有） |
| `{action}` | `added`、`modified` 或 `deleted` |
| `{change_url}` | 数据源提供的变更详情 URL；直接作为 Embed `url` 且为空时省略 |
| `{timestamp}` | 变更发生时间的 UTC ISO 8601 值，可直接用于 Discord `timestamp` |
| `{unix_time}` | 变更时间的 Unix 秒数，可用于 Discord 时间标记 |

合并条目可使用上表全部占位符。合并 Embed 可使用 `{monitor_id}`、`{project}`、`{locale}` 以及以下批次占位符：

| 占位符 | 内容 |
| --- | --- |
| `{count}` | 当前消息包含的变更数 |
| `{actors}` / `{actor_count}` | 去重后的编辑者及其数量 |
| `{actor_avatar_url}` | 单一编辑者的头像 URL；多人或缺失时为空 |
| `{added_count}` / `{modified_count}` / `{deleted_count}` | 最终动作的数量 |
| `{first_timestamp}` / `{last_timestamp}` | 最早和最晚变更时间的 UTC ISO 8601 值 |
| `{first_unix_time}` / `{last_unix_time}` | 最早和最晚变更时间的 Unix 秒数 |
| `{entries}` | 按 `entries.separator` 连接后的完整变更条目 |

使用 `truncate=N` 缩略 `{key}`、`{source}`、`{old_value}`、`{new_value}` 和 `{actor}`；合并 Embed 还支持 `{actors}` 和 `{entries}`。`N` 包含省略号，宽字符按两格计算：

```yaml
value: "`{new_value:truncate=160}`"
```

使用 `fallback=...` 可为缺失的字符串值指定替代内容：

```yaml
icon_url: "{actor_avatar_url:fallback=https://github.com/noctalia-dev.png}"
```

不可用的作者地址会被省略。YAML 支持 Discord Markdown 和整数 `color`；过大的合并消息按条目拆分。

## 安装

需要使用带 systemd 和 `apt-get` 的 Debian 或 Ubuntu、Python 3.12+、root 权限和至少一个 Discord Webhook。

```bash
sudo bash scripts/install.sh
sudoedit /etc/noctalia-i18n-watcher/config.json
sudo noctalia-i18n-watcher check
sudo noctalia-i18n-watcher run
sudo systemctl enable --now noctalia-i18n-watcher.timer
```

安装只会在缺失时创建 `config.json` 和内置文案文件。配置与文案位于 `/etc/noctalia-i18n-watcher`，程序将状态保存到由 systemd 管理的 `/var/lib/noctalia-i18n-watcher` 目录。

### 更新

更新仓库工作区后运行：

```bash
sudo bash scripts/update.sh
```

更新会保留配置、现有文案和监听状态；仅在目标路径不存在时安装仓库新增的文案文件。

### 卸载

```bash
sudo bash scripts/uninstall.sh
```

卸载会删除程序及 systemd 配置，保留配置和状态。彻底删除全部数据时运行：

```bash
sudo bash scripts/uninstall.sh --purge
```

## 运维

运行 `noctalia-i18n-watcher --help` 可查看已安装命令的摘要。运维命令需要使用 `sudo`。

| 命令 | 用途 |
| --- | --- |
| `sudo noctalia-i18n-watcher run [--monitor ID] [--dry-run]` | 立即采集并投递当前队列；`--dry-run` 仅预览，不发送或写入状态 |
| `sudo noctalia-i18n-watcher reset baseline [--monitor ID] [--notify]` | 重建数据源游标和英文原文快照；`--notify` 发送基线摘要 |
| `sudo noctalia-i18n-watcher reset state [--monitor ID] [--notify]` | 清空监听状态并重新建立基线；`--notify` 发送基线摘要 |
| `sudo noctalia-i18n-watcher check [--monitor ID]` | 校验配置、文案和状态，不轮询数据源或连接 Discord |
| `sudo noctalia-i18n-watcher status [--monitor ID] [--notify]` | 查看 timer 状态、上次服务结果和各监听器的状态摘要；`--notify` 发送状态摘要 |
| `sudo noctalia-i18n-watcher changes 1 [--monitor ID]` | 查看数据源原生历史记录的第一页，不发送消息或写入状态 |

未指定 `--monitor` 时，`run`、`reset`、`check` 和 `status` 会处理全部监听器；配置多个监听器时，`changes` 必须指定目标。其页码从 1 开始。重置操作会暂停 timer，并在结束后恢复原有运行状态。

### 修改配置

修改进程设置、监听器、数据源和 Webhook 路由：

```bash
sudoedit /etc/noctalia-i18n-watcher/config.json
```

在 `messages` 选择的 YAML 文件中修改 Discord 文案和排版：

```bash
sudoedit /etc/noctalia-i18n-watcher/messages/target/chinese.yaml
```

修改后校验全部运行配置：

```bash
sudo noctalia-i18n-watcher check
```

文件会在下次运行时加载；检查周期参见[检查周期](#检查周期)。

## 开发

需要 Python 3.12 或更高版本及 uv。按照 `uv.lock` 创建或更新开发环境：

```bash
uv sync --locked
```

运行全部测试，或在开发时仅运行一个模块：

```bash
uv run python -m unittest discover -v
uv run python -m unittest tests.test_web -v
```

默认测试使用固定 HTTP 响应与临时数据库，验证配置、数据源、监控流水线、SQLite 状态和 Discord 投递，不访问外部服务。

显式检查数据源与实时上游的契约：

```bash
NOCTALIA_LIVE_CONFIG=config/config.json \
  uv run python -m unittest tests.live.LiveSourceTests -v
```

使用临时 Webhook 向 Discord 发送一条真实测试消息：

```bash
NOCTALIA_TEST_WEBHOOK_URL='https://discord.com/api/webhooks/...' \
  uv run python -m unittest tests.live.LiveDiscordTests -v
```

检查格式、代码规则和静态类型：

```bash
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
```

### 数据源

在 `noctalia_i18n_watcher/sources/` 中创建与 `monitors[].source.adapter` 同名的 `<adapter>.py`。每个适配器接收所选项目，并负责自己的 `source.options`。

| 成员 | 契约 |
| --- | --- |
| `create(options, *, project, timeout, proxy)` | 校验适配器选项，并在不联网的情况下构造所选项目的数据源 |
| `poll(cursor)` | 根据适配器自有的 JSON 游标返回 `PollResult` |
| `close()` | 释放适配器持有的全部资源 |
| `page(number)` | 可选的一页历史记录，页码从 1 开始，用于 `changes` 命令 |

`PollResult` 包含变更、适配器自有 JSON 游标、扫描数量和 `source_texts`。初次轮询及包含变更的轮询提供完整英文原文映射；已有基线且无变更时可省略 `source_texts`。变更 ID 必须稳定、按时间从旧到新排列并带时区；属于其他适配器或项目的游标必须报错。

新增 `tests/test_<adapter>.py`，使用共享的 [`PollScenario`](tests/contracts.py) 与确定的上游响应。
