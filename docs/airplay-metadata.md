# AirPlay 元数据与封面

v0.1.8 将 Shairport Sync 的歌曲信息映射到原有 media_player。没有新实体、配置入口或守护进程；原音源优先级、播放控制、音量、语音和 LED 行为保持。旧音箱不提供这些字段时，原功能继续工作，只缺少 AirPlay 歌曲/封面。

## 采集与显示

Probe 原本每 15 秒读取 Shairport 主接口及 RemoteControl 的 GetAll。现在从同一份 RemoteControl 回复提取 Metadata 和 ProgressString，不增加 D-Bus 请求，不读取 FIFO、监听新事件、设置更新间隔或修改 Shairport 配置。只在 AirPlay 活跃且 Playing/Paused 时启动一次短寿命 C 解析 helper；查询/解析失败只丢弃本次媒体信息，不影响其他 Probe 状态和原控制接口。

现有 C 缓存、sequence 事件和 HA Coordinator 继续负责更新，切歌受现有 Probe/轮询周期及网络影响，没有新增高频轮询。停止、未知或查询失败不展示 Shairport 遗留的上一首歌曲。仅当前选中的 AirPlay 音源使用这些信息，MPD/蓝牙不冒用它们。

| Shairport 4.3.5 字段 | HA 属性 |
|---|---|
| xesam:title / xesam:artist / xesam:album | media_title / media_artist / media_album_name |
| mpris:trackid | media_content_id，媒体标识，不是文件路径 |
| mpris:length，微秒 | media_duration，秒 |
| ProgressString，start/current/end RTP 时间戳 | media_position / media_position_updated_at |
| mpris:artUrl，本地文件 URI 或路径 | 受限 artwork_id，再由原生 HA 图片代理提供 entity_picture |

解析器按 D-Bus 类型/结构读取整个有界回复，再用 JSON 校验；不执行字段内容，不从歌名推断属性。保留中文、引号、反斜杠和多个歌手。畸形、截断、重复键、无效 UTF-8、NUL、控制/跨行字符串或过深结构安全降级。输入小于 64 KiB，标题/专辑各最多 512 UTF-8 字节，歌手最多 8 个、每个 256 字节，媒体标识最多 256 字节；超限字段不显示。封面不是内联 Base64，不把图片字节放进状态响应。

字段定义和时间单位依据官方 [4.3.5 D-Bus 定义](https://github.com/mikebrady/shairport-sync/blob/4.3.5/org.gnome.ShairportSync.xml)、[Metadata 导出](https://github.com/mikebrady/shairport-sync/blob/4.3.5/dbus-service.c)与 [RTP progress 定义](https://github.com/mikebrady/shairport-sync/blob/4.3.5/rtsp.c)。保留现有 protocol=1 的向后兼容可选字段。

## 进度边界

4.3.5 的 RTP 输入进度以 44100 帧/秒计数；按 32 位无符号差值处理回绕。负值、溢出、current 超出范围、含糊的半周期跨度及与当前歌曲时长不符的旧计数不生成位置；无时限流也不套用旧歌曲进度。

只有新进度观测才重设 HA 时间锚点。相同 ProgressString 经重复 Probe、任务通知或音量状态更新时保留锚点，让 HA 原生播放器在播放状态下平滑推进；暂停冻结、继续后重新计时，跳转由后续发送端进度纠正。换歌但进度尚未变化时位置为未知，避免继承上一首位置；探测失败后也不把重新出现的旧计数当新观测。

这是发送端报告值加 HA 本地时间的估算，不能声明采样级同步。首次接入时若发送端长期未更新 ProgressString，显示可能滞后；暂停/切歌也受既有采集周期影响。本轮没有开启 FramePosition 定时更新或修改 Shairport 的进度设置。

## 封面安全与缓存

Bridge 只认 /tmp/shairport-sync/.cache/coverart/cover-<32 位小写十六进制>.jpg 或 .png，与上游默认缓存布局相同。file:/// URI 先约束为这个固定路径再转换为文件名 ID；HTTP/其他路径、authority、穿越、百分号路径和查询参数均不接受。自定义 Shairport 缓存位于其他目录时歌曲仍可显示，封面安全降级，不扩大文件读取范围。

HA 使用已有 Bearer 认证访问 GET /api/airplay/artwork/ID；浏览器只收到 HA 自己的 media_player_proxy URL，不接触音箱 Token、本地路径或任意远程图片地址。端点沿用 IP 白名单、限流和连接超时，只服务最新成功 Probe 当前公布的图片；旧 ID、失败 Probe 或发布后超过 30 秒的缓存返回 404。

每一级目录和最终文件都通过 openat/O_NOFOLLOW 打开，不跟随目录或文件符号链接；只接受单链接普通文件、正确 PNG/JPEG 签名且最多 2 MiB。FIFO、目录、硬链接、超大/缺失/错误类型文件拒绝。用打开的 FD 和固定 Content-Length 分段发送，复用原有 32 KiB 连接缓冲，不在 LX06 分配整张图片或复制封面。

HA 每台设备仅缓存当前一张、最多 2 MiB，成功缓存 300 秒，失败缓存 30 秒；并发请求共用一次读取。到期由已有 Coordinator 更新或下一次请求清理，没有额外定时器；切歌/换封面、音源变化、停止、状态过期和卸载立即清空。请求期间切歌或卸载的旧结果丢弃。Bridge 不管理或删除 Shairport 自己的文件，原服务继续拥有其缓存；没有新磁盘缓存或无限累积的图片目录。

## 验证与未验证项

用户授权后只读查询了 LX06：确认 Shairport 4.3.5、上述字段类型和默认封面目录；当时处于 Stopped，仍残留上一首 Metadata/ProgressString，FramePosition 为空。通过 SSH 的只读回复直接送入工作站新解析器，确认标题/歌手/专辑/时长/进度及受限封面 ID 均成功解析；不保存或提交真实歌曲、文件名、设备信息或认证材料。

自动化使用完全合成数据覆盖解析/边界/回绕、一次 GetAll 复用、停止与失败清除、原生 HA 元数据/图片代理、进度锚点与切歌、缓存并发/过期/卸载、HTTP 认证/路径限制、分段传输和慢客户端时语音事件，以及原有完整回归。ARM QEMU 验证同一解析入口。具体数量以 PR CI/JUnit 为准。

没有上传或部署新代码，没有通过 SSH 改播放、音量、LED、配置或进程。新版实际 HA UI、连续切歌/拖动进度、暂停/恢复、图片删除竞态、CPU/RSS 与语音并发仍需另获部署授权后验收。只读格式验证不等于新版设备功能或性能验收通过。
