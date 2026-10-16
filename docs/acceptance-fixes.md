# 0.1.5：0.1.3 验收问题修复与复验

本轮基于用户 2026-10-12 的 LX06 验收记录。用户已验证语音、上下文、AirPlay、音量、抢占/取消/队列/幂等和重启链路；空闲主进程 RSS/HWM 380 KB、31 分钟内稳定的记录不代表短寿命 worker 或负载峰值。本轮使用只读真机审计的脱敏结构；未部署或改变音箱/HA。完整修复映射及真机清单见 [0.1.5 LX06 修复记录](lx06-v015.md)。

## 修复范围

| 项目 | 实现与验证 | 真机仍需检查 |
|---|---|---|
| BUG-07 | 三个 HA SelectSelector 严格校验来源各一次；保留旧 source_priority；真实 probatio 转换、重复/缺失/未知值、保存再打开测试 | 打开配置、保存、重载、再次打开 |
| BUG-01 | 原 show_led 1 提前；一次 jq 合并快照/voice_begin；状态采集仍在衰减前；DBus 自带 600 ms 超时，MPD CLI 有界；音量/音频锁不绕过 | WAKE/activated→LED、LED→录音；不同来源与 G4～G6/H1～H3 |
| PERF-02 | 请求字段和配置字段各一次 jq；无临时 TTS 音量时不读取保存值；消除重复音量读取；阶段 trace | 相同输入、网络和播放长度的新旧对比 |
| BUG-03 | v0.1.7 统一原生 1/2/11/shut 灯效、共享 LED 锁和所有权、退出清理及 AirPlay 恢复；无 RGB 渲染 | 原生动画、AirPlay/Listener 协作、取消/失败/抢占 |
| IMPROVE-06 | 默认关闭、双端验证开关；AirPlay 确认活动时 mysoftvol 上限；新目标版本恢复和双顺序语音交接测试 | 确认实际音频路由、sender 百分比、G7/G8 |
| IMPROVE-09 | 原生 text/button/switch 调用现有服务/JobManager；非空/255 字符/忙碌/离线/能力检查；快速重复发送防护 | 设备页日常使用、实际 Recorder/事件保留策略 |
| IMPROVE-10 | tag/手动发布均重新构建并依赖全部 CI；版本/ABI/内外 SHA256；仅新 Release、最小权限 | GitHub workflow 实际运行、下载包人工安装 |
| BUG-04 | 首次有能力才添加，失去能力时 unavailable；生命周期不删旧实体、不重复注册 | adapter ready/离线/恢复 |
| OBS-05 | 首选来源、并发、上级未知、过期分别处理；本地观测冻结直到语音结束；优先级随 HA policy 传入 | AirPlay 暂停事件与轮询延迟 |
| IMPROVE-08 | 0.1.7 精确移除 peak_rss/memory_total/raw_volume 旧实体；raw dB 合并为百分比传感器属性 | 旧自动化、手动启用的实体与历史增长 |

## 远程灯效

v0.1.7 移除独立灯光实体及颜色 API。HA 排队中的任务不抢灯；设备开始处理后自动沿用真实 Launcher 的 `show_led 1` → `show_led 2`，处理、合成与播放期间保持 2，结束执行 `show_led 11` 和 `shut_led 1/2/3`。原脚本注释的 show_led 3 不启用，不模拟录音或 STT。

原生命令存在且可执行时默认工作，旧配置中的 led/task_led 验证字段不影响行为。无回复、失败、取消、超时同样收尾；语音持有原 LED 锁，旧任务按 PID/starttime 校验所有权。任务结束重放已审核 AirPlay Guard 的原音量灯，语音抢占时延后到语音结束。异常死亡的标记由启动、既有 Cron 或下次任务按同一锁恢复，无新常驻进程。原生程序不可用时安全跳过，硬件动画仍需复验。

## 音乐避让

主动播报和有声远程文字回复默认自动避让。v0.1.9 将检查与衰减从 TTS 调用前移到实际解码器入口：LLM、合成及回退合成期间不压低背景音乐，原生处理灯效继续显示；播放前同步完成实时音源检查、锁、Journal 和衰减，再把原参数与音频交给解码器，播放结束即恢复。只有 AirPlay 明确活跃、Bluetooth/MPD 明确不活跃且无冲突时，才把高于 10% 的 mysoftvol 降至原生唤醒默认的 10% 目标；它是绝对值，原音量更低时不提高，也不叠乘衰减。只改背景 master，不改 AirPlay 发送端或独立 notifyvol。较低优先级音源未知时也跳过，不能只看 uncertain=false；非关键探测失败不阻断 TTS。静默文字指令和空回复不衰减，原生语音继续使用已经持有的语音 Journal，不安装这个任务播放器入口。

入口仅覆盖本次 HA 任务 PATH 中的 mpg123/ogg123/aplay，以及通过 PATH 调用它们的 miplay；原 TTS 和 miplay 文件不改写，不缓冲或转码音频，也不根据后台播放器轮询追赶衰减。音量读取、Journal 或锁失败时不启动解码器，不因该安全错误触发提供方回退；真实解码失败仍保留原回退链。用户自定义脚本若重置 PATH 或绝对调用解码器，不在本入口覆盖范围内，不能声称已避让；LX06 播放器实际调用、首字听感及开销仍待实机验证，见 [v0.1.9 修复记录](lx06-v019.md)。

音源快照与锁内 Journal 保留各自的 AirPlay 查询时刻。后一次查询失败、停止或缺少有效发送端音量时，删除尚未应用的本次记录并跳过衰减；不以较早观测替代恢复基线。恢复仍待处理时，HA 利用现有 volume_recovery_pending 状态暂停导入硬件音量，避免把临时 10% 保存成新的期望值。

Probe 在短暂的非阻塞 Volume Lock 内关联 mixer 读数与任一 Journal 的存在状态（包含活跃 owner），避免查询期间恢复完毕后把旧临时值标成可信。锁忙则本次本地音量为 null 并暂停导入；DBus/BlueZ 全部在释放锁后执行，下一次原有 Probe 再更新。该保护覆盖 voice-volume、duck、tts-volume，不改写恢复或所有权规则。

HA 不再显示避让开关和上限，加载配置条目时只清理 announce_ducking/announce_duck_volume 旧选项，保留其他配置。音箱示例和 probe 删除专用验证标记/能力；旧配置中残留的 verified.announcement_ducking 不参与判断。协议 1 保持，旧端在过渡期间发送的合法数值 duck_volume 仍通过类型校验但被忽略，不能改变固定衰减目标。先更新音箱端再更新 HA；新 HA 连接旧音箱时原播报/对话继续可用，自动避让需新版音箱端。

复用原语音的 bridge_volume_journal / bridge_volume_recover_locked、通道版本与音量锁。取消、TTS 失败、超时的 TERM 清理恢复原值或用户新目标。独立 duck Journal 保留语音抢占所需的原始基线，在 volume.lock 下移交给 listener；旧任务不把衰减值当原值，也不覆盖录音期衰减。恢复锁/混音器失败保留 /tmp 恢复记录，释放音频/语音/LED 所有权后有界重试，下一次安全操作也重试。发送端目标已改变时不回写旧 master。v0.1.9 对仍等于 cap 的 Duck PENDING 增加受限恢复：只有验证了运行中 Shairport 的已加载配置、实际 ALSA dB TLV 和目标曲线，才按当前 raw 恢复共享 mixer，并复查发送端已应用该值；持续变化/失败保留恢复标志，HA 新版本仍优先。不能确认目标时保留原 PENDING，不猜测映射或回发旧 raw，详见 [v0.1.9 修复记录](lx06-v019.md#发送端调音后-duck-卡在-pending)。/tmp 掉电丢失、实际路由、听感及共享控件竞态仍需真机验收。

## 文本输入隐私

原生 text 操作仅用于非敏感内容，最多 255 个字符，这是 HA State Machine 的限制；较长文本仍使用原服务（4096 字节）。「播放文字」提交即调用 `xiaoai_speaker.announce`，「执行文字指令」提交即调用 `xiaoai_speaker.send_text`，不需要独立发送按钮。v0.1.9 起成功入队后自动清空，通过 HA 原生 `logbook.log` 将完整原文关联到对应实体；记录表示成功提交，不表示音箱任务已经完成。提交失败不写 Logbook，并在现有内存输入字段中保留未入队的有效文字；正在提交或仍有未完成任务时继续拒绝重复发送。任务结束后可以再次提交相同文字，每次成功提交单独记录。后续任务失败仍由原有 status/error/request_id 属性反馈，不另写一次提交记录；空白、NUL 和超长输入继续拒绝。快捷文字对话使用原服务 speak_response=true、use_context=true 默认值；两个独立开关和其内存状态已删除。

Logbook 使用实际 Entity ID（含用户改名）、实体名称和原服务上下文，message 保留空白、换行和完整原文。由 HA 原生 Recorder/Logbook 持久化、过滤和清理，集成不另建历史数据库或保留已提交文本。Logbook 未启用时不强制启动 Recorder/Logbook；写入服务异常仅记录不含原文的警告，不把已经入队的任务误报失败或重新提交。提交值及未入队的输入状态也可能进入 HA 服务事件、自动化、日志和历史；输入使用 Password 模式不能把这些渠道变成安全存储。集成诊断不导出输入、回复或 token。

在输入前配置 Recorder 排除**实际的两个 text Entity ID**，例如：

```yaml
recorder:
  exclude:
    entities:
      - text.your_speaker_play_text
      - text.your_speaker_execute_text_directive
```

Recorder 按实际 Entity ID 排除时，也会过滤关联的 Logbook 条目；仅使用 Logbook 展示过滤不等于阻止 Recorder 入库，实体定向查询遵循 HA 原生规则。排除实体不能保证服务事件、日志、外部自动化或已有历史均无文本。敏感任务应关闭/不使用这些输入，并按自己的 HA 数据保留策略处理已有历史。更长/敏感输入使用原服务也不获得“不留痕”保证。

## 实体迁移

新安装只创建有使用价值的来源/AirPlay 状态和控制入口。资源、队列、job_status、会话剩余秒数、不确定性默认禁用；两个文本实体属性提供各自的任务状态、request_id 和错误码。Bridge API/诊断保留原始资源及 AirPlay dB。

HA 侧更新保留 `announce_text` / `send_text_text` 原 unique_id、用户自定义 Entity ID 和启用选择；仅移除当前集成条目的 `announce_send` / `send_text_send` 旧按钮注册，其他按钮、实体和配置保留。现有 `xiaoai_speaker.announce` / `send_text` 服务自动化不变。曾用旧 `button.press` 发送的自动化需改为直接调用原服务，或用以下原生操作（换成原有实际 Entity ID）：

```yaml
action: text.set_value
target:
  entity_id: text.your_speaker_execute_text_directive
data:
  value: "打开客厅灯"
```

此控件调整只更新 HA 集成文件，按正常流程重启 HA 后使用；音箱端无需更新或重新生成适配器。

离线、能力缺失或状态过期时文本实体显示 unavailable，不派发任务。HA 原生 `text.set_value` 会跳过不可用目标并记录警告；原 `xiaoai_speaker.announce` / `send_text` 服务仍按原逻辑抛出错误。繁忙和无效输入在可用文本控件中正常报错，未改 HA 的原生服务分发机制。

0.1.7 会精确清理已删除实体（包括 bridge_peak_rss、memory_total、airplay_raw_volume），禁用或改名也处理；保留实体的 Unique ID、Entity ID 和启用选择不变。引用被删实体的自动化应先调整。完整迁移清单见 [0.1.7 精简说明](lx06-v017.md)。

## 阶段测量

用户手动在测试时向原启动环境导出 `XIAOAI_PROFILE=1`，然后按既有启动流程加载新版 adapter。默认关闭，无新增后台服务。记录在 `/tmp/xiaoai_bridge.profile`，超过 64 KiB 保留一份 `.1`；只含 pid/phase/uptime，没有文字、响应、token 或地址。启用 trace 的额外 wc/旋转开销必须纳入实验说明。测试后取消该环境变量。

时间使用 Linux `/proc/uptime`。wake→wake_led 是进入 Bridge hook 后的本地亮灯时间，不包含树莓派 KWS 通信；wake_led→recording_ready 包括音量、快照、event emit、音频锁。recording_ready 是允许原录音逻辑继续，不是录音设备真实启动，应单独对齐原录音日志。

task_begin→task_parsed 测本地解析；session_wait→session_ready 测锁；config_ready/sources_ready/voice_guard_finished/volume_plan_finished/intent_finished/conversation_begin/conversation_finished 区分本地与网络。tts_launch→tts_playing 是观测到播放器的延迟，tts_launch→tts_finished 包含合成与播放。原 TTS 首包没有单独观测点，不能把任务总时间认作网络合成时间；未发现本次调用的播放器时不伪造 playing。原 Agent 内部 jq 与 OAuth 由原脚本管理，Conversation 包络不等于纯 LLM 时间。

使用 `python3 tools/profile_report.py /path/to/copied.profile` 输出各 PID 的相邻阶段耗时。先复制用户审阅过的阶段记录到本地；不要提交原对话日志或私有运行配置。

本地模拟测试覆盖来源组合、一次 jq 启动、亮灯先于受控延迟快照、上下文/401 续期、恢复与并发；真实模型/STT/TTS 网络、音箱录音启动时间和负载峰值未测。本轮不以模拟结果声明真机提速。

可复现的工作站实验：`python3 tools/benchmark_snapshot.py --baseline dbc77b643bfbf7871814752112be1a30b382926e --jq-delay 0.03 --samples 3`。对空闲/AirPlay/蓝牙/MPD/多来源分别注入每次 jq 30 ms 延迟，旧版中位数约 0.100～0.122 秒，新版约 0.057～0.070 秒；每次 jq 2→1，五种路由输出相符。这是**带受控延迟的 macOS 合成实验**，用于验证 fork 减少的收益方向，不能替代 LX06 或实际网络测量。每次实验应保存自己的结果，不使用这些数字推算真机总耗时。

## 手动升级与复验

v0.1.7 使用 [正式 Release](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases/tag/v0.1.7) 附件及其 SHA256SUMS，由用户授权部署。从已验证的 v0.1.6 原地升级，沿用通过哈希校验的 format=4/hook=4、KWS=1、AirPlay guard=3 私有副本；不改设备 UUID/token、OAuth、Agent、KWS/STT/TTS、AirPlay 曲线或旧验证字段，不卸载、不删除恢复备份。详细步骤及最小实测见 [v0.1.7 原生灯效说明](lx06-v017.md)。

不兼容的活跃 AirPlay Guard 仍按安装器原规则拒绝；已匹配的 v0.1.6 Guard 不必移除再启用。失败恢复沿用同一完整包的 --rollback，恢复升级前 v0.1.6；HA 回退保留其他实体 ID，已移除的旧手动灯光若要恢复自定义 ID，使用升级前 HA 备份。旧灯光自动化不会自动修改或删除。
