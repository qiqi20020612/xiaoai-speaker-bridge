"""Public installation and protocol constants."""

from homeassistant.const import Platform

DOMAIN = "xiaoai_speaker"
PROTOCOL_VERSION = 1
PLATFORMS = [
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TEXT,
]
DEFAULT_PORT = 8766
DEFAULT_MPD_PORT = 6600
DEFAULT_POLL = 15
MAX_TEXT = 4096
MAX_QUEUE = 32
JOB_TIMEOUT = 180
TARGETS = ("auto", "master", "tts", "bluetooth", "headphone", "airplay", "all")
SOURCE_PRIORITY = ("airplay", "bluetooth", "mpd")
TERMINAL = frozenset({"completed", "failed", "cancelled", "unknown"})
CONTROL_ACTIONS = frozenset({"play", "pause", "stop", "next", "previous", "mute", "unmute"})
