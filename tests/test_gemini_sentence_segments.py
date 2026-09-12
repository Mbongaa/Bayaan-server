from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_stt.sentences import split_complete_sentences


class SentenceSegmentsTests(unittest.TestCase):
    def test_arabic_sentences_and_unfinished_tail(self):
        self.assertEqual(split_complete_sentences('  الحمد لله. كيف حالكم؟ بخير! والكلام مستمر  '),
                         (['الحمد لله.', 'كيف حالكم؟', 'بخير!'], 'والكلام مستمر'))

    def test_no_clause_or_unpunctuated_cuts(self):
        text = 'قال: هذا جيد، ولكنه طويل؛ and this, too; has no ending: ' + 'كلام ' * 1500
        self.assertEqual(split_complete_sentences(text), ([], text.strip()))

    def test_preserves_interior_whitespace_and_closing_marks(self):
        text = '  قال: «هذا  جيد؟!»  [He said:\n“Really?!”] Remaining  '
        self.assertEqual(split_complete_sentences(text),
                         (['قال: «هذا  جيد؟!»', '[He said:\n“Really?!”]'], 'Remaining'))

    def test_decimal_scripts_and_domain_names(self):
        cases = [
            ('It costs 3.14 dollars. Next', ['It costs 3.14 dollars.'], 'Next'),
            ('السعر ٣.١٤ دينار. والعدد ۴.۵!', ['السعر ٣.١٤ دينار.', 'والعدد ۴.۵!'], ''),
            ('Visit example.com or first.last@example.org. Next.',
             ['Visit example.com or first.last@example.org.', 'Next.'], ''),
            ('Version 1.2.3 uses 127.0.0.1.', ['Version 1.2.3 uses 127.0.0.1.'], ''),
            ('Visit example.com', [], 'Visit example.com'),
        ]
        for text, sentences, tail in cases:
            with self.subTest(text=text):
                self.assertEqual(split_complete_sentences(text), (sentences, tail))

    def test_abbreviations_initials_and_initialisms(self):
        text = 'Dr. A. B. Smith met Prof. Jones in the U.S. office, e.g. yesterday. Next'
        self.assertEqual(split_complete_sentences(text),
                         (['Dr. A. B. Smith met Prof. Jones in the U.S. office, e.g. yesterday.'], 'Next'))
        for text in ['Ask Dr.', 'He lives in the U.S.', 'Bring paper, pens, etc.']:
            with self.subTest(text=text):
                self.assertEqual(split_complete_sentences(text), ([], text))

    def test_punctuation_runs_and_repeated_sentences(self):
        self.assertEqual(split_complete_sentences('Really?! Yes... Yes... Next'),
                         (['Really?!', 'Yes...', 'Yes...'], 'Next'))

    def test_cjk_indic_and_urdu(self):
        self.assertEqual(split_complete_sentences('你好。元気ですか？はい！ ठीक है। अगला॥ یہ ٹھیک ہے۔ باقی'),
                         (['你好。', '元気ですか？', 'はい！', 'ठीक है।', 'अगला॥', 'یہ ٹھیک ہے۔'], 'باقی'))

    def test_standalone_punctuation_never_becomes_sentence(self):
        for text in ['.', '؟!', '...”', '(?!)', '... ! ؟']:
            with self.subTest(text=text):
                self.assertEqual(split_complete_sentences(text), ([], text))
        self.assertEqual(split_complete_sentences('Done. ؟'), (['Done.'], '؟'))

    def test_buffered_tail_can_be_completed_by_later_punctuation(self):
        ready, tail = split_complete_sentences('Finished. لم يكتمل الكلام')
        self.assertEqual(ready, ['Finished.'])
        self.assertEqual(split_complete_sentences(tail + '؟'), (['لم يكتمل الكلام؟'], ''))

    def test_empty_and_invalid_input(self):
        self.assertEqual(split_complete_sentences(' \n\t'), ([], ''))
        with self.assertRaises(TypeError):
            split_complete_sentences(None)


if __name__ == '__main__':
    unittest.main()
