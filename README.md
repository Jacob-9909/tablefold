# tablefold

Fold a wide physical schema into a handful of wide logical models an LLM can
read in one shot — then expand SQL written against those models back into SQL
the database can run.

```
53 physical tables  ──fold──>  7 logical models  (~3k tokens)
                    <─expand──  SQL that actually runs
```

The model count is an output, not a setting. You state how much of the schema
you want covered and what a model has to earn to justify its tokens; the fold
reports how many that took and why it stopped.

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
tablefold fold --ddl fixtures/retail_50.sql

# 53 tables -> 7 models (7.6:1), 44 covered (83%)
# stopped: no table left brings enough new ones — lower --min-gain or raise
#          --max-hops for more coverage
#
#   orders          64 fields (base 12, joined 23, agg 29)  absorbs 16 tables
#   products        54 fields (base  8, joined  8, agg 38)  absorbs 16 tables
#   customers       32 fields (base  7, joined  5, agg 20)  absorbs 13 tables
#   shipments       48 fields (base  6, joined 39, agg  3)  absorbs 13 tables
#   payments        44 fields (base  5, joined 36, agg  3)  absorbs 11 tables
#   campaigns       12 fields (base  5, joined  2, agg  5)  absorbs  5 tables
#   invoice_lines   39 fields (base  3, joined 36, agg  0)  absorbs 10 tables
#
#   9 tables uncovered

# every table in a model, at the cost of more of them
tablefold fold --ddl fixtures/retail_50.sql --coverage 1.0 --min-gain 1
# 53 tables -> 16 models (3.3:1), 53 covered (100%)

# the other direction: pay for a model only if it brings three new tables
tablefold fold --ddl fixtures/retail_50.sql --min-gain 3
# 53 tables -> 4 models (13.2:1), 38 covered (72%)

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

result = fold(DDLIntrospector.from_path("schema.sql").introspect())
print(len(result.layer.models), result.layer.coverage, result.layer.stop_reason)

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

**4. Pick anchors.** Not by score: in a connected schema the top few facts are
all one hop apart and describe the same corner. Each table can absorb a known
set of neighbours, so anchor selection is minimum set cover, solved greedily.

Two questions stay separate. *Who may anchor?* Every table — a model anchored on
a table exposes that table's grain, and a low fact score does not make that
impossible. Gating the pool on score was measured to strand tables no other
anchor reaches: `employees` scores 0.24, is referenced by nothing, and anchors a
four-table model that nothing else can cover.

*Who is worth anchoring?* Whatever the policy admits. Two rules a candidate
clears before it is ranked at all — one on what it brings, one on what it
charges:

| Knob | Default | Effect |
|---|---|---|
| `--coverage` | `0.90` | fraction of tables the models should jointly cover |
| `--min-gain` | `2` | new tables an extra model must bring |
| `--max-cost` | `10` | fields it may spend per new table |
| `--max-models` | unset | hard ceiling; a safety valve, not the plan |

`--max-cost` exists because counting gain alone makes a model look free, and it
is not — the reader pays for its whole field list. Without it the fixture buys
three models at ~80% overlap with `orders`, each spending forty-odd fields to
reach two tables nothing else covers:

```
                    models  covered   tokens   tables per 1k tokens
--max-cost 10 (default)  6    41/53    ~2.2k          18.7
--max-cost 1000          7    44/53    ~3.0k          14.4
```

Ranking deliberately stays on *gain*, not gain-per-field. Dividing by price
optimises for cheap coverage, which was measured to produce ten models anchored
on `regions`, `carriers` and `payment_methods` — a third of the tokens, and
nothing a person would ask a question about.

**Who decides.** Building the candidate lattice — every table, what it reaches,
what it would cost — has one answer. Choosing from it is a judgement, so it sits
behind a `Selector`. Greedy is the default and needs nothing:

```python
from tablefold.select import LLMSelector
from tablefold.pipeline import fold

result = fold(schema, selector=LLMSelector(complete))   # complete: str -> str
```

An `LLMSelector` exists because the lattice cannot express that `payments`,
`invoices` and `returns` are one billing story rather than three, that
`invoice_lines` is a poor name for a model a person will read, or that nobody
here asks about campaigns. It also names the models, which greedy cannot — it
has no basis for anything but the anchor's table name.

What the LLM is *not* trusted with is anything countable:

- it picks from a closed set; names the lattice does not know are dropped
- coverage, membership and marginal gain are recomputed from the graph, never
  read back from the reply
- `--max-models` still binds it
- an unusable completion falls back to greedy, and the layer says so
  (`anchors chosen by: greedy (llm fallback)`)
- the grain rules in `compose` and `expand` never see its output

The worst a bad completion can do is choose a poor set of *real* anchors. It
cannot produce a wrong number.

`complete` is any `str -> str`, so no vendor SDK reaches the core. A ready
adapter ships behind the `llm` extra:

```bash
uv sync --extra llm
export ANTHROPIC_API_KEY=...
tablefold fold --ddl fixtures/retail_50.sql --llm
```

`--min-gain` is the knob that finds the knee of the coverage curve:

```
models    1     2     3     4     5     7    11    16
covered  16    29    35    38    40    44    48    53
tokens  ~0.8k ~1.4k ~1.8k ~2.2k ~2.5k ~3.0k ~4.0k ~4.4k
gain    +16   +13    +6    +3    +2   +2,+1  +1    +1
```

Selection stops when the target is met, when nothing left clears the admission
rules, or at the ceiling — and the run says which. A fold that could not reach
its target reports the shortfall instead of returning a short model list that
reads as complete.

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

- **Coverage is a setting, and the shortfall is reported.** At the defaults, 44
  of 53 tables land in a model; `--coverage 1.0 --min-gain 1` reaches all 53 at
  16 models. What no setting reaches is a grandchild through more than
  `--max-hops` steps — nothing at the anchor's grain can expose one without
  aggregating an aggregate. `fold` lists whatever it missed and says why it
  stopped.
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
