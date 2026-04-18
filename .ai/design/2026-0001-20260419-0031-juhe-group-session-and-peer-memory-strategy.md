# 2026-0001

## 1. Metadata
- Design ID: `2026-0001`
- Created At: `2026-04-19 00:31 +0800`
- Status: `draft`
- Related Area: `backend`

## 2. Original Question
> 仔细看代码，我新增了以下能力，基于上面的，你设计几个群对话，私聊，存储等方案，可以是重构，也可以是改进；
> 结果
> qwsaas 这边新增了 Juhe 能力 wrapper，覆盖群 @、引用、撤回、会话已读、内部消息已读、联系人同步/搜索/批量详情、群列表/详情/成员/增量同步、标签同步、消息同步。核心改动在 messages.py、contacts.py、rooms.py、tags.py、sync.py、init.py。
>
> Hermes 这边新增了 Juhe 本地缓存层 juhe_cache.py，并把 juhe.py 升级为：
>
> 连接后执行启动同步
> 用回调实时 upsert 联系人/群最小目录
> 记录最近消息索引，包含 message_id / appinfo / refer_id / conversation_id / seq
> 对 refer_id != 0 的二次回调只索引不投递
> 用 sync_msg 做断线补偿时，新的用户消息会重新走现有 inbound 流程，已投递过的不重复回复
> 目录和工具也接好了：channel_directory.py 现在会直接读 Juhe 缓存来给 send_message(action="list") 提供真实联系人和群名；新加的 juhe_tool.py 提供 Juhe 专用 action 工具；toolsets.py 也把它纳入现有 messaging 工具集。
>
> 验证

## 3. Original Requirements
> 选择1
> 并且可以不可以给每个群和每个好友如果有聊天做一个持久记忆，用md或者什么形式都好，模仿openclaw的记忆或者灵魂md这种

## 4. Context

## 5. Problem Statement

## 6. Goals

## 7. Non-goals

## 8. Constraints

## 9. Options Considered

## 10. Chosen Design

## 11. Implementation Plan (high-level before confirmation)

## 12. Risks and Rollback

## 13. Validation and Test Plan

## 14. Open Questions

## 15. Outcome (fill after implementation)

## 16. Deviations from Design (fill after implementation)

## 17. Validation Results (fill after implementation)
