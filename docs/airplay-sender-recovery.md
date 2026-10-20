# v0.2.0 AirPlay Duck 后置确认修复

本项基于已发布的 `v0.2.0`（`0791c6f`），仅修复 `sender-recovery.sh` 的恢复确认，不改 ALSA 目标模型、协议、Journal 格式、Shairport 配置或正常语音/播放入口。本文描述待审阅修复，不表示已经发布或通过真机验收。

## 用户观测与离线复现

LX06 / Shairport Sync 4.3.5 / standard profile 上，Duck Journal 的 `sender_recovery=true` 持续 PENDING：

| 观测 | 值 |
| --- | --- |
| 主接口 `Volume` | `-18` |
| `RemoteControl.AirplayVolume` | `-19.875` |
| ALSA `mysoftvol` | `177,177` |
| `bridge_sender_target` | `177,177`，退出码 0 |
| helper 严格读回 | 退出码 0 |

v0.2.0 后置校验仍要求 Remote 等于主值的两位小数表示，因此上述数据即使写入、读回和主属性检查全部成功，也会返回失败，保留 Journal。回归用相同数据及真实恢复代码，在修改前复现四个失败组合：Duck / 已交接的 voice Journal，以及硬件仍为 cap / 已经为 `177,177`。这是基于用户观测的受控复现，没有连接或操作真实设备。

## 官方 4.3.5 的更新顺序

- [rtsp.c](https://github.com/mikebrady/shairport-sync/blob/4.3.5/rtsp.c#L3624-L3647)：启用 D-Bus 时，收到发送端的 `volume:` 请求会设置主接口 `Volume`。
- [dbus-service.c](https://github.com/mikebrady/shairport-sync/blob/4.3.5/dbus-service.c#L632-L652)：主属性回调调用 `player_volume()`；主属性与播放器应用之间仍有时序窗口。
- [player.c](https://github.com/mikebrady/shairport-sync/blob/4.3.5/player.c#L3503-L3553)：应用硬件增益、投递 `pvol`，随后才更新 `config.airplay_volume`。虽然 `pvol` 字符串将 raw 格式化为两位小数，这不是 Remote 属性实际取值的来源。
- [metadata_hub.c](https://github.com/mikebrady/shairport-sync/blob/4.3.5/metadata_hub.c#L659-L672)：处理 `pvol` 时不解析该 payload，而是读取 `config.airplay_volume`。如果该线程先于播放器的全局值更新运行，就可能取到前一个值。
- [dbus_metadata_watcher](https://github.com/mikebrady/shairport-sync/blob/4.3.5/dbus-service.c#L59-L68)：将 metadata store 的 double 直接发布为 Remote `AirplayVolume`，没有两位小数转换。

据此，Remote 可以滞后且带有完整小数，不能把它当成与主值严格相等的提交确认；持续重试也没有保证使它追平。这解释了本次数据与旧判断的冲突，但不据此声称已在真机追踪到当次线程调度。

## 最小修复与保护

只在现有 sender PENDING 恢复分支内，保存本次恢复前的 Remote 观测，写入前再检查主 `Volume` / `Active`，然后执行原来的事务和 ALSA 写入/严格读回。写后先重新读取 Remote，再读取主接口；主值必须与计算目标时的完整数值一致，不能舍入或使用容差。

Remote 的写前观测必须为有限数，且在 `[-30,0]` 内或为静音值 `-144`；后置条件改为保持恢复前的数值，或追平本次精确主值。变成其他值、查询失败、空值或无效数均拒绝结束恢复，并保留 Journal。这样允许稳定的 `-19.875` 与主 `-18` 并存，同时保留独立的变化检测。若主值在计算后、写入前已经改变，立即拒绝该旧目标写入；若变化发生于写入中或最终 Remote 查询期间，后置主值检查失败，保留恢复依据，下一次尝试使用新主值。

原有保护继续生效：

- 不写 Shairport 主属性或 `SetAirplayVolume`，不把旧值回发 iPhone。
- `Volume` 完整数值、`Active`、4.3.5 版本、Profile 与 PID/starttime 校验；原配置与 TLV 验证保持。
- 写前原子提交 `sender_recovery`，沿用 Volume Lock、音量版本及较新 HA 目标优先规则。
- 原 `cset name=mysoftvol 左,右`，两声道严格读回和目标一致性检查；任何失败保留恢复依据。
- 静音仍须 Remote 明确为 `-144` 才解除 cap，并在新写前观测中再次确认；停止分支、独立通道恢复、duck→voice 交接和有界重试保持。

这不是与 Shairport 共用的原子事务：它不持有 Bridge 的 Volume Lock，观测完成后仍可能收到下一次用户操作。本修复不承诺消除所有硬件竞态，也不把一次属性读取当成硬件应用完成的证明。已观察到的更新必须拒绝旧写入或拒绝清理，无法验证时继续保守恢复。

额外成本只在活跃 sender PENDING 分支：增加一次写前 Remote 查询和一次写前主接口查询，沿用 600 ms D-Bus 回复超时。普通恢复、未进入此分支的唤醒不增加查询；无新常驻进程、轮询、长期状态或配置。本轮没有真机耗时或资源测量。

## 验证与交付边界

`tests/test_airplay_sender_recovery.py` 使用真实 Shell 恢复代码、C helper 和严格整数 amixer 参数替身，覆盖本次数据、Remote 稳定滞后/追平/其他变化、未知观测、精确小数，以及主值在三个窗口再次变化。真实发送端变更由夹具注入主值及新 ALSA 整数值，断言写前变更不发生旧写入、最终查询期间变更不被后续旧写覆盖、失败保留标志、下一次以新目标结束恢复，并始终禁止发送端写请求。

既有双声道读回失败、配置/TLV、HA 版本、静音/停止、SIGKILL、独立通道及语音交接测试保留；另验证稳定 Remote 不能掩盖 `Active`、版本、Profile 或进程 starttime 的变化，覆盖 Remote 合法边界、越界数值和静音确认在新写前观测失效。元数据替身默认保留完整 double，移除旧的强制两位小数假设。原“主/Remote 不等必须失败”用例改为未知观测及真实更新的拒绝场景，避免保留已经证伪的属性契约。

本地与现有完整 Linux CI 的结果记录在 PR；CI 继续包含真实 jq 1.6 / BusyBox、HA 2026.10、ShellCheck、C/sanitizers、ARM hard-float / QEMU、安装恢复与包校验，不新增或修改 Review 工作流。

未部署或操作真实 LX06 / HA，未合并或发布。后续真机仍须验证本次稳定滞后数据能结束 Duck、连续 iPhone 调音、静音/停止、HA 新目标与语音抢占，以及实际听感和资源占用。回退使用原有安装事务和保留包，不删除 Journal、配置或私有文件；回退 v0.2.0 会重新出现本次确认问题。
