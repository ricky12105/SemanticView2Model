-- Step 4: step3 + fact_claim and view-level cross-table ratio metrics.
-- Exercises the DAX rewriter for metrics that reference other metrics across
-- tables, plus the holding-table pattern for view-level (table-less) measures.
CREATE OR REPLACE SEMANTIC VIEW INSURANCE_POC.ACTUARIAL.STEP4

  TABLES (
    effective_date   AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         COMMENT = 'Policy effective date dimension',
    txn_date         AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         COMMENT = 'Premium transaction date dimension',
    loss_date        AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         COMMENT = 'Claim loss date dimension',
    dim_policyholder AS INSURANCE_POC.ACTUARIAL.DIM_POLICYHOLDER PRIMARY KEY (POLICYHOLDER_KEY) COMMENT = 'Insureds',
    fact_policy      AS INSURANCE_POC.ACTUARIAL.FACT_POLICY      PRIMARY KEY (POLICY_KEY)       COMMENT = 'Issued policies',
    fact_premium_txn AS INSURANCE_POC.ACTUARIAL.FACT_PREMIUM_TXN PRIMARY KEY (PREMIUM_TXN_KEY)  COMMENT = 'Premium transactions',
    fact_claim       AS INSURANCE_POC.ACTUARIAL.FACT_CLAIM       PRIMARY KEY (CLAIM_KEY)        COMMENT = 'Reported claims'
  )

  RELATIONSHIPS (
    policy_to_policyholder   AS fact_policy(POLICYHOLDER_KEY)    REFERENCES dim_policyholder(POLICYHOLDER_KEY),
    policy_to_effective_date AS fact_policy(EFFECTIVE_DATE_KEY)  REFERENCES effective_date(DATE_KEY),
    premium_to_policy        AS fact_premium_txn(POLICY_KEY)     REFERENCES fact_policy(POLICY_KEY),
    premium_to_date          AS fact_premium_txn(TXN_DATE_KEY)   REFERENCES txn_date(DATE_KEY),
    claim_to_policy          AS fact_claim(POLICY_KEY)           REFERENCES fact_policy(POLICY_KEY),
    claim_to_loss_date       AS fact_claim(LOSS_DATE_KEY)        REFERENCES loss_date(DATE_KEY)
  )

  FACTS (
    fact_policy.exposure_units        AS EXPOSURE_UNITS,
    fact_policy.annual_premium        AS ANNUAL_PREMIUM,
    fact_premium_txn.written_premium  AS WRITTEN_PREMIUM,
    fact_premium_txn.earned_premium   AS EARNED_PREMIUM,
    fact_premium_txn.commission_amount AS COMMISSION_AMOUNT,
    fact_claim.paid_loss              AS PAID_LOSS,
    fact_claim.case_reserve           AS CASE_RESERVE,
    fact_claim.salvage_subro          AS SALVAGE_SUBRO
  )

  DIMENSIONS (
    effective_date.effective_date_key AS DATE_KEY,
    effective_date.effective_year     AS YEAR_NUM,

    txn_date.txn_date_key             AS DATE_KEY,
    txn_date.txn_year                 AS YEAR_NUM,

    loss_date.loss_date_key           AS DATE_KEY,
    loss_date.loss_year               AS YEAR_NUM,

    dim_policyholder.first_name       AS FIRST_NAME,
    dim_policyholder.last_name        AS LAST_NAME,

    fact_policy.policy_number         AS POLICY_NUMBER,
    fact_policy.policy_status         AS POLICY_STATUS,

    fact_claim.claim_number           AS CLAIM_NUMBER,
    fact_claim.claim_status           AS CLAIM_STATUS
  )

  METRICS (
    fact_policy.policy_count           AS COUNT(POLICY_KEY)                                    COMMENT = 'Number of policies',
    fact_policy.total_exposure         AS SUM(EXPOSURE_UNITS)                                  COMMENT = 'Total exposure units',
    fact_policy.total_annual_premium   AS SUM(ANNUAL_PREMIUM)                                  COMMENT = 'Sum of annualized premium',

    fact_premium_txn.total_written     AS SUM(WRITTEN_PREMIUM)                                 COMMENT = 'Gross written premium',
    fact_premium_txn.total_earned      AS SUM(EARNED_PREMIUM)                                  COMMENT = 'Gross earned premium',
    fact_premium_txn.total_commission  AS SUM(COMMISSION_AMOUNT)                               COMMENT = 'Commission paid to agents',

    fact_claim.claim_count             AS COUNT(CLAIM_KEY)                                     COMMENT = 'Number of claims',
    fact_claim.total_paid_loss         AS SUM(PAID_LOSS)                                       COMMENT = 'Paid losses',
    fact_claim.total_case_reserve      AS SUM(CASE_RESERVE)                                    COMMENT = 'Outstanding case reserves',
    fact_claim.total_incurred_loss     AS SUM(PAID_LOSS + CASE_RESERVE - SALVAGE_SUBRO)        COMMENT = 'Incurred losses net of salvage/subro',

    loss_ratio        AS fact_claim.total_incurred_loss / NULLIF(fact_premium_txn.total_earned, 0)                       COMMENT = 'Incurred losses / earned premium',
    expense_ratio     AS fact_premium_txn.total_commission / NULLIF(fact_premium_txn.total_written, 0)                   COMMENT = 'Commissions / written premium',
    combined_ratio    AS (fact_claim.total_incurred_loss + fact_premium_txn.total_commission) / NULLIF(fact_premium_txn.total_earned, 0) COMMENT = 'Loss ratio + expense ratio',
    claim_frequency   AS fact_claim.claim_count / NULLIF(fact_policy.total_exposure, 0)                                  COMMENT = 'Claims per unit of exposure'
  );
