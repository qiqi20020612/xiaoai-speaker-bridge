0.2.1，以已发布 v0.2.0 为基线，修复 Shairport Sync 4.3.5 Remote 音量滞后导致 AirPlay Duck 无法结束恢复的问题。

- 项目、HA manifest 与 Bridge VERSION 同步为 0.2.1；协议继续为 1，配置、实体 Unique ID、持久音量和 Journal 格式保持兼容。
- 修复主 `Volume=-18`、Remote `AirplayVolume=-19.875`、ALSA `177,177` 且目标计算和严格读回成功时，`sender_recovery=true` 仍持续 PENDING 的情况。Remote 来自元数据线程对全局值的采样，可能滞后，不能作为与主值两位小数相等的应用确认。
- 在已有 sender PENDING 分支内，允许合法 Remote 保持恢复前数值或追平当前精确主值；变成其他值、未知、空值、非有限数或越界观测继续保留 Journal。
- 写前检查主 Volume/Active，拒绝已知过期目标写入；写后先读 Remote 再精确复查主值，检测计算后、写入中及最终查询期间的发送端再次调音，不舍入或使用容差掩盖变化。
- 保留 iPhone 最新音量保护、PID/starttime、Profile/版本、配置/TLV、双声道整数 cset 与严格读回、Volume Lock、音量版本、HA 新目标优先、静音确认、失败恢复及 duck→voice 交接。不写 Shairport Volume 或 SetAirplayVolume。
- 不新增常驻进程、配置或轮询，不改变 HA 音量显示、Listener、KWS、STT、TTS、LED 或 Shairport 配置。仅活跃 sender PENDING 恢复增加一次写前 Remote 查询和一次主接口查询，沿用原 600 ms 回复超时；没有真机耗时或资源改善数据。
- 已合并修复的本地相关回归 319 项、两组完整 CI 各 1125 项通过，覆盖本次真机数据、Remote 边界/滞后/追平/变化、真实发送端再次调音的受控注入、进程身份、静音及失败保留；v0.2.1 tag 通过同一套 CI、ARM/QEMU 和包校验后才发布。
- 详细行为、官方源码证据及未测边界见 [v0.2.1 恢复记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.2.1/docs/airplay-sender-recovery.md)，修复由 [PR #11](https://github.com/qiqi20020612/xiaoai-speaker-bridge/pull/11) 合并。

升级前下载同一版本的 LX06 包、HA ZIP、`RELEASE_NOTES.md` 与 `SHA256SUMS`，核验外层及包内 SHA256，保留配置、私有适配器和有效历史备份，沿用原安装事务原地升级。无需卸载、重新配对、清除 PENDING Journal 或改动 Shairport 配置。失败保留恢复依据，使用原安装事务及历史包回滚；回退到 [v0.2.0](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases/tag/v0.2.0) 会重新带回 Remote 确认问题。

上述证据来自用户观测、官方源码和离线合成/QEMU 环境，不是修复后的真机验收。LX06 的连续 iPhone 调音、静音/停止、HA 新目标、语音抢占、听感及资源占用仍待真机复验。本次发布不包含真实 LX06 或 Home Assistant 的部署。
