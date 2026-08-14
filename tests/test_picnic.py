"""Tests for the Picnic digital-PDF parser (coordinate-based).

Uses a synthetic word-list that reproduces Picnic's grid with fake data — the
real receipt contains personal info and must not be committed.
"""
import os

import pytest

from app import parse_picnic_words, parse_picnic_pdf, DIGITAL_PDF_PARSERS


def w(text, x0, top, size):
    return {'text': text, 'x0': x0, 'top': top, 'size': size}


def _synthetic_page():
    """One page: a normal item, a discounted item, a wrapped-name item, and a
    Pfand/Zwischensumme totals section that must be excluded."""
    words = []

    # 1) Normal item "Apple Juice" = €3.84 (euros size 14, cents superscript size 8)
    words += [w('Apple', 238, 100, 8), w('Juice', 270, 100, 8),
              w('3', 390, 100, 14), w('84', 398, 104, 8),
              w('1', 193, 104, 8), w('.', 398, 110, 8), w('1L', 238, 110, 6)]

    # 2) Discounted item "Discount Cola": 2.00 struck -> 1.50 (the red price below)
    words += [w('Discount', 238, 150, 8), w('Cola', 285, 150, 8),
              w('2', 390, 150, 14), w('00', 398, 154, 8),           # original
              w('1', 390, 174, 14), w('50', 398, 178, 8),           # discounted (final)
              w('10%', 242, 180, 6), w('Rabatt', 262, 180, 6),
              w('1', 193, 154, 8)]

    # 3) Wrapped two-line name "Organic Whole / Grain Bread" = €2.49
    words += [w('Organic', 238, 200, 8), w('Whole', 275, 200, 8),
              w('Grain', 238, 213, 8), w('Bread', 268, 213, 8),
              w('2', 390, 210, 14), w('49', 398, 214, 8),
              w('1', 193, 214, 8), w('500g', 238, 220, 6)]

    # 4) Totals/deposits — everything here must be excluded
    words += [w('Pfand', 210, 250, 8), w('0', 390, 250, 14), w('39', 398, 254, 8),
              w('Zwischensumme', 262, 270, 8), w('6', 390, 270, 14), w('33', 398, 274, 8)]
    return words


def test_parses_normal_discount_and_wrapped_items():
    items = parse_picnic_words([_synthetic_page()])
    got = [(i['description'], i['price']) for i in items]
    assert got == [
        ('Apple Juice', 3.84),
        ('Discount Cola', 1.50),          # discounted price, not the 2.00 original
        ('Organic Whole Grain Bread', 2.49),
    ]


def test_totals_and_deposits_excluded():
    items = parse_picnic_words([_synthetic_page()])
    joined = ' '.join(i['description'].lower() for i in items)
    assert 'zwischensumme' not in joined and 'pfand' not in joined
    assert all(any(ch.isalpha() for ch in i['description']) for i in items)


def test_sum_of_items():
    items = parse_picnic_words([_synthetic_page()])
    assert round(sum(i['price'] for i in items), 2) == 7.83


def test_detector_matches_picnic_text():
    detect = DIGITAL_PDF_PARSERS[0][0]
    assert detect('… Dein Bon … Picnic GmbH …') is True
    assert detect('EDEKA Filiale 1234 Bergmannstraße') is False


def test_multiple_pages_concatenate():
    items = parse_picnic_words([_synthetic_page(), _synthetic_page()])
    assert len(items) == 6


# Optional end-to-end smoke test against the real receipt, kept OUT of git.
# Drop the file at tests/fixtures/picnic_sample.pdf to run it locally.
_FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'picnic_sample.pdf')


@pytest.mark.skipif(not os.path.exists(_FIXTURE), reason="real Picnic fixture not present")
def test_real_pdf_smoke():
    import pdfplumber
    with pdfplumber.open(_FIXTURE) as pdf:
        items = parse_picnic_pdf(pdf)
    assert len(items) >= 25
    assert all(i['price'] > 0 for i in items)
