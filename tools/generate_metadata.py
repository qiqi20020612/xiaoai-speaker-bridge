#!/usr/bin/env python3
"""Generate localizable service and entity metadata from a small explicit catalog."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "custom_components/xiaoai_speaker"
EN = {
    "number": {
        "volume_master": "Master volume",
        "volume_tts": "TTS volume",
        "volume_bluetooth": "Bluetooth volume",
    },
    "sensor": {
        "memory_available": "Available memory",
        "cpu_load": "CPU load",
        "bridge_rss": "Bridge memory",
        "airplay_volume": "AirPlay volume",
    },
    "switch": {
        "listener": "Listening",
    },
    "text": {
        "announce_text": "Play text",
        "send_text_text": "Execute text directive",
    },
    "button": {
        "refresh": "Refresh",
        "reset_context": "Reset conversation",
        "restart_bridge": "Restart bridge",
    },
}
ZH = {
    "number": {
        "volume_master": "主音量",
        "volume_tts": "播报音量",
        "volume_bluetooth": "蓝牙音量",
    },
    "sensor": {
        "memory_available": "可用内存",
        "cpu_load": "CPU 负载",
        "bridge_rss": "桥接内存",
        "airplay_volume": "AirPlay 音量",
    },
    "switch": {
        "listener": "语音监听",
    },
    "text": {
        "announce_text": "播放文字",
        "send_text_text": "执行文字指令",
    },
    "button": {
        "refresh": "刷新状态",
        "reset_context": "重置会话",
        "restart_bridge": "重启桥接",
    },
}
SERVICE_NAMES = {
    "announce": ("Announce text", "主动播报"),
    "send_text": ("Send conversation text", "远程文字对话"),
    "set_volume": ("Set channel volume", "设置音量"),
    "volume_up": ("Increase volume", "增大音量"),
    "volume_down": ("Decrease volume", "减小音量"),
    "cancel_job": ("Cancel a job", "取消任务"),
    "get_job": ("Read job status", "查询任务"),
    "get_queue": ("Read MPD queue", "读取 MPD 队列"),
    "send_ir": ("Send infrared code", "发送红外码"),
}
FIELD_NAMES = {
    "device_id": ("Speaker device", "音箱设备"),
    "entity_id": ("Speaker entity", "音箱实体"),
    "text": ("Text", "文本"),
    "volume": ("Temporary announcement volume", "本次播报音量"),
    "priority": ("Priority", "优先级"),
    "request_id": ("Unique request ID", "唯一请求标识"),
    "wait": ("Wait for completion", "等待完成"),
    "speak_response": ("Speak response", "播报回复"),
    "use_context": ("Use shared context", "使用共享上下文"),
    "target": ("Volume target", "音量对象"),
    "level": ("Volume percent", "音量百分比"),
    "step": ("Adjustment step", "调整步长"),
    "code": ("Comma-separated infrared pulses", "逗号分隔的红外脉冲"),
    "carrier": ("Carrier frequency", "载波频率"),
}
fields = {
    "device_id": {"selector": {"device": {"integration": "xiaoai_speaker"}}},
    "entity_id": {"selector": {"entity": {"integration": "xiaoai_speaker"}}},
    "text": {"required": True, "selector": {"text": {"multiline": True}}},
    "volume": {"selector": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%"}}},
    "priority": {"default": "normal", "selector": {"select": {"options": ["normal", "urgent"]}}},
    "request_id": {"selector": {"text": {}}},
    "wait": {"default": False, "selector": {"boolean": {}}},
    "speak_response": {"default": True, "selector": {"boolean": {}}},
    "use_context": {"default": True, "selector": {"boolean": {}}},
    "target": {
        "default": "auto",
        "selector": {
            "select": {
                "options": ["auto", "master", "tts", "bluetooth", "headphone", "airplay", "all"]
            }
        },
    },
    "level": {
        "required": True,
        "selector": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%"}},
    },
    "step": {"default": 10, "selector": {"number": {"min": 1, "max": 100}}},
    "code": {"required": True, "selector": {"text": {"multiline": True}}},
    "carrier": {"default": 38000, "selector": {"number": {"min": 20000, "max": 60000}}},
}
service_fields = {
    "announce": ["text", "volume", "priority", "request_id", "wait"],
    "send_text": [
        "text",
        "speak_response",
        "use_context",
        "priority",
        "request_id",
        "wait",
    ],
    "set_volume": ["target", "level"],
    "volume_up": ["target", "step"],
    "volume_down": ["target", "step"],
    "cancel_job": ["request_id"],
    "get_job": ["request_id"],
    "get_queue": [],
    "send_ir": ["code", "carrier"],
}
yaml_services = {
    name: {"fields": {f: fields[f] for f in ["device_id", "entity_id", *fs]}}
    for name, fs in service_fields.items()
}
yaml_services["cancel_job"]["fields"]["request_id"] = {**fields["request_id"], "required": True}
yaml_services["get_job"]["fields"]["request_id"] = {**fields["request_id"], "required": True}
(ROOT / "services.yaml").write_text(yaml.safe_dump(yaml_services, sort_keys=False))
for language, catalog, index in [("en", EN, 0), ("zh-Hans", ZH, 1)]:
    zh = index == 1
    config_fields = {
        "host": "音箱 IP 或主机名" if zh else "Speaker IP or hostname",
        "port": "桥接端口" if zh else "Bridge port",
        "token": "桥接认证令牌" if zh else "Bridge authentication token",
        "mpd_port": "MPD 端口" if zh else "MPD port",
        "mpd_password": "MPD 密码（可选）" if zh else "MPD password (optional)",
    }
    data = {
        "title": "XiaoAI Speaker Bridge",
        "config": {
            "step": {
                step: {
                    "title": "连接小爱音箱" if zh else "Connect XiaoAI speaker",
                    "data": config_fields
                    if step != "reauth_confirm"
                    else {"token": config_fields["token"]},
                }
                for step in ["user", "reconfigure", "reauth_confirm"]
            },
            "error": {
                "invalid_auth": "认证失败" if zh else "Authentication failed",
                "cannot_connect": "无法连接音箱" if zh else "Cannot connect to speaker",
                "invalid_input": "配置格式无效" if zh else "Invalid configuration",
                "incompatible_protocol": "桥接协议不兼容" if zh else "Incompatible bridge protocol",
            },
            "abort": {
                "already_configured": "设备已配置" if zh else "Device already configured",
                "wrong_device": "设备身份不匹配" if zh else "Device identity does not match",
                "reauth_successful": "认证已更新" if zh else "Authentication updated",
                "reconfigure_successful": "配置已更新" if zh else "Configuration updated",
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": "音量和状态选项" if zh else "Volume and state options",
                    "data": {
                        "poll_interval": "健康检查间隔（秒）"
                        if zh
                        else "Health poll interval (seconds)",
                        **{
                            f"source_priority_{i}": f"音源优先级 {i}"
                            if zh
                            else f"Source priority {i}"
                            for i in range(1, 4)
                        },
                    },
                }
            },
            "error": {
                "invalid_source_priority": "三个音源必须各选择一次"
                if zh
                else "Choose each of the three sources exactly once"
            },
        },
        "entity": {
            platform: {key: {"name": name} for key, name in values.items()}
            for platform, values in catalog.items()
        },
        "services": {
            name: {
                "name": SERVICE_NAMES[name][index],
                "description": SERVICE_NAMES[name][index],
                "fields": {
                    f: {"name": FIELD_NAMES[f][index], "description": FIELD_NAMES[f][index]}
                    for f in ["device_id", "entity_id", *fs]
                },
            }
            for name, fs in service_fields.items()
        },
    }
    for path in [ROOT / "translations" / f"{language}.json"] + (
        [ROOT / "strings.json"] if language == "en" else []
    ):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print("Generated service metadata and English/Simplified Chinese translations")
