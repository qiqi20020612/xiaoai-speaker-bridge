#!/bin/sh
# Read-only compatibility checks for private, reviewed sibling scripts.
bridge_adapter_reject() {
    printf '%s\n' "$1" 'Regenerate and review private adapters with this release prepare_adapters.py, using the saved unmodified launcher/LED sources; place them in the extracted package adapter/ directory and retry --dry-run.' >&2
    return 1
}
bridge_check_adapter_directory() {
    adapter_dir=$1
    original_launcher=$2
    review_file=$adapter_dir/review.json
    listener_file=$adapter_dir/listener.sh
    [ -r "$review_file" ] && [ -x "$listener_file" ] && [ -r "$original_launcher" ] || {
        bridge_adapter_reject 'Listener adapter or its original/review is missing.';return 1;
    }
    jq -e '.format==2 and .compatibility.listener_hooks==2' "$review_file" >/dev/null 2>&1 || {
        bridge_adapter_reject 'Listener adapter is incompatible with the early wake LED hooks.';return 1;
    }
    listener_source=$(jq -er '.files["listener.sh"].source_sha256' "$review_file") && \
    listener_output=$(jq -er '.files["listener.sh"].output_sha256' "$review_file") || {
        bridge_adapter_reject 'Listener review is missing its source/output hashes.';return 1;
    }
    [ "$listener_source" = "$(sha256sum "$original_launcher" | awk '{print $1}')" ] && \
    [ "$listener_output" = "$(sha256sum "$listener_file" | awk '{print $1}')" ] || {
        bridge_adapter_reject 'Listener source/output no longer matches its private review.';return 1;
    }
    # Check the actual shape too: one hook before one snapshot, no second LED call.
    sh -n "$listener_file" && awk '
        /^[[:space:]]*#/ {next}
        /\/bin\/show_led/ {bad=1}
        /^\. \/data\/xiaoai_bridge\/lib\/listener-hooks\.sh$/ {hooks++;h=NR}
        /^bridge_voice_begin \|\| exit 1$/ {begins++;b=NR}
        /^SAVED_VOL=/ {snapshots++;s=NR}
        END {exit !(hooks==1 && begins==1 && snapshots==1 && h<b && s==b+1 && !bad)}
    ' "$listener_file" || {
        bridge_adapter_reject 'Listener adapter still has an LED command or an unverified wake hook order.';return 1;
    }
    if [ -e "$adapter_dir/airplay-led.sh" ];then
        led_file=$adapter_dir/airplay-led.sh
        led_output=$(jq -er 'select(.compatibility.airplay_led_guard==2) | .files["airplay-led.sh"].output_sha256' "$review_file") || {
            bridge_adapter_reject 'AirPlay LED adapter lacks the shared LED lock review.';return 1;
        }
        [ -x "$led_file" ] && [ "$led_output" = "$(sha256sum "$led_file" | awk '{print $1}')" ] && \
        sh -n "$led_file" && grep -qF 'bridge_lock_for 0 6' "$led_file" && grep -qF 'bridge_led_busy' "$led_file" || {
            bridge_adapter_reject 'AirPlay LED adapter is incompatible or changed since review.';return 1;
        }
    fi
}
