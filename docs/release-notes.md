0.1.6 修复 LX06 真机交接的 jq 1.6 文本拒绝、唤醒到录音的重复 JSON 初始化开销及结束灯清理路径。稳定 0.1.3 可直接升级；无需中间安装。没有连接或操作真实 LX06 / HA，自动化通过不等于真机验收。

附件包含 LX06 ARM EABI5 hard-float 静态安装包、HA ZIP、RELEASE_NOTES.md 和 SHA256SUMS。在同一目录执行 `sha256sum --check SHA256SUMS`（macOS：`shasum -a 256 --check SHA256SUMS`），通过后再准备私有部署副本。

- **P0**：用 Unicode 码点检查替代 jq 1.6 有缺陷的 `contains("\u0000")`；正常中文/英文/引号/换行/emoji 可用，NUL 与畸形字段在业务动作前拒绝。CI 全部音箱脚本测试使用校验过的官方 jq 1.6。
- **录音关键路径**：在现有 Bridge 内增加有限的固定 JSON 操作，录音前正常路径及 duck 接管不启动 jq；保留同步来源检查、音量记录和原有锁。没有新增常驻服务或预录缓冲，实际提速与首字完整度由真机测量。
- **结束灯**：最后一次 TTS / 临时音量恢复后，可先结束当前周期的 LED 11/shut，再做回复构造与 guard 清理；父 Listener 的结束动作幂等，音频/LED/voice 所有权和恢复记录保留到最终清理。增加跨 PID 同周期的可选阶段 trace，默认关闭，不记录音频/文字/凭据。
- 保留音量锁、逐通道恢复、发送端不确定性保护、取消/抢占、AirPlay 控制、KWS、OAuth 自动刷新、五分钟上下文、protocol=1 和原 HA 实体/便捷操作。任务 LED 与音乐避让默认关闭，不自动设置 verified。
- **必须重新生成私有适配**：review format=4 / listener_hooks=4；KWS guard=1、AirPlay guard=3。旧 format=1/3 在替换前拒绝，用本版工具从原始三个脚本重新生成并审阅，不能只改格式数字。原认证配置不需要上传；失败回滚仍接受升级前 format=1。
- 回归包含真实 Launcher/KWS/AirPlay/TTS 脱敏结构、jq 1.6、BusyBox、JSON 对照和 ASan/UBSan、锁/安装/OAuth/取消故障、QEMU ARM CLI/HTTP/固定操作与录音数据透传、HACS/hassfest 和内外 SHA256。

**完整升级、失败恢复、格式迁移、阶段测量及亲测清单**：[LX06 0.1.6 专项说明](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.6/docs/lx06-v016.md)。排空任务、暂停新输入，保留私有升级前快照，审核 format=4，预览通过后安装。非零退出即停止，recovery_required 用同一完整 0.1.6 包 --rollback；不用卸载代替恢复，不覆盖已经刷新的 OAuth 配置。真实音频/LED/发送端竞争、首字完整度及资源峰值仍须用户验收。
