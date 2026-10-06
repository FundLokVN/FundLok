import pytest

from app.gverify.normalize import normalize_identifier, to_ascii_digits


def test_to_ascii_digits_standard():
    assert to_ascii_digits("1234567890") == "1234567890"


def test_to_ascii_digits_fullwidth():
    # Fullwidth digits ０１２３４５６７８９
    fullwidth = "０１２３４５６７８９"
    assert to_ascii_digits(fullwidth) == "0123456789"


def test_to_ascii_digits_arabic_indic():
    # Arabic-Indic digits ٠١٢٣٤٥٦٧٨٩
    arabic_indic = "٠١٢٣٤٥٦٧٨٩"
    assert to_ascii_digits(arabic_indic) == "0123456789"


def test_normalize_identifier_removes_whitespace():
    assert normalize_identifier("  079 203 001 234  ") == "079203001234"
    assert normalize_identifier(" ０７９ ２０３ ００１ ２３４ ") == "079203001234"
