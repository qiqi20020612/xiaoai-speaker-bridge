# LX06：v0.1.7 发布基线与 v0.1.8 技术交接

本文件供没有旧聊天上下文的新会话使用。它记录冻结于 v0.1.7 的实际实现与防回归理由，**没有实施或批准 v0.1.8 功能优化**。先读根目录 [AGENTS.md](../AGENTS.md)，再按下列源码入口核对当前任务；未来实现变化后应同步维护交接，不能把历史描述当永久 API。

## 1. 基线、证据和操作边界

| 项目 | 已核实事实 |
|---|---|
| 当前发布源码 | `v0.1.7`，Commit `fa019c92ca041badaa2a434fce3721e146dd2f77` |
| Tag 对象 | 注释 Tag 对象 `e007ebdbbd2049f7d06d07a7607a46952f8c3fca`；它不是 Commit SHA，解引用后为上一行提交 |
| 合并 | [PR #6](https://github.com/qiqi20020612/xiaoai-speaker-bridge/pull/6) 已于 2026-10-15 合并，包含原生 LED、实体/Agent/策略精简及重启交接修复 |
| 正式发布 | [v0.1.7 Release](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases/tag/v0.1.7)，2026-10-15 发布，非草稿、非预发布 |
| 发布门禁 | [Tag 工作流](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions/runs/38712828814) 全部成功，实际 JUnit 为 430 项、0 失败/错误/跳过，包含发布 job |
| 本次文档工作 | 以该 Tag 创建文档分支；仅新增开发规范和交接，不改运行代码、测试、版本或设备，不合并文档 PR，不创建新 Tag/Release |

工作站可用 `git rev-parse 'v0.1.7^{commit}'` 核对基线。发布状态通过 GitHub 元数据和工作流验证，不能仅凭文件内 VERSION 判断。

证据分三类：

- **源码事实**：下文明确列出 v0.1.7 的文件/函数、默认周期和控制流，可直接核对。
- **部署背景**：用户使用 LX06、树莓派中文 KWS、Microsoft STT、原 EOAIC2、豆包 TTS 和原回退链；这些外部/私有实现没有完整放在公开仓库。用户已确认的 v0.1.6 功能和压力测试不等于 v0.1.7 或未来 v0.1.8 的全部真机验收。
- **缺失证据**：没有本次采集的 v0.1.7 真机阶段分布、有效 PCM 首帧、实际 CPU/内存竞争和最新固件参数。不得填写猜测的耗时或用历史口述数字替代测量。

公开 [结构样本](../tests/fixtures/README.md) 源于脱敏只读审计，保留 Launcher/KWS/AirPlay/TTS 的结构，不包含真实凭据；默认英语、STT provider、TTS 路径示例、文档网段和 `.invalid` 地址不代表当前真机配置。样本只有在隔离替换硬件命令和路径后才能执行，不得直接在 LX06 运行或当安装文件。原 EOAIC2 的测试替身也不是原设备脚本全文。

## 2. 架构与责任归属

### 2.1 启动与执行关系

| 模块 | 启动/执行者 | 状态、持久化和事件 | 同步/异步边界 |
|---|---|---|---|
| 树莓派中文 KWS | 既有树莓派部署自行启动；服务端不在此仓库，Bridge 不管理其生命周期 | LX06 `/data/kws_client` 传音频并接收 `READY`/`WAKE`；服务端命中时刻需其自身证据 | 识别在外部服务；客户端返回成功后 Launcher 才继续 |
| 原 Listener、KWS client/guard | 设备既有 Listener 服务/procd；`listener-service.sh` 在已启用审核适配时接管其启动命令；guard 每轮监督 client | 私有 `/data/listener`、原 Launcher、KWS 文件保留；PID/starttime 与 ready 标记在运行目录 | Listener 是长期循环；一次唤醒返回后顺序执行录音、STT、处理和恢复；guard 的 client 为子进程 |
| 私有 Listener 适配器 | `prepare_adapters.py` 在工作站生成，`activate.sh`/bootstrap 经审核启用 sibling | `adapter/review.json` 保存源/输出 SHA256 与兼容格式；不是认证配置 | 只 source 必要定义/配置，不再启动第二套 KWS 初始化；生成器拒绝未知锚点 |
| C Bridge | `start.sh`；既有 boot/Cron 调用 `xiaoai_boot.sh` 恢复 | 内存中的 Probe 缓存、voice 状态、sequence、instance_id、有限任务历史；`/tmp` 策略投影 | 单个 poll 事件循环；按需 fork Shell worker、HTTP/Unix datagram；不是另一套 STT/TTS 服务 |
| HA 集成/Coordinator | HA 配置条目 setup，平台实体共用一个 Coordinator | HA Store 持久化期望音量；ConfigEntry 保留配对/选项；队列/任务和快捷输入在内存 | asyncio 轮询、事件长轮询和队列 worker；服务提交任务，不用第二套网络链路 |
| AirPlay/BlueZ/MPD/ALSA | 原 Shairport Sync、蓝牙、MPD 等独立服务；原启动体系负责 | DBus、ALSA mixer、MPD 协议是设备事实；HA 决定路由；Snapclient/UPnP 等不由 Bridge 替换 | Probe 是后台观测；录音前另有本机实时快照；媒体操作按请求执行 |
| EOAIC2 Conversation/OAuth | Listener 加载原配置；`text.sh` 同步调用原 Agent 包装函数 | 原 `EOAIC2_STATE`/TTL；OAuth 更新原认证配置并与既有 Cron 锁协作 | 网络 Conversation 同步执行，session 锁保护缓存；仅明确 401 允许受控续期重试 |
| Microsoft STT、豆包/回退 TTS | STT 保留原 Launcher 管道；TTS 调用既有配置脚本 | 认证、声音、provider 参数仍归原设备/HA 配置 | 录音与 STT 上传是流式管道；TTS 脚本包含合成和播放，Bridge 等其结束 |
| 原生 LED/AirPlay Guard | Listener/远程任务按需调用原生命令；原 AirPlay 音量 Hook 和有界 worker | 同一 LED 锁、feedback 所有权、voice cycle/done 和最新 AirPlay 事件记录 | 原生命令调用会占用当前步骤；任务只在 begin/end 协调，不新增常驻渲染器 |

主要源码入口：

- 启动：[start.sh](../speaker/start.sh)、[xiaoai_boot.sh](../speaker/xiaoai_boot.sh)、[bootstrap.sh](../speaker/lib/bootstrap.sh)、[listener-service.sh](../speaker/lib/listener-service.sh)。
- 适配：[prepare_adapters.py](../speaker/tools/prepare_adapters.py) 的 `prepare_listener()` / `prepare_kws_guard()` / `prepare_airplay_led()`，配合 [adapter-check.sh](../speaker/lib/adapter-check.sh)。
- 本地语音：[listener-hooks.sh](../speaker/lib/listener-hooks.sh)、[source-snapshot.sh](../speaker/lib/source-snapshot.sh)、[text.sh](../speaker/lib/text.sh)。
- C 端：[bridge.c](../speaker/src/bridge.c) 的 `server()` / `dispatch()` / `worker_spawn()` / `finish_worker()` / `read_local()`；JSON 与音频 trace helper 在 [json_ops.c](../speaker/src/json_ops.c)。
- HA：[coordinator.py](../custom_components/xiaoai_speaker/coordinator.py)、[volume.py](../custom_components/xiaoai_speaker/volume.py)、[jobs.py](../custom_components/xiaoai_speaker/jobs.py)、[views.py](../custom_components/xiaoai_speaker/views.py)、[client.py](../custom_components/xiaoai_speaker/client.py)。

`xiaoai_boot.sh` 沿用已有每分钟 Cron 和启动后 uptime 门限，首次 boot 序列保留已有 Squeezelite 停止逻辑；boot 完成后的重试只恢复 Bridge，不重复执行整套音频启动。不能把这个既有例外扩大成每次 Bridge 重启都可重启音频服务。Listener 开关实体（key=`listener`）沿用 Listener 启停，和只重启 Bridge 的按钮是两个不同能力。

### 2.2 当前功能边界

完整 LX06 能力下为 14 个 HA 实体，仍有能力门控和用户禁用选择，并非要求 14 个全部显示为启用。清单与 Unique ID 见 [v0.1.7 说明](lx06-v017.md)，实际注册见各平台与 [__init__.py](../custom_components/xiaoai_speaker/__init__.py) 的 `RETIRED_ENTITIES`。

- Media Player 保持标准播放状态、`source`/`active_source`，没有 `activity`。
- 两个 TextEntity 提交即通过 `QuickControls.send()` 调用原 `announce`/`send_text` 服务，成功提交后清空；任务结束后可以再次发送相同文字。没有独立发送按钮。
- 服务保留 `speak_response`/`use_context`，默认均为 true；不保留对应内存开关。
- AirPlay 百分比 sensor 附加 `raw_volume`、`raw_volume_unit=dB`；保留 master/TTS/Bluetooth 三个 number。耳机/AirPlay 的已支持显式服务通道并未因滑块删除而删除。
- 删除了独立 Light/RGB、动态 Agent 覆盖、Agent/idle Select、idle policy 服务及展示专用诊断；不可从旧文档/测试恢复它们。
- `target=auto` 的空闲默认为 master，活动 AirPlay→AirPlay、蓝牙→Bluetooth、MPD→master；`target=all` 在 AirPlay 下避免同时改 AirPlay 软件音量和 master。原音源优先级仍可配置。

## 3. 原生语音执行顺序与录音关键路径

### 3.1 先区分一次性初始化

原 Launcher 定义函数并 source `/data/listener`/TTS 配置，按原机制选择录音命令、配置 LX06、检查 TLS 日期和 STT 设置，然后进入 KWS 循环。`prepare_listener()` 在原定义之后引入 hooks，guard 后调用 `bridge_listener_initialized()`，由实际 KWS 子进程启动后的 `bridge_listener_ready()` 发布版本 4 标记。

`hooks_ready()` 校验标记中的 PID/starttime、版本和 Listener 命令身份，不能仅凭 `/etc/init.d/listener status` 或某个存活 PID 判就绪。生成 sibling 保留原始顺序和配置覆盖；有效录音参数以实际展开的 `RECORD_COMMAND` 为准，不凭样本默认值猜测。

### 3.2 一次正常、非空 STT 的 20 个步骤

下表以生成器对 [LX06 Launcher 结构样本](../tests/fixtures/lx06_launcher.sh) 的变换及实际 helper 为依据；原始私有 Launcher 不在仓库。标为“录音前”的同步工作会阻塞后面的录音启动，即使其目的只是状态或安全协调。

| 步骤 | 实现、顺序及理由 | 对本轮录音的影响 |
|---|---|---|
| 1. KWS 成功 | 既有服务识别，client 接收 `WAKE`，清理其 rec/sox/nc/FIFO 后返回成功；guard 等待 client 退出。见 KWS 结构样本 | KWS 清理仍在 Listener 继续之前，远端命中到客户端接收不能当成同一时刻 |
| 2. Listener 处理结果 | KWS 成功后 `bridge_voice_detected()` 先创建 cycle、记录 `wake`；保留原 ubus/activated 日志，再到 `SAVED_VOL` 前的 hook | 原日志、ubus 也在录音前；`wake` 不是服务端识别时间 |
| 3. 预约语音所有权 | `bridge_voice_begin()`：`wake_setup_begin`，必要时 `bridge_recover_pending()`，写 `PID starttime` 的 voice 与 cycle 标记、安装退出 trap | 同步；失败不能继续无恢复依据地录音。预约必须早于后续音频锁 |
| 4. 原生唤醒灯 | 获得 LED 锁 FD 6，保存被抢占 feedback 身份，原 `show_led 1`，记录 `wake_led`；生成器移去原位置的重复 1 | 同步命令/锁等待；灯先反馈不代表录音已经启动 |
| 5. 音量基线与 Journal | 获 volume.lock，读 bluetooth/mysoftvol；`bridge_volume_journal()` 查询 AirPlay Active/Volume，原子创建语音 Journal；有 duck 时在同一事务取回原基线；`journal-fields` 读回后释放短期音量锁，记录 `wake_volumes` | 同步 amixer、DBus、进程启动、文件提交和锁等待均阻塞录音；绝不能保存衰减值当原值 |
| 6. 衰减前音源快照 | `source-snapshot.sh`：CLI `--snapshot` 读策略投影，Shairport DBus、CLI `--mpd-state` 读本地 MPD、BlueZ DBus；C `snapshot()` 合成活动列表/未知观测/优先级与基线音量，记录 `wake_snapshot` | 同步实时查询；必须在衰减及 TTS 之前，避免后续路由随播放状态改变 |
| 7. 通知 Bridge | CLI `--emit` 向本机 Unix datagram socket 发送 `voice_begin`，记录 `wake_event`；不等待 HA HTTP 应答 | 本地发送本身同步，发送失败按当前代码忽略；C 消费与 HA 展示异步 |
| 8. 获得音频锁 | 随后等待 audio.lock FD 8，保留语音优先；C `read_local()` 取消可让出的 announce 进程组，其清理交接 duck 基线；成功后才删除已接管 duck 记录，记录 `wake_audio_ready` | 同步等待，默认预算 120 秒是失败上限而非正常延迟目标；不能删锁换速度 |
| 9. 原录音前衰减 | 原 Launcher 读取 `SAVED_VOL` 并判断阈值，需要衰减时调用 `bridge_voice_attenuate()`；先原子写 applied 状态，再同步写 mysoftvol、bluetooth，记录 `wake_attenuated` | 仍在录音前；原遗留音量读取/条件也不能从分析中漏掉 |
| 10. 原提示音 | 保留 `miplay sound ${NOTIFY_SOUND}` 与原 `sleep 0.15`、相关 timing 日志 | 在录音前，实际调用是否等待及耗时需真机测；不以只提前提示音作为优化成果 |
| 11. 录音与 STT 流式上传 | 原管道入口改为 `bridge_record_response`，记录 `recording_ready` / `recording_launch`，执行原 `RECORD_COMMAND`，经 tee/ffmpeg 转换后流式 curl 到原 HA STT 路径 | 这些 phase 只定位软件调用。管道可以并发传输；进入命令、打开 ALSA、首块输出和实际采样不是一个时刻 |
| 12. 处理 STT | 原管道返回后 `show_led 2`，按原 JSON 解析/文本清理处理成功、空文本和错误；非空文本进入 `bridge_process_text()` | 已在录音之后；不要为性能目标误改 STT 或麦克风参数 |
| 13. HA 音量规则/本地 Intent | `text.sh voice` 在 session 锁下加载原配置/Agent，使用已冻结 `_snapshot`；voice_guard、HA `volume_plan`，未命中再本地 `/intent`，VolumeUp/Down 走原路由规则 | 都是 STT 后同步步骤。不是每次录音前必须等待的 HA 刷新 |
| 14. 原 EOAIC2 | 未被音量/Intent 处理才由 `bridge_conversation_request()` 调原 Agent 的 `curl()` 包装，仍使用原 `EOAIC2_AGENT_ID` | 同步网络，可能执行真实设备指令；不能因超时盲目重发 |
| 15. 上下文读写 | 原包装管理 `EOAIC2_STATE` 与滑动 TTL；`text.sh` 在请求前备份旧缓存，异常恢复原字节，成功才接受新上下文。该步骤穿插于第 14 步，并非另一次 Conversation | session 锁保护；完成缓存操作后先释放 session，再等待可能被语音持有的音频锁 |
| 16. TTS | `speak()` 调既有 `TTS_PATH`，正常语音继承 FD 8；原豆包脚本负责 Microsoft→Google 回退，Bridge 观察本次播放器及后代并等待 provider 退出，恢复临时 TTS/duck 音量 | 录音已结束；`playing` 是进程观测，不能当物理扬声器首帧/末帧 |
| 17. 原生 LED 收尾 | 正常 voice 在回复完成和临时音量恢复后调用 `bridge_voice_led_finish()`，执行 11/shut 1/2/3，再做最终 JSON/guard 清理；父 Launcher 的结束 hook 按同一 cycle done 标记避免重复 | 视觉提前结束但父进程仍持 voice/audio/LED 和语音 Journal；失败不能伪造 done |
| 18. 语音音量恢复 | 父 Launcher 的 `bridge_restore_volume()` 按通道版本、最新 HA 目标和发送端状态恢复；保留失败通道 Journal，安排有界恢复 | 不允许用旧快照覆盖新值；错误/空 STT 等路径也须最终恢复 |
| 19. 释放并通知结束 | `bridge_voice_release()` 释放音频和 LED、只移除本所有者标记，发送 `voice_end`；需要时按被抢占任务身份恢复灯光/AirPlay；失败恢复可在所有权释放后继续 | 所有权、FD 与清理路径相互关联，不可仅删标记来表示成功 |
| 20. HA 刷新 | C 消费事件后增加 sequence、安排 Probe，HA 长轮询返回后请求刷新；常规轮询也继续 | 不是父 Listener 等待的一次同步完成握手，不应阻塞下一次录音 |

空 STT 的原 miplay 提示可以提前调用覆盖后的 `set_volume()`；它使用 `bridge_volume_recover(..., keep)`，保留 Journal 和语音所有权直到最终清理。STT 错误提示用 `bridge_error_tts()`，也遵守音频锁。不要只验证正常非空路径。

`bridge_voice_begin()` 不同步请求 HA 当前状态，但仍依赖本地审核适配器、C helper、配置、文件和锁。`--emit` 可忽略失败不代表所有步骤都可失败后放行；owner/Journal 等失败会阻止不安全的后续操作。

## 4. Probe、HA 同步与三种不同快照

### 4.1 Bridge Probe 不等于 HA 轮询

`bridge.c` 中 `PROBE_INTERVAL=15`、`PROBE_TIMEOUT=15`。`server()` 为 Probe 按需创建 kind=0 worker，执行 `device.sh probe`，同类 Probe 不重叠；与普通操作、音频任务共用有界 worker 数。初始化失败有有限重试；不是“HTTP 已监听即硬件就绪”。

`device.sh::probe()` 读 ALSA 控件/音量、Shairport 主/RemoteControl 属性、蓝牙 player/BlueZ、Listener/ready、能力验证字段、可用内存/Bridge RSS/负载与恢复待办。**它没有采集 MPD 状态**；HA Coordinator 另用 `MPDClient` 的 TCP discover/status/currentsong，唤醒则用 C CLI 的本地 Unix socket。

`finish_worker()` 只提交有效完整 JSON 的 capabilities/state 到 C 内存缓存，更新 `cached_at`/`probe_ok`；失败保留可表达的过期状态。有效内容改变、任务阶段/完成、语音事件和 AirPlay 事件文件变化可增加 sequence 或提前安排下一次 Probe。

`/api/state` 返回缓存，加上 C 当前 voice/audio、`updated`、`state_stale`、`instance_id`、设备身份；GET 不为每个请求同步启动一次完整 Probe，也不执行恢复 mixer 写入。

HA `DEFAULT_POLL=15` 是 `DataUpdateCoordinator` 默认更新周期，可在既有选项里配置；它不是 C Probe 的开关或 Listener 的等待周期。两套后台观测可能造成资源竞争，需要测量，但不能写成“每次唤醒先等 HA 15 秒”。

### 4.2 事件与 policy

- `/api/events?since=N` 是最多约 25 秒的 HTTP 长轮询；sequence 改变或等待到期返回序号，客户端序号大于新序号时可返回 reset。它是刷新提示，不是持久消息队列，也不携带每个硬件事件的完整重放。
- Coordinator `_events()` 在序号变化/reset 时 `async_request_refresh()`，连接失败按 1→最多 30 秒退避。读取状态、补充 MPD、决策音源后更新共享实体；没有为新属性建立额外轮询。
- `/api/policy` 接收 HA 已算好的 source、uncertain、priority，`save_snapshot()` 写运行目录中的 `xiaoai_bridge.source.json`（含 updated）。它保存策略投影，不执行媒体动作、不持久化任务队列，也不是全量实时硬件缓存。
- CLI `--snapshot` 只读该本地文件，不经 HTTP 向守护进程查询。文件缺失返回 source=null/uncertain=true。当前 `json_ops.c::snapshot()` 会结合新的硬件观测，**没有把该文件改成带 TTL 的完整硬件快路径缓存**。
- `read_local()` 收到 `voice_begin` 保存衰减前 source/active_sources/observed_sources/volumes，增加 sequence 并安排 Probe；收到 `voice_end` 清空。发送 datagram 返回不等于 C 已消费或 HA 已刷新。

### 4.3 音源与期望音量

`choose_source()` 默认优先级为 AirPlay、Bluetooth、MPD，语音中优先看冻结的三源观测，按 HA 选定的优先级处理并发、较高优先级未知及 state_stale。下级未知不一定改变已选上级来源；多个确定活动源仍可标记不确定。

区分三个对象：C Probe 的后台设备缓存；HA 写回的策略投影；录音前 `source-snapshot.sh` 新采集的语音快照。不能直接互换。衰减和 TTS 后再采集可能改变音乐状态/音量，导致“调大声”或最终恢复路由错误。

`resolve_targets()` 在未匹配活动来源时将 auto 默认映射到 master；这不是一套自动拒绝所有未知状态的全局策略。source=null、明确空闲和未知并非同义。v0.1.8 若研究缓存，必须保留观测的 null/可信度和优先级，不能用旧 source=null 伪造“已确认空闲”。原有自动路由行为与新快路径所需的安全判据要分别验证。

`VolumeManager` 用 HA Store `xiaoai_speaker.<UUID>.volume` 保存成功应答的期望值。音箱 `common.sh::volume_write()` 在 volume.lock 下原子写 `/tmp/xiaoai_bridge.desired.json` 的全局 revision 和通道 versions；录音/duck 期间 master、Bluetooth 等按各自条件延后应用。两份记录用途不同：HA Store 跨重启持久，设备运行目录只为本地安全协调。

Coordinator 首次看到新 `instance_id` 会逐个同步仍支持的已保存目标，失败留待重试。只有无 voice/audio、无正在执行 HA 任务且状态新鲜时，才把外部硬件变化提交为新 HA 期望值，避免把临时衰减值永久保存。新实例不意味着重放可能已执行的自然语言任务；运行中的任务历史不是跨重启持久队列。

## 5. 为什么必须保留这些安全机制

### 5.1 Journal、发送端和音量恢复

源码：[common.sh](../speaker/lib/common.sh) 的 `volume_write()`， [volume-recovery.sh](../speaker/lib/volume-recovery.sh) 的 `bridge_volume_journal()` / `bridge_volume_recover_locked()`， [json_ops.c](../speaker/src/json_ops.c) 的 `journal()` / `recovery()` / `desired_update()`， [ducking.sh](../speaker/lib/ducking.sh)。

Journal 记录 owner/start、保存时 revision、通道基线、AirPlay raw/active 和必要的 applied_master。在实际衰减前原子提交；每个成功恢复的通道才能从记录移除。恢复时：

1. 通道版本较新，采用新的明确 HA 目标；其他通道没有新目标，不应被同一全局变化误伤。
2. AirPlay sender 可能同时更新软件音量和 mysoftvol。比较发送端 raw/active 及实际 applied 值，不能把临时 cap、格式不同的小数或失联状态猜成发送端新目标。
3. 不确定时可保留 master pending，同时恢复独立 Bluetooth/TTS 通道；失败保留记录供有界重试。明确的新 HA 目标仍可进入，帮助解除模糊恢复，不能因 pending 一刀切拒绝所有音量写入。
4. duck→voice 在 volume 锁下移交原基线；音频预约失败不得提前删除原 duck Journal。否则会把“已压低的音量”当原始值，或者恢复覆盖录音衰减。

远程 announcement ducking 仍默认关闭，需原双端配置与已验证路由；只在唯一、确定 AirPlay 时降低 mysoftvol 的上限，不改 sender DBus 音量/notifyvol。不能借 v0.1.8 性能工作默认启用可选功能。`volume-recovery.sh recover` 是去重、有界、按需恢复进程，不是新增常驻服务；`/tmp` 的恢复依据不保证掉电持久，重启要靠 HA 重同步等既有机制。

### 5.2 锁与执行一次

| 资源/文件 | v0.1.7 约定 | 不能删掉的原因 |
|---|---|---|
| `xiaoai_bridge.led.lock` | Listener 通常 FD 6 跨语音周期持有；远程任务/Guard 短暂使用同一锁 | 原声优先、旧任务不得灭新灯、AirPlay worker 要协作 |
| `xiaoai_bridge.volume.lock` | helper 通常 FD 7；Bridge 重启交接中为 FD 5 | Journal/desired/mixer 的事务和原值交接；不是一个可任意移出锁的 JSON 写入 |
| `xiaoai_bridge.audio.lock` | 通常 FD 8；voice 子处理继承父进程同一 open file description | 录音与 TTS 互斥；不能另开 FD 把自己锁死或提前让新任务进入 |
| `xiaoai_bridge.session.lock` | `text.sh` 通常 FD 9；重启交接为 FD 7 | 原 Agent 上下文串行、重置互斥；远程 Conversation 完成缓存后先解锁再等语音音频 |
| config FD 与 OAuth mkdir | config.lock 通常 FD 4，兼容既有 `ha_oauth_refresh.lock` 目录 | 与旧 Cron 和安装事务都协作，避免丢失轮换凭据；不能强拆他人锁 |
| KWS serial/owner | 每轮 guard 的串行 FD 与 PID/starttime 目录 | KWS 重入不能删除仍有效的监督者或启动第二套录音 |
| install/boot/start/restart | 各自锁保护不同生命周期；API 短寿命重启调用者 stdin 保留 restart guard | 事务、重复点击、启动竞争和锁继承不能靠 PID 文件代替 |

FD 数字不是全局资源 ID。`locks.sh::bridge_lock_for()` 兼容 BusyBox 的 FD flock，以内核锁和有界等待为基础，不支持凭经验加入 GNU `flock -w`。不要删除仍持有的锁文件制造第二把锁。

voice、feedback 与 KWS owner 标记读 PID/starttime；voice_ready 还含 hook 版本。C 守护进程的 PID 文件仍是单 PID，`start.sh::owned_pid()` 另核对可执行文件身份。不能把不同文件统一成一种格式，也不能把整个双字段行交给单变量 read 后当数字。

C 只维护有界任务历史与 worker，不替代 HA `JobManager` 的队列、优先级和指纹去重。voice_begin 可以 TERM 正在 announce 的整组子进程，保留清理宽限；正在运行的 `send_text` 返回 `cannot_cancel_running_conversation`，不能为了看起来“取消成功”而杀掉可能已执行控制动作的请求。HA 提交结果不确定时只按同一 request_id 查询，不能换 ID 重发；busy 且尚未接收才可安全等待重试。

### 5.3 原 Agent、上下文、OAuth 和 TTS

`conversation.sh::bridge_load_conversation_adapter()` 显式加载原 `/data/eoai_agent.sh` 的 curl 函数，保留 Listener 已选的 Agent/缓存路径/TTL，缺少原包装即失败，不用裸 HTTP 静默换成 HA 默认 Agent。

原包装管理 `{agent,id,ts}`；用户部署为约 300 秒滑动语义，Bridge 保留原 `EOAIC2_KEEP_SECONDS`，不维护第二套 TTL 或覆盖已有选择。公开 [synthetic_eoai_agent.sh](../tests/synthetic_eoai_agent.sh) 用 300 秒验证契约，不是本次真机配置取证。成功会话可续滑动时间，过期/未来时间戳不复用；`use_context=false` 仅将状态路径换到本任务 WORK 中，不能清空或污染共享会话。失败时恢复原缓存，包括原本缺失或畸形的字节状态。重置只在 session/voice 保护下删除原 EOAIC2_STATE。

`bridge_conversation_request()` 通过受控 transport 包装观测真实 HTTP 状态，仅明确 401 才续期一次再用相同语义请求。网络断连、超时、5xx 可能发生在设备动作执行之后，不能当作“未执行”自动重试。`bridge_refresh_ha_token()` 在旧 OAuth 目录锁和 config FD 下重读凭据；若 Cron 已轮换就复用新值，提取原刷新函数而不执行完整 Launcher/KWS，失败不让远程文字子进程停掉原 Listener。

正常回复用配置中的 `TTS_PATH`（默认 `/data/tts_doubao`）；Launcher 的错误提示仍经原 `SPEAK`。既有 provider 管理豆包→Microsoft→Google 回退；样本 Microsoft 经 HA TTS 接口取音频、转换后用 notify 播放器，豆包/Google 用各自既有播放方式。Bridge 不实现第二套回退。

`text.sh::speak()` 观察本次 provider 本身及有限后代的 `miplay`/`mpg123`/`ogg123`/`aplay`，支持 provider 直接 exec；找不到后代接口时不能把系统里无关播放器算作本任务。播放阶段只是状态反馈；完成仍以本次 provider wait、退出状态和音量恢复为依据。

### 5.4 LED、重启和安装

LED 由 [voice-led.sh](../speaker/lib/voice-led.sh)、[task-feedback.sh](../speaker/lib/task-feedback.sh) 和生成 AirPlay Guard 协作。真实 Launcher 的顺序是 1→录音/STT 后 2→结束 11/shut 1/2/3；注释的 3 不启用。远程任务开始用 1/2，后续处理/合成/播放保持 2，退出统一收尾，不启动录音/STT。

原生命令可用时默认反馈；没有 `verified.led/task_led` 门槛或新配置。Guard 必须保留，因为原 AirPlay Hook/worker 不持 Bridge 音频锁、可能直接重绘。默认原生任务反馈的安装预检要求审核过且可恢复原始源的 Guard。任务和语音共享锁/所有权，完成只清理自己；收尾未成功保留 marker，既有 boot/Cron/下次任务恢复。重放最新音量灯前确认 AirPlay 当前 Active=true，旧 `airplay_led.last` 不能证明还在播放。

`bridge_voice_led_finish()` 可以在最终 JSON/voice_guard 清理之前结束视觉反馈，但仅在正确 cycle 和继承锁下写 done 标记；父 Listener 仍保留音频/voice/Journal 到最终恢复。把“灯灭了”当“可以释放所有锁”会重新引入音量与抢占竞态。

Bridge 重启按钮保持旧 `restart_listener` Unique ID，但调用 `/api/bridge/restart`。C 校验业务空闲、以 restart flock 合并重复请求，短寿命调用者延迟后复用 `start.sh restart`，强制 `XIAOAI_SKIP_LISTENER_START=1`；202 只表示接受调度，不承诺重启成功。`stop_bridge()` 在延迟和 start 锁等待之后、TERM 之前再检查语音/门锁。旧进程退出至新事件 Socket 就绪之间保留现有 volume/audio/session 门；首次有效健康应答（包括 Probe pending）即释放，不等完整探测。新进程必须关闭继承锁；替换失败退出释放门锁，使用原恢复机制重试。既不能放过新唤醒，也不能让 startup/慢 Probe 长期占住语音门。

安装由 [install.sh](../speaker/install.sh) 的预检/阶段状态、`finish_apply()` / `recover_snapshot()` 和 activation helper 实现。当前私有契约为 format=4、listener_hooks=4、kws_guard=1、airplay_led_guard=3；源 SHA256 针对未经归一化的原始字节，输出另有哈希。已启用 Guard 的原备份也要匹配 source，不能用已生成输出代替。

安装先排除活跃语音/任务、boot 与 OAuth 竞争，再保存旧 Bridge/启用状态、切换目录、恢复 Listener 实际就绪；启动失败明确区分 `failed_restored` 与 `recovery_required`。历史备份以唯一 retained 名保留，更新后的 OAuth 不应被旧快照覆盖。详细升级/恢复沿用 [v0.1.7 说明](lx06-v017.md)，本次交接不运行安装器（包括 dry-run），不修改其流程。

## 6. 已解决的历史问题如何转化为规则

**以下是已完成修复的防回归案例，不是当前缺陷清单。** 不重新审查 PR #1～#6 的旧评论，不重开已解决线程。链接和当前测试用于解释保护存在的原因；测试文件名中的 v016 等不代表应恢复该版本删除的功能。

| 历史教训与修复证据 | 当前防护与回归证据 | 删除/误改后的后果 |
|---|---|---|
| 合成 Launcher 不能代表原始 1/2/11、KWS 初始化及 CRLF；[ba73e5e](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/ba73e5e48122d0a88839a40d5e87c36a4c681c0a) | `prepare_listener()`/adapter check；`test_real_launcher_retains_record_loading_end_and_initialization_order`、`test_review_hashes_the_raw_crlf_source` | 错拒合法设备、重复初始化或录音/收尾顺序变更 |
| PID 单字段升级为 PID/starttime；[PR #3 修复](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/dad8b66022235b75720a58b5e0d6eb9b74d4d7ae) | `install.sh::voice_idle()` 双变量 read；live/stale owner 安装测试 | ash 单变量把整行当非数字，安装误判空闲；仅 kill -0 又可能误认复用 PID |
| jq 1.6 NUL 判断及频繁启动开销；[PR #4 修复](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/95349699be92ea1e540f47c61ac0d11ea6cc495c) | `explode/index(0)`、批量字段解析、原生 JSON helper；`test_real_jq16_reproduces_nul_bug_and_codepoint_check`、`test_wake_through_recorder_and_restore_uses_zero_jq_even_with_duck` | 正常文本误拒绝；重新用很多 jq 执行短字段会增加关键路径成本，但实际增量需测 |
| 恢复锁失败、duck 接管和发送端并发；[ba73e5e](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/ba73e5e48122d0a88839a40d5e87c36a4c681c0a) 及当前 Journal 实现 | `test_restore_lock_failure_releases_voice_audio_led_and_retries`、`test_failed_audio_handoff_retains_and_recovers_original_duck`、`test_sender_change_is_preserved_unless_new_ha_master_target` | 丢失原音量、旧恢复覆盖新目标、永久保留录音锁或双重衰减 |
| 相互等待 session/audio、上下文与 OAuth；当前统一管线承接既有修复 | `test_remote_conversation_releases_context_lock_before_waiting_for_voice_audio`、`test_real_http_401_reuses_original_refresh_then_same_agent_and_conversation`、`test_real_http_non_auth_failure_is_never_retried_or_refreshed` | 死锁、共享会话污染、续期覆盖新凭据或设备指令执行两遍 |
| TTS 直接 exec 与不同回退播放器；[23f973e](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/23f973e125bf6819f953946c240fdb75b2c4e93c) | `test_tts_direct_player_stage_and_volume_restore_with_optional_children`、`test_full_real_tts_fallback_chain_reports_playing` | 错认合成为播放/漏报，或以无关进程决定提前结束 |
| 默认原生灯效需要 AirPlay 合作；[b13445e](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/b13445e2fb012b0662f35ce8329d9a59afeda168) | install native guard 预检/事务；`test_default_native_install_activates_reviewed_guard_and_restores_inactive_snapshot` | 音量 Hook 绕过 LED 锁，覆盖语音/任务动画 |
| 失败收尾 marker 与原始备份完整性；[de062b9](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/de062b9955644ec5885107533037b575b21553a4) | `test_voice_preemption_defers_airplay_replay_until_voice_ends`、`test_active_guard_requires_recoverable_source_backup` | 无法重试灭灯，或回滚留下依赖已移走 Bridge 的活跃 Hook |
| 旧音量灯记录不能证明活跃；[2719710](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/2719710c3b3bd2eb5fba0e932545569611bff80f) | `test_replay_requires_current_airplay_activity_not_just_saved_volume` | AirPlay 已断开仍反复亮旧音量灯 |
| 重启前检查不够，替换窗口也会丢事件；[f5dcc1e](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/f5dcc1eeaf00564b81f7a304ef5764da8f2ed393)、[ee2985c](https://github.com/qiqi20020612/xiaoai-speaker-bridge/commit/ee2985cb4713640eb59115f28ca8fc3806b63fd3) | `test_wake_after_restart_ack_aborts_handoff_and_keeps_source_tracking`、`test_wake_during_daemon_gap_reaches_replacement_before_probe_finishes`、替换失败/锁继承测试 | 新实例漏掉 voice_begin 和来源，错误接收远程任务或影响恢复 |
| 只看 PID 就绪、复制旧认证和强删备份均不安全 | `test_enabled_v013_snapshot_is_restored_after_new_listener_readiness_failure`、`test_existing_oauth_directory_blocks_install_without_breaking_owner`、`test_storage_preflight_does_not_retain_or_replace_existing_backup` | 假成功、破坏稳定旧版/新 Token，或在可恢复空间不足时先移动备份 |

测试定位：[test_lx06_regressions.py](../tests/test_lx06_regressions.py)、[test_lx06_v016.py](../tests/test_lx06_v016.py)、[test_text_pipeline.py](../tests/test_text_pipeline.py)、[test_volume.py](../tests/test_volume.py)、[test_jobs.py](../tests/test_jobs.py)、[test_native_led.py](../tests/test_native_led.py)、[test_install.py](../tests/test_install.py)、[test_bridge_restart.py](../tests/test_bridge_restart.py)。实体精简/注册迁移由 [test_v017_simplification.py](../tests/test_v017_simplification.py) 保护。

根目录规范只保留这些案例的不变量：当前源码/调用者为准、保留未知状态、原子提交、按资源锁与所有权、结果不确定不重发、验证真实结构及失败恢复。历史数字、旧 Agent/RGB 实现和所有旧评论不进入长期规则。

## 7. v0.1.8 唯一主要目标与阶段

**在保持现有语音、音量、播放及 HA 控制正确性的前提下，缩短 KWS 成功到正式麦克风录音之间的额外耗时。** 本文仅提出调查与验收方案，不修改版本号或实现。

候选成本包括两处 AirPlay DBus 读取（Journal 与 source snapshot）、MPD/BlueZ 实时查询、音量读取与 Journal 文件提交/解析、Shell/C helper 进程、串行等待和 audio 锁竞争。Probe 与 HA 后台观测可能间接争用资源。它们是有源码依据的候选，不能未测就断言是全部延迟来源，更不能承诺省下某个固定毫秒数。

### 阶段 A：先取得可比较的基线

使用既有 `XIAOAI_PROFILE=1` 机制，需在用户另行授权的真机测量任务中让**实际 Listener 的启动环境**带入该变量；在工作站 export 或只重启 Bridge 不保证现有 Listener 继承它。启用通常涉及设备环境/服务操作及 `/tmp` 写入，不是本次文档任务的只读检查，本次未启用。

`common.sh::bridge_profile()` 记录无文本的 pid/phase/uptime/cycle，正常默认关闭；约 64 KiB 轮转一份。`bridge_voice_detected()` 的 cycle 跨 Launcher、text 子进程和 trace helper 关联；`tools/profile_report.py` 按 cycle（旧记录按 PID）输出相邻阶段与 from_pid。只在工作站对用户审阅后的阶段副本执行：

```sh
python3 tools/profile_report.py /path/to/copied.profile
```

不要上传原 STT/Conversation 日志、录音或配置来补时间；那些文件可能包含对话和凭据。profile 本身也应先检查，仅保留阶段所需信息。

| 测量区间 | 现有观测点与限制 |
|---|---|
| 远端命中→Listener | KWS client 样本有 WAKE_RX/CLEANUP_DONE，Launcher 有 ACTIVATED/UBUS_DONE，hook 有 wake/wake_setup_begin；先核实部署文件是否保留这些点。树莓派命中与 LX06 接收不共用一个时钟，未对齐不能直接相减 |
| wake_setup_begin→wake_led | 包含旧恢复检查、owner/cycle、LED 锁与 show_led 调用；不能等同全部 KWS 耗时 |
| wake_led→wake_volumes | 包含音量锁、amixer、Journal 的 AirPlay 查询/创建/读回；现有点不能分离每个内部操作。若需更细标记，先提出最小测量变更并另获授权 |
| wake_volumes→wake_snapshot | 实时 source-snapshot 整段，包括 policy 文件读取、DBus、MPD 和组合 JSON；缺少各查询独立耗时 |
| wake_snapshot→wake_event→wake_audio_ready | 区分 event emit 与音频锁/清理等待；不把等待别的 TTS 当 JSON 开销 |
| wake_audio_ready→recording_launch | 包含原 SAVED_VOL/阈值、可能的衰减、提示音、0.15 秒等待和管道准备；结合 wake_attenuated、原 SOUND/RECORD_LAUNCH 点解释 |
| recording_launch→recording_first_output | C `bridge_trace_pcm()` 观察首个 read 得到的字节，不解析 WAV，可能仅是头部；只证明 recorder 有输出 |
| STT/Conversation/TTS 后续 | 使用原管道结束和 task/session/config/voice_guard/volume_plan/intent/conversation/tts/restore/LED/cleanup phase；这些包络含脚本、网络和等待，不是纯模型时间 |

必须区分：命令启动、ALSA 打开、软件首块 PCM 到达、麦克风首个实际采样、首个有效语音样本。现有 trace **没有证明真实首个麦克风采样时间**；测有效数据要识别实际格式与 WAV chunk，不能把固定 44 字节假设或 Microsoft TTS 的特殊 WAV 处理套用到所有录音。声学首字完整性还需受控输入/监听验证，不能只看非空文件。

LX06 各 phase 使用 `/proc/uptime` 同一 boot-time 时间基准；分辨率与观测开销需记录，不混用跨机器时钟。开启 profile 自带 helper/文件大小检查、旋转和管道观察成本，A/B 应保持同样观测条件，并单独说明其影响。

每种场景重复多次，保存样本数、分布/异常值、来源、负载、网络、是否暖启动及测量条件；比较中位数和尾部耗时，不只挑最快一次。记录 Bridge Probe 是否正运行、KWS 清理、锁竞争和首字结果；没有可比真机样本时只报告缺口。工作站 `benchmark_snapshot.py` 的受控 fork/延迟实验用于机制验证，不能换算成当前 LX06 提速。

### 阶段 B：证据支持的低风险局部优化

仅在获得具体开发授权后，一次处理一个已测成本：消除同一时刻的重复查询、复用已经得到的数据、减少无用进程与串行步骤。对每项列明修改前/后的阶段分布、查询数量、输出等价、未知/失败分支、锁内数据一致性、Journal 可恢复性和资源变化。

复用 Journal/source 的 AirPlay 结果也必须解释采样时刻和发送端并发，不能简单删掉其一。保留实时观测的正确性、音量事务、版本和安全锁；不能删原提示音等待、改麦克风参数或提前录音与 TTS 混播来冒充局部提速。

### 阶段 C：可选快路径，尚未批准

只有 A/B 的数据仍证明有收益，再向用户提出有边界的方案：最近 Bridge 来源缓存、按优先级有条件省略查询、时间戳/可信度与失效处理。至少覆盖快速切源、多源、未知上级、掉线、重启、过期和 sender 调音；未知/过期不能误判确定空闲并路由 master。

当前 `--snapshot` 的策略投影并非已经实现了此方案。**不得依据本文直接实现完整缓存快路径、新状态机、新常驻服务或更改默认配置。** 先提交证据和具体方案，由用户另行决定。

## 8. v0.1.8 必须保留与未来验收矩阵

保留原中文 KWS、录音完整性、Microsoft STT、原 EOAIC2/HA 控制、豆包及回退、五分钟/独立上下文；保留 HA 文字/播报、媒体及各通道音量路由、发送端保护、抢占/取消、原生 LED/Guard、Bridge 与 Listener 独立性、状态同步和完整升级/回滚。不得新增常驻服务或恢复 v0.1.7 删除的实体/Agent/RGB/idle 配置。

| 场景 | 对照测量与必须保持的结果 | 当前证据边界 |
|---|---|---|
| 待机、AirPlay、Bluetooth、MPD 分别唤醒 | KWS→录音阶段分布、实际来源、正确衰减/恢复、首字和端到端响应 | 自动化有来源组合；本次无 v0.1.7 真机分布 |
| 快速切源/多源/未知后立即唤醒 | 冻结观测不被旧缓存覆盖；不确定状态保守表达，路由符合优先级 | 有优先级/未知状态软件测试；需真实播放切换复验 |
| 语音调音、唤醒期间 HA 改 master/Bluetooth | 最终是新目标；录音期间不提前解除衰减；各通道独立 | Journal/版本/并发故障测试已有，优化后继续跑并做真机对照 |
| AirPlay sender 改音量、临时音乐避让 | 不双重衰减、不猜 sender 目标、不恢复旧 master；不开启未验证可选项 | 有 sender/duck 受控测试，真实曲线与时序待测 |
| 远程 TTS 被语音抢占、排队/重复/取消 | 原语音优先、清理后正常录音；不重复执行指令；运行中 Conversation 的取消限制保留 | 有实际 C/隔离 HTTP/进程测试，真实音频需复验 |
| 语音中断网、401、STT/Conversation/TTS 错误及回退 | 不污染会话/凭据，不盲重发，原回退和恢复可用 | 有 HTTP/OAuth/TTS 样本故障测试，不代表实际 provider 服务验收 |
| 锁竞争、异常退出、恢复 mixer 失败 | 无死锁，释放可释放的资源；保留失败通道依据并可后续恢复 | 有 BusyBox/取消/超时/锁故障测试，硬件阻塞时长未测 |
| 连续多次唤醒、低内存、后台 Probe 重叠 | 无资源增长、空临时文件泄漏、KWS 重入或首字缺失；记录尾部延迟 | 内存边界/结构测试已有，真实低内存和高负载分布缺失 |
| 原生 LED、AirPlay 音量灯、语音抢占 | 正确时序、所有权、失败重试；旧任务不能灭新灯，未播回复也收尾 | 原生命令为隔离替身；固件动画/退出时长待测 |
| Bridge 重启、HA 重连、升级及失败回滚 | 仅 Bridge 重启，不丢语音事件；保留身份、私有状态、新 OAuth 和备份 | 已有重启/安装故障测试；本次不实机执行 |

软件门禁仍采用 [.github/workflows/ci.yml](../.github/workflows/ci.yml)：HA 2026.10、校验真实 jq 1.6 和 BusyBox 1.36.1、ShellCheck、C/ASan/UBSan、ARM 静态 ELF/QEMU、HACS/hassfest、安装恢复与包校验。固件 BusyBox/用户空间 ABI 不能仅由该 CI 推断；实际版本和可用选项需在获授权设备检查中核对。

每次交付分列：自动化通过/失败/跳过、模拟边界、真实设备已验收场景、仍未测场景与回退条件。历史 v0.1.6 的 31 分钟空闲/8 次播报属于用户提供的旧基线，不能移作 v0.1.8 实测结果。没有真实的有效音频/首字与锁并发证据就不宣称录音启动提速成功。

## 9. 自动 Code Review 的适用范围

根目录使用官方支持的 `## Code Review Rules` 章节，以普通 Markdown 自然语言规定简体中文标题/说明/总结、保留优先级标识和代码原文，并按受影响模块聚焦实际回归；没有虚构 `review_language` 等配置字段。

依据 [OpenAI 官方 GitHub Review 文档](https://learn.chatgpt.com/docs/third-party/github)，Codex 查找适用的 AGENTS.md 并按变更文件应用根目录/更具体规则；[规则设计说明](https://developers.openai.com/blog/custom-code-review-rules-for-codex) 建议规则有范围、证据和安全路径。规则是审查指导，不是确定性执行器，不替代 CI、分支保护或批准。

仓库自动 Review 已由用户配置，本次不手动提及机器人，不新增或修改工作流。规则不会自行开启 Review 或决定触发时机；Draft 是否被审查、使用的具体触发选项和指令加载情况取决于实际服务设置，本次不对其作无证据保证。文档 PR 仍保持 Draft，不为触发审查擅自改成 ready。该规范尚未合并到默认分支时，也不能声称所有未来会话/审查已经使用新规则。

简体中文是仓库要求，不能保证平台固定 UI/模板、优先级过滤或每次模型输出完全一致；也不能据此要求自动服务强制展示全部 P0～P3。以后在正常配置触发的审查中观察规则和语言是否生效，不通过手动触发或改 CI 来弥补本次交接。

## 10. 新会话阅读顺序与开始方式

1. [AGENTS.md](../AGENTS.md) 与本文第 1、3、4、7、8 节：确认源码基线、录音关键路径和授权边界。
2. `listener-hooks.sh` → `source-snapshot.sh` → `volume-recovery.sh` / `json_ops.c`：跟踪录音前所有同步依赖、Journal 与输出契约。
3. `prepare_adapters.py`、结构样本与 `test_lx06_regressions.py` / `test_lx06_v016.py`：核对真实形状、旧问题根因及观测边界。
4. `bridge.c`、`coordinator.py`、`volume.py`、`jobs.py`、`views.py`：区分后台同步、来源冻结、期望值与实际设备动作。
5. `text.sh` / `conversation.sh`、native LED、restart 和 install 相关测试：理解不得破坏的后半程、锁顺序和恢复。
6. [v0.1.7 功能/迁移说明](lx06-v017.md)、[测试说明](testing.md) 和工作流：核对仍存在的 14 实体、契约、升级与门禁。较早文档可能混合历史阶段，不能越过当前源码恢复已删除实现。

新会话首先应列出阶段 A 已有观测点、缺口、可比较的测量方案及所需设备授权；若已有审阅后的无敏感 trace，可先离线分析。缺少授权就不启用设备 profile、不改 Listener 环境、不部署、不试运行优化。缺少数据就标记未知，不把候选瓶颈当既定结论。

本交接止于文档 Draft PR。下一步是否合并规范、如何授权真机测量以及是否实施 A/B/C，均由用户决定；v0.1.8 尚未开发、未验收、未发布。
