"""Where do deletions (words in the uncut reference, missing live) sit relative to live cut seams?"""
import sys
from score import load, norm_words, align
arms = sys.argv[1].split(','); clips = sys.argv[2].split(','); show = int(sys.argv[3]) if len(sys.argv) > 3 else 0
for arm in arms:
    tot = dict(dels=0, seam=0, inner=0, words=0, seams=0, end_drop=0, start_drop=0)
    examples = []
    for clip in clips:
        d, r = load(arm, clip), load('F-bs', clip)
        if not d or not r: continue
        ref = norm_words(r['transcript']); hyp, starts, ends = [], set(), set()
        for f in d['finals']:
            w = norm_words(f['text'])
            if w: starts.add(len(hyp)); hyp.extend(w); ends.add(len(hyp) - 1)
        ops = align(ref, hyp)
        tot['words'] += len(ref); tot['seams'] += len(ends)
        # group consecutive deletions into runs
        k = 0
        while k < len(ops):
            if ops[k][0] != 'D': k += 1; continue
            run = []
            while k < len(ops) and ops[k][0] == 'D': run.append(ops[k]); k += 1
            j = run[0][2]  # hyp index before which the run is missing
            tot['dels'] += len(run)
            at_seam = j in starts or j == len(hyp)
            if at_seam:
                tot['seam'] += len(run)
                if len(examples) < show:
                    i = run[0][1]
                    examples.append(f'{clip}: …{" ".join(hyp[max(0,j-5):j])} ‖ {" ".join(hyp[j:j+4])}…   missing: [{" ".join(ref[i:i+len(run)])}]')
            else:
                tot['inner'] += len(run)
    print(f"{arm:12s} deleted words {tot['dels']:4d} | at a seam {tot['seam']:3d} ({100*tot['seam']/max(1,tot['dels']):.0f}%) | inside {tot['inner']:3d} | seams {tot['seams']} | ref words {tot['words']}")
    for e in examples: print('    ', e)
