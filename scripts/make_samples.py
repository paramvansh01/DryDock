"""Two small sample files for trying "Use your own data" without any data of your own.

    uv run python scripts/make_samples.py        # writes examples/sample_system_a.csv and _b.csv

Drawn from the synthetic demo generator (bench/generate.py): about 400 customers in System A and the
System B records that match them, the decoys planted next to them (relatives, shared phones, a person
and their company) and some B customers with no match. The column names and formats are deliberately
not Drydock's own, so the upload screen has a real mapping to suggest.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.generate import generate  # noqa: E402

OUT = ROOT / "examples"
N_A = 400


def main() -> int:
    ds = generate(20260913)
    a = ds.a[:N_A]
    a_ids = {x.CUST_ID for x in a}
    linked = {b for a_id, b, _match, _decoy, _kind in ds.true_pairs if a_id in a_ids}
    by_ref = {x.CLIENT_REF: x for x in ds.b}
    b = [by_ref[r] for r in sorted(linked) if r in by_ref]
    unmatched = [x for x in ds.b if x.CLIENT_REF not in {p[1] for p in ds.true_pairs}][:80]
    b += unmatched
    OUT.mkdir(exist_ok=True)
    with (OUT / "sample_system_a.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Customer ID", "Name", "Email Address", "Phone", "Street", "City", "Postcode", "Country",
                    "Date of Birth", "Last Updated"])
        for x in a:
            w.writerow([x.CUST_ID, x.FULL_NAME, x.EMAIL or "", x.PHONE, x.ADDR_LINE, x.CITY, x.POSTCODE, x.COUNTRY,
                        x.DATE_OF_BIRTH.isoformat() if x.DATE_OF_BIRTH else "", x.CREATED_AT.strftime("%Y-%m-%d %H:%M")])
    with (OUT / "sample_system_b.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["client_ref", "first_name", "surname", "e-mail", "mobile", "address_line_1", "town", "zip",
                    "country_code", "dob", "last_seen"])
        for x in b:
            w.writerow([x.CLIENT_REF, x.FIRST_NAME, x.LAST_NAME, x.EMAIL_ADDR or "", x.MOBILE, x.STREET, x.TOWN,
                        x.ZIP, x.NATION, x.DOB or "", x.LAST_SEEN.strftime("%Y-%m-%dT%H:%M:%S")])
    print(f"System A: {len(a)} rows, System B: {len(b)} rows ({len(unmatched)} with no match) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
