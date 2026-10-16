0.1.9 是以已发布 v0.1.8 为基线的开发版本。PR 保持草稿，尚有其他待补充修复项；未合并、创建新 Tag/Release 或部署到真实 LX06 / Home Assistant。

- 修复 HA 把 Bridge accepted 误当成本地排队的问题：请求派发后取消必须到达音箱，不能只修改 HA 状态；明确 busy 可本地撤销，结果不确定时按 request_id 查询，不重复执行。
- 主动播报可取消；文字指令仅在意图/Conversation/上下文处理完成后的 synthesizing/playing 阶段允许取消回复播报。执行中的 Conversation 仍拒绝取消，已经执行的动作和上下文不会撤销或重复。
- 取消继续向本次 worker 进程组发送 TERM，保留原清理宽限；结束后先清理该组残留提供方/播放器，再回收父 PID。重复取消不延长期限，子进程不继承 daemon 的停止处理器。
- 保留音量 Journal、通道版本、中途调音、语音抢占及原生 LED 清理。必要的延迟恢复仍运行既有有界脚本，通过独立会话避免随 TTS 进程组结束；不新增常驻进程、定时任务或长期缓存。
- 增加真实本地 HTTP/C/Shell 进程测试、脱敏豆包/微软/Google 合成与播放取消、请求不确定性、指令执行保护、资源恢复及 ARM/QEMU 回归。

具体根因、验证和真机边界见 [v0.1.9 修复记录](https://github.com/qiqi20020612/xiaoai-speaker-bridge/blob/codex/v019-fixes/docs/lx06-v019.md)。自动化使用静默播放器和模拟提供方，不代表真实 LX06 已验收；本轮不修改或上传用户私有文件。

后续获单独部署授权后再使用同一构建的完整包，先执行 SHA256SUMS 外层及音箱包内校验，沿用原地升级，不卸载重配。失败保留恢复目录：failed_restored 表示自动恢复；recovery_required 使用同一新包 install.sh --rollback 继续恢复。保留 UUID/Token、启用状态、私有适配器、历史备份和最新 OAuth，不整份回写旧认证；本轮未实机安装或回滚。
