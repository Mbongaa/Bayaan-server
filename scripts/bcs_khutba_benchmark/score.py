"""Score live arms against a reference transcript (word alignment).

usage: python score.py REF_ARM ARM [ARM ...] [--clips a,b] [--show N]
Reports per clip: WER, and for live arms the error rate within 1 word of a live cut
versus inside segments. --show prints N error snippets per clip for manual review.
"""
import json, re, sys, unicodedata
from pathlib import Path

RUNS = Path(__file__).resolve().parent / 'runs'
CLIPS = ['khutbaA', 'khutbaB', 'kuduzovic', 'pezic', 'golos', 'kadri']
ARABIC = re.compile(r'[؀-ۿ]')
# Spelling variants that are not recognition errors.
EQUIV = {'džemate': 'džemaate', "džema'ate": 'džemaate', 'džematu': 'džemaatu', 'historija': 'istorija',
         'historiji': 'istoriji', 'alejhisselam': 'alejhiselam', 'alejhi-selam': 'alejhiselam',
         'kerim': 'kerim'}


def norm_words(text):
    text = unicodedata.normalize('NFC', text.lower()).replace("'", '').replace('’', '').replace('-', ' ')
    words = [w for w in re.findall(r"[\w]+", text) if not ARABIC.search(w)]
    return [EQUIV.get(w, w) for w in words]


def align(ref, hyp):
    n, m = len(ref), len(hyp)
    prev = list(range(m + 1))
    back = [bytearray(m + 1) for _ in range(n + 1)]  # 0 match/sub 1 del 2 ins
    for j in range(1, m + 1):
        back[0][j] = 2
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        back[i][0] = 1
        r = ref[i - 1]
        for j in range(1, m + 1):
            s = prev[j - 1] + (r != hyp[j - 1])
            d = prev[j] + 1
            ins = cur[j - 1] + 1
            if s <= d and s <= ins:
                cur[j] = s
            elif d <= ins:
                cur[j], back[i][j] = d, 1
            else:
                cur[j], back[i][j] = ins, 2
        prev = cur
    ops, i, j = [], n, m
    while i or j:
        b = back[i][j]
        if i and j and b == 0:
            ops.append(('=' if ref[i - 1] == hyp[j - 1] else 'S', i - 1, j - 1)); i -= 1; j -= 1
        elif i and b == 1:
            ops.append(('D', i - 1, j)); i -= 1
        else:
            ops.append(('I', i, j - 1)); j -= 1
    return ops[::-1]


def load(arm, clip):
    p = RUNS / arm / f'{clip}.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    show = int(next((a.split('=')[1] for a in sys.argv if a.startswith('--show=')), 0))
    clips = next((a.split('=')[1].split(',') for a in sys.argv if a.startswith('--clips=')), CLIPS)
    ref_arm, arms = args[0], args[1:]
    totals = {}
    for clip in clips:
        ref_d = load(ref_arm, clip)
        if not ref_d:
            continue
        ref = norm_words(ref_d['transcript'])
        line = [f'{clip:10s} ref={len(ref):5d}']
        for arm in arms:
            d = load(arm, clip)
            if not d:
                line.append(f'{arm}: -'); continue
            finals = d.get('finals')
            if finals is not None:
                hyp, cut_after = [], set()
                for f in finals:
                    w = norm_words(f['text'])
                    if w:
                        hyp.extend(w); cut_after.add(len(hyp) - 1)
            else:
                hyp, cut_after = norm_words(d['transcript']), set()
            ops = align(ref, hyp)
            err = [o for o in ops if o[0] != '=']
            near = lambda j: any(k in cut_after for k in (j - 1, j, j + 1)) or (j - 2) in cut_after and False
            near_err = sum(1 for o in err if near(o[2]))
            near_slots = sum(1 for o in ops if near(o[2]))
            t = totals.setdefault(arm, [0, 0, 0, 0, 0, 0])
            t[0] += len(err); t[1] += len(ref); t[2] += near_err; t[3] += near_slots
            t[4] += sum(1 for o in err if o[0] == 'D'); t[5] += len(cut_after)
            line.append(f'{arm}: WER {100 * len(err) / len(ref):5.1f}%'
                        + (f' cut-zone {100 * near_err / max(1, near_slots):4.1f}% (n={len(cut_after)})' if cut_after else ''))
            if show:
                print(f'--- {clip} {arm}')
                shown = 0
                for k, o in enumerate(ops):
                    if o[0] != '=' and shown < show:
                        lo, hi = max(0, o[1] - 4), o[1] + 5
                        hj = [x[2] for x in ops[max(0, k - 4):k + 5] if x[0] != 'D']
                        hs = ' '.join(hyp[min(hj):max(hj) + 1]) if hj else ''
                        print(f'  {o[0]} REF: {" ".join(ref[lo:hi])}\n    HYP: {hs}{"  [CUT]" if near(o[2]) else ""}')
                        shown += 1
        print(' | '.join(line))
    print('TOTAL')
    for arm, t in totals.items():
        print(f'  {arm:14s} WER {100 * t[0] / t[1]:5.2f}%  deletions {t[4]}  cut-zone err {100 * t[2] / max(1, t[3]):4.1f}%'
              f'  other err {100 * (t[0] - t[2]) / max(1, t[1] - t[3]):4.1f}%  cuts {t[5]}')


if __name__ == '__main__':
    main()
