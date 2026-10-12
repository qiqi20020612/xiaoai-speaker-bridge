import subprocess
from pathlib import Path

import pytest

from speaker.tools.prepare_adapters import prepare_airplay_led, prepare_listener

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = (ROOT / "tests/fixtures/lx06_launcher.sh").read_text()


def test_adapter_is_sibling_and_fail_closed_on_unknown_source():
    transformed = prepare_listener(SYNTHETIC)
    assert "kws_guard || /usr/bin/porcupine" in SYNTHETIC
    assert transformed.index("\nbridge_voice_begin ||") < transformed.index("\nSAVED_VOL=")
    assert transformed.index("listener-hooks.sh") > transformed.index("set_volume(){")
    assert "bridge_process_text" in transformed and "bridge_restore_volume" in transformed
    assert ". /data/xiaoai_bridge/adapter/kws-guard.sh" in transformed
    with pytest.raises(ValueError):
        prepare_listener(transformed)
    with pytest.raises(ValueError):
        prepare_listener(SYNTHETIC.replace("SAVED_VOL=", "UNKNOWN="))


def test_listener_adapter_rejects_other_or_background_led_modes():
    for command in (
        "/bin/show_led 10",
        "/bin/show_led 0",
        "/bin/show_led 1 &",
        "/bin/show_led 1\n/bin/show_led 1",
    ):
        with pytest.raises(ValueError, match="verified standalone wake/loading/end LED"):
            prepare_listener(SYNTHETIC.replace("/bin/show_led 1", command))


def test_listener_adapter_rejects_additional_led_commands():
    with pytest.raises(ValueError, match="verified standalone wake/loading/end LED"):
        prepare_listener(SYNTHETIC.replace("/bin/show_led 1", "/bin/show_led 1\n/bin/show_led 2"))


def test_all_shipped_scripts_are_valid_posix_shell():
    for file in (ROOT / "speaker").rglob("*.sh"):
        subprocess.run(["sh", "-n", str(file)], check=True)


def test_led_worker_yields_to_voice_without_touching_original():
    source = (ROOT / "tests/fixtures/lx06_airplay_led.sh").read_text()
    prepared = prepare_airplay_led(source)
    assert "bridge_lock_for 0 6" in prepared
    assert 'bridge_original_draw "$@"' in prepared
    assert "if bridge_led_busy;" in prepared
    assert "/data/xiaoai_bridge/adapter/airplay-led.sh --worker" in prepared
    assert "voice_busy" not in source
