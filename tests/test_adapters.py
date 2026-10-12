import subprocess
from pathlib import Path

import pytest

from speaker.tools.prepare_adapters import prepare_airplay_led, prepare_listener

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = """#!/bin/sh
CONFIG_FILE=/data/listener
[ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"
set_volume(){ :; }
# Set lower rate if cannot convert later
. /data/kws_guard.sh
while true; do
kws_guard
SAVED_VOL=`current_volume`
set_volume 10
/bin/show_led 1
STT_TEXT="test"
    if [ "${API_AVAILABLE}" = "1" ]; then
      intent_run test
    fi
    original_conversation
    fi # INTENT_SUCCESS
set_volume $SAVED_VOL
done
"""


def test_adapter_is_sibling_and_fail_closed_on_unknown_source():
    transformed = prepare_listener(SYNTHETIC)
    assert "kws_guard\nSAVED_VOL" in SYNTHETIC
    assert transformed.index("bridge_voice_begin") < transformed.index("SAVED_VOL=")
    assert transformed.index("listener-hooks.sh") > transformed.index("set_volume(){")
    assert "bridge_process_text" in transformed and "bridge_restore_volume" in transformed
    assert ". /data/kws_guard.sh" in transformed
    with pytest.raises(ValueError):
        prepare_listener(transformed)
    with pytest.raises(ValueError):
        prepare_listener(SYNTHETIC.replace("SAVED_VOL=", "UNKNOWN="))


def test_all_shipped_scripts_are_valid_posix_shell():
    for file in (ROOT / "speaker").rglob("*.sh"):
        subprocess.run(["sh", "-n", str(file)], check=True)


def test_led_worker_yields_to_voice_without_touching_original():
    source = """#!/bin/sh
draw() {
    :
}
    while [ -f "$STATE" ]; do
        draw
    done
/data/airplay_volume_led --worker
"""
    prepared = prepare_airplay_led(source)
    assert "bridge_lock_for 0 6" in prepared
    assert 'bridge_original_draw "$@"' in prepared
    assert "if bridge_led_busy;" in prepared
    assert "/data/xiaoai_bridge/adapter/airplay-led.sh --worker" in prepared
    assert "voice_busy" not in source
