#!/usr/bin/env bash
set -euo pipefail

# Bootstrap helper for agent startup context.
# Usage:
#   scripts/bootstrap_ai_memory.sh "task context text"
#
# Configure one of:
# - AI_MEMORY_QUERY_CMD='your-cli --query %QUERY%'
#   (%QUERY% placeholder is replaced with the provided query text)
# - AI_MEMORY_QUERY_CMD='your-cli --query'
#   (query text is appended as the final argument)

QUERY="${1:-trading-bot startup context}"
CMD="${AI_MEMORY_QUERY_CMD:-}"
DISABLE_FALLBACK="${AI_MEMORY_DISABLE_FALLBACK:-0}"
FALLBACK_RESOLVER="${AI_MEMORY_FALLBACK_RESOLVER:-../ai-memory/scripts/context_resolver.py}"

if [[ -z "${CMD}" ]]; then
  if [[ "${DISABLE_FALLBACK}" != "1" && -f "${FALLBACK_RESOLVER}" ]]; then
    # Convenient default for local sibling setup:
    # trading-bot/ and ai-memory/ live one level under the same parent.
    exec python3 "${FALLBACK_RESOLVER}" "${QUERY}" --mode always --top-k 8
  fi

  echo "AI-MEMORY NOT AVAILABLE AND I'M NOT ABLE TO USE IT ATM"
  exit 2
fi

if [[ "${CMD}" == *"%QUERY%"* ]]; then
  SAFE_QUERY="${QUERY//\"/\\\"}"
  EXPANDED="${CMD//%QUERY%/${SAFE_QUERY}}"
  eval "${EXPANDED}"
else
  eval "${CMD} \"${QUERY}\""
fi
