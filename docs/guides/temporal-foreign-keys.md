---
script:
  basepath: data
---

# Temporal Foreign Keys

> This guide assumes basic familiarity with the Frictionless Framework. To learn more, please read the [Introduction](https://framework.frictionlessdata.io/docs/guides/introduction) and [Validating Data](https://framework.frictionlessdata.io/docs/guides/validating-data).

A regular foreign key answers the question "does this value exist in the reference table?". For versioned reference data that is not enough: a product code can appear many times in a slowly changing dimension table, once per version, and a sales record is only related to the version that was **in effect at the moment of the sale**. Temporal foreign keys express exactly this relation: every fact row must find **exactly one** version row valid at its business moment.

## Declaring a Temporal Foreign Key

The declaration lives in the fact table's schema under `temporalForeignKeys` (Python: `temporal_foreign_keys`, default `[]`):

```json
{
  "fields": ["product"],
  "at": "occurred",
  "reference": {
    "resource": "versions",
    "fields": ["product"],
    "validFrom": "begin",
    "validTo": "end"
  }
}
```

- `fields`: the key fields of the fact table (one or more, composite keys are supported)
- `at`: the field of the fact table holding the business moment (`date` or `datetime`)
- `reference.resource`: the resource holding the versions (`""` means the same table)
- `reference.fields`: the key fields of the version table, in the same order as `fields`
- `reference.validFrom`: the field with the start of the validity interval (required)
- `reference.validTo`: the field with the end of the validity interval (optional; an empty value means the version has no right boundary)

Key fields must be `string` or `integer` and pairwise of the same type on both sides. `at`, `validFrom` and `validTo` must share the same type: `date` or `datetime`. Declarations violating these rules fail as metadata errors before any data is read.

## A Complete Example

Consider a `versions` table keeping the history of product prices and a `sales` table recording when each sale happened:

```python script tabs=Python
from frictionless import Package

package = Package(
    {
        "resources": [
            {
                "name": "sales",
                "data": [
                    ["product", "occurred", "amount"],
                    ["A", "2023-02-15T10:00:00+00:00", 3],
                    ["A", "2023-08-15T10:00:00+00:00", 1],
                    ["B", "2023-06-15T10:00:00+00:00", 2],
                    ["C", "2023-08-15T10:00:00+00:00", 5],
                ],
                "schema": {
                    "fields": [
                        {"name": "product", "type": "string"},
                        {"name": "occurred", "type": "datetime"},
                        {"name": "amount", "type": "integer"},
                    ],
                    "temporalForeignKeys": [
                        {
                            "fields": ["product"],
                            "at": "occurred",
                            "reference": {
                                "resource": "versions",
                                "fields": ["product"],
                                "validFrom": "begin",
                                "validTo": "end",
                            },
                        }
                    ],
                },
            },
            {
                "name": "versions",
                "data": [
                    ["product", "begin", "end", "price"],
                    ["A", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", 10],
                    ["A", "2023-07-01T00:00:00+00:00", "", 12],
                    ["B", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", 20],
                    ["B", "2023-01-01T00:00:00+00:00", "2023-07-01T00:00:00+00:00", 20],
                ],
                "schema": {
                    "fields": [
                        {"name": "product", "type": "string"},
                        {"name": "begin", "type": "datetime"},
                        {"name": "end", "type": "datetime"},
                        {"name": "price", "type": "integer"},
                    ]
                },
            },
        ]
    }
)
report = package.validate()
print(report.flatten(["taskNumber", "rowNumber", "type", "matchCount", "referenceRows"]))
```

The two sales of product `A` each find exactly one version (the price changed in July). The sale of `B` finds **two** identical version rows, and `C` has no version at all:

```
[[1, 4, 'temporal-key-ambiguous', 2, [4, 5]],
 [1, 5, 'temporal-key-missing', 0, []]]
```

## Semantics

- **Intervals are left-closed, right-open**: a version covers moments `validFrom <= at < validTo`. Adjacent versions may share an endpoint without causing ambiguity. An empty `validTo` means the version is open-ended.
- **Exactly one match is required**: zero matching version rows produce a `temporal-key-missing` error, more than one (physically duplicated rows count as separate versions) produce a `temporal-key-ambiguous` error with the exact `matchCount` and up to two `referenceRows` (ascending row numbers).
- **Invalid intervals are reported**: a version row with a missing `validFrom`, with `validFrom >= validTo`, or with a naive `datetime` boundary produces a `temporal-interval` error on the version resource and does not take part in the relation.
- **The moment must be comparable**: a fact row with a missing `at` value or a naive `datetime` produces a `temporal-time` error and does not take part in the relation. Aware datetimes are compared by their actual instant, so `2023-06-01T02:00:00+02:00` equals `2023-06-01T00:00:00+00:00`. Naive datetimes are never interpreted in the local timezone.
- **Missing key components exempt a fact row**: if any field of a composite key is empty, that row is exempt from the relation (like regular foreign keys).
- **Type errors come first**: rows whose involved cells fail to decode keep their original `type-error` only; no temporal errors are stacked on top.
- **Overlapping versions are not an error by themselves**: an ambiguity is only reported when a fact moment actually falls into the overlapping range.

Errors are reported in the validation task of the resource that owns the problem row: fact-side errors (`temporal-time`, `temporal-key-missing`, `temporal-key-ambiguous`) in the fact resource's task, interval errors in the version resource's task. Within a task, temporal errors are ordered by row number and declaration order.

## Limitations

Temporal validation reads every involved resource exactly once in a single sequential pass, staging the decoded keys and intervals in a temporary on-disk store (cleaned up afterwards, also on failures). Because of that, `Package.validate()` currently rejects `parallel=True`, a non-empty `name`, and `limit_rows` when temporal foreign keys are present — the rejection happens before any source is read. `limit_errors` is supported and only bounds the amount of stored errors; it never truncates the reads needed to build complete relation views for other resources.

## Performance Notes

The join is organized per key: version starts and ends of each key are sorted once, and every fact moment is counted with two binary searches (`O(log n)` per fact row), so both keys with a long disjoint history and keys with hundreds of overlapping versions are handled at the same cost — and the reported `matchCount` stays exact in both cases. The intermediate data lives in a temporary SQLite database on local disk, so the extra working memory is bounded by the largest version group of a single key and does not grow with the table sizes. Measured on a fixture mixing long-history and high-overlap keys (100k versions / 200k facts, and 500k versions / 1M facts), the added Python memory peak stays under 3 MiB while the staging store uses 13–67 MiB of disk. The trade-off is that the first temporal error is only known after every involved resource has been read completely — the join needs the whole version table — whereas plain validation can stop at the first error limit.
