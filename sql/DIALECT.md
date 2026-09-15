# DIALECT.md: Exasol dialect evidence

Every Exasol construct Drydock relies on, with the exact statement and the exact
result from a live instance. `scripts/dialect_lint.py` reads this file: opaque
statements and banned patterns are allowed only with a PROVEN entry here.
`scripts/verify.py` appends entries automatically.

Entry format (machine-read by `scripts/dialect_lint.py` — keep it exact):

```
### <construct>
PROVEN <date> | DISPROVEN <date>
SQL:    <exact statement run>
RESULT: <exact output or exact Exasol error text + code>
NOTE:   <optional, one line>
ALLOWS: <optional banned.txt IDs this proves safe, comma-separated>
```

`PROVEN` entries whitelist opaque statements (same leading-keyword signature,
e.g. `RENAME TABLE`) and banned-pattern IDs named in `ALLOWS:`.
Only results from a live instance go under PROVEN/DISPROVEN.

---

## Tool facts (sqlglot / pyexasol — not Exasol facts, not PROVEN entries)

### tool: sqlglot exasol parser is permissive
TOOL-FACT 2026-09-10 sqlglot 30.18.0
SQL:    INSERT INTO t (a) VALUES (1) RETURNING a  |  ... ON CONFLICT DO NOTHING  |  a ILIKE 'x'  |  a::int
RESULT: all four parse under read="exasol" without error
NOTE:   Gate 1's "pg-ok but exa-fails" check almost never fires. Gate 2 + Gate 3 carry the weight.

### tool: sqlglot exasol writer rewrites silently
TOOL-FACT 2026-09-10 sqlglot 30.18.0
SQL:    CREATE TABLE t (id DECIMAL(18,0) IDENTITY, ...)  |  SUBSTR(h,1,15)  |  a TEXT  |  a::int
RESULT: -> AUTO_INCREMENT  |  -> SUBSTRING(h, 1, 15)  |  -> LONG VARCHAR  |  -> CAST(a AS INT)
NOTE:   never round-trip DDL through sqlglot; lint every rendered string, not just the source.

### tool: sqlglot opaque statements
TOOL-FACT 2026-09-10 sqlglot 30.18.0
SQL:    RENAME TABLE s.t1 TO t2  |  CREATE OR REPLACE PYTHON3 SCALAR SCRIPT ...
RESULT: parsed as exp.Command (unparsed passthrough). OPEN SCHEMA parses as exp.Use.
NOTE:   lint requires a PROVEN entry with the same keyword signature for these.

### tool: pyexasol 2.4.0 defaults
TOOL-FACT 2026-09-10 pyexasol 2.4.0
SQL:    (inspection of pyexasol.constant / ExaConnection.__init__)
RESULT: DEFAULT_AUTOCOMMIT=True; encryption=True; cert validation REQUIRED unless DSN is host/FINGERPRINT:port or host/nocertcheck:port
NOTE:   every connection in this repo passes autocommit explicitly.

---

## Proven / disproven on the live instance

### CREATE TABLE AS SELECT
PROVEN 2026-09-12 (auto, verify.py)
SQL:    CREATE TABLE PROBE_SCRATCH.V3_CTAS AS SELECT * FROM PROBE_SCRATCH.V3_SRC WHERE GOLDEN_ID < 'G00036600'
RESULT: OK, 36600 rows affected, 87ms
NOTE:   200k-row CTAS median 138ms

### Same-schema RENAME (unqualified target)
PROVEN 2026-09-12 (auto, verify.py)
SQL:    RENAME TABLE PROBE_SCRATCH.V5_A TO V5_B
RESULT: OK, 0 rows affected, 25ms
NOTE:   RENAME TABLE s.t1 TO t2 — drydock/merge.py uses this form when PROVEN

### Same-schema RENAME (qualified target)
PROVEN 2026-09-12 (auto, verify.py)
SQL:    RENAME TABLE PROBE_SCRATCH.V5_B TO PROBE_SCRATCH.V5_C
RESULT: OK, 0 rows affected, 25ms
NOTE:   RENAME TABLE s.t1 TO s.t2

### Cross-schema RENAME
DISPROVEN 2026-09-12 (auto, verify.py)
SQL:    RENAME TABLE PROBE_SCRATCH.V5_C TO PROBE_SCRATCH2.V5_X
RESULT: [42000] invalid identifier chain for new name [line 1, column 55] (Session: 1876139910926958592)
NOTE:   expected DISPROVEN (docs: cannot move objects between schemas)

### CREATE PYTHON3 SCALAR SCRIPT
DISPROVEN 2026-09-12 (auto, verify.py)
SQL:    CREATE OR REPLACE PYTHON3 SCALAR SCRIPT PROBE_SCRATCH.HELLO(x VARCHAR(100)) RETURNS VARCHAR(2000) AS
def run(ctx):
    return 'hi ' + ctx.x

RESULT: [42000] No usable script language container is installed for the requested language. Install the required script language package and try again. (Session: 1876139910926958592)
NOTE:   script body is sent WITHOUT the client-side / terminator

### IDENTITY columns
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT ID, V FROM PROBE_SCRATCH.V9_I ORDER BY V
RESULT: ID | V / 1 | a / 2 | b / (2 rows, 2ms)
NOTE:   generated on INSERT without the column

### PRIMARY KEY enforcement (default)
DISPROVEN 2026-09-12 (auto, verify.py)
SQL:    INSERT INTO PROBE_SCRATCH.V9_P VALUES (1, 'b')
RESULT: [27002] constraint violation - primary key (SYS_132778252552708450200004707328 on table V9_P) (Session: 1876139910926958592)
NOTE:   PROVEN here means the duplicate INSERT SUCCEEDED, i.e. NOT enforced

### DROP SCHEMA ... CASCADE
PROVEN 2026-09-12 (auto, verify.py)
SQL:    DROP SCHEMA PROBE_BRANCH_V11 CASCADE
RESULT: OK, 0 rows affected, 33ms
NOTE:   branch schema with 200k-row table dropped in 33ms

### REGEXP_LIKE infix predicate
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT CASE WHEN 'abc' REGEXP_LIKE 'a.c' THEN 1 ELSE 0 END
RESULT: CASE WHEN ('abc' REGEXP_LIKE 'a.c') THEN 1 ELSE 0 END / 1 / (1 rows, 2ms)
NOTE:   drydock/er.py uses the infix form when PROVEN

### REGEXP_LIKE function form
DISPROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT CASE WHEN REGEXP_LIKE('abc', 'a.c') THEN 1 ELSE 0 END
RESULT: [42000] syntax error, unexpected REGEXP_LIKE_ [line 1, column 18] (Session: 1876139910926958592)

### REGEXP_REPLACE
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT REGEXP_REPLACE('(555) 010-2233', '[^0-9]', '')
RESULT: REGEXP_REPLACE('(555) 010-2233','[^0-9]',NULL) / 5550102233 / (1 rows, 2ms)

### REGEXP_SUBSTR
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT REGEXP_SUBSTR('JOHN SMITH JR', '[^ ]+$'), REGEXP_SUBSTR('a@b.com', '^[^@]+')
RESULT: REGEXP_SUBSTR('JOHN SMITH JR','[^ ]+$') | REGEXP_SUBSTR('a@b.com','^[^@]+') / JR | a / (1 rows, 2ms)

### YEARS_BETWEEN
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT YEARS_BETWEEN(DATE '1989-07-14', DATE '1961-03-02')
RESULT: YEARS_BETWEEN((TO_DATE('1989-07-14','YYYY-MM-DD')),(TO_DATE('1961-03-02','YYYY-MM-DD'))) / 28.36559139784946 / (1 rows, 2ms)

### SECONDS_BETWEEN
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT SECONDS_BETWEEN(TIMESTAMP '2026-09-10 12:00:10', TIMESTAMP '2026-09-10 12:00:00')
RESULT: SECONDS_BETWEEN(TIMESTAMP '2026-09-10 12:00:10',TIMESTAMP '2026-09-10 12:00:00') / 10 / (1 rows, 1ms)

### RIGHT
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT RIGHT('5550102233', 7)
RESULT: RIGHT('5550102233',7) / 0102233 / (1 rows, 2ms)

### SUBSTR
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT SUBSTR('abcdef', 2, 3)
RESULT: SUBSTR('abcdef',2,3) / bcd / (1 rows, 2ms)

### TO_CHAR date fmt
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT TO_CHAR(DATE '2026-09-10', 'YYYY-MM-DD')
RESULT: TO_CHAR((TO_DATE('2026-09-10','YYYY-MM-DD')),'YYYY-MM-DD') / 2026-09-10 / (1 rows, 1ms)

### TO_DATE fmt
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT TO_DATE('14/07/1989', 'DD/MM/YYYY')
RESULT: TO_DATE('14/07/1989','DD/MM/YYYY') / 1989-07-14 / (1 rows, 1ms)

### FULL OUTER JOIN
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT COALESCE(a.K, b.K) AS K, CASE WHEN a.K IS NULL THEN 'ADDED' WHEN b.K IS NULL THEN 'DELETED' WHEN a.V <> b.V THEN 'CHANGED' ELSE 'UNCHANGED' END AS C FROM PROBE_SCRATCH.L4_A a FULL OUTER JOIN PROBE_SCRATCH.L4_B b ON a.K = b.K ORDER BY 1
RESULT: K | C / 1 | DELETED / 2 | UNCHANGED / 3 | CHANGED / ... 1 more rows / (4 rows, 33ms)

### MERGE INTO
PROVEN 2026-09-12 (auto, verify.py)
SQL:    MERGE INTO PROBE_SCRATCH.L4_A t USING PROBE_SCRATCH.L4_B s ON t.K = s.K WHEN MATCHED THEN UPDATE SET t.V = s.V WHEN NOT MATCHED THEN INSERT VALUES (s.K, s.V)
RESULT: OK, 3 rows affected, 29ms

### LIMIT n OFFSET m
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT K FROM PROBE_SCRATCH.L4_A ORDER BY K LIMIT 2 OFFSET 1
RESULT: K / 2 / 3 / (2 rows, 2ms)
ALLOWS: LIMIT_OFFSET

### Double-quoted identifier
PROVEN 2026-09-12 (auto, verify.py)
SQL:    SELECT 1 AS "lower_case"
RESULT: lower_case / 1 / (1 rows, 2ms)

### CREATE USER ... IDENTIFIED BY
PROVEN 2026-09-12 (auto, verify.py)
SQL:    CREATE USER PROBE_U_L7 IDENTIFIED BY "***"
RESULT: OK, 0 rows affected, 26ms
NOTE:   password in double quotes
ALLOWS: DQUOTE_IDENT

### GRANT / REVOKE on schema
PROVEN 2026-09-12 (auto, verify.py)
SQL:    GRANT SELECT ON SCHEMA PROBE_SCRATCH TO PROBE_U_L7
RESULT: OK, 0 rows affected, 19ms

### PRIMARY KEY enforced by default (duplicate INSERT refused)
PROVEN 2026-09-13 (auto, verify.py)
SQL:    INSERT INTO PROBE_SCRATCH.V9_P VALUES (1, 'b')
RESULT: [27002] constraint violation - primary key (SYS_132778252691082074890007398400 on table V9_P) (Session: 1876154425032048640)
NOTE:   PROVEN = the duplicate was refused with 27002. Supersedes the 2026-09-12 entry titled 'PRIMARY KEY enforcement (default)', whose DISPROVEN label meant the same fact, inverted.

### REGEXP_REPLACE with the Unicode letter class \p{L}
PROVEN 2026-09-15 (scripts/probe.py)
SQL:    SELECT UPPER(TRIM(REGEXP_REPLACE('José Müller-O''Neil 李小龍 Łukasz', '[^\p{L} '']', ''))) AS N, UPPER(TRIM(REGEXP_REPLACE('José Müller-O''Neil 李小龍 Łukasz', '[^A-Za-z '']', ''))) AS OLD_N
RESULT: JOSÉ MÜLLERO'NEIL 李小龍 ŁUKASZ | JOS MLLERO'NEIL  UKASZ
NOTE:   drydock/er.py name normalisation. The ASCII class it replaced dropped accented letters and erased names in other scripts.

### REGEXP_REPLACE with Unicode letters and digits \p{L}\p{N}
PROVEN 2026-09-15 (scripts/probe.py)
SQL:    SELECT UPPER(REGEXP_REPLACE('Königstraße 5, Wohnung ٣ — 北京路12号', '[^\p{L}\p{N}]', '')) AS NEW_N, UPPER(REGEXP_REPLACE('Königstraße 5, Wohnung ٣ — 北京路12号', '[^A-Za-z0-9]', '')) AS OLD_N
RESULT: KÖNIGSTRAßE5WOHNUNG٣北京路12号 | KNIGSTRAE5WOHNUNG12
NOTE:   drydock/er.py street normalisation (STREET_N).

### CREATE TABLE IF NOT EXISTS (idempotent)
PROVEN 2026-09-15 (scripts/probe.py)
SQL:    CREATE TABLE IF NOT EXISTS PROBE_SCRATCH.UP_T (CUST_ID VARCHAR(64), FULL_NAME VARCHAR(200), DATE_OF_BIRTH DATE, CREATED_AT TIMESTAMP)
RESULT: OK, 0 rows affected, 29ms; run again: OK, 0 rows affected, 1ms (no error, table unchanged)
NOTE:   sql/03_uploads.sql, applied at every orchestrator startup.

### Multi-row INSERT ... VALUES with DATE and TIMESTAMP literals
PROVEN 2026-09-15 (scripts/probe.py)
SQL:    INSERT INTO PROBE_SCRATCH.UP_T (CUST_ID, FULL_NAME, DATE_OF_BIRTH, CREATED_AT) VALUES ('A-1', 'José O''Neil', DATE '1980-02-29', TIMESTAMP '2024-01-01 10:00:00.000'), ('A-2', NULL, NULL, NULL)
RESULT: OK, 2 rows affected, 24ms
NOTE:   drydock/uploads.py loads rows this way, 500 per statement; DRYDOCK_SVC holds no IMPORT privilege (check V3).

### NULL in || concatenation, and the empty string
PROVEN 2026-09-15 (scripts/probe.py)
SQL:    SELECT NULL || ' ' || 'Smith' AS NULL_FIRST, 'Jo' || ' ' || NULL AS NULL_LAST, TRIM(NULL || ' ' || 'Smith') AS TRIMMED, CASE WHEN '' IS NULL THEN 'empty string is NULL' ELSE 'empty string is a value' END AS EMPTY
RESULT:  Smith | Jo  | Smith | empty string is NULL
NOTE:   NULL concatenates as ''. An uploaded System B record with only a last name keeps its name through FIRST_NAME || ' ' || LAST_NAME.

### Unmerge LIFO by application order (self-join on DRYDOCK.MERGES)
PROVEN 2026-09-15 (live data, run_202609142149)
SQL:    SELECT l.MERGE_ID FROM DRYDOCK.MERGES l JOIN DRYDOCK.MERGES m ON m.MERGE_ID = 87 WHERE l.MERGE_ID <> m.MERGE_ID AND l.DECISION = 'MERGED' AND l.UNMERGED_AT IS NULL AND (l.RESOLVED_AT > m.RESOLVED_AT OR (l.RESOLVED_AT = m.RESOLVED_AT AND l.MERGE_ID > m.MERGE_ID)) AND (',' || l.TABLES_MERGED || ',') LIKE '%,CUSTOMERS,%' ORDER BY l.RESOLVED_AT DESC, l.MERGE_ID DESC LIMIT 1
RESULT: 86
NOTE:   merges applied in the order 85, 87, 84, 86. The previous rule (MIN(MERGE_ID) > 87) returned NULL and allowed undoing 87, which would have discarded 84 and 86.
