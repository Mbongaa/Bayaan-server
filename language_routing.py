"""Provider selection is metadata only; all languages share one LiveKit project."""
from dataclasses import dataclass
import json
from pathlib import Path
import re


GEMINI_LANGUAGES = json.loads(Path(__file__).with_name('transcription_languages.json').read_text(encoding='utf-8'))
LEGACY_SPEECHMATICS_LANGUAGES = frozenset({'ar', 'ba', 'eu', 'ga', 'cy', 'ta', 'ug', 'eo', 'ia', 'ur'})
_GEMINI_BY_CODE = {}
for _language in GEMINI_LANGUAGES:
    _GEMINI_BY_CODE[_language['code'].lower()] = _language
    _GEMINI_BY_CODE[_language['hint'].lower()] = _language
# Persisted selections used before the full locale catalog existed.
for _alias, _target in {'zh': 'cmn', 'zh-cn': 'cmn', 'fil': 'tl', 'nb': 'no'}.items():
    if _target in _GEMINI_BY_CODE:
        _GEMINI_BY_CODE[_alias] = _GEMINI_BY_CODE[_target]


@dataclass(frozen=True)
class TranscriptionRoute:
    provider: str
    code: str
    hint: str
    source_name: str


def transcription_route(code: str) -> TranscriptionRoute:
    if not isinstance(code, str):
        raise ValueError('A transcription language is required')
    if code in LEGACY_SPEECHMATICS_LANGUAGES:
        return TranscriptionRoute('speechmatics', code, code, 'Arabic' if code == 'ar' else code)
    entry = _GEMINI_BY_CODE.get(code.lower())
    if entry:
        return TranscriptionRoute('gemini', code, entry['hint'],
                                  'Arabic' if entry['code'] == 'ar-gemini' else entry['name'])
    raise ValueError(f'Unsupported transcription language: {code}')


def source_language_names() -> dict[str, dict[str, str]]:
    return {code: {'name': 'Arabic' if entry['code'] == 'ar-gemini' else entry['name'], 'flag': ''}
            for code, entry in _GEMINI_BY_CODE.items()}


@dataclass(frozen=True)
class TranslationLanguage:
    value: str
    name: str


def translation_language(code: str, established=None) -> TranslationLanguage:
    """Accept recognized language/script/region tags, never raw prompt instructions.

    This matches the classroom picker: lowercase wire tags, at most 16 characters.
    Existing production names are retained so its GPT templates are unchanged.
    """
    if not isinstance(code, str) or len(code) > 16 or not re.fullmatch(
            r'[a-zA-Z]{2,3}(?:-[a-zA-Z]{4})?(?:-(?:[a-zA-Z]{2}|[0-9]{3}))?', code):
        raise ValueError('Invalid translation language code')
    normalized = code.lower()
    if normalized in {'und', 'mul', 'zxx', 'mis', 'off'}:
        raise ValueError('Select a specific translation language')
    if established and normalized in established:
        return TranslationLanguage(normalized, established[normalized]['name'])
    import langcodes
    language = langcodes.Language.get(normalized)
    if not language.is_valid() or language.language in {'und', 'mul', 'zxx', 'mis'}:
        raise ValueError('Unknown translation language')
    if language.script == 'Zzzz' or language.territory == 'ZZ':
        raise ValueError('Unknown translation script or region')
    name = language.display_name('en')
    if name.startswith('Unknown language'):
        raise ValueError('Unknown translation language')
    return TranslationLanguage(normalized, name)
