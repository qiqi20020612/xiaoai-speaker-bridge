# Conversation 原执行链路与 HTTP 验证

普通语音在 KWS/STT 完成后通过 `listener-hooks.sh` 进入 `text.sh voice`；远程文字通过同一个脚本的 `send_text` 入口。原本地意图和音量规则先执行，只有未被处理的文字进入 Conversation。HA 集成仍只发起请求，不代替音箱执行 Conversation。

## 原 Agent 与缓存

`text.sh` 加载现有 listener 配置，再显式加载 `/data/eoai_agent.sh`，验证它确实定义了原 `curl()` 包装函数，保留 listener 已选的 Agent、缓存路径和期限。缺失时返回 `conversation_adapter_missing`，不会绕过它请求默认 HA Agent。listener 中其他同名函数也不能意外替代这个入口。

向 HA Conversation 发出的 JSON 仍显式设置原配置 `EOAIC2_AGENT_ID` 对应的 `agent_id`；原包装函数负责读原 `EOAIC2_STATE` 的 `{agent,id,ts}`，校验 Agent、时间及原 `EOAIC2_KEEP_SECONDS=300`，注入仍有效的 `conversation_id`，并保存 HA 返回的 ID 和完成时间。桥接没有另写一套上下文读取或滑动期限算法。

两种输入共用 session FD 锁。语音和文字共用原默认 EOAIC2 文件。Bridge 不再接受动态 Agent 覆盖，不重写 listener 的 `EOAIC2_AGENT_ID`，也不退回 HA 默认 Agent。`use_context=false` 将原函数的状态路径临时指向本次工作目录，结束后清除，不读取或延长任何持久会话。

成功必须得到带非空 Conversation ID 的有效回复。HTTP/网络错误、错误回复或无 ID 回复均不延长缓存；若原包装函数已经保存错误回复的 ID/时间，桥接在同一锁内恢复之前的内容。上下文处理结束后先释放 session 锁，再等 TTS 音频锁。原豆包 TTS、音量恢复、KWS/STT 都保留。

## 原令牌续期

保存的原实现中，`eoai_agent.sh` 负责 Agent/上下文，`ha_refresh_token()` 位于原 launcher。`conversation.sh` 只提取已审核形状的这个函数，不 source 或执行整个 KWS launcher。默认来源是 `/data/porcupine_launcher_kws`；成功分支保留原 `grant_type`、按原方式取得的 client ID、refresh token 和仅更新 listener `HA_TOKEN` 行的行为。响应临时文件改为本次私有工作目录，避免与原 STT 续期文件冲突；不在桥接配置或 HA 集成存储 OAuth token。

按需 curl 转发脚本只为原 Agent 的实际 Conversation 请求记录 HTTP 响应头，传出的 JSON、认证头与原回复输出不变。只有明确的 **401** 才调用原续期函数并重试 **一次**。断连、HTTP 500、其他错误及第二次 401 不会重复执行自然语言动作。续期失败返回当前任务失败，原函数中播放错误音/停止 listener 的分支不会在文本子进程执行。

现有配置无需迁移；本机高级字段 `eoai_agent_script`、`ha_auth_source` 可指定经审核的现有文件，默认值如上。未知续期函数形状会拒绝提取，需要审阅原脚本后适配，不会猜测 OAuth 参数、覆盖原配置或建立新的会话服务。

## 测试证据

`tests/test_text_pipeline.py` 已移除调用 Python 会话替身的 `curl()`。测试使用真实 curl 向临时本机 HTTP 服务发送请求；服务端记录原始 JSON 和请求头，不补 Agent/Conversation ID，也不写本机会话缓存。现有 Agent 的合成 shell 契约 fixture 负责与原文件相同的包装职责，所有网络请求仍由真实 curl 发出；公开 fixture 不含任何真机配置或原始文件副本。

例如有效缓存下收到的结构为（全部是测试数据）：

```json
{
  "text": "what is my name",
  "language": "zh-CN",
  "agent_id": "conversation.synthetic",
  "conversation_id": "synthetic-cached-id"
}
```

测试检查语音→文字→语音使用同一个 ID、成功后的滑动时间更新、300 秒/未来时间排除、旧覆盖记录不影响原 Agent、拒绝已删除参数、临时无上下文、缺包装函数拒绝请求、401 原续期表单及新认证头、同 ID 重试、续期失败、断连/500/错误回复不续期或重试，以及音量/TTS/音频锁回归。Linux CI 在真实 BusyBox 1.36.1 ash/flock 下运行，文本链路的 awk/sed 也使用真实 BusyBox applet；测试 HTTP 服务只属于测试环境，运行部署仍严格只有 HA 集成与 LX06 两端。本轮未连接音箱。

0.1.7 删除 `/api/conversation/agent`、HA Agent Select、配置条目 `agent_id` 和文字任务单次 `agent_id`。旧 Bridge `/tmp/xiaoai_bridge.agent.json` 及覆盖缓存不读取、不更新；保留残留原字节以便回退旧版本，随原临时目录自然清除，不增加清理分支。重置会话只清除原配置 `EOAIC2_STATE`，不再选择覆盖缓存。原生 HA 请求中的 `agent_id` 属于 EOAIC2 的必需协议，仍保留。
