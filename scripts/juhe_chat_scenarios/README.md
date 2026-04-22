# Juhe Chat Replay

这个目录配合 [`../juhe_chat_replay.py`](../juhe_chat_replay.py) 使用，用来做 Juhe 群聊/私聊的离线实战回放。

这次脚本的默认目标已经不是“只看 Juhe 适配器有没有吃到消息”，而是：

- 把 Juhe callback 真正送进 `GatewayRunner`
- 走正式的 session / transcript / pending context 注入链路
- 把消息真正“送给 agent”这一层也跑通
- 最后用 mock send 记录“本来会发回去什么”

默认模式下，它跑的是：

`Juhe callback -> JuheAdapter.handle_message -> GatewayRunner._handle_message -> _run_agent -> adapter.send`

区别在于：

- 默认 `agent_mode=mock`
  不真的调用线上模型，但会真的走到 `_run_agent(...)` 这层边界，并把 agent 输入、最终回复、transcript 都跑出来
- 可选 `agent_mode=real`
  真的调用当前 Hermes 配置里的模型和工具链

如果你只想做老式的“适配器入站验证”，也还保留了 `mode=adapter`。

## 1. 快速开始

在仓库根目录执行：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py scripts/juhe_chat_scenarios/group_shared_session.json
```

如果场景里的 `expect` 全部命中，退出码是 `0`；只要有一条断言不符，退出码就是 `1`。

把结果保存成文件：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json \
  --output /tmp/juhe-group-replay-result.json
```

查看帮助：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py --help
```

## 2. 当前默认行为

### 默认是完整 gateway 流程

如果你不写 `mode`，脚本会按 `gateway` 处理。

这表示：

- 真的进入 `GatewayRunner`
- 真的生成 session
- 真的写 transcript
- 真的触发 `pending_context` 注入
- 真的走回复发送逻辑
- 但回复发送会被 mock 掉，只记录，不会发给真实 Juhe

### 默认 agent 是 mock

如果你不写 `agent.mode`，默认是 `mock`。

这表示：

- 真的走到 `_run_agent(...)` 这层
- 但 `_run_agent` 返回的是场景定义的 mock 结果
- 这样可以验证完整链路，又不会真的消耗模型额度

### 轻量模式仍然可用

如果你只想验证“这条消息是否触发、session_key 是什么、群上下文有没有补进去”，可以写：

```json
{
  "mode": "adapter"
}
```

这时脚本只停在 Juhe 适配器层，不会进入 `GatewayRunner` 和 agent。

## 3. 现成样例

目录里现在有 4 个样例：

- [`group_shared_session.json`](./group_shared_session.json)
  验证“客户先说，员工 A `@bot` 触发，共享群 session，并真正把上下文送给 agent”
- [`group_non_trigger_at_bot.json`](./group_non_trigger_at_bot.json)
  验证“没有触发权限的人即使 `@bot` 也不会直接触发”
- [`dm_basic.json`](./dm_basic.json)
  验证 Juhe 私聊完整链路
- [`group_complex_requirement_flow.json`](./group_complex_requirement_flow.json)
  验证更长的客户群需求讨论流程，包含两次正式触发和方案更新

最简单的方式就是复制其中一个 JSON 改。

## 4. 场景文件结构

一个更贴近当前脚本默认行为的场景长这样：

```json
{
  "name": "group-shared-session",
  "description": "客户消息先进入群上下文池，员工 A @bot 后再触发",
  "mode": "gateway",
  "env": {
    "JUHE_HOME_CHANNEL": "R:2001"
  },
  "agent": {
    "mode": "mock"
  },
  "adapter_extra": {
    "app_key": "app-key",
    "app_secret": "app-secret",
    "guid": "guid-123",
    "group_policy": "allowlist",
    "group_allow_from": ["R:2001"],
    "trigger_user_ids": ["1001"],
    "group_sessions_per_user": false
  },
  "steps": [
    {
      "name": "customer backlog",
      "message": {
        "sender": "1002",
        "sender_name": "客户张三",
        "roomid": "2001",
        "id": "room-msg-1",
        "content": "Earlier customer context"
      },
      "expect": {
        "dispatched": false,
        "room_history_size": 1
      }
    },
    {
      "name": "authorized trigger user",
      "message": {
        "sender": "1001",
        "sender_name": "员工A",
        "roomid": "2001",
        "id": "room-msg-2",
        "at_list": ["bot"],
        "content": "请整理一下刚才的需求"
      },
      "agent": {
        "final_response": "收到，我来整理刚才群里确认的需求。"
      },
      "expect": {
        "dispatched": true,
        "agent_called": true,
        "session_key": "agent:main:juhe:group:R:2001",
        "outbound_count": 1,
        "pending_context_contains": "Earlier customer context",
        "agent_message_contains": [
          "[Recent room context]",
          "[员工A] 请整理一下刚才的需求"
        ],
        "final_response_contains": "群里确认的需求"
      }
    }
  ]
}
```

## 5. 顶层字段说明

### `name`

场景名，只用于结果展示。

### `description`

可选，写这组场景要验证什么。

### `mode`

可选，支持两个值：

- `gateway`
  默认值。走完整 gateway + agent 链路
- `adapter`
  只做 Juhe 适配器层验证

### `env`

可选。给这次回放临时注入环境变量。

最常见用途是压掉“未设置 home channel”的提醒，例如：

```json
{
  "env": {
    "JUHE_HOME_CHANNEL": "R:2001"
  }
}
```

### `agent`

可选。定义 agent 层如何表现。

常用字段：

- `mode`
  `mock` 或 `real`
- `final_response`
  mock 模式下的最终回复
- `result`
  更完整地覆盖 `_run_agent` 返回结构

### `adapter_extra`

这一块会直接作为 Juhe 适配器的 `PlatformConfig.extra` 注入，所以它决定了这轮回放的 Juhe 策略环境。

常用字段：

- `guid`
- `allow_from`
- `group_policy`
- `group_allow_from`
- `trigger_user_ids`
- `group_sessions_per_user`
- `room_log_limit`
- `pending_context_limit`

当前 Juhe 群触发规则是：

- 群本身要先通过 `group_allow_from`
- 发送者要在 `trigger_user_ids` 里
- 这条触发消息还要带 `at_list`，也就是测试时要显式写 `@bot`

如果你想测试“同一个消息序列，在不同群策略下会怎样”，通常改这里就够了。

### `steps`

按时间顺序写回放步骤。脚本会一条一条喂进去。

## 6. Step 写法

每个 step 至少包含：

- `name`
- `message` 或 `event`

### 推荐：`message`

最适合手工改。脚本会帮你补成标准 Juhe callback event。

示例：

```json
{
  "name": "employee reply",
  "message": {
    "sender": "1001",
    "sender_name": "员工A",
    "roomid": "2001",
    "id": "room-msg-2",
    "at_list": ["bot"],
    "content": "请整理一下刚才的需求"
  }
}
```

常用 `message` 字段：

- `sender`
- `sender_name`
- `roomid`
- `id`
- `content`
- `at_list`
- `msg_type`
- `content_type`
- `sendtime`

补充规则：

- 文本消息默认补 `msg_type=2`
- 如果没写 `id`，脚本会自动生成 `replay-step-<序号>`
- `roomid` 可以写 `2001`，也可以写 `R:2001`，脚本会自动规范化
- 没写 `roomid` 时，会当作私聊

### 高级：`event`

如果你想完全控制 Juhe callback payload，可以直接写完整事件。

示例：

```json
{
  "name": "raw callback",
  "event": {
    "guid": "guid-123",
    "notify_type": 11010,
    "data": {
      "msg_type": 2,
      "sender": "1001",
      "content": "hello"
    }
  }
}
```

### Step 级 `agent`

在 `gateway + mock` 模式下，你可以给某一步单独指定 agent 返回内容：

```json
{
  "agent": {
    "final_response": "收到，我来整理。"
  }
}
```

这非常适合做“同一条群消息，看看最终 transcript 和 outbound 长什么样”的回归测试。

## 7. `expect` 支持什么

`expect` 用来断言这一步是否符合预期。不命中时会在结果里列出失败原因。

当前支持：

- `dispatched`
  这一条是否进入正式消息处理链路
- `agent_called`
  这一条是否真正触发了 `_run_agent(...)`
- `session_key`
  最终命中的 session key
- `room_history_size`
  当前群滚动窗口消息数
- `outbound_count`
  本轮 mock 出站消息数
- `pending_context_contains`
  断言补入的临时群上下文包含某段文本
- `agent_message_contains`
  断言真正送给 agent 的 message 包含某段文本
- `final_response_contains`
  断言最终回复包含某段文本

例如：

```json
{
  "expect": {
    "dispatched": true,
    "agent_called": true,
    "session_key": "agent:main:juhe:group:R:2001",
    "pending_context_contains": "Earlier customer context",
    "agent_message_contains": "[Recent room context]",
    "final_response_contains": "群里确认的需求"
  }
}
```

`pending_context_contains`、`agent_message_contains`、`final_response_contains` 都可以是字符串，也可以是字符串数组。

## 8. 输出怎么看

脚本输出是 JSON。最关键的字段如下。

### 顶层

- `scenario`
- `mode`
- `agent_mode`
- `all_expectations_passed`
- `hermes_home`
- `steps`

### 每个 step

- `handled`
  这条 callback 是否被正常处理
- `dispatched`
  是否进入正式处理链路
- `agent_called`
  是否真正调用了 `_run_agent(...)`
- `conversation_id`
  当前会话对象，例如 `R:2001` 或 `S:1001`
- `session_key`
  命中的 session key
- `agent_message`
  真正发给 agent 的文本
- `context_prompt`
  本轮构造给 agent 的上下文 prompt
- `final_response`
  agent 最终返回的回复
- `outbound`
  本轮 mock send 记录
- `pending_context`
  本轮补进 agent 的群临时上下文
- `room_history_tail`
  最近几条群原始消息摘要
- `transcript_tail`
  最近几条 transcript 摘要
- `memory_updates`
  本轮是否触发了群 memory 更新调度
- `failures`
  断言失败原因

## 9. 两种模式怎么选

### 想看“完整实战链路”

用默认值，也就是：

```json
{
  "mode": "gateway",
  "agent": {
    "mode": "mock"
  }
}
```

适合你现在的目标：确认消息到底有没有真正送到 agent，以及最终本来会回什么。

### 想只看 Juhe 入站策略

用：

```json
{
  "mode": "adapter"
}
```

适合只验证：

- 群触发门槛
- session_key
- 群滚动窗口
- pending context 补入

### 想真的跑模型

可以用：

```json
{
  "mode": "gateway",
  "agent": {
    "mode": "real"
  }
}
```

或者命令行覆盖：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json \
  --agent-mode real
```

注意这会真的使用当前 Hermes 的模型配置，也可能真的走工具调用，所以成本和副作用都更高。

## 10. 最常见的实战场景

### 1. 验证“客户先说，员工后触发”

从 [`group_shared_session.json`](./group_shared_session.json) 改。

通常只需要：

- 改 `group_allow_from`
- 改 `trigger_user_ids`
- 在正式触发那一步补上 `at_list`
- 按真实说话顺序改 `steps`
- 给触发步写一个你想要的 `agent.final_response`

### 2. 验证“非触发人 @bot 也不回”

从 [`group_non_trigger_at_bot.json`](./group_non_trigger_at_bot.json) 改。

### 3. 验证私聊完整链路

从 [`dm_basic.json`](./dm_basic.json) 改。

### 4. 验证更复杂的群内需求讨论

从 [`group_complex_requirement_flow.json`](./group_complex_requirement_flow.json) 改。

这个样例更适合你现在想看的“问题长一些、流程复杂一些”的情况：

- 多条客户消息先进入上下文池
- 一次正式触发输出结构化方案
- 后面又来了新的限制条件
- 再次正式触发，让 agent 更新 v2 方案

## 11. 容易踩坑的地方

### 私聊默认不是全开放

Juhe 私聊默认 `dm_policy=allowlist`。所以测私聊时，通常要在 `adapter_extra.allow_from` 里把用户加进去，否则消息会被正常拦掉，看起来像“没有 dispatch”。

### 群触发和群准入是两回事

不要混淆：

- `group_allow_from`
  决定这个群能不能接入系统
- `trigger_user_ids`
  决定这个群里谁能直接触发回复

一个群即使允许接入，如果发言人不在 `trigger_user_ids` 里，也仍然可能只进入上下文池。

并且当前 Juhe 群正式触发还要求这条消息带 `at_list`。也就是说，触发用户没 `@bot` 时，消息也会先进入上下文池，不会立刻 dispatch。

### `roomid=0` 会被当作私聊

如果你想测群，`roomid` 不要写成 `0`。

### 没配 home channel 会多一条提示

在 `gateway` 模式里，如果没有 `JUHE_HOME_CHANNEL`，Hermes 会像真实运行时那样先发一条“未设置 home channel”的提醒。

如果你不想让这条干扰测试结果，就在场景顶层加：

```json
{
  "env": {
    "JUHE_HOME_CHANNEL": "R:2001"
  }
}
```

### `mock` 不等于“没送给 agent”

默认 `agent.mode=mock` 的意思是：

- 真的送到 `_run_agent(...)` 这一层
- 但 `_run_agent` 的返回值是你在场景里定义的 mock 结果

所以它已经能验证：

- agent 输入长什么样
- session transcript 怎么落
- 最终 outbound 会发什么

只是不会真的调用外部模型。

## 12. 建议你的使用方式

最实用的方式不是只保留 1 个样例，而是按你业务里最关键的群聊模式各建一个 JSON。

例如：

- `customer_group_employee_a_trigger.json`
- `customer_group_employee_b_trigger.json`
- `customer_group_non_trigger_mentions_bot.json`
- `customer_group_requote_requirement.json`
- `dm_sales_followup.json`

这样以后每次改 Juhe 群聊策略，你只要批量回放这些 JSON，就能很快知道有没有改坏。

## 13. 常用命令

默认完整链路：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json
```

强制走适配器模式：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json \
  --mode adapter
```

强制走真实 agent：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json \
  --agent-mode real
```

保存结果：

```bash
cd /Users/chou/.hermes/hermes-agent
./.venv/bin/python scripts/juhe_chat_replay.py \
  scripts/juhe_chat_scenarios/group_shared_session.json \
  --output /tmp/group-shared-session-result.json
```
