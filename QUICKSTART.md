# SemanticView2Model (sv2m) — Quick Start Guide

**Get from zero to a deployed Fabric semantic model in ~60 minutes.**

This guide walks you through the complete setup using the bundled `insurance_actuarial` demo — a 10-table P&C insurance star schema with 14 relationships, 20 measures, and 4 role-playing date dimensions. By the end, you'll have a working DirectLake + Import composite model deployed to Microsoft Fabric, ready to query via Power BI.

---

## What you'll build

```
┌─────────────────────┐                  ┌────────────────────────────┐
│  Snowflake          │  Fabric          │ Microsoft Fabric           │
│  INSURANCE_POC      │  Mirroring       │ Lakehouse (OneLake Delta)  │
│  .ACTUARIAL schema  ├─────────────────►│ Tables/Actuarial/*         │
│  (10 tables)        │                  └────────────┬───────────────┘
│                     │                               │ Direct Lake + Import
│  CREATE SEMANTIC    │                               ▼
│  VIEW insurance     │                  ┌────────────────────────────┐
│  _actuarial         │   sv2m translate │ Power BI Semantic Model    │
│  (DDL)              ├─────────────────►│ InsuranceActuarial.pbip    │
└─────────────────────┘                  │ 14 tables, 20 measures     │
                                         └────────────────────────────┘
```

**What's happening:**
1. Snowflake tables + semantic view → mirrored to Fabric Lakehouse
2. Semantic view DDL → translated to Power BI TMDL (DirectLake + Import composite)
3. Model deployed to Fabric workspace, refreshed, ready for analysis

---

## 0. Before you begin

### Architecture recap

sv2m is a **translator** — it converts Snowflake `CREATE SEMANTIC VIEW` DDL (or YAML) into Power BI TMDL (`.SemanticModel` folder + `.pbip` file). The semantic view describes tables, relationships, facts, dimensions, and metrics in a vendor-neutral way; sv2m rewrites that metadata into Power BI's native format (TMDL), maps Snowflake SQL expressions to DAX, and optionally deploys the result to Fabric.

**Key concepts:**
- **Direct Lake mode** — Power BI queries the Fabric Lakehouse Delta tables live, no data import. Fast, no duplication.
- **Import mode** — For tables with calculated columns (read-only mirrored shortcuts can't be altered), data is pulled into the model.
- **Composite model** — Mixes Direct Lake (most tables) + Import (calc-col tables) in one semantic model.

For full context, see [README.md](README.md) and [INSTRUCTIONS.md](INSTRUCTIONS.md).

### Prerequisites checklist

Before starting, ensure you have:

- [ ] **Snowflake account** with:
  - Warehouse access (any size, e.g. `COMPUTE_WH`)
  - Ability to create database/schema (e.g. `INSURANCE_POC.ACTUARIAL`)
  - Role with `CREATE SEMANTIC VIEW` privilege
- [ ] **Microsoft Fabric** workspace with:
  - Capacity (F64+, trial, or PPU)
  - Contributor or Admin role on the workspace
  - Ability to create a Mirrored Database and Lakehouse
- [ ] **Azure CLI** installed and authenticated (`az login`) — for deployment via REST API
- [ ] **Power BI Desktop** (latest version) — to validate `.pbip` files locally
- [ ] **Python 3.10+** (3.13 recommended) — for running the translator
- [ ] **Git** (optional) — to clone this repo, or download the ZIP

**Optional:**
- [ ] **Snowflake CLI** (`snow`) — for scripted setup (Track B)
- [ ] **VS Code** — for editing `mapping.yml` and inspecting TMDL

---

## 1. Prerequisites & accounts

### 1.1 Snowflake setup

**If you don't have a Snowflake account:** Sign up for a free trial at [signup.snowflake.com](https://signup.snowflake.com).

**Verify access:**
```sql
-- In Snowsight (Snowflake web UI), run:
SELECT CURRENT_WAREHOUSE(), CURRENT_ROLE(), CURRENT_USER();
```

You need:
- A running warehouse (default or create one: `CREATE WAREHOUSE DEMO_WH`)
- A role with `CREATE DATABASE`, `CREATE SCHEMA`, `CREATE SEMANTIC VIEW` privileges

**Target database/schema:** This guide uses `INSURANCE_POC.ACTUARIAL`. You can use any names — just update the SQL session variables later.

### 1.2 Fabric workspace setup

1. Navigate to [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
2. Create a new workspace (or use an existing one): **Workspaces → New workspace**
   - Name: e.g. `SemanticModelDemo`
   - License mode: Trial, Fabric capacity, or Premium Per User
3. Note your **workspace ID** (from the URL: `app.fabric.microsoft.com/groups/<workspace-id>/...`) — you'll need this for `config/mapping.yml`

### 1.3 Install Azure CLI & authenticate

```powershell
# Install (if not already present):
winget install Microsoft.AzureCLI

# Authenticate:
az login

# Verify:
az account show
```

The translator uses `DefaultAzureCredential` for deployment, which chains through `az login` credentials, VS Code auth, managed identities, etc.

### 1.4 Install Power BI Desktop

Download from [powerbi.microsoft.com/desktop](https://powerbi.microsoft.com/desktop) or the Microsoft Store. Verify you can open `.pbip` files.

---

## 2. Install sv2m

### 2.1 Clone or download the repository

```powershell
# Option A: Git clone
git clone https://github.com/your-org/SemanticView2Model.git
cd SemanticView2Model

# Option B: Download ZIP from GitHub, extract, then cd into the folder
```

### 2.2 Create a Python virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # On Windows PowerShell
# or: .venv\Scripts\activate.bat  (cmd.exe)
# or: source .venv/bin/activate   (bash/macOS/Linux)
```

### 2.3 Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

**Required packages:**
- `pyyaml` — YAML semantic view parsing
- `pydantic>=2.6` — IR validation
- `sqlglot` — SQL parsing utilities
- `azure-identity` — Azure authentication
- `requests` — HTTP client for Fabric REST API
- `semantic-link-labs` — (optional) Fabric semantic model helpers
- `pytest` — for running tests

### 2.4 Install the sv2m CLI (editable mode)

```powershell
pip install -e .
```

This installs the `sv2m` console script. Verify:

```powershell
sv2m --help
# or: python -m translator.cli --help
```

You should see subcommands: `parse`, `translate`, `deploy`, `roundtrip`.

---

## 3. Populate Snowflake

You have two options: **Track A** (manual copy/paste in Snowsight) or **Track B** (scripted via Snowflake CLI).

### Track A: Snowsight worksheet (manual)

1. Open [Snowsight](https://app.snowflake.com) and sign in
2. Create a new SQL worksheet
3. Set session variables (update as needed):
   ```sql
   SET TARGET_DB = 'INSURANCE_POC';
   SET TARGET_SCHEMA = 'ACTUARIAL';
   ```
4. Create the database and schema:
   ```sql
   CREATE DATABASE IF NOT EXISTS IDENTIFIER($TARGET_DB);
   USE DATABASE IDENTIFIER($TARGET_DB);
   CREATE SCHEMA IF NOT EXISTS IDENTIFIER($TARGET_SCHEMA);
   USE SCHEMA IDENTIFIER($TARGET_SCHEMA);
   ```
5. Copy and paste the contents of `mock_data/01_create_schema.sql` into the worksheet and run it (creates 10 tables: `DIM_DATE`, `DIM_GEOGRAPHY`, `DIM_PRODUCT_LOB`, etc.)
6. Copy and paste the contents of `mock_data/02_seed_data.sql` and run it (inserts ~600K rows across all tables)
7. Copy and paste the contents of `mock_data/03_validation.sql` and run it (verifies row counts and referential integrity)

**Expected output from validation:**
```
Table                    | Row Count
-------------------------|----------
DIM_DATE                 | 2,192
DIM_GEOGRAPHY            | 50
DIM_PRODUCT_LOB          | 5
DIM_COVERAGE             | 6
DIM_AGENT                | 500
DIM_POLICYHOLDER         | 10,000
FACT_POLICY              | 50,000
FACT_PREMIUM_TXN         | ~250,000
FACT_CLAIM               | ~30,000
FACT_LOSS_RESERVE        | ~600,000
```

### Track B: Snowflake CLI (scripted)

**Prerequisites:** Install Snowflake CLI:
```powershell
# Install via pip:
pip install snowflake-cli-labs

# Or via Homebrew (macOS):
brew install snowflake-cli
```

**One-time connection setup:**
```powershell
snow connection add demo_connection \
  --account <your-account-locator> \
  --user <your-username> \
  --password  # will prompt securely
  # or use --authenticator externalbrowser for SSO
```

Test the connection:
```powershell
snow connection test -c demo_connection
```

**Run the setup scripts:**
```powershell
# Navigate to the repo root:
cd SemanticView2Model

# Execute schema creation:
snow sql -f mock_data/01_create_schema.sql \
  -c demo_connection \
  --variable TARGET_DB=INSURANCE_POC \
  --variable TARGET_SCHEMA=ACTUARIAL

# Seed data:
snow sql -f mock_data/02_seed_data.sql \
  -c demo_connection \
  --variable TARGET_DB=INSURANCE_POC \
  --variable TARGET_SCHEMA=ACTUARIAL

# Validate:
snow sql -f mock_data/03_validation.sql \
  -c demo_connection \
  --variable TARGET_DB=INSURANCE_POC \
  --variable TARGET_SCHEMA=ACTUARIAL
```

**Tip:** Wrap these in a PowerShell or bash script for repeatability (see **§12 Automation Roadmap** for ideas).

---

## 4. Create the semantic view

The semantic view DDL lives in `semantic_view/insurance_actuarial.sql`. This declares the logical model: tables, relationships, facts, dimensions, and metrics.

### 4.1 Run the DDL

**Snowsight (Track A):**
1. Open a new SQL worksheet
2. Copy the entire contents of `semantic_view/insurance_actuarial.sql`
3. Run it

**Snowflake CLI (Track B):**
```powershell
snow sql -f semantic_view/insurance_actuarial.sql \
  -c demo_connection
```

### 4.2 Verify the semantic view

```sql
-- In Snowsight or via snow sql:
SHOW SEMANTIC VIEWS IN SCHEMA INSURANCE_POC.ACTUARIAL;

-- Inspect the DDL:
SELECT GET_DDL('SEMANTIC_VIEW', 'INSURANCE_POC.ACTUARIAL.INSURANCE_ACTUARIAL');
```

You should see output confirming `INSURANCE_ACTUARIAL` semantic view exists.

---

## 5. Set up Fabric workspace + Snowflake mirroring

### 5.1 Create a Fabric Mirrored Database (UI)

1. Navigate to your Fabric workspace: [app.fabric.microsoft.com/groups/<workspace-id>](https://app.fabric.microsoft.com)
2. Click **+ New → Mirrored database → Snowflake**
3. **Connection details:**
   - **Snowflake account locator:** e.g. `abc12345.us-east-1.aws`
   - **Warehouse:** `COMPUTE_WH` (or your warehouse name)
   - **Database:** `INSURANCE_POC`
   - **Authentication:** Username/password or Azure AD SSO
4. Click **Connect**
5. **Select tables to mirror:**
   - Expand `ACTUARIAL` schema
   - Select all 10 tables (check `DIM_*`, `FACT_*`)
   - Click **Mirror**
6. Fabric creates a mirrored database and starts syncing
7. Wait for initial replication (typically 5-15 minutes for this dataset)
8. **Note the Lakehouse name and ID** — Fabric auto-creates a Lakehouse to store the mirrored shortcuts

**After mirroring completes:**
- Navigate to the Lakehouse item (e.g. `InsuranceActuarialLH`)
- Browse **Tables → Actuarial** — you should see all 10 tables with uppercase names (Snowflake casing preserved)
- Note the **SQL endpoint FQDN** (from Lakehouse settings → SQL connection string): `<guid>.datawarehouse.fabric.microsoft.com`

**Critical gotcha — casing:**
Snowflake tables arrive in OneLake as **uppercase** (`ACTUARIAL.DIM_DATE`, not `actuarial.dim_date`). The translator's `mapping.yml` must match this exact casing for Direct Lake to bind correctly at refresh time.

### 5.2 REST API / CLI automation note (future)

For scripted mirroring setup, use the Fabric REST API:
- `POST /v1/workspaces/{workspaceId}/mirroredDatabases` (requires preview API access)
- See [Fabric REST API docs](https://learn.microsoft.com/rest/api/fabric) and **§12 Automation Roadmap** for details

---

## 6. Configure `config/mapping.yml`

The mapping file tells the translator how to resolve Snowflake references (`INSURANCE_POC.ACTUARIAL.DIM_DATE`) to Fabric entities (`Actuarial.DIM_DATE` in `InsuranceActuarialLH`).

### 6.1 Open `config/mapping.yml` in your editor

```yaml
fabric:
  workspace_id: "00000000-0000-0000-0000-000000000000"  # ← REPLACE with your workspace GUID
  workspace_name: "SemanticModelDemo"                   # ← Your workspace display name
  lakehouse_id: "00000000-0000-0000-0000-000000000000"  # ← REPLACE with your lakehouse GUID
  lakehouse_name: "InsuranceActuarialLH"                # ← Your lakehouse display name
  sql_endpoint: "00000000000000000000000000000000-00000000000000000000000000.datawarehouse.fabric.microsoft.com"  # ← REPLACE

default_storage_mode: directlake

calc_column_strategy: dax

namespace_map:
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL }
    fabric:    { schema: Actuarial }   # ← Match OneLake casing (usually uppercase from Snowflake)

table_overrides:
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: dim_policyholder }
    storage_mode: import
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: fact_policy }
    storage_mode: import
  - snowflake: { database: INSURANCE_POC, schema: ACTUARIAL, table: fact_claim }
    storage_mode: import

model:
  name: "InsuranceActuarial"
  culture: "en-US"
  compatibility_level: 1604
```

### 6.2 Fill in your workspace & lakehouse details

**Where to find these values:**

- **`workspace_id`**: From the Fabric URL: `app.fabric.microsoft.com/groups/<workspace_id>/...`
- **`workspace_name`**: Display name from Fabric portal
- **`lakehouse_id`**: From the Lakehouse URL or Settings pane
- **`lakehouse_name`**: Display name of the Lakehouse item
- **`sql_endpoint`**: From Lakehouse → Settings → SQL connection string (FQDN only, no `Data Source=` prefix)

**Key settings explained:**

- **`default_storage_mode: directlake`** — Most tables query live from OneLake
- **`calc_column_strategy: dax`** — Emit calculated columns as DAX (requires Import mode for those tables)
- **`table_overrides`** — Three tables have calculated columns (`AGE_YEARS`, `IS_OPEN_CLAIM`, `IS_ACTIVE`), so we override them to Import mode. Mirrored shortcuts are read-only; we can't materialize calc columns in the lakehouse. Instead, PBI imports the data and adds the calc columns in-model.
- **`namespace_map`** — Maps Snowflake `INSURANCE_POC.ACTUARIAL` to Fabric schema `Actuarial` (case-sensitive!)

**Common mistake:** Setting `fabric.schema: actuarial` (lowercase) when OneLake has `Actuarial` (uppercase) will cause Direct Lake binding to fail silently at refresh time. Always match the OneLake casing exactly.

---

## 7. Run the translator

### 7.1 Translate the semantic view to TMDL

```powershell
python -m translator.cli translate `
    semantic_view/insurance_actuarial.sql `
    --mapping config/mapping.yml `
    --out out_demo `
    --pbip
```

**What this does:**
1. Parses `insurance_actuarial.sql` into an intermediate representation (IR)
2. Resolves logical table names to Fabric entities via `mapping.yml`
3. Rewrites Snowflake SQL expressions to DAX (measures + calc columns)
4. Emits TMDL files:
   - `out_demo/InsuranceActuarial.SemanticModel/definition/*.tmdl`
   - `out_demo/InsuranceActuarial.pbip` (with `--pbip` flag)

### 7.2 Inspect the output

```
out_demo/
├── InsuranceActuarial.SemanticModel/
│   ├── .platform
│   ├── definition.pbism
│   ├── diagramLayout.json
│   └── definition/
│       ├── database.tmdl
│       ├── model.tmdl
│       ├── expressions.tmdl
│       ├── relationships.tmdl
│       └── tables/
│           ├── _Measures.tmdl
│           ├── dim_agent.tmdl
│           ├── dim_coverage.tmdl
│           ├── dim_geography.tmdl
│           ├── dim_policyholder.tmdl   ← Import mode (calc col AGE_YEARS)
│           ├── dim_product_lob.tmdl
│           ├── effective_date.tmdl     ← Role-playing date
│           ├── txn_date.tmdl           ← Role-playing date
│           ├── loss_date.tmdl          ← Role-playing date
│           ├── reserve_date.tmdl       ← Role-playing date
│           ├── fact_claim.tmdl         ← Import mode (calc col IS_OPEN_CLAIM)
│           ├── fact_loss_reserve.tmdl
│           ├── fact_policy.tmdl        ← Import mode (calc col IS_ACTIVE)
│           └── fact_premium_txn.tmdl
└── InsuranceActuarial.pbip
```

**Key files to review:**

- **`expressions.tmdl`** — M expression for the lakehouse SQL endpoint connection (Direct Lake on SQL)
- **`relationships.tmdl`** — 14 relationships; any `isActive: false` edges are cycle-breakers (union-find)
- **`_Measures.tmdl`** — View-level derived metrics (e.g. `loss_ratio`, `t12m_earned_premium`)
- **Tables with `mode: import`** — `dim_policyholder`, `fact_policy`, `fact_claim` (have calc columns)

---

## 8. Validate in Power BI Desktop

Before deploying to Fabric, validate locally in Power BI Desktop.

### 8.1 Open the .pbip file

```powershell
# From the repo root:
start out_demo\InsuranceActuarial.pbip
```

Power BI Desktop should launch and load the model.

### 8.2 Common errors and fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` | Relationship cycle | Check `relationships.tmdl` for `isActive: false` edges; these are intentional cycle-breakers |
| `Calculated column 'X' is not allowed in Direct Lake table` | Direct Lake table has a calc column | Verify `calc_column_strategy: dax` + table override to `import` in `mapping.yml` |
| `Column 'X' not found` | Measure references a column that doesn't exist | Check DAX rewriter column ownership (see [INSTRUCTIONS.md §5.1](INSTRUCTIONS.md)) |
| `Entity name 'actuarial.dim_date' not found` | Casing mismatch | Update `namespace_map` to match OneLake casing (usually uppercase from Snowflake) |
| `TMDL Format Error: Indentation` | Mixed tabs/spaces | All TMDL files must use **tabs only** for indentation |

### 8.3 Inspect the model

1. Switch to **Model view** (left sidebar icon)
2. Verify all 14 tables are present (4 `date` role-playing tables + 10 business tables)
3. Check relationships (should show 14 lines connecting tables)
4. Click on a measure (e.g. `total_earned_premium`) and inspect the DAX expression in the formula bar
5. Switch to **Data view** — Import tables (`dim_policyholder`, `fact_policy`, `fact_claim`) show data; Direct Lake tables show placeholder (data loads on first query)

**If the model loads without errors, you're ready to deploy!**

---

## 9. Deploy to Fabric

You have two options: **MCP tools** (if available) or **REST API via CLI** (more reliable).

### Option A: Deploy via REST API (recommended)

The translator includes a built-in Fabric REST API deployment client.

**Prerequisites:**
- `az login` completed (Step 1.3)
- Contributor or Admin role on the target Fabric workspace

**Deploy:**
```powershell
python -m translator.cli deploy `
    out_demo\InsuranceActuarial.SemanticModel `
    --workspace-id <your-workspace-id> `
    --name InsuranceActuarialDemo
```

**What this does:**
1. Encodes all TMDL files as base64
2. Calls `POST /v1/workspaces/{id}/semanticModels` (or PATCH if the model already exists)
3. Polls the operation status until `Succeeded` or `Failed`
4. Returns the deployed model's item ID

**Example output:**
```
Deploying to workspace 00000000-0000-0000-0000-000000000000...
Creating semantic model 'InsuranceActuarialDemo'...
Operation started: 12345678-90ab-cdef-1234-567890abcdef
Polling status... (attempt 1/60)
✓ Deployment succeeded!
Model ID: abcdef12-3456-7890-abcd-ef1234567890
URL: https://app.fabric.microsoft.com/groups/00000000.../semanticModels/abcdef12-...
```

### Option B: Deploy via MCP tools (if available)

If you have the `mcp_powerbi-model_*` MCP server configured (see [INSTRUCTIONS.md §9.8](INSTRUCTIONS.md)):

1. **Import from TMDL folder:**
   ```
   Operation: ImportFromTmdlFolder
   tmdlFolderPath: out_demo\InsuranceActuarial.SemanticModel\definition
   connectionName: demo_local
   ```
2. **Deploy to Fabric:**
   ```
   Operation: DeployToFabric
   deployToFabricRequest: {
     newDatabaseName: "InsuranceActuarialDemo",
     targetWorkspaceName: "SemanticModelDemo"
   }
   ```
3. **Connect to Fabric:**
   ```
   Operation: ConnectFabric
   workspaceName: "SemanticModelDemo"
   semanticModelName: "InsuranceActuarialDemo"
   ```
   *(Capture the returned `connectionName` for subsequent operations)*

### 9.1 Refresh Import tables

The deployed model has 3 Import-mode tables (`dim_policyholder`, `fact_policy`, `fact_claim`) that need an initial refresh before data is visible.

**Via Fabric portal:**
1. Navigate to the semantic model in your workspace
2. Click **Settings → Refresh now**
3. Wait for the refresh to complete (typically 1-2 minutes for this dataset)

**Via REST API:**
```powershell
# Trigger refresh via Fabric Datasets API
curl -X POST "https://api.powerbi.com/v1.0/myorg/groups/<workspace-id>/datasets/<model-id>/refreshes" `
  -H "Authorization: Bearer $(az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv)" `
  -H "Content-Type: application/json" `
  -d '{}'
```

**Via MCP tools:**
```
Operation: RefreshWithAPI
refreshType: Full
```

### 9.2 Verify the deployment

1. Open the Fabric workspace in your browser
2. Locate the `InsuranceActuarialDemo` semantic model
3. Click **Open → Analyze in Excel** or **Build a report** to test queries
4. Run a simple query (e.g. "Total policies by state") to confirm Direct Lake tables load

---

## 10. Troubleshooting

### 10.1 Quick reference

| Symptom | Root Cause | Solution |
|---------|------------|----------|
| `PFE_XL_USERELATIONSHIP_AMBIGUOUS_PATH` | Relationship cycle in model | Check `relationships.tmdl` — some edges marked `isActive: false`; this is intentional. For background, see [INSTRUCTIONS.md §4.2](INSTRUCTIONS.md) |
| `Calculated column not allowed in Direct Lake table` | Calc column on a Direct Lake table | Verify `calc_column_strategy: dax` in `mapping.yml` AND table override to `storage_mode: import` |
| `Entity name 'actuarial.dim_date' not found` during refresh | Schema/table casing mismatch | Update `namespace_map` in `mapping.yml` to match OneLake casing exactly (usually uppercase from Snowflake mirroring) |
| `TMDL Format Error: Indentation` | Mixed tabs/spaces in `.tmdl` files | Use **tabs only** for indentation; no spaces allowed |
| `Column 'X' not found` in measure DAX | DAX rewriter misidentified column ownership | Check [INSTRUCTIONS.md §5.1](INSTRUCTIONS.md) for per-table column ownership rules |
| `Measure 'X' not found` | Metric reference in another metric failed | Check `_Measures.tmdl` for correct bracketed form: `[measure_name]`, not `'table'[measure_name]` |
| Import table shows no data after deploy | Refresh not triggered | Run **Refresh now** in Fabric portal or trigger via REST API (§9.1) |
| `DefaultAzureCredential` authentication failed | No active Azure login | Run `az login` and verify with `az account show` |
| Direct Lake tables show no data in Desktop | Expected behavior | Direct Lake data loads on first query; Desktop shows placeholders until you query a table |

### 10.2 Verbose logging

To see detailed parser and emitter logs:

```powershell
# Enable debug logging:
$env:SV2M_LOG_LEVEL = "DEBUG"
python -m translator.cli translate ...
```

### 10.3 Test suite

The repo includes pytest-based regression tests:

```powershell
pytest -v
```

All tests should pass. If any fail after code changes, review [INSTRUCTIONS.md §7](INSTRUCTIONS.md).

---

## 11. Using your own Snowflake semantic view

To translate **your own** semantic view instead of the demo:

### 11.1 Export your semantic view DDL

```sql
-- In Snowsight:
SELECT GET_DDL('SEMANTIC_VIEW', '<your_db>.<your_schema>.<your_sv_name>');
```

Save the output to a new file, e.g. `semantic_view/my_model.sql`.

### 11.2 Set up Fabric mirroring for your tables

Follow **§5** (Fabric workspace + mirroring), but select **your** Snowflake database/schema and tables.

### 11.3 Update `mapping.yml`

1. Change `namespace_map` to match your Snowflake db/schema → Fabric schema
2. Update `workspace_id`, `lakehouse_id`, `sql_endpoint` to point to your Fabric resources
3. If your semantic view has calculated columns (dimensions/facts with `AS <expr>`), decide on strategy:
   - **Materialize** (write calc columns back to lakehouse Delta tables via PySpark) — requires writable lakehouse
   - **DAX** (emit calc columns as DAX, use Import mode) — works with read-only mirrored shortcuts
   - Add `table_overrides` for any tables with calc columns to set `storage_mode: import`
4. Change `model.name` to your desired semantic model name

### 11.4 Translate and deploy

```powershell
python -m translator.cli translate `
    semantic_view/my_model.sql `
    --mapping config/mapping.yml `
    --out out_my_model `
    --pbip

# Validate in Desktop:
start out_my_model\<YourModel>.pbip

# Deploy:
python -m translator.cli deploy `
    out_my_model\<YourModel>.SemanticModel `
    --workspace-id <your-workspace-id> `
    --name <YourModelName>
```

### 11.5 Handling calculated columns

**If your semantic view has calculated columns** (e.g. `age_years AS DATEDIFF(year, birth_date, CURRENT_DATE())`):

- **Option 1 — Materialize (for owned lakehouses):**
  ```yaml
  calc_column_strategy: materialize
  ```
  The translator generates a PySpark script (`out_*/your_model.materialize.py`) to add physical columns. Run it in a Fabric Notebook attached to your lakehouse.

- **Option 2 — DAX (for mirrored/read-only lakehouses):**
  ```yaml
  calc_column_strategy: dax
  table_overrides:
    - snowflake: { database: YOUR_DB, schema: YOUR_SCHEMA, table: your_table }
      storage_mode: import
  ```
  Tables with calc columns become Import mode; others stay Direct Lake. This is the approach used in the demo.

For full context, see [INSTRUCTIONS.md §10 (Calculated columns deep-dive)](INSTRUCTIONS.md).

---

## 12. Automation Roadmap

The steps above are largely manual (UI clicks, copy/paste SQL, editing YAML). Here's a roadmap for automating the end-to-end setup via notebooks, CLIs, REST APIs, and MCPs.

### 12.1 Snowflake automation

**Goal:** Script the entire Snowflake setup (DB → schema → tables → seed → semantic view) from one command or notebook.

**Tools:**
- **Snowflake CLI (`snow`)** — Already used in Track B; wrap `snow sql -f` calls in a PowerShell/bash script
- **Python `snowflake-connector-python`** — Execute SQL from a Python script or notebook:
  ```python
  import snowflake.connector
  conn = snowflake.connector.connect(...)
  cur = conn.cursor()
  cur.execute(open('mock_data/01_create_schema.sql').read())
  cur.execute(open('mock_data/02_seed_data.sql').read())
  cur.execute(open('semantic_view/insurance_actuarial.sql').read())
  ```

**Automation idea:**
- Single script: `setup_snowflake.py` or `setup.ps1` that:
  1. Prompts for Snowflake credentials (or reads from env vars)
  2. Runs `01_create_schema.sql` → `02_seed_data.sql` → `03_validation.sql` → semantic view DDL
  3. Verifies success via row counts

### 12.2 Fabric mirroring automation

**Goal:** Create a Mirrored Database + Lakehouse via REST API, no UI clicks.

**Tools:**
- **Fabric REST API** — `POST /v1/workspaces/{id}/mirroredDatabases` (preview)
- **Azure CLI (`az rest`)** — For scripted HTTP calls with Azure auth
- **MCP server** — If a Fabric MCP supports mirroring operations (not yet available as of this guide)

**Automation idea:**
- Script or notebook that:
  1. Calls Fabric Items API to create a MirroredDatabase item
  2. Configures Snowflake connection details (account, warehouse, DB, credentials)
  3. Selects tables/schemas to mirror
  4. Polls mirroring status until initial replication completes
  5. Returns lakehouse ID and SQL endpoint for `mapping.yml`

**Current limitation:** Fabric Mirroring REST API is in preview; check [Microsoft Learn](https://learn.microsoft.com/rest/api/fabric) for updates.

### 12.3 Auto-populate `mapping.yml`

**Goal:** Generate `mapping.yml` automatically by querying Fabric workspace metadata.

**Tools:**
- **Fabric REST API** — `GET /v1/workspaces/{id}/items` to list lakehouses
- **Fabric Lakehouse API** — `GET /v1/workspaces/{id}/lakehouses/{lhId}` for SQL endpoint
- **Snowflake Information Schema** — Query `INFORMATION_SCHEMA.TABLES` for schema discovery

**Automation idea:**
- Script that:
  1. Prompts for workspace name/ID
  2. Lists all lakehouses in that workspace
  3. Prompts user to select the target lakehouse (or auto-selects if only one exists)
  4. Queries lakehouse metadata for schemas/tables
  5. Generates `mapping.yml` with correct `workspace_id`, `lakehouse_id`, `sql_endpoint`, and `namespace_map`
  6. Optionally scans for calc columns in the semantic view and suggests `table_overrides`

### 12.4 Calc-column materialization notebook

**Goal:** Generalize the existing `sv2m_materialize_calc_columns.ipynb` notebook.

**Current state:** A prototype notebook exists in `add_helper_columns_test1v2/` that runs the generated `materialize.py` script.

**Automation idea:**
- Enhance the translator to emit a **parameterized notebook** (`.ipynb` or `.py`) that:
  1. Takes `schema`, `table`, `column`, `expr`, `spark_type` as parameters
  2. Connects to the Fabric lakehouse via `spark`
  3. Runs idempotent `ALTER TABLE ADD COLUMNS` + `UPDATE` statements
  4. Returns a summary of materialized columns

- Integrate with Fabric Pipelines: Auto-generate a pipeline activity to run the notebook after mirroring completes.

### 12.5 Deploy orchestration

**Goal:** Single-command end-to-end deployment from semantic view DDL to deployed Fabric model.

**Tools:**
- **Translator CLI** — `sv2m translate` + `sv2m deploy` (already exists)
- **MCP PowerBI-Model server** — For robust TMDL import + Fabric deploy + refresh
- **Fabric Pipelines** — Orchestrate: translate → deploy → refresh → validate

**Automation idea:**
- Fabric Notebook or Pipeline that:
  1. Clones/downloads this repo
  2. Runs `sv2m translate` with pre-configured `mapping.yml`
  3. Calls `sv2m deploy` (or MCP `DeployToFabric`)
  4. Triggers refresh on Import tables
  5. Polls refresh status until complete
  6. Validates model via DAX query (e.g. `EVALUATE TOPN(10, 'fact_policy')`)
  7. Sends success/failure notification (email, Teams webhook)

### 12.6 Azure CLI / MCP integration

**Goal:** Leverage Azure CLI and MCP servers for seamless auth, workspace provisioning, and deployment.

**Tools:**
- **Azure CLI (`az`)** — For Fabric workspace creation, RBAC assignment, KeyVault secrets
- **Fabric MCP server** — For workspace/item CRUD operations
- **Power BI MCP server** — For semantic model management (already prototyped in INSTRUCTIONS.md §9.8)

**Automation idea:**
- Shell script or notebook that:
  1. Runs `az login` and validates subscription access
  2. Creates a Fabric workspace via REST API (if doesn't exist)
  3. Assigns current user as Workspace Admin
  4. Calls MCP tools to create Lakehouse + Mirrored Database
  5. Waits for mirroring sync
  6. Auto-generates `mapping.yml`
  7. Runs translator → deploy → refresh via MCP or CLI

### 12.7 End-state vision: `sv2m bootstrap`

**Ultimate goal:** Single command that provisions everything.

```powershell
sv2m bootstrap `
  --snowflake-account <acct> `
  --snowflake-db INSURANCE_POC `
  --snowflake-schema ACTUARIAL `
  --fabric-workspace SemanticModelDemo `
  --semantic-view semantic_view/insurance_actuarial.sql
```

**What it does:**
1. Authenticates to Snowflake and Fabric (prompts for credentials or reads env vars)
2. Creates Snowflake DB/schema/tables/semantic view (if not exist)
3. Creates Fabric workspace + Lakehouse + Mirrored Database (if not exist)
4. Waits for mirroring sync
5. Generates `mapping.yml` on the fly
6. Runs `translate` → `deploy` → `refresh`
7. Opens the deployed model URL in a browser

**Implementation:** Combine all the above ideas into a single orchestration script or Fabric Pipeline with parameterized steps.

---

## Next steps

**You've successfully:**
- ✅ Populated Snowflake with the insurance demo schema + semantic view
- ✅ Mirrored the data to a Fabric Lakehouse
- ✅ Translated the semantic view to a Power BI TMDL semantic model
- ✅ Deployed and refreshed the model in Fabric
- ✅ Validated queries in Power BI

**Where to go from here:**

1. **Build reports** — Create visualizations on top of the semantic model in Power BI Desktop or Fabric
2. **Add more metrics** — Edit `insurance_actuarial.sql`, add new `METRICS`, re-translate, re-deploy
3. **Extend the schema** — Add more tables to the semantic view, mirror them, update `mapping.yml`, re-translate
4. **Automate** — Implement one or more automation ideas from §12
5. **Explore advanced features:**
   - Semi-additive measures (see [INSTRUCTIONS.md §10.4](INSTRUCTIONS.md))
   - Window-function metrics (§10.5)
   - Role-playing date aliases (§9.5)
   - Hybrid composite models (§10.3)
6. **Contribute** — Found a bug or want a feature? Open an issue or PR on the [GitHub repo](https://github.com/your-org/SemanticView2Model)

**Recommended reading:**
- [README.md](README.md) — Feature matrix, architecture overview, CLI reference
- [INSTRUCTIONS.md](INSTRUCTIONS.md) — Lessons learned, troubleshooting deep-dives, deployment recipes
- [Microsoft Learn: Snowflake Semantic Views](https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view)
- [Microsoft Learn: Fabric Mirroring](https://learn.microsoft.com/fabric/database/mirrored-database/snowflake)
- [Microsoft Learn: Power BI Direct Lake](https://learn.microsoft.com/power-bi/enterprise/directlake-overview)

---

**Happy modeling!** 🚀
