#!/bin/sh
# Verifica que falha no meio de deploy_body (subshell com set -e) aborta o restante.
set -eu

step() { printf '%s' "$1"; }

run_subshell() {
  (
    set -e
    step ok
    false
    step never
  )
}

out=$(run_subshell 2>&1) || rc=$?
rc=${rc:-0}
printf '%s' "$out" | grep -q ok || exit 1
printf '%s' "$out" | grep -q never && exit 1
[ "$rc" -ne 0 ] || exit 1
exit 0