0.2.0，以已发布 v0.1.9 为基线，修复 HA 音量显示和 LX06 AirPlay Duck 恢复兼容问题。

- 项目、HA manifest 与 Bridge VERSION 同步为 0.2.0。
- 修复 HA 音量显示回弹：主音量、播报音量、蓝牙音量和媒体播放器立即显示最新目标；失败恢复最近成功值并正常报错，成功后通过后续有效 Probe 同步实际值。
- 保护连续设置、旧响应、空 Probe、语音/Duck 延迟应用和临时衰减；刷新显示不重发音量控制，保留既有 Store、设备版本和 Journal 恢复语义。本项运行代码仅改 HA 集成。
- 修正文字音量指令成功回调被旧显示目标遮蔽的问题；持久化成功后同步更新显示及查询代次，后续相对操作使用新目标，失败保留原值。
- 修正取消排队音量请求时的显示回退：恢复前一个仍在执行或排队的目标，保留相对调音的正确起点，相同目标的请求按身份独立清理。
- 修复 v0.1.9 在 LX06 上的 AirPlay Duck 恢复写入兼容：将不兼容的负 dB `sset` 改为按真实 ALSA TLV 量化的双声道整数 `cset`，按 Shairport/ALSA 最近刻度规则舍入，写后严格读回；失败保留 Journal，不覆盖 iPhone 最新音量。
- 补充严格命令契约、无效 TLV、舍入、单声道/无效读回、native/sanitizers 与 ARM/QEMU 回归；原 HA 版本保护、静音/停止、锁、抢占与失败恢复继续保留。该 ALSA 修复尚未部署到真机验收。
- 新增真实 HA 服务/实体回归，覆盖并发、失败、路由及重启同步；隔离的 v0.1.9 源码对照复现四个入口的旧显示行为。真机 HA/LX06 UI 和声音恢复仍待部署验收。
- 行为、兼容性、验证证据和待测边界见 [v0.2.0 修复记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.2.0/docs/lx06-v020.md)。
- 回退基线为 [v0.1.9](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases/tag/v0.1.9)，回退后仍可能遇到旧版 dB 写入失败；其修复及真机边界见 [版本记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.9/docs/lx06-v019.md)。

升级前下载同版本 LX06 包、HA ZIP、`RELEASE_NOTES.md` 与 `SHA256SUMS`，校验外层及包内 SHA256，保留现有配置、私有适配器和有效历史备份后按原有事务原地升级；无需卸载、重新配对或改动 Shairport 配置。协议、实体 Unique ID、持久音量与 Journal 格式兼容。失败沿用安装事务和历史包恢复，不删除恢复依据。

完整 CI 及两端打包流程校验 ARM ABI、版本和包内文件；1074 项自动化回归通过。上述结果来自离线合成环境与 QEMU，新 ALSA 写入、HA 显示及听感仍需 LX06 真机复验。本次发布不包含真实 LX06 或 Home Assistant 的部署。
