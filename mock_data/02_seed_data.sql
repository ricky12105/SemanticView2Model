-- ============================================================================
-- File:    mock_data/02_seed_data.sql
-- Purpose: Deterministic seed of P&C insurance actuarial data.
-- Notes:
--   * Volumes target a small POC: ~6y dates, 50 states, 5 LOBs, 6 coverages,
--     500 agents, 10k policyholders, 50k policies, ~250k premium txns, ~30k claims,
--     ~600k loss-reserve snapshots.
--   * Determinism: UNIFORM(low, high, RANDOM(<seed>)) and SEQ4 give reproducible
--     results across runs (subject to micro-batch ordering — acceptable for a POC).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- DIM_DATE  (6 years: 2020-01-01 .. 2025-12-31)
-- ---------------------------------------------------------------------------
INSERT INTO DIM_DATE
SELECT
    TO_NUMBER(TO_CHAR(d, 'YYYYMMDD'))                                AS DATE_KEY,
    d                                                                 AS DATE_VAL,
    EXTRACT(DAY     FROM d)                                           AS DAY_OF_MONTH,
    EXTRACT(MONTH   FROM d)                                           AS MONTH_NUM,
    TO_CHAR(d, 'MMMM')                                                AS MONTH_NAME,
    EXTRACT(QUARTER FROM d)                                           AS QUARTER_NUM,
    EXTRACT(YEAR    FROM d)                                           AS YEAR_NUM,
    TO_CHAR(d, 'YYYY-MM')                                             AS YEAR_MONTH,
    (d = LAST_DAY(d))                                                 AS IS_MONTH_END,
    (d = LAST_DAY(d, 'QUARTER'))                                      AS IS_QUARTER_END
FROM (
    SELECT DATEADD(day, SEQ4(), '2020-01-01'::DATE) AS d
    FROM TABLE(GENERATOR(ROWCOUNT => 2192))   -- 6 * 365 + 2 (leap)
) WHERE d <= '2025-12-31';

-- ---------------------------------------------------------------------------
-- DIM_GEOGRAPHY  (50 US states)
-- ---------------------------------------------------------------------------
INSERT INTO DIM_GEOGRAPHY VALUES
 ( 1,'AL','Alabama','South','Atlantic Hurricane'),
 ( 2,'AK','Alaska','West',NULL),
 ( 3,'AZ','Arizona','West','Wildfire'),
 ( 4,'AR','Arkansas','South','Tornado Alley'),
 ( 5,'CA','California','West','Wildfire'),
 ( 6,'CO','Colorado','West','Wildfire'),
 ( 7,'CT','Connecticut','Northeast',NULL),
 ( 8,'DE','Delaware','Northeast',NULL),
 ( 9,'FL','Florida','South','Atlantic Hurricane'),
 (10,'GA','Georgia','South','Atlantic Hurricane'),
 (11,'HI','Hawaii','West','Pacific Hurricane'),
 (12,'ID','Idaho','West','Wildfire'),
 (13,'IL','Illinois','Midwest','Tornado Alley'),
 (14,'IN','Indiana','Midwest','Tornado Alley'),
 (15,'IA','Iowa','Midwest','Tornado Alley'),
 (16,'KS','Kansas','Midwest','Tornado Alley'),
 (17,'KY','Kentucky','South','Tornado Alley'),
 (18,'LA','Louisiana','South','Atlantic Hurricane'),
 (19,'ME','Maine','Northeast',NULL),
 (20,'MD','Maryland','Northeast',NULL),
 (21,'MA','Massachusetts','Northeast',NULL),
 (22,'MI','Michigan','Midwest',NULL),
 (23,'MN','Minnesota','Midwest',NULL),
 (24,'MS','Mississippi','South','Atlantic Hurricane'),
 (25,'MO','Missouri','Midwest','Tornado Alley'),
 (26,'MT','Montana','West','Wildfire'),
 (27,'NE','Nebraska','Midwest','Tornado Alley'),
 (28,'NV','Nevada','West','Wildfire'),
 (29,'NH','New Hampshire','Northeast',NULL),
 (30,'NJ','New Jersey','Northeast',NULL),
 (31,'NM','New Mexico','West','Wildfire'),
 (32,'NY','New York','Northeast',NULL),
 (33,'NC','North Carolina','South','Atlantic Hurricane'),
 (34,'ND','North Dakota','Midwest',NULL),
 (35,'OH','Ohio','Midwest','Tornado Alley'),
 (36,'OK','Oklahoma','South','Tornado Alley'),
 (37,'OR','Oregon','West','Wildfire'),
 (38,'PA','Pennsylvania','Northeast',NULL),
 (39,'RI','Rhode Island','Northeast',NULL),
 (40,'SC','South Carolina','South','Atlantic Hurricane'),
 (41,'SD','South Dakota','Midwest',NULL),
 (42,'TN','Tennessee','South','Tornado Alley'),
 (43,'TX','Texas','South','Atlantic Hurricane'),
 (44,'UT','Utah','West','Wildfire'),
 (45,'VT','Vermont','Northeast',NULL),
 (46,'VA','Virginia','South',NULL),
 (47,'WA','Washington','West','Wildfire'),
 (48,'WV','West Virginia','South',NULL),
 (49,'WI','Wisconsin','Midwest',NULL),
 (50,'WY','Wyoming','West','Wildfire');

-- ---------------------------------------------------------------------------
-- DIM_PRODUCT_LOB
-- ---------------------------------------------------------------------------
INSERT INTO DIM_PRODUCT_LOB VALUES
 (1,'AUTO',     'Personal Auto',          'Personal'),
 (2,'HOME',     'Homeowners',             'Personal'),
 (3,'UMB',      'Personal Umbrella',      'Personal'),
 (4,'COMM',     'Commercial Property',    'Commercial'),
 (5,'WC',       'Workers Compensation',   'Commercial');

-- ---------------------------------------------------------------------------
-- DIM_COVERAGE
-- ---------------------------------------------------------------------------
INSERT INTO DIM_COVERAGE VALUES
 (1,'BI',    'Bodily Injury',           'Liability'),
 (2,'PD',    'Property Damage',         'Liability'),
 (3,'COLL',  'Collision',               'Property'),
 (4,'COMP',  'Comprehensive',           'Property'),
 (5,'DWELL', 'Dwelling',                'Property'),
 (6,'LIAB',  'Personal Liability',      'Liability'),
 (7,'CAT',   'Catastrophe',             'Catastrophe'),
 (8,'WCMED', 'WC Medical',              'Liability');

-- ---------------------------------------------------------------------------
-- DIM_AGENT  (500 agents)
-- ---------------------------------------------------------------------------
INSERT INTO DIM_AGENT
SELECT
    SEQ4() + 1                                                        AS AGENT_KEY,
    'A' || LPAD(SEQ4() + 1, 6, '0')                                   AS AGENT_NUMBER,
    'Agent '   || (SEQ4() + 1)                                        AS AGENT_NAME,
    'Agency '  || (1 + MOD(SEQ4(), 80))                               AS AGENCY_NAME,
    DATEADD(day, -UNIFORM(180, 7300, RANDOM(1001)), CURRENT_DATE())   AS HIRE_DATE,
    DECODE(MOD(SEQ4(), 10),
        0,'Platinum', 1,'Platinum',
        2,'Gold',     3,'Gold',     4,'Gold',
        5,'Silver',   6,'Silver',   7,'Silver',
                                    'Bronze')                          AS TIER
FROM TABLE(GENERATOR(ROWCOUNT => 500));

-- ---------------------------------------------------------------------------
-- DIM_POLICYHOLDER  (10,000)
-- ---------------------------------------------------------------------------
INSERT INTO DIM_POLICYHOLDER
SELECT
    SEQ4() + 1                                                        AS POLICYHOLDER_KEY,
    'PH' || LPAD(SEQ4() + 1, 8, '0')                                  AS POLICYHOLDER_NUMBER,
    'First' || (SEQ4() + 1)                                           AS FIRST_NAME,
    'Last'  || (SEQ4() + 1)                                           AS LAST_NAME,
    DATEADD(day, -UNIFORM(6570, 29200, RANDOM(2002)), CURRENT_DATE()) AS DATE_OF_BIRTH,
    DECODE(MOD(SEQ4(), 3), 0,'M', 1,'F', 'X')                         AS GENDER,
    1 + MOD(ABS(RANDOM(2003) + SEQ4()), 50)                           AS GEOGRAPHY_KEY,
    'ph' || (SEQ4() + 1) || '@example.com'                            AS EMAIL,
    DATEADD(day, -UNIFORM(30, 2000, RANDOM(2004)), CURRENT_DATE())    AS ACQUIRED_DATE
FROM TABLE(GENERATOR(ROWCOUNT => 10000));

-- ---------------------------------------------------------------------------
-- FACT_POLICY  (50,000 policies)
-- ---------------------------------------------------------------------------
INSERT INTO FACT_POLICY
WITH base AS (
    SELECT
        SEQ4() + 1                                                          AS POLICY_KEY,
        1 + MOD(ABS(RANDOM(3001) + SEQ4()), 10000)                          AS POLICYHOLDER_KEY,
        1 + MOD(ABS(RANDOM(3002) + SEQ4()), 500)                            AS AGENT_KEY,
        1 + MOD(ABS(RANDOM(3003) + SEQ4()), 5)                              AS PRODUCT_KEY,
        1 + MOD(ABS(RANDOM(3004) + SEQ4()), 50)                             AS GEOGRAPHY_KEY,
        DATEADD(day, UNIFORM(0, 2190, RANDOM(3005)), '2020-01-01'::DATE)    AS EFFECTIVE_DATE,
        DECODE(MOD(SEQ4(), 4), 0, 6, 1, 12, 2, 12, 12)                      AS TERM_MONTHS,
        UNIFORM(50, 5000, RANDOM(3006))::NUMBER(12,4) / 100.0               AS EXPOSURE_UNITS,
        UNIFORM(50000, 500000, RANDOM(3007))::NUMBER(14,2) / 100.0          AS ANNUAL_PREMIUM
    FROM TABLE(GENERATOR(ROWCOUNT => 50000))
)
SELECT
    POLICY_KEY,
    'POL' || LPAD(POLICY_KEY, 10, '0')                                       AS POLICY_NUMBER,
    POLICYHOLDER_KEY,
    AGENT_KEY,
    PRODUCT_KEY,
    GEOGRAPHY_KEY,
    TO_NUMBER(TO_CHAR(EFFECTIVE_DATE, 'YYYYMMDD'))                           AS EFFECTIVE_DATE_KEY,
    TO_NUMBER(TO_CHAR(DATEADD(month, TERM_MONTHS, EFFECTIVE_DATE), 'YYYYMMDD')) AS EXPIRY_DATE_KEY,
    CASE
        WHEN DATEADD(month, TERM_MONTHS, EFFECTIVE_DATE) < CURRENT_DATE() THEN
            DECODE(MOD(POLICY_KEY, 10), 0,'CANCELLED', 1,'NONRENEW', 'EXPIRED')
        ELSE
            DECODE(MOD(POLICY_KEY, 20), 0,'CANCELLED', 'ACTIVE')
    END                                                                      AS POLICY_STATUS,
    TERM_MONTHS,
    EXPOSURE_UNITS,
    ANNUAL_PREMIUM
FROM base;

-- ---------------------------------------------------------------------------
-- FACT_PREMIUM_TXN  (~5 per policy ≈ 250k)
-- ---------------------------------------------------------------------------
INSERT INTO FACT_PREMIUM_TXN
WITH expanded AS (
    SELECT
        p.POLICY_KEY,
        p.EFFECTIVE_DATE_KEY,
        p.PRODUCT_KEY,
        p.ANNUAL_PREMIUM,
        p.TERM_MONTHS,
        s.SEQ AS TXN_OFFSET                              -- 0..4 monthly install + 0..1 endorsements
    FROM FACT_POLICY p,
         LATERAL (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 5))) s
)
SELECT
    ROW_NUMBER() OVER (ORDER BY POLICY_KEY, TXN_OFFSET)                        AS PREMIUM_TXN_KEY,
    POLICY_KEY,
    -- Coverage selection biased by LOB
    CASE PRODUCT_KEY
        WHEN 1 THEN DECODE(MOD(TXN_OFFSET,4), 0,1, 1,2, 2,3, 4)               -- AUTO: BI/PD/COLL/COMP
        WHEN 2 THEN DECODE(MOD(TXN_OFFSET,3), 0,5, 1,6, 7)                    -- HOME: DWELL/LIAB/CAT
        WHEN 3 THEN 6                                                          -- UMB: LIAB
        WHEN 4 THEN DECODE(MOD(TXN_OFFSET,2), 0,5, 7)                          -- COMM: DWELL/CAT
        WHEN 5 THEN 8                                                          -- WC: WCMED
    END                                                                        AS COVERAGE_KEY,
    TO_NUMBER(TO_CHAR(
        DATEADD(month, TXN_OFFSET,
            TO_DATE(TO_CHAR(EFFECTIVE_DATE_KEY), 'YYYYMMDD')),
        'YYYYMMDD'))                                                           AS TXN_DATE_KEY,
    TO_CHAR(DATEADD(month, TXN_OFFSET,
        TO_DATE(TO_CHAR(EFFECTIVE_DATE_KEY), 'YYYYMMDD')), 'YYYY-MM')          AS ACCOUNTING_PERIOD,
    ROUND(ANNUAL_PREMIUM / 5.0, 2)                                             AS WRITTEN_PREMIUM,
    ROUND((ANNUAL_PREMIUM / 5.0) * LEAST(1, (TXN_OFFSET + 1) / 5.0), 2)        AS EARNED_PREMIUM,
    ROUND((ANNUAL_PREMIUM / 5.0) * GREATEST(0, 1 - (TXN_OFFSET + 1) / 5.0), 2) AS UNEARNED_PREMIUM,
    ROUND((ANNUAL_PREMIUM / 5.0) * 0.12, 2)                                    AS COMMISSION_AMOUNT,
    ROUND((ANNUAL_PREMIUM / 5.0) * 0.025, 2)                                   AS TAX_AMOUNT
FROM expanded;

-- ---------------------------------------------------------------------------
-- FACT_CLAIM  (~30,000 — frequency ~ 0.6 per policy)
-- ---------------------------------------------------------------------------
INSERT INTO FACT_CLAIM
WITH src AS (
    SELECT
        SEQ4() + 1                                                          AS CLAIM_KEY,
        1 + MOD(ABS(RANDOM(4001) + SEQ4()), 50000)                          AS POLICY_KEY,
        UNIFORM(0, 2190, RANDOM(4002))                                      AS LOSS_DAY_OFFSET,
        UNIFORM(0, 60,   RANDOM(4003))                                      AS REPORT_LAG_DAYS,
        UNIFORM(30, 720, RANDOM(4004))                                      AS CLOSE_LAG_DAYS,
        UNIFORM(50000, 25000000, RANDOM(4005))::NUMBER(14,2) / 100.0        AS REPORTED_AMOUNT,
        UNIFORM(1, 100,  RANDOM(4006))                                      AS STATUS_ROLL,
        UNIFORM(1, 1000, RANDOM(4007))                                      AS COVERAGE_ROLL,
        UNIFORM(1, 100,  RANDOM(4008))                                      AS CAUSE_ROLL
    FROM TABLE(GENERATOR(ROWCOUNT => 30000))
)
SELECT
    CLAIM_KEY,
    'CLM' || LPAD(CLAIM_KEY, 10, '0')                                       AS CLAIM_NUMBER,
    POLICY_KEY,
    1 + MOD(COVERAGE_ROLL, 8)                                               AS COVERAGE_KEY,
    TO_NUMBER(TO_CHAR(DATEADD(day, LOSS_DAY_OFFSET, '2020-01-01'::DATE), 'YYYYMMDD'))                            AS LOSS_DATE_KEY,
    TO_NUMBER(TO_CHAR(DATEADD(day, LOSS_DAY_OFFSET + REPORT_LAG_DAYS, '2020-01-01'::DATE), 'YYYYMMDD'))          AS REPORT_DATE_KEY,
    CASE WHEN STATUS_ROLL > 25
         THEN TO_NUMBER(TO_CHAR(DATEADD(day, LOSS_DAY_OFFSET + REPORT_LAG_DAYS + CLOSE_LAG_DAYS, '2020-01-01'::DATE), 'YYYYMMDD'))
         ELSE NULL END                                                                                            AS CLOSE_DATE_KEY,
    DECODE(LEAST(STATUS_ROLL/25, 3), 0,'OPEN', 1,'CLOSED', 2,'CLOSED', 'REOPENED')                                AS CLAIM_STATUS,
    DECODE(MOD(CAUSE_ROLL, 10),
        0,'Collision', 1,'Theft', 2,'Fire', 3,'Wind', 4,'Hail',
        5,'Water Damage', 6,'Liability', 7,'Vandalism', 8,'Flood', 'Other')                                       AS CAUSE_OF_LOSS,
    REPORTED_AMOUNT,
    CASE WHEN STATUS_ROLL > 25 THEN ROUND(REPORTED_AMOUNT * UNIFORM(40, 95, RANDOM(4009)) / 100.0, 2) ELSE ROUND(REPORTED_AMOUNT * UNIFORM(5, 60, RANDOM(4010)) / 100.0, 2) END AS PAID_LOSS,
    ROUND(REPORTED_AMOUNT * UNIFORM(2, 15, RANDOM(4011)) / 1000.0, 2)                                             AS PAID_ALAE,
    CASE WHEN STATUS_ROLL > 25 THEN 0 ELSE ROUND(REPORTED_AMOUNT * UNIFORM(30, 80, RANDOM(4012)) / 100.0, 2) END  AS CASE_RESERVE,
    ROUND(REPORTED_AMOUNT * UNIFORM(0, 50, RANDOM(4013)) / 1000.0, 2)                                             AS SALVAGE_SUBRO
FROM src;

-- ---------------------------------------------------------------------------
-- FACT_LOSS_RESERVE  (monthly snapshots per (policy, coverage) — sampled)
--   Use ~600k rows: 50k policies × 1 coverage × ~12 months, sampled.
-- ---------------------------------------------------------------------------
INSERT INTO FACT_LOSS_RESERVE
WITH snap AS (
    SELECT
        ROW_NUMBER() OVER (ORDER BY p.POLICY_KEY, m.SEQ)                       AS LOSS_RESERVE_KEY,
        p.POLICY_KEY,
        1 + MOD(ABS(RANDOM(5001) + p.POLICY_KEY + m.SEQ), 8)                   AS COVERAGE_KEY,
        DATEADD(month, m.SEQ,
            TO_DATE(TO_CHAR(p.EFFECTIVE_DATE_KEY), 'YYYYMMDD'))                AS AS_OF_DATE,
        p.ANNUAL_PREMIUM
    FROM FACT_POLICY p,
         LATERAL (SELECT SEQ4() AS SEQ FROM TABLE(GENERATOR(ROWCOUNT => 12))) m
)
SELECT
    LOSS_RESERVE_KEY,
    POLICY_KEY,
    COVERAGE_KEY,
    TO_NUMBER(TO_CHAR(AS_OF_DATE, 'YYYYMMDD'))                                 AS AS_OF_DATE_KEY,
    TO_CHAR(AS_OF_DATE, 'YYYY-MM')                                             AS ACCOUNTING_PERIOD,
    ROUND(ANNUAL_PREMIUM * UNIFORM(5,  60, RANDOM(5002)) / 100.0, 2)           AS CASE_RESERVE,
    ROUND(ANNUAL_PREMIUM * UNIFORM(2,  30, RANDOM(5003)) / 100.0, 2)           AS IBNR_RESERVE,
    ROUND(ANNUAL_PREMIUM * UNIFORM(1,   8, RANDOM(5004)) / 100.0, 2)           AS ULAE_RESERVE,
    ROUND(ANNUAL_PREMIUM * UNIFORM(0,  40, RANDOM(5005)) / 100.0, 2)           AS PAID_TO_DATE
FROM snap
WHERE AS_OF_DATE <= CURRENT_DATE();
