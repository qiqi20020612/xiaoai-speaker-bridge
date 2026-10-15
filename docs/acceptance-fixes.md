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

HA 配置中的“播报时压低背景音乐”默认关闭；开启后上限默认 10%，来自已验证的本地语音避让目标。该值是绝对上限，原音量更低时不提高也不再乘一次比例。音箱还必须设置 `verified.announcement_ducking=true`，由用户先确认 AirPlay 确实通过 mysoftvol、TTS 使用独立 notifyvol；多来源或状态不确定时跳过。

取消、TTS 失败、超时的 TERM 清理恢复原值或用户新目标。语音抢占时，原始恢复值在 volume.lock 下移交给 listener；旧 ducking 不把衰减值当原值，也不覆盖录音期衰减。恢复锁/混音器失败保留 /tmp 恢复记录，释放音频/语音/LED 所有权后有界重试，下一次安全的语音/任务/音量操作也重试。/tmp 在掉电后可能丢失，重启及 OOM 路径仍需真机检查。发送端目标已改变时不回写旧 master；共享 mysoftvol 的极短竞态仍未经过功能验收。

## 文本输入隐私

原生 text 操作仅用于非敏感内容，最多 255 个字符，这是 HA State Machine 的限制；较长文本仍使用原服务（4096 字节）。「播放文字」提交即调用 `xiaoai_speaker.announce`，「执行文字指令」提交即调用 `xiaoai_speaker.send_text`，不需要独立发送按钮。成功入队后各自保留完整原文，下一次成功提交时更新；提交失败不覆盖原状态，任务结束后可以再次提交相同文字。后续任务失败仍保留已提交文字，由原有 status/error 属性反馈；空白、NUL 和超长输入继续拒绝。快捷文字对话使用原服务 speak_response=true、use_context=true 默认值；两个独立开关和其内存状态已删除。交互参考 [Xiaomi Miot 的 TextEntity 动作设计](https://github.com/al-one/hass-xiaomi-miot/blob/master/custom_components/xiaomi_miot/text.py)，执行仍通过本集成原有服务与队列。

实体在当前集成运行期间保留最后一次成功提交的原文；提交值经过 HA 服务，可能进入服务事件、自动化、日志和历史。输入使用 Password 模式也不能把这些渠道变成安全存储。集成诊断不导出输入、回复或 token。

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
