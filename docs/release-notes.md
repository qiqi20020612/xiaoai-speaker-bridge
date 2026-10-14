0.1.7 移除独立 LED 灯环控制，HA 主动播报和文字对话默认复用 LX06 的原生灯效。当前为待审阅版本，尚未合并或发布；没有操作真实 LX06 或 HA。

CI 产物包含 LX06 ARM EABI5 hard-float 静态安装包、HA ZIP、`RELEASE_NOTES.md` 和 `SHA256SUMS`。在同一目录执行 `sha256sum --check SHA256SUMS`（macOS：`shasum -a 256 --check SHA256SUMS`），通过后才准备部署副本。

- 删除 HA `light` 平台、手动 RGB/亮度实体及翻译、`/api/led`、设备端独立 `led` 操作、`capabilities.led` / `state.led`、手动状态记录和 RGB JSON helper。旧控制接口返回 404。其他实体 Unique ID、两个文本操作和现有服务 API 保留。
- 首次成功连接后，精确清理本配置条目、`xiaoai_speaker` 平台、`light` 域、`<设备 Unique ID>_led` 的旧实体，包括禁用和用户改名的实体。设备注册、其他实体、配置和自动化不删除；曾引用旧灯环的自动化需由用户调整。
- 原 Launcher 的顺序是 `show_led 1` → `show_led 2` → `show_led 11` → `shut_led 1/2/3`，其中 `show_led 3` 已注释。远程任务保持相同原生动画，处理/合成/播放期间沿用 2；不启动录音或 STT，不增加颜色渲染、配置或常驻进程。
- 原生命令存在时任务灯效自动工作，旧 `verified.led` / `verified.task_led` 值保留但不参与判断。命令缺失或失败不影响播报/对话。保留 FD 灯锁、PID/starttime 所有权、语音优先和原 AirPlay 音量灯/Guard。
- 正常完成、失败、取消、超时按任务所有权收尾。语音抢占后由语音完成原生收尾；同一文字任务仍存活时续接处理动画，已退出时恢复延期的 AirPlay 灯效。SIGKILL 后使用启动、下一次任务或现有 Cron 的一次性恢复，不新增定时任务。
- 语音原生收尾未确认成功时保留/重试任务恢复标记。已启用 Guard 必须有符合 source SHA256 的原始备份，缺失或伪备份时安装/恢复/卸载及独立工具均停止，保留仍被引用的 helper。
- v0.1.6 可原地升级，私有适配契约仍为 format=4 / listener_hooks=4 / KWS guard=1 / AirPlay guard=3，不要求重新生成有效适配器。原生命令和 AirPlay hook 存在时安装要求匹配的私有 Guard，自动复用原工具启用，已有活跃匹配 Guard 不动。快照记录升级前启用状态：失败/回滚保留原先已启用的 Guard，本轮新启用的则恢复原程序。身份、认证、Listener/KWS、AirPlay 曲线、历史备份及当前 OAuth 值保留。

CI 验证 HA/旧实体迁移、真实脱敏 Launcher/AirPlay 结构、jq 1.6、BusyBox、锁/进程退出/取消/安装故障、C sanitizers、ARM QEMU、HACS/hassfest 和包内外 SHA256。自动化不验证原生程序在真机上的动画时长或音频效果。详情及最小真机清单见 [0.1.7 原生灯效说明](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/codex/native-led-v017/docs/lx06-v017.md)。
