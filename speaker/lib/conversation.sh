#!/bin/sh
# The existing EOAIC2 wrapper, not the bridge, owns {agent,id,ts} and its TTL.
bridge_load_conversation_adapter() {
    original_curl=$1
    [ -x "$original_curl" ] || return 1
    agent_script=$(config_value '.eoai_agent_script // "/data/eoai_agent.sh"')
    [ -r "$agent_script" ] || return 1
    # Reload its functions without resetting settings already selected by the
    # existing listener (including a locally configured cache path/TTL).
    saved_agent_set=${EOAIC2_AGENT_ID+yes};saved_agent=${EOAIC2_AGENT_ID:-}
    saved_state_set=${EOAIC2_STATE+yes};saved_state=${EOAIC2_STATE:-}
    saved_ttl_set=${EOAIC2_KEEP_SECONDS+yes};saved_ttl=${EOAIC2_KEEP_SECONDS:-}
    # Do not silently fall through to an executable or a different listener curl().
    unset -f curl 2>/dev/null || true
    . "$agent_script" || return 1
    [ "$saved_agent_set" != yes ] || EOAIC2_AGENT_ID=$saved_agent
    [ "$saved_state_set" != yes ] || EOAIC2_STATE=$saved_state
    [ "$saved_ttl_set" != yes ] || EOAIC2_KEEP_SECONDS=$saved_ttl
    LC_ALL=C type curl 2>/dev/null | grep -q 'function' || return 1
    [ -n "${EOAIC2_AGENT_ID:-}" ] && [ -n "${EOAIC2_STATE:-}" ] || return 1
    mkdir "$WORK/transport" || return 1
    cp "$BRIDGE_DIR/lib/conversation-curl.sh" "$WORK/transport/curl" || return 1
    chmod 700 "$WORK/transport/curl" || return 1
    XIAOAI_CONVERSATION_CURL=$original_curl
    XIAOAI_CONVERSATION_URL="${HA_URL%/}/api/conversation/process"
    XIAOAI_CONVERSATION_HEADERS="$WORK/conversation.headers"
    export XIAOAI_CONVERSATION_CURL XIAOAI_CONVERSATION_URL XIAOAI_CONVERSATION_HEADERS
    PATH="$WORK/transport:$PATH"
    export PATH
}

bridge_restore_conversation_state() {
    if [ "$had_state" = true ];then
        # Restore the exact previous bytes, even if the old cache was malformed.
        cp -p "$WORK/previous.json" "$EOAIC2_STATE.$$" && \
            mv -f "$EOAIC2_STATE.$$" "$EOAIC2_STATE"
    else
        rm -f "$EOAIC2_STATE"
    fi
}

bridge_refresh_ha_token() {
    [ -n "${HA_REFRESH_TOKEN:-}" ] || return 1
    auth_source=$(config_value '.ha_auth_source // "/data/porcupine_launcher_kws"')
    [ -r "$auth_source" ] || return 1
    # Extract only the reviewed original refresh function; never execute the KWS
    # launcher. Keep its grant, client ID and HA_TOKEN persistence. Its fatal
    # playback/service-stop branch becomes a task failure in this child pipeline.
    awk '
        /^ha_refresh_token\(\) \{$/ {active=1;print;next}
        !active {next}
        $0=="  log \"refreshing token\"" {next}
        $0=="  if [ \"$?\" -gt 0 ]; then" {
            error_branch=1;print;print "    return 1";next
        }
        error_branch {
            if ($0=="  fi") {error_branch=0;safe_error=1;print}
            next
        }
        $0=="  HA_TOKEN=$(jq -r .access_token /tmp/ha_token.json)" {
            token=1
            print "  HA_TOKEN=$(jq -er '\''.access_token | select(type==\"string\" and length>0)'\'' \"$WORK/ha_token.json\") || return 1"
            print "  case \"$HA_TOKEN\" in '\'''\''|*[!a-zA-Z0-9._-]*) return 1 ;; esac"
            next
        }
        {
            if (index($0,"grant_type=refresh_token&client_id=http://${SPEAKER_IP}&refresh_token=${HA_REFRESH_TOKEN}")) grant=1
            if (index($0,"sed -i \"s/^HA_TOKEN=.*/HA_TOKEN=${HA_TOKEN}/\" ${CONFIG_FILE}")) persist=1
            gsub(/\/tmp\/ha_token.json/,"\"$WORK/ha_token.json\"")
            print
            if ($0=="}") {complete=1;exit}
        }
        END {if (!complete || !safe_error || !token || !grant || !persist) exit 1}
    ' "$auth_source" > "$WORK/refresh.sh" || return 1
    sh -n "$WORK/refresh.sh" || return 1
    . "$WORK/refresh.sh" || return 1
    CONFIG_FILE=$LISTENER_CONFIG
    # The original function calls curl() for /auth/token. The Agent delegates
    # that URL unchanged to real curl; it never touches the conversation cache.
    ha_refresh_token > "$WORK/refresh.out" 2> "$WORK/refresh.err"
}

bridge_conversation_request() {
    : > "$XIAOAI_CONVERSATION_HEADERS"
    if curl --connect-timeout 3 --max-time 90 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        -d "$1" -XPOST "$XIAOAI_CONVERSATION_URL" \
        > "$WORK/conversation.json" 2> "$WORK/conversation.err";then
        return 0
    fi
    status=$(awk '/^HTTP\// {code=$2} END {print code}' "$XIAOAI_CONVERSATION_HEADERS")
    # Only an explicit 401 permits retry. Transport failures and other HTTP
    # errors may follow an executed action and must not trigger duplicate work.
    [ "$status" = 401 ] || return 1
    bridge_refresh_ha_token || return 1
    : > "$XIAOAI_CONVERSATION_HEADERS"
    curl --connect-timeout 3 --max-time 90 \
        -H "Authorization: Bearer ${HA_TOKEN}" -H 'Content-Type: application/json' \
        -d "$1" -XPOST "$XIAOAI_CONVERSATION_URL" \
        > "$WORK/conversation.json" 2> "$WORK/conversation.err"
}
