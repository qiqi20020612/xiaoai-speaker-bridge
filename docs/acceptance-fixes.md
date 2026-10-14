# 0.1.5：0.1.3 验收问题修复与复验

本轮基于用户 2026-10-12 的 LX06 验收记录。用户已验证语音、上下文、AirPlay、音量、抢占/取消/队列/幂等和重启链路；空闲主进程 RSS/HWM 380 KB、31 分钟内稳定的记录不代表短寿命 worker 或负载峰值。本轮使用只读真机审计的脱敏结构；未部署或改变音箱/HA。完整修复映射及真机清单见 [0.1.5 LX06 修复记录](lx06-v015.md)。

## 修复范围

| 项目 | 实现与验证 | 真机仍需检查 |
|---|---|---|
| BUG-07 | 三个 HA SelectSelector 严格校验来源各一次；保留旧 source_priority；真实 probatio 转换、重复/缺失/未知值、保存再打开测试 | 打开配置、保存、重载、再次打开 |
| BUG-01 | 原 show_led 1 提前；一次 jq 合并快照/voice_begin；状态采集仍在衰减前；DBus 自带 600 ms 超时，MPD CLI 有界；音量/音频锁不绕过 | WAKE/activated→LED、LED→录音；不同来源与 G4～G6/H1～H3 |
| PERF-02 | 请求字段和配置字段各一次 jq；无临时 TTS 音量时不读取保存值；消除重复音量读取；阶段 trace | 相同输入、网络和播放长度的新旧对比 |
| BUG-03 | 远程阶段灯效、共享 LED 锁、退出清理及原灯效恢复；未验证原语不写 sysfs | 灯色、AirPlay/原 listener 协作、取消/失败/抢占 |
| IMPROVE-06 | 默认关闭、双端验证开关；AirPlay 确认活动时 mysoftvol 上限；新目标版本恢复和双顺序语音交接测试 | 确认实际音频路由、sender 百分比、G7/G8 |
| IMPROVE-09 | 原生 text/button/switch 调用现有服务/JobManager；非空/255 字符/忙碌/离线/能力检查；快速重复发送防护 | 设备页日常使用、实际 Recorder/事件保留策略 |
| IMPROVE-10 | tag/手动发布均重新构建并依赖全部 CI；版本/ABI/内外 SHA256；仅新 Release、最小权限 | GitHub workflow 实际运行、下载包人工安装 |
| BUG-04 | 首次有能力才添加，失去能力时 unavailable；生命周期不删旧实体、不重复注册 | adapter ready/离线/恢复 |
| OBS-05 | 首选来源、并发、上级未知、过期分别处理；本地观测冻结直到语音结束；优先级随 HA policy 传入 | AirPlay 暂停事件与轮询延迟 |
| IMPROVE-08 | 新装略去 peak_rss/memory_total/raw_volume；旧 registry 继续兼容；诊断默认禁用 | 旧自动化、手动启用的实体与历史增长 |

## 远程灯效

HA 队列中的 queued/accepted 仅显示任务状态，不抢占当前声音的灯。设备工作时 processing 为橙色；调用原 TTS 时 synthesizing 为紫色；只有检测到该 TTS 进程或后代中的 miplay、mpg123、ogg123、aplay才显示青色 playing。无回复播报的文字任务只有处理灯。完成、失败、取消走同一清理，恢复此前命令灯或重放经过适配的原 AirPlay 音量灯。语音持有同一 LED 锁并优先让远程任务退让。

默认 `verified.task_led=false`，独立于原有手动灯光 `verified.led`。只有用户确认该型号的真实 sysfs 原语、审核并启用新版 airplay-led 私有副本后，才同时设置 `verified.led=true`、`verified.task_led=true`。已有手动灯光配置升级后不会自动启用远程灯效。测试使用临时文件，不等价于 LX06 灯效验收。

## 音乐避让

HA 配置中的“播报时压低背景音乐”默认关闭；开启后上限默认 10%，来自已验证的本地语音避让目标。该值是绝对上限，原音量更低时不提高也不再乘一次比例。音箱还必须设置 `verified.announcement_ducking=true`，由用户先确认 AirPlay 确实通过 mysoftvol、TTS 使用独立 notifyvol；多来源或状态不确定时跳过。

取消、TTS 失败、超时的 TERM 清理恢复原值或用户新目标。语音抢占时，原始恢复值在 volume.lock 下移交给 listener；旧 ducking 不把衰减值当原值，也不覆盖录音期衰减。恢复锁/混音器失败保留 /tmp 恢复记录，释放音频/语音/LED 所有权后有界重试，下一次安全的语音/任务/音量操作也重试。/tmp 在掉电后可能丢失，重启及 OOM 路径仍需真机检查。发送端目标已改变时不回写旧 master；共享 mysoftvol 的极短竞态仍未经过功能验收。

## 文本输入隐私

原生 text 操作仅用于非敏感内容，最多 255 个字符，这是 HA State Machine 的限制；较长文本仍使用原服务（4096 字节）。「播放文字」提交即调用 `xiaoai_speaker.announce`，「执行文字指令」提交即调用 `xiaoai_speaker.send_text`，不需要独立发送按钮。提交成功后清空，任务结束后可以再次提交相同文字；空白、NUL 和超长输入拒绝。回复播报与共享上下文开关初始均为 true，开关/输入存于内存，集成重载后恢复默认。交互参考 [Xiaomi Miot 的 TextEntity 动作设计](https://github.com/al-one/hass-xiaomi-miot/blob/master/custom_components/xiaomi_miot/text.py)，执行仍通过本集成原有服务与队列。

提交值经过 HA 服务，可能进入服务事件、自动化和日志；旧版已保存的文本状态及历史也不会被删除。输入使用 Password 模式也不能把这些渠道变成安全存储。提交后清空只是方便下一次输入，不删除历史。集成诊断不导出输入、回复或 token。

在输入前配置 Recorder 排除**实际的两个 text Entity ID**，例如：

```yaml
recorder:
  exclude:
    entities:
      - text.your_speaker_play_text
      - text.your_speaker_execute_text_directive
```

排除状态不能保证服务事件、日志、外部自动化或已有历史均无文本。敏感任务应关闭/不使用这些输入，并按自己的 HA 数据保留策略处理已有历史。更长/敏感输入使用原服务也不获得“不留痕”保证。

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

如果 registry 已有 bridge_peak_rss、memory_total、airplay_raw_volume，仍注册同一 unique_id/Entity ID 并保留 enabled/disabled 选择。其他诊断实体更改默认策略也不覆盖已有 registry 设置。用户确认没有自动化依赖后可自行移除兼容实体；集成不自动删除或改名。

## 阶段测量

用户手动在测试时向原启动环境导出 `XIAOAI_PROFILE=1`，然后按既有启动流程加载新版 adapter。默认关闭，无新增后台服务。记录在 `/tmp/xiaoai_bridge.profile`，超过 64 KiB 保留一份 `.1`；只含 pid/phase/uptime，没有文字、响应、token 或地址。启用 trace 的额外 wc/旋转开销必须纳入实验说明。测试后取消该环境变量。

时间使用 Linux `/proc/uptime`。wake→wake_led 是进入 Bridge hook 后的本地亮灯时间，不包含树莓派 KWS 通信；wake_led→recording_ready 包括音量、快照、event emit、音频锁。recording_ready 是允许原录音逻辑继续，不是录音设备真实启动，应单独对齐原录音日志。

task_begin→task_parsed 测本地解析；session_wait→session_ready 测锁；config_ready/sources_ready/voice_guard_finished/volume_plan_finished/intent_finished/conversation_begin/conversation_finished 区分本地与网络。tts_launch→tts_playing 是观测到播放器的延迟，tts_launch→tts_finished 包含合成与播放。原 TTS 首包没有单独观测点，不能把任务总时间认作网络合成时间；未发现本次调用的播放器时不伪造 playing。原 Agent 内部 jq 与 OAuth 由原脚本管理，Conversation 包络不等于纯 LLM 时间。

使用 `python3 tools/profile_report.py /path/to/copied.profile` 输出各 PID 的相邻阶段耗时。先复制用户审阅过的阶段记录到本地；不要提交原对话日志或私有运行配置。

本地模拟测试覆盖来源组合、一次 jq 启动、亮灯先于受控延迟快照、上下文/401 续期、恢复与并发；真实模型/STT/TTS 网络、音箱录音启动时间和负载峰值未测。本轮不以模拟结果声明真机提速。

可复现的工作站实验：`python3 tools/benchmark_snapshot.py --baseline dbc77b643bfbf7871814752112be1a30b382926e --jq-delay 0.03 --samples 3`。对空闲/AirPlay/蓝牙/MPD/多来源分别注入每次 jq 30 ms 延迟，旧版中位数约 0.100～0.122 秒，新版约 0.057～0.070 秒；每次 jq 2→1，五种路由输出相符。这是**带受控延迟的 macOS 合成实验**，用于验证 fork 减少的收益方向，不能替代 LX06 或实际网络测量。每次实验应保存自己的结果，不使用这些数字推算真机总耗时。

## 手动升级与复验

通过 [Releases](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases) 下载 0.1.5 包并校验 SHA256。用本版工具从保存的未修改原脚本重新生成私有 listener/airplay-led 副本并检查 diff/hash；放入解压包的 adapter 后执行 `install.sh --dry-run`、`--apply`。安装器只沿用 review format=3、hooks revision=3、原始/输出哈希和 hook 顺序均匹配且仅保留经过审阅的 show_led 2/11 调用的副本；否则在替换文件/重启服务前明确要求重新生成，相同包重装也校验。原配置和 UUID/token 被保留。升级后的 HA 配置可直接使用旧选项，不需重新配对。

已启用的旧 AirPlay LED 保护若与新版副本不匹配，先用旧安装目录的 `tools/activate_airplay_led.sh --remove` 恢复原程序，升级后再审核启用新版保护。升级时保持 `verified.task_led=false`；只有新版保护已启用并完成验证后才开启。

回滚使用同一完整包的 `install.sh --rollback`；HA 回退版本后重启。新增实体可能不可用，保留它们的 registry 供以后恢复，不改旧 ID。0.1.5 的三个版本字段同步更新，正式 tag 必须指向通过完整 CI 的合并提交。本轮发布不执行真机部署。

按用户原验收脚本复跑 E1～E5、G4～G8、H1～H5；再检查重启、空闲 ≥31 分钟与实际负载峰值/短寿命 worker/原 listener/整机资源。MPD 队列/控制及其他机型另行实测。
