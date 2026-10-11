# 故障排查

| 现象/错误 | 检查和处理 |
|---|---|
| cannot_connect | bridge 是否本机健康、IPv4/端口/白名单/防火墙，HA 与音箱 LAN 是否可达 |
| auth_failed | 本机随机 bridge token 与 HA entry 一致，配置 `0600`；重认证不改 SSH 或 HA 长期 token |
| device_identity_changed | 地址指向不同设备或 UUID 被重建；不要让旧 entry 的持久音量写到新音箱，按新设备添加 |
| voice_adapter_not_ready | 私有 listener 副本未激活或 voice_ready PID 已失效；核对 source/output 哈希和原启动链 |
| unsupported_volume_target | 对应控件不存在或反向能力未验证；AirPlay 不会降级为 master |
| volume_unavailable / playback_unavailable | 方法虽存在但发送端/播放器当前不可用；恢复真实连接后再试 |
| busy | 语音/会话/硬件锁有使用者；HA 排队，勿另起音箱业务队列或强杀 KWS |
| voice_preempted | 正常唤醒优先，桥接自己的播报被取消；不是原语音被中断 |
| recursive_text_request | 自动化尝试把当前同一文字回送；修改自动化触发条件，announce 与 send_text 分开 |
| unknown / job_timeout | 请求可能已执行但响应丢失；先核实结果，不盲目重发有副作用的文字 |
| tts_failed | 任务以原 TTS 的真实退出码结束；保持原回退配置，私下检查供应商/音频链 |
| state_stale | probe 失败，实体暂不可用；本机只读检查依赖，不填假数据 |
| MPD 不可用 | 原生端口/密码/commands 权限；无需安装 Linn，不能以 MPD 命令代替蓝牙/AirPlay |
| 启动后没有 done | boot 等待至少 90 秒；stop 异步最多等 15 秒，bridge 健康失败会让 Cron 下次重试 |
| `flock: invalid option -- 'w'` / 0.1.0 部分安装 | 使用 0.1.1+ 完整修复包在 `/tmp` 运行新版 `--rollback`，再 `--apply`；见 [恢复步骤](install.md#8-从-010-的-busybox-启动失败状态恢复) |
| 备份或 staging 已存在 | 看 `backup/status`；未完成/旧无标记事务先新版 `--rollback`/`--uninstall`。已恢复状态可直接重试，旧副本编号保留，不能先删除私有配置 |
| 修改并发/哈希不符 | 原脚本/OAuth 更新了；重新只读获取、生成、审阅，不能覆盖刷新后的配置 |

本机日志只有 `/tmp/xiaoai_boot.log` 和 `/tmp/xiaoai_bridge.log`，有界轮转；worker stderr 不进入永久日志。HA 的集成诊断已去除身份和对话字段，发 Issue 前仍应检查内容。不要发原 `/data/listener`、完整 private 目录、token、配置包或会话缓存。

禁用 HA 集成不会停音箱原服务。若需要移除设备 bridge，先用安装器撤销 adapter include/LED guard 并恢复原 listener，再移除 active 目录。不要用 `killall` 解决 bridge 问题，也不要卸载 Snapclient/upmpdcli。
