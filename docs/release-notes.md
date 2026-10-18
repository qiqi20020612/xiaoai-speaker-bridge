0.2.0 开发中（草稿），以已发布 v0.1.9 为基线。

- 项目、HA manifest 与 Bridge VERSION 同步为 0.2.0。
- 修复 HA 音量显示回弹：主音量、播报音量、蓝牙音量和媒体播放器立即显示最新目标；失败恢复最近成功值并正常报错，成功后通过后续有效 Probe 同步实际值。
- 保护连续设置、旧响应、空 Probe、语音/Duck 延迟应用和临时衰减；刷新显示不重发音量控制，保留既有 Store、设备版本和 Journal 恢复语义。运行代码仅改 HA 集成。
- 新增真实 HA 服务/实体回归，覆盖并发、失败、路由及重启同步；隔离的 v0.1.9 源码对照复现四个入口的旧显示行为。真机 HA/LX06 UI 和声音恢复仍待部署验收。
- 行为、兼容性与验证证据见 [v0.2.0 开发记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/codex/v020-development/docs/lx06-v020.md)。
- 当前正式版仍为 [v0.1.9](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases/tag/v0.1.9)；其修复及真机边界见 [版本记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/v0.1.9/docs/lx06-v019.md)。

后续正式发布继续使用现有完整 CI 与两端打包流程，校验 RELEASE_NOTES.md、SHA256SUMS、ARM ABI、版本和包内文件。本次未创建 v0.2.0 Tag/Release，未部署或操作真实 LX06 / Home Assistant。
