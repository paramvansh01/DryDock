-- Uploaded datasets. Run as DRYDOCK_SVC; idempotent, so the orchestrator applies it at startup
-- (drydock/dataset.py) and scripts/setup.py applies it after 01_drydock_ddl.sql.
--
-- A person's own two files are loaded here, never into SOURCE_A / SOURCE_B: those belong to the
-- admin and hold the demo data that BENCH (the answer key) scores. The tables have exactly the
-- demo sources' columns, so matching, branching, the gate and merges run on uploaded data unchanged.
-- Branch SQL may read UPLOADS but never write it (drydock/retarget.py lists it READ_ONLY), and the
-- agent holds no grant on it.

CREATE SCHEMA IF NOT EXISTS UPLOADS;

-- System A: the list GOLDEN starts from. CUST_ID is the file's own id, prefixed "A-".
CREATE TABLE IF NOT EXISTS UPLOADS.SOURCE_A (
    CUST_ID       VARCHAR(64),
    FULL_NAME     VARCHAR(200),
    EMAIL         VARCHAR(200),
    PHONE         VARCHAR(50),
    ADDR_LINE     VARCHAR(300),
    CITY          VARCHAR(100),
    POSTCODE      VARCHAR(20),
    COUNTRY       VARCHAR(60),
    DATE_OF_BIRTH DATE,
    CREATED_AT    TIMESTAMP
);

-- System B: folded into GOLDEN. CLIENT_REF is the file's own id, prefixed "B-", so it can never
-- collide with an A id when an unmatched B record becomes a new golden row ('G-' || CLIENT_REF).
CREATE TABLE IF NOT EXISTS UPLOADS.SOURCE_B (
    CLIENT_REF VARCHAR(64),
    FIRST_NAME VARCHAR(100),
    LAST_NAME  VARCHAR(100),
    EMAIL_ADDR VARCHAR(200),
    MOBILE     VARCHAR(50),
    STREET     VARCHAR(300),
    TOWN       VARCHAR(100),
    ZIP        VARCHAR(20),
    NATION     VARCHAR(60),
    DOB        VARCHAR(10),        -- 'DD/MM/YYYY', the demo's B format, so one mapping serves both
    LAST_SEEN  TIMESTAMP
);

-- GOLDEN's starting point for this dataset: one row per System A record, as BENCH.GOLDEN_CLEAN is
-- for the demo. A reset or a dataset switch recreates GOLDEN.CUSTOMERS from here (merge.reseed).
CREATE TABLE IF NOT EXISTS UPLOADS.GOLDEN_SEED (
    GOLDEN_ID     VARCHAR(64),
    FULL_NAME     VARCHAR(200),
    EMAIL         VARCHAR(200),
    PHONE         VARCHAR(50),
    ADDR_LINE     VARCHAR(300),
    CITY          VARCHAR(100),
    POSTCODE      VARCHAR(20),
    COUNTRY       VARCHAR(60),
    DATE_OF_BIRTH DATE,
    SOURCE_A_REF  VARCHAR(64),
    SOURCE_B_REF  VARCHAR(64),
    MERGED_A_REFS VARCHAR(2000),
    MERGED_AT     TIMESTAMP,
    SURVIVORSHIP  VARCHAR(2000)
);

-- Which dataset GOLDEN currently holds: one row, replaced on every switch. No row = the demo.
-- DETAIL is JSON: the file names, the column mapping and the data-quality report.
CREATE TABLE IF NOT EXISTS DRYDOCK.DATASET (
    NAME         VARCHAR(20),       -- demo | upload
    LABEL        VARCHAR(400),
    ACTIVATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ROWS_A       DECIMAL(18,0),
    ROWS_B       DECIMAL(18,0),
    DETAIL       VARCHAR(2000000)
);
