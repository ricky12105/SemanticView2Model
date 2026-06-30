-- ============================================================================
-- File:    semantic_view/insurance_actuarial.sql
-- Purpose: Equivalent CREATE SEMANTIC VIEW DDL for insurance_actuarial.yaml.
-- Spec:    https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view
-- Note:    Clause order matters: TABLES, RELATIONSHIPS, FACTS, DIMENSIONS, METRICS.
-- ============================================================================

CREATE OR REPLACE SEMANTIC VIEW INSURANCE_POC.ACTUARIAL.INSURANCE_ACTUARIAL

  TABLES (
    effective_date    AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         WITH SYNONYMS = ('policy date','effective date') COMMENT = 'Policy effective date dimension',
    txn_date          AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         WITH SYNONYMS = ('transaction date','premium date') COMMENT = 'Premium transaction date dimension',
    loss_date         AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         WITH SYNONYMS = ('claim date','loss date') COMMENT = 'Claim loss date dimension',
    reserve_date      AS INSURANCE_POC.ACTUARIAL.DIM_DATE         PRIMARY KEY (DATE_KEY)         WITH SYNONYMS = ('reserve date','as of date') COMMENT = 'Reserve snapshot date dimension',
    dim_geography     AS INSURANCE_POC.ACTUARIAL.DIM_GEOGRAPHY    PRIMARY KEY (GEOGRAPHY_KEY)    COMMENT = 'US states & catastrophe zones',
    dim_product_lob   AS INSURANCE_POC.ACTUARIAL.DIM_PRODUCT_LOB  PRIMARY KEY (PRODUCT_KEY)      WITH SYNONYMS = ('line of business','LOB','product') COMMENT = 'Line of business / product',
    dim_coverage      AS INSURANCE_POC.ACTUARIAL.DIM_COVERAGE     PRIMARY KEY (COVERAGE_KEY)     COMMENT = 'Coverage type within a policy',
    dim_agent         AS INSURANCE_POC.ACTUARIAL.DIM_AGENT        PRIMARY KEY (AGENT_KEY)        UNIQUE (AGENT_NUMBER) COMMENT = 'Producing agents',
    dim_policyholder  AS INSURANCE_POC.ACTUARIAL.DIM_POLICYHOLDER PRIMARY KEY (POLICYHOLDER_KEY) UNIQUE (POLICYHOLDER_NUMBER) COMMENT = 'Insureds',
    fact_policy       AS INSURANCE_POC.ACTUARIAL.FACT_POLICY      PRIMARY KEY (POLICY_KEY)       UNIQUE (POLICY_NUMBER) WITH SYNONYMS = ('policies','book of business') COMMENT = 'Issued policies',
    fact_premium_txn  AS INSURANCE_POC.ACTUARIAL.FACT_PREMIUM_TXN PRIMARY KEY (PREMIUM_TXN_KEY)  WITH SYNONYMS = ('premium','premium transactions') COMMENT = 'Premium accounting transactions',
    fact_claim        AS INSURANCE_POC.ACTUARIAL.FACT_CLAIM       PRIMARY KEY (CLAIM_KEY)        UNIQUE (CLAIM_NUMBER) WITH SYNONYMS = ('claims','losses') COMMENT = 'Reported claims',
    fact_loss_reserve AS INSURANCE_POC.ACTUARIAL.FACT_LOSS_RESERVE PRIMARY KEY (LOSS_RESERVE_KEY) COMMENT = 'Monthly loss-reserve snapshots'
  )

  RELATIONSHIPS (
    policy_to_policyholder   AS fact_policy(POLICYHOLDER_KEY)   REFERENCES dim_policyholder(POLICYHOLDER_KEY),
    policy_to_agent          AS fact_policy(AGENT_KEY)          REFERENCES dim_agent(AGENT_KEY),
    policy_to_product        AS fact_policy(PRODUCT_KEY)        REFERENCES dim_product_lob(PRODUCT_KEY),
    policy_to_geography      AS fact_policy(GEOGRAPHY_KEY)      REFERENCES dim_geography(GEOGRAPHY_KEY),
    policy_to_effective_date AS fact_policy(EFFECTIVE_DATE_KEY)  REFERENCES effective_date(DATE_KEY),
    premium_to_policy        AS fact_premium_txn(POLICY_KEY)    REFERENCES fact_policy(POLICY_KEY),
    premium_to_coverage      AS fact_premium_txn(COVERAGE_KEY)  REFERENCES dim_coverage(COVERAGE_KEY),
    premium_to_date          AS fact_premium_txn(TXN_DATE_KEY)  REFERENCES txn_date(DATE_KEY),
    claim_to_policy          AS fact_claim(POLICY_KEY)          REFERENCES fact_policy(POLICY_KEY),
    claim_to_coverage        AS fact_claim(COVERAGE_KEY)        REFERENCES dim_coverage(COVERAGE_KEY),
    claim_to_loss_date       AS fact_claim(LOSS_DATE_KEY)       REFERENCES loss_date(DATE_KEY),
    reserve_to_policy        AS fact_loss_reserve(POLICY_KEY)   REFERENCES fact_policy(POLICY_KEY),
    reserve_to_coverage      AS fact_loss_reserve(COVERAGE_KEY) REFERENCES dim_coverage(COVERAGE_KEY),
    reserve_to_date          AS fact_loss_reserve(AS_OF_DATE_KEY) REFERENCES reserve_date(DATE_KEY)
  )

  FACTS (
    fact_policy.exposure_units                            AS EXPOSURE_UNITS,
    fact_policy.annual_premium                            AS ANNUAL_PREMIUM,
    fact_premium_txn.written_premium                      AS WRITTEN_PREMIUM,
    fact_premium_txn.earned_premium                       AS EARNED_PREMIUM,
    fact_premium_txn.unearned_premium                     AS UNEARNED_PREMIUM,
    fact_premium_txn.commission_amount                    AS COMMISSION_AMOUNT,
    fact_premium_txn.tax_amount                           AS TAX_AMOUNT,
    fact_claim.reported_amount                            AS REPORTED_AMOUNT,
    fact_claim.paid_loss                                  AS PAID_LOSS,
    fact_claim.paid_alae                                  AS PAID_ALAE         COMMENT = 'Allocated loss adjustment expense',
    fact_claim.case_reserve                               AS CASE_RESERVE,
    fact_claim.salvage_subro                              AS SALVAGE_SUBRO,
    PRIVATE fact_claim.incurred_loss                      AS PAID_LOSS + CASE_RESERVE - SALVAGE_SUBRO COMMENT = 'Helper: incurred loss per row',
    fact_loss_reserve.case_reserve_snap                   AS CASE_RESERVE,
    fact_loss_reserve.ibnr_reserve                        AS IBNR_RESERVE      COMMENT = 'Incurred but not reported reserve',
    fact_loss_reserve.ulae_reserve                        AS ULAE_RESERVE      COMMENT = 'Unallocated loss adjustment expense reserve',
    fact_loss_reserve.paid_to_date                        AS PAID_TO_DATE,
    PRIVATE dim_policyholder.policyholder_count           AS 1 COMMENT = 'Helper for COUNT(DISTINCT policyholder)'
  )

  DIMENSIONS (
    effective_date.effective_date_key    AS DATE_KEY,
    effective_date.effective_date        AS DATE_VAL    COMMENT = 'Policy effective date',
    effective_date.effective_year        AS YEAR_NUM    WITH SYNONYMS = ('policy year'),
    effective_date.effective_year_month  AS YEAR_MONTH,
    effective_date.effective_quarter     AS QUARTER_NUM,

    txn_date.txn_date_key               AS DATE_KEY,
    txn_date.txn_date                   AS DATE_VAL          COMMENT = 'Premium transaction date',
    txn_date.txn_year                   AS YEAR_NUM          WITH SYNONYMS = ('transaction year'),
    txn_date.txn_year_month             AS YEAR_MONTH        WITH SYNONYMS = ('accounting month'),
    txn_date.txn_month_name             AS MONTH_NAME,
    txn_date.txn_quarter                AS QUARTER_NUM,

    loss_date.loss_date_key             AS DATE_KEY,
    loss_date.loss_date                 AS DATE_VAL          COMMENT = 'Date of loss',
    loss_date.loss_year                 AS YEAR_NUM          WITH SYNONYMS = ('claim year', 'accident year'),
    loss_date.loss_year_month           AS YEAR_MONTH,
    loss_date.loss_quarter              AS QUARTER_NUM,

    reserve_date.reserve_date_key       AS DATE_KEY,
    reserve_date.reserve_date           AS DATE_VAL          COMMENT = 'Reserve snapshot date',
    reserve_date.reserve_year           AS YEAR_NUM,
    reserve_date.reserve_year_month     AS YEAR_MONTH,
    reserve_date.is_month_end           LABELS = (FILTER) AS IS_MONTH_END COMMENT = 'Use as filter to restrict to month-end snapshots',

    dim_geography.geography_key AS GEOGRAPHY_KEY,
    dim_geography.state_code    AS STATE_CODE WITH SYNONYMS = ('state'),
    dim_geography.state_name    AS STATE_NAME,
    dim_geography.region        AS REGION,
    dim_geography.cat_zone      AS CAT_ZONE COMMENT = 'Catastrophe zone classification (null for non-CAT-exposed states)',

    dim_product_lob.product_key AS PRODUCT_KEY,
    dim_product_lob.lob_code    AS LOB_CODE,
    dim_product_lob.lob_name    AS LOB_NAME,
    dim_product_lob.line_group  AS LINE_GROUP WITH SYNONYMS = ('business segment'),

    dim_coverage.coverage_key   AS COVERAGE_KEY,
    dim_coverage.coverage_code  AS COVERAGE_CODE,
    dim_coverage.coverage_name  AS COVERAGE_NAME,
    dim_coverage.peril_category AS PERIL_CATEGORY,

    dim_agent.agent_key         AS AGENT_KEY,
    dim_agent.agent_number      AS AGENT_NUMBER,
    dim_agent.agent_name        AS AGENT_NAME,
    dim_agent.agency_name       AS AGENCY_NAME,
    dim_agent.tier              AS TIER,
    dim_agent.hire_date         AS HIRE_DATE,

    dim_policyholder.policyholder_key    AS POLICYHOLDER_KEY,
    dim_policyholder.policyholder_number AS POLICYHOLDER_NUMBER,
    dim_policyholder.first_name          AS FIRST_NAME,
    dim_policyholder.last_name           AS LAST_NAME,
    dim_policyholder.gender              AS GENDER,
    dim_policyholder.email               AS EMAIL,
    dim_policyholder.age_years           AS DATEDIFF(year, DATE_OF_BIRTH, CURRENT_DATE()) COMMENT = 'Current age (calculated)',
    dim_policyholder.date_of_birth       AS DATE_OF_BIRTH,
    dim_policyholder.acquired_date       AS ACQUIRED_DATE,

    fact_policy.policy_key      AS POLICY_KEY,
    fact_policy.policy_number   AS POLICY_NUMBER,
    fact_policy.policy_status   AS POLICY_STATUS,
    fact_policy.term_months     AS TERM_MONTHS,
    fact_policy.is_active       LABELS = (FILTER) AS POLICY_STATUS = 'ACTIVE' COMMENT = 'Active-policy filter',

    fact_premium_txn.premium_txn_key   AS PREMIUM_TXN_KEY,
    fact_premium_txn.premium_accounting_period AS ACCOUNTING_PERIOD WITH SYNONYMS = ('premium accounting month'),

    fact_claim.claim_key      AS CLAIM_KEY,
    fact_claim.claim_number   AS CLAIM_NUMBER,
    fact_claim.claim_status   AS CLAIM_STATUS,
    fact_claim.cause_of_loss  AS CAUSE_OF_LOSS,
    fact_claim.is_open_claim  LABELS = (FILTER) AS CLAIM_STATUS IN ('OPEN','REOPENED') COMMENT = 'Open-claim filter',
    fact_claim.is_cat_claim   LABELS = (FILTER) AS CAUSE_OF_LOSS IN ('Wind','Hail','Flood','Fire') COMMENT = 'Catastrophe-related cause-of-loss filter',

    fact_loss_reserve.loss_reserve_key  AS LOSS_RESERVE_KEY,
    fact_loss_reserve.reserve_accounting_period AS ACCOUNTING_PERIOD
  )

  METRICS (
    fact_policy.policy_count           AS COUNT(*)                                  COMMENT = 'Number of policies',
    fact_policy.total_exposure         AS SUM(EXPOSURE_UNITS)                       COMMENT = 'Total exposure units',
    fact_policy.total_annual_premium   AS SUM(ANNUAL_PREMIUM)                       COMMENT = 'Sum of annualized premium amounts',

    fact_premium_txn.total_written_premium AS SUM(WRITTEN_PREMIUM)                  WITH SYNONYMS = ('WP','GWP') COMMENT = 'Gross written premium',
    fact_premium_txn.total_earned_premium  AS SUM(EARNED_PREMIUM)                   WITH SYNONYMS = ('EP','GEP') COMMENT = 'Gross earned premium',
    fact_premium_txn.total_commission      AS SUM(COMMISSION_AMOUNT)                COMMENT = 'Commission paid to agents',
    fact_premium_txn.t12m_earned_premium   AS SUM(SUM(EARNED_PREMIUM))
        OVER (ORDER BY txn_date.txn_date ROWS BETWEEN 11 PRECEDING AND CURRENT ROW)
        COMMENT = 'Trailing-12-month earned premium (window function metric)',

    fact_claim.claim_count             AS COUNT(*)                                  COMMENT = 'Number of claims',
    fact_claim.total_paid_loss         AS SUM(PAID_LOSS)                            COMMENT = 'Paid losses',
    fact_claim.total_paid_alae         AS SUM(PAID_ALAE)                            COMMENT = 'Paid ALAE',
    fact_claim.total_case_reserve      AS SUM(CASE_RESERVE)                         COMMENT = 'Outstanding case reserves',
    fact_claim.total_incurred_loss     AS SUM(PAID_LOSS + CASE_RESERVE - SALVAGE_SUBRO) WITH SYNONYMS = ('incurred losses') COMMENT = 'Incurred losses net of salvage/subro',
    fact_claim.average_claim_size      AS SUM(PAID_LOSS + CASE_RESERVE) / NULLIF(COUNT(*), 0) COMMENT = 'Average claim severity',

    fact_loss_reserve.total_case_reserves
        NON ADDITIVE BY (reserve_date.reserve_date DESC NULLS LAST)
        AS SUM(CASE_RESERVE)
        COMMENT = 'Case reserves at the latest snapshot date',
    fact_loss_reserve.total_ibnr_reserves
        NON ADDITIVE BY (reserve_date.reserve_date DESC NULLS LAST)
        AS SUM(IBNR_RESERVE)
        COMMENT = 'IBNR reserves at the latest snapshot date',
    fact_loss_reserve.total_reserves
        NON ADDITIVE BY (reserve_date.reserve_date DESC NULLS LAST)
        AS SUM(CASE_RESERVE + IBNR_RESERVE + ULAE_RESERVE)
        COMMENT = 'Total loss reserves (case + IBNR + ULAE) at latest snapshot',

    loss_ratio        AS fact_claim.total_incurred_loss / NULLIF(fact_premium_txn.total_earned_premium, 0)                       WITH SYNONYMS = ('LR') COMMENT = 'Incurred losses ÷ earned premium',
    expense_ratio     AS fact_premium_txn.total_commission / NULLIF(fact_premium_txn.total_written_premium, 0)                   COMMENT = 'Commissions ÷ written premium',
    combined_ratio    AS (fact_claim.total_incurred_loss + fact_premium_txn.total_commission) / NULLIF(fact_premium_txn.total_earned_premium, 0) WITH SYNONYMS = ('CR') COMMENT = 'Loss ratio + expense ratio',
    claim_frequency   AS fact_claim.claim_count / NULLIF(fact_policy.total_exposure, 0)                                          COMMENT = 'Claims per unit of exposure'
  )

  COMMENT = 'P&C actuarial semantic view covering premium, losses, reserves and standard ratios'

  AI_VERIFIED_QUERIES (
    loss_ratio_by_lob_ytd AS (
      QUESTION 'What is the year-to-date loss ratio by line of business?'
      ONBOARDING_QUESTION TRUE
      SQL 'SELECT * FROM SEMANTIC_VIEW(INSURANCE_POC.ACTUARIAL.INSURANCE_ACTUARIAL DIMENSIONS dim_product_lob.lob_name METRICS loss_ratio) WHERE loss_date.loss_year = YEAR(CURRENT_DATE())'
    ),
    top_states_by_incurred AS (
      QUESTION 'Which 10 states have the highest incurred losses this year?'
      SQL 'SELECT * FROM SEMANTIC_VIEW(INSURANCE_POC.ACTUARIAL.INSURANCE_ACTUARIAL DIMENSIONS dim_geography.state_name METRICS fact_claim.total_incurred_loss) WHERE loss_date.loss_year = YEAR(CURRENT_DATE()) ORDER BY total_incurred_loss DESC LIMIT 10'
    )
  )
  
  WITH TAG (
    INSURANCE_POC.ACTUARIAL.domain   = 'actuarial',
    INSURANCE_POC.ACTUARIAL.maturity = 'poc'
  );
