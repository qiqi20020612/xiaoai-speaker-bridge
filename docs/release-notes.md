0.1.4 正式版本修复此前 0.1.3 验收中发现的问题。本次仅发版，没有真机部署；新行为由用户升级后复验。本包仅针对用户实际测试过的小爱音箱 Pro LX06，二进制为 ARM32 little-endian EABI5 hard-float、静态 ELF。其他型号须独立验证。QEMU 的 CLI 检查不代表硬件兼容。

修复与变化：

- HA 2026.10 配置表单改用三个原生排序选择器，保留旧有序配置。
- 唤醒亮灯提前到状态采集之前；录音仍等待衰减前快照和音频锁。减少本地 jq 启动，并提供默认关闭的有界阶段记录。
- 远程任务灯效区分处理、合成、实际播放；新增独立 `verified.task_led=false` 开关，同时要求 `verified.led=true` 和新版 AirPlay LED 保护已审核启用，语音抢占时让出。旧手动 LED 配置不会自动打开远程灯效。
- HA 增加非敏感播报/对话文本、发送按钮和回复播报/共享上下文开关。输入不自动发送。**文本会进入 HA 状态及可能的历史、日志和事件；发送后清空不删除历史。**
- 主动播报 ducking 默认关闭；HA 选项与音箱 `verified.announcement_ducking` 均启用后，只对单一确定的 AirPlay 路径使用 mysoftvol 音量上限，默认 10%。发送端音量和 notifyvol 独立，取消/失败/抢占按版本恢复。
- 能力后续出现时注册实体，能力消失或离线时不可用；保留旧 Entity ID 和用户启用选择。新安装不创建三个低价值诊断传感器；已注册的实体继续兼容。
- 来源不确定性按优先级和衰减前观测判断；下级未知来源不遮盖确定的首选音源，并保留并发、上级未知及过期状态提示。

升级：先在同一下载目录执行 `sha256sum --check SHA256SUMS`（macOS 使用 `shasum -a 256 --check SHA256SUMS`）。HACS 方式照常更新 HA；手工安装解压 HA ZIP 到配置目录并重启 HA。HA 选项向导显示三项优先级，新输入不影响已保存的顺序。

音箱升级由用户手动执行：从本机保存的**未修改原 launcher/LED 脚本**用本版 `prepare_adapters.py` 重新生成并审阅私有适配副本，放入解压后的 `xiaoai_bridge/adapter/`；用新包 `install.sh --dry-run` 检查后执行 `--apply`。安装器在替换文件/重启服务前检查 review 格式、原始/生成 SHA256、wake hook 顺序及重复 LED 命令；旧版不兼容副本明确拒绝并要求重新生成，相同包重装也不会绕过校验。只沿用符合本版规则的私有副本。私有配置、UUID/token 和旧实体 ID 保留，禁止把私有副本上传仓库。详见 [安装与回滚](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/main/docs/install.md)。

**已启用旧 AirPlay LED 保护的用户**：若预览提示 active guard 不匹配，先使用仍在运行的旧安装目录 `tools/activate_airplay_led.sh --remove` 恢复原 LED 程序，再升级；安装后审阅并执行新版 `--dry-run`/`--apply` 重新启用保护。升级期间保持 `verified.task_led=false`；确认灯原语、共享 LED 锁和语音退让后才打开。音乐避让仍需 HA 选项与 `verified.announcement_ducking=true` 两端显式开启，默认关闭。

HACS 仍从同一仓库的 `custom_components/xiaoai_speaker/` 安装，manifest 与音箱版本均为 0.1.4；HA ZIP 是手工安装包，不更改 HACS 下载布局。Release 包含 LX06 安装包、HA ZIP、更新说明及 SHA256SUMS；先上传草稿再下载复核，任何失败不覆盖已有 Release。输入文本前请阅读 [隐私与 Recorder 排除](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/main/docs/acceptance-fixes.md#文本输入隐私)。

回滚：使用这次完整安装包的 `install.sh --rollback`，按安装事务状态恢复旧桥接和自己的 boot/Cron 变更；不要用旧 OAuth 配置整份覆盖当前配置。HA 回退到原版本后重启，新增实体可能不可用，旧 ID 不改名。

自动测试覆盖合成设备/真实本地 HTTP/HA 表单和实体。新修复的 LX06 真机复验仍由用户完成：配置入口、KWS→LED 和录音时序、E1～E5、G4～G8、H1～H5、重启与 ≥31 分钟空闲及负载资源测量。没有真实模型/TTS 网络阶段数据时，不宣称对应性能收益。
