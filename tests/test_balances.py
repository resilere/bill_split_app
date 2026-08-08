"""Tests for the pure balance math (compute_balances / compute_settlements)."""
from datetime import date

from app import compute_balances, compute_settlements

EARLY = date(2000, 1, 1)  # founding-member sentinel


def _user(uid, joined_at=EARLY):
    return {'id': uid, 'name': uid.upper(), 'joined_at': joined_at}


def net_by_id(result):
    return {u['id']: u['net'] for u in result['users']}


def share_by_id(result):
    return {u['id']: u['shared_share'] for u in result['users']}


def test_basic_shared_personal_and_excluded():
    users = [_user('a'), _user('b')]
    receipts = [{'id': 1, 'payer_id': 'a', 'receipt_date': date(2025, 1, 1)}]
    items = [
        {'price': 10.0, 'assigned_to': 'shared',   'receipt_id': 1},
        {'price': 4.0,  'assigned_to': 'a',        'receipt_id': 1},
        {'price': 6.0,  'assigned_to': 'b',        'receipt_id': 1},
        {'price': 2.0,  'assigned_to': 'excluded', 'receipt_id': 1},  # ignored
    ]
    result = compute_balances(users, receipts, items)

    assert result['shared_total'] == 10.0
    nets = net_by_id(result)
    # a paid 20 (10 shared + 4 + 6), owes 5+4=9  -> +11
    # b paid 0,  owes 5+6=11                      -> -11
    assert nets == {'a': 11.0, 'b': -11.0}
    assert result['settlements'] == [
        {'from': 'b', 'from_name': 'B', 'to': 'a', 'to_name': 'A', 'amount': 11.0}
    ]


def test_excluded_item_not_credited_to_payer():
    users = [_user('a'), _user('b')]
    receipts = [{'id': 1, 'payer_id': 'a', 'receipt_date': date(2025, 1, 1)}]
    items = [
        {'price': 8.0, 'assigned_to': 'shared',   'receipt_id': 1},
        {'price': 5.0, 'assigned_to': 'excluded', 'receipt_id': 1},
    ]
    result = compute_balances(users, receipts, items)
    paid = {u['id']: u['paid_total'] for u in result['users']}
    assert paid['a'] == 8.0  # not 13 — excluded item doesn't count


def test_join_date_excludes_member_from_older_receipts():
    # C joined mid-2025; a receipt from before then must not be split with C.
    users = [_user('a'), _user('b'), _user('c', joined_at=date(2025, 6, 1))]
    receipts = [
        {'id': 1, 'payer_id': 'a', 'receipt_date': date(2025, 1, 1)},  # before C
        {'id': 2, 'payer_id': 'a', 'receipt_date': date(2025, 7, 1)},  # after C
    ]
    items = [
        {'price': 9.0, 'assigned_to': 'shared', 'receipt_id': 1},  # 2-way -> 4.5
        {'price': 9.0, 'assigned_to': 'shared', 'receipt_id': 2},  # 3-way -> 3.0
    ]
    result = compute_balances(users, receipts, items)
    shares = share_by_id(result)
    assert shares['c'] == 3.0            # only the post-join receipt
    assert shares['a'] == 4.5 + 3.0      # both receipts
    assert shares['b'] == 4.5 + 3.0
    assert result['shared_total'] == 18.0


def test_settlement_kept_out_of_personal_and_paid():
    # A real €10 shared bill (Eser paid), then David settles €5 back to Eser.
    users = [_user('eser'), _user('david')]
    receipts = [
        {'id': 1, 'payer_id': 'eser',  'receipt_date': date(2026, 8, 1)},
        {'id': 2, 'payer_id': 'david', 'receipt_date': date(2026, 8, 2), 'is_settlement': True},
    ]
    items = [
        {'price': 10.0, 'assigned_to': 'shared', 'receipt_id': 1},
        {'price': 5.0,  'assigned_to': 'eser',   'receipt_id': 2},  # payback to Eser
    ]
    result = compute_balances(users, receipts, items)
    by = {u['id']: u for u in result['users']}

    # The €5 payback must NOT show up as Eser's personal item or David's bill paid.
    assert by['eser']['personal_total'] == 0.0
    assert by['eser']['paid_total'] == 10.0
    assert by['eser']['settlement'] == -5.0   # received 5
    assert by['david']['paid_total'] == 0.0
    assert by['david']['settlement'] == 5.0   # paid 5
    # Net is unchanged by the reclassification: both settled to 0.
    assert by['eser']['net'] == 0.0
    assert by['david']['net'] == 0.0


def test_settlements_zero_out_the_balances():
    users = [_user('a'), _user('b')]
    receipts = [{'id': 1, 'payer_id': 'a', 'receipt_date': date(2025, 1, 1)}]
    items = [{'price': 10.0, 'assigned_to': 'shared', 'receipt_id': 1}]
    result = compute_balances(users, receipts, items)
    # b owes a 5.00
    assert result['settlements'] == [
        {'from': 'b', 'from_name': 'B', 'to': 'a', 'to_name': 'A', 'amount': 5.0}
    ]


def test_empty_group_returns_no_settlements():
    result = compute_balances([], [], [])
    assert result == {'users': [], 'shared_total': 0, 'settlements': []}


def test_compute_settlements_multi_party():
    # One creditor (+10), two debtors (-4, -6): each debtor pays the creditor.
    balances = [
        {'id': 'a', 'name': 'A', 'net': 10.0},
        {'id': 'b', 'name': 'B', 'net': -4.0},
        {'id': 'c', 'name': 'C', 'net': -6.0},
    ]
    settlements = compute_settlements(balances)
    pairs = {(s['from'], s['to'], s['amount']) for s in settlements}
    assert pairs == {('b', 'a', 4.0), ('c', 'a', 6.0)}


def test_compute_settlements_ignores_settled_balances():
    balances = [
        {'id': 'a', 'name': 'A', 'net': 0.0},
        {'id': 'b', 'name': 'B', 'net': 0.0},
    ]
    assert compute_settlements(balances) == []
