#!/bin/sh
# Synthetic fixture for the existing sourced Agent contract. HTTP is real curl;
# this wrapper alone injects the Agent/cache fields, just like the original file.
EOAIC2_AGENT_ID=conversation.synthetic
EOAIC2_KEEP_SECONDS=300
EOAIC2_STATE="$STATE_DIR/eoai_conversation.json"

curl() {
    case " $* " in
        *"/api/conversation/process"*) ;;
        *) command curl "$@";return $? ;;
    esac
    local previous='' body='' part now cid payload reply latest tmp
    for part in "$@";do
        if [ "$previous" = -d ];then body=$part;break;fi
        previous=$part
    done
    [ -n "$body" ] || return 1
    now=$(date +%s)
    cid=''
    if [ -s "$EOAIC2_STATE" ];then
        cid=$(jq -r --arg agent "$EOAIC2_AGENT_ID" --argjson now "$now" \
            --argjson ttl "$EOAIC2_KEEP_SECONDS" \
            'if .agent==$agent and (.ts|type)=="number" and $now>=.ts and ($now-.ts)<$ttl then .id // "" else "" end' "$EOAIC2_STATE")
    fi
    payload=$(printf '%s' "$body" | jq -c --arg agent "$EOAIC2_AGENT_ID" --arg cid "$cid" \
        '.agent_id=$agent | if $cid!="" then .conversation_id=$cid else del(.conversation_id) end') || return 1
    reply=$(command curl -fsS --connect-timeout 10 --max-time 90 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        -d "$payload" "${HA_URL%/}/api/conversation/process") || return 1
    latest=$(printf '%s' "$reply" | jq -er '.conversation_id | select(type=="string" and length>0)')
    if [ -n "$latest" ];then
        tmp="${EOAIC2_STATE}.$$"
        (umask 077
            jq -n --arg agent "$EOAIC2_AGENT_ID" --arg id "$latest" --argjson ts "$(date +%s)" \
                '{agent:$agent,id:$id,ts:$ts}' > "$tmp"
        ) && mv -f "$tmp" "$EOAIC2_STATE" || rm -f "$tmp"
    fi
    printf '%s\n' "$reply"
}
