-- ======================================================================
-- The privilege floor.
-- Run as DRYDOCK_SVC after GOLDEN.CUSTOMERS exists (scripts/setup.py does this).
--
-- Identities (created by the admin with scripts/create_users.py):
--   DRYDOCK_AGENT  CREATE SESSION + SELECT on the read surfaces below. NOTHING ELSE.
--   DRYDOCK_SVC    owns GOLDEN, GOLDEN_V, DRYDOCK, ER_WORK and every BR_* schema.
--                  The only writer to GOLDEN, and only inside drydock/merge.py.
--
-- The agent's GOLDEN read goes through a VIEW, not a grant on GOLDEN.CUSTOMERS:
-- the stage-then-swap merge renames the table object, and an object-level
-- grant may follow the renamed object into CUSTOMERS__ARCH_<id> (verify V14).
-- With the view, the agent holds zero privileges on GOLDEN, so the archive and
-- staging tables are unreachable by construction.
-- ======================================================================

CREATE OR REPLACE VIEW GOLDEN_V.CUSTOMERS AS
    SELECT GOLDEN_ID, FULL_NAME, EMAIL, PHONE, ADDR_LINE, CITY, POSTCODE, COUNTRY,
           DATE_OF_BIRTH, SOURCE_A_REF, SOURCE_B_REF, MERGED_A_REFS, MERGED_AT, SURVIVORSHIP
    FROM GOLDEN.CUSTOMERS;

GRANT SELECT ON SCHEMA GOLDEN_V TO DRYDOCK_AGENT;

-- SOURCE_A / SOURCE_B SELECT for the agent is granted by the admin in
-- bench/sql/10_sources.sql (the admin owns those schemas).
--
-- Deliberately NOT granted to DRYDOCK_AGENT, anywhere:
--   INSERT, UPDATE, DELETE, MERGE, TRUNCATE, CREATE *, DROP *, ALTER *, GRANT *
--   any privilege on GOLDEN, DRYDOCK, ER_WORK, BENCH, or any BR_* schema.
-- Test 18.4 asserts each of these fails as DRYDOCK_AGENT.
