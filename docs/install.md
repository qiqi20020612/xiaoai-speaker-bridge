# 逐端安装、启用及回滚

部署只有 HA 集成和 LX06 两个单元。工作站仅用于构建、读取副本和审阅，安装完成后无需任何工作站服务。

**本次开发没有执行以下设备写入步骤。** `--apply` 是供日后获得设备写入授权的安装者使用的明确入口。当前只读要求下应停留在本地构建、审阅及只读调查。

## 1. 构建/获取通用包

在 GitHub CI 中下载 `generic-two-end-packages`，内含 HA ZIP、ARM hard-float 音箱包及校验值。包中没有配置令牌或任何具体设备的适配副本。

解压 CI 产物后，在包含两个安装包及 `SHA256SUMS` 的同一目录执行 `sha256sum --check SHA256SUMS`，全部通过后再准备部署副本。

也可以使用校验过的 Zig 0.14.1 交叉编译器：

```sh
make -C speaker CC='zig cc -target arm-linux-musleabihf -static' LDFLAGS='-s' OUT=../build/armhf/xiaoai_bridge
python3 tools/package_speaker.py --binary build/armhf/xiaoai_bridge --output dist/xiaoai-bridge-armhf.tar.gz
```

这产生 32 位 ARM EABI5、hard-float、静态 ELF；不依赖设备动态 libc。**内核的架构名称不能单独决定用户空间 ABI**，每次支持新硬件须读取系统 ELF 头确认。QEMU 的 `--version` 只能验证程序入口，不能证明 ALSA、DBus 或 KWS 兼容。

## 2. 准备私有适配副本

只读获取音箱实际 `/data/porcupine_launcher_kws` 和 `/data/airplay_volume_led`，保存在工作站 `.private/`。不将 `/data/listener`、HA/OAuth 配置或整个 `/data` 放进安装包/公开仓库。

```sh
python3 speaker/tools/prepare_adapters.py \
  --listener .private/original-launcher \
  --airplay-led .private/original-airplay-led \
  --output .private/prepared
```

工具按已审核的 source 顺序及锚点生成 `listener.sh`、`airplay-led.sh`、每个文件的 `.diff` 和 `review.json`。原文件不变，未知版本锚点不匹配时失败，不自动猜测。生成的副本保留原双通道 KWS、录音/STT、错误提示等部分，只替换 STT 后文本入口与音量恢复，并在函数定义之后加载 hooks。

审阅重点：

1. `bridge_voice_begin` 位于 `SAVED_VOL` 与降音量之前；恢复调用改为按通道版本安全恢复。
2. 原 KWS guard、麦克风通道、STT、语言和音频设备设置均保留。
3. 原 `/data/listener` 在执行时加载，因此 Agent、OAuth token 刷新与 TTS 参数没有被复制到新版配置。
4. LED `draw()` 和后台 worker 在语音活动时退让；自派生 worker 仍位于桥接同一端。

审核通过后，将私有生成目录放入**本机私有部署副本** `xiaoai_bridge/adapter/`。不要上传这份混合了本机原脚本的副本。通用包的 `SHA256SUMS` 核验通用文件；`review.json` 单独核验私有生成文件。

## 3. 日后在 LX06 上预览及安装

先核对 `jq`、`flock`、`curl`、`amixer`、`sha256sum`、`timeout`、DBus 工具以及现有脚本。检查 `/data` 空間，安装器计算新包、原包备份和余量，空间不足时拒绝安装。

将审阅的部署副本解压在 `/tmp`，保留 `/data` 的存储空间，然后在音箱本地执行：

```sh
sh /tmp/xiaoai_bridge/install.sh --dry-run
# 仅在具备设备写入授权、完成审阅后：
sh /tmp/xiaoai_bridge/install.sh --apply
```

安装器：

- 验证通用包 SHA256，备份原 boot、Cron 和已有 bridge；已有备份/暂存目录存在时拒绝覆盖。
- 原子切换 `/data/xiaoai_bridge/`，新安装生成一次随机设备 UUID 与 64 位十六进制桥接令牌；升级保留原配置和私有适配。
- 保留所有原 Cron 行，只去重并补充标准 boot 行；更新已有 `/data/xiaoai_boot.sh`，不另建启动服务。
- 不改只读固件；不关闭 Snapclient/upmpdcli；不清除已完成的 boot 标志，也不因为升级重复停用临时手动开启的 Squeezelite。
- 启动固定 `start.sh` 并检查 `/api/health` 就绪，失败时返回非零并保留回滚资料。

新包首次启动默认不启用语音副本。原 listener 继续运行；在 voice hooks 未就绪时，播报/远程对话和可能被旧恢复覆盖的主/蓝牙音量写入会明确受限。随后必须完成共享入口启用，不能把这种保护状态当成功能验收。

## 4. 日后单独启用共享语音入口

```sh
sh /data/xiaoai_bridge/tools/activate.sh --dry-run
# 审阅副本 diff 和下述单行变更，再授权启用：
sh /data/xiaoai_bridge/tools/activate.sh --apply
```

此操作**不会用模板覆盖原配置**。它备份 `/data/listener`，仅在原 `. /data/kws_bootstrap.sh` 之前增加：

```sh
. /data/xiaoai_bridge/lib/bootstrap.sh
```

现有设置逐字保留。该引导仅在原入口名为 `porcupine_launcher`、私有副本存在且本地 enable 标志为 true 时重定向到兄弟副本；普通 reload、OAuth 刷新和重启仍使用同一个原启动链。原 `/data/porcupine_launcher_kws`、系统 `/usr/bin/porcupine_launcher`、KWS/STT/Agent/TTS 脚本均不替换。

启用前检查 source/output SHA256、voice 空闲和配置是否并发改变。禁止在 OAuth 正进行写入时启用；检测到变化会退出，重新读取审阅后重试。listener 通过原 procd 实例运行，停止后等待最多 15 秒异步退出，再确认新实例健康。

移除引导时用 `--remove`，**不会恢复可能含过期 OAuth token 的旧配置整份备份**；仅删除自己的精确 include 行，保留当前刷新后的所有值。完整恢复流程使用安装器回滚/卸载。

## 5. 日后启用 AirPlay LED 冲突保护

```sh
sh /data/xiaoai_bridge/tools/activate_airplay_led.sh --dry-run
# 在获得写入授权、审核 LED 差异后：
sh /data/xiaoai_bridge/tools/activate_airplay_led.sh --apply
```

该独立操作用原 LED 程序的审阅副本加入语音退让逻辑，保留原亮度/颜色/音量映射与防抖。它核验输入、输出哈希并备份原程序，**不触碰 Shairport 配置、hook 路径、软件音量方案或 AirPlay 音频服务**。变更仅作用于 LED 程序，不能当作 AirPlay 反向控制已验证。

`--remove` 只在当前程序与已审阅版本匹配时恢复原 LED 程序。检测到其他更新则停止，要求审阅合并，防止覆盖新的本机改动。安装器卸载/回滚会先移除 LED 保护再移除桥接目录。

## 6. 添加 HA 端

HACS 自定义仓库填写本仓库 URL，类型选 Integration。安装并重启 HA；也可手工仅复制 `custom_components/xiaoai_speaker` 到 HA 的 `config/custom_components`。

在 HA 添加集成，读取**本机** `config.json` 的随机桥接 token 配对。不要填写 SSH 密码或复制 HA 长期 token。选项配置轮询间隔、来源优先级和空闲音量对象。Agent select 默认禁用，可在高级实体中启用；`device_default` 使用原配置，其他 Agent 的上下文与默认 Agent 隔离。

经过实际手机音量同步/播放控制、蓝牙、LED、IR 测试后，才分别修改本地 `verified` 标志并重新加载集成，让对应实体出现。`verified=true` 是安装者记录的验证结果，不是检测到方法名后的自动推断。

## 7. 启停、开机及回滚

```sh
/data/xiaoai_bridge/start.sh status
/data/xiaoai_bridge/start.sh start
/data/xiaoai_bridge/start.sh stop
/data/xiaoai_bridge/start.sh restart
```

重复 start 不创建多实例；活进程不健康时返回错误，不强行创建第二份。停止只给自己的进程发送 TERM 并等待退出；不 kill 共享播放器、KWS 或音频服务。

开机 Cron 保留原标准行。boot 脚本至少等待 uptime 90 秒，用内核 flock 防并发，先调用原 Squeezelite stop 再等最多 15 秒；成功后启动 bridge、健康确认并写 `/tmp/xiaoai_boot.done`，失败无 done、下次 Cron 重试。桥接缺失可安全跳过。日志位于 `/tmp`，64 KiB 轮转、最多当前及上一份。

```sh
sh /tmp/xiaoai_bridge/install.sh --rollback
sh /tmp/xiaoai_bridge/install.sh --uninstall
```

回滚先移除已启用的适配、恢复原 listener，再恢复旧 bridge 和 boot。首次安装无旧 bridge 时移除 active 路径。当前 Cron 保留，包括安装之后新增的任务；boot 行在桥接缺失时仍安全。失败/卸载目录和备份保留，需用户审阅后归档，后续升级不覆写这些目录。

卸载 HA 集成只停止 HA 端任务和订阅。音箱原语音依然工作。如需同时卸载 LX06 bridge，先用上述安装器恢复 adapter，再删除 HA 集成。**不要先手工删除 bridge 文件，否则仍被原启动链/LED 引用的 helper 会缺失。**
