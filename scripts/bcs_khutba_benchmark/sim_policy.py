"""Offline cut policy: after MIN s, cut at the first quiet run of QUIET ms; hard cut at MAX s.

Uses the same quiet test as the live pause snap (10 ms frame RMS below max(60, 0.2 x the
70th percentile of the last 3 s)). The cut lands at the end of the quiet run, as it would
live (audio is streamed as it arrives). Writes window stubs for sim_windows.py.

usage: python sim_policy.py CLIP OUT_ARM --min 8 --quiet-ms 300 --max 16
"""
import argparse, json, wave
from array import array
from collections import deque
from pathlib import Path
from score import RUNS

AUDIO = Path(__file__).resolve().parent.parent / 'audio'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('clip'); p.add_argument('out')
    p.add_argument('--min', type=float, default=8.0); p.add_argument('--quiet-ms', type=int, default=300)
    p.add_argument('--max', type=float, default=16.0)
    a = p.parse_args()
    with wave.open(str(AUDIO / f'{a.clip}.wav'), 'rb') as w:
        samples = array('h', w.readframes(w.getnframes()))
    frames = len(samples) // 160
    levels, recent = [], deque(maxlen=300)
    for i in range(frames):
        chunk = samples[i * 160:(i + 1) * 160]
        levels.append((sum(s * s for s in chunk) / 160) ** 0.5)
    windows, start, quiet_run = [], 0, 0
    for i, level in enumerate(levels):
        recent.append(level)
        speech = sorted(recent)[int(len(recent) * 0.7)]
        quiet_run = quiet_run + 1 if level < max(60.0, 0.2 * speech) else 0
        length = (i + 1 - start) / 100
        reason = None
        if length >= a.max:
            reason = 'duration_limit'
        elif length >= a.min and quiet_run * 10 >= a.quiet_ms:
            reason = 'pause'
        if reason:
            windows.append(dict(start=start / 100, end=(i + 1) / 100, reason=reason, text=''))
            start, quiet_run = i + 1, 0
    if start < frames:
        windows.append(dict(start=start / 100, end=frames / 100, reason='eof', text=''))
    out = RUNS / a.out
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{a.clip}.json').write_text(json.dumps(dict(finals=windows, transcript=''), indent=1), encoding='utf-8')
    forced = sum(1 for x in windows if x['reason'] == 'duration_limit')
    lengths = sorted(x['end'] - x['start'] for x in windows)
    print(a.out, a.clip, len(windows), 'windows', 'forced', forced, 'median len', round(lengths[len(lengths) // 2], 1))


if __name__ == '__main__':
    main()
