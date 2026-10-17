# GitHub Release 发布

固定下载入口：[Releases](https://github.com/qiqi20020612/xiaoai-speaker-bridge/releases)。HACS 仓库更新方式不变，HA ZIP 用于手工安装。

当前正式版本为 **0.1.9**，修复范围、离线证据与待真机验收项目见 [修复记录](lx06-v019.md)。`pyproject.toml`、HA `manifest.json` 和 C `VERSION` 同步更新。正式版本使用 `vX.Y.Z`，预发布使用 `vX.Y.Z-alpha.N`、`-beta.N` 或 `-rc.N`，三个版本字段均须与 tag 的无 v 部分一致。v0.1.8 的离线证据、真机缺口和回退见 [版本说明](lx06-v018.md)。

推送已有 `v*` tag 后，CI 在 tag 的同一提交执行 Python/元数据/隐私/ShellCheck、原生 C 与 ASan/UBSan、真实 BusyBox ash 及 LX06 结构/故障测试、ARM 静态交叉编译/ELF 检查/QEMU CLI 与 HTTP/worker、HACS 和 hassfest。全部通过后才运行发布 job。普通分支或 PR 构建不会发布。

手动发布在 Actions 的 CI 页面选择 `workflow_dispatch`，填写已存在的 release_tag，并明确勾选 publish。所有构建/检查均 checkout 该 tag，发布前再次检查 HEAD 对应 tag、三个版本一致、正式/预发布 channel 一致。未勾选 publish 只构建；release_tag 留空也不发布。

只有 release job 具有 `contents: write`，其余 job 保持只读。每个第三方 Action 固定完整 SHA。tag 通过环境变量传递，经过严格版本校验，不拼成 shell 指令。发布前查询已有 Release；正式、预发布、草稿均拒绝覆盖。网络/API 错误也中止。新 Release 先以草稿上传完整 Assets，再下载草稿附件，核验附件集合、内外 SHA256、ABI 和版本，全部成功后才发布；失败遗留的草稿须维护者检查后手动处理，工作流不自动删除或覆盖。建议仓库开启 GitHub Release immutability 和 tag 保护。

Assets：

- `xiaoai-speaker-lx06-arm-eabi5-hf-X.Y.Z.tar.gz`：经过架构/ABI/静态检查的 LX06 安装包，内含逐文件 SHA256。
- `xiaoai-speaker-ha-X.Y.Z.zip`：保留 `custom_components/xiaoai_speaker/` 布局，无缓存、私有运行配置。
- `RELEASE_NOTES.md`：改动、范围限制、用户手动升级/回滚、隐私与真机复验要求。
- `SHA256SUMS`：上述三个 Assets 的外层 SHA256。

包通过公开文件白名单产生，不包含私有 adapter、token、对话或工作站路径。发布前重新核验外层校验、音箱逐文件校验、ELF 和 HA manifest。用户下载后仍应在同一目录执行 `sha256sum --check SHA256SUMS`。

发布流程参考：[GitHub CLI Release create](https://cli.github.com/manual/gh_release_create)、[下载构建产物](https://github.com/actions/download-artifact)。CLI/QEMU 通过不能代替用户真机安装、硬件或资源复验。
