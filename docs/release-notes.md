0.1.8 以 v0.1.7 为基线，包含语音关键路径局部优化和媒体显示增强。本次发布通过自动化验证与离线测量，尚未部署新版 LX06 / Home Assistant；AirPlay 元数据仅经用户授权进行了真机只读格式核对，未修改设备。

- 本轮已取得且有效的 Listener 自身 starttime 显式复用于 voice owner 和 Journal；为空或无效时重读校验，仍失败则在本轮资源操作前安全退出，避免 PID 复用误判。其他调用继续重新读取身份。
- 同一份 Shairport 回复一次解析 Volume/Active；已有 Journal JSON 直接原子写入，保留验证、0600、rename、锁和恢复规则。
- 在无旧恢复/duck、需要衰减的受控唤醒路径中，减少两次 sed、三次 awk、两次 cat。两次 AirPlay 查询、BlueZ/MPD 查询、mixer 读取/写入及 JSON 验证数量保持。
- 修复查询非零退出仍采信部分 stdout，或拼接 fallback 后将未知误判不活跃；失败注入覆盖 policy、AirPlay、BlueZ、MPD 和 Journal 基线。
- 复用默认关闭的 XIAOAI_PROFILE；详细阶段和 First PCM 分别额外 opt-in。普通录音不运行 trace helper 或 PCM 解析；诊断保持字节透传、不保存音频，First PCM 不是 ALSA 首样本或声学首字。
- 增加 cycle 汇总和离线 A/B 工具；正常流程、未知/多源、并发恢复、原 Agent/上下文、14 实体、安装和回滚继续回归。
- 修正安装文档中已删除的 Agent Select 与空闲音量策略说明。协议、配置、实体 Unique ID、私有适配格式不变。
- 「播放文字」「执行文字指令」两个原生 TextEntity 成功入队后保留完整原文，下一次成功提交时更新，提交失败不覆盖原状态；保持原生输入、任务反馈与并发保护。
- 主动播报及有声文字回复自动压低唯一、确认活跃的 AirPlay 背景音乐至原生默认 10% 目标，低音量不提高，结束/失败后复用现有 Journal 恢复与中途调音保护。移除 HA 避让开关/上限与音箱专用验证标记；保留音源未知/冲突保护、语音抢占和原值交接，不重复衰减原生语音。旧 HA 选项精确清理，协议中的合法旧 duck_volume 数值忽略。
- Journal 后一次 AirPlay 观测缺少有效恢复基线时跳过衰减；恢复待处理时，HA 不把临时硬件音量导入持久期望。
- 修复 Probe 跨恢复边界的音量快照：非阻塞地在 Volume Lock 内关联 mixer 与三类 Journal，避免播报恢复后仍把旧的 10% 保存为 HA 期望；锁忙跳过本地音量读数，DBus/BlueZ 不持锁。
- 复用原有 GetAll/Probe，在原 media_player 显示 AirPlay 歌曲、歌手、专辑、时长与估算进度；封面经受限、认证接口与 HA 原生图片代理按需提供。音箱不复制图片，HA 仅缓存一张并有界过期/清理；停止、查询失败或路径/图片不安全时降级。无新实体、D-Bus 查询、常驻进程或 Shairport 配置改动；未改变音频、音量或 LED。进度不是采样级同步，新版真机 UI/性能仍未验证。

未实施缓存快路径、优先级短路、锁重写或提前 voice_begin；保留两次 AirPlay 观测时刻、原录音/STT/提示音、三个 Journal、通道版本、发送端新音量保护、duck 交接、音频/会话互斥及原 LED Guard。

离线结果只验证调用数量、输出等价和受控阶段；没有 v0.1.7/v0.1.8 LX06 性能、CPU/RSS 或首字完整性结论。完整测量条件、证据和真机清单见 [v0.1.8 说明](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.8/docs/lx06-v018.md)，包内说明也使用这一版本链接。

后续另获部署授权后，取得同一构建的 LX06 ARM 包、HA ZIP、RELEASE_NOTES.md 与 SHA256SUMS，执行 sha256sum --check SHA256SUMS（macOS 使用 shasum -a 256 -c SHA256SUMS），再核验音箱包内清单。校验通过后沿用原地升级，不卸载重配。失败保留恢复目录：failed_restored 表示自动恢复；recovery_required 用同一新包 install.sh --rollback 继续恢复。主动回退也由新包恢复 v0.1.7 快照，保留 UUID/Token、私有适配器、启用状态、有效历史备份和最新 OAuth，不回写旧认证整份备份。本轮未实机安装或回滚。
