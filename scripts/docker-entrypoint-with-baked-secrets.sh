#!/usr/bin/env sh
set -e

BAKED_SECRETS_FILE="/app/env/prod.runtime.secrets.env"

if [ -f "$BAKED_SECRETS_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*)
        continue
        ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    export "$key=$value"
  done < "$BAKED_SECRETS_FILE"
fi

exec "$@"
