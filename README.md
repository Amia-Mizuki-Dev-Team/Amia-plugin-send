# Amia-plugin-send

`Amia-plugin-send` 是 Mizuki Bot 的消息活动统计插件。

它负责监听 OneBot V11 消息事件、保存群聊与私聊活动数据，并通过 `amia-core` 注册 `StatsProvider("send")`，供个人资料、群活跃分析和管理报表使用。

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
- 用户最近活动统计；
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
- 检测旧数据库结构后停止写入，不自动修改生产数据。

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
- merged DAU：使用 canonical 映射归并绑定前后记录，检测到冲突时返回不可用。

所有日期范围使用半开区间：

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

关键配置说明：

- `AMIA_SEND_BOT_APP_ID` 必须显式配置，插件不猜测本地配置文件；
- `AMIA_SEND_CROSS_CONTEXT_USER_ID_STABLE` 只有确认 Gensokyo 跨群 ID 稳定时才能启用；
- `AMIA_TIMEZONE` 决定自然日、月和年的边界；
- dead-letter 默认与数据库放在同一目录。

## 数据库

默认数据库：

```text
<插件目录>/data.db
```

主要表：

```text
activity_daily
activity_hourly
legacy_daily_metrics
schema_migrations
```

运行时使用：

- WAL；
- busy timeout；
- 批量 upsert；
- 有界写入队列。

以下文件属于运行数据，不应提交：

```text
data.db
data.db-wal
data.db-shm
*.dead-letter.jsonl
```

## 写入失败处理

数据库写入会有限重试。重试失败或队列溢出时，记录会写入：

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

消费者不应直接读取这些内部状态，后续可通过 HealthProvider 暴露必要诊断信息。

## amia-core 对接

插件启动后注册：

```python
registry.register_stats_provider(
    "send",
    activity_service,
    replace=True,
)
```

消费者通过：

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

调用方必须使用 `call_provider_safe()`，处理 Provider 缺失、异常和超时，不应直接查询 Send 的 SQLite。

### Profile 对接

`Amia-plugin-profile` 使用 `get_user_activity()` 获取最近消息量。

### Group Insight 对接

`Amia-plugin-group-insight` 使用 `get_group_activity_summary()` 获取指定群的消息总数和活跃成员数。

## IdentityResolver 对接

Send 可以调用 `amia-core` 中已注册的 IdentityResolver，把：

```text
self_id + gensokyo_user_id
```

解析为：

```text
canonical_user_id
```

Resolver 不存在、超时或失败时，消息记录仍可写入，只是不包含 canonical ID。

Send 不直接修改 Gensokyo idmap，也不把昵称作为身份主键。

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

以下旧表不会在启动阶段自动迁移：

```text
msg_stats
private_stats
hourly_stats
traffic_stats
```

检测到只有旧结构时，插件会停止写入并保留原数据。

生产迁移启用前必须完成：

- 文件级备份；
- `PRAGMA integrity_check`；
- 部分迁移状态识别；
- 已存在备份表时拒绝继续；
- 迁移前后消息总量校验；
- 失败注入和完整回滚测试。

这些条件未完成前，不要对生产 `data.db` 执行迁移。

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
- 旧库检测和迁移预检；
- Provider 注册和消费。

## 已知限制

- 生产数据库迁移尚未达到可执行标准；
- 跨群实例 DAU 依赖明确 AppID 和稳定用户 ID；
- 当前未提供完整 HealthProvider；
- 尚未完成所有插件同时加载的集成测试。

## 维护边界

- 不直接修改 Gensokyo idmap；
- 不用昵称作为身份主键；
- 不自动迁移生产数据库；
- 不把未验证实例 DAU 描述成准确值；
- 不提交数据库、WAL、SHM 和 dead-letter；
- 当前仓库尚未确定公开许可证。