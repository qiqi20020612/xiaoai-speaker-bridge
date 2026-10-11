# 资源采样与报告

新增 LX06 常驻 RSS 优化目标 ≤3 MiB，预算目标 ≤5 MiB，空闲额外 CPU <1%。这是待实测目标。**目前常驻服务尚未完成真实 LX06 的启动和负载验收；CLI 能执行不代表任何场景已达到资源预算。** 真机原基线及数据只保存在私有工作站报告，公开仓库无原设备采样。

## 已完成的工程约束

C 端固定 8 个连接、16 条幂等结果、3 个 worker，单播报/对话并发；无常驻 Python/Node/模型/解码器。规则、持久期望、一般队列和多设备编排都在 HA。probe 默认每 15 秒按需派生，AirPlay 文件变化每秒只 stat、无空闲毫秒级外部命令循环。worker 超时、有界输出及自己进程组清理。

构建产物为 ARM EABI5 静态可执行文件；包的空间由安装器在本机估算，不能用二进制体积推断 RSS。控制端不保存音频；现有 TTS 的缓存和回退行为保持原样。自己的日志仅 `/tmp` 且启动日志有界轮转。

`/api/state.system` 包含读取的桥接 RSS/HWM、整机可用内存与 1 分钟 load average。`worker_peak_rss_kb` 来自退出时 `wait4` 的 rusage 高水位，记录至本进程生命周期结束；它是 worker/被收割子树的**单进程峰值指标**，不是同时运行的所有进程物理内存之和，也不是整机 CPU 使用率。

## 日后采样

只有安装、激活和基本功能回归获得写入授权并成功后，才测新增桥接进程。在工作站通过 SSH 读流，把 stdout 重定向到私有文件；不要在 `/data` 写长时间采样。

音箱上的只读采样入口：

```sh
sh /data/xiaoai_bridge/tools/resource_probe.sh BRIDGE_PID 1860 2
```

参数为 PID、持续秒数、间隔秒数。该脚本只读取 `/proc`、`df`，输出 CSV。分别采集 bridge 与 listener 的树，避免漏掉由原 procd 托管、并非 bridge 子进程的正常语音处理。

字段：`elapsed_s,pid,rss_kb,hwm_kb,available_kb,process_ticks,total_ticks,idle_ticks,children_rss_kb,children_hwm_kb,tmp_kb,data_free_kb`。

- 桥接进程 CPU%：相邻 `process_ticks` 差 / `CLK_TCK` / 时间差 ×100。读取 `getconf CLK_TCK`，不要默认为所有平台相同。
- 整机繁忙比例：`1-Δidle_ticks/Δtotal_ticks`。total 不重复计 guest；idle 含 iowait，需同时保留原 `/proc/stat` 作审阅。
- `cpu_load` 是 load average，不是 CPU%。采样器自身会产生 awk/cat 开销，应另测关闭采样时的基线，比较额外 CPU。
- 进程树 RSS 累计只描述采样时的上界/构成，共享页不可简单相加当实际增量。整机 `MemAvailable` 前后差也受既有服务缓存影响。
- 间隔采样可能漏掉短命子进程；同时保存 API 的 `worker_peak_rss_kb`、父进程 VmHWM 与实际音频子进程的高水位。高压场景可缩短到 1 秒，注意观测开销。
- HA 端使用同一测试条件记录 HA 进程 RSS/CPU 前后差，并保留版本、加载集成数；不要把开发机 HA fixture 结果冒充树莓派实测。

## 待填验收表

每个场景记录实测区间、桥接 RSS 中位数/最大值、VmHWM、worker 峰值、子进程 RSS 构成、最低 MemAvailable、idle/峰值 CPU、tmp/data 变化、错误/OOM。所有未执行项保持“未执行”。

| 场景 | 最短运行/操作 | 本轮状态 |
|---|---|---|
| 空闲 | 31 分钟，轮询/长轮询稳定 | 未执行 |
| MPD | 连续播放及队列/切歌/seek | 未执行 |
| AirPlay | 手机调音量、暂停/继续、LED 与来源切换 | 未执行 |
| 蓝牙 | 真正连接并播放，检查路由/音量 | 未执行 |
| KWS/STT | 双通道唤醒、录音、成功/失败/重复唤醒 | 未执行 |
| 普通语音/TTS | 原 Agent、五分钟滑动上下文、原 TTS/回退 | 未执行 |
| 主动播报 | 普通/urgent、取消、临时音量，至少 32 条有界负载 | 未执行 |
| 文字对话 | 上下文双向衔接、silent reply、Agent 隔离 | 未执行 |
| 并发 | 语音录音中播报/文字/音量同时到达 | 未执行 |
| 断线恢复 | HA/bridge 中断，确认不重播/不重复设备动作 | 未执行 |
| 重启/升级/卸载 | Cron、退出、原语音、保留原服务 | 未执行 |

若超过预算，先按证据识别固定 buffers、probe fork、现有 TTS/模型等来源，减少静态缓冲/昂贵状态采样，把可迁移缓存和规则继续留在 HA，不能关闭核心音量、对话、鉴权或本地互斥来换取数字。仍超预算须提供测量和权衡后由使用者决定；当前没有足够数据作此判定。
