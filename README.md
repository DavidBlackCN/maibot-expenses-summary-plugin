# maibot-expenses-summary-plugin - 麦麦财务总结

一个 MaiBot 1.0.9+ / sdk2.6.0+ 插件，使用最新版本sdk新增的 `self.ctx.statistics` 能力代理，统计当天模型调用次数、回复量和回复成本，并生成财报文本与图片。

## 功能

- 统计今日模型请求、回复和成本
- 支持 `/expenses` 和 `/今日财报` 指令立即生成财报
- 支持作为 Tool 被麦麦主动调用
- 支持“默认”和“麦晨风”两种财报模式
- 支持管理员命令在线切换财报模式
- 支持合并转发消息或普通消息发送
- 使用 HTML 渲染图片展示累计请求次数、回复成本和各模型回复成本
- 可选定时发送

## 财报模式

### 默认模式

默认模式文案偏正常，适合日常查看成本。默认情况下：

1. 第一条消息：今日财报开头语
2. 第二条消息：图片，包含累计请求、回复消息、回复成本、各模型成本

默认模式第一条文本可通过 `report.default_opening` 修改，支持 `{date}` 占位符。

### 麦晨风模式

让你的麦麦化身“户晨风本风”，每天用咬牙切齿的语气公开处刑自己（并顺带感谢股东）：

1. 第一条消息：麦晨风风格开头语
2. 第二条消息：图片，包含累计请求、回复消息、回复成本、各模型成本
3. 第三条消息：感谢文案

## 指令

```text
/expenses
/今日财报
```

管理员可切换模式：

```text
/财报模式 默认
/财报模式 麦晨风
/expensesmode default
/expensesmode maichenfeng
```

`/财报模式` 和 `/expensesmode` 始终仅管理员可用。管理员通过 `permission.admins` 配置 QQ 号。

## 配置

仓库提供 `config.example.toml`，可作为默认配置参考。主要配置如下：

```toml
[plugin]
config_version = "1.0.2"

[report]
mode = "default"
title = "今日模型调用财报"
llm_task = "utils"
use_forward_message = true
default_opening = "{date}模型调用财报已生成，以下是今日请求次数、回复量与模型成本汇总。"

[permission]
query_admin_only = false
admins = []

[scheduler]
enabled = false
time = "23:30"
group_ids = []
private_ids = []

[fallback]
xiao_names = ["小麦"]
locations = ["KFC", "卧室", "广州塔", "下水道"]
poems = [
  "How do you do, you like me and I like you.",
  "Shut up! I read this inside the book I read before."
]
thanks_list = ["810", "艾斯比"]

# BGM 音频功能自 1.0.1 起暂停启用：当前 sdk2.x 暂未提供 send.audio 能力。
```

`report.use_forward_message = true` 时使用合并转发消息发送；设为 `false` 时会按普通消息逐条发送文本和图片。

`report.llm_task` 用于配置麦晨风模式生成地点、“我去了……”和诗句时使用的任务名，插件会通过 SDK 公共接口 `ctx.llm.generate(..., model=report.llm_task)` 调用，默认使用 `utils`。小名不会交给 LLM 生成，只会从 `fallback.xiao_names` 中选择。

`permission.query_admin_only = true` 时，`/expenses` 和 `/今日财报` 仅管理员可用；模式切换命令始终仅管理员可用。

`scheduler.enabled = true` 时会按 `scheduler.time` 每天定时发送。`scheduler.group_ids` 填 QQ 群号，`scheduler.private_ids` 填私聊 QQ 号，插件会通过 `ctx.chat.get_stream_by_group_id()` / `ctx.chat.get_stream_by_user_id()` 解析目标会话后发送。

## Tool

麦麦在需要“生成今日财报”“公开模型调用成本”“麦晨风式收入汇报”等场景下可以调用 `expenses_summary`。

## 安装

将插件目录放入 MaiBot 的插件目录，确认 `_manifest.json` 与 `plugin.py` 位于同一目录后重启 MaiBot。

## 兼容性

- MaiBot 最低版本：`1.0.9`
- SDK 版本：`2.6`

插件使用 `ctx.statistics.local.*` 获取统计数据，使用 `ctx.render.html2png()` 生成图片。合并转发模式使用 `ctx.send.forward()`，普通消息模式使用 `ctx.send.text()` 和 `ctx.send.image()`。

统计口径：MaiBot 统计 API 的 `days` 参数表示最近 N 天数据；插件会使用小时粒度趋势数据，并按本地日期过滤为当天 0 点至当前时间，避免新的一天继续计入前一日的 24H 数据。

## 更新日志

### 1.0.2

- 精简 manifest 统计能力声明，移除顶层 `statistics` 和 `statistics.local`，仅保留实际使用的方法级能力。
- 修复定时发送目标解析逻辑，按 QQ 群号/QQ 号解析会话后发送定时财报。

### 1.0.1

- 暂停启用麦晨风模式下的 BGM 音频发送功能。
- 移除 `send.audio` 能力声明，避免当前 sdk2.x 无该能力时加载或审查失败。
- 从默认配置示例中移除 `[audio]` 配置节，后续 SDK 提供公共音频发送能力后再恢复。

### 1.0.0

- 移植到 MaiBot 1.0.9+ 与 sdk2.6.0+。
- 新增默认模式与麦晨风模式。
- 新增合并转发消息/普通消息发送配置。
- 新增 `/expenses`、`/今日财报` 查询命令。
- 新增 `/财报模式`、`/expensesmode` 管理员模式切换命令。
- 新增管理员列表与查询命令权限配置。
- 新增 `config.example.toml` 默认配置示例。
- 使用 `ctx.statistics.local.*` 统计当天模型调用与成本，按本地日期过滤当天 0 点后的小时数据。
- 使用 `ctx.llm.generate(..., model=report.llm_task)` 生成麦晨风模式短素材。
- 使用 HTML 转图片展示累计请求、回复消息、回复成本和各模型回复成本。

## 鸣谢

[Kmaj1st/expenses_summary](https://github.com/Kmaj1st/expenses_summary) - 一个 MaiBot 插件，让你的麦麦化身“户晨风本风”，每天用咬牙切齿的语气公开处刑自己（并顺带感谢股东），麦晨风模式取自此插件。

## 许可证
MIT
