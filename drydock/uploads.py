"""Bring your own data: two customer files -> UPLOADS.SOURCE_A / SOURCE_B -> the active dataset.

    parse()      read a file as uploaded: encoding, delimiter, header, rows
    profile()    what each column holds, a preview, and a suggested column mapping
    normalise()  apply a person's mapping to every row: the rows to load, plus a data-quality report
    load()       write both files and GOLDEN's starting snapshot, in one transaction

System A is the list GOLDEN starts from; System B is folded into it. Ids are namespaced ("A-", "B-")
so the two files may number their customers the same way: an unmatched B record becomes golden row
'G-' || CLIENT_REF, which must never collide with an A record's 'G-' || CUST_ID.

Parsing and normalisation are plain Python and offline-testable. Every SQL statement still passes the
dialect firewall in Db.execute. Rows go in as multi-row INSERTs: DRYDOCK_SVC holds no IMPORT privilege
(check V3 found none), and an upload is small enough that it does not need the bulk path.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field

from .config import ROOT
from .db import Db, lit

MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 300_000
BATCH = 500
UPLOAD_DIR = ROOT / "runs" / "uploads"
SIDES = ("a", "b")


class UploadError(ValueError):
    """A problem with a file that a person can fix. The message says how, in plain language."""


# ------------------------------------------------------------------ the fields a person maps

@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str


FIELDS = (
    Field("id", "Customer ID", "A code that is different for every customer in this file. "
          "No such column? Leave it empty and Drydock numbers the rows for you."),
    Field("full_name", "Full name", "The whole name in one column. If your file splits the name, "
          "use First name and Last name instead."),
    Field("first_name", "First name", "Only if the name is split across two columns."),
    Field("last_name", "Last name", "Only if the name is split across two columns."),
    Field("email", "Email", "The strongest clue that two records are the same person."),
    Field("phone", "Phone", "Any format: Drydock compares the digits."),
    Field("address", "Street address", "House number and street."),
    Field("city", "City or town", ""),
    Field("postcode", "Postcode / ZIP", ""),
    Field("country", "Country", ""),
    Field("dob", "Date of birth", "Tells apart relatives who share a name and an address."),
    Field("updated", "Last updated", "When the record last changed. When two records disagree on an "
          "address, the newer one is kept."),
)
FIELD_KEYS = tuple(f.key for f in FIELDS)

# Header names, compared after lower-casing and dropping everything but letters and digits.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "id": ("id", "customerid", "custid", "clientid", "clientref", "customerref", "ref", "reference", "accountid",
           "accountnumber", "accountno", "customernumber", "customerno", "clientnumber", "recordid", "contactid",
           "memberid", "uid", "uuid", "key"),
    "full_name": ("name", "fullname", "customername", "clientname", "contactname", "displayname", "membername",
                  "personname"),
    "first_name": ("firstname", "forename", "forenames", "givenname", "first", "fname", "christianname"),
    "last_name": ("lastname", "surname", "familyname", "last", "lname"),
    "email": ("email", "emailaddress", "mail", "emailaddr", "emailid", "contactemail", "primaryemail"),
    "phone": ("phone", "phonenumber", "phoneno", "mobile", "mobilenumber", "mobilephone", "tel", "telephone",
              "telephonenumber", "cell", "cellphone", "contactnumber", "primaryphone"),
    "address": ("address", "street", "streetaddress", "address1", "addressline1", "addr", "addrline", "addressline",
                "addr1", "line1", "billingaddress", "shippingaddress"),
    "city": ("city", "town", "locality", "towncity", "citytown", "suburb", "hometown"),
    "postcode": ("postcode", "postalcode", "zip", "zipcode", "postcodezip", "zippostcode", "pincode", "pin"),
    "country": ("country", "nation", "countrycode", "countryname", "countryiso"),
    "dob": ("dob", "dateofbirth", "birthdate", "birthday", "born", "bdate"),
    "updated": ("updated", "updatedat", "lastupdated", "modified", "modifiedat", "lastmodified", "lastseen",
                "lastactive", "lastactivity", "updatedon", "changedat", "lastchanged", "created", "createdat",
                "createdon", "datecreated"),
}

PLACEHOLDERS = {"", "-", "--", "---", ".", "?", "n/a", "na", "n.a.", "none", "null", "nil", "unknown", "#n/a",
                "not known", "not available", "tbc", "tbd"}

EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RX = re.compile(r"^[+()\d\s.\-/]{7,}$")

DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%m/%d/%y",
                "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y%m%d")
DATE_LABELS = {
    "%Y-%m-%d": "YYYY-MM-DD", "%d/%m/%Y": "DD/MM/YYYY (day first)", "%m/%d/%Y": "MM/DD/YYYY (month first)",
    "%d.%m.%Y": "DD.MM.YYYY", "%d-%m-%Y": "DD-MM-YYYY", "%Y/%m/%d": "YYYY/MM/DD",
    "%d/%m/%y": "DD/MM/YY (day first)", "%m/%d/%y": "MM/DD/YY (month first)", "%d %b %Y": "3 Apr 1985",
    "%d %B %Y": "3 April 1985", "%b %d %Y": "Apr 3 1985", "%B %d %Y": "April 3 1985", "%Y%m%d": "YYYYMMDD",
}
DAY_FIRST = {"%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y"}
MONTH_FIRST = {"%m/%d/%Y", "%m/%d/%y"}

# Column widths of UPLOADS.SOURCE_A / SOURCE_B (sql/03_uploads.sql). Longer values are shortened and reported.
A_COLS = ("CUST_ID", "FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "COUNTRY", "DATE_OF_BIRTH",
          "CREATED_AT")
B_COLS = ("CLIENT_REF", "FIRST_NAME", "LAST_NAME", "EMAIL_ADDR", "MOBILE", "STREET", "TOWN", "ZIP", "NATION", "DOB",
          "LAST_SEEN")
WIDTH = {"id": 60, "full_name": 200, "first_name": 100, "last_name": 100, "email": 200, "phone": 50,
         "address": 300, "city": 100, "postcode": 20, "country": 60}


# ------------------------------------------------------------------ parsing

@dataclass
class Table:
    filename: str
    encoding: str
    delimiter: str
    header: list[str]
    rows: list[list[str]]
    ragged: int = 0             # rows with more or fewer fields than the header
    blank: int = 0              # completely empty lines, skipped


def _decode(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc), "UTF-8" if enc == "utf-8-sig" else "Windows-1252"
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "Latin-1"


def _delimiter(text: str) -> str:
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        return max(",;\t|", key=first.count) if any(d in first for d in ",;\t|") else ","


def parse(raw: bytes, filename: str) -> Table:
    if not raw.strip():
        raise UploadError("This file is empty.")
    if len(raw) > MAX_BYTES:
        raise UploadError(f"This file is {len(raw) / 1e6:.0f} MB; Drydock accepts up to {MAX_BYTES // 1_000_000} MB "
                          "per file. Split it, or remove columns you do not need, and try again.")
    if raw[:4] == b"PK\x03\x04" or raw[:4] == b"\xd0\xcf\x11\xe0":
        raise UploadError("This looks like an Excel file. In Excel choose File > Save As > "
                          "'CSV UTF-8 (Comma delimited)', then upload the .csv file.")
    text, encoding = _decode(raw)
    text = text.replace("\x00", "")
    delim = _delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    header: list[str] | None = None
    rows: list[list[str]] = []
    ragged = blank = 0
    try:
        for rec in reader:
            if not any(c.strip() for c in rec):
                blank += header is not None
                continue
            if header is None:
                header = _clean_header(rec)
                continue
            if len(rec) != len(header):
                ragged += 1
                rec = (rec + [""] * len(header))[:len(header)]
            rows.append(rec)
            if len(rows) > MAX_ROWS:
                raise UploadError(f"This file has more than {MAX_ROWS:,} rows, the most Drydock accepts per file.")
    except csv.Error as e:
        raise UploadError(f"This file could not be read as CSV ({e}). Save it again as 'CSV UTF-8' and retry.") from e
    if header is None:
        raise UploadError("This file has no header row. The first line should name the columns.")
    if not rows:
        raise UploadError("This file has a header row but no customers under it.")
    if len(header) < 2:
        raise UploadError("Only one column was found. Is the file separated by something other than commas, "
                          "semicolons or tabs? Save it again as 'CSV UTF-8' and retry.")
    return Table(filename, encoding, delim, header, rows, ragged, blank)


def _clean_header(rec: list[str]) -> list[str]:
    out: list[str] = []
    for i, h in enumerate(rec):
        name = h.strip().strip('"').strip() or f"Column {i + 1}"
        base, n = name, 2
        while name in out:
            name, n = f"{base} ({n})", n + 1
        out.append(name)
    return out


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _value(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return None if s.lower() in PLACEHOLDERS else s


# ------------------------------------------------------------------ dates

def _strip_time(s: str) -> str:
    return re.split(r"[ T](?=\d{1,2}:\d{2})", s, maxsplit=1)[0].strip()


def parse_date(s: str, fmt: str | None) -> dt.date | None:
    s = _strip_time(s)
    fmts = (fmt,) if fmt else DATE_FORMATS
    for f in fmts:
        try:
            d = dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
        if "%y" in f and d > dt.date.today():      # a two-digit year in the future is last century: '45 -> 1945
            d = d.replace(year=d.year - 100)
        return d
    return None


def parse_timestamp(s: str, fmt: str | None) -> dt.datetime | None:
    try:
        v = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass
    d = parse_date(s, fmt)
    if d is None:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    h, mi, se = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)
    try:
        return dt.datetime(d.year, d.month, d.day, h, mi, se)
    except ValueError:
        return dt.datetime(d.year, d.month, d.day)


def guess_date_format(values: list[str]) -> dict | None:
    """The format that reads (nearly) every sampled value. `ambiguous` when day-first and month-first
    both read everything (no day above 12 in the sample): the person is asked which one it is."""
    sample = [_strip_time(v) for v in values[:300] if v]
    if not sample:
        return None
    ok = {}
    for f in DATE_FORMATS:
        n = 0
        for v in sample:
            try:
                dt.datetime.strptime(v, f)
                n += 1
            except ValueError:
                pass
        if n:
            ok[f] = n
    if not ok:
        return None
    best_n = max(ok.values())
    if best_n < 0.8 * len(sample):
        return None
    best = [f for f in DATE_FORMATS if ok.get(f) == best_n]
    ambiguous = any(f in DAY_FIRST for f in best) and any(f in MONTH_FIRST for f in best)
    return {"format": best[0], "label": DATE_LABELS[best[0]], "ambiguous": ambiguous,
            "options": [{"format": f, "label": DATE_LABELS[f]} for f in best]}


# ------------------------------------------------------------------ profiling + suggested mapping

def _kind(values: list[str]) -> str:
    vals = [v for v in values if v][:300]
    if not vals:
        return "empty"
    n = len(vals)
    if sum(bool(EMAIL_RX.match(v)) for v in vals) >= 0.6 * n:
        return "email"
    if guess_date_format(vals):
        return "date"
    if sum(bool(PHONE_RX.match(v)) and len(re.sub(r"\D", "", v)) >= 7 for v in vals) >= 0.7 * n:
        return "phone"
    if all(v.isdigit() for v in vals):
        return "number"
    return "text"


def profile(t: Table) -> dict:
    cols = []
    for i, name in enumerate(t.header):
        vals = [_value(r[i]) for r in t.rows]
        filled = [v for v in vals if v]
        distinct = list(dict.fromkeys(filled))
        kind = _kind(filled)
        cols.append({"name": name, "filled": len(filled), "unique": len(set(filled)), "samples": distinct[:4],
                     "kind": kind, "date": guess_date_format(filled) if kind == "date" else None})
    return {"filename": t.filename, "encoding": t.encoding, "delimiter": {",": "comma", ";": "semicolon",
            "\t": "tab", "|": "pipe"}.get(t.delimiter, t.delimiter), "rows": len(t.rows), "ragged": t.ragged,
            "columns": cols, "suggested": suggest(t.header, cols), "preview": [r[:12] for r in t.rows[:5]],
            "fields": [{"key": f.key, "label": f.label, "help": f.help} for f in FIELDS]}


def suggest(header: list[str], cols: list[dict]) -> dict[str, str | None]:
    out: dict[str, str | None] = {k: None for k in FIELD_KEYS}
    used: set[str] = set()
    keys = {h: _key(h) for h in header}
    for field_key in FIELD_KEYS:                                   # exact header matches first
        for h in header:
            if h not in used and keys[h] in SYNONYMS[field_key]:
                out[field_key], used = h, used | {h}
                break
    kind = {c["name"]: c["kind"] for c in cols}
    for field_key in FIELD_KEYS:                                   # then headers that contain a synonym
        if out[field_key]:
            continue
        for h in header:
            k = keys[h]
            # Short words ("name", "mail", "city") are too loose inside longer headers: "Company Name" is not a
            # person's name and "Mailing address" is not an email. Dates must also look like dates.
            if h in used or (field_key in ("dob", "updated") and kind.get(h) != "date"):
                continue
            if any(len(s) >= 5 and s in k for s in SYNONYMS[field_key]):
                out[field_key], used = h, used | {h}
                break
    by_kind = {"email": "email", "phone": "phone", "date": "dob"}   # finally, what the values look like
    for c in cols:
        target = by_kind.get(c["kind"])
        if target and not out[target] and c["name"] not in used:
            out[target], used = c["name"], used | {c["name"]}
    if out["first_name"] and out["last_name"] and out["full_name"] and _key(out["full_name"]) == "name":
        out["full_name"] = None                                    # "Name" next to First/Last is usually a label
    return out


# ------------------------------------------------------------------ normalisation + quality report

@dataclass
class Report:
    side: str
    rows: int = 0
    filled: Counter = field(default_factory=Counter)
    issues: dict = field(default_factory=dict)        # code -> {"message", "count", "examples", "level"}

    def add(self, code: str, level: str, message: str, row: int | None = None, value: str | None = None) -> None:
        it = self.issues.setdefault(code, {"code": code, "level": level, "message": message, "count": 0,
                                           "examples": []})
        it["count"] += 1
        if len(it["examples"]) < 5 and (row is not None or value is not None):
            it["examples"].append({"row": row, "value": value})

    def as_dict(self) -> dict:
        order = {"error": 0, "warning": 1, "info": 2}
        issues = sorted(self.issues.values(), key=lambda i: (order[i["level"]], -i["count"]))
        return {"side": self.side, "rows": self.rows, "filled": dict(self.filled), "issues": issues,
                "errors": sum(1 for i in issues if i["level"] == "error"),
                "can_load": not any(i["level"] == "error" for i in issues)}


def _mapping(mapping: dict, header: list[str]) -> dict[str, int | None]:
    idx = {h: i for i, h in enumerate(header)}
    out: dict[str, int | None] = {}
    for k in FIELD_KEYS:
        col = (mapping.get("fields") or {}).get(k)
        if col and col not in idx:
            raise UploadError(f"The column '{col}' chosen for {k} is not in the file.")
        out[k] = idx[col] if col else None
    return out


def _cut(v: str | None, key: str, rep: Report, row: int) -> str | None:
    if v is None:
        return None
    if len(v) > WIDTH[key]:
        rep.add("TRUNCATED", "warning", "Some values were longer than Drydock stores and were shortened.", row,
                v[:40] + "…")
        return v[:WIDTH[key]]
    return v


def _name(get, has_split: bool) -> tuple[str | None, str | None, str | None]:
    """(full, first, last). "Smith, John" in a full-name column reads as John Smith."""
    full, first, last = get("full_name"), get("first_name"), get("last_name")
    if full and not has_split and full.count(",") == 1:
        last_part, first_part = (p.strip() for p in full.split(","))
        if first_part and last_part:
            full = f"{first_part} {last_part}"
    if not full and (first or last):
        full = " ".join(p for p in (first, last) if p)
    if full and not (first or last):
        parts = full.split()
        last = parts[-1]
        first = " ".join(parts[:-1]) or None
    return full, first, last


def normalise(t: Table, side: str, mapping: dict) -> tuple[list[tuple], dict]:
    """Every row in the shape of UPLOADS.SOURCE_A (side "a") or UPLOADS.SOURCE_B (side "b"), and the report."""
    if side not in SIDES:
        raise UploadError("side must be a or b")
    col = _mapping(mapping, t.header)
    fmts = mapping.get("formats") or {}
    rep = Report(side, rows=len(t.rows))
    prefix = side.upper() + "-"
    has_split = col["first_name"] is not None or col["last_name"] is not None
    if col["full_name"] is None and not has_split:
        rep.add("NO_NAME_COLUMN", "error", "Choose the column that holds the customer's name (a full name, or "
                "first and last name). Drydock cannot match people without it.")
    if col["email"] is None and col["phone"] is None:
        rep.add("NO_CONTACT", "warning", "Neither email nor phone is chosen. Matching will rely on names, "
                "addresses and dates of birth, and will find fewer duplicates.")
    if t.ragged:
        rep.issues["RAGGED"] = {"code": "RAGGED", "level": "warning", "count": t.ragged, "examples": [],
                                "message": "Some rows had more or fewer columns than the header; they were "
                                           "padded or cut to fit."}

    out: list[tuple] = []
    ids: dict[str, int] = {}
    emails: Counter = Counter()
    phones: Counter = Counter()
    for n, r in enumerate(t.rows, start=2):                      # row 1 is the header
        def get(k: str, _r=r) -> str | None:
            i = col[k]
            return _value(_r[i]) if i is not None else None

        if col["id"] is None:
            raw_id = f"row{n - 1:06d}"
        else:
            raw_id = get("id")
            if not raw_id:
                rep.add("MISSING_ID", "error", "Some rows have no customer ID. Choose a different ID column, or "
                        "leave Customer ID empty and Drydock will number the rows.", n)
                continue
        if len(raw_id) > WIDTH["id"]:
            rep.add("LONG_ID", "error", f"Some IDs are longer than {WIDTH['id']} characters. Choose a shorter ID "
                    "column, or leave Customer ID empty and Drydock will number the rows.", n, raw_id[:40])
            continue
        if raw_id in ids:
            rep.add("DUPLICATE_ID", "error", "Some customer IDs appear more than once. Every customer needs their "
                    "own ID: choose a different ID column, or leave Customer ID empty and Drydock will number the "
                    "rows.", n, f"{raw_id} (also on row {ids[raw_id]})")
            continue
        ids[raw_id] = n

        full, first, last = _name(get, has_split)
        if not full:
            rep.add("NO_NAME", "warning", "Some rows have no name. They can still be matched by email or phone.", n)
        email = get("email")
        if email and not EMAIL_RX.match(email):
            rep.add("BAD_EMAIL", "warning", "Some email addresses don't look valid and were left blank.", n, email)
            email = None
        if email:
            emails[email.lower()] += 1
        phone = get("phone")
        if phone and len(re.sub(r"\D", "", phone)) < 6:
            rep.add("BAD_PHONE", "warning", "Some phone numbers have too few digits and were left blank.", n, phone)
            phone = None
        if phone:
            phones[re.sub(r"\D", "", phone)[-10:]] += 1
        dob = None
        if (raw := get("dob")) is not None:
            dob = parse_date(raw, fmts.get("dob"))
            if dob is None or not (dt.date(1900, 1, 1) <= dob <= dt.date.today()):
                label = DATE_LABELS.get(fmts.get("dob") or "", "a date")
                rep.add("BAD_DOB", "warning", f"Some dates of birth could not be read as {label}, or are in the "
                        "future, and were left blank.", n, raw)
                dob = None
        updated = None
        if (raw := get("updated")) is not None:
            updated = parse_timestamp(raw, fmts.get("updated"))
            if updated is None:
                rep.add("BAD_UPDATED", "warning", "Some 'last updated' dates could not be read and were left blank.",
                        n, raw)

        vals = {k: _cut(v, k, rep, n) for k, v in (("full_name", full), ("first_name", first), ("last_name", last),
                ("email", email), ("phone", phone), ("address", get("address")), ("city", get("city")),
                ("postcode", get("postcode")), ("country", get("country")))}
        for k, v in vals.items():
            if v:
                rep.filled[k] += 1
        if dob:
            rep.filled["dob"] += 1
        if updated:
            rep.filled["updated"] += 1
        rid = prefix + raw_id
        if side == "a":
            out.append((rid, vals["full_name"], vals["email"], vals["phone"], vals["address"], vals["city"],
                        vals["postcode"], vals["country"], dob, updated))
        else:
            out.append((rid, vals["first_name"], vals["last_name"], vals["email"], vals["phone"], vals["address"],
                        vals["city"], vals["postcode"], vals["country"], dob.strftime("%d/%m/%Y") if dob else None,
                        updated))

    for what, counts in (("email address", emails), ("phone number", phones)):
        shared = [(v, c) for v, c in counts.most_common(5) if c >= 5]
        if shared:
            code = "SHARED_EMAIL" if what.startswith("email") else "SHARED_PHONE"
            rep.issues[code] = {"code": code, "level": "warning", "count": sum(c for _, c in shared),
                                "examples": [{"row": None, "value": f"{v} ({c} customers)"} for v, c in shared],
                                "message": f"Some customers share one {what} (an office line or a placeholder). "
                                           "Shared details can make different people look alike; Drydock holds "
                                           "those groups for a person instead of merging them."}
    if col["id"] is None:
        rep.issues["IDS_GENERATED"] = {"code": "IDS_GENERATED", "level": "info", "count": len(out), "examples": [],
                                       "message": "No ID column was chosen, so Drydock numbered the rows (row000001, "
                                                  "row000002, ...). Re-uploading a changed file renumbers them."}
    report = rep.as_dict()
    report["loaded"] = len(out)
    report["sample"] = [list(map(_show, r)) for r in out[:3]]
    return out, report


def _show(v) -> str | None:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def overlap(rows_a: list[tuple], rows_b: list[tuple]) -> dict:
    """A quick preview of how much the two files have in common, before anything is loaded."""
    ea = {r[2].lower() for r in rows_a if r[2]}
    eb = {r[3].lower() for r in rows_b if r[3]}
    pa = {re.sub(r"\D", "", r[3])[-10:] for r in rows_a if r[3]}
    pb = {re.sub(r"\D", "", r[4])[-10:] for r in rows_b if r[4]}
    return {"shared_emails": len(ea & eb), "shared_phones": len(pa & pb)}


# ------------------------------------------------------------------ storage between steps

def save(side: str, raw: bytes, filename: str) -> dict:
    """Parse and keep an uploaded file until it is loaded (runs/ is git-ignored). Returns its profile."""
    if side not in SIDES:
        raise UploadError("side must be a or b")
    t = parse(raw, filename)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"{side}.csv").write_bytes(raw)
    (UPLOAD_DIR / f"{side}.json").write_text(json.dumps({"filename": filename, "bytes": len(raw)}))
    return profile(t)


def stored(side: str) -> Table | None:
    p = UPLOAD_DIR / f"{side}.csv"
    if not p.exists():
        return None
    meta = json.loads((UPLOAD_DIR / f"{side}.json").read_text())
    return parse(p.read_bytes(), meta["filename"])


def discard(side: str | None = None) -> None:
    """Delete the uploaded copies: after a load, the data lives in Exasol only."""
    for s in ((side,) if side else SIDES):
        for ext in ("csv", "json"):
            (UPLOAD_DIR / f"{s}.{ext}").unlink(missing_ok=True)


def prepare(mapping_a: dict, mapping_b: dict) -> tuple[list[tuple], list[tuple], dict]:
    """Normalise both stored files with the chosen mappings: the rows, and the report for both."""
    ta, tb = stored("a"), stored("b")
    if ta is None or tb is None:
        raise UploadError("Upload both files first: System A and System B.")
    rows_a, rep_a = normalise(ta, "a", mapping_a)
    rows_b, rep_b = normalise(tb, "b", mapping_b)
    report = {"a": {**rep_a, "filename": ta.filename}, "b": {**rep_b, "filename": tb.filename},
              "overlap": overlap(rows_a, rows_b), "can_load": rep_a["can_load"] and rep_b["can_load"]}
    return rows_a, rows_b, report


# ------------------------------------------------------------------ loading

def insert_sql(table: str, cols: tuple[str, ...], rows: list[tuple]) -> str:
    values = ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows)
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES {values}"


SEED_SQL = ("INSERT INTO UPLOADS.GOLDEN_SEED (GOLDEN_ID, FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY, "
            "DATE_OF_BIRTH, SOURCE_A_REF, SOURCE_B_REF, MERGED_A_REFS, MERGED_AT, SURVIVORSHIP) "
            "SELECT 'G-' || CUST_ID, FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY, DATE_OF_BIRTH, "
            "CUST_ID, NULL, NULL, NULL, NULL FROM UPLOADS.SOURCE_A")


def load(db: Db, rows_a: list[tuple], rows_b: list[tuple]) -> dict:
    """Replace UPLOADS with these rows and rebuild GOLDEN's starting snapshot, all or nothing."""
    from . import dataset
    dataset.ensure_schema(db)
    with db.transaction():
        for table, cols, rows in (("UPLOADS.SOURCE_A", A_COLS, rows_a), ("UPLOADS.SOURCE_B", B_COLS, rows_b)):
            db.run(f"DELETE FROM {table}")
            for i in range(0, len(rows), BATCH):
                db.run(insert_sql(table, cols, rows[i:i + BATCH]))
        db.run("DELETE FROM UPLOADS.GOLDEN_SEED")
        seeded = db.run(SEED_SQL)
    got_a = int(db.scalar("SELECT COUNT(*) FROM UPLOADS.SOURCE_A"))
    got_b = int(db.scalar("SELECT COUNT(*) FROM UPLOADS.SOURCE_B"))
    if (got_a, got_b, seeded) != (len(rows_a), len(rows_b), len(rows_a)):
        raise RuntimeError(f"load check failed: A {got_a}/{len(rows_a)}, B {got_b}/{len(rows_b)}, seed {seeded}")
    return {"rows_a": got_a, "rows_b": got_b, "golden_rows": seeded}
