#!/usr/bin/env bash
# Runs the 4 demos in order (this is also the video script). Stops at the first failure.
# Responsible for: ordering and the final summary only. NOT responsible for: any scenario logic.
# Serves all criteria.
source "$(dirname "$0")/lib.sh"

for demo in 1_provision 2_failover 3_bad_config 4_reclaim; do
    echo; echo "${BOLD}════════ $demo ════════${RESET}"
    bash "scripts/$demo.sh"
done
echo; echo "${BOLD}${GREEN}All 4 demos passed.${RESET}"
