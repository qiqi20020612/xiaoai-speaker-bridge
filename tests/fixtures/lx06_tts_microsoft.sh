#!/bin/sh
# Wyoming Microsoft TTS fallback through Home Assistant.
# Produces MP3 from the streaming PCM WAV then plays it using mpg123.
# Returns nonzero on failure, NEVER invokes another TTS provider.

umask 077
[ "$#" -gt 0 ] || exit 1
TEXT="$*"

[ -r /data/listener ] || { echo '[Microsoft TTS] /data/listener missing' >&2; exit 1; }
. /data/listener
[ -r /data/doubao_tts.conf ] && . /data/doubao_tts.conf

HA_TTS_PROVIDER="${HA_TTS_PROVIDER:-tts.microsoft}"
HA_TTS_VOICE="${HA_TTS_VOICE:-zh-CN-XiaoxiaoNeural}"
HA_TTS_LANGUAGE="${HA_TTS_LANGUAGE:-zh-CN}"
HA_TTS_BASE_URL="${HA_TTS_BASE_URL:-https://tts.example.invalid}"

[ -n "${HA_URL:-}" ] && [ -n "${HA_TOKEN:-}" ] || {
    echo '[Microsoft TTS] HA_URL or HA_TOKEN missing' >&2
    exit 1
}

WORK="/tmp/microsoft_tts_$$"
mkdir "$WORK" 2>/dev/null || exit 1
trap 'rm -rf "$WORK"' 0
trap 'exit 1' 1 2 3 15

jq -nc \
    --arg engine "$HA_TTS_PROVIDER" \
    --arg message "$TEXT" \
    --arg language "$HA_TTS_LANGUAGE" \
    --arg voice "$HA_TTS_VOICE" \
    '{engine_id:$engine,message:$message,language:$language,cache:true,
      options:{voice:$voice,preferred_format:"wav"}}' > "$WORK/request.json" || exit 1

if ! curl -fsS --connect-timeout 5 --max-time 15 \
    -H "Authorization: Bearer ${HA_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data-binary @"$WORK/request.json" \
    -o "$WORK/url.json" \
    "${HA_URL%/}/api/tts_get_url"; then
    echo '[Microsoft TTS] failed to request audio URL' >&2
    exit 1
fi

TTS_PATH=$(jq -r '.path // empty' "$WORK/url.json" 2>/dev/null)
case "$TTS_PATH" in
    /api/tts_proxy/*) ;;
    *) echo '[Microsoft TTS] invalid audio URL response' >&2; exit 1 ;;
esac

if ! curl -fsSL --connect-timeout 5 --max-time 35 \
    -o "$WORK/speech.wav" \
    "${HA_TTS_BASE_URL%/}${TTS_PATH}"; then
    echo '[Microsoft TTS] WAV download failed' >&2
    exit 1
fi

SIZE=$(wc -c < "$WORK/speech.wav" | tr -d ' ')
if [ "$SIZE" -le 44 ] || [ "$(head -c 4 "$WORK/speech.wav")" != 'RIFF' ]; then
    echo '[Microsoft TTS] WAV is empty or invalid' >&2
    exit 1
fi

# HA Wyoming streaming WAV: the 44-byte header advertises 0 frames.
# The LX06's aplay won't play it; convert the PCM payload into MP3.
# Format verified on this LX06: signed 16-bit little-endian, 16 kHz, mono.
if ! tail -c +45 "$WORK/speech.wav" |
    ffmpeg -hide_banner -loglevel error \
        -f s16le -ar 16000 -ac 1 -i pipe:0 \
        -codec:a libmp3lame -b:a 64k -ar 44100 \
        -y "$WORK/speech.mp3"; then
    echo '[Microsoft TTS] WAV to MP3 failed' >&2
    exit 1
fi

[ -s "$WORK/speech.mp3" ] || { echo '[Microsoft TTS] no MP3 generated' >&2; exit 1; }

if ! mpg123 -q -a notify "$WORK/speech.mp3"; then
    echo '[Microsoft TTS] MP3 playback failed' >&2
    exit 1
fi
exit 0
