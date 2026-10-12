#!/bin/sh

[ -f /data/tts.conf ] && . /data/tts.conf
LANGUAGE="${LANGUAGE:-zh-CN}"

[ -n "$*" ] || exit 0

OUT="/tmp/tts_zh_$$.mp3"

curl -fsSL -G \
  -A "Mozilla/5.0" \
  --data-urlencode "ie=UTF-8" \
  --data-urlencode "client=tw-ob" \
  --data-urlencode "tl=${LANGUAGE}" \
  --data-urlencode "q=$*" \
  -o "$OUT" \
  "https://tts.example.invalid" || {
    rm -f "$OUT"
    exit 1
  }

/bin/miplay "$OUT" -w
RESULT=$?

rm -f "$OUT"
exit "$RESULT"
