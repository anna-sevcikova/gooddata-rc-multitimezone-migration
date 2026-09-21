# GoodData Time Migration Tool v1.4.1

Python-first GoodData time migration tool using individual Entity API operations. Analytics as Code is not required.

V1.4.1 keeps the proven migration/transform logic and the V1.4 discovery features. The only functional addition is input compatibility with the unchanged six-column, headerless GoodData Platform export used as the migration scope.

Note.: How to work with this repo is explained in "00_instruction-process.txt" / "01_notes_for_handover.txt" (explained as for dummies like me :)

## Core model

```text
replacement-rules.csv = HOW a known legacy time source maps
workspace-scope.csv    = WHERE migration is authorized
GD_HOST/GD_WORKSPACE   = target GoodData workspace
```

`source_workspace_id` and `legacy_object_id` in scope are audit/provenance fields. They do not choose the target workspace.

## Scope input formats

V1.4.1 accepts **both** scope formats below.

### A. Raw GoodData Platform export — no preprocessing required

The original headerless six-column file can be passed directly to `--scope`:

```text
<platform_workspace_id>;<attr.source_attribute>;<object_title>;<legacy_object_id>;<legacy_object_type>;<yes|no|n/a>
```

Example:

```text
okme...;attr.agentstate.agentstatestarttime;Agent State Change Raw;3840045;visualizationObject;yes
```

The parser uses the columns as follows:

```text
1 source Platform workspace ID  -> audit/provenance only
2 attr.<source_attribute>       -> strips only the leading "attr." and matches replacement-rules.csv
3 object title                  -> exact target Cloud lookup title
4 legacy Platform object ID     -> audit/provenance only
5 legacy object type            -> category mapping
6 yes/no/n/a                    -> ACCEPTED BUT COMPLETELY IGNORED
```

Object-type mapping:

```text
visualizationObject -> visualization
analyticalDashboard -> dashboard
metric               -> metric
```

The sixth column has **no effect** on discovery, planning, transformation, apply, verification, or rollback. It stays in the source file only so the Platform export can be used unchanged.

Usage:

```bash
python3 migrate.py --scope input/platform-export.csv plan
```

### B. Normalized/headered scope

The existing V1.3/V1.4 format remains supported:

```text
source_workspace_id;source_attribute;object_title;legacy_object_id;legacy_object_type;aac_lookup_category
```

Category column may also be named `object_category` (either name is accepted).

`discover` continues to generate this normalized format (`aac_lookup_category`). Both formats feed the same strict whitelist logic after parsing.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export GD_HOST="https://your-gooddata-host"
export GD_WORKSPACE="your_workspace_id"
read -s GD_TOKEN
export GD_TOKEN
```

## 1. Self-test

```bash
python3 migrate.py self-test
```

No API calls/writes. V1.4.1 self-test covers the existing migration cases plus discovery of fields, filters, metrics, dashboard filterContexts, `source_attribute != source_label`, and the missed-scope diagnostic.

## 2. Discover scope — new in V1.4

```bash
python3 migrate.py discover
```

This performs **GET/list calls only** and scans all supported effective objects in `GD_WORKSPACE` against the shared replacement rules.

Default output:

```text
input/discovered-scope-<GD_WORKSPACE>.csv
```

Custom output:

```bash
python3 migrate.py discover --output input/my-scope.csv
```

The generated CSV is directly usable by `plan`. Extra audit columns are included:

```text
occurrence_types
occurrence_count
```

Discovery scans:

- metric MAQL (`{label/...}` and `{attribute/...}`),
- visualization fields,
- visualization attribute filters,
- dashboard filters through the referenced `filterContext`.

It generates **one row per object + logical `source_attribute`**, even when the source occurs multiple times or as both field and filter.

Discovery never migrates anything and never expands an existing scope automatically.

If a discovered object title is ambiguous inside the same category, that row is not emitted and a sibling `*.csv.warnings.txt` file is created. Dashboard/filterContext shapes that cannot be safely resolved are also reported there.

## 3. Plan

```bash
python3 migrate.py --scope input/my-scope.csv plan
```

`plan` is mandatory and performs no API writes.

Statuses:

- `READY`
- `NOT_FOUND`
- `NO_OCCURRENCE`
- `AMBIGUOUS`
- `BLOCKED`

Apply requires `AMBIGUOUS = 0` and `BLOCKED = 0`.

### V1.4 diagnostic

For every successfully resolved scoped object, `plan` also scans against **all** replacement rules. If another known legacy time source exists in that object but is not in the scope, it prints:

```text
WARNING UNSCOPED LEGACY: <source_attribute> (...)
```

and records in `plan.md`:

```text
WARNING — UNSCOPED KNOWN LEGACY
```

This warning is intentionally **non-blocking and non-mutating**. The tool does not assume permission to migrate the missing source; add it to scope explicitly (or regenerate scope) if desired.

## 4. Apply

After reviewing the plan:

```bash
python3 migrate.py apply \
  --confirm-host "$GD_HOST" \
  --confirm-workspace "$GD_WORKSPACE"
```

Before the first PUT, the tool re-GETs every planned write entity and compares it with the plan backup. Any concurrent change aborts before writes.

Each write is:

```text
PUT one Entity API object
GET verify
```

On first failure, already-written objects are rolled back from exact backups.

## 5. Verify

```bash
python3 migrate.py verify
```

## 6. Rollback

```bash
python3 migrate.py rollback \
  --confirm-host "$GD_HOST" \
  --confirm-workspace "$GD_WORKSPACE"
```

## Recommended workflow

```text
self-test
  -> discover
  -> review/edit generated CSV
  -> plan
  -> review plan + UNSCOPED warnings
  -> apply
  -> verify
  -> UI spot-check
  -> same plan again (idempotence: 0 writes)
```

## Strict whitelist behavior

If one visualization contains two known legacy sources but scope contains only one, only the scoped source may be changed. V1.4 warns about the other one but does not touch it.

This is deliberate: **discovery proposes scope; scope authorizes migration**.

## Existing migration behavior

V1.4.1 preserves the existing rules and safety semantics, including:

- exact title + category lookup,
- Case A/B/C/D visualization coexistence handling,
- exact metric MAQL token replacement,
- visualization legacy attribute filter -> unrestricted date filter,
- dashboard `filterContext` conversion,
- dashboard `attributeFilterConfigs` cleanup,
- existing date filters immutable,
- target dataset/label validation,
- individual Entity API PUTs only,
- concurrency check, GET verification and rollback,
- idempotent repeated plans.

See `docs/TECHNICAL-NOTES.md` for payload details and implementation notes.
