0.1.7 是功能与代码精简版本，包含 LX06 原生 LED 重构。改动通过 PR #6 合并；开发与发布未连接或操作真实 LX06 / Home Assistant。

- HA 完整 LX06 能力下保留 14 个实体，所有保留 Unique ID 不变。Media Player 保留标准播放状态和原音源属性；AirPlay 百分比传感器附加原始 dB。
- 删除 light、binary_sensor、select 平台与实现；移除手动 RGB、来源/语音/AirPlay 状态展示、两个对话开关、Agent 选择、空闲策略及多余诊断/通道实体。按当前配置条目、平台、域和 Unique ID 精确清理旧注册项，禁用/改名同样处理；保留设备身份、配对、凭据和其他用户配置。
- 快捷文字仍通过原 announce/send_text 服务，提交成功后清空；speak_response/use_context 动作参数保留、默认 true。删除 agent_id 动作参数、Agent 覆盖 API/状态读写/独立缓存逻辑，固定使用原 Listener/EOAIC2 配置。旧覆盖记录不再影响执行；重置会话清除原 EOAIC2_STATE，原 OAuth 与五分钟上下文保留。
- 删除 set_idle_volume_policy 服务和配置，空闲 auto 固定 master；活动 AirPlay/蓝牙/MPD 自动路由、显式通道和双重衰减保护不变。原音源优先级继续可配置。
- 重启按钮名称和行为改为只重启 Bridge，保留 restart_listener Unique ID 与自定义 Entity ID。现有 start.sh 强制跳过 Listener，短期锁防重复，立即接受调度，不入队或等待重启成功。活动任务/语音和安装竞争保护仍在。
- 删除展示专用的 uptime、MemTotal、VmHWM、会话倒计时探测/JSON、worker rusage 峰值和 HA jobs.last 重复快照；保留当前常驻 Bridge RSS、可用内存、CPU load、健康检查和内部任务接口。
- 删除 /api/led、手动 RGB/亮度/颜色 helper 和状态。任务复用 show_led 1 → 2 → 11、shut_led 1/2/3；缺少命令时安全跳过。只在开始/结束协调灯光，去掉合成/播放的重复锁操作。保留原语音优先、LED 所有权/异常清理、AirPlay Guard 与活动检查后最新音量灯恢复。
- 支持 0.1.6 原地升级：私有 format=4/hooks=4/KWS guard=1/AirPlay guard=3 不变。配置、UUID、Token、Listener/KWS/OAuth、音量曲线、有效历史备份、失败自动恢复与回滚保留。旧灯效 verified 字段忽略，不重写整份配置。

已移除实体、Agent 参数和 idle 服务属于明确兼容性变化；相关自动化须调整。恢复这些实体的旧自定义 Entity ID/选项需升级前 HA 备份。音箱回滚使用同一完整包 install.sh --rollback，不卸载重装、不删除恢复目录、不覆盖较新 OAuth。

正式 Release 附件为 LX06 ARM EABI5 hard-float 静态包、HA ZIP、RELEASE_NOTES.md 与 SHA256SUMS。验证外部与包内 SHA256 后才准备部署副本。测试覆盖 HA 2026.10、14 实体迁移、原 Agent/上下文、重启故障、真实 jq 1.6、BusyBox、音量/灯效并发、安装恢复、C sanitizers、ARM/QEMU、HACS/hassfest 和包校验。

CI 不代表固件、音频、动画或资源真机验收。完整迁移、升级/回滚与最小测试清单见 [0.1.7 精简说明](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.7/docs/lx06-v017.md)。
