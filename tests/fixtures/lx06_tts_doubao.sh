#!/bin/sh
# Doubao Seed-TTS 2.0 V3 adapter for xiaoai-patch LX06.
# Usage: /data/tts_doubao "text to speak"
# Optional credentials: DOUBAO_API_KEY (new console) OR
# DOUBAO_APP_ID + DOUBAO_ACCESS_KEY (old console).

CONF=/data/doubao_tts.conf
[ -r "$CONF" ] && . "$CONF"

TEXT="$*"
[ -n "$TEXT" ] || exit 0

RESOURCE_ID="${DOUBAO_RESOURCE_ID:-seed-tts-2.0}"
SPEAKER="${DOUBAO_SPEAKER:-zh_female_xiaohe_uranus_bigtts}"
API_KEY="${DOUBAO_API_KEY:-}"
APP_ID="${DOUBAO_APP_ID:-}"
ACCESS_KEY="${DOUBAO_ACCESS_KEY:-}"

fallback() {
    # 第一回退：Microsoft 晓晓
    echo "Doubao TTS unavailable; trying Microsoft TTS" >&2

    if [ -x /data/tts_microsoft_fallback ]; then
        if /data/tts_microsoft_fallback "$TEXT"; then
            return 0
        fi
    fi

    # 第二回退：Google TTS
    echo "Microsoft TTS unavailable; trying Google TTS" >&2

    if [ -x /data/tts_google_zh ]; then
        /data/tts_google_zh "$TEXT"
    else
        return 1
    fi
}

if [ -z "$API_KEY" ] && { [ -z "$APP_ID" ] || [ -z "$ACCESS_KEY" ]; }; then
    echo "Doubao TTS credentials missing in $CONF" >&2
    fallback
    exit $?
fi

WORK="/tmp/doubao_tts_$$"
if ! mkdir "$WORK" 2>/dev/null; then
    echo "Cannot create temporary TTS directory" >&2
    fallback
    exit $?
fi
trap 'rm -rf "$WORK"' 0
trap 'exit 1' 1 2 3 15

REQ_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)"
[ -n "$REQ_ID" ] || REQ_ID="$(date +%s)-$$"

if ! jq -nc --arg text "$TEXT" --arg speaker "$SPEAKER" \
    '{req_params:{text:$text,speaker:$speaker,audio_params:{format:"mp3",sample_rate:24000}}}' \
    > "$WORK/request.json"; then
    fallback
    exit $?
fi

set -- \
    -H 'Content-Type: application/json' \
    -H "X-Api-Resource-Id: $RESOURCE_ID" \
    -H "X-Api-Request-Id: $REQ_ID"

if [ -n "$API_KEY" ]; then
    set -- "$@" -H "X-Api-Key: $API_KEY"
else
    set -- "$@" \
        -H "X-Api-App-Id: $APP_ID" \
        -H "X-Api-Access-Key: $ACCESS_KEY"
fi

if ! curl -fsS --compressed \
    --connect-timeout 8 --max-time 35 \
    "$@" \
    -o "$WORK/response.jsonl" \
    --data-binary @"$WORK/request.json" \
    'https://tts.example.invalid'; then
    echo 'Doubao HTTP request failed' >&2
    fallback
    exit $?
fi

# V3 returns JSON records with base64 MP3 chunks and a final 20000000 code.
API_ERRORS="$(jq -r 'select(.code != 0 and .code != 20000000) |
    "code=\(.code): \(.message // "unknown")"' "$WORK/response.jsonl" 2>/dev/null)"
if [ -n "$API_ERRORS" ]; then
    printf 'Doubao API error: %s\n' "$API_ERRORS" >&2
    fallback
    exit $?
fi

if ! jq -e 'select(.code == 20000000)' "$WORK/response.jsonl" >/dev/null; then
    jq -r 'select(.code != 0 and .code != 20000000) |
        "Doubao error code=\(.code): \(.message // "unknown")"' \
        "$WORK/response.jsonl" >&2
    echo 'Doubao response incomplete or failed' >&2
    fallback
    exit $?
fi

if ! jq -r 'select(.code == 0) | .data |
    select(type == "string" and length > 0)' \
    "$WORK/response.jsonl" > "$WORK/chunks.b64"; then
    fallback
    exit $?
fi

: > "$WORK/speech.mp3"
while IFS= read -r chunk || [ -n "$chunk" ]; do
    if ! printf '%s' "$chunk" | base64 -d >> "$WORK/speech.mp3"; then
        echo 'Doubao base64 decoding failed' >&2
        fallback
        exit $?
    fi
done < "$WORK/chunks.b64"

if [ ! -s "$WORK/speech.mp3" ]; then
    echo 'Doubao returned no audio' >&2
    fallback
    exit $?
fi

if /bin/miplay "$WORK/speech.mp3" -w; then
    exit 0
fi

fallback
