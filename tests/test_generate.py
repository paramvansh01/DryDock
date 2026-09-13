"""Generator: exact documented counts, exactly 400 decoy pairs, deterministic output.
The live counterpart is tests/test_invariants.py::test_loaded_counts.
"""

from __future__ import annotations

import re
from functools import lru_cache

from bench.generate import DECOY_KINDS, generate


@lru_cache(maxsize=1)
def ds():
    return generate(20260913)


def test_documented_counts():
    c = ds().counts
    assert c["source_a"] == 36_600
    assert c["source_b"] == 32_400
    assert c["true_matches"] == 28_000
    assert c["decoy_pairs"] == 400
    assert c["true_dups"] == 600
    assert c["decoys_by_kind"] == DECOY_KINDS


def test_deterministic():
    again = generate(20260913)
    assert again.true_pairs == ds().true_pairs
    assert again.a[:50] == ds().a[:50] and again.b[-50:] == ds().b[-50:]


def test_ids_unique_and_shaped():
    a_ids = [x.CUST_ID for x in ds().a]
    b_ids = [x.CLIENT_REF for x in ds().b]
    assert len(set(a_ids)) == len(a_ids) and len(set(b_ids)) == len(b_ids)
    assert all(re.fullmatch(r"A\d{7}", i) for i in a_ids)
    assert all(re.fullmatch(r"C-\d{7}", i) for i in b_ids)


def test_decoys_are_not_matches_and_reference_real_records():
    a = {x.CUST_ID for x in ds().a}
    b = {x.CLIENT_REF for x in ds().b}
    for a_id, b_id, is_match, is_decoy, _kind in ds().true_pairs:
        assert a_id in a and b_id in b
        assert not (is_match and is_decoy)


def test_family_decoys_differ_by_dob_or_suffix():
    a = {x.CUST_ID: x for x in ds().a}
    b = {x.CLIENT_REF: x for x in ds().b}
    for a_id, b_id, _m, _d, kind in ds().true_pairs:
        if kind == "FAMILY_SAME_ADDRESS":
            ar, br = a[a_id], b[b_id]
            assert ar.ADDR_LINE == br.STREET
            assert br.LAST_NAME.endswith(" Jr") or br.DOB is None or br.DOB != ar.DATE_OF_BIRTH.strftime("%d/%m/%Y")


def test_dups_are_disjoint_from_matches_and_decoys():
    dup_ids = {i for pair in ds().true_dups for i in pair}
    assert not any(p[0] in dup_ids for p in ds().true_pairs)


# ------------------------------------------------------------------ diversity: generated, not repeated or hardcoded

def _matched():
    am = {x.CUST_ID: x for x in ds().a}
    bm = {x.CLIENT_REF: x for x in ds().b}
    return [(am[p[0]], bm[p[1]]) for p in ds().true_pairs if p[2]]


def test_records_are_diverse_not_looped():
    a = ds().a
    names = [x.FULL_NAME for x in a]
    assert len(set(names)) >= 0.85 * len(a)
    assert len({x.ADDR_LINE for x in a}) >= 0.9 * len(a)
    assert len({x.POSTCODE for x in a}) >= 0.8 * len(a)
    top = max(names.count(n) for n in set(names[:2000]))
    assert top <= 0.002 * len(a)              # no name dominates the way a short loop would
    distinct_rows = {(x.FULL_NAME, x.EMAIL, x.PHONE, x.ADDR_LINE, x.DATE_OF_BIRTH) for x in a}
    assert len(distinct_rows) >= len(a) - 60  # only typo'd duplicates may coincide


def test_locale_mix_follows_the_configured_weights():
    from collections import Counter
    from bench.generate import LOCALES
    c = Counter(x.COUNTRY for x in ds().a)
    n = sum(c.values())
    for _loc, _cc, country, _dial, w in LOCALES:
        assert abs(c[country] / n - w) < 0.02, (country, c[country] / n, w)


def test_divergence_rates_match_the_stated_probabilities():
    import re
    from bench.generate import ABBR
    m = _matched()
    n = len(m)
    dob_missing = sum(1 for _, y in m if y.DOB is None) / n
    stale = sum(1 for x, y in m if x.POSTCODE != y.ZIP) / n
    email_one_side = sum(1 for x, y in m if (x.EMAIL is None) != (y.EMAIL_ADDR is None)) / n
    assert abs(dob_missing - 0.08) < 0.01
    assert abs(stale - 0.15) < 0.01
    assert abs(email_one_side - 0.10) < 0.01
    abbreviable = [(x, y) for x, y in m if x.POSTCODE == y.ZIP and any(lng in x.ADDR_LINE for lng, _ in ABBR)]
    abbreviated = sum(1 for x, y in abbreviable if x.ADDR_LINE != y.STREET) / len(abbreviable)
    assert abs(abbreviated - 0.40) < 0.03, abbreviated
    shapes = {re.sub(r"\d", "9", y.MOBILE) for _, y in m}
    assert len(shapes) >= 5


def test_every_decoy_satisfies_its_kind_rule():
    am = {x.CUST_ID: x for x in ds().a}
    bm = {x.CLIENT_REF: x for x in ds().b}
    for a_id, b_id, _m, _d, kind in [p for p in ds().true_pairs if p[3]]:
        x, y = am[a_id], bm[b_id]
        a_given, a_family = x.FULL_NAME.split(" ", 1)[0], x.FULL_NAME.rsplit(" ", 1)[-1]
        if kind == "FAMILY_SAME_ADDRESS":
            assert y.FIRST_NAME == a_given and y.LAST_NAME.startswith(a_family) and y.STREET == x.ADDR_LINE
        elif kind == "SPOUSE_SHARED_CONTACT":
            assert y.STREET == x.ADDR_LINE and y.FIRST_NAME != a_given
            assert y.MOBILE.replace("(", "").replace(")", "").replace("-", "").replace(".", "").replace(" ", "")[-10:] \
                == x.PHONE.replace(" ", "")[-10:]
        elif kind == "PERSON_VS_BUSINESS":
            assert y.LAST_NAME.endswith(" Ltd") and y.FIRST_NAME == a_given[0] and y.STREET == x.ADDR_LINE
        elif kind == "COMMON_NAME_SAME_CITY":
            assert (y.FIRST_NAME, y.LAST_NAME, y.TOWN) == (a_given, a_family, x.CITY) and y.STREET != x.ADDR_LINE
        elif kind == "RECYCLED_EMAIL":
            assert y.EMAIL_ADDR == x.EMAIL and y.FIRST_NAME + " " + y.LAST_NAME != x.FULL_NAME
