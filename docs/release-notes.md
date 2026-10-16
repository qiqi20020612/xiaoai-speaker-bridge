0.1.9 是以已发布 v0.1.8 为基线的开发版本。PR 保持草稿，尚有其他待补充修复项；未合并、创建新 Tag/Release 或部署到真实 LX06 / Home Assistant。

- 修复 HA 把 Bridge accepted 误当成本地排队的问题：请求派发后取消必须到达音箱，不能只修改 HA 状态；明确 busy 可本地撤销，结果不确定时按 request_id 查询，不重复执行。
- 主动播报可取消；文字指令仅在意图/Conversation/上下文处理完成后的 synthesizing/playing 阶段允许取消回复播报。执行中的 Conversation 仍拒绝取消，已经执行的动作和上下文不会撤销或重复。
- 取消继续向本次 worker 进程组发送 TERM，保留原清理宽限；结束后先清理该组残留提供方/播放器，再回收父 PID。重复取消不延长期限，子进程不继承 daemon 的停止处理器。
- 保留音量 Journal、通道版本、中途调音、语音抢占及原生 LED 清理。必要的延迟恢复仍运行既有有界脚本，通过独立会话避免随 TTS 进程组结束；不新增常驻进程、定时任务或长期缓存。
- 增加真实本地 HTTP/C/Shell 进程测试、脱敏豆包/微软/Google 合成与播放取消、请求不确定性、指令执行保护、资源恢复及 ARM/QEMU 回归。
- 两个原生 TextEntity 成功入队后清空输入，并通过 HA 原生 logbook.log 将完整原文关联到对应实体；提交失败不写 Logbook，保留未入队的有效输入。原实体身份、任务状态/错误、并发保护和 255 字符限制保留；历史遵循用户 Recorder/Logbook 配置，无新增实体、按钮或历史数据库。本项不修改音箱代码或协议。
- HA 主动播报和有声文字回复的 AirPlay Duck 从 TTS 调用前移至实际解码器入口：合成与回退合成期间音乐正常、原处理灯效持续，播放前同步完成锁/Journal/衰减，播放结束恢复。仅本次 HA 任务使用临时 PATH 入口，原生语音、LED 和私有 TTS/miplay 文件不修改；保留音源判断、中途调音、抢占与失败恢复。真实 LX06 播放器 PATH 调用及首字听感待验证，自定义绝对解码器调用不在覆盖范围内，无新增常驻进程。
- 修复 iPhone 在 Duck 期间调音后 master 卡在 10%、Journal 持续 PENDING 的已知共享 mixer 场景。仅对异常分支验证 Shairport 4.3.5 有效配置与 ALSA dB TLV，按当前 standard/flat 曲线恢复，并确认发送端状态；不写回旧 raw 或旧 master。原 Journal 增加可交接的恢复标志，支持写后中断/连续调音重试，HA 新版本仍优先。未知目标继续保守处理，无新增实体、配置、常驻进程或轮询。已只读核对目标音箱的配置，尚未部署或实测新的硬件写入。
- HA 语音监听开关的 start/stop 目标复用原生物理静音键 `/etc/init.d/listener toggle`：先查实际状态，仅不符时调用一次，再验证目标及已启用的 KWS Adapter 就绪。提示音和静音灯完全由原生服务处理，重复目标不重复提示；保留实体、协议、忙碌保护及现有 Probe 同步。内部开机、升级、重启仍走原静默启停，不新增灯效或轮询。

具体根因、验证和真机边界见 [v0.1.9 修复记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/codex/v019-fixes/docs/lx06-v019.md)。自动化使用静默播放器和模拟提供方，不代表真实 LX06 已验收；本轮不修改或上传用户私有文件。

后续获单独部署授权后再使用同一构建的完整包，先执行 SHA256SUMS 外层及音箱包内校验，沿用原地升级，不卸载重配。失败保留恢复目录：failed_restored 表示自动恢复；recovery_required 使用同一新包 install.sh --rollback 继续恢复。保留 UUID/Token、启用状态、私有适配器、历史备份和最新 OAuth，不整份回写旧认证；本轮未实机安装或回滚。
