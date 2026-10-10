# 协议与安全行为

桥接协议 `1`、软件版本 `0.1.0`。IPv4 HTTP 默认 `8766`，随机 Bearer token 必填；配置 `0600`、持久 UUID 稳定。白名单最多 8 个 IPv4/CIDR，空数组表示允许所有可达地址；建议限制为 HA 地址。回环用于本机健康检查。HTTP 仅用于可信 LAN，部署者负责禁止公网端口映射。

## 设备接口

| 方法/路径 | 请求/结果 |
|---|---|
| `GET /api/info` | 协议、设备 UUID、通用型号、软件版本、实际 capabilities；不返回密钥 |
| `GET /api/state` | 更新时间、stale 标志、独立音量、真实 AirPlay/蓝牙状态、voice 来源快照、资源、instance ID |
| `GET /api/playback` | 同一只读快照；MPD 全部业务状态由 HA 直连协议补齐 |
| `GET /api/health` | 首次成功 probe 后才 ready，依赖状态在 `/api/state`；不回显配置 |
| `GET /api/events?since=N` | 25 秒长轮询，返回单调 sequence；重启/较大旧游标产生 reset |
| `POST /api/policy` | HA 当前来源/不确定状态的小快照，不在音箱运行 HA 规则引擎 |
| `POST /api/volume` | `request_id,target,level`；只接受明确硬件目标，`auto/all` 由 HA 先解析 |
| `POST /api/playback/control` | `request_id,source,action`；只路由到真实 AirPlay/BlueZ；MPD 由 HA 直连 |
| `POST /api/tts/announce` | `request_id,text[,volume]`；仅朗读 |
| `POST /api/conversation/text` | `request_id,text[,speak_response,use_context,agent_id]`；LX06 执行原处理入口 |
| `GET /api/jobs/ID` | accepted/running/synthesizing/playing/completed/failed/cancelled；可返回回复文字/错误 |
| `POST /api/jobs/ID/cancel` | 取消自己启动的播报；执行中的对话拒绝取消 |
| `POST /api/listener` | 固定 start/stop/restart，语音活动时拒绝 |
| `POST /api/conversation/reset` | 锁定共享会话后只重置当前 Agent 状态 |
| `POST /api/conversation/agent` | 安全校验 Agent 或 null 恢复原 Agent；不会改原配置 |
| `POST /api/led`、`POST /api/ir` | 有限、类型校验的硬件字段；需要验证标志和真实接口 |

所有控制请求都需要 `[A-Za-z0-9_-]{1,64}` request ID。同 ID、同接口及同请求体在最近 16 条记录窗口内返回已有状态；不同请求返回 `request_id_conflict`。音箱重启后记录消失，**不能把重发当跨重启 exactly-once**。HA 使用自己的运行期语义摘要，完成后不保留指令原文。

## 有界执行

- 8 个 HTTP 连接，3 个 worker 槽：一个低频 probe、一个快速控制、一个播报/对话。音箱无一般任务队列。
- 文本最大 4096 UTF-8 字节，HTTP body 最大 12287 字节，header 最大约 4 KiB；回复任务结果最大 8191 字节，超出明确失败，不伪装成功。
- 严格 JSON，重复 key、无效 UTF-8、嵌入 NUL、过深结构拒绝；HTTP 不支持 chunked、重复 Content-Length、请求流水线或任意 Shell 路由。
- 认证请求限流为 burst 40、补充 10 次/秒。连接读取有超时，长轮询每 25 秒返回，子任务最长 180 秒。
- 启动脚本与二进制均有 flock 多实例保护。worker 用固定 argv 执行受信本机脚本，请求经 stdin JSON/安全参数传递，不拼接命令。
- 任务超时/取消只终止自己派生的进程组，先 TERM，最多 5 秒后清理，不碰原共享服务。

## HA 内部编排

每设备最多 32 条待执行任务，保留最近 16 条完成记录。`urgent` 只提高排队优先级，不抢占原语音。正常唤醒优先，可取消桥接自己启动的播报，原语音在录音前保留音频锁；远程 Conversation 完成会话更新后释放 session 锁，再等待原语音音频锁，避免双方互相等待。只有收到明确 `busy` 且设备尚未接收时才有界等待后重试；POST 回应不确定时，仅按 ID GET 查询，绝不再发一次可能执行过的 Conversation。HA/设备重启、窗口过期或网络无法确认时状态为 `unknown`，由操作者核对，不能自动重复执行。

HA 动作包括 `announce`、`send_text`、`get_job`、`cancel_job`、`set_volume`、`volume_up/down`、`set_idle_volume_policy`、`get_queue`、`send_ir`。指定一台设备的 `device_id` 或实体 `entity_id`。`wait=true` 等待实际完成，支持动作 response data；默认立即返回 accepted 和 request ID。

音箱原语音向 HA 集成的认证 `voice_guard` view 登记短期文本指纹；HA 已排队/执行的远程文字也记录指纹，阻止同一活跃文字的自动化回注。guard 最多 8 条，最长 240 秒，结束时清除，不记录原文到诊断。不能阻止用户自己创建任意异文无限循环，自动化仍应避免自触发。

## 音量与会话协调

HA `Store` 持久保存成功确认的期望值。bridge 重启后，HA 按新 instance ID 恢复可写通道，失败则保留等待后续有界刷新。空闲稳定状态可更新持久值；录音/桥接播报期间的临时音量不被保存为新期望值。

唤醒前记录活动来源集合及主/蓝牙音量。HA 选择优先级，TTS 不能覆盖快照。LX06 用每通道版本号保存本轮新期望；录音时主/蓝牙仍衰减，恢复在同一硬件音量锁内读取最新期望并应用，随后清除 voice marker，避免恢复与新指令竞争。TTS 临时音量也在相同锁内保存/恢复；取消时仍进行恢复。

AirPlay 原始 dB ≤ -144 映射静音 0%；其余钳制到 [-30,0] 并线性映射至 [0,100]。这是控制百分比，不是响度百分比；反向 0% 为 -144，其余 `0.3*percent-30`。原 Shairport 衰减曲线不变；发送端同步未实测前反向接口关闭。`all` 在 AirPlay 活跃时不写 master，能力不足时预检失败。

语音与远程文字共享本机 session flock、原 Agent 函数和原 `{agent,id,ts}` 缓存。成功按原五分钟滑动规则更新；失败回复回滚误更新，`use_context=false` 使用临时独立缓存，Agent 覆盖使用独立缓存（最多 8 个覆盖上下文）。原 OAuth token 从现有 listener 加载，桥接配置不存 HA 长期 token。`announce` 不进入上述 Conversation 阶段。

## 能力与诊断

ALSA 按实际控件、DBus 按实际属性、蓝牙按实际对象和播放器检测。不存在/失败的状态为 null/unknown，不回填假 false；反向操作需要 `verified` 和运行时可用性。缺失依赖时 stale 标志使实体不可用。LED 仅报告命令回执，voice 接管后清除旧状态。

诊断导出使用字段白名单，只含通用能力、版本、资源、任务状态和队列数量；不含 IP、连接名称、设备 UUID、HA/bridge token、原配置、对话文本、回复或会话 ID。运行日志只在 `/tmp`；固定启动日志有界轮转，worker stderr 不写入持久日志。
