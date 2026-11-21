"""Replay Bosnian sentence units through the production translation logic, in order.

Mirrors translator.py: system prompt (DEFAULT_PROMPT formatted for the source/target,
plus the source-language note), the last 12 (source, own translation) pairs, then the
new unit; model/settings from config.translation. Production now adds the Bosnian/Croatian/Serbian
source notes itself; --notes appends extra notes for a new variant.

usage: python tx_run.py UNITS.json OUT.jsonl --target Dutch [--notes FILE] [--limit N]
"""
import argparse, asyncio, json, os, sys, time
from collections import deque
from pathlib import Path

BAYAAN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BAYAAN))
os.environ.setdefault('SUPABASE_URL', 'http://unused.invalid'); os.environ.setdefault('SUPABASE_SERVICE_ROLE_KEY', 'unused')
os.environ.setdefault('LIVEKIT_URL', 'ws://unused'); os.environ.setdefault('LIVEKIT_API_KEY', 'x'); os.environ.setdefault('LIVEKIT_API_SECRET', 'x')

from config import get_config  # noqa: E402
import prompt_builder as pb  # noqa: E402
from translator import Translator  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402


def build_system(source, target, notes=None):
    prompt = pb.PromptBuilder.DEFAULT_PROMPT.format(
        source_lang=source, target_lang=target, language_notes=pb.PromptBuilder.language_notes(target))
    if notes:
        prompt = prompt.rstrip() + '\n\n' + notes.strip()
    return Translator._with_source_language_note(prompt, source, target)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('units'); p.add_argument('out')
    p.add_argument('--target', default='Dutch'); p.add_argument('--source', default='Bosnian')
    p.add_argument('--notes'); p.add_argument('--limit', type=int)
    p.add_argument('--key-file', required=True)
    a = p.parse_args()
    key = next(l.split('=', 1)[1].strip().strip('"') for l in Path(a.key_file).read_text(encoding='utf-8-sig').splitlines()
               if l.startswith('OPENAI_API_KEY='))
    cfg = get_config().translation
    client = AsyncOpenAI(api_key=key)
    system = build_system(a.source, a.target, Path(a.notes).read_text(encoding='utf-8') if a.notes else None)
    units = json.loads(Path(a.units).read_text(encoding='utf-8'))
    units = units['units'] if isinstance(units, dict) else units
    units = units[:a.limit] if a.limit else units
    history = deque(maxlen=cfg.max_context_pairs * 2)
    usage = dict(prompt=0, cached=0, completion=0)
    with open(a.out, 'w', encoding='utf-8') as out:
        for i, u in enumerate(units):
            text = u['text'] if isinstance(u, dict) else u
            messages = [dict(role='system', content=system), *history, dict(role='user', content=text)]
            t = time.monotonic()
            r = await client.chat.completions.create(model=cfg.model, messages=messages, **cfg.llm_settings(cfg.model))
            result = r.choices[0].message.content or ''
            u_ = r.usage
            usage['prompt'] += u_.prompt_tokens; usage['completion'] += u_.completion_tokens
            usage['cached'] += getattr(getattr(u_, 'prompt_tokens_details', None), 'cached_tokens', 0) or 0
            history.extend([dict(role='user', content=text), dict(role='assistant', content=result)])
            out.write(json.dumps(dict(i=i, src=text, tx=result, secs=round(time.monotonic() - t, 2)), ensure_ascii=False) + '\n')
    cost = ((usage['prompt'] - usage['cached']) * 0.20 + usage['cached'] * 0.02 + usage['completion'] * 1.00) / 1e6
    print(a.out, len(units), 'units', usage, f'~${cost:.3f}')


if __name__ == '__main__':
    asyncio.run(main())
