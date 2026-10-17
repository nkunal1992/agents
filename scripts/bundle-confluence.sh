#!/usr/bin/env bash
# Back-compat wrapper — the real logic is bundle-service.sh.
exec "$(dirname "${BASH_SOURCE[0]}")/bundle-service.sh" confluence "$@"
