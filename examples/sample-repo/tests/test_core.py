import pytest
from textstats import average_word_length, word_count
from textstats.core import words


def test_words_ignores_punctuation() -> None:
    assert words("Hello, world! It's fine.") == ["Hello", "world", "It's", "fine"]


def test_word_count() -> None:
    assert word_count("one two  three") == 3


def test_word_count_empty() -> None:
    assert word_count("") == 0
    assert word_count("  ...  ") == 0


def test_average_word_length() -> None:
    assert average_word_length("ab abcd") == pytest.approx(3.0)


def test_average_word_length_empty() -> None:
    assert average_word_length("") == 0.0
