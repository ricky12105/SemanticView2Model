# SemanticView2Model — Translator Instructions & Lessons Learned

A field guide for anyone extending the Snowflake `CREATE SEMANTIC VIEW` → Power BI
TMDL/PBIP translator. Captures every non-obvious rule we discovered while
iterating from a 2-table toy view up to the full 10-table `insurance_actuarial`
model and pushing the result through Power BI Desktop and Fabric DirectLake.

---

## 1. Project shape

```
translator/
  parsers/       sql_parser.py (hand-rolled), yaml_parser.py, tmdl_parser.py (reverse)
  ir/            pydantic v2 models (SemanticView, LogicalTable, Metric, ...)
  emitters/      tmdl_writer.py, dax_rewriter.py, mapping.py,
                 sv_writer.py (IR→Snowflake DDL), dax_to_sql.py (DAX→SQL, reverse)
  diff.py        structural drift between two SemanticView IRs
  reports/       markdown.py (drift + change reports)
  deploy/        fabric_client.py (deploy + get_semantic_model pull),
                 snowflake_client.py (execute DDL)
  cli.py         translate / deploy / pull / reverse / diff / sync / roundtrip
semantic_view/   sample SVs (step1..step5 = insurance_actuarial.sql)
config/          mapping.yml (logical → Fabric workspace/lakehouse/schema)
tests/           pytest (14 tests today, all must stay green)
```

Iterative complexity ladder: `step1` (2 tables, 1 measure) → `step5`
(10 tables, 14 relationships, derived metrics). Add a new step whenever you
introduce a new semantic-view feature.

---

## 2. Snowflake `CREATE SEMANTIC VIEW` parser rules

The official spec is the source of truth:
<https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view>

### 2.1 Clause order matters
`TABLES → RELATIONSHIPS → FACTS → DIMENSIONS → METRICS → AI_VERIFIED_QUERIES`.
The parser scans for top-level keywords in this order; an out-of-order clause is
silently ignored. Always preserve this order in source SQL.

### 2.2 `LABELS = (...)` must be stripped BEFORE other trailers
Snowflake supports `LABELS = (FILTER)` (or `MEASURE`, `DIMENSION`, etc.) on
dimensions/facts/metrics. Our regex for `WITH SYNONYMS`/`COMMENT` would
mis-tokenise if `LABELS` is left in place. The parser pre-strips it. **Lesson:**
if you add a new trailer keyword, pre-strip it the same way or update the
trailer regex to be aware of it.

### 2.3 Comma-splitting inside parenthesised lists
`_split_csv_list` must respect nested `(...)`, `'...'`, and `"..."`. A naïve
`str.split(",")` will break on `WITH SYNONYMS = ('a','b')`. Reuse the existing
helper for any new comma-separated clause.

### 2.4 Role-playing dims are NOT automatic
`CREATE SEMANTIC VIEW` allows declaring the same physical table multiple times
under different logical names (`effective_date AS ...DIM_DATE`,
`txn_date AS ...DIM_DATE`, …). The parser correctly produces one `LogicalTable`
per alias. **But if the source declares only one `dim_date` and points several
FKs at it, the parser cannot invent role aliases for you** — see §4.2.

---

## 3. IR conventions

* `LogicalTable.name` is the **logical** name from the `TABLES` clause; it is
  the identifier used everywhere downstream (columns, relationships, DAX).
* `LogicalTable.base_table` is the physical `DB.SCHEMA.TABLE` resolved via
  `config/mapping.yml` to a Fabric `FabricTable` (schema + table + storage
  mode).
* Column names in IR are stored **in the case the SV author used** (typically
  uppercase, matching Snowflake DDL). The emitter keeps the *physical* casing
  on the `sourceColumn:` property, but the *logical* TMDL column name is the
  SV `DIMENSIONS` alias when one is declared (e.g. `effective_date_key AS
  DATE_KEY` → TMDL column `effective_date_key`, `sourceColumn: DATE_KEY`).
  Relationship emission uses `_lookup_logical_column` to bridge SV physical
  refs back to the declared logical name — see § 4.9.
* `primary_key` is a list of column names; the emitter auto-creates hidden
  columns for any PK not otherwise declared (see §4.3).

---

## 4. TMDL emission rules (the hard-won ones)

### 4.1 Calculated columns and Direct Lake modes
**Two Direct Lake variants** (detailed in §10.1):
* **Direct Lake on SQL**: Uses `Sql.Database(endpoint, lakehouse)` connector.
  **Forbids all calculated columns**. Universal availability.
* **Direct Lake on OneLake**: Uses `AzureStorage.DataLake(onelake_url, [HierarchicalNavigation=true])`
  connector. **Supports DAX calculated columns** (preview, CL ≥ 1604).
  Deployment reliability via MCP is inconsistent (§10.1).

**Handling calculated columns** (dimensions/facts with `AS <expr>`):
* **Strategy 1 — Materialize** (`calc_column_strategy: materialize` in mapping.yml):
  Generate PySpark script to `ALTER TABLE ADD COLUMNS` + `UPDATE` in the
  lakehouse. Emitter treats them as physical `sourceColumn` refs. Uses
  `Sql.Database` connector. Blocked when lakehouse tables are read-only
  shortcuts (§10.2).
* **Strategy 2 — DAX calc columns** (`calc_column_strategy: dax`):
  Emit as `column 'name' = <DAX expr>` blocks. Requires Import or DirectQuery
  mode for affected tables. Recommended for mirrored/read-only lakehouses:
  mark calc-col tables as `storage_mode: import` via `table_overrides` in
  mapping.yml (§10.3).
* **Strategy 3 — Upstream computation**: Add columns in Snowflake before the
  mirror syncs. Emitter sees them as bare physical columns.

**Production pattern**: Hybrid composite model (§10.3) — Import mode for
calc-col tables, Direct Lake for the rest.

### 4.2 Ambiguous relationship paths break Power BI Desktop
Symptom: `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` on model load:
> *"There are ambiguous paths between 'fact_claim' and 'dim_date': ..."*

Cause: Power BI requires the relationship graph to be a **forest** (no cycles,
no duplicate paths). When several facts share one `dim_date` AND there are
fact-to-fact relationships, the union of edges has cycles.

Fix implemented in `_relationships_tmdl`: greedy union-find over logical tables.
The first edge connecting two components stays active; every subsequent edge
that would close a cycle gets `isActive: false`. Consumers re-activate it at
query time with `USERELATIONSHIP(...)`.

**Better long-term fix (not yet implemented):** emit role-playing alias tables
when one physical dim is referenced by multiple FKs. Each alias points at the
same `entityName` but appears as a distinct table in PBI, so each direct
relationship is unambiguous. See §8 backlog.

### 4.3 Auto-emit PK and FK columns
Relationships reference columns by name. If a PK or FK isn't already declared
as a dimension or fact, the emitter would produce dangling `fromColumn`/
`toColumn` references and TMDL refuses to load. We auto-emit hidden `string`
columns for:
* every entry in `LogicalTable.primary_key`
* every `left_column`/`right_column` of every relationship that targets this
  table (and is not already declared)

Always run this de-dup against the `seen` set in `_table_tmdl`.

### 4.4 Fact numeric default = `double`
A bare `SUM(x)` fact with no explicit type would default to `string` if we used
the dimension default. That made every numeric measure error out. Rule: in
`_table_tmdl`, facts with unknown `data_type` get `dataType: double`.

### 4.5 Measure DAX must be one line
TMDL parses multi-line property values as separate properties. `_measure_block`
flattens the DAX expression with `" ".join(dax.split())` before emitting.
Comments inside DAX are stripped upstream by the rewriter.

### 4.6 Documentation comments use `///`
TMDL uses Roslyn-style `///` doc comments above an object, not `//` or `/* */`.
`_doc_lines` enforces this. Synonyms become `annotation Synonyms = "a", "b"`.

### 4.7 Shared `DatabaseQuery` M expression
DirectLake tables all reference an `expressionSource: DatabaseQuery` defined
once in `definition/expressions.tmdl`. Do not inline the SQL endpoint per
partition.

### 4.8 PBIP folder layout (matches what Power BI Desktop writes)
```
out/<Model>.SemanticModel/
  .platform
  definition.pbism
  diagramLayout.json
  definition/
    database.tmdl
    model.tmdl
    expressions.tmdl
    cultures/<culture>.tmdl
    tables/<table>.tmdl  (one per logical table + `_Measures.tmdl` if any view-level metrics)
    relationships.tmdl
out/<Model>.pbip         (only with --pbip; references the .SemanticModel folder)
```
The `.platform` file's `logicalId` must be `00000000-0000-0000-0000-000000000000`
when Power BI is expected to assign one on first save.

### 4.9 Relationships must reference logical column names, not physical
Snowflake `RELATIONSHIPS` clauses reference the *physical* PK/FK column
(`REFERENCES effective_date(DATE_KEY)`), but when the same column is also
declared in `DIMENSIONS` under a logical alias
(`effective_date.effective_date_key AS DATE_KEY`), the emitter names the TMDL
column `effective_date_key`, not `DATE_KEY`. TMDL is case-insensitive on
column names, so single-key dim relationships often work by accident
(`policyholder_key` vs `POLICYHOLDER_KEY`). **Role-playing date dims break
this** — `effective_date.DATE_KEY` won't resolve because the column is
named `effective_date_key`.

Deploy fails with:
`Property ToColumn of object "relationship policy_to_effective_date" refers to
an object which cannot be found`.

**Fix** (`_lookup_logical_column` in `tmdl_writer.py`): for each relationship
endpoint, walk the target table's dimensions + facts looking for a bare
identifier `expr` whose upper-cased form matches the physical column
referenced in the SV. Return the logical `name` when found, else the physical
name as fallback (which is what auto-emitted FK columns use). Applied to both
`fromColumn` and `toColumn` in `_relationships_tmdl`.

---

## 5. DAX rewriter (`dax_rewriter.py`)

### 5.1 Column ownership is per-table, not global
Bug we hit: `total_case_reserves` on `fact_loss_reserve` rewrote
`CASE_RESERVE` to `'fact_claim'[CASE_RESERVE]` because the global `column_owner`
map registered `fact_claim` first.

**Rule:** `rewrite_metric` takes a `local_columns: set[str] | None`. When a
bare column ref is found AND the column exists on the owning table, always
prefer the owning table. Only fall back to `column_owner` for columns that
genuinely live on another table.

```python
def _resolve(col: str) -> str | None:
    if local_columns and owning_table_dax and col in local_columns:
        return owning_table_dax
    return column_owner.get(col, owning_table_dax)
```

### 5.2 View-level (holding-table) metrics have no owning table
`_holding_table_tmdl` calls `rewrite_metric` with `owning=None` and
`local_columns=None`. The rewriter must fall back to `column_owner` for every
reference. Test this path whenever you change resolution logic.

### 5.3 Metric references use `[name]`, not `'table'[name]`
DAX measure references are bare bracket form. The `metric_owner` map stores
`"[MeasureName]"` keyed by both `"table.metric"` (lowercase) and `"metric"`.
Look up the qualified form first.

### 5.4 SQL → DAX translation scope
We translate a deliberate subset:
* `SUM`, `COUNT`, `AVG`, `MIN`, `MAX`, `COUNT(DISTINCT ...)` → native DAX
  aggregations.
* Bare arithmetic between metric refs.
* **Window functions** — trailing-N-period patterns: `SUM(SUM(x)) OVER (ORDER BY date ROWS BETWEEN N PRECEDING AND CURRENT ROW)`
  translates to `CALCULATE(SUM(...), DATESINPERIOD(date, LASTDATE(date), -(N+1), MONTH))`.
  Requires `dim_resolver` callable to map SV dim names to physical columns (§10.5).
  Other window functions (RANK, LAG, LEAD, etc.) are not yet supported.
* `a / NULLIF(b, 0)` → `DIVIDE(a, b)`.

Anything else should raise rather than silently mis-translate.

---

## 6. `mapping.yml` rules

* Logical table name → `{ workspace, lakehouse, schema, table, storage_mode }`.
* Storage mode is per-table. Mixing `directlake` and `import` in one model is
  legal but every DirectLake table must come from the same Lakehouse SQL
  endpoint.
* When a physical table is shared across multiple logical tables (role aliases
  or future role-playing), every alias must point at the same Fabric table —
  the resolver does not enforce this, the human author does.

---

## 7. Validation workflow (do this every iteration)

1. `.venv\Scripts\python.exe -m pytest -q` — all tests green.
2. `python -m translator.cli translate semantic_view/<file>.sql --mapping config/mapping.yml --out out_stepN --pbip`
3. Inspect:
   * `out_stepN/<Model>.SemanticModel/definition/tables/*.tmdl`
   * `out_stepN/<Model>.SemanticModel/definition/relationships.tmdl` —
     verify any `isActive: false` flags are intentional.
   * `out_stepN/<Model>.SemanticModel/definition/expressions.tmdl` —
     verify connector type matches strategy (Sql.Database vs AzureStorage.DataLake).
   * If `calc_column_strategy: materialize`, check `out_stepN/<Model>.materialize.py`
     for correct Spark SQL expressions.
4. Open the `.pbip` in Power BI Desktop. Common errors:
   * `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` → §4.2.
   * "Calculated columns not allowed in DirectLake" → §4.1 / §10.1.
   * "Column not found" in measure → §5.1 (per-table ownership) or §4.3
     (missing PK/FK auto-emit).
5. Deploy to Fabric:
   * **Option A (MCP)**: Via MCP PowerBI-Model server (§9.8) — reliable for
     DL/SQL models, inconsistent for DL/OL with calc cols.
   * **Option B (REST API)**: `python -m translator.cli deploy <model_dir> --workspace-id <guid> --name <name>`
     (§10.6) — most reliable, requires `az login` or VS Code auth.
6. For composite models (Import + Direct Lake), trigger refresh on Import tables
   via Fabric portal or refresh API before validating data.

---

## 8. Backlog / known limitations

* **Role-playing alias generation (preferred fix for §4.2).** Algorithm:
  group relationships by `right_table`; when count > 1, clone the dim as
  `<right_table>__<fk_column>` logical tables, redirect each relationship to
  its alias, copy dimensions/columns. Then no `isActive: false` is needed.
  * Alternative: current union-find cycle-breaking works cleanly when dims
    are declared separately (§9.5); consider this solved for most cases.
* ~~**Window functions in metrics.**~~ ✅ **DONE (§10.5)**: `DATESINPERIOD`
  translation with `dim_resolver` handles trailing-N-month patterns.
  Remaining: other window functions (RANK, LAG, etc.) still need templates.
* ~~**Semi-additive measures.**~~ ✅ **DONE (§10.4)**: `LASTNONBLANK` /
  `FIRSTNONBLANK` wrapping for `NON ADDITIVE BY` metrics implemented.
* **`AI_VERIFIED_QUERIES` clause.** Parsed but not emitted anywhere; could
  become PBI Q&A linguistic schema entries or `.Report` page annotations.
* **Many-to-many cardinality** is not yet inferred; all relationships are
  emitted as default (single, many-to-one). Add `crossFilteringBehavior` /
  `cardinality` detection when source declares it.
* ~~**Fabric publish via REST.**~~ ✅ **DONE (§10.6)**: `translator.deploy.fabric_client`
  with `DefaultAzureCredential` + Items API.
* ~~**Bidirectional sync + drift.**~~ ✅ **DONE (§12, v1.1)**: reverse pipeline
  (TMDL→IR→Snowflake DDL), drift report, `pull`/`reverse`/`diff`/`sync` CLI,
  optional Snowflake DDL execution.
* **Calc column data-type inference.** Current heuristic (`_map_data_type` in
  `dax_rewriter.py`) guesses based on operators; consider AST-based inference
  or explicit type annotations in SV source.
* **Import partition M query optimization.** Currently generates
  `Source{{[Schema="...",Item="..."]}}[Data]` for every Import table; could
  batch via single `Sql.Database` call with table list for better refresh perf.
* **Incremental refresh metadata.** Power BI supports incremental refresh on
  Import tables; consider emitting `annotation PBI_RefreshPolicy` when SV
  declares a partitioning strategy.
* **Composite model refresh orchestration.** When mixing Import + Direct Lake,
  Import tables need scheduled refresh; could auto-generate a Fabric Data
  Pipeline or notebook to trigger refresh + monitor.
* **Direct Lake on OneLake reliability.** MCP `DeployToFabric` inconsistently
  rejects DL/OL models with calc columns (§10.1); track Fabric service updates
  and re-test when GA.
* **Calc-column name casing.** Materialized calc cols with lowercase names
  (e.g. `is_active`) don't register in `column_owner` (§10.9); consider
  auto-uppercasing all calc-col names or adjusting the DAX rewriter to handle
  mixed-case lookups.

---

## 11. House rules for contributors

* Every new SV feature gets a new `step<N>.sql` and a regression test.
* Never let `column_owner` resolution be the only check — always pass the
  per-table `local_columns` set.
* Always run pytest **and** open the generated PBIP in Desktop before claiming
  done. TMDL that parses is not TMDL that loads.
* When PBI Desktop reports an error, capture the exact error code
  (e.g. `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH`) — it's the fastest path to
  the right Power BI docs page.
* **Calc column strategy**: Default to `materialize` for owned lakehouses,
  `dax` + Import overrides for mirrored shortcuts (§10.7).
* Keep DAX measure expressions single-line in TMDL.
* When adding new DAX rewrite patterns, always pass `dim_resolver` to handle
  SV dimension name → physical column mapping (§10.5).
* Test composite models (Import + Direct Lake) in Desktop before deploying —
  cross-partition filters can behave unexpectedly.
* Document calc-col strategy choice in `mapping.yml` comments for future
  maintainers.

---

## 9. Direct Lake deployment lessons (test1v2 run, May 2026)


This run translated the full `insurance_actuarial.sql` (4 role-date tables, 10
business tables, 14 relationships, 20 measures) and successfully deployed it to
Fabric workspace `SemanticModelDemo` as Direct Lake semantic model `test1v2`,
sourced from lakehouse `InsuranceActuarialLH` (schema `ACTUARIAL`, mirrored
from Snowflake `INSURANCE_POC.ACTUARIAL`).

### 9.1 Parser: `NON ADDITIVE BY` may appear before `AS`
Snowflake allows `NON ADDITIVE BY (...)` to sit between `tbl.col` and the
`AS <expr>` (not only as a trailer after the expression). The original
`_parse_metrics` only handled it as a post-AS trailer and raised
`Unparseable metric item`.

Fix (in `translator/parsers/sql_parser.py::_parse_metrics`): mirror the
existing `LABELS` pre-strip — detect `_RE_NON_ADD` at depth 0 before the first
`AS`, consume its parenthesised body via `_consume_paren`, parse with
`_parse_non_additive`, splice it out of the item text, then merge the result
back into trailers after `_extract_trailers` runs.

**General rule:** any Snowflake clause that can legally appear either before
or after `AS` must be pre-stripped the same way (`LABELS`, `NON ADDITIVE BY`,
and any future trailer).

### 9.2 Direct Lake (classic) blocks calculated columns
Deploying with any `column foo = <DAX>` on a Direct Lake table fails with:
`Calculated column 'foo' is not allowed in Direct Lake table 'X'. Please
remove the calculated columns. See aka.ms/fabric-directlake-limits`.

Implications for the translator:
* SVs with `AS <expr>` on dimensions/facts (calc dims / private facts)
  cannot ship as Direct Lake calc columns. Options:
  1. Materialise via a Spark notebook into new Delta tables in the
     lakehouse's writable schema (e.g. `dbo`), then add them as additional
     Direct Lake tables.
  2. Convert to DAX **measures** when the semantics permit (counts/sums).
     Boolean FILTER columns (`is_active`, `is_open_claim`, etc.) generally
     cannot be expressed as measures and require option 1.
* Note: this is the *classic* Direct Lake limit. Direct Lake on OneLake
  (CL ≥ 1604, certain tenants) does allow calc columns; if you target that
  variant, gate the emission behind a mapping flag.

### 9.3 Lakehouse entity casing must match OneLake exactly
The translator emitted `schemaName: Actuarial` / `entityName: dim_agent`
(lower-case), but the mirrored lakehouse exposes them as `ACTUARIAL` /
`DIM_AGENT` (Snowflake-style upper-case). Direct Lake binding is
case-sensitive at refresh time even though the lakehouse SQL endpoint is not.

**Fix:** uppercase `schemaName` and `entityName` in every DirectLake
partition. This should be driven from `mapping.yml` (record the *physical*
casing as it lands in OneLake, not the lowercase logical name).

**Automation** (added post-`test1v2`): set `preserve_table_case: true` at the
top level of `mapping.yml`. `MappingConfig.resolve()` then returns the source
DDL casing verbatim instead of lower-casing. Default remains `false` so
existing outputs (e.g. `out_step5`, which targeted `InsuranceActuarialLH`
with lowercase tables) keep working. Combine with a `namespace_map` entry
whose `fabric.schema` matches OneLake case (usually upper-case for Snowflake
mirrors).

### 9.4 TMDL is indentation-sensitive — tabs only, exactly one level
A measure line written with no leading tab parses cleanly visually but the
TMDL importer errors with:
`TMDL Format Error: Parsing error type - Indentation`.

Rules confirmed:
* Use **tab** characters for indentation, never spaces.
* `measure` / `column` / `partition` inside a `table` block must be indented
  exactly one tab.
* Child properties (`dataType:`, `sourceColumn:`, `summarizeBy:`,
  `annotation`) must be indented exactly two tabs.
* When hand-editing post-emit, verify with
  `Get-Content file.tmdl -Raw` and look for stray spaces.

### 9.5 Role-playing dates: option A works cleanly
Four separate logical date tables (`effective_date`, `txn_date`, `loss_date`,
`reserve_date`), each as its own Direct Lake partition over the same
`ACTUARIAL.DIM_DATE` entity, with all four FK relationships **active**.
No `USERELATIONSHIP` plumbing needed; measures reference the role table
directly (e.g. `DATESINPERIOD('txn_date'[DATE_VAL], ...)`).

### 9.6 Window-function metrics still need hand translation
`t12m_earned_premium AS SUM(SUM(EARNED_PREMIUM))` (Snowflake window form) does
not auto-translate. Manual rewrite:
`CALCULATE([total_earned_premium], DATESINPERIOD('txn_date'[DATE_VAL], MAX('txn_date'[DATE_VAL]), -12, MONTH))`.
Backlog item to add a `WINDOW_FN → DATESINPERIOD/DATEADD` template.

### 9.7 `NON ADDITIVE BY` is emitted as a measure annotation, not semantics
Current emitter writes
`annotation NonAdditiveBy = "reserve_date.reserve_date descending NULLS LAST"`
but the measure is still plain `SUM(...)`. True semi-additive behaviour
requires wrapping with `LASTNONBLANK(reserve_date[DATE_VAL], 1)` or similar.
Backlog: enable a `--semi-additive=lastnonblank` flag that rewrites the DAX.

### 9.8 Deploy → connect → refresh recipe (MCP tools)
1. `mcp_powerbi-model_database_operations` Operation=`ImportFromTmdlFolder`,
   `tmdlFolderPath` = `out_step5\<ModelName>.SemanticModel\definition`,
   `connectionName` = `<anything>_local`.
2. Same tool, Operation=`DeployToFabric`, `deployToFabricRequest` =
   `{ newDatabaseName, targetWorkspaceName }`.
3. `mcp_powerbi-model_connection_operations` Operation=`ConnectFabric`,
   `{ workspaceName, semanticModelName }`. Capture the returned
   `connectionName` (`Fabric-<ws>-<model>`).
4. `mcp_powerbi-model_model_operations` Operation=`RefreshWithAPI`,
   `refreshType: Full`. Capture `requestId`.
5. Poll with same tool Operation=`CheckStatusOfRefreshWithAPI` until
   `status: Completed`. Direct Lake refresh on a mirrored lakehouse is
   typically <30s (no data movement, framing only).
6. `Operation=GetStats` to confirm `TableCount` / `RelationshipCount` /
   `TotalMeasureCount` match expectations.

### 9.9 Mirrored-lakehouse tables are read-only Delta
Tables under `Tables/ACTUARIAL/*` in `InsuranceActuarialLH` are OneLake
**shortcuts** to a MirroredDatabase (`INSURANCE_POC`). They cannot be altered
in place — no `ALTER TABLE ADD COLUMN`, no Spark write. To materialise
derived columns physically, write new Delta tables into the writable `dbo`
schema via a Spark notebook, then expose them as additional Direct Lake
tables in the model.

### 9.10 Listing tables on a schemas-enabled lakehouse
The classic `GET /lakehouses/{id}/tables` REST endpoint returns
`UnsupportedOperationForSchemasEnabledLakehouse` when the lakehouse has
schemas turned on. Use OneLake DFS instead:
`GET https://onelake.dfs.fabric.microsoft.com/{workspace}/{lakehouse}.Lakehouse/Tables/{schema}?resource=filesystem&recursive=false`.

### 9.11 Stale roundtrip tests
After SQL fixture grows (e.g. splitting one `dim_date` into four role
tables), `tests/test_roundtrip.py` hard-coded counts (`assert len(v.tables) == 10`)
go stale. Either regenerate the YAML fixture from the SQL, or move counts
into per-step fixtures so old steps stay green.

### 9.12 MirroredDatabase as the Direct Lake source (`InsuranceActuarial` run, Jul 2026)
A MirroredDatabase item is itself a valid Direct Lake source — there is no
requirement to layer an owned Lakehouse with shortcuts on top of it. The
OneLake path
`https://onelake.dfs.fabric.microsoft.com/{workspace}/{mirroredDbId}/Tables/{SCHEMA}/{TABLE}`
resolves to the same Delta files the Lakehouse shortcut would, so the
`AzureStorage.DataLake` M expression works unchanged.

Worked example (`config/mapping.insurance_poc.yml`):
```yaml
fabric:
  workspace_id: "00000000-0000-0000-0000-000000000000"    # SemanticModelDemo
  lakehouse_id: "00000000-0000-0000-0000-000000000000"    # INSURANCE_POC MirroredDatabase
  lakehouse_name: "INSURANCE_POC"
  sql_endpoint: "...datawarehouse.fabric.microsoft.com"    # from mirroredDatabases API
preserve_table_case: true
namespace_map:
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL }
    fabric:    { schema: ACTUARIAL }
table_overrides:
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: dim_policyholder }
    storage_mode: import   # age_years calc col
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: fact_policy }
    storage_mode: import   # is_active FILTER dim
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: fact_claim }
    storage_mode: import   # is_open_claim / is_cat_claim FILTER dims
```

Discovery cheat sheet (all `az rest --resource https://api.fabric.microsoft.com`):
* `GET /v1/workspaces` — find the workspace GUID by `displayName`.
* `GET /v1/workspaces/{id}/items?type=MirroredDatabase` — list mirrored DBs.
* `GET /v1/workspaces/{id}/mirroredDatabases/{id}` — returns
  `properties.sqlEndpointProperties.connectionString` (the `sql_endpoint`
  value) and `properties.defaultSchema`.
* Schema/table discovery via the OneLake table API
  (`mcp_fabric_mcp_se_onelake_list_table_namespaces` /
  `mcp_fabric_mcp_se_onelake_list_tables`) confirms the exact casing to feed
  `namespace_map` / `preserve_table_case`.

Tradeoffs vs. the Lakehouse-shortcut pattern (§9.9):
* **Pro**: one less item to provision and manage; no shortcut refresh lag.
* **Pro**: the mirrored DB's SQL analytics endpoint is available directly
  for Import-mode partitions.
* **Con**: still read-only — `calc_column_strategy: materialize` remains
  blocked. Use `dax` + per-table Import overrides (as above).
* **Con**: no place to stage `dbo`-schema materialized tables. If you need
  materialized calc cols, layer a Lakehouse on top with shortcuts and
  write derived tables into its writable schema.

---

## 10. Calculated columns deep-dive (test1v3 run, May 2026)

This run addressed the calculated-column deployment blocker discovered in 9.2.
After hitting repeated deployment failures with Direct Lake on OneLake (DL/OL)
models containing DAX calc columns, we implemented a dual-strategy system and
successfully deployed a **hybrid composite model** (`test1v3`) mixing Import
mode (for tables with calc columns) + Direct Lake (for mirrored shortcuts).

### 10.1 Two Direct Lake flavors: SQL vs OneLake
Microsoft Fabric supports two Direct Lake modes with different capabilities:

**Direct Lake on SQL** (classic, universally available):
* Connector: `Sql.Database("endpoint", "lakehouse")` in expressions.tmdl
* Source: Lakehouse SQL endpoint
* **Forbids all calculated columns and calculated tables**
* Proven reliable deployment path via both MCP and REST API

**Direct Lake on OneLake** (preview, CL ≥ 1604):
* Connector: `AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{wsId}/{lhId}", [HierarchicalNavigation=true])`
* Source: OneLake Delta storage root
* **Supports unmaterialized DAX calculated columns** (per Microsoft Learn docs)
* Requires `compatibilityLevel: 1604+`
* **MCP `DeployToFabric` path inconsistently rejects these models** — three
  deployment attempts with correct connector all failed with "Calculated
  column not allowed in Direct Lake table" despite proper DL/OL metadata

### 10.2 Calc column strategy configuration
Added `calc_column_strategy` to `mapping.yml` with two modes:

**`materialize` strategy** (default):
* Pushes calculated columns upstream to physical Delta tables in the lakehouse
* Generates a PySpark script (`<model>.materialize.py`) with idempotent
  ALTER TABLE + UPDATE statements
* Snowflake → Spark SQL rewriter: `CURRENT_DATE()` → `current_date()`,
  `DATEDIFF(year,a,b)` → `CAST(FLOOR(months_between(b,a)/12) AS BIGINT)`, etc.
* Emitter treats materialized calc columns as physical `sourceColumn` refs
* Uses `Sql.Database` connector (DL/SQL)
* **Blocked by mirrored shortcuts**: tables under `Tables/{schema}/*` in
  mirrored lakehouses are OneLake shortcuts, read-only, no ALTER/UPDATE allowed

**`dax` strategy**:
* Emits calculated columns as in-model DAX (`column 'foo' = <expr>`)
* Uses `AzureStorage.DataLake` connector (DL/OL)
* Requires **Import or DirectQuery mode** for tables with calc columns when
  deploying to production (DL/OL deployment unreliable via MCP)

### 10.3 Hybrid composite models (the production workaround)
When source data is read-only (mirrored shortcuts) AND you need calculated
columns, use a **composite model**:

**Configuration** (in `mapping.yml`):
```yaml
calc_column_strategy: dax
table_overrides:
  - snowflake: { database: DB, schema: SCHEMA, table: TABLE_WITH_CALC_COLS }
    storage_mode: import
```

**Result**:
* Tables with calc columns: **Import mode** with DAX calc columns, data pulled
  from lakehouse SQL endpoint into model storage
* Tables without calc columns: **Direct Lake mode**, live query to mirrored
  shortcuts (no data duplication)

**Benefits**:
* No lakehouse modifications needed
* Works with read-only mirrored shortcuts
* Calc-col tables get full DAX expressiveness
* Other tables retain Direct Lake query performance

**Tradeoffs**:
* Import tables require periodic refresh (via Fabric refresh API)
* Calc-col tables lose Direct Lake's sub-second latency
* Model storage grows proportional to imported data volume

### 10.4 Semi-additive measures: LASTNONBLANK implementation
Snowflake's `NON ADDITIVE BY (dim DESC)` semantics require wrapping the
measure in `LASTNONBLANK`:

**Original IR**:
```python
Metric(
  name="total_case_reserve",
  expr="SUM(CASE_RESERVE)",
  non_additive_dimensions=[
    NonAdditiveDim(table="reserve_date", dimension="reserve_date", sort_direction=DESC)
  ]
)
```

**Emitted DAX** (`_measure_block` in tmdl_writer.py):
```dax
measure 'total_case_reserve' = 
  CALCULATE(
    SUM('fact_loss_reserve'[CASE_RESERVE]), 
    LASTNONBLANK('reserve_date'[DATE_VAL], 1)
  )
```

**Dimension resolution**: The `nad.dimension` field references the SV-level
logical dimension name (e.g. `reserve_date`), but the DAX must reference the
physical/calc column name exposed in TMDL (e.g. `DATE_VAL` for a bare column,
or `reserve_date` if it's a calc col). The emitter walks the `dimensions` +
`time_dimensions` lists to resolve this mapping.

### 10.5 Window function DAX translation: dim_resolver
Snowflake window functions reference SV dimension names:
`SUM(SUM(x)) OVER (ORDER BY txn_date.txn_date ROWS BETWEEN 11 PRECEDING ...)`

The DAX rewriter must translate `txn_date.txn_date` → the physical column
name `'txn_date'[DATE_VAL]` (not `'txn_date'[txn_date]`).

**Solution**: Added `dim_resolver: callable | None` parameter to `rewrite_metric`:
```python
def resolve_dim(table_lower: str, dim_lower: str) -> str | None:
    tbl = view.get_table(table_lower)
    if tbl is None:
        return None
    for d in list(tbl.dimensions) + list(tbl.time_dimensions):
        if d.name.lower() == dim_lower:
            if d.expr is bare uppercase:
                return d.expr  # physical column
            return d.name      # calc col name
```

Pass `ctx.resolve_dim` when calling `rewrite_metric` for both table-level and
view-level metrics. The window-fn regex branch calls it and substitutes the
resolved name.

### 10.6 Fabric REST API deploy (the reliable path)
When MCP `DeployToFabric` fails or is unavailable, use the Fabric Items REST
API via `translator.deploy.fabric_client`:

**CLI**:
```powershell
python -m translator.cli deploy <model_dir> \
  --workspace-id <guid> \
  --name <display_name>
```

**Implementation** (`translator/deploy/fabric_client.py`):
* Uses `DefaultAzureCredential` (Azure CLI, VS Code auth, Managed Identity, etc.)
* Lists existing items by `displayName`, updates if found, creates if new
* Encodes every TMDL file as base64 `InlineBase64` parts
* POST `/v1/workspaces/{id}/semanticModels` (create) or PATCH (update)
* Polls the operation status until `Succeeded` or `Failed`

**Dependencies**: Add to `requirements.txt`:
```
requests>=2.31.0
azure-identity>=1.15.0
```

**Authentication**: Ensure `az login` or VS Code is signed in with a Fabric
Contributor/Admin role on the target workspace.

### 10.7 When to use each calc-col strategy

| Scenario | Strategy | Mode | Notes |
|----------|----------|------|-------|
| **Owned writable lakehouse** | `materialize` | Direct Lake on SQL | Run materialization notebook once; all tables stay DL |
| **Mirrored/read-only lakehouse** | `dax` + Import overrides | Composite (Import + DL) | Calc-col tables imported; others stay DL shortcuts |
| **Pure prototype (no prod deploy)** | `dax` | All Import | Simplest; no lakehouse access needed |
| **Snowflake upstream control** | (pre-compute in Snowflake) | Direct Lake on SQL | Add columns in Snowflake before mirror syncs |

### 10.8 Materialization script structure (when `materialize` strategy)
Generated as `out/<model>.materialize.py`:

```python
STEPS = [
    {
        "schema": "Actuarial",
        "table": "dim_policyholder",
        "column": "age_years",
        "spark_type": "BIGINT",
        "expr": "CAST(FLOOR(months_between(current_date(), DATE_OF_BIRTH) / 12) AS BIGINT)",
        ...
    },
]

for step in STEPS:
    fq = f"{step['schema']}.{step['table']}"
    existing = {f.name.lower() for f in spark.table(fq).schema.fields}
    if step['column'].lower() not in existing:
        spark.sql(f"ALTER TABLE {fq} ADD COLUMNS ({step['column']} {step['spark_type']})")
    spark.sql(f"UPDATE {fq} SET {step['column']} = {step['expr']}")
```

**Idempotent**: Checks for column existence; skips ALTER if present; always
UPDATEs to refresh values. Safe to re-run after source data changes.

**Execution**: Run in a Fabric Notebook attached to the target lakehouse.
Requires Contributor role on the lakehouse.

### 10.9 Column registration for materialized calc columns
When `strategy = "materialize"`, the `_build_context` function must register
calc-column names in `column_owner` and `table_columns` so downstream DAX
rewriting resolves them correctly.

**Caution**: Only register when the calc-col name is **already uppercase**
(e.g. `IS_ACTIVE`, `AGE_YEARS`). Registering a lowercase name (e.g. `is_active`)
against an uppercase token key breaks bracketing:
* Query: `IS_ACTIVE` → resolves to `column_owner["IS_ACTIVE"]`
* Emitted: `[IS_ACTIVE]` — correct
* If registered as `is_active`: lookup fails, emits bare `IS_ACTIVE`, TMDL
  parse error

**Implementation** (in `_build_context`):
```python
elif materialise and d.name.isupper():
    column_owner.setdefault(d.name, _q(t.name))
    cols.add(d.name)
```

### 10.10 Lessons for production deployments
1. **Always test composite models in Desktop first** — Import + Direct Lake
   mixing can have subtle cross-partition filter issues.
2. **Set refresh schedules on Import tables** — they're stale until refreshed.
3. **Monitor Import table refresh durations** — large calc-col tables may hit
   timeout limits (default 2 hours).
4. **Document the calc-col strategy in mapping.yml comments** — future
   maintainers need to know why certain tables are Import.
5. **Consider Power BI Premium per-user** — composite models work on Pro, but
   refresh scheduling requires Premium.
6. **Keep track of CL upgrades** — when DL/OL becomes GA, revisit the strategy
   and potentially migrate back to full Direct Lake with in-model calc columns.

---

## 12. Reverse pipeline (Fabric SM → Snowflake) — v1.1

The translator is bidirectional. The reverse path reconstructs a `SemanticView`
IR from a Fabric semantic model and syncs edits back to Snowflake, with a
reviewable Markdown drift report so the two platforms never silently diverge.

Pipeline: `.SemanticModel TMDL` → `tmdl_parser` → `SemanticView` IR →
`sv_writer` → `CREATE OR REPLACE SEMANTIC VIEW` DDL → (optional) `snowflake_client`.
DAX expressions are rewritten to SQL by `dax_to_sql` (mirror of `dax_rewriter`).

CLI: `pull` (REST getDefinition), `reverse` (TMDL→DDL), `diff` (drift `.md`),
`sync` (SM→Snowflake DDL, `--execute` to run it). `translate` also writes a
`<model>.changes.md`.

### 12.1 `tmdl_parser` accepts both folder layouts
PBIP pull gives `definition/tables/*.tmdl`; a Tabular-Editor / modeling-MCP
`ExportToTmdlFolder` gives a **flat** `definition/*.tmdl`. The parser globs both
and skips non-table files (model/database/expressions/relationships/culture)
because they parse to `None`.

### 12.2 Column & measure classification on the way back
* `column X` with `sourceColumn:` → `Fact` when numeric, else `Dimension`
  (`expr` = the physical `sourceColumn`).
* Calc `column X = <DAX>` → `Dimension`, or `Fact` (PRIVATE) when `isHidden`.
* `measure X = <DAX>` → `Metric`; `annotation Synonyms` → synonyms; `isHidden`
  → PRIVATE. `_Measures` holding table → view-level metrics.
* Primary keys are inferred from relationship `toColumn` targets (TMDL has no PK).

### 12.3 DAX→SQL precedence: wrap compound numerators
`DIVIDE(a - b, c)` must reverse to `(a - b) / NULLIF(c, 0)`. Without the parens,
`a - b / NULLIF(c, 0)` binds the division first. `dax_to_sql._has_top_level_binop`
wraps the numerator only when it has a top-level `+ - * /`.

### 12.4 Re-qualify cross-metric references (Snowflake requires it)
Forward turns `fact_claim.claim_count` into DAX `[claim_count]`; the reverse
yields bare `claim_count`, which Snowflake rejects (`invalid identifier`).
`sv_writer._qualify_metric_refs` prefixes bare metric names with their owning
table (`fact_claim.claim_count`) using the table-scoped metric map.

### 12.5 `sync` preserves un-round-trippable measures from source
Semi-additive `LASTNONBLANK`/`FIRSTNONBLANK` wrappers reverse to a
`/* review: ... */` placeholder, and window-function metrics reverse to the
*physical* date column (`txn_date.DATE_VAL`) which is invalid in a Snowflake
metric expression (needs the logical dim `txn_date.txn_date`). Both are invalid
SQL, so `_cmd_sync` replaces those measure expressions with the **original
Snowflake expression** (matched by name) so the emitted DDL stays executable.
The drift report still surfaces them under **Review flags**.

### 12.6 Snowflake CLI execution gotchas
* `snow sql` needs `--enable-templating NONE` — a `&` in a comment (`P&C`,
  `states & zones`) is parsed as a legacy template variable (`'C' is undefined`).
* `WITH TAG (...)` requires the tags to exist: `CREATE TAG IF NOT EXISTS` first.
* `sv2m sync --execute` uses `snowflake_client` (env creds: `SNOWFLAKE_ACCOUNT`,
  `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`/`SNOWFLAKE_TOKEN`). With an OAuth
  `snow` connection instead, run the generated `sync.sql` via `snow sql -f`.

### 12.7 Deploy identity mismatch
`sv2m deploy` (REST + `DefaultAzureCredential`) 404s when the `az`/VS Code
identity isn't the Fabric workspace owner. Route through the `mcp_powerbi-model`
`ImportFromTmdlFolder` → `DeployToFabric` path, which uses your Fabric identity.

### 12.8 Drift diff normalization
`diff.py` compares normalized projections: whitespace-collapsed, lower-cased,
and it strips `<table>.` qualifiers on metric refs and resolves relationship
columns to their physical source column (so role-played `DATE_KEY` ↔
`effective_date_key` don't read as drift). A freshly-translated model shows
drift only for genuine asymmetries (semi-additive, window fn, dropped constant
helpers).