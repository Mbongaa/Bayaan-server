#!/bin/bash
# usage: batch.sh ARM "CODES" [VOCAB] -- runs all tuning clips for one arm in parallel
S="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$S/../../.venv/Scripts/python.exe}"
ARM=$1; CODES=$2; VOCAB=$3; CLIPS=${CLIPS:-"khutbaA khutbaB kuduzovic pezic golos kadri"}
mkdir -p "$S/runs/$ARM" "$S/logs"
cd "$S"
for c in $CLIPS; do
  extra="$FLAGS"; [ -n "$VOCAB" ] && extra="$extra --vocab $VOCAB"
  "$PY" stt_run.py "$S/audio/$c.wav" "$S/runs/$ARM/$c.json" --codes "$CODES" $extra > "$S/logs/$ARM-$c.log" 2>&1 &
done
wait
echo "$ARM done"; grep -h "finals" "$S"/logs/$ARM-*.log
