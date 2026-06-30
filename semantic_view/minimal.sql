-- Minimal SV: 1 fact, 1 dim, 1 relationship. No metrics. For PBI Desktop load test.
CREATE OR REPLACE SEMANTIC VIEW INSURANCE_POC.ACTUARIAL.MINIMAL

  TABLES (
    dim_policyholder AS INSURANCE_POC.ACTUARIAL.DIM_POLICYHOLDER PRIMARY KEY (POLICYHOLDER_KEY) COMMENT = 'Insureds',
    fact_policy      AS INSURANCE_POC.ACTUARIAL.FACT_POLICY      PRIMARY KEY (POLICY_KEY)       COMMENT = 'Issued policies'
  )

  RELATIONSHIPS (
    policy_to_policyholder AS fact_policy(POLICYHOLDER_KEY) REFERENCES dim_policyholder(POLICYHOLDER_KEY)
  )

  FACTS (
    fact_policy.exposure_units AS EXPOSURE_UNITS,
    fact_policy.annual_premium AS ANNUAL_PREMIUM
  )

  DIMENSIONS (
    dim_policyholder.first_name AS FIRST_NAME,
    dim_policyholder.last_name  AS LAST_NAME,
    fact_policy.policy_number   AS POLICY_NUMBER
  )

  METRICS (
    fact_policy.policy_count AS COUNT(POLICY_KEY) COMMENT = 'Number of policies'
  );
