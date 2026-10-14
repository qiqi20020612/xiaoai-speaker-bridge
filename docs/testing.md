# 测试证据与边界

## 自动化

CI 使用 Python 3.14、Home Assistant 2026.10.0、官方 HA fixture 的 custom-component 包；依赖固定于 `requirements-dev.txt`。HA 2026.10 实际校验库为 probatio，本集成按此环境进行配置流/动作 schema 测试。Linux CI 从官方发布包构建真实 BusyBox **1.36.1**，固定并核验源码 SHA-256，使用 `tests/busybox.config` 明确启用 `ash`、`flock`、`awk` 和 `sed`，校验版本及四个 applet 后设置 `XIAOAI_TEST_BUSYBOX`；Shell 回归用它的 `ash` 和真实 `flock`。Ubuntu 自带 BusyBox 包没有 `flock`，不能用它替代。macOS 本地仅用拒绝 GNU 参数的后备 FD 实现，真实 BusyBox 专项会跳过，不能代替 Linux CI。

| 测试 | 覆盖 |
|---|---|
| `test_homeassistant.py` | config flow、鉴权失败、稳定身份、实体、AirPlay active/MPD stop、无 Linn、服务定位、options、卸载 |
| `test_volume.py` | 冻结来源、显式目标、空闲策略、并发优先级、AirPlay 双重衰减预检、中英文音量规则 |
| `test_mpd.py` | 真正 asyncio 原生协议对话、队列、安全转义、ACK、坏参数先验证再清队列 |
| `test_client.py` | 分段 HTTP、回复上限、不确定 POST 标记 |
| `test_bridge.py` | 本机真实 C HTTP 进程、认证、阶段状态、忙碌、幂等/冲突、输入/Unicode/HTTP 限制、Unix voice event、取消、多实例 |
| `test_jobs.py` | 失去接收回应仅查询一次、队列优先级/取消、同活跃文字递归防护、多设备隔离 |
| `test_text_pipeline.py` | 实际 shell + 真实 curl/本机 HTTP；服务端核对 Agent/Conversation ID、语音↔文字同缓存、滑动/过期、覆盖/无上下文、401 原续期与有界重试、失败不续期/不重复、silent reply、临时音量取消恢复 |
| `test_native_led.py` | 实际原生调用顺序；默认/旧配置、正常/失败/无回复、真实 C 取消/超时、灯锁/语音抢占/孤儿恢复、原 AirPlay Guard 避让与重放、无原生命令安全降级、旧 API/helper 删除、HA 灯环精确迁移 |
| `test_boot.py` | 原 stop 先返回、进程随后退出；只 stop Squeezelite，done/失败重试 |
| `test_busybox.py` | 实际 BusyBox 明确拒绝 `-w`；所有调用的参数契约、FD 生命周期、超时/释放/重试、并行临界区互斥 |
| `test_install.py` | 真实安装器在合成文件系统上模拟启动失败、boot/Cron恢复、身份保持和重试、旧无标记备份/目录切换、缺配置/二进制的回滚/幂等卸载、升级失败、Cron/listener reload失败续跑、boot锁竞态、原配置值保留；包/已安装/retry 适配器格式和哈希校验、重复 LED/错误 hook 顺序拒绝、相同包不能绕过、活跃 LED 保护兼容检查 |
| `test_adapters.py` | source 顺序/锚点、保留 KWS、未知版本拒绝、LED 避让、所有脚本 `sh -n` |
| `test_release.py` | 三端版本/tag、ARM EABI5 hard-float/静态 ELF、包内外 SHA256、已有 Release/草稿拒绝覆盖；上传/下载失败或损坏附件不公开，网络失败不写入 |
| `json_test.c` | ASan/UBSan，严格 JSON、嵌入 NUL、坏 UTF-8/代理对/重复 key |

测试替身都明确标记 synthetic，未包含任何本机原配置或用户语音。最终每次实际执行的数量和时间以 [CI](https://github.com/qiqi20020612/xiaoai-speaker-bridge/actions)及 JUnit 产物为准。

文本测试不再使用 Python Agent 替身自行补会话字段；合成 HTTP 服务只按实际收到的请求处理，不能掩盖缺 Agent/ID 的请求。令牌续期函数只从测试 launcher 的定义中提取，整个 launcher 不执行。详细测试边界见 [Conversation 链路](conversation.md)。

ARM CI 检验 ELF 头、无动态 INTERP、QEMU `--version`。这验证目标 ABI/程序入口，不验证扬声器、DBus 回控、发送端滑块、LED 或峰值资源。

## 未执行的真机验收

0.1.1 的 BusyBox 修复依据用户提供的设备环境和报错，本轮没有连接或修改任何音箱。用户报告 ARM CLI、校验和及旧版 dry run 通过、实际 apply 失败；CI 的真实 BusyBox 检查仍不是 LX06 硬件验收。

因为本次设备访问明确禁止写入，以下都未执行：安装/激活、任何音量/LED/IR 写入、TTS、远程 Conversation（可能执行设备动作）、媒体播放控制、关闭/重启 listener、Cron 写入、新 boot 的重启实测、新 bridge 的长时间/并发/资源测试。

只读调查核对了原调用链、原 boot/Cron、ABI、原服务、现有接口与状态。私有原始材料及可行性报告与公开源码分离；公开库不给出用户设备清单、地址或测量样本。

完成设备部署后按 [覆盖矩阵](coverage.md) A1–E10 回归、按 [资源报告](resources.md) 采样，保留每项证据。发现硬件限制时先报告数据，不把 verification flag 当测试通过。
