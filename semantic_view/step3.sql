-- Step 3: step2 + filter-labeled boolean dims and a calc dim.
-- Verifies that calculated dimensions are safely skipped (DirectLake forbids
-- calculated columns) while regular categorical columns load normally.
CREATE OR REPLACE SEMANTIC VIEW INSURANCE_POC.ACTUARIAL.STEP3

  TABLES (
    effective_date   AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         COMMENT = 'Policy effective date dimension',
    txn_date         AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         COMMENT = 'Premium transaction date dimension',
    dim_policyholder AS INSURANCE_POC.ACTUARIAL.DIM_POLICYHOLDER PRIMARY KEY (POLICYHOLDER_KEY) COMMENT = 'Insureds',
    fact_policy      AS INSURANCE_POC.ACTUARIAL.FACT_POLICY      PRIMARY KEY (POLICY_KEY)       COMMENT = 'Issued policies',
    fact_premium_txn AS INSURANCE_POC.ACTUARIAL.FACT_PREMIUM_TXN PRIMARY KEY (PREMIUM_TXN_KEY)  COMMENT = 'Premium transactions'
  )

  RELATIONSHIPS (
    policy_to_policyholder   AS fact_policy(POLICYHOLDER_KEY)    REFERENCES dim_policyholder(POLICYHOLDER_KEY),
    policy_to_effective_date AS fact_policy(EFFECTIVE_DATE_KEY)  REFERENCES effective_date(DATE_KEY),
    premium_to_policy        AS fact_premium_txn(POLICY_KEY)     REFERENCES fact_policy(POLICY_KEY),
    premium_to_date          AS fact_premium_txn(TXN_DATE_KEY)   REFERENCES txn_date(DATE_KEY)
  )

  FACTS (
    fact_policy.exposure_units       AS EXPOSURE_UNITS,
    fact_policy.annual_premium       AS ANNUAL_PREMIUM,
    fact_premium_txn.written_premium AS WRITTEN_PREMIUM,
    fact_premium_txn.earned_premium  AS EARNED_PREMIUM
  )

  DIMENSIONS (
    effective_date.effective_date_key   AS DATE_KEY,
    effective_date.effective_date       AS DATE_VAL    COMMENT = 'Policy effective date',
    effective_date.effective_year       AS YEAR_NUM,
    effective_date.effective_year_month AS YEAR_MONTH,

    txn_date.txn_date_key               AS DATE_KEY,
    txn_date.txn_date                   AS DATE_VAL    COMMENT = 'Premium transaction date',
    txn_date.txn_year                   AS YEAR_NUM,
    txn_date.txn_year_month             AS YEAR_MONTH,

    dim_policyholder.first_name         AS FIRST_NAME,
    dim_policyholder.last_name          AS LAST_NAME,
    dim_policyholder.gender             AS GENDER,
    dim_policyholder.date_of_birth      AS DATE_OF_BIRTH,
    -- Calculated dim: must be skipped (DirectLake forbids calc columns).
    dim_policyholder.age_years          AS DATEDIFF(year, DATE_OF_BIRTH, CURRENT_DATE()) COMMENT = 'Current age (calculated)',

    fact_policy.policy_number           AS POLICY_NUMBER,
    fact_policy.policy_status           AS POLICY_STATUS,
    fact_policy.term_months             AS TERM_MONTHS,
    -- Filter-labeled boolean dim: also a calc column, must be skipped.
    fact_policy.is_active   LABELS = (FILTER) AS POLICY_STATUS = 'ACTIVE' COMMENT = 'Active-policy filter'
  )

  METRICS (
    fact_policy.policy_count         AS COUNT(POLICY_KEY)    COMMENT = 'Number of policies',
    fact_policy.total_annual_premium AS SUM(ANNUAL_PREMIUM)  COMMENT = 'Sum of annualized premium',
    fact_premium_txn.total_written   AS SUM(WRITTEN_PREMIUM) COMMENT = 'Gross written premium',
    fact_premium_txn.total_earned    AS SUM(EARNED_PREMIUM)  COMMENT = 'Gross earned premium'
  );
