"""Split supplied transcript punctuation without creating or correcting text.

This is stateless text processing, not a decision that an interim is authoritative.
The caller owns buffering, transcript revisions and any final-tail policy.
"""
import re


_ENDINGS = frozenset('.!?؟。！？｡।॥۔')
_CLOSERS = frozenset('"\')]}»”’﴾」』】》〉〕）］｝')
_ENGLISH_TOKEN = re.compile(r'([A-Za-z]+(?:\.[A-Za-z]+)*)$')
_INITIALISM = re.compile(r'(?:[A-Za-z]\.)+[A-Za-z]$')
_ABBREVIATIONS = frozenset({
    'mr', 'mrs', 'ms', 'dr', 'prof', 'rev', 'fr', 'sr', 'jr', 'st',
    'vs', 'etc', 'e.g', 'i.e', 'a.m', 'p.m', 'ph.d', 'approx', 'dept',
    'fig', 'vol', 'inc', 'ltd',
})


def _period_ends(text: str, index: int) -> bool:
    before = text[index - 1] if index else ''
    after = text[index + 1] if index + 1 < len(text) else ''
    # isdecimal includes Arabic-Indic and extended Arabic-Indic digits.
    if before.isdecimal() and after.isdecimal():
        return False
    # Internal dots in ASCII domains, emails, versions and dotted abbreviations.
    # This deliberately treats "word.Word" as ambiguous instead of guessing.
    if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
        return False
    match = _ENGLISH_TOKEN.search(text, 0, index)
    if match:
        token = match.group(1)
        if token.lower() in _ABBREVIATIONS or _INITIALISM.fullmatch(token):
            return False
        if len(token) == 1 and token.isupper():
            return False
    return True


def split_complete_sentences(text: str) -> tuple[list[str], str]:
    """Return punctuation-complete sentences and the remaining trimmed tail.

    Outer whitespace is trimmed; all interior text, punctuation runs and adjacent
    closing quotes/brackets are retained. Commas, colons and semicolons do not
    end sentences. A candidate containing no letters/numbers stays in the tail
    rather than becoming a standalone punctuation translation.

    Conservative abbreviation/initial heuristics can retain a genuinely complete
    sentence ending in e.g. "etc." or "U.S.". This helper cannot resolve that
    ambiguity, infer omitted punctuation, or anticipate future stream revisions.
    """
    if not isinstance(text, str):
        raise TypeError('text must be a string')
    sentences = []
    start = index = 0
    while index < len(text):
        char = text[index]
        if char not in _ENDINGS or (char == '.' and not _period_ends(text, index)):
            index += 1
            continue
        end = index + 1
        # Preserve adjacent runs such as ?! and nested closing quotes/brackets.
        while end < len(text) and text[end] in _ENDINGS | _CLOSERS:
            end += 1
        candidate = text[start:end].strip()
        if any(char.isalnum() for char in candidate):
            sentences.append(candidate)
            start = end
        index = end
    return sentences, text[start:].strip()
