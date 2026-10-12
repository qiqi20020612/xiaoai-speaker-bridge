# XiaoAI Speaker Bridge

[![CI](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions/workflows/ci.yml)

Home Assistant 2026.10 自定义集成，以及 LX06 `/data` 下的轻量受控端。统一原生媒体播放器、独立音量、智能跟随、主动播报、远程文字对话、监听管理和设备状态。KWS、STT、现有 Conversation Agent、五分钟上下文及原有 TTS 回退链继续由音箱已有脚本执行。

**用户已于 2026-10-12 在 LX06 完成 0.1.3 核心链路与空闲主进程资源验收。本轮修复仍需用户真机复验；开发未连接、部署或修改设备，自动测试使用合成数据。空闲 RSS/HWM 记录不代表负载和短寿命 worker 峰值。** 本轮范围、隐私、迁移和复验步骤见[验收修复说明](docs/acceptance-fixes.md)。

## 两端架构

```mermaid
flowchart LR
    subgraph HA["Home Assistant 进程"]
        integration["custom_components/xiaoai_speaker<br/>规则、持久期望值、队列、实体、多设备"]
        conversation["现有 Conversation API / Agent"]
    end
    subgraph LX["LX06 音箱"]
        bridge["/data/xiaoai_bridge<br/>C HTTP 受控端与按需脚本"]
        mpd["现有 MPD"]
        voice["现有 KWS / STT / Agent / TTS<br/>共享文本入口与本地互斥"]
        hardware["ALSA / Shairport / BlueZ / LED / IR"]
    end
    integration <-->|"认证 HTTP / 状态事件"| bridge
    integration <-->|"MPD 原生协议"| mpd
    bridge --> voice
    voice -->|"音箱发起文字对话"| conversation
    conversation -->|"回复由音箱播报"| voice
    bridge <--> hardware
```

HA 集成直接运行于 HA 内。音箱仅有一个新增 C 常驻进程，硬件与语音脚本按需调用。无需部署 HA 外的服务端、容器或 Broker。媒体播放完全独立于 Linn / OpenHome；既有 Snapclient、upmpdcli 保留。

## 功能

| 入口 | 行为 |
|---|---|
| `media_player` | MPD 直连状态、元数据、播放/暂停/停止、切歌、定位、队列、重复/随机及 URI 播放；逐项根据 MPD `commands` 开放 |
| 独立 `number` | `mysoftvol`、`notifyvol`、蓝牙、耳机、AirPlay 分开；只创建具备实际可写能力的通道 |
| 智能音量 | `auto` 使用唤醒前/文字请求前快照；明确对象优先；空闲对象及并发音源优先级在 HA 配置 |
| `announce` | 纯朗读，直接使用音箱原 TTS；不执行意图、不更新上下文 |
| `send_text` | HA → LX06 → 原本地意图/Agent → HA Conversation → LX06；支持共享上下文、独立 Agent 覆盖、关闭回复播报 |
| 任务 | HA 内有界优先队列、去重、查询、取消、阶段状态、断线后按 ID 查询；不会盲目重发已经可能执行的文字 |
| 设备 | 在线、音源、语音活动、内存/运行时间等诊断；监听开关、会话重置、Agent 选择 |
| AirPlay/蓝牙 | 真实服务检测和独立状态；反向控制需要逐项真机验证后开启本地 capability 标志 |
| LED/IR | 保留高级适配和能力接口；未验证时明确拒绝操作，不出现假按钮 |
| 开机 | 固定 `/data/xiaoai_boot.sh`，90 秒后仅停 Squeezelite，等待异步退出，启动桥接并确认就绪；Cron 幂等、可重试 |

AirPlay 音量具有只读传感器，原始 dB 保留在 API/诊断及已注册的兼容实体；没有通过发送端同步验证时，不会用主音量冒充反向控制。远程灯效仅在验证原语后开启，语音优先。诊断实体的新默认策略保留旧 Entity ID 与用户启用选择。

## 安装

先阅读 [逐端安装、启用、升级及回滚](docs/install.md)。固定契约为：

**0.1.1 修复 BusyBox 1.36.1 的 `flock` 兼容性，并增加安装失败自动恢复。** 0.1.0 若已更新 boot/Cron 后启动失败，请先按[部分安装恢复步骤](docs/install.md#8-从-010-的-busybox-启动失败状态恢复)使用新包回滚，再重新安装。

**0.1.2 显式接入原 EOAIC2 Agent、共享缓存及原令牌续期函数。** 真实 HTTP 回归核对 Agent/Conversation ID、401 续期和成功后的滑动期限，见 [Conversation 执行链路](docs/conversation.md)。用完整新包升级即可，现有 listener/Agent/KWS/STT/TTS 配置不替换。

**0.1.3 增加首次启动容错和诊断。** probe 超时提高到 15 秒，首次失败后分别等待 1、2 秒重试，启动健康等待预算为 75 秒。首次成功探测前保持未就绪；连续三次失败仍自动恢复安装事务。初始化、探测原因和安装恢复状态写入有界日志，见[启动诊断](docs/troubleshooting.md#首次启动与安装失败诊断)。

```text
HA:    config/custom_components/xiaoai_speaker/
LX06:  /data/xiaoai_bridge/start.sh
       /data/xiaoai_bridge/bin/xiaoai_bridge
       /data/xiaoai_bridge/config.json
       /data/xiaoai_boot.sh
Cron:  * * * * * /bin/sh /data/xiaoai_boot.sh
```

HACS 添加本仓库为 **Integration** 自定义仓库并安装，重启 HA，然后在“设置 → 设备与服务”添加 **XiaoAI Speaker Bridge**。填写音箱地址、桥接端口（默认 `8766`）、本地随机桥接令牌，以及 MPD 端口/可选密码。`8766` 与既有 KWS 服务端口分开。多台音箱分别添加，每台有独立设备 UUID、鉴权、音量存储和任务队列。

HACS 安装布局与本地 `brand/` 资源遵循 [HACS Integration 要求](https://www.hacs.xyz/docs/publish/integration/) 和 [HA 集成文件结构](https://developers.home-assistant.io/docs/creating_integration_file_structure/)。HACS 只安装 HA 端。安装包固定下载入口是 [GitHub Releases](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases)；尚未发版的构建可从 [CI](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions/workflows/ci.yml) 获取。仅声明已测 LX06/ARM EABI5 hard-float 兼容，音箱包须由用户审阅部署。

下载同一版本的 LX06 包、HA ZIP、`RELEASE_NOTES.md` 与 `SHA256SUMS` 到同一目录，执行 `sha256sum --check SHA256SUMS`（macOS：`shasum -a 256 --check SHA256SUMS`）。`v*` tag 经全部 CI 后才创建新 Release；手动发布也重新构建、严格校验，不能覆盖已有正式/预发布或草稿。维护者发版流程见[发布说明](docs/releases.md)。当前版本字段仍为 0.1.3，本轮未创建新版 tag 或发布 Release。

音箱安装器默认 `--dry-run`。原配置不会被通用模板替换；共享语音入口需要从本机实际 launcher 生成私有副本，并另行启用一行引导。LED 协调通过审阅既有 LED 程序的守护补丁实现，**不修改 Shairport 配置或软件音量曲线**。详情和移除方法见安装文档。

## 调用示例

设备页面增加非敏感播报/对话文本输入及发送按钮，只有点击发送才执行；回复播报和共享上下文开关默认开启。**原生文本状态可能进入 HA 历史、日志、服务事件和自动化；发送后清空不删除历史。** 输入前阅读[隐私与 Recorder 排除](docs/acceptance-fixes.md#文本输入隐私)。背景音乐避让默认关闭，须同时验证音箱实际 AirPlay/mysoftvol 路由并开启 HA 选项。

将实体 ID 换成实际设备的 ID。主动播报和对话是不同动作：

```yaml
action: xiaoai_speaker.announce
data:
  entity_id: media_player.your_speaker
  text: "客厅窗户还没关。"
  volume: 45
  priority: normal
  wait: true
```

```yaml
action: xiaoai_speaker.send_text
data:
  entity_id: media_player.your_speaker
  text: "打开客厅的灯，然后告诉我结果"
  speak_response: true
  use_context: true
  wait: true
```

```yaml
action: xiaoai_speaker.set_volume
data:
  entity_id: media_player.your_speaker
  target: auto
  level: 30
```

`target` 支持 `auto|master|tts|bluetooth|headphone|airplay|all`。空闲策略支持 `master|tts|master_tts`。`all` 在 AirPlay 活跃时排除主音量，且先检查所有目标能力。

MPD 示例、任务查询、自动化和可选 Dashboard 文字输入见 [examples/](examples/)。默认只向 Assist 暴露有需要的媒体播放器；将 TTS 音量、监听管理、Agent、重置按钮留在高级界面，避免泛化语音指令误操作。

## 安全和行为边界

使用随机桥接令牌、局域网和 IP 白名单；HTTP 本身不加密，应仅用于可信网络，禁止公网转发。音箱配置为 `0600`，不保存另一份 HA 长期令牌；现有 OAuth 与 Agent 配置仍由原脚本加载。支持的操作固定，文本不会当 Shell 执行。

优先级 `urgent` 只排在待执行任务前面，不打断原语音或正在执行的 Conversation。正常唤醒优先于桥接自己的播报，并在录音前保留音频锁。正在运行的对话不可取消，防止意图已经执行而错误宣称撤销；可取消排队任务和桥接自己启动的播报。HA 卸载会清理其任务/事件订阅，不会关闭音箱原语音服务。

详细限制、状态机、失败恢复、认证和边界值见 [协议与安全行为](docs/protocol.md)。

## 开发与验证

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
make -C speaker
make -C speaker check
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python tools/validate_metadata.py
.venv/bin/python tools/privacy_audit.py
```

CI 检查真实 HA 2026.10 测试框架、合成设备回归、C 编译/ASan/UBSan、ARM EABI5 静态构建、QEMU CLI、HACS、hassfest 和公开资料扫描。QEMU 与开发机测试不是 LX06 真机测试。

- [功能覆盖和责任矩阵](docs/coverage.md)
- [测试证据和待执行验收](docs/testing.md)
- [资源采样与验收报告模板](docs/resources.md)
- [故障排查](docs/troubleshooting.md)

公开仓库与通用产物不包含真机 IP/账号、配置快照、原语音脚本、私有生成副本、会话或资源采样。私人调查报告与审阅差异只在工作站 `.private/` 中保留。

参考：复用 [xiaoai-patch](https://github.com/duhow/xiaoai-patch) 既有链路，直接使用 [MPD 协议](https://mpd.readthedocs.io/en/stable/protocol.html)、[Shairport Sync](https://github.com/mikebrady/shairport-sync) 本机接口与 [HA Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/)。本仓库新代码采用 MIT；用户本机原脚本保留各自许可。
