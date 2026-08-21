# SemanticView2Model (`sv2m`)

**Translate a Snowflake `SEMANTIC VIEW` (or its YAML equivalent) into a
Microsoft Fabric Power BI semantic model (TMDL / `.pbip`), running in
**Direct Lake** mode over a Fabric Lakehouse that mirrors the underlying
Snowflake tables.**

> Status: working POC. Successfully round-trips a 10-table P&C insurance
> actuarial star schema (14 relationships, 20 measures, 4 role-playing date
> tables, 5 calculated columns, 2 window functions, 2 semi-additive measures)
> and deploys it to Fabric as a hybrid Import+DirectLake composite model. See
> [INSTRUCTIONS.md](INSTRUCTIONS.md) for the detailed lessons-learned guide.

---

## Why this exists

Snowflake's
[Semantic Views](https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view)
let you describe a logical, business-friendly layer (tables, relationships,
facts, dimensions, metrics, synonyms, comments) on top of physical Snowflake
tables. Microsoft Fabric Power BI semantic models describe **exactly the same
concepts** in a different format (TMDL, with DAX measures), and can read the
same data via Direct Lake when the Snowflake tables are mirrored into a
OneLake Lakehouse.

There is no off-the-shelf converter between the two. `sv2m` is that converter.

```
┌─────────────────────┐                  ┌────────────────────────────┐
│  Snowflake          │  Fabric          │ Microsoft Fabric           │
│  CREATE SEMANTIC    │  Mirroring       │ Lakehouse (OneLake Delta)  │
│  VIEW ────────┐     ├─────────────────►│ Tables/<schema>/<table>    │
│               │     │                  └────────────┬───────────────┘
│  Physical     │     │                               │ Direct Lake
│  tables ──────┘     │                               ▼
└───────┬─────────────┘                  ┌────────────────────────────┐
        │                                │ Power BI Semantic Model    │
        │   sv2m translate               │ (TMDL / .pbip)             │
        └───────────────────────────────►│ tables + relationships +   │
                                         │ DAX measures + perspectives│
                                         └────────────────────────────┘
```

---

## What it does

Given Snowflake semantic-view source (either DDL or YAML), `sv2m`:

1. **Parses** it into a strongly-typed Pydantic v2 IR (`SemanticView`,
   `LogicalTable`, `Relationship`, `Dimension`, `Fact`, `Metric`,
   `VerifiedQuery`, …).
2. **Resolves** Snowflake `<db>.<schema>.<table>` references to the
   corresponding Fabric Lakehouse Delta entity via `config/mapping.yml`.
3. **Rewrites** Snowflake SQL expressions in measures to DAX (subset:
   SUM/COUNT/AVG/MIN/MAX/COUNT DISTINCT and bare arithmetic between metric
   references).
4. **Emits** a Fabric semantic-model folder
   (`<Name>.SemanticModel/definition/*.tmdl`) and optionally a `.pbip`
   wrapper with a thin `.Report`.
5. **(Optional)** **Deploys** the model to a Fabric workspace via REST
   (or, in practice, via the `mcp_powerbi-model_*` MCP server which is more
   robust — see [INSTRUCTIONS.md § 9.8](INSTRUCTIONS.md)).

### Supported Snowflake semantic-view features

| Feature | Status |
|---|---|
| `TABLES` + `PRIMARY KEY` / `UNIQUE` / `WITH SYNONYMS` / `COMMENT` | ✅ |
| `RELATIONSHIPS` (single + composite keys) | ✅ |
| `FACTS` (numeric, with `PRIVATE`, comments, synonyms) | ✅ |
| `DIMENSIONS` (calc dim expressions, `LABELS = (FILTER)` flag) | ✅ (two strategies: materialize or DAX — see § 10 in INSTRUCTIONS) |
| `METRICS` table-scoped + view-level | ✅ |
| `WITH SYNONYMS`, `COMMENT`, `LABELS`, `NON ADDITIVE BY` (any order) | ✅ (semi-additive auto-wraps with `LASTNONBLANK`) |
| `AI_VERIFIED_QUERIES` | Parsed, not emitted |
| `WITH TAG (...)` | ✅ |
| Window-function metrics (`SUM(SUM(x)) OVER ...`) | ✅ Auto-translated to `CALCULATE` + `DATESINPERIOD` |
| Role-playing on a shared dimension | ✅ via duplicate logical tables |
| Ambiguous-path detection (union-find cycle breaking) | ✅ extra edges marked `isActive: false` |

---

## Repository layout

```
SemanticView2Model/
├── translator/
│   ├── cli.py                 sv2m CLI entry point
│   ├── parsers/
│   │   ├── sql_parser.py      Hand-rolled Snowflake CREATE SEMANTIC VIEW parser
│   │   └── yaml_parser.py     YAML semantic-view parser (Snowflake's YAML form)
│   ├── ir/
│   │   └── model.py           Pydantic v2 IR: SemanticView, LogicalTable, Metric, …
│   ├── emitters/
│   │   ├── tmdl_writer.py       IR → TMDL files (model, tables, relationships, expressions)
│   │   ├── dax_rewriter.py      Snowflake SQL → DAX (aggregations, calcs, window fns)
│   │   ├── mapping.py           Loads mapping.yml; resolves logical → physical
│   │   └── calc_materializer.py PySpark script generator for calc-column materialization
│   └── deploy/                  Fabric REST publish helpers (azure-identity)
├── semantic_view/
│   ├── insurance_actuarial.sql   Full 10-table reference SV (DDL)
│   ├── insurance_actuarial.yaml  Same view, YAML form
│   ├── minimal.sql               Smallest valid SV
│   └── step1..step4.sql          Complexity ladder for iterating features
├── mock_data/                Snowflake DDL + seed scripts for the insurance schema
├── config/
│   └── mapping.yml           Snowflake → Fabric Lakehouse resolution
├── out_step1..out_step5/     Last-known-good translator outputs per step
├── tests/                    pytest (parser roundtrip + emitter goldens)
├── INSTRUCTIONS.md           Field guide / lessons learned (READ THIS)
├── pyproject.toml            Installs the `sv2m` script
├── requirements.txt
└── README.md
```

---

## How it works (under the hood)

### Pipeline

```
   .sql / .yaml                            mapping.yml
        │                                       │
        ▼                                       ▼
 ┌──────────────┐    IR     ┌───────────────────────────┐    TMDL files
 │ sql_parser / │──────────►│ tmdl_writer + dax_rewriter│───────────────►
 │ yaml_parser  │ (Pydantic)│ (resolves logical→Fabric  │
 └──────────────┘           │  Lakehouse Delta entity)  │
                            └───────────────────────────┘
                                       │
                                       │ (optional --pbip)
                                       ▼
                            <Name>.SemanticModel/  +  <Name>.pbip
                                       │
                                       │ sv2m deploy   /   mcp_powerbi-model_*
                                       ▼
                            Fabric workspace semantic model
```

### Parser highlights (`translator/parsers/sql_parser.py`)

- Hand-rolled (sqlglot can't parse Snowflake-specific `CREATE SEMANTIC VIEW`).
- Scans top-level keywords in fixed clause order: `TABLES → RELATIONSHIPS →
  FACTS → DIMENSIONS → METRICS → AI_VERIFIED_QUERIES`.
- `_split_csv_list` respects nested `(...)`, `'...'`, `"..."` — never use
  `str.split(",")`.
- Trailer clauses (`WITH SYNONYMS`, `LABELS`, `COMMENT`, `NON ADDITIVE BY`)
  are pre-stripped when they appear **before** the `AS <expr>` portion of a
  metric/dimension/fact item.

### Emitter highlights (`translator/emitters/tmdl_writer.py`)

- Emits one `<table>.tmdl` per logical table plus a shared `_Measures.tmdl`
  home for view-level derived metrics.
- All Direct Lake partitions share a single M expression `DatabaseQuery`
  (`expressions.tmdl`), pointing at the Lakehouse SQL endpoint.
- `expressionSource: DatabaseQuery`, `schemaName: <fabric_schema>`,
  `entityName: <fabric_table>` — **case must match OneLake exactly**
  (Snowflake-mirrored tables are upper-case).
- FK columns auto-emitted as `isHidden`; fact numeric columns default to
  `double`.
- Relationships: union-find pass marks cycle-closing edges as
  `isActive: false` to avoid PBI's
  `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH`.
- Compatibility level `1604` (Direct Lake classic).

### DAX rewriter (`translator/emitters/dax_rewriter.py`)

| Snowflake form | DAX |
|---|---|
| `SUM(col)` | `SUM('table'[col])` |
| `COUNT(*)` | `COUNTROWS('table')` |
| `COUNT(DISTINCT col)` | `DISTINCTCOUNT('table'[col])` |
| `AVG/MIN/MAX(col)` | `AVERAGE/MIN/MAX('table'[col])` |
| `metric_a / NULLIF(metric_b, 0)` | `DIVIDE([metric_a], [metric_b])` |
| `SUM(a + b - c)` | `SUMX('table', 'table'[a] + 'table'[b] - 'table'[c])` |
| `SUM(SUM(x)) OVER (ORDER BY d ROWS BETWEEN ...)` | `CALCULATE([measure], DATESINPERIOD(date[col], MAX(...), -12, MONTH))` |
| `DATEDIFF(year, a, b)` (calc columns) | `DATEDIFF(b, a, YEAR)` (inverted arg order) |
| `CURRENT_DATE()` (calc columns) | `TODAY()` |
| `IN ('a', 'b')` (calc columns) | `IN {"a", "b"}` (curly braces, double quotes) |

Measure references use bare bracket form `[measure]`, table columns use
`'table'[col]`.

### Mapping (`config/mapping.yml`)

```yaml
fabric:
  workspace_id:    "<guid>"
  workspace_name:  "SemanticModelDemo"
  lakehouse_id:    "<guid>"
  lakehouse_name:  "InsuranceActuarialLH"
  sql_endpoint:    "<dwh-sql-endpoint-fqdn>"

default_storage_mode: directlake   # directlake | import | directquery

calc_column_strategy: materialize  # materialize | dax (see INSTRUCTIONS § 10.7)
                                   # materialize = PySpark script adds physical columns
                                   # dax = DAX calc-column expressions in Import tables

namespace_map:
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL }
    fabric:    { schema: ACTUARIAL }   # ⚠ case-sensitive at refresh time

table_overrides: []
#   - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: FACT_POLICY }
#     fabric_table: fact_policy
#     storage_mode: import  # For hybrid models: Import tables can have DAX calc cols

model:
  name: "InsuranceActuarial"
  culture: "en-US"
  compatibility_level: 1604
```

---

## Quick start

### Prerequisites

- Python 3.10+ (developed on 3.13)
- PowerShell (commands below are PowerShell; bash equivalents are trivial)
- A Fabric workspace with a Lakehouse mirroring your Snowflake schema
  (see [Fabric Mirroring for Snowflake](https://learn.microsoft.com/fabric/database/mirrored-database/snowflake))
- For deploy: `az login` for `DefaultAzureCredential`, or service-principal
  env vars

### 1. Install

```pwsh
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# or, editable install with the sv2m console script:
pip install -e .
```

### 2. Build the Snowflake side (optional — for the demo schema)

```sql
-- run from a Snowflake worksheet
@mock_data/01_create_schema.sql
@mock_data/02_seed_data.sql
@mock_data/03_validation.sql
@semantic_view/insurance_actuarial.sql
```

### 3. Mirror the Snowflake schema into a Fabric Lakehouse

Use Fabric's UI: **New → Mirrored database → Snowflake**. The mirrored
tables land in your Lakehouse as **read-only shortcuts** under
`Tables/<SCHEMA>/<TABLE>`. Note the exact case — you will need it in
`mapping.yml`.

### 4. Configure `config/mapping.yml`

Fill in `workspace_id`, `lakehouse_id`, `sql_endpoint`, and any
`namespace_map` entries. Use the **physical** casing as it appears in
OneLake (typically Snowflake upper-case).

### 5. Translate

```pwsh
python -m translator.cli translate `
    semantic_view/insurance_actuarial.sql `
    --mapping config/mapping.yml `
    --out out_step5 `
    --pbip
```

Output:

```
out_step5/
├── InsuranceActuarial.SemanticModel/
│   └── definition/
│       ├── database.tmdl
│       ├── model.tmdl
│       ├── expressions.tmdl
│       ├── relationships.tmdl
│       └── tables/
│           ├── _Measures.tmdl
│           ├── dim_agent.tmdl
│           ├── dim_coverage.tmdl
│           ├── ...
│           └── fact_premium_txn.tmdl
└── InsuranceActuarial.pbip
```

### 6. Validate locally (Power BI Desktop)

Open `InsuranceActuarial.pbip` in Power BI Desktop. It should load without
errors. Browse the model tab to confirm tables, relationships, and measures.

### 7. Deploy to Fabric

**Recommended (MCP tools, full lifecycle):**

```text
1. mcp_powerbi-model_database_operations  Operation=ImportFromTmdlFolder
     tmdlFolderPath = out_step5\InsuranceActuarial.SemanticModel\definition
     connectionName = anything_local
2. mcp_powerbi-model_database_operations  Operation=DeployToFabric
     deployToFabricRequest = { newDatabaseName: "MyModel",
                               targetWorkspaceName: "SemanticModelDemo" }
3. mcp_powerbi-model_connection_operations  Operation=ConnectFabric
     workspaceName, semanticModelName
4. mcp_powerbi-model_model_operations  Operation=RefreshWithAPI  refreshType=Full
5. mcp_powerbi-model_model_operations  Operation=CheckStatusOfRefreshWithAPI
6. mcp_powerbi-model_model_operations  Operation=GetStats  (verify counts)
```

**Or via the built-in CLI (REST):**

```pwsh
python -m translator.cli deploy `
    out_step5\InsuranceActuarial.SemanticModel `
    --workspace-id <fabric-workspace-guid> `
    --name MyModel
```

Deployment uses
[`azure-identity.DefaultAzureCredential`](https://learn.microsoft.com/python/api/azure-identity/azure.identity.defaultazurecredential)
against scope `https://api.fabric.microsoft.com/.default`. The easiest path
is `az login`. For automation, set:

```pwsh
$env:AZURE_CLIENT_ID     = "<sp-app-id>"
$env:AZURE_TENANT_ID     = "<tenant-id>"
$env:AZURE_CLIENT_SECRET = "<secret>"
```

---

## CLI reference

```
sv2m parse      <input>
sv2m translate  <input> --mapping <yml> --out <dir> [--pbip]
sv2m deploy     <model_dir> --workspace-id <guid> --name <display-name>
sv2m roundtrip  --yaml <file.yaml> --sql <file.sql>
```

`<input>` may be `.sql` (Snowflake DDL) or `.yaml` (Snowflake YAML form).

| Subcommand | Purpose |
|---|---|
| `parse` | Parse and print the IR as JSON. Useful for inspecting what the parser saw. |
| `translate` | Parse + emit a `.SemanticModel` (and optionally a `.pbip` wrapper). |
| `deploy` | Push an emitted model folder to a Fabric workspace via REST. |
| `roundtrip` | Parse a YAML and SQL of the same view and report a structural summary diff. |

---

## Limitations & gotchas

These are the sharp edges. Full details in [INSTRUCTIONS.md](INSTRUCTIONS.md).

1. **Calculated columns require strategy choice.** Direct Lake tables cannot
   have DAX calculated columns. Choose `calc_column_strategy: materialize`
   (generates PySpark script to add physical columns — requires Contributor
   on lakehouse) or `calc_column_strategy: dax` (generates hybrid
   Import+DirectLake composite model — requires Import for tables with calc
   columns). For mirrored shortcuts (read-only), use `dax` strategy with
   per-table `storage_mode: import` overrides. See [INSTRUCTIONS § 10](INSTRUCTIONS.md).
2. **Mirrored tables are read-only.** `Tables/<schema>/*` from a
   Mirrored Database are OneLake shortcuts — no `ALTER`, no Spark writes.
   Use `calc_column_strategy: dax` + `storage_mode: import` overrides for
   tables with calculated columns.
3. **Schema/entity casing is case-sensitive at refresh.** Even though the
   SQL endpoint accepts any case, Direct Lake binding does not. Set
   `schemaName` and `entityName` to the exact OneLake casing.
4. **Window-function metrics auto-translate with caveats.** `SUM(SUM(x))
   OVER (...)` translates to `CALCULATE([inner_measure],
   DATESINPERIOD(...))`, but requires the translator to resolve Semantic
   View dimension names (e.g. `txn_date.txn_date`) to physical columns
   (e.g. `DATE_VAL`). Verify the generated DAX matches your intent.
5. **Semi-additive measures use `LASTNONBLANK` wrapping.** `NON ADDITIVE BY
   date_dim (LAST_VALUE BY sort_col)` auto-wraps the measure with
   `CALCULATE(<expr>, LASTNONBLANK(date_dim[sort_col], 1))`. The dimension
   and sort column must exist in the model. Reverse chronological (latest
   snapshot) uses `LASTNONBLANK`; forward chronological uses
   `FIRSTNONBLANK`.
6. **Role-playing dates use Option A.** Duplicate logical date tables
   (`effective_date`, `txn_date`, `loss_date`, `reserve_date`) all
   bound to the same physical `DIM_DATE`. All FK relationships stay
   active — no `USERELATIONSHIP` needed.
7. **Cycle-breaking is greedy.** Union-find marks the first cycle-closing
   edge `isActive: false`. Re-order relationships in the source SQL if
   you need a specific active path.
8. **TMDL is indentation-sensitive.** Tabs only. Hand-edits that introduce
   spaces will fail to import.
9. **`AI_VERIFIED_QUERIES` is parsed but not emitted** — could feed a
   Q&A linguistic schema in the future.
10. **Hybrid composite models need Import table refresh.** After deploying
    a model with `storage_mode: import` tables, trigger a Full refresh via
    the Fabric portal or Power BI REST API before the model is usable.

---

## Development

### Run the tests

```pwsh
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

The test suite covers:

- `test_roundtrip.py` — parses both `insurance_actuarial.yaml` and
  `insurance_actuarial.sql` and asserts a structural match.
- `test_emitter.py` — emits TMDL for fixture SVs and asserts against
  golden snapshots.

> Note: when you grow the SQL fixture (e.g. split a single date dim into
> role-playing copies), the hard-coded counts in `test_roundtrip.py` need
> to be updated, or the YAML fixture regenerated.

### Adding a new Snowflake-semantic-view feature

1. Add a `semantic_view/stepN.sql` with the smallest possible example.
2. Extend `translator/ir/model.py` with new fields if needed.
3. Extend `translator/parsers/sql_parser.py` to populate them.
   - If the new clause can appear in arbitrary order around `AS`,
     **pre-strip** it (see `_RE_NON_ADD` handling in `_parse_metrics`).
4. Extend `translator/emitters/tmdl_writer.py` to emit it.
5. Add a regression test.
6. Run `python -m pytest -q`, then translate + open in Desktop, then
   deploy + refresh in Fabric. **All three** must pass.

### Iteration ladder (existing examples)

| File | What it adds |
|---|---|
| `minimal.sql` | Smallest valid SV |
| `step1.sql` | 2 tables, 1 metric |
| `step2.sql` | Synonyms, comments, labels |
| `step3.sql` | Multiple facts/dims, derived metrics |
| `step4.sql` | Composite keys, tags |
| `insurance_actuarial.sql` (step 5) | Full 10-table star, role-playing dates, semi-additive, window fn |

---

## Reference: the deployed `test1v3` model (hybrid composite)

A successful end-to-end deploy of `insurance_actuarial.sql` with
`calc_column_strategy: dax` produces a **hybrid Import+DirectLake composite
model**:

| Metric | Count |
|---|---:|
| Tables | 14 (10 business + 4 role-date + `_Measures`) |
| Storage modes | 3 Import (with calc columns) + 11 Direct Lake |
| Relationships | 14 (12 active, 2 inactive — `claim_to_coverage`, `reserve_to_coverage`) |
| Physical columns | 103 |
| **DAX calculated columns** | **5** (`age_years`, `is_active`, `is_open_claim`, `is_cat_claim`, `incurred_loss`) |
| Measures | 20 (including 2 semi-additive with `LASTNONBLANK` wrapping) |
| Partitions | 3 Import + 11 Direct Lake (over `AzureStorage.DataLake` connector) |
| Compatibility level | 1604 |
| Connector | `AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/...")` |

**Import tables** (with calculated columns):
- `dim_policyholder` (1 calc column: `age_years`)
- `fact_policy` (1 calc column: `is_active`)
- `fact_claim` (3 calc columns: `is_open_claim`, `is_cat_claim`, `incurred_loss`)

**Direct Lake tables** (no calc columns):
- All date dimensions, dimension tables, and transactional facts

End-to-end workflow:
1. Deploy model via Fabric REST API (or MCP tools)
2. Trigger Full refresh on Import partitions (~10 sec for 3 tables)
3. Direct Lake tables bind instantly (~0 sec framing)

**Key lessons** (see [INSTRUCTIONS § 10](INSTRUCTIONS.md) for details):
- Direct Lake SQL connector (`Sql.Database`) cannot have calc columns even when service supports them
- Direct Lake OneLake connector (`AzureStorage.DataLake`) supports calc columns in preview (CL ≥ 1604) but is unreliable via MCP deploy
- **Production workaround**: Hybrid model with Import overrides for tables needing calc columns
- Deployed via Fabric Items REST API with `azure-identity.DefaultAzureCredential`

---

## Further reading

- [INSTRUCTIONS.md](INSTRUCTIONS.md) — the full field guide and lessons
  learned (parser rules, TMDL gotchas, deploy recipe, ambiguity fix,
  Direct Lake limits).
- [Snowflake — CREATE SEMANTIC VIEW](https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view)
- [Fabric — Direct Lake overview](https://learn.microsoft.com/fabric/fundamentals/direct-lake-overview)
- [Fabric — Mirrored databases (Snowflake)](https://learn.microsoft.com/fabric/database/mirrored-database/snowflake)
- [TMDL reference](https://learn.microsoft.com/analysis-services/tmdl/tmdl-overview)
- [Fabric REST — Semantic Models](https://learn.microsoft.com/rest/api/fabric/semanticmodel/items)

---

## License

POC / internal — no license declared. Treat as "all rights reserved" until a
LICENSE file is added.
