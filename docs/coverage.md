# 功能覆盖与两端责任矩阵

“实现”指源码/适配接口；“自动化”指 HA 框架或合成设备验证。**用户已确认 0.1.6 真机基线通过；本轮 0.1.7 未连接或操作设备，新增改动的硬件验收均未执行。** 下面不以模拟结果代替硬件结果。私有调查细节不在公开仓库。

| 功能 | HA 责任 | LX06/复用服务 | 实现与限制 |
|---|---|---|---|
| 配置、多设备 | config/reauth/reconfigure/options、稳定注册 | UUID、独立随机鉴权 | 已实现、框架测试 |
| 状态与诊断 | coordinator、实体、白名单导出 | 低频 probe、Unix 事件、现有服务只读接口 | 已实现；缺失值 unknown，不捏造 |
| MPD | 原生协议、队列、元数据、控制、动态 features | 现有 MPD；本地只读状态供来源快照 | 已实现、原生协议模拟联调；真实控制待测 |
| AirPlay | 聚合状态、独立软件音量展示 | Shairport DBus、原音量事件文件 | 已实现读取与受控写适配；反向发送端同步未验证前关闭 |
| 蓝牙 | 聚合状态与真实动作显示 | BlueZ、bluetoothctl、实际 ALSA 控件 | 已实现适配；存在播放器且逐项验证后才能写 |
| 独立音量 | 规则、持久 desired、固定空闲 master/可配置来源优先级 | ALSA/DBus 固定硬件动作、每通道版本 | 已实现；无 voice hooks 时拒绝不安全主/蓝牙写入 |
| 唤醒前来源 | 在 HA 选择规则与优先级 | 录音前实时来源/音量快照 | 已实现；临时降音量不改变来源 |
| 临时音量恢复 | 保存成功确认期望、避免保存临时音量 | 硬件 flock、版本、安全恢复 | 已实现；没有持久硬件规则引擎 |
| 主动播报 | 有界队列、优先级、任务、去重/取消 | 现有 TTS 与真实回退；本地音频互斥 | 已实现；不执行 Conversation、不更新会话 |
| 远程文字 | 发文字、排队/超时/查询、循环防护 | 共同 STT 后入口、本地 intent、原 Agent、原 TTS | 已实现、语音↔文字上下文模拟测试 |
| 会话 | 文本动作参数 | 原缓存原子更新、session flock、成功滑动 TTL | 已实现；原 EOAIC2 默认缓存、单次无上下文 |
| 监听管理 | 监听开关；高级按钮仅重启 Bridge | 原 listener/procd 服务、原双通道 KWS 保留 | 已实现；活动语音拒绝管理操作 |
| 原生任务灯效 | announce / send_text 自动触发，无独立 light 平台 | 原 show_led / shut_led，既有 LED 锁/所有权和 AirPlay Guard | 默认工作、不可用时跳过；阶段/退出/抢占/升级测试，实际动画待 v0.1.7 复验 |
| IR | 固定高级动作和校验 | 存在且验证过的 IR sysfs | 已实现适配；仅 38 kHz、有限脉冲，不承诺未验证硬件 |
| 开机 | 无额外 HA 外进程 | 固定 boot/Cron/start、只停 Squeezelite | 已实现模拟异步停止测试，真实部署/重启未执行 |
| 安装与回滚 | HACS 仅安装 HA 端 | 通用包 checksum、私有 adapter hash、原始备份 | 已实现模拟安装/幂等/回滚测试，真机执行待授权 |
| 资源 | HA 承担业务、队列、缓存与规则 | 有界 C、有限 worker、三个低频资源字段、按需只读采样 | 已仪表化；新进程资源目标尚无真机结论 |

## 核心验收追踪

| 编号 | 覆盖的实现/自动化证据 | 真机结论 |
|---|---|---|
| A1 | 每通道 `volume_write`、HA 独立 number、目标预检 | 写入未执行 |
| A2 | notify 独立、临时播报音量安全恢复 | 写入/播放未执行 |
| A3 | desired 每通道版本、恢复共用硬件锁、不存临时值 | hooks 未部署，待真机 |
| A4 | wake 快照、HA 优先级、不可写 AirPlay 明确错误 | 反向控制未验证 |
| A5 | 空闲固定 master，活动 AirPlay/蓝牙/MPD 自动路由测试 | 写入未执行 |
| A6 | 中英文有限音量意图、显式 TTS 优先测试 | 真实语音待测 |
| B1 | announce 朗读“打开灯”不调用原 Conversation/缓存 | 真机播报未执行 |
| B2 | LX06 原 curl wrapper → Conversation，HA 仅入队 | 实际设备动作未执行 |
| B3 | voice→text 同 Agent/cid/五分钟缓存测试 | 实际 KWS/STT 对话待测 |
| B4 | text→voice 同上下文测试 | 实际语音待测 |
| B5 | 本地 session/audio 锁、voice 繁忙、单 C worker、HA 队列 | 并发真机待测 |
| B6 | speak_response=false 仍经过原 wrapper 的测试 | 实际对话未执行 |
| C1 | AirPlay 百分比附加 raw dB、事件文件监听，未使用 master 假同步 | 手机真实事件待测 |
| C2 | 私有 LED 副本 draw/worker 避让、预览/启用/移除接口 | 补丁未部署，真实灯效待测 |
| C3 | 原 TTS 不替换，任务失败按真实退出码、取消恢复测试 | 实际回退供应商未测 |
| C4 | 丢失 POST 回应只 GET 查 ID、超时 unknown、不会自动重播 | LAN 中断待测 |
| C5 | HA 卸载清理测试、原服务保留、boot/adapter 回滚逻辑 | HA/音箱重启待测 |
| D1 | HA 测试无 Linn，manifest 无依赖 | 安装新集成待测 |
| D2 | MPD commands/status/queue/控制原生模拟协议测试 | 真实播放控制未执行 |
| D3 | AirPlay 播放时 MPD stop，统一实体仍 playing 的框架测试 | 实际播放未执行 |
| D4 | 不同 source 的 capability 动态 features、明确拒绝 | 真实设备动作待验证 |
| D5 | 代码没有停用/代理 Snapclient/upmpdcli | 私有只读调查有记录，公开不含设备清单 |
| E1 | 所有 HA 代码在 custom_components，无第三端服务 | 新集成部署待测 |
| E2 | uptime 90 秒、Cron、退出等待/超时测试 | 原机制只读核对；新 boot 未部署/重启 |
| E3 | boot/install 仅停 Squeezelite 的模拟测试 | 私有只读调查有记录 |
| E4 | C flock 多实例测试、start 幂等/健康等待 | 真机 start 未运行 |
| E5 | 原 Cron 原任务、新增任务、唯一 boot 行安装回归 | 真机 Cron 没有写入 |
| E6 | 未安装 bridge 安全跳过，done 语义实现 | 原现场只读核对；新实现未写入 |
| E7 | 有界 resident/worker、采样脚本；≤3 MiB 优化、≤5 MiB 预算、idle <1% 为目标 | 未测新增资源，不能判定通过 |
| E8 | 此矩阵保留全部确定功能和待验证硬件适配 | 待写入/负载验收完成 |
| E9 | HA 负责策略/队列/缓存，多设备隔离；LX06 仅执行/互斥 | 源码审阅与结构验证 |
| E10 | Bridge RSS/可用内存/load 的常规探测；进程树/高水位/CPU/tmp/data 按需采样方案 | 部署、30+ 分钟、负载场景未执行 |

尚缺的是**设备写入授权后的部署和真实硬件/资源证据**，没有因内存目标删除确定功能。未验证的高级能力保持检测/适配，不能设置验证标志来代替验收。
