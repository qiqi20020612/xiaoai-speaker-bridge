#!/bin/sh
# Synthetic reviewed launcher shape. Never source the entire launcher in tests.
ha_refresh_token() {
  log "refreshing token"

  SPEAKER_IP=$(ip addr show wlan0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
  curl -s -f -d "grant_type=refresh_token&client_id=http://${SPEAKER_IP}&refresh_token=${HA_REFRESH_TOKEN}" \
       -o /tmp/ha_token.json ${HA_URL}/auth/token

  if [ "$?" -gt 0 ]; then
    # Sentinel: production extraction must replace the whole original fatal path.
    printf fatal > "$STATE_DIR/fatal-refresh"
    exit 1
  fi

  HA_TOKEN=$(jq -r .access_token /tmp/ha_token.json)
  sed -i "s/^HA_TOKEN=.*/HA_TOKEN=${HA_TOKEN}/" ${CONFIG_FILE}
}
printf 'Do not execute a launcher to load its functions' >&2
exit 99
