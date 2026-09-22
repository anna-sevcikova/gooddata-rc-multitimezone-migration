# Technical Notes — v1.4.1

## What changed in v1.4.1

V1.4.1 does **not** change the migration transformation engine. It adds input normalization in `migration_tool/config.py` so the unchanged, headerless six-column GoodData Platform export can be passed directly to `--scope`. `migrate.py` only gains self-test coverage for the parser and `runner.py` only gets the version bump.

Still byte-for-byte unchanged from v1.4:

```text
migration_tool/transform.py
migration_tool/api.py
migration_tool/discovery.py
config/replacement-rules.csv
```

### Raw Platform input contract

Headerless columns:

```text
1 source Platform workspace ID
2 attr.<logical source attribute>
3 object title
4 legacy Platform object ID
5 legacy object type
6 yes/no/n/a -- ignored
```

Normalization is deliberately narrow:

```text
attr.foo.bar          -> foo.bar
visualizationObject   -> visualization
analyticalDashboard   -> dashboard
metric                -> metric
```

Columns 1 and 4 remain provenance/audit data. Column 6 is parsed only to accept the original file shape and is discarded immediately; it has no migration semantics.

The existing normalized/headered scope remains supported, so V1.4-generated discovery files and older scope files continue to work unchanged.

## Discovery contract

`discover` is read-only. It builds Cloud `dependentEntitiesGraph` entry points from
every `source_attribute` / `source_label` in `replacement-rules.csv`
(`relation=DEPENDENTS`), then content-scans only the resulting metric /
visualization / dashboard candidates. It does not attempt heuristic detection of
unknown legacy time attributes. Transitive graph hits without a direct content
occurrence are dropped by the scanners.

A logical source is identified using the rule's exact API identity:

```text
source_attribute = whitelist/mapping key
source_label     = actual display-form/label ID when different
```

Detection matrix:

```text
metric:
  {label/<source_label>}
  {attribute/<source_attribute>}

visualization:
  bucket attribute displayForm.identifier.id == source_label
  positive/negative attribute filter displayForm.identifier.id == source_label

dashboard:
  analyticalDashboard -> filterContextRef
  filterContext.filters[].attributeFilter.displayForm.identifier.id == source_label
```

The generator aggregates multiple occurrences into one scope row because one whitelist row already authorizes all occurrences of that source inside that exact object.

Generated evidence columns:

```text
occurrence_types
occurrence_count
```

The existing V1.3 scope parser ignores these extra columns, so generated output can be passed directly to `plan`.

## Ambiguous titles

Migration lookup prefers a live Cloud ``legacy_object_id`` when present in scope,
then falls back to exact ``title + category``.

``discover`` therefore **emits** rows even when multiple objects share a title; it
warns, but keeps each clone distinguishable by Entity ID. ``plan`` resolves those
rows by ID.

AMBIGUOUS remains only when the title is duplicated **and** the scope row has no
usable live Cloud ID (for example a Platform-only provenance ID that is not an
Entity ID in the target workspace).

## UNSCOPED KNOWN LEGACY warning

During `plan`, after a scoped object has been loaded, V1.4 runs the same read-only scanner against all known replacement rules and subtracts the sources authorized for that object.

Example:

```text
visualization contains:
  interactionstarttime.interactionstarthalfhour  (field)
  interactionstarttime.interactionstarthour      (filter)

scope authorizes only:
  interactionstarttime.interactionstarthalfhour
```

Result:

```text
normal transform proceeds for Half Hour
Hour filter is NOT modified
plan prints UNSCOPED KNOWN LEGACY warning for Hour
```

The warning does not affect READY/BLOCKED accounting and does not expand authorization.

## Existing transformation semantics retained

### Case A

Target date dimension absent -> replace source field in place with mapped default label/granularity/title.

### Case B

Exactly one target DAY field -> reuse that DAY field as chronological SECOND/MINUTE/HOUR and remove source field.

After the source field is removed, any leftover exact string references to its
``localIdentifier`` (sorts, columnWidths, properties, …) are remapped onto the
reused DAY field's ``localIdentifier``. If a reference cannot be remapped cleanly
(still present as a substring elsewhere), the object stays BLOCKED.

### Case C

Target exists only as non-DAY -> existing target usages immutable; replace source separately using mapped default periodic granularity.

### Case D

More than one target DAY -> BLOCKED.

### Visualization legacy filter

Converted to unrestricted target date filter. Existing date filters are immutable. API form omits `from`/`to`.

### Dashboard legacy filter

Scope boundary is the analytical dashboard. Technical storage is resolved through `filterContextRef`. Conversion can produce two writes:

```text
analyticalDashboard   -> remove obsolete matching attributeFilterConfigs
filterContext         -> attributeFilter -> unrestricted dateFilter
```

The source filter local identifier is preserved. Existing date filters remain unchanged.

### Metrics

Only exact MAQL tokens are replaced; no broad text substitution.

## Safety model retained

```text
plan: no writes
apply gate: BLOCKED=0 and AMBIGUOUS=0
pre-write: re-GET every planned entity and compare with backup
write: one PUT -> one GET verification
failure: stop + rollback already-written entities
rollback: exact plan backups
```

## Testing strategy for v1.4

No new workspace or repeat of the full migration is required because V1.4 does not change the migration transformation engine.

Required validation:

1. synthetic self-test for discovery and missed-scope warnings,
2. live `discover` on the already-used DEV workspace (read-only),
3. verify that known forgotten legacy occurrences are found,
4. optionally migrate only those forgotten records with the normal `plan -> apply -> verify` flow.

## Version history

### v1.3

Added optional `source_label` to separate logical source attribute identity from the exact GoodData display-form/label ID. Verified example:

```text
source_attribute = startedtime.startedhhmm
source_label     = startedtime.startedhhmm.startedhh
```

### v1.4

Added:

- `discover` read-only scope generator,
- deterministic generated scope CSV,
- evidence columns `occurrence_types` / `occurrence_count`,
- warning file for discovery cases that cannot be safely emitted,
- non-blocking `UNSCOPED KNOWN LEGACY` warning during `plan`,
- synthetic tests for discovery and source-label differences.

### v1.4.1

Added:

- automatic detection of the unchanged six-column headerless GoodData Platform scope export,
- removal of the leading `attr.` transport prefix before replacement-rule lookup,
- object type mapping `visualizationObject/analyticalDashboard/metric` -> internal categories,
- explicit ignoring of the sixth `yes/no/n/a` column,
- self-test coverage proving both raw Platform and normalized scope formats are accepted.

No transformation, discovery, Entity API write, concurrency, verification, or rollback semantics changed.
