#!/usr/bin/env bash
# Запуск с ограничением по времени: tmo.sh <секунд> команда... Работает и там, где нет timeout (macOS без coreutils).
s="$1"; shift
if command -v timeout >/dev/null 2>&1; then exec timeout "$s" "$@"; fi
if command -v gtimeout >/dev/null 2>&1; then exec gtimeout "$s" "$@"; fi
"$@" &
p=$!
( sleep "$s"; kill "$p" 2>/dev/null; sleep 2; kill -9 "$p" 2>/dev/null ) &
w=$!
wait "$p" 2>/dev/null
rc=$?
kill "$w" 2>/dev/null
wait "$w" 2>/dev/null
exit $rc
