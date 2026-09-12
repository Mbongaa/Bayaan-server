"""Unicode word accounting for the confirmed sentence buffer."""
import unicodedata

def word_count(text):
    """Count letter/number words without counting Arabic vowel marks separately."""
    count, in_word = 0, False
    for index, char in enumerate(text):
        category = unicodedata.category(char)[0]
        previous = text[index - 1] if index else ''
        following = text[index + 1] if index + 1 < len(text) else ''
        if category in ('L', 'N'):
            if not in_word:
                count += 1
            in_word = True
        elif category == 'M' or char in "'’":
            continue
        elif char in '.,\u066b\u066c' and previous.isdigit() and following.isdigit():
            continue  # Decimal/thousands separators do not create extra words.
        elif char == '.' and previous.isascii() and previous.isalpha() and following.isascii() and following.isalpha():
            continue  # Dotted abbreviations/domains count as a single word.
        elif char in '-‐‑' and previous.isalnum() and following.isalnum():
            continue
        else:
            in_word = False
    return count
