# 协议与安全行为

桥接协议 `1`、软件版本 `0.2.0`（沿用 v0.1.9 的协议和配置语义）。IPv4 HTTP 默认 `8766`，随机 Bearer token 必填；配置 `0600`、持久 UUID 稳定。白名单最多 8 个 IPv4/CIDR，空数组表示允许所有可达地址；建议限制为 HA 地址。回环用于本机健康检查。HTTP 仅用于可信 LAN，部署者负责禁止公网端口映射。

## 设备接口

| 方法/路径 | 请求/结果 |
|---|---|
| `GET /api/info` | 协议、设备 UUID、通用型号、软件版本、实际 capabilities；不返回密钥 |
| `GET /api/state` | 更新时间、stale 标志、独立音量、真实 AirPlay/蓝牙状态、voice 来源快照、资源、instance ID |
| `GET /api/playback` | 同一只读快照；MPD 全部业务状态由 HA 直连协议补齐 |
| `GET /api/airplay/artwork/ID` | 认证后只读当前受限封面 ID；PNG/JPEG，最多 2 MiB；旧/缺失/不安全图片 404 |
| `GET /api/health` | 首次成功 probe 后才 ready；未就绪时 HTTP 503 携带 `probe_status`、`probe_attempt`、`probe_running`、`startup_failed`，不回显配置 |
| `GET /api/events?since=N` | 25 秒长轮询，返回单调 sequence；重启/较大旧游标产生 reset |
| `POST /api/policy` | HA 当前来源/不确定状态的小快照，不在音箱运行 HA 规则引擎 |
| `POST /api/volume` | `request_id,target,level`；只接受明确硬件目标，`auto/all` 由 HA 先解析 |
| `POST /api/playback/control` | `request_id,source,action`；只路由到真实 AirPlay/BlueZ；MPD 由 HA 直连 |
| `POST /api/tts/announce` | `request_id,text[,volume]`；仅朗读 |
| `POST /api/conversation/text` | `request_id,text[,speak_response,use_context]`；LX06 执行原处理入口 |
| `GET /api/jobs/ID` | accepted/running/synthesizing/playing/completed/failed/cancelled；可返回回复文字/错误 |
| `POST /api/jobs/ID/cancel` | 取消播报或文字对话的 synthesizing/playing 尾段；意图/Conversation 执行中仍拒绝取消 |
| `POST /api/listener` | 固定 start/stop/restart，语音活动时拒绝；start/stop 保持目标语义，仅状态不符时调用原生 toggle，验证目标/Adapter 就绪；restart 仍静默 |
| `POST /api/conversation/reset` | 锁定共享会话后只重置原 EOAIC2_STATE |
| `POST /api/bridge/restart` | `{}`；202 仅表示已调度，只调用 start.sh restart 并跳过 Listener；不入任务队列 |
| `POST /api/ir` | 有限、类型校验的红外字段；需要验证标志和真实接口 |

v0.1.9 的取消不会撤销已经执行的指令或清除已更新的上下文。HA 只有未派发或明确被 busy 拒绝的任务可本地取消，其余情况必须调用音箱端；丢失确认时按同一 request_id 查询，不重新执行。`cancelled` 确认取消请求，`audio_active` 在清理完成前保持，避免新任务抢占尚未恢复的资源；同组残留进程在 worker 被回收前停止。重复取消不延长原有清理宽限。具体行为与验证边界见 [v0.1.9 修复记录](lx06-v019.md)。

除 `/api/policy`、任务取消和 Bridge 重启外，控制请求都需要 `[A-Za-z0-9_-]{1,64}` request ID。同 ID、同接口及同请求体在最近 16 条记录窗口内返回已有状态；不同请求返回 `request_id_conflict`。音箱重启后记录消失，**不能把重发当跨重启 exactly-once**。HA 任务队列使用运行期语义摘要并在完成后清空 payload；v0.1.9 的两个原生文字实体成功入队后清空输入，经 HA `logbook.log` 为对应实体记录本次完整原文，沿用用户 Recorder/Logbook 配置。这只改变 HA 交互，不改变音箱协议。

## 有界执行

AirPlay 的可选 song/status/progress/artwork_id 由现有 Probe 的同一份 RemoteControl.GetAll 回复生成；不加入原生唤醒快照，不增加 D-Bus 查询。图片只按需分段读取，HA 每设备缓存一张并沿用现有更新清理。路径、类型、失效和进度精度见 [AirPlay 元数据](airplay-metadata.md)。

- 8 个 HTTP 连接，3 个 worker 槽：一个低频 probe、一个快速控制、一个播报/对话。音箱无一般任务队列。
- 文本最大 4096 UTF-8 字节，HTTP body 最大 12287 字节，header 最大约 4 KiB；回复任务结果最大 8191 字节，超出明确失败，不伪装成功。
- 严格 JSON，重复 key、无效 UTF-8、嵌入 NUL、过深结构拒绝；HTTP 不支持 chunked、重复 Content-Length、请求流水线或任意 Shell 路由。
- 认证请求限流为 burst 40、补充 10 次/秒。连接读取有超时，长轮询每 25 秒返回，子任务最长 180 秒。
- 启动脚本与二进制均有 flock 多实例保护。worker 用固定 argv 执行受信本机脚本，请求经 stdin JSON/安全参数传递，不拼接命令。
- 任务超时/取消只终止自己派生的进程组，先 TERM，最多 20 秒清理宽限后才强制结束，不碰原共享服务。
- probe 最长 15 秒；首次失败后在 1、2 秒后重试，启动最多三次快速尝试，之后恢复低频 15 秒间隔。启动脚本的单调等待预算为 75 秒，每次 CLI 健康请求共用一个 2 秒网络截止时间，HTTP 503 不会被当成健康成功。

本机 `--health` 的退出码为：`0` 已就绪，`1` HTTP 不可达/无响应/拒绝/响应无效，`3` 首次 probe 等待中，`4` probe 已失败但仍在重试，`5` 首次三次尝试已耗尽。旧版本未提供探测诊断的 503 按等待中处理，兼容已有实例检查。CLI 不输出响应正文或认证头。

## HA 内部编排

每设备最多 32 条待执行任务，保留最近 16 条完成记录。`urgent` 只提高排队优先级，不抢占原语音。正常唤醒优先，可取消桥接自己启动的播报，原语音在录音前保留音频锁；远程 Conversation 完成会话更新后释放 session 锁，再等待原语音音频锁，避免双方互相等待。只有收到明确 `busy` 且设备尚未接收时才有界等待后重试；POST 回应不确定时，仅按 ID GET 查询，绝不再发一次可能执行过的 Conversation。HA/设备重启、窗口过期或网络无法确认时状态为 `unknown`，由操作者核对，不能自动重复执行。

HA 动作包括 `announce`、`send_text`、`get_job`、`cancel_job`、`set_volume`、`volume_up/down`、`get_queue`、`send_ir`。指定一台设备的 `device_id` 或实体 `entity_id`。`wait=true` 等待实际完成，支持动作 response data；默认立即返回 accepted 和 request ID。

音箱原语音向 HA 集成的认证 `voice_guard` view 登记短期文本指纹；HA 已排队/执行的远程文字也记录指纹，阻止同一活跃文字的自动化回注。guard 最多 8 条，最长 240 秒，结束时清除，不记录原文到诊断。不能阻止用户自己创建任意异文无限循环，自动化仍应避免自触发。

## 音量与会话协调

HA `Store` 持久保存成功确认的期望值。bridge 重启后，HA 按新 instance ID 恢复可写通道，失败则保留等待后续有界刷新。空闲稳定状态可更新持久值；录音/桥接播报期间的临时音量不被保存为新期望值。

唤醒前记录活动来源集合及主/蓝牙音量。HA 选择优先级，TTS 不能覆盖快照。LX06 用每通道版本号保存本轮新期望；录音时主/蓝牙仍衰减，恢复在同一硬件音量锁内读取最新期望并应用，随后清除 voice marker，避免恢复与新指令竞争。TTS 临时音量也在相同锁内保存/恢复；取消时仍进行恢复。

主动播报及有声文字回复由音箱端自动避让唯一、确定活跃的 AirPlay，master 高于 10% 时临时降到 10%，低音量不提高。使用同一套 Journal/版本恢复与 duck→voice 交接，不修改发送端音量或重复衰减原生语音。多源、任何未确认音源、无 TTS 时跳过；原 HA 避让选项与专用验证标记不再使用。旧协议数值 duck_volume 仅为过渡兼容而校验并忽略，不作为可配置目标。

Probe 使用同一 Volume Lock 非阻塞采样本地 mixer 与三个 Journal 是否存在，随后释放锁再查询 DBus/BlueZ。采样时任何 Journal（包括活跃 owner）存在，volume_recovery_pending 都为 true，禁止 HA 将该快照的临时音量导入持久期望；任务在后续查询期间恢复也不能使旧样本变可信。锁忙时本次本地音量为 null、该标志为 true，能力保持，按原 Probe 周期重新观察；不等待语音或新增重试任务。

AirPlay 原始 dB ≤ -144 映射静音 0%；其余钳制到 [-30,0] 并线性映射至 [0,100]。这是控制百分比，不是响度百分比；反向 0% 为 -144，其余 `0.3*percent-30`。原 Shairport 衰减曲线不变；发送端同步未实测前反向接口关闭。`all` 在 AirPlay 活跃时不写 master，能力不足时预检失败。

语音与远程文字共享本机 session flock、原 Agent 函数和原 `{agent,id,ts}` 缓存。成功按原五分钟滑动规则更新；失败回复回滚误更新，`use_context=false` 使用临时独立缓存；Agent 固定采用原 Listener/EOAIC2 配置。原 OAuth token 从现有 listener 加载，桥接配置不存 HA 长期 token。`announce` 不进入上述 Conversation 阶段。

原 Agent 显式加载并验证函数入口，原函数注入有效的 Conversation ID。仅明确 HTTP 401 复用原 launcher 续期并重试一次；其他错误不重试。具体调用、令牌来源及真实 HTTP 请求测试见 [Conversation 链路](conversation.md)。

## 能力与诊断

ALSA 按实际控件、DBus 按实际属性、蓝牙按实际对象和播放器检测。不存在/失败的状态为 null/unknown，不回填假 false；反向操作需要 `verified` 和运行时可用性。缺失依赖时 stale 标志使实体不可用。原生任务灯效不维护手动 RGB 状态。

诊断导出使用字段白名单，只含通用能力、版本、资源、任务状态和队列数量；不含 IP、连接名称、设备 UUID、HA/bridge token、原配置、对话文本、回复或会话 ID。桥接运行日志只在 `/tmp`，C 与启动/安装脚本仅记录固定事件、时间、尝试次数及数字错误码，运行过程中也检查轮转；当前日志和一份 `.log.1` 各最多约 64 KiB。worker 的原始 stdout/stderr 均不复制到日志。

## 原生任务灯效

任务自动调用原生 `show_led 1` → `show_led 2`，合成/播放阶段保持原生处理动画，最后 `show_led 11` 和 `shut_led 1/2/3` 收尾。无回复、失败、取消、超时均走同一清理。语音优先，任务使用原有 LED 锁及 PID/starttime 所有权；已审核的 AirPlay Guard 在任务/语音期间退让，结束后重放原音量灯。缺少原生命令时跳过灯效。

不再提供 `/api/led`、`capabilities.led` 或 `state.led`，合法认证旧控制请求返回 404。旧配置中的两个手动/任务灯效验证字段被忽略，协议仍为 1，其他可选能力及原服务保持兼容。

0.1.7 不再提供动态 Agent API（404）、单次 `agent_id`（400 `unsupported_agent_override`）或空闲策略服务。HA 动作的两个 Conversation 标志默认 true，仍可显式 false。空闲 auto 固定 master；活动音源的路由、显式通道和双重衰减保护不变。`system` 仅保留 `memory_available`、`bridge_rss`、`cpu_load`，删除 display-only conversation.remaining、system.uptime/memory_total/bridge_peak_rss 和 worker_peak_rss_kb。健康与任务接口不变。

Bridge 重启使用独立短寿命调用者和 runtime restart flock，返回接受确认后调用现有 start.sh，强制 `XIAOAI_SKIP_LISTENER_START=1`。正在执行的任务、语音/会话锁、安装事务会拒绝或阻止重启；连续请求共享同一次调度。接受之后的脚本失败只写既有启动诊断，不承诺重启成功，不进行长时间任务查询。
