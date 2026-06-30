-- Step 2: step1 + role-playing date dimension (DIM_DATE used as two roles).
CREATE OR REPLACE SEMANTIC VIEW INSURANCE_POC.ACTUARIAL.STEP2

  TABLES (
    effective_date   AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY) COMMENT = 'Policy effective date dimension',
    txn_date         AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY) COMMENT = 'Premium transaction date dimension',
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
    fact_policy.policy_number           AS POLICY_NUMBER
  )

  METRICS (
    fact_policy.policy_count         AS COUNT(POLICY_KEY)    COMMENT = 'Number of policies',
    fact_policy.total_annual_premium AS SUM(ANNUAL_PREMIUM)  COMMENT = 'Sum of annualized premium',
    fact_premium_txn.total_written   AS SUM(WRITTEN_PREMIUM) COMMENT = 'Gross written premium',
    fact_premium_txn.total_earned    AS SUM(EARNED_PREMIUM)  COMMENT = 'Gross earned premium'
  );
