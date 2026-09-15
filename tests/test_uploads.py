"""Bring-your-own-data: parsing, suggested mappings, normalisation and the quality report. Offline."""

from __future__ import annotations

import datetime as dt

import pytest

from drydock import uploads as u
from drydock.lintguard import clean

A_CSV = ("Customer No;Name;E-mail;Tel.;Street Address;Town;Post Code;Birth Date;Last Modified;Company Name\n"
         "1001;Müller, José;jose@example.com;+44 20 7946 0958;12 High St;London;SW1A 1AA;29/02/1980;2024-03-01 10:00;Acme\n"
         "1002;Mary O'Neil;n/a;07700 900123;3 Low Rd;Leeds;LS1 4AB;31/12/1975;;\n")
B_CSV = ("id,first_name,last_name,email,mobile,address,city,zip,dob,updated_at\n"
         "1001,Jose,Muller,JOSE@example.com,020 7946 0958,12 High Street,London,SW1A1AA,1980-02-29,2025-01-05T09:30:00Z\n"
         "7,Li,Wei,li@example.cn,+86 10 1234 5678,,Beijing,,,\n")


def table(text: str, name: str = "f.csv", enc: str = "utf-8") -> u.Table:
    return u.parse(text.encode(enc), name)


# ------------------------------------------------------------------ parse

def test_parse_detects_semicolons_and_windows_encoding():
    t = table(A_CSV, enc="cp1252")
    assert (t.delimiter, t.encoding, len(t.rows)) == (";", "Windows-1252", 2)
    assert t.rows[0][1] == "Müller, José"


def test_parse_utf8_with_bom_and_blank_lines():
    t = u.parse(("\ufeff" + B_CSV + "\n\n").encode("utf-8"), "b.csv")
    assert t.header[0] == "id" and t.encoding == "UTF-8" and len(t.rows) == 2


@pytest.mark.parametrize("raw, words", [
    (b"", "empty"),
    (b"a,b,c\n", "no customers"),
    (b"PK\x03\x04rest-of-an-xlsx", "Excel"),
    (b"onlyonecolumn\nx\ny\n", "one column"),
])
def test_parse_refuses_with_a_fix(raw, words):
    with pytest.raises(u.UploadError, match=words):
        u.parse(raw, "x.csv")


def test_ragged_rows_are_padded_and_reported():
    t = table("id,name,email\n1,Ann\n2,Bob,b@x.io,extra\n")
    assert t.ragged == 2 and all(len(r) == 3 for r in t.rows)


def test_duplicate_and_empty_headers_get_distinct_names():
    assert u._clean_header(["Name", "", "Name"]) == ["Name", "Column 2", "Name (2)"]


# ------------------------------------------------------------------ suggestions

def test_suggested_mapping_reads_real_world_headers():
    s = u.profile(table(A_CSV, enc="cp1252"))["suggested"]
    assert s["id"] == "Customer No" and s["full_name"] == "Name" and s["email"] == "E-mail"
    assert s["phone"] == "Tel." and s["postcode"] == "Post Code" and s["dob"] == "Birth Date"
    assert "Company Name" not in s.values()          # not a person's name


def test_split_name_headers_map_to_first_and_last():
    s = u.profile(table(B_CSV))["suggested"]
    assert (s["first_name"], s["last_name"], s["full_name"]) == ("first_name", "last_name", None)
    assert s["phone"] == "mobile" and s["updated"] == "updated_at"


def test_unlabelled_email_column_found_by_its_values():
    s = u.profile(table("ref,who,contact\n1,Ann,ann@x.io\n2,Bob,bob@y.org\n3,Cy,cy@z.net\n"))["suggested"]
    assert s["email"] == "contact"


def test_a_short_word_inside_a_longer_header_is_not_a_match():
    s = u.profile(table("Mailing Address,Created By,Full Name\n1 High St,admin,Ann Lee\n"))["suggested"]
    assert s["email"] is None and s["updated"] is None and s["address"] == "Mailing Address"


# ------------------------------------------------------------------ dates

def test_date_format_ambiguity_is_detected():
    g = u.guess_date_format(["03/04/1985", "01/02/1990", "12/11/2001"])
    assert g["ambiguous"] and {o["format"] for o in g["options"]} >= {"%d/%m/%Y", "%m/%d/%Y"}


def test_a_day_above_twelve_settles_day_first():
    g = u.guess_date_format(["03/04/1985", "25/12/1990"])
    assert g["format"] == "%d/%m/%Y" and not g["ambiguous"]


def test_two_digit_birth_years_are_last_century():
    assert u.parse_date("01/02/45", "%d/%m/%y") == dt.date(1945, 2, 1)


def test_timestamps_with_zone_become_utc():
    assert u.parse_timestamp("2025-01-05T09:30:00+02:00", None) == dt.datetime(2025, 1, 5, 7, 30)


# ------------------------------------------------------------------ normalise + report

def mapping(t: u.Table, **formats) -> dict:
    return {"fields": u.profile(t)["suggested"], "formats": formats}


def test_ids_are_namespaced_so_the_two_files_never_collide():
    ta, tb = table(A_CSV, enc="cp1252"), table(B_CSV)
    ra, _ = u.normalise(ta, "a", mapping(ta, dob="%d/%m/%Y"))
    rb, _ = u.normalise(tb, "b", mapping(tb))
    assert ra[0][0] == "A-1001" and rb[0][0] == "B-1001"


def test_system_a_row_shape_and_name_order():
    ta = table(A_CSV, enc="cp1252")
    ra, rep = u.normalise(ta, "a", mapping(ta, dob="%d/%m/%Y"))
    assert len(ra[0]) == len(u.A_COLS)
    assert ra[0][1] == "José Müller"                         # "Surname, First" reads naturally
    assert ra[0][8] == dt.date(1980, 2, 29) and ra[0][9] == dt.datetime(2024, 3, 1, 10, 0)
    assert ra[1][2] is None                                  # "n/a" is a placeholder, not an email
    assert rep["can_load"]


def test_system_b_row_shape_uses_the_demo_b_formats():
    tb = table(B_CSV)
    rb, _ = u.normalise(tb, "b", mapping(tb))
    assert len(rb[0]) == len(u.B_COLS)
    assert rb[0][1:3] == ("Jose", "Muller") and rb[0][9] == "29/02/1980"   # DOB as 'DD/MM/YYYY' text


def test_a_full_name_is_split_for_system_b():
    tb = table("ref,name\n1,Anna Maria Lopez\n")
    rb, _ = u.normalise(tb, "b", {"fields": {"id": "ref", "full_name": "name"}})
    assert rb[0][1:3] == ("Anna Maria", "Lopez")


def test_duplicate_ids_block_the_load_with_a_fix():
    t = table("id,name\n1,Ann\n1,Bob\n")
    rows, rep = u.normalise(t, "a", {"fields": {"id": "id", "full_name": "name"}})
    assert not rep["can_load"] and rep["issues"][0]["code"] == "DUPLICATE_ID"
    assert "number the rows" in rep["issues"][0]["message"]


def test_no_id_column_numbers_the_rows():
    t = table("name,email\nAnn,a@x.io\nBob,b@x.io\n")
    rows, rep = u.normalise(t, "a", {"fields": {"full_name": "name", "email": "email"}})
    assert [r[0] for r in rows] == ["A-row000001", "A-row000002"] and rep["can_load"]


def test_no_name_column_is_an_error():
    t = table("id,email\n1,a@x.io\n")
    _, rep = u.normalise(t, "a", {"fields": {"id": "id", "email": "email"}})
    assert not rep["can_load"] and any(i["code"] == "NO_NAME_COLUMN" for i in rep["issues"])


def test_bad_values_are_blanked_and_counted():
    t = table("id,name,email,phone,dob\n1,Ann,not-an-email,12,31/02/1990\n2,Bob,b@x.io,0161 496 0000,01/01/2999\n")
    rows, rep = u.normalise(t, "a", {"fields": {"id": "id", "full_name": "name", "email": "email", "phone": "phone",
                                                "dob": "dob"}, "formats": {"dob": "%d/%m/%Y"}})
    codes = {i["code"]: i["count"] for i in rep["issues"]}
    assert codes["BAD_EMAIL"] == 1 and codes["BAD_PHONE"] == 1 and codes["BAD_DOB"] == 2
    assert rows[0][2] is None and rows[0][3] is None and rows[1][8] is None


def test_overlong_values_are_cut_to_the_column_width():
    t = table(f"id,name,city\n1,Ann,{'X' * 150}\n")
    rows, rep = u.normalise(t, "a", {"fields": {"id": "id", "full_name": "name", "city": "city"}})
    assert len(rows[0][5]) == 100 and any(i["code"] == "TRUNCATED" for i in rep["issues"])


def test_shared_contact_details_are_flagged():
    body = "".join(f"{i},Person {i},info@acme.com\n" for i in range(6))
    _, rep = u.normalise(table("id,name,email\n" + body), "a",
                         {"fields": {"id": "id", "full_name": "name", "email": "email"}})
    assert any(i["code"] == "SHARED_EMAIL" for i in rep["issues"])


def test_unknown_column_in_mapping_is_refused():
    with pytest.raises(u.UploadError, match="not in the file"):
        u.normalise(table("id,name\n1,Ann\n"), "a", {"fields": {"id": "nope"}})


def test_overlap_counts_shared_emails_and_phones_across_files():
    ta, tb = table(A_CSV, enc="cp1252"), table(B_CSV)
    ra, _ = u.normalise(ta, "a", mapping(ta, dob="%d/%m/%Y"))
    rb, _ = u.normalise(tb, "b", mapping(tb))
    assert u.overlap(ra, rb) == {"shared_emails": 1, "shared_phones": 1}


# ------------------------------------------------------------------ SQL passes the firewall

def test_insert_and_seed_sql_are_dialect_clean():
    ta, tb = table(A_CSV, enc="cp1252"), table(B_CSV)
    ra, _ = u.normalise(ta, "a", mapping(ta, dob="%d/%m/%Y"))
    rb, _ = u.normalise(tb, "b", mapping(tb))
    clean(u.insert_sql("UPLOADS.SOURCE_A", u.A_COLS, ra), "test")
    clean(u.insert_sql("UPLOADS.SOURCE_B", u.B_COLS, rb), "test")
    clean(u.SEED_SQL, "test")
