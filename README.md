# tablefold

Fold a wide physical schema into a handful of wide logical models an LLM can
read in one shot — then expand SQL written against those models back into SQL
the database can run.

```
53 physical tables  ──fold──>  4 logical models  (~1.3k tokens)
                    <─expand──  SQL that actually runs
```

## The problem

Text-to-SQL over a real database fails on context, not on reasoning. Fifty
tables of DDL is tens of thousands of tokens of mostly-irrelevant structure,
and the model has to rediscover every join path from foreign keys each time it
is asked anything.

Handing it four wide models instead — `orders`, `products`, `customers`,
`shipments`, each already carrying its related attributes as flat fields — is a
smaller and better-shaped problem. `SELECT customer_tier_label, SUM(grand_total)
FROM orders GROUP BY 1` needs no join reasoning at all.

The catch is that those models have to be *real*: something has to turn that
query back into the four-way join it stands for, without breaking the
arithmetic.

## What it does

**Fold.** Read the physical schema, build the foreign-key graph, score each
table on how fact-like it is, then pick anchors by greedy maximum coverage and
compose one wide model per anchor.

**Expand.** Rewrite SQL written against a model into a CTE over the physical
tables — emitting only the joins the query's fields actually need.

Both directions are deterministic. No model in the loop, so the same schema
always folds the same way and a surprising result is four numbers you can read
off the table.

## Install

```bash
uv sync                          # or: pip install -e .
uv sync --extra postgres         # to introspect a live database
```

## Use

```bash
# what the fold produced
tablefold fold --ddl fixtures/retail_50.sql -n 4

# 53 tables -> 4 models (13.2:1)
#
#   orders       64 fields (base 12, joined 23, agg 29)  absorbs 16 tables
#   products     54 fields (base  8, joined  8, agg 38)  absorbs 16 tables
#   customers    32 fields (base  7, joined  5, agg 20)  absorbs 13 tables
#   shipments    48 fields (base  6, joined 39, agg  3)  absorbs 13 tables
#
#   15 tables uncovered

# the prompt-sized rendering
tablefold context --ddl fixtures/retail_50.sql --max-fields 30

# why those anchors
tablefold inspect --ddl fixtures/retail_50.sql

# logical SQL -> physical SQL
tablefold expand "SELECT customer_tier_label, SUM(grand_total) AS revenue
                  FROM orders GROUP BY customer_tier_label" \
    --ddl fixtures/retail_50.sql

# against a live database
tablefold fold --dsn "postgresql://user@host:5432/db" --schema public -f yaml -o layer.yaml
```

From Python:

```python
from tablefold.introspect.ddl import DDLIntrospector
from tablefold.pipeline import fold
from tablefold.expand import expand

result = fold(DDLIntrospector.from_path("schema.sql").introspect(), target_models=4)
print(result.layer.compression_ratio)

expansion = expand("SELECT SUM(grand_total) FROM orders", result.layer, result.graph)
print(expansion.sql, expansion.joins_pruned)
```

## How the fold works

**1. Introspect.** DDL script (sqlglot) or live PostgreSQL (`pg_catalog`). Both
produce the same `PhysicalSchema`.

**2. Recover missing keys.** Restored backups usually arrive with every
constraint stripped, and no foreign keys means no graph means no fold. Columns
whose names strip to a known table (`customer_id` → `customers`) and whose types
match that table's key are wired back up and flagged `inferred`. On the
53-table fixture with all constraints removed, over 75% of the real edges come
back.

**3. Score.** Four structural signals per table: measure density, temporal
columns, outgoing key count, row estimate. Measure density is damped by
absolute count — without that, `cart_items(cart_id, product_id, quantity)`
scores a perfect 1.0 and outranks an orders table with five measures.

**4. Pick anchors.** Not by score: in a connected schema the top four facts are
all one hop apart and describe the same corner. Each fact can absorb a known
set of neighbours, so anchor selection is maximum coverage, solved greedily.
High in-degree dimensions join the candidate pool — `customers` has no measures
but eight tables point at it, and excluding it strands that whole half of the
schema.

**5. Compose.** One model per anchor, one row per anchor row, three field kinds:

| Kind | Source | Why it is safe |
|---|---|---|
| `base` | the anchor's own columns | — |
| `joined` | reached by following keys *forwards* | at most one row matches, so the grain cannot change |
| `aggregated` | a child table, folded through `SUM` / `AVG` / `COUNT` | a child fans out and can never be inlined |

Foreign-key columns are dropped from the output — the row they identify is
promoted in their place, so a raw `customer_id` would be a redundant integer in
a context window.

## The part that matters: grain

Order 1 has two line items. Join `order_items` to `orders` naively and order 1's
`total` of 100 appears twice; revenue comes back as 250 instead of 150. Nothing
errors. The query looks right.

So children are grouped *before* they are joined:

```sql
LEFT JOIN (
  SELECT order_id, SUM(line_total) AS order_items_line_total_sum
  FROM order_items
  GROUP BY order_id
) AS agg_order_items ON base.id = agg_order_items.order_id
```

The subquery yields at most one row per parent key, so the join cannot change
the row count. This is asserted by executing the expanded SQL against a live
SQLite database and checking the number — not by inspecting the SQL string.

## Join pruning

A model may fold sixteen tables; a query touching four fields expands to the
joins those four fields need.

```
$ tablefold expand "SELECT customer_tier_label, SUM(grand_total) AS revenue
                    FROM orders WHERE placed_at >= '2026-01-01'
                    GROUP BY customer_tier_label" --ddl fixtures/retail_50.sql

-- models: orders | joins 3/14 (11 pruned)
WITH tf__orders AS (
  SELECT
    base.grand_total AS grand_total,
    base.placed_at AS placed_at,
    j_customers__customer_tiers.label AS customer_tier_label
  FROM orders AS base
  LEFT JOIN customers AS j_customers
    ON base.customer_id = j_customers.id
  LEFT JOIN customer_tiers AS j_customers__customer_tiers
    ON j_customers.tier_id = j_customers__customer_tiers.id
)
SELECT customer_tier_label, SUM(grand_total) AS revenue
FROM tf__orders AS orders
WHERE placed_at >= '2026-01-01'
GROUP BY customer_tier_label
```

Two details worth naming. Aliases are keyed on the whole join *path*, not the
target table — `countries` reached via `addresses` and via `stores` are
different joins. And the CTE is `tf__orders`, not `orders`: a CTE named after
its own base table is a self-reference, and the physical table becomes
unreachable.

## Limits

- **Coverage is partial and reported, not inflated.** On the fixture, 38 of 53
  tables land in a model at four anchors. The rest are grandchildren (nothing at
  the anchor's grain can expose them without aggregating an aggregate) or
  disconnected islands. `fold` lists them.
- **Anchor naming is mechanical.** Models are named after their anchor table.
  Nothing here proposes that `orders` is "the sales subject area".
- **Aggregate choice is fixed.** `COUNT`, plus `SUM`/`AVG` over the first three
  numeric columns of each child. No configuration yet.
- **Expansion covers single-statement `SELECT`.** Joins between two logical
  models in one query work, but are not yet well tested.
- **No column-value knowledge.** `status = 4` means nothing here. That is
  enrichment on top, not folding.

## Where this came from

The approach is adapted from [WrenAI](https://github.com/Canner/WrenAI)'s open
context layer — specifically its manifest extraction and CTE rewriting. What is
*not* borrowed is the compression itself: WrenAI's models are roughly 1:1 with
physical tables, and it reduces prompt size by retrieval and progressive
disclosure rather than by collapsing tables. Folding fifty tables into four is a
different operation, closer to subject-area denormalisation, and it is what this
repo implements.

## Development

```bash
uv run pytest tests/ --cov=src/tablefold   # 80 tests, 91% coverage
uv run ruff check src tests
uv run ruff format src tests
```

## License

MIT
