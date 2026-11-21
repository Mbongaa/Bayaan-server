"""Print the passages where two arms disagree, for manual judging (randomised A/B order).

usage: python pairdiff.py ARM_X ARM_Y CLIP [--n 25] [--seed 1] [--key OUT.json]
"""
import json, random, sys
from pathlib import Path
from score import RUNS, align, load


def words(d):
    if d.get('finals') is not None:
        return ' '.join(f['text'].strip() for f in d['finals'] if f['text'].strip()).split()
    return d['transcript'].split()


def main():
    x, y, clip = sys.argv[1:4]
    n = int(next((a.split('=')[1] for a in sys.argv if a.startswith('--n=')), 25))
    seed = int(next((a.split('=')[1] for a in sys.argv if a.startswith('--seed=')), 1))
    keyfile = next((a.split('=')[1] for a in sys.argv if a.startswith('--key=')), None)
    wx, wy = words(load(x, clip)), words(load(y, clip))
    lx = [w.lower().strip('.,!?;:"\'«»„“”') for w in wx]
    ly = [w.lower().strip('.,!?;:"\'«»„“”') for w in wy]
    ops = align(lx, ly)
    regions, cur = [], None
    for o in ops:
        if o[0] == '=':
            if cur: regions.append(cur); cur = None
            continue
        cur = cur or dict(xi=[], yj=[])
        if o[0] in 'SD': cur['xi'].append(o[1])
        if o[0] in 'SI': cur['yj'].append(o[2])
    if cur: regions.append(cur)
    rng = random.Random(seed)
    picked = sorted(rng.sample(range(len(regions)), min(n, len(regions))))
    key = {}
    print(f'{clip}: {len(regions)} differing passages, showing {len(picked)}')
    for k in picked:
        r = regions[k]
        xi = r['xi'] or [None]; yj = r['yj'] or [None]
        # context from X side
        anchor = r['xi'][0] if r['xi'] else next((o[1] for o in ops if o[0] == 'I' and o[2] == r['yj'][0]), 0)
        left = ' '.join(wx[max(0, anchor - 6):anchor])
        endx = (r['xi'][-1] + 1) if r['xi'] else anchor
        right = ' '.join(wx[endx:endx + 6])
        tx = ' '.join(wx[i] for i in r['xi']) or '∅'
        ty = ' '.join(wy[j] for j in r['yj']) or '∅'
        flip = rng.random() < 0.5
        a, b = (ty, tx) if flip else (tx, ty)
        key[str(k)] = 'A=' + (y if flip else x)
        print(f'#{k}: …{left} [A: {a} | B: {b}] {right}…')
    if keyfile:
        Path(keyfile).write_text(json.dumps(key), encoding='utf-8')


if __name__ == '__main__':
    main()
