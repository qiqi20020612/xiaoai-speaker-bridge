0.1.5 是下一次 LX06 测试版本，直接从已验证的 0.1.3 升级；无需安装或验收 0.1.4。本轮发布未部署、重启或操作真实 LX06/HA，自动化检查不能替代真机验收。

Release 提供 LX06 安装包、HA ZIP、`RELEASE_NOTES.md` 和 `SHA256SUMS`。下载同一版本的全部附件，先执行 `sha256sum --check SHA256SUMS`（macOS：`shasum -a 256 --check SHA256SUMS`）；私有适配另外核验 review.json。

- 修复只读审计 F01～F07：真实 Launcher 的 LED 1/2/11、已启用 0.1.3 的完整失败恢复、AW20054 BGR、恢复锁失败清理、ducking 接管、Microsoft/Google/豆包播放检测、KWS 重入与私有进程身份。
- review format=3：用本版 prepare_adapters.py 从未修改的 Launcher、kws_guard 和 AirPlay LED 原件重新生成三个私有副本，核验原始字节 SHA256、输出 SHA256 和完整顺序；不更改设备原 KWS client/guard、OAuth、Agent 或 TTS 脚本。
- 音量恢复记录逐通道保存，锁或硬件失败也释放音频/语音/LED所有权；对已改变的 AirPlay 发送端目标保守恢复，新 HA 音量目标优先。目标仍与临时 cap 无法区分时保留 pending，不猜测硬件映射。
- 安装事务协调原 OAuth Cron 的目录锁、新配置 FD、原 Listener/Bridge 写入与实际初始化就绪；等待刷新后重新读取已轮换凭据；初始化 401 不被安装锁阻断。恢复保留旧二进制、私有适配、enable、KWS 和当前 OAuth。
- 安装、回滚、卸载兼容旧单 PID 和新 PID/starttime 语音占用标记，拒绝活跃语音时不修改事务状态或停止服务；启动时间校验防止遗留标记误认重用的 PID。
- 保护完整 AirPlay draw/restore 与后台 worker；清理过期 worker 目录；远程任务与语音共享 LED 锁，语音优先。原 ledserver、KWS离线灯和物理按键仍须实测。
- 音频任务清理宽限 20 秒；probe 原 5 秒宽限和启动重试预算保留。boot 完成后的 Cron 仅恢复 Bridge daemon，不重复原音频启动流程或启动被用户停止的 Listener。
- 保留 protocol=1、原 HA 实体身份、上下文和便捷操作；新 ducking capability 防止 HA 先升级时发送旧 Bridge 未支持的新选项。task_led、announcement_ducking 均默认关闭。
- 完整脱敏 LX06 Launcher/KWS/AirPlay/TTS 样本与故障注入；CI 包含真实 BusyBox、HA、C sanitizers、HACS、hassfest、静态 ARM EABI5 hard-float 和 QEMU HTTP/worker 校验；发布附件有内外 SHA256，私有配置和适配副本不入公开包。

**升级与失败恢复**：[0.1.3 → 0.1.5 专项说明及真机清单](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.5/docs/lx06-v015.md)。先排空 HA 队列、暂停新输入，保留私有备份，重新生成审核适配器，再预览和安装；非零退出即停止检查。recovery_required 用同一完整 0.1.5 包 --rollback，不能用卸载代替恢复稳定 0.1.3。已有启用状态保留，无需重复 activate。

**仍需重点验证**：共享 mysoftvol 的发送端/录音/ducking 并发、真实 LED/录音时序、OAuth reload、短播放轮询、KWS离线/物理灯竞争、掉电/OOM/重启和 ≥31 分钟资源稳定性。音乐避让和任务灯在各自真机验收前继续关闭。二进制只针对 LX06 的 ARM32 EABI5 hard-float 用户空间；其他型号及备用 LED 驱动未被此次证明。
