"""A sender slider update must not leave the shared mixer at the Duck cap."""

import fcntl
import json
import os
import signal
import subprocess
import time

import pytest

from tests.test_lx06_v016 import json_binary as json_binary
from tests.test_lx06_v016 import op
from tests.test_text_pipeline import enable_airplay_duck_fixture, script
from tests.test_text_pipeline import ha_http as ha_http
from tests.test_text_pipeline import text_device as text_device

CONFIG = """general = {
    volume_control_profile = "standard";
    volume_range_db = 30;
    volume_max_db = -4.0;
};
alsa = { mixer_control_name = "mysoftvol"; };
"""
CONTROL = """numid=1,iface=MIXER,name='mysoftvol'
  ; type=INTEGER,access=rw---RW-,values=2,min=0,max=255,step=0
  : values=26,26
  | dBscale-min=-51.00dB,step=0.20dB,mute=0
"""


def gain(
    binary, path, raw, profile="standard", baseline="60", control=CONTROL, start="1", verify=False
):
    return subprocess.run(
        [
            str(binary),
            "--airplay-volume-target",
            str(path),
            raw,
            profile,
            baseline,
            start,
            *(["--verify"] if verify else []),
        ],
        input=control,
        capture_output=True,
        text=True,
        timeout=5,
    )


def config_file(path, source=CONFIG):
    path.write_text(source)
    os.utime(path, (1, 1))
    return path


@pytest.mark.parametrize(
    "raw,target",
    [
        ("-30", "85,85"),
        ("-29", "97,97"),
        ("-25", "143,143"),
        ("-20", "168,168"),
        ("-18", "177,177"),
        ("-17", "181,181"),
        ("-15", "190,190"),
        ("-10", "210,210"),
        ("-6", "220,220"),
        ("-5", "223,223"),
        ("0", "235,235"),
        ("-144", "60%"),
    ],
)
def test_standard_gain_uses_shairport_centi_db_taper_not_linear_percent(
    json_binary, tmp_path, raw, target
):
    result = gain(json_binary, config_file(tmp_path / "sps.conf"), raw)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout.strip() == target


@pytest.mark.parametrize("raw,target", [("-30", "85,85"), ("-18", "145,145"), ("0", "235,235")])
def test_flat_profile_is_taken_from_current_dbus_not_cached_config(
    json_binary, tmp_path, raw, target
):
    result = gain(json_binary, config_file(tmp_path / "sps.conf"), raw, "flat")
    assert result.returncode == 0 and result.stdout.strip() == target


@pytest.mark.parametrize(
    "control,target",
    [
        (CONTROL, 177),  # -15.70 dB is halfway: round up to -15.60 dB.
        (CONTROL.replace("-51.00dB", "-50.99dB"), 176),
        (CONTROL.replace("-51.00dB", "-51.01dB"), 177),
        (CONTROL.replace("-51.00dB", "-51.08dB").replace("0.20dB", "0.21dB"), 168),
        (CONTROL.replace("-51.00dB", "-51.09dB").replace("0.20dB", "0.21dB"), 169),
        (CONTROL.replace("min=0,max=255", "min=10,max=265"), 187),
        (CONTROL.replace("max=255", "max=510").replace("0.20dB", "0.10dB"), 353),
        (CONTROL.replace("step=0\n", "step=1\n"), 177),
        (CONTROL.replace("rw---RW-", "rw---R--"), 177),
        (CONTROL.replace("name='mysoftvol'", "name='mysoftvol',index=0"), 177),
    ],
)
def test_integer_target_uses_exact_tlv_range_and_alsa_nearest_rounding(
    json_binary, tmp_path, control, target
):
    result = gain(json_binary, config_file(tmp_path / "sps.conf"), "-18", control=control)
    assert result.returncode == 0 and result.stdout == f"{target},{target}\n"


@pytest.mark.parametrize(
    "control",
    [
        CONTROL.replace("max=255", "max=0"),
        CONTROL.replace("min=0", "min=-1"),
        CONTROL.replace("max=255", "max=65536"),
        CONTROL.replace("min=0", "min=99999999999999999999999999999"),
        CONTROL.replace("step=0\n", "step=2\n"),
        CONTROL.replace("step=0\n", "step=-1\n"),
        CONTROL.replace("values=2,min", "values=4294967298,min"),
        CONTROL.replace("26,26", "26"),
        CONTROL.replace("26,26", "26,26,26"),
        CONTROL.replace("26,26", "-1,26"),
        CONTROL.replace("26,26", "26,256"),
        CONTROL.replace("26,26", "26.0,26"),
        CONTROL.replace("26,26", "26,26garbage"),
        CONTROL.replace("  : values=26,26\n", ""),
        CONTROL.replace("rw---RW-", "r----R--"),
        CONTROL.replace("0.20dB", "0.205dB"),
        CONTROL.replace("-51.00dB", "-51.001dB"),
        CONTROL.replace("name='mysoftvol'", "name='mysoftvol',index=1"),
        CONTROL.replace("name='mysoftvol'", "name='mysoftvol',device=1"),
        CONTROL.replace("type=INTEGER", "type=INTEGER64"),
        CONTROL.replace(" : values=26,26", " : values=26,26\n : values=26,26"),
        CONTROL.replace("mute=0", "mute=0 trailing"),
        CONTROL + "\0hidden",
    ],
)
def test_unsupported_or_malformed_control_cannot_produce_a_raw_write(
    json_binary, tmp_path, control
):
    result = gain(json_binary, config_file(tmp_path / "sps.conf"), "-18", control=control)
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize(
    "pair,success", [("177,177", True), ("176,177", False), ("177,176", False), ("26,26", False)]
)
def test_verify_requires_both_channels_at_the_quantized_target(
    json_binary, tmp_path, pair, success
):
    result = gain(
        json_binary,
        config_file(tmp_path / "sps.conf"),
        "-18",
        control=CONTROL.replace("26,26", pair),
        verify=True,
    )
    assert result.returncode == (0 if success else 2)
    assert result.stdout == ("177,177\n" if success else "")


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.replace("volume_max_db = -4.0;", ""),
        lambda s: s.replace("volume_range_db = 30;", ""),
        lambda s: s.replace('"mysoftvol"', '"another-control"'),
        lambda s: s.replace("mixer_control_name", 'output_device="hw:1"; mixer_control_name'),
        lambda s: s.replace("volume_range_db = 30;", "volume_range_db = 90;"),
        lambda s: s.replace("volume_range_db = 30;", "volume_range_db = -1;"),
        lambda s: s.replace("volume_max_db = -4.0;", "volume_max_db = 5;"),
        lambda s: s + '@include "another.conf"',
        lambda s: s.replace("volume_max_db = -4.0;", "volume_max_db = -4; volume_max_db = -8;"),
        lambda s: s.replace(
            "volume_range_db = 30;", 'volume_range_db = 30; ignore_volume_control="yes";'
        ),
        lambda s: s.replace(
            "mixer_control_name", 'use_hardware_mute_if_available="yes"; mixer_control_name'
        ),
        lambda s: s.replace(
            "mixer_control_name", 'mute_using_playback_switch="yes"; mixer_control_name'
        ),
        lambda s: s.replace("mixer_control_name", "mixer_control_index=1; mixer_control_name"),
        lambda s: s.replace(
            "volume_range_db = 30;", 'volume_range_db = 30; output_backend="pipe";'
        ),
        lambda s: s.replace("volume_range_db = 30;", "volume_range_db = 30.5;"),
        lambda s: s + "\0",
        lambda s: s + "/* unterminated",
        lambda s: s + "x" * 16384,
    ],
)
def test_unknown_or_unsafe_config_cannot_produce_a_guessed_target(json_binary, tmp_path, change):
    result = gain(json_binary, config_file(tmp_path / "sps.conf", change(CONFIG)), "-6")
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize(
    "raw,profile,control,start",
    [
        ("nan", "standard", CONTROL, "1"),
        ("inf", "standard", CONTROL, "1"),
        ("-31", "standard", CONTROL, "1"),
        ("1", "standard", CONTROL, "1"),
        ("-6", "dasl_tapered", CONTROL, "1"),
        ("-6", "standard", CONTROL.replace("mute=0", "mute=1"), "1"),
        ("-6", "standard", CONTROL.replace("step=0.20dB", "step=0.00dB"), "1"),
        ("-6", "standard", CONTROL.replace("dBscale-min", "dBlinear-min"), "1"),
        ("-6", "standard", CONTROL.replace("values=2", "values=1"), "1"),
        ("-6", "standard", CONTROL.replace("mysoftvol", "another-control"), "1"),
        ("-6", "standard", CONTROL * 2, "1"),
        ("-6", "standard", "x" * 20000, "1"),
        ("-6", "standard", CONTROL, "0"),
        ("-6", "standard", CONTROL, "1.5"),
        ("-6", "standard", CONTROL, "9007199254740991"),
    ],
)
def test_invalid_live_inputs_fail_without_a_target(
    json_binary, tmp_path, raw, profile, control, start
):
    result = gain(
        json_binary, config_file(tmp_path / "sps.conf"), raw, profile, control=control, start=start
    )
    assert result.returncode == 2 and not result.stdout


def test_config_modified_after_process_start_is_not_treated_as_loaded(json_binary, tmp_path):
    path = tmp_path / "sps.conf"
    path.write_text(CONFIG)
    assert gain(json_binary, path, "-6").returncode == 2


def test_symlink_config_cannot_change_which_configuration_is_verified(json_binary, tmp_path):
    source = config_file(tmp_path / "source.conf")
    link = tmp_path / "sps.conf"
    link.symlink_to(source)
    assert gain(json_binary, link, "-6").returncode == 2


def test_supported_comments_and_unrelated_config_sections_are_not_interpreted(
    json_binary, tmp_path
):
    source = (
        CONFIG.replace("general =", "/* comment */ general =").replace(
            "volume_max_db", "// comment\nvolume_max_db"
        )
        + 'pipe = { name="synthetic"; }; diagnostics={ log_verbosity=0; };'
    )
    result = gain(json_binary, config_file(tmp_path / "sps.conf", source), "-6")
    assert result.returncode == 0 and result.stdout.strip() == "220,220"


def install_shared_mixer(path):
    """Only proc/mixer/DBus transport is synthetic; recovery code stays real."""
    enable_airplay_duck_fixture(path)
    config_file(path / "sps.conf")
    (path / "sps-control").write_text(CONTROL)
    (path / "sps-cmdline").write_bytes(
        b"/usr/bin/shairport-sync\0-c\0" + os.fsencode(path / "sps.conf") + b"\0"
    )
    recovery = path / "bridge/lib/sender-recovery.sh"
    recovery.write_text(
        recovery.read_text().replace('"/proc/$sender_pid/cmdline"', '"$STATE_DIR/sps-cmdline"')
    )
    script(path / "tools/pidof", f"#!/bin/sh\nprintf '%s\\n' {os.getpid()}\n")
    script(
        path / "tools/dbus-send",
        """#!/bin/sh
case "$*" in
 *org.bluez*) printf 'string "State"\\nvariant string "idle"\\n';exit 0;;
 *Properties.GetAll*) ;;
 *) : > "$STATE_DIR/sender-write-attempted";exit 1;;
esac
if [ -f "$STATE_DIR/fail-query" ];then exit 1;fi
raw=-18;[ ! -r "$STATE_DIR/sender-raw" ] || read raw < "$STATE_DIR/sender-raw"
active=true;[ ! -r "$STATE_DIR/sender-active" ] || read active < "$STATE_DIR/sender-active"
profile=standard;[ ! -r "$STATE_DIR/sender-profile" ] || read profile < "$STATE_DIR/sender-profile"
version=4.3.5;[ ! -r "$STATE_DIR/sender-version" ] || read version < "$STATE_DIR/sender-version"
case "$*" in *string:org.gnome.ShairportSync.RemoteControl*)
 [ ! -f "$STATE_DIR/fail-remote-query" ] || exit 1
 committed=$raw
 [ ! -r "$STATE_DIR/committed-raw" ] || read committed < "$STATE_DIR/committed-raw"
 printf 'string "AirplayVolume"\\nvariant double %s\\n' "$committed"
 if [ -f "$STATE_DIR/next-remote-read" ];then
   mv "$STATE_DIR/next-remote-read" "$STATE_DIR/committed-raw"
 fi
 if [ -f "$STATE_DIR/raw-after-remote" ];then
   mv "$STATE_DIR/raw-after-remote" "$STATE_DIR/sender-raw"
   if [ -f "$STATE_DIR/mixer-after-remote" ];then
     mv "$STATE_DIR/mixer-after-remote" "$STATE_DIR/mixer-raw-mysoftvol"
   fi
 fi
 if [ -f "$STATE_DIR/gain-written" ] && [ -f "$STATE_DIR/raw-after-final-remote" ];then
   mv "$STATE_DIR/raw-after-final-remote" "$STATE_DIR/sender-raw"
   if [ -f "$STATE_DIR/mixer-after-final-remote" ];then
     mv "$STATE_DIR/mixer-after-final-remote" "$STATE_DIR/mixer-raw-mysoftvol"
   fi
 fi
 exit;;esac
printf 'string "Active"\\nvariant boolean %s\\nstring "Volume"\\nvariant double %s\\n' "$active" "$raw"
printf 'string "Version"\\nvariant string "%s"\\nstring "VolumeControlProfile"\\nvariant string "%s"\\n' "$version" "$profile"
""",
    )
    mixer = path / "tools/amixer"
    original = path / "tools/original-amixer"
    mixer.rename(original)
    script(
        mixer,
        """#!/bin/sh
{ printf '%s\\t' "$@";printf '\\n'; } >> "$STATE_DIR/amixer-commands"
# Match the LX06 command contract. Do not silently accept GNU-style -- or dB.
[ "$1" != -- ] || exit 1
case "${3:-}" in *dB) exit 1;;esac
if [ "$1" = cget ];then
 [ "$#" = 2 ] && [ "$2" = name=mysoftvol ] || exit 1
 if [ -f "$STATE_DIR/gain-written" ];then
   [ ! -f "$STATE_DIR/fail-gain-read" ] || exit 1
   if [ -f "$STATE_DIR/readback-control" ];then cat "$STATE_DIR/readback-control";exit;fi
 fi
 pair=26,26;[ ! -r "$STATE_DIR/mixer-raw-mysoftvol" ] || read pair < "$STATE_DIR/mixer-raw-mysoftvol"
 awk -v pair="$pair" '{if($0 ~ /^  : values=/)print "  : values=" pair;else print}' "$STATE_DIR/sps-control"
 exit
fi
case "$1:$2" in
 cset:name=mysoftvol)
   [ "$#" = 3 ] || exit 1
   printf '%s\\n' "$3" | awk -F, 'NF!=2 || $1 !~ /^(0|[1-9][0-9]*)$/ || $2 !~ /^(0|[1-9][0-9]*)$/ || $1>255 || $2>255 {exit 1}' || exit 1
   [ ! -f "$STATE_DIR/fail-gain-write" ] || exit 1
   printf 'mysoftvol %s\\n' "$3" >> "$STATE_DIR/mixer-writes"
   if [ ! -f "$STATE_DIR/no-effect-cset" ];then
     printf '%s\\n' "$3" > "$STATE_DIR/mixer-raw-mysoftvol"
     level=$(printf '%s\\n' "$3" | awk -F, '{print int($1*100/255+0.5)}')
     printf '%s\\n' "$level" > "$STATE_DIR/mixer-mysoftvol"
   fi
   touch "$STATE_DIR/gain-written"
   if [ -f "$STATE_DIR/change-during-write" ];then
     rm "$STATE_DIR/change-during-write";printf '%s\\n' -10 > "$STATE_DIR/sender-raw"
   fi
   if [ -f "$STATE_DIR/next-raw" ];then
     mv "$STATE_DIR/next-raw" "$STATE_DIR/sender-raw"
   fi
   if [ -f "$STATE_DIR/next-committed-raw" ];then
     mv "$STATE_DIR/next-committed-raw" "$STATE_DIR/committed-raw"
   fi
   for field in active profile version;do
     if [ -f "$STATE_DIR/next-sender-$field" ];then
       mv "$STATE_DIR/next-sender-$field" "$STATE_DIR/sender-$field"
     fi
   done
   if [ -f "$STATE_DIR/query-fails-after-write" ];then
     rm "$STATE_DIR/query-fails-after-write";touch "$STATE_DIR/fail-query"
   fi
   if [ -f "$STATE_DIR/hold-after-write" ];then
     sleep 30
   fi
   exit 0;;
 sset:*)
   [ "$#" = 3 ] || exit 1
   printf '%s\\n' "$3" | grep -Eq '^[0-9]+([.][0-9]+)?%$' || exit 1
   if [ "$2" = mysoftvol ];then
     pair=$(LC_ALL=C awk -v percent="${3%?}" 'BEGIN{v=int(percent*255/100+0.5);printf "%d,%d",v,v}')
     printf '%s\\n' "$pair" > "$STATE_DIR/mixer-raw-mysoftvol"
   fi;;
 get:*) [ "$#" = 2 ] || exit 1;;
 *) exit 1;;
esac
exec "$STATE_DIR/tools/original-amixer" "$@"
""",
    )


def pending_journal(path, **extra):
    journal = path / "xiaoai_bridge.duck.json"
    journal.write_text(
        json.dumps(
            {
                "owner": 0,
                "start": "",
                "revision": 1,
                "volumes": {"master": 60},
                "applied_master": 10,
                "airplay_raw": "-18",
                "airplay_active": "true",
                **extra,
            }
        )
    )
    (path / "sender-raw").write_text("-6\n")
    (path / "mixer-mysoftvol").write_text("10\n")
    return journal


def recover(path, env, speaker_shell):
    return subprocess.run(
        [*speaker_shell, "-c", f'. "{path}/bridge/lib/common.sh"\nbridge_recover_pending'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("kind", ["duck", "voice-volume"])
def test_real_lx06_pending_uses_integer_stereo_write_and_readback(text_device, speaker_shell, kind):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True, volumes={"master": 69})
    if kind != "duck":
        journal = journal.rename(path / f"xiaoai_bridge.{kind}.json")
    (path / "sender-raw").write_text("-18\n")
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "177,177"
    assert (path / "mixer-mysoftvol").read_text().strip() == "69"
    commands = (path / "amixer-commands").read_text().splitlines()
    write = commands.index("cset\tname=mysoftvol\t177,177\t")
    assert commands[write + 1] == "cget\tname=mysoftvol\t"
    assert (path / "sender-raw").read_text().strip() == "-18"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize("kind", ["duck", "voice-volume"])
@pytest.mark.parametrize("already_applied", [False, True])
def test_lx06_main_minus18_remote_minus19_875_finishes_verified_recovery(
    text_device, speaker_shell, kind, already_applied
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True, volumes={"master": 69})
    if kind != "duck":
        journal = journal.rename(path / f"xiaoai_bridge.{kind}.json")
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text("-19.875\n")
    if already_applied:
        (path / "mixer-raw-mysoftvol").write_text("177,177\n")
        (path / "mixer-mysoftvol").write_text("69\n")
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "177,177"
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 177,177"]
    commands = (path / "amixer-commands").read_text().splitlines()
    write = commands.index("cset\tname=mysoftvol\t177,177\t")
    assert commands[write + 1] == "cget\tname=mysoftvol\t"
    assert (path / "sender-raw").read_text().strip() == "-18"
    assert (path / "committed-raw").read_text().strip() == "-19.875"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize(
    "arguments", [["--", "sset", "mysoftvol", "-45.80dB"], ["sset", "mysoftvol", "-45.80dB"]]
)
def test_transport_fixture_cannot_silently_accept_db_writes(text_device, arguments):
    _, path, env = text_device
    install_shared_mixer(path)
    # Only the integer cset contract is confirmed. Reject unverified dB
    # alternatives too, so this fixture cannot accidentally certify them.
    result = subprocess.run([str(path / "tools/amixer"), *arguments], env=env, capture_output=True)
    assert result.returncode == 1
    assert not (path / "mixer-writes").exists()


def test_lx06_confirmed_cset_form_accepts_two_raw_integer_channels(text_device):
    _, path, env = text_device
    install_shared_mixer(path)
    result = subprocess.run(
        [str(path / "tools/amixer"), "cset", "name=mysoftvol", "26,26"],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "26,26"
    assert (path / "mixer-mysoftvol").read_text().strip() == "10"


@pytest.mark.parametrize(
    "fault",
    [
        "no-effect-cset",
        "fail-gain-read",
        "wrong-right",
        "wrong-left",
        "wrong-tlv",
        "malformed-control",
    ],
)
def test_cset_success_without_valid_stereo_readback_retains_journal_and_retries(
    text_device, speaker_shell, fault
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True, volumes={"master": 69, "tts": 55})
    (path / "sender-raw").write_text("-18\n")
    if fault in ("no-effect-cset", "fail-gain-read"):
        marker = path / fault
        marker.touch()
    else:
        marker = path / "readback-control"
        control = CONTROL.replace("26,26", "177,177")
        if fault == "wrong-right":
            control = control.replace("177,177", "177,26")
        elif fault == "wrong-left":
            control = control.replace("177,177", "26,177")
        elif fault == "wrong-tlv":
            control = control.replace("0.20dB", "0.10dB").replace("max=255", "max=510")
        else:
            control = control.replace("values=2,min", "values=1,min")
        marker.write_text(control)
    assert recover(path, env, speaker_shell).returncode != 0
    saved = json.loads(journal.read_text())
    assert saved["sender_recovery"] is True and saved["volumes"] == {"master": 69}
    assert (path / "mixer-notifyvol").read_text().strip() == "55"
    assert (path / "sender-raw").read_text().strip() == "-18"
    assert not (path / "sender-write-attempted").exists()
    marker.unlink()
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "177,177"


@pytest.mark.parametrize("kind", ["duck", "voice-volume"])
def test_ordinary_recovery_does_not_load_sender_resolver(text_device, speaker_shell, kind):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    if kind != "duck":
        journal = journal.rename(path / f"xiaoai_bridge.{kind}.json")
    (path / "sender-raw").write_text("-18\n")
    (path / "bridge/lib/sender-recovery.sh").write_text(
        ': > "$STATE_DIR/resolver-loaded"\nexit 99\n'
    )
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists() and not (path / "resolver-loaded").exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "60"


@pytest.mark.parametrize("mode", ["announce", "send_text"])
def test_sender_change_while_playing_ends_duck_at_exact_latest_gain(text_device, mode):
    call, path, _ = text_device
    install_shared_mixer(path)
    script(path / "tools/mpg123", "#!/bin/sh\nprintf '%s\\n' -6 > \"$STATE_DIR/sender-raw\"\n")
    result, _ = call(mode, "synthetic", volume=20)
    assert result.returncode == 0, result.stdout
    assert (path / "mixer-mysoftvol").read_text().strip() == "86"
    assert (path / "sender-raw").read_text().strip() == "-6"
    assert (path / "mixer-notifyvol").read_text().strip() == "60"
    assert not (path / "xiaoai_bridge.duck.json").exists()
    assert not (path / "sender-write-attempted").exists()
    assert "mysoftvol 60%" not in (path / "mixer-writes").read_text().splitlines()


@pytest.mark.parametrize(
    "fault", ["change-during-write", "query-fails-after-write", "fail-gain-write"]
)
def test_racing_sender_or_failed_write_keeps_repair_intent_for_retry(
    text_device, speaker_shell, fault
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    (path / fault).touch()
    result = recover(path, env, speaker_shell)
    assert result.returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    if fault == "query-fails-after-write":
        (path / "fail-query").unlink()
        (path / "sender-raw").write_text("-10\n")
    elif fault == "fail-gain-write":
        (path / fault).unlink()
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0, result.stderr
    assert not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == (
        "86" if fault == "fail-gain-write" else "82"
    )
    assert not (path / "sender-write-attempted").exists()


def test_newer_ha_master_wins_over_interrupted_sender_repair(text_device, speaker_shell):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "xiaoai_bridge.desired.json").write_text(
        '{"revision":2,"versions":{"master":2},"volumes":{"master":75}}'
    )
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0 and not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "75"
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 75%"]
    assert not any(
        line.startswith("cset\t") for line in (path / "amixer-commands").read_text().splitlines()
    )


def test_sender_can_intentionally_choose_the_same_ten_percent_as_the_cap(
    text_device, speaker_shell
):
    _, path, env = text_device
    install_shared_mixer(path)
    config_file(path / "sps.conf", CONFIG.replace("volume_range_db = 30", "volume_range_db = 45"))
    journal = pending_journal(path)
    (path / "sender-raw").write_text("-29.1\n")
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0 and not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "10"
    assert (path / "mixer-writes").read_text().splitlines() == ["mysoftvol 26,26"]


@pytest.mark.parametrize(
    "fault",
    ["fail-remote-query", "invalid-remote", "missing-remote", "-30.001", "0.001", "-145", "nan"],
)
def test_unknown_remote_observation_retains_journal_without_writing(
    text_device, speaker_shell, fault
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    if fault == "fail-remote-query":
        marker = path / fault
        marker.touch()
    else:
        marker = path / "committed-raw"
        marker.write_text(
            {"invalid-remote": "invalid", "missing-remote": ""}.get(fault, fault) + "\n"
        )
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    assert not (path / "mixer-writes").exists()
    marker.unlink()
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize("before", ["-19.875", "-30", "0", "-144"])
@pytest.mark.parametrize(
    "after,success", [("-18", True), ("-18.0", True), ("-12", False), ("-19.876", False)]
)
def test_remote_may_stay_stale_or_catch_up_but_another_update_retains_journal(
    text_device, speaker_shell, before, after, success
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text(before + "\n")
    (path / "next-committed-raw").write_text(after + "\n")
    result = recover(path, env, speaker_shell)
    assert (result.returncode == 0) is success
    assert journal.exists() is not success
    if not success:
        assert json.loads(journal.read_text())["sender_recovery"] is True
        assert recover(path, env, speaker_shell).returncode == 0
        assert not journal.exists()
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "177,177"
    assert (path / "sender-raw").read_text().strip() == "-18"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize("when", ["before-write", "during-write", "after-remote"])
def test_latest_main_change_is_rejected_even_while_remote_stays_at_previous_value(
    text_device, speaker_shell, when
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text("-19.875\n")
    if when == "during-write":
        (path / "next-raw").write_text("-10\n")
    else:
        suffix = "remote" if when == "before-write" else "final-remote"
        (path / f"raw-after-{suffix}").write_text("-10\n")
        (path / f"mixer-after-{suffix}").write_text("210,210\n")
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    assert (path / "sender-raw").read_text().strip() == "-10"
    if when == "before-write":
        assert not (path / "mixer-writes").exists()
    if when != "during-write":
        assert (path / "mixer-raw-mysoftvol").read_text().strip() == "210,210"
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "210,210"
    assert (path / "sender-raw").read_text().strip() == "-10"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize("after", ["", "invalid", "nan"])
def test_missing_or_invalid_postwrite_remote_keeps_recovery_intent(
    text_device, speaker_shell, after
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text("-19.875\n")
    (path / "next-committed-raw").write_text(after + "\n")
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    (path / "committed-raw").write_text("-19.875\n")
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize(
    "field,value", [("active", "false"), ("version", "4.3.6"), ("profile", "flat")]
)
def test_stable_stale_remote_cannot_hide_shairport_state_changes(
    text_device, speaker_shell, field, value
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text("-19.875\n")
    (path / f"next-sender-{field}").write_text(value + "\n")
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    assert not (path / "sender-write-attempted").exists()


def test_stable_stale_remote_cannot_hide_changed_process_starttime(text_device, speaker_shell):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-18\n")
    (path / "committed-raw").write_text("-19.875\n")
    # Inject PID reuse only after cset; all normal /proc parsing stays intact.
    locks = path / "bridge/lib/locks.sh"
    locks.write_text(
        locks.read_text().replace(
            "bridge_pid_start() {\n",
            f"""bridge_pid_start() {{
    if [ "$1" = {os.getpid()} ] && [ -f "$STATE_DIR/gain-written" ];then
        printf '1\\n';return 0
    fi
""",
            1,
        )
    )
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    assert (path / "mixer-raw-mysoftvol").read_text().strip() == "177,177"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize("remote", ["-6.001", "-19.875"])
@pytest.mark.parametrize("race", [False, True])
def test_exact_fractional_main_and_remote_values_do_not_hide_tiny_main_updates(
    text_device, speaker_shell, race, remote
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    (path / "sender-raw").write_text("-6.001\n")
    (path / "committed-raw").write_text(remote + "\n")
    if race:
        # Do not round away a real change, even if its ALSA target is identical.
        (path / "next-raw").write_text("-6.002\n")
    result = recover(path, env, speaker_shell)
    assert (result.returncode != 0) is race
    assert journal.exists() is race
    if race:
        assert recover(path, env, speaker_shell).returncode == 0 and not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "86"


def test_sender_mute_is_preserved_and_cap_is_only_lifted_after_mute_ack(text_device, speaker_shell):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    (path / "sender-raw").write_text("-144\n")
    (path / "committed-raw").write_text("-6\n")
    assert recover(path, env, speaker_shell).returncode != 0
    assert not (path / "mixer-writes").exists() and journal.exists()
    (path / "committed-raw").write_text("-144\n")
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "60"
    assert (path / "sender-raw").read_text().strip() == "-144"
    assert not (path / "sender-write-attempted").exists()


def test_mute_confirmation_must_still_hold_at_the_new_prewrite_observation(
    text_device, speaker_shell
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=True)
    (path / "sender-raw").write_text("-144\n")
    (path / "committed-raw").write_text("-144\n")
    (path / "next-remote-read").write_text("-6\n")
    assert recover(path, env, speaker_shell).returncode != 0
    assert json.loads(journal.read_text())["sender_recovery"] is True
    assert not (path / "mixer-writes").exists()
    (path / "committed-raw").write_text("-144\n")
    assert recover(path, env, speaker_shell).returncode == 0
    assert not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == "60"
    assert (path / "sender-raw").read_text().strip() == "-144"
    assert not (path / "sender-write-attempted").exists()


@pytest.mark.parametrize(
    "args", [b"-c\0/unused\0--\0-m\0other\0", b"-o\0pipe\0", b"-c\0relative.conf\0"]
)
def test_unproved_process_arguments_do_not_modify_master(text_device, speaker_shell, args):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    (path / "sps-cmdline").write_bytes(b"/usr/bin/shairport-sync\0" + args)
    assert recover(path, env, speaker_shell).returncode != 0
    assert journal.exists() and not (path / "mixer-writes").exists()


@pytest.mark.parametrize("fault", ["fail-query", "unknown-profile", "unknown-config"])
def test_unknown_observation_never_discards_journal_or_blocks_other_channels(
    text_device, speaker_shell, fault
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, volumes={"master": 60, "tts": 55})
    if fault == "fail-query":
        (path / fault).touch()
    elif fault == "unknown-profile":
        (path / "sender-profile").write_text("unsupported\n")
    else:
        config_file(path / "sps.conf", CONFIG + '@include "unknown.conf"')
    result = recover(path, env, speaker_shell)
    assert result.returncode != 0
    assert json.loads(journal.read_text())["volumes"] == {"master": 60}
    assert (path / "mixer-mysoftvol").read_text().strip() == "10"
    assert (path / "mixer-notifyvol").read_text().strip() == "55"


@pytest.mark.parametrize("forced,current", [(False, 10), (True, 10), (True, 86)])
def test_confirmed_stopped_sender_can_finish_without_touching_a_new_master(
    text_device, speaker_shell, forced, current
):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path, sender_recovery=forced)
    (path / "mixer-mysoftvol").write_text(f"{current}\n")
    (path / "sender-active").write_text("false\n")
    result = recover(path, env, speaker_shell)
    assert result.returncode == 0 and not journal.exists()
    assert (path / "mixer-mysoftvol").read_text().strip() == ("60" if current == 10 else "86")


def test_kill_after_gain_write_recovers_after_owner_dies(text_device, speaker_shell):
    _, path, env = text_device
    install_shared_mixer(path)
    journal = pending_journal(path)
    (path / "hold-after-write").touch()
    proc = subprocess.Popen(
        [*speaker_shell, "-c", f'. "{path}/bridge/lib/common.sh"\nbridge_recover_pending'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not (path / "gain-written").exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert json.loads(journal.read_text())["sender_recovery"] is True
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)
        (path / "hold-after-write").unlink()
        (path / "sender-raw").write_text("-10\n")
        assert recover(path, env, speaker_shell).returncode == 0
        assert not journal.exists()
        assert (path / "mixer-mysoftvol").read_text().strip() == "82"
        with (path / "xiaoai_bridge.volume.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate(timeout=5)


def test_sender_repair_marker_survives_duck_to_voice_handoff(json_binary, tmp_path):
    duck = tmp_path / "duck.json"
    duck.write_text(
        json.dumps(
            {
                "owner": 77,
                "start": "88",
                "revision": 1,
                "volumes": {"master": 60},
                "airplay_raw": "-18",
                "airplay_active": "true",
                "applied_master": 10,
                "sender_recovery": True,
            }
        )
    )
    desired = tmp_path / "desired.json"
    desired.write_text("{}")
    result = op(
        json_binary,
        "journal",
        desired,
        duck,
        '{"master":86,"bluetooth":50}',
        77,
        88,
        "-6",
        "true",
        86,
    )
    assert result.returncode == 0
    voice = json.loads(result.stdout)
    assert voice["sender_recovery"] is True and voice["volumes"] == {"master": 86, "bluetooth": 50}
    target = tmp_path / "voice.json"
    target.write_text(json.dumps({**voice, "applied_master": 10}))
    result = op(json_binary, "recovery-plan", target, desired, "-6", "true", "10")
    assert result.stdout.splitlines() == ["master\tpending", "bluetooth\t50"]
