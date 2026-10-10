# 测试证据与边界

## 自动化

CI 使用 Python 3.14、Home Assistant 2026.10.0、官方 HA fixture 的 custom-component 包；依赖固定于 `requirements-dev.txt`。HA 2026.10 实际校验库为 probatio，本集成按此环境进行配置流/动作 schema 测试。

| 测试 | 覆盖 |
|---|---|
| `test_homeassistant.py` | config flow、鉴权失败、稳定身份、实体、AirPlay active/MPD stop、无 Linn、服务定位、options、卸载 |
| `test_volume.py` | 冻结来源、显式目标、空闲策略、并发优先级、AirPlay 双重衰减预检、中英文音量规则 |
| `test_mpd.py` | 真正 asyncio 原生协议对话、队列、安全转义、ACK、坏参数先验证再清队列 |
| `test_client.py` | 分段 HTTP、回复上限、不确定 POST 标记 |
| `test_bridge.py` | 本机真实 C HTTP 进程、认证、阶段状态、忙碌、幂等/冲突、输入/Unicode/HTTP 限制、Unix voice event、取消、多实例 |
| `test_jobs.py` | 失去接收回应仅查询一次、队列优先级/取消、同活跃文字递归防护、多设备隔离 |
| `test_text_pipeline.py` | 实际通用 shell 文本流程 + 合成原 Agent/硬件；语音↔文字同缓存、silent reply、失败不续期、临时音量取消恢复 |
| `test_boot.py` | 原 stop 先返回、进程随后退出；只 stop Squeezelite，done/失败重试 |
| `test_install.py` | 真实安装器对合成文件系统的 dry run、幂等、Cron 保留、首次回滚、卸载、单行 activation/当前鉴权值保留 |
| `test_adapters.py` | source 顺序/锚点、保留 KWS、未知版本拒绝、LED 避让、所有脚本 `sh -n` |
| `json_test.c` | ASan/UBSan，严格 JSON、嵌入 NUL、坏 UTF-8/代理对/重复 key |

测试替身都明确标记 synthetic，未包含任何本机原配置或用户语音。最终每次实际执行的数量和时间以 [CI](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions)及 JUnit 产物为准。

ARM CI 检验 ELF 头、无动态 INTERP、QEMU `--version`。这验证目标 ABI/程序入口，不验证扬声器、DBus 回控、发送端滑块、LED 或峰值资源。

## 未执行的真机验收

因为本次设备访问明确禁止写入，以下都未执行：安装/激活、任何音量/LED/IR 写入、TTS、远程 Conversation（可能执行设备动作）、媒体播放控制、关闭/重启 listener、Cron 写入、新 boot 的重启实测、新 bridge 的长时间/并发/资源测试。

只读调查核对了原调用链、原 boot/Cron、ABI、原服务、现有接口与状态。私有原始材料及可行性报告与公开源码分离；公开库不给出用户设备清单、地址或测量样本。

完成设备部署后按 [覆盖矩阵](coverage.md) A1–E10 回归、按 [资源报告](resources.md) 采样，保留每项证据。发现硬件限制时先报告数据，不把 verification flag 当测试通过。
