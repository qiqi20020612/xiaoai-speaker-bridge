#!/bin/sh
# Observe the original Agent's real HTTP status without changing its JSON or
# stdout contract. Non-conversation curl calls are passed through unchanged.
for argument in "$@";do
    if [ "$argument" = "$XIAOAI_CONVERSATION_URL" ];then
        exec "$XIAOAI_CONVERSATION_CURL" --dump-header "$XIAOAI_CONVERSATION_HEADERS" "$@"
    fi
done
exec "$XIAOAI_CONVERSATION_CURL" "$@"
