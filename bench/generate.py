"""Synthetic two-system customer data with ground truth by construction.

    uv run python bench/generate.py --seed 20260913            # generate + load (admin identity)
    uv run python bench/generate.py --seed 20260913 --csv out/ # write CSVs only, no database

Deterministic: the same seed gives byte-identical data.

  40,000 people
    28,000 in both systems, with realistic divergence (nickname, phone format,
           abbreviated street, stale address, missing email, surname typo, missing DOB)
     8,000 in SOURCE_A only;  4,000 in SOURCE_B only
       600 typo'd second records inside SOURCE_A, drawn from the A-only people
           (so deduplication does not collide with transitive closure)
       400 decoy people in SOURCE_B who are near-identical to an A record but are
           not the same person (5 kinds, table below)
  SOURCE_A = 36,600 rows, SOURCE_B = 32,400 rows, GOLDEN seeded from SOURCE_A.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

N_BOTH, N_A_ONLY, N_B_ONLY, N_DUPS = 28_000, 8_000, 4_000, 600
DECOY_KINDS = {"FAMILY_SAME_ADDRESS": 120, "SPOUSE_SHARED_CONTACT": 90, "PERSON_VS_BUSINESS": 60,
               "COMMON_NAME_SAME_CITY": 80, "RECYCLED_EMAIL": 50}
N_DECOYS = sum(DECOY_KINDS.values())
N_PEOPLE = N_BOTH + N_A_ONLY + N_B_ONLY

LOCALES = [("en_GB", "GB", "United Kingdom", "44", 0.45), ("en_US", "US", "United States", "1", 0.20),
           ("es_ES", "ES", "Spain", "34", 0.12), ("de_DE", "DE", "Germany", "49", 0.12),
           ("nl_NL", "NL", "Netherlands", "31", 0.11)]
NICK = {"Robert": "Bob", "William": "Bill", "Elizabeth": "Liz", "Katherine": "Kate", "Michael": "Mike",
        "James": "Jim", "Margaret": "Maggie", "Thomas": "Tom", "Richard": "Rick", "Jennifer": "Jen",
        "Christopher": "Chris", "Daniel": "Dan", "Joseph": "Joe", "Alexander": "Alex", "Samuel": "Sam",
        "Susan": "Sue", "Patricia": "Pat", "Anthony": "Tony", "Stephen": "Steve", "Matthew": "Matt",
        "Charlotte": "Lottie", "Edward": "Ed", "Benjamin": "Ben", "Jonathan": "Jon", "Rebecca": "Becky"}
# Common postal abbreviations for the street suffixes Faker's locales actually emit.
ABBR = [("Street", "St"), ("Road", "Rd"), ("Avenue", "Ave"), ("Lane", "Ln"), ("Drive", "Dr"), ("Court", "Ct"),
        ("Place", "Pl"), ("Square", "Sq"), ("Crescent", "Cres"), ("Gardens", "Gdns"), ("Boulevard", "Blvd"),
        ("Parkway", "Pkwy"), ("Terrace", "Ter"), ("Mountain", "Mtn"), ("Heights", "Hts"), ("Circle", "Cir"),
        ("Point", "Pt"), ("Harbor", "Hbr"), ("Junction", "Jct"), ("Island", "Is"), ("Springs", "Spgs"),
        ("Station", "Sta"), ("Village", "Vlg"), ("Ville", "Vl"), ("Plains", "Plns"), ("Fields", "Flds"),
        ("Grove", "Grv"), ("Hills", "Hls"), ("Mews", "Mw"), ("Way", "Wy"), ("Park", "Pk"), ("Close", "Cl"),
        ("Straße", "Str."), ("straße", "str."), ("Weg", "W."), ("Platz", "Pl."), ("straat", "str."),
        ("laan", "ln."), ("plein", "pl."), ("Calle", "C/"), ("Avenida", "Av."), ("Plaza", "Pza."),
        ("Paseo", "Pº"), ("Camino", "Cno.")]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me", "web.de", "hotmail.co.uk"]
MALE = ["John", "David", "Peter", "Mark", "Paul", "Andrew", "James", "Robert", "Carlos", "Jan", "Lukas"]
FEMALE = ["Mary", "Sarah", "Emma", "Laura", "Anna", "Sophie", "Maria", "Lucia", "Eva", "Julia", "Claire"]


@dataclass
class Person:
    pid: int
    locale: str
    cc: str
    country: str
    dial: str
    given: str
    family: str
    dob: dt.date
    email: str | None
    phone10: str
    street: str
    city: str
    postcode: str


@dataclass
class ARec:
    CUST_ID: str
    FULL_NAME: str
    EMAIL: str | None
    PHONE: str
    ADDR_LINE: str
    CITY: str
    POSTCODE: str
    COUNTRY: str
    DATE_OF_BIRTH: dt.date | None
    CREATED_AT: dt.datetime


@dataclass
class BRec:
    CLIENT_REF: str
    FIRST_NAME: str
    LAST_NAME: str
    EMAIL_ADDR: str | None
    MOBILE: str
    STREET: str
    TOWN: str
    ZIP: str
    NATION: str
    DOB: str | None
    LAST_SEEN: dt.datetime


@dataclass
class Dataset:
    seed: int
    a: list[ARec]
    b: list[BRec]
    true_pairs: list[tuple[str, str, bool, bool, str | None]]   # A_ID, B_ID, IS_MATCH, IS_DECOY, DECOY_KIND
    true_dups: list[tuple[str, str]]
    counts: dict


def _fakers(seed: int):
    from faker import Faker
    fk = {}
    for i, (loc, *_rest) in enumerate(LOCALES):
        f = Faker(loc)
        f.seed_instance(seed * 31 + i)
        fk[loc] = f
    return fk


def _phone10(r: random.Random) -> str:
    return str(r.randint(2, 9)) + "".join(str(r.randint(0, 9)) for _ in range(9))


def _fmt_phone_a(p: Person) -> str:
    n = p.phone10
    return f"+{p.dial} {n[:3]} {n[3:6]} {n[6:]}"


def _fmt_phone_b(p: Person, r: random.Random) -> str:
    n = p.phone10
    return r.choice([n, f"({n[:3]}) {n[3:6]}-{n[6:]}", f"{p.dial}{n}", f"{n[:3]}.{n[3:6]}.{n[6:]}", f"0{n}"])


def _abbreviate(street: str) -> str:
    for long, short in ABBR:
        if long in street:
            return street.replace(long, short)
    return street


def _typo(s: str, r: random.Random) -> str:
    if len(s) < 4:
        return s + s[-1]
    i = r.randint(1, len(s) - 2)
    op = r.choice(["swap", "drop", "dup"])
    if op == "swap":
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "drop":
        return s[:i] + s[i + 1:]
    return s[:i] + s[i] + s[i:]


def _clean(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum()).lower() or "x"


def generate(seed: int = 20260913) -> Dataset:
    r = random.Random(seed)
    fk = _fakers(seed)
    weights = [loc[4] for loc in LOCALES]
    people: list[Person] = []
    for pid in range(N_PEOPLE):
        loc, cc, country, dial, _w = r.choices(LOCALES, weights)[0]
        f = fk[loc]
        given = f.first_name().split(" ")[0]
        family = f.last_name().split(" ")[-1]
        dob = dt.date(1936, 1, 1) + dt.timedelta(days=r.randint(0, 70 * 365))
        email = f"{_clean(given)}.{_clean(family)}{r.randint(1, 999)}@{r.choice(DOMAINS)}"
        people.append(Person(pid, loc, cc, country, dial, given, family, dob, email, _phone10(r),
                             f.street_address().replace("\n", " "), f.city(), f.postcode()))

    idx = list(range(N_PEOPLE))
    r.shuffle(idx)
    both, a_only, b_only = idx[:N_BOTH], idx[N_BOTH:N_BOTH + N_A_ONLY], idx[N_BOTH + N_A_ONLY:]
    a_ids = [f"A{n:07d}" for n in r.sample(range(1_000_000, 9_999_999), N_BOTH + N_A_ONLY + N_DUPS)]
    b_ids = [f"C-{n:07d}" for n in r.sample(range(1_000_000, 9_999_999), N_BOTH + N_B_ONLY + N_DECOYS)]
    ai = iter(a_ids)
    bi = iter(b_ids)

    def a_rec(p: Person, cid: str) -> ARec:
        created = dt.datetime(2010, 1, 1) + dt.timedelta(minutes=r.randint(0, 12 * 365 * 24 * 60))
        return ARec(cid, f"{p.given} {p.family}", p.email, _fmt_phone_a(p), p.street, p.city, p.postcode,
                    p.country, p.dob, created)

    def b_rec(p: Person, ref: str, *, diverge: bool) -> BRec:
        given, family, email, street, city, postcode = p.given, p.family, p.email, p.street, p.city, p.postcode
        dob: dt.date | None = p.dob
        if diverge:
            if given in NICK and r.random() < 0.35:
                given = NICK[given]
            if r.random() < 0.05:
                family = _typo(family, r)
            if r.random() < 0.40:
                street = _abbreviate(street)
            if r.random() < 0.15:  # stale address on one side
                f = fk[p.locale]
                street, city, postcode = f.street_address().replace("\n", " "), f.city(), f.postcode()
            if r.random() < 0.05 and email:
                email = email.split("@")[0] + "@" + r.choice(DOMAINS)
            if r.random() < 0.08:
                dob = None
        seen = dt.datetime(2020, 1, 1) + dt.timedelta(minutes=r.randint(0, 6 * 365 * 24 * 60))
        return BRec(ref, given, family, email, _fmt_phone_b(p, r), street, city, postcode, p.cc,
                    dob.strftime("%d/%m/%Y") if dob else None, seen)

    a: list[ARec] = []
    b: list[BRec] = []
    pairs: list[tuple[str, str, bool, bool, str | None]] = []
    a_of: dict[int, str] = {}
    for pid in both:
        p = people[pid]
        if r.random() < 0.10:  # missing email on one side
            if r.random() < 0.5:
                p_a = Person(**{**asdict(p), "email": None})
                ar, br = a_rec(p_a, next(ai)), b_rec(p, next(bi), diverge=True)
            else:
                ar, br = a_rec(p, next(ai)), b_rec(Person(**{**asdict(p), "email": None}), next(bi), diverge=True)
        else:
            ar, br = a_rec(p, next(ai)), b_rec(p, next(bi), diverge=True)
        a.append(ar)
        b.append(br)
        a_of[pid] = ar.CUST_ID
        pairs.append((ar.CUST_ID, br.CLIENT_REF, True, False, None))
    for pid in a_only:
        ar = a_rec(people[pid], next(ai))
        a.append(ar)
        a_of[pid] = ar.CUST_ID
    for pid in b_only:
        b.append(b_rec(people[pid], next(bi), diverge=False))

    # Internal duplicates inside SOURCE_A, from A-only people.
    dups: list[tuple[str, str]] = []
    dup_people = set(r.sample(a_only, N_DUPS))
    for pid in sorted(dup_people):
        p = people[pid]
        d = a_rec(Person(**{**asdict(p), "family": _typo(p.family, r),
                            "email": p.email if r.random() < 0.5 else None}), next(ai))
        d.PHONE = p.phone10 if r.random() < 0.5 else d.PHONE
        a.append(d)
        dups.append((a_of[pid], d.CUST_ID))

    # Decoys: new people in SOURCE_B, near-identical to an existing A record, NOT the same person.
    a_email = {x.CUST_ID: x.EMAIL for x in a}
    pool = [pid for pid in both + a_only if pid not in dup_people]
    with_email = [pid for pid in pool if a_email.get(a_of[pid])]
    recycled = r.sample(with_email, DECOY_KINDS["RECYCLED_EMAIL"])
    rest = r.sample([pid for pid in pool if pid not in set(recycled)], N_DECOYS - len(recycled))
    decoy_targets = rest[:N_DECOYS - len(recycled)] + recycled  # RECYCLED_EMAIL is the last kind consumed
    ti = iter(decoy_targets)
    for kind, n in DECOY_KINDS.items():
        for _ in range(n):
            t = people[next(ti)]
            f = fk[t.locale]
            seen = dt.datetime(2020, 1, 1) + dt.timedelta(minutes=r.randint(0, 6 * 365 * 24 * 60))
            if kind == "FAMILY_SAME_ADDRESS":
                junior_dob = t.dob + dt.timedelta(days=365 * r.randint(24, 34) + r.randint(0, 300))
                last = t.family + (" Jr" if r.random() < 0.5 else "")
                phone = _fmt_phone_b(t, r) if r.random() < 0.5 else _phone10(r)
                rec = BRec("", t.given, last, f"{_clean(t.given)}{r.randint(10, 99)}@{r.choice(DOMAINS)}", phone,
                           t.street, t.city, t.postcode, t.cc,
                           junior_dob.strftime("%d/%m/%Y") if r.random() < 0.85 else None, seen)
            elif kind == "SPOUSE_SHARED_CONTACT":
                pool = FEMALE if t.given in MALE or r.random() < 0.5 else MALE
                given = r.choice([g for g in pool if g != t.given])   # a spouse never shares the first name
                family = t.family if r.random() < 0.8 else f.last_name().split(" ")[-1]
                sp_dob = t.dob + dt.timedelta(days=r.randint(-5 * 365, 5 * 365))
                rec = BRec("", given, family, f"{_clean(given)}.{_clean(family)}@{r.choice(DOMAINS)}",
                           _fmt_phone_b(t, r), t.street, t.city, t.postcode, t.cc, sp_dob.strftime("%d/%m/%Y"), seen)
            elif kind == "PERSON_VS_BUSINESS":
                rec = BRec("", t.given[0], f"{t.family} Ltd", f"info@{_clean(t.family)}ltd.co.uk", _phone10(r),
                           t.street, t.city, t.postcode, t.cc, None, seen)
            elif kind == "COMMON_NAME_SAME_CITY":
                other = t.dob + dt.timedelta(days=r.choice([-1, 1]) * r.randint(2 * 365, 30 * 365))
                rec = BRec("", t.given, t.family, f"{_clean(t.given)}{_clean(t.family)}{r.randint(1, 99)}@{r.choice(DOMAINS)}",
                           _phone10(r), f.street_address().replace("\n", " "), t.city, f.postcode(), t.cc,
                           other.strftime("%d/%m/%Y"), seen)
            else:  # RECYCLED_EMAIL
                given, family = f.first_name().split(" ")[0], f.last_name().split(" ")[-1]
                other = dt.date(1940, 1, 1) + dt.timedelta(days=r.randint(0, 60 * 365))
                rec = BRec("", given, family, a_email[a_of[t.pid]], _phone10(r), f.street_address().replace("\n", " "), f.city(),
                           f.postcode(), t.cc, other.strftime("%d/%m/%Y"), seen)
            rec.CLIENT_REF = next(bi)
            b.append(rec)
            pairs.append((a_of[t.pid], rec.CLIENT_REF, False, True, kind))

    r.shuffle(a)
    r.shuffle(b)
    counts = {"source_a": len(a), "source_b": len(b), "true_matches": sum(1 for p in pairs if p[2]),
              "decoy_pairs": sum(1 for p in pairs if p[3]), "true_dups": len(dups),
              "decoys_by_kind": {k: sum(1 for p in pairs if p[4] == k) for k in DECOY_KINDS}}
    return Dataset(seed, a, b, pairs, dups, counts)


def golden_rows(ds: Dataset):
    for x in ds.a:
        yield (f"G-{x.CUST_ID}", x.FULL_NAME, x.EMAIL, x.PHONE, x.ADDR_LINE, x.CITY, x.POSTCODE, x.COUNTRY,
               x.DATE_OF_BIRTH, x.CUST_ID, None, None, None, None)


def write_csv(ds: Dataset, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, rows, header in (("source_a", [asdict(x) for x in ds.a], list(ARec.__annotations__)),
                               ("source_b", [asdict(x) for x in ds.b], list(BRec.__annotations__))):
        with (out / f"{name}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(rows)
    with (out / "true_pairs.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["A_ID", "B_ID", "IS_MATCH", "IS_DECOY", "DECOY_KIND"])
        w.writerows(ds.true_pairs)
    (out / "counts.json").write_text(json.dumps(ds.counts, indent=2))


def load(ds: Dataset, record: list[dict] | None = None) -> dict:
    """Load as the ADMIN identity: admin owns SOURCE_A/SOURCE_B/BENCH (read-only to svc, invisible to agent).
    Every statement that runs is appended to `record` (scripts/setup.py passes its list): that is the live
    evidence scripts/probe_all.py uses for admin-owned SQL it cannot itself run as DRYDOCK_SVC."""
    from drydock.config import require_verified
    from drydock.db import Db, lit
    sys.path.insert(0, str(ROOT / "scripts"))
    from _common import split_sql

    require_verified("V3")
    db = Db("admin")
    for stmt in split_sql((ROOT / "bench" / "sql" / "10_sources.sql").read_text()):
        n = db.run(stmt)
        if record is not None:
            from _common import now_iso
            record.append({"file": "10_sources.sql", "identity": "admin", "ts": now_iso(), "sql": stmt, "rows": n})
    db.import_rows(([getattr(x, c) for c in ARec.__annotations__] for x in ds.a), "SOURCE_A", "CUSTOMERS")
    db.import_rows(([getattr(x, c) for c in BRec.__annotations__] for x in ds.b), "SOURCE_B", "CLIENTS")
    db.import_rows(iter(ds.true_pairs), "BENCH", "TRUE_PAIRS")
    db.import_rows(iter(ds.true_dups), "BENCH", "TRUE_DUPS")
    db.import_rows(golden_rows(ds), "BENCH", "GOLDEN_CLEAN")
    db.run(f"INSERT INTO BENCH.GENERATION VALUES ({ds.seed}, CURRENT_TIMESTAMP, {lit(json.dumps(ds.counts))})")
    got = {
        "source_a": int(db.scalar("SELECT COUNT(*) FROM SOURCE_A.CUSTOMERS")),
        "source_b": int(db.scalar("SELECT COUNT(*) FROM SOURCE_B.CLIENTS")),
        "true_matches": int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_PAIRS WHERE IS_MATCH")),
        "decoy_pairs": int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_PAIRS WHERE IS_DECOY")),
        "true_dups": int(db.scalar("SELECT COUNT(*) FROM BENCH.TRUE_DUPS")),
        "golden_clean": int(db.scalar("SELECT COUNT(*) FROM BENCH.GOLDEN_CLEAN")),
    }
    return got


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--csv", help="write CSVs to this directory instead of loading")
    a = ap.parse_args()
    ds = generate(a.seed)
    print(json.dumps(ds.counts, indent=2))
    if a.csv:
        write_csv(ds, Path(a.csv))
        print(f"wrote CSVs to {a.csv}")
        return 0
    got = load(ds)
    print("database counts:", json.dumps(got, indent=2))
    expected = {k: ds.counts[k] for k in ("source_a", "source_b", "true_matches", "decoy_pairs", "true_dups")}
    expected["golden_clean"] = ds.counts["source_a"]
    ok = all(got[k] == v for k, v in expected.items())
    print("COUNTS MATCH" if ok else f"COUNT MISMATCH expected {expected}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
