"""Tests for OCR text parsing and item-description normalization."""
from app import parse_bill_text, _match_key


def parsed(text):
    """Return [(description, price), ...] for a block of OCR text."""
    return [(i['description'], i['price']) for i in parse_bill_text(text)]


def test_simple_items_comma_and_period_decimals():
    items = parsed("Cola 1,99\nChicken Breast 12.99")
    assert items == [('Cola', 1.99), ('Chicken Breast', 12.99)]


def test_stops_at_total_line():
    # Parsing should stop once a totals line ('summe'/'total') is reached.
    text = "Milk 1,50\nBread 2,00\nSumme 3,50\nCash 5,00"
    assert parsed(text) == [('Milk', 1.50), ('Bread', 2.00)]


def test_english_total_also_stops():
    text = "Apple 0,80\nTotal 0,80"
    assert parsed(text) == [('Apple', 0.80)]


def test_filter_keywords_are_skipped():
    # Lines with tax/discount/payment keywords are not items.
    text = "Beer 2,50\nMwSt 19% 0,40\nRabatt -0,50\nWater 1,00"
    assert parsed(text) == [('Beer', 2.50), ('Water', 1.00)]


def test_per_kg_unit_price_line_filtered():
    # Weight-priced items print an extra unit-price line that isn't an item.
    text = "Bananen 1,20\n0,636kg x 1,89 €/kg"
    assert parsed(text) == [('Bananen', 1.20)]


def test_thousands_separator():
    assert parsed("Laptop 1.234,56") == [('Laptop', 1234.56)]


def test_negative_price_kept():
    # Pfand/deposit returns can be negative.
    assert parsed("Pfand -0,25") == [('Pfand', -0.25)]


def test_blank_and_unmatched_lines_ignored():
    text = "\nRandom header text\nJuice 3,49\n   \n"
    assert parsed(text) == [('Juice', 3.49)]


def test_match_key_normalizes_punctuation_and_case():
    assert _match_key('Herz.Soft-EisSch.') == 'herzsofteissch'
    assert _match_key('HERZ SOFT EISSCH') == 'herzsofteissch'
    assert _match_key('Herz.Soft-EisSch.') == _match_key('herz soft eissch')


def test_match_key_empty_for_no_alphanumerics():
    assert _match_key('  --- ') == ''
    assert _match_key(None) == ''
