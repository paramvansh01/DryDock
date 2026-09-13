-- Drydock's control-plane tables. Run as DRYDOCK_SVC (scripts/setup.py does this).

CREATE OR REPLACE TABLE DRYDOCK.TABLE_KEYS (
    SCHEMA_NAME   VARCHAR(128),
    TABLE_NAME    VARCHAR(128),
    KEY_COLUMN    VARCHAR(128),          -- single column; NULL => keyless mode
    VERIFIED_AT   TIMESTAMP,
    IS_UNIQUE     BOOLEAN
);

CREATE OR REPLACE TABLE DRYDOCK.BRANCHES (
    BRANCH_ID     VARCHAR(64),           -- also the schema name
    RUN_ID        VARCHAR(64),
    OPENED_AT     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    OPENED_BY     VARCHAR(64),           -- agent | human | script
    CLOSED_AT     TIMESTAMP,
    STATUS        VARCHAR(16),           -- OPEN|MERGED|DISCARDED|BLOCKED|EXPIRED
    PURPOSE       VARCHAR(2000),
    DEFECT_CLASS  VARCHAR(64),           -- the ER match class for this branch
    GATE_ENABLED  BOOLEAN,
    TTL_SECONDS   DECIMAL(9,0) DEFAULT 1800,
    NOTES         VARCHAR(2000)
);

CREATE OR REPLACE TABLE DRYDOCK.MATERIALISATIONS (
    MAT_ID           DECIMAL(18,0) IDENTITY,
    BRANCH_ID        VARCHAR(64),
    SOURCE_SCHEMA    VARCHAR(128),
    SOURCE_TABLE     VARCHAR(128),
    MATERIALISED_AT  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    BASE_ROWCOUNT    DECIMAL(18,0),
    BASE_FINGERPRINT VARCHAR(72),
    COLUMN_SIGNATURE VARCHAR(20000),     -- name:type list at copy time; merge refuses on change
    KEY_MODE         VARCHAR(10),        -- KEYED | KEYLESS
    COPY_MS          DECIMAL(9,0),
    TRIGGERED_BY     VARCHAR(100000)
);

CREATE OR REPLACE TABLE DRYDOCK.BRANCH_OPS (
    OP_ID          DECIMAL(18,0) IDENTITY,
    BRANCH_ID      VARCHAR(64),
    SEQ            DECIMAL(9,0),
    SUBMITTED_AT   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    IDEM_KEY       VARCHAR(72),
    ORIGINAL_SQL   VARCHAR(100000),
    RETARGETED_SQL VARCHAR(100000),
    TABLES_READ    VARCHAR(4000),
    TABLES_WRITTEN VARCHAR(4000),
    ROWS_AFFECTED  DECIMAL(18,0),
    STATUS         VARCHAR(16),          -- OK|ERROR|BLOCKED
    ERROR_TEXT     VARCHAR(4000),
    ACKNOWLEDGED   BOOLEAN DEFAULT FALSE, -- an ERROR op must be acknowledged before merge
    RATIONALE      VARCHAR(4000),
    EXEC_MS        DECIMAL(9,0)
);

CREATE OR REPLACE TABLE DRYDOCK.DIFFS (
    DIFF_ID         DECIMAL(18,0) IDENTITY,
    BRANCH_ID       VARCHAR(64),
    COMPUTED_AT     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    TARGET_SCHEMA   VARCHAR(128),
    TARGET_TABLE    VARCHAR(128),
    MODE            VARCHAR(10),         -- KEYED | KEYLESS
    ROWS_ADDED      DECIMAL(18,0),
    ROWS_CHANGED    DECIMAL(18,0),
    ROWS_DELETED    DECIMAL(18,0),
    ROWS_UNCHANGED  DECIMAL(18,0),
    COLUMN_ORDER    VARCHAR(20000),      -- JSON array; bit i of DIFF_ROWS.COLS_MASK = column i
    COLUMNS_TOUCHED VARCHAR(20000),      -- JSON {col: changed_count}
    SAMPLE          VARCHAR(2000000),    -- JSON, rarest columns first (UI only, never sent to the planner)
    BASE_DRIFTED    BOOLEAN,             -- informational: table fingerprint moved since copy
    DIFF_MS         DECIMAL(9,0)
);

-- Full changed-row keyset: what makes row-level approval and
-- key-scoped drift detection possible. Replaced per branch+table on re-diff.
CREATE OR REPLACE TABLE DRYDOCK.DIFF_ROWS (
    BRANCH_ID      VARCHAR(64),
    TARGET_SCHEMA  VARCHAR(128),
    TARGET_TABLE   VARCHAR(128),
    KEY_VALUE      VARCHAR(200),
    CLASS          VARCHAR(10),          -- ADDED|CHANGED|DELETED
    COLS_MASK      DECIMAL(20,0),        -- bitmask over DIFFS.COLUMN_ORDER (max 60 columns)
    BASE_HASH      VARCHAR(64),          -- row hash in GOLDEN at copy time (NULL for ADDED)
    HEAD_HASH      VARCHAR(64),          -- row hash in the branch (NULL for DELETED)
    APPROVED       BOOLEAN DEFAULT TRUE, -- human may flip; panel SPLIT rows default FALSE
    DEFAULT_REASON VARCHAR(64)           -- why APPROVED started where it did
);

-- Per-row hash of the branch copy AT COPY TIME. The diff classifies against
-- this snapshot (not against current GOLDEN, which other merges may have moved),
-- and key-scoped drift = current GOLDEN hash <> this hash for a touched key.
CREATE OR REPLACE TABLE DRYDOCK.BASE_HASHES (
    BRANCH_ID     VARCHAR(64),
    TARGET_TABLE  VARCHAR(128),
    KEY_VALUE     VARCHAR(200),
    H             VARCHAR(64)
);

CREATE OR REPLACE TABLE DRYDOCK.MERGES (
    MERGE_ID       DECIMAL(18,0) IDENTITY,
    BRANCH_ID      VARCHAR(64),
    REQUESTED_AT   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    REQUESTED_BY   VARCHAR(64),
    IDEM_KEY       VARCHAR(72),
    TOTAL_CHANGED  DECIMAL(18,0),
    CONFIDENCE     DECIMAL(6,4),
    RISK_SCORE     DECIMAL(12,4),
    TIER_AT_TIME   DECIMAL(3,0),
    DECISION       VARCHAR(16),          -- MERGED|PENDING|BLOCKED|REJECTED|APPLYING
    BLOCK_REASON   VARCHAR(512),
    RATIONALE      VARCHAR(4000),
    ROWS_APPLIED   DECIMAL(18,0),
    ROWS_EXCLUDED  DECIMAL(18,0),
    TABLES_MERGED  VARCHAR(4000),
    ARCHIVE_SUFFIX VARCHAR(64),
    SWAP_STAGE     VARCHAR(16),          -- STAGED|ARCHIVED|SWAPPED — crash recovery
    PRE_MERGE_FINGERPRINT  VARCHAR(72),  -- GOLDEN.T just before the swap; unmerge verifies against this
    POST_MERGE_FINGERPRINT VARCHAR(72),
    RESOLVED_AT    TIMESTAMP,
    RESOLVED_BY    VARCHAR(64),
    REJECT_CODE    VARCHAR(32),          -- WRONG_MATCH|TOO_BROAD|INSUFFICIENT_EVIDENCE|POLICY|OTHER
    REJECT_NOTE    VARCHAR(1000),
    UNMERGED_AT    TIMESTAMP,
    UNMERGE_FP_MATCH BOOLEAN
);

CREATE OR REPLACE TABLE DRYDOCK.PRECEDENTS (
    PRECEDENT_ID  DECIMAL(18,0) IDENTITY,
    CREATED_AT    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    DEFECT_CLASS  VARCHAR(64),
    COLUMN_NAME   VARCHAR(128),
    BEFORE_VALUE  VARCHAR(500),          -- rendered A record (stays in the database)
    AFTER_VALUE   VARCHAR(500),          -- rendered B record (stays in the database)
    CONTEXT       VARCHAR(1000),         -- non-identifying comparison summary; this is what retrieval ranks on
    HUMAN_VERDICT VARCHAR(16),           -- APPROVED|REJECTED
    REJECT_CODE   VARCHAR(32),
    HUMAN_NOTE    VARCHAR(1000),
    DECIDED_BY    VARCHAR(64),
    SOURCE_MERGE  DECIMAL(18,0),
    TIMES_CITED   DECIMAL(9,0) DEFAULT 0
);

CREATE OR REPLACE TABLE DRYDOCK.ADJUDICATIONS (
    ADJ_ID          DECIMAL(18,0) IDENTITY,
    RUN_ID          VARCHAR(64),
    PAIR_ID         DECIMAL(18,0),
    INPUT_TEXT      VARCHAR(2000),       -- exactly what the adjudicator saw
    PRECEDENTS_USED VARCHAR(500),        -- JSON array of PRECEDENT_IDs
    VERDICT         VARCHAR(32),         -- SAME_PERSON|DIFFERENT_PERSON|INSUFFICIENT_EVIDENCE
    SCORE           DECIMAL(6,4),
    ADJUDICATOR     VARCHAR(32),         -- exasol_ai_precedent | exasol_ai | gemini_fallback
    LATENCY_MS      DECIMAL(9,0),
    DECIDED_AT      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Gate tiers, sized for entity-resolution volumes.
CREATE OR REPLACE TABLE DRYDOCK.TIER_CONFIG (
    TIER           DECIMAL(3,0),
    LABEL          VARCHAR(64),
    MAX_CHANGED    DECIMAL(18,0),
    MAX_RISK       DECIMAL(12,4),
    ALLOW_DELETES  BOOLEAN,
    MAX_DELETE_PCT DECIMAL(5,2)          -- over this -> PENDING; over DRYDOCK hard ceiling (5%) -> BLOCKED
);

INSERT INTO DRYDOCK.TIER_CONFIG VALUES (0, 'Observe', 0, 0, FALSE, 0);
INSERT INTO DRYDOCK.TIER_CONFIG VALUES (1, 'Propose', 0, 0, FALSE, 0);
INSERT INTO DRYDOCK.TIER_CONFIG VALUES (2, 'Merge (governed)', 30000, 200, TRUE, 1.00);
INSERT INTO DRYDOCK.TIER_CONFIG VALUES (3, 'Merge (broad)', 60000, 2000, TRUE, 5.00);

CREATE OR REPLACE TABLE DRYDOCK.RUNS (
    RUN_ID            VARCHAR(64),
    STARTED_AT        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ENDED_AT          TIMESTAMP,
    TIER              DECIMAL(3,0),
    GATE_ENABLED      BOOLEAN,
    MODE              VARCHAR(16),       -- agent | scripted | replay
    MIN_CONFIDENCE    DECIMAL(6,4),
    REJECTIONS_SEEN   DECIMAL(6,0),
    SEED              DECIMAL(18,0),
    PLAN_FROM_RUN     VARCHAR(64),       -- A/B: treatment replays the control's frozen plan
    METRICS           VARCHAR(20000),    -- JSON: precision/recall/F1/false merges (bench/score.py)
    NOTES             VARCHAR(2000)
);

-- L2.4, extended: KIND distinguishes A<->B from A<->A (dedup) pairs.
CREATE OR REPLACE TABLE DRYDOCK.CANDIDATES (
    PAIR_ID         DECIMAL(18,0) IDENTITY,
    RUN_ID          VARCHAR(64),
    KIND            VARCHAR(2),          -- AB | AA
    A_ID            VARCHAR(64),
    B_ID            VARCHAR(64),         -- for AA: the second A id
    BLOCK_RULES     VARCHAR(400),        -- comma list of every rule that produced the pair
    SIGNALS         VARCHAR(4000),       -- JSON per-signal scores
    SCORE_TOTAL     DECIMAL(6,4),
    VOTE_DETERM     VARCHAR(16),
    VOTE_PROB       VARCHAR(16),
    VOTE_SKEPTIC    VARCHAR(16),
    PANEL_RESULT    VARCHAR(20),         -- UNANIMOUS_MERGE|SPLIT|UNANIMOUS_REJECT
    AI_VERDICT      VARCHAR(32),
    AI_SCORE        DECIMAL(6,4),
    ADJUDICATOR     VARCHAR(32),
    PRECEDENTS_USED VARCHAR(500),
    VERDICT         VARCHAR(16),         -- MERGE|FLAG|REJECT
    MATCH_CLASS     VARCHAR(32),
    RATIONALE       VARCHAR(4000),
    COMPONENT_ID    VARCHAR(64),
    COMPONENT_SIZE  DECIMAL(6,0)
);

-- Raw blocking output before dedup across rules. Cleared per run.
CREATE OR REPLACE TABLE DRYDOCK.CANDIDATES_RAW (
    RUN_ID      VARCHAR(64),
    KIND        VARCHAR(2),
    A_ID        VARCHAR(64),
    B_ID        VARCHAR(64),
    BLOCK_RULE  VARCHAR(64)
);

-- The agent's declared A->B column mapping, per run (profiling output).
CREATE OR REPLACE TABLE DRYDOCK.MAPPINGS (
    RUN_ID      VARCHAR(64),
    DECLARED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    MAPPING     VARCHAR(20000)           -- JSON
);

-- Event log: the UI is a pure function of this stream; persisted for replay.
CREATE OR REPLACE TABLE DRYDOCK.EVENTS (
    EVENT_SEQ   DECIMAL(18,0) IDENTITY,
    RUN_ID      VARCHAR(64),
    BRANCH_ID   VARCHAR(64),
    TS          TIMESTAMP,
    TYPE        VARCHAR(64),
    BODY        VARCHAR(2000000)
);

-- PRIMARY KEY on IDEM_KEY: Exasol enforces it (check V9), so the database arbitrates idempotency races.
CREATE OR REPLACE TABLE DRYDOCK.IDEMPOTENCY (
    IDEM_KEY    VARCHAR(72) PRIMARY KEY,
    TOOL        VARCHAR(64),
    CREATED_AT  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    RESULT_JSON VARCHAR(20000)   -- not RESULT: that is a RESERVED keyword on Exasol (2026.2.0-dev.0,
                                 -- EXA_SQL_KEYWORDS; setup.py failed with 42000 "unexpected RESULT_")
);

INSERT INTO DRYDOCK.TABLE_KEYS (SCHEMA_NAME, TABLE_NAME, KEY_COLUMN) VALUES ('GOLDEN', 'CUSTOMERS', 'GOLDEN_ID');
