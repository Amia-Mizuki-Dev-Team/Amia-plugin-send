# Amia-plugin-send

`Amia-plugin-send` 是 Mizuki Bot 的消息活动统计插件。

它监听 OneBot V11 消息事件，将群聊与私聊活动写入 SQLite，并通过 `amia-core` 注册 `StatsProvider("send")`，供个人资料、群活跃分析和管理报表使用。

本插件不是消息发送层，也不负责欢迎、公告或业务消息转发。

## 插件作用

```text
OneBot 消息事件
      ↓
Amia-plugin-send
      ↓
SQLite 活动数据
      ↓
StatsProvider("send")
      ├── Amia-plugin-profile
      ├── Amia-plugin-group-insight
      └── 后续管理报表
```

当前主要提供：

- 每日和小时级消息统计；
- 群聊消息排行；
- 群活跃人数；
- 用户最近活动；
- Bot 实例活跃用户统计；
- 管理员活动概览；
- 绑定前后身份归并统计。

## 当前能力

- 记录消息数量和估算字节数；
- 保存显示名、首次出现和最后出现时间；
- 按 `self_id + group_id` 隔离群统计；
- 支持今日、本月、今年排行；
- 通过 IdentityResolver 保存可选 canonical user ID；
- 使用有界异步队列和批量写入降低 SQLite 压力；
- 写入失败或队列溢出时保存 dead-letter；
- 检测旧数据库结构后停止正常写入；
- 提供旧库 dry-run 和显式迁移工具；
- 迁移前执行完整性检查、状态检查和 SQLite 一致性备份；
- 迁移后校验消息、小时和流量总量。

## 用户指令

### 群聊排行

```text
今日发言
今日排行榜
本月发言
本月排行榜
今年发言
今年排行榜
```

### 超级用户统计

```text
今日DAU
全群统计
bot数据
本月DAU
本月统计
今年DAU
年度统计
```

未配置可验证的 Bot AppID 时，群聊排行仍可使用，但跨群实例 DAU 会显示为未验证。

## 统计身份与作用域

每条活动记录包含：

```text
adapter_type
adapter_instance_id
bot_id
bot_app_id
context_type
context_id
gensokyo_user_id
canonical_user_id
date
```

统计口径：

- 群排行：当前 `self_id + group_id`；
- 群 DAU：指定群、指定日期内不同 `gensokyo_user_id`；
- 用户活动：当前 `self_id` 内；
- 实例活跃用户：仅在明确配置 AppID 且确认跨上下文 ID 稳定时启用；
- merged DAU：使用 canonical 映射归并绑定前后记录，检测冲突时返回不可用。

日期范围统一采用半开区间：

```text
start_date <= date < end_date
```

调用方必须传入排除结束日的 `end_date`。

## 配置

```env
AMIA_SEND_DB_PATH=
AMIA_SEND_ADAPTER_INSTANCE_ID=qqbot-local
AMIA_SEND_BOT_APP_ID=
AMIA_SEND_CROSS_CONTEXT_USER_ID_STABLE=false
AMIA_SEND_WRITER_QUEUE_SIZE=2000
AMIA_SEND_WRITER_BATCH_SIZE=100
AMIA_SEND_WRITER_FLUSH_SECONDS=0.5
AMIA_SEND_RESOLVER_TIMEOUT_SECONDS=0.2
AMIA_SEND_DEAD_LETTER_PATH=
AMIA_SEND_DEAD_LETTER_MAX_BYTES=5242880
AMIA_TIMEZONE=Asia/Shanghai
```

说明：

- `AMIA_SEND_BOT_APP_ID` 必须显式配置，插件不猜测本地配置文件；
- `AMIA_SEND_CROSS_CONTEXT_USER_ID_STABLE` 只有确认 Gensokyo 跨群 ID 稳定时才能启用；
- `AMIA_TIMEZONE` 决定自然日、月和年的边界；
- dead-letter 默认与数据库位于同一目录。

## 数据库

默认路径：

```text
<插件目录>/data.db
```

主要表：

```text
activity_daily
activity_hourly
legacy_daily_metrics
legacy_hourly_metrics
schema_migrations
```

说明：

- `activity_daily`：具备明确用户和上下文语义的日统计；
- `activity_hourly`：新版本产生的小时统计；
- `legacy_daily_metrics`：无法安全映射为用户活动的旧日数据；
- `legacy_hourly_metrics`：缺少可靠群/私聊上下文的旧小时数据；
- `schema_migrations`：迁移状态。

运行时使用：

- WAL；
- busy timeout；
- 批量 upsert；
- 有界写入队列。

运行文件：

```text
data.db
data.db-wal
data.db-shm
*.dead-letter.jsonl
*.pre-v2.<timestamp>.bak
```

均不得提交到 Git。

## 写入失败处理

数据库写入会有限重试。重试失败或队列溢出时，记录写入：

```text
<data.db>.dead-letter.jsonl
```

运行状态可从 `ActivityWriter` 获取：

```text
dropped_records
last_dropped_at
failed_batches
failed_records
last_failure_at
last_failure_error
```

消费者不应直接依赖这些内部字段。后续可以通过 HealthProvider 暴露必要诊断信息。

## amia-core 对接

启动后注册：

```python
registry.register_stats_provider(
    "send",
    activity_service,
    replace=True,
)
```

消费者获取：

```python
provider = registry.get_stats_provider("send")
```

常用接口：

```text
get_user_activity
get_group_rank
get_group_dau
get_group_activity_summary
get_user_activity_summary
get_instance_active_users
get_merged_dau
get_admin_dashboard_data
```

调用方应通过 `call_provider_safe()` 处理 Provider 缺失、异常和超时，不应直接读取 Send SQLite。

### Profile 对接

`Amia-plugin-profile` 使用 `get_user_activity()` 获取最近消息量。

### Group Insight 对接

`Amia-plugin-group-insight` 使用 `get_group_activity_summary()` 获取指定群的消息总数和活跃成员数。

## IdentityResolver 对接

Send 可以调用 `amia-core` 中的 IdentityResolver，将：

```text
self_id + gensokyo_user_id
```

解析为：

```text
canonical_user_id
```

Resolver 不存在、超时或失败时，消息仍可写入，只是不包含 canonical ID。

Send 不直接修改 Gensokyo idmap，也不把昵称作为身份主键。

运行时通过 `from nonebot import require; require("amia_core")` 加载契约，
不依赖固定的 `src.plugins` 部署路径。

## 推荐加载顺序

```text
amia-core
qbind / IdentityResolver
Amia-plugin-send
Amia-plugin-profile
Amia-plugin-group-insight
```

Profile 和 Group Insight 可以在 Send 缺失时启动，但只能返回降级结果。

## 旧数据库迁移

旧表：

```text
msg_stats
private_stats
hourly_stats
traffic_stats
```

插件启动不会自动迁移旧库。检测到旧结构时，正常写入会停止，避免新旧结构继续混合。

### dry-run 预检

迁移前预检会检查：

- 数据库是否存在；
- `PRAGMA integrity_check`；
- 旧表是否存在；
- `*_legacy_bak` 是否已存在；
- 新旧表同时存在的部分迁移状态；
- 数据库绝对路径和文件大小。

以下情况应拒绝迁移：

- integrity check 不是 `ok`；
- 已存在 legacy backup 表；
- 检测到部分迁移状态；
- 当前数据库不是预期目标文件。

### 一致性备份

迁移前使用 SQLite backup API 创建：

```text
<data.db>.pre-v2.<timestamp>.bak
```

该方式会复制已提交的 WAL 内容。备份完成后还会执行完整性检查。

如果备份文件已存在、为空、无法创建或 integrity check 失败，迁移会停止。

### 数据语义

- `msg_stats` 和 `private_stats` 迁移到 `activity_daily`；
- `hourly_stats` 缺少可靠上下文，因此进入 `legacy_hourly_metrics`；
- `traffic_stats` 进入 `legacy_daily_metrics`；
- 不会为了填满新表而伪造群号或上下文。

### 迁移校验

迁移记录状态前会校验：

- 群消息总量；
- 私聊消息总量；
- 小时消息总量；
- 流量字节总量。

任一总量不一致都会抛出错误，不应把该次迁移标记为成功。

### 生产执行要求

现有代码已经具备主要安全预检和总量校验，但仍不代表可以未经演练直接修改生产库。

生产迁移前必须：

1. 停止 Bot 写入；
2. 对生产库执行 dry-run；
3. 在生产库副本上完成一次完整迁移；
4. 核对统计总量和主要排行；
5. 确认备份文件可以独立打开；
6. 准备明确回滚步骤；
7. 最后才对生产文件执行迁移。

当前不会在插件启动阶段自动执行迁移。

## 测试

```powershell
$env:PYTHONPATH = '<project-root>'
python -m unittest discover -s src/plugins/Amia-plugin-send/tests -v
```

应覆盖：

- SQLite 初始化和批量写入；
- 群统计作用域；
- 不同 `self_id` 的用户隔离；
- 日、月、年半开区间；
- canonical 身份归并；
- dead-letter 写入；
- dry-run 的完整性和部分迁移检测；
- SQLite 一致性备份；
- 群、私聊、小时和流量迁移总量；
- Provider 注册和消费；
- 失败后的数据库状态和回滚行为。

## 已知限制

- 生产迁移尚未在真实生产副本上完成演练；
- 跨群实例 DAU 依赖明确 AppID 和稳定用户 ID；
- 当前未提供完整 HealthProvider；
- 尚未完成所有插件同时加载的集成测试。

## 维护边界

- 不直接修改 Gensokyo idmap；
- 不用昵称作为身份主键；
- 不在启动阶段自动迁移生产数据库；
- 不把未验证实例 DAU描述成准确值；
- 不提交数据库、WAL、SHM、备份和 dead-letter；
- 当前仓库尚未确定公开许可证。
