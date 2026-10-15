0.1.8 是以 v0.1.7 为基线的语音关键路径局部优化版本。目前为开发构建，尚未创建 Tag/Release；本次开发未连接或操作真实 LX06 / Home Assistant。

- 本轮已取得的 Listener 自身 starttime 显式复用于 voice owner 和 Journal；其他调用继续重新读取身份。
- 同一份 Shairport 回复一次解析 Volume/Active；已有 Journal JSON 直接原子写入，保留验证、0600、rename、锁和恢复规则。
- 在无旧恢复/duck、需要衰减的受控唤醒路径中，减少两次 sed、三次 awk、两次 cat。两次 AirPlay 查询、BlueZ/MPD 查询、mixer 读取/写入及 JSON 验证数量保持。
- 修复查询非零退出仍采信部分 stdout，或拼接 fallback 后将未知误判不活跃；失败注入覆盖 policy、AirPlay、BlueZ、MPD 和 Journal 基线。
- 复用默认关闭的 XIAOAI_PROFILE；详细阶段和 First PCM 分别额外 opt-in。普通录音不运行 trace helper 或 PCM 解析；诊断保持字节透传、不保存音频，First PCM 不是 ALSA 首样本或声学首字。
- 增加 cycle 汇总和离线 A/B 工具；正常流程、未知/多源、并发恢复、原 Agent/上下文、14 实体、安装和回滚继续回归。
- 修正安装文档中已删除的 Agent Select 与空闲音量策略说明。协议、配置、实体 Unique ID、私有适配格式不变。

未实施缓存快路径、优先级短路、锁重写或提前 voice_begin；保留两次 AirPlay 观测时刻、原录音/STT/提示音、三个 Journal、通道版本、发送端新音量保护、duck 交接、音频/会话互斥及原 LED Guard。

离线结果只验证调用数量、输出等价和受控阶段；没有 v0.1.7/v0.1.8 LX06 性能、CPU/RSS 或首字完整性结论。完整测量条件、证据和真机清单见 [v0.1.8 说明](lx06-v018.md)。

后续另获部署授权后，校验同一完整包内外 SHA256，沿用原地升级，不卸载重配。失败保留恢复目录：failed_restored 表示自动恢复；recovery_required 用同一新包 install.sh --rollback 继续恢复。主动回退也由新包恢复 v0.1.7 快照，保留 UUID/Token、私有适配器、启用状态、有效历史备份和最新 OAuth，不回写旧认证整份备份。本轮未实机安装或回滚。
