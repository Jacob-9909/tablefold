"""논리 모델(Logical Model)을 타깃으로 작성된 SQL을
실제 데이터베이스에서 실행 가능한 SQL로 확장(Expand/Rewrite)합니다.

쿼리가 와이드 모델 및 해당 필드를 참조하면, 이를 물리 테이블로 재구성하는
CTE(Common Table Expression)를 생성하고 해당 CTE를 조회하도록 SQL 쿼리를
다시 작성(Rewrite)합니다.

핵심 원칙 2가지:

**1. 데이터 입도(Grain) 보존**: N:1 조인은 직접 인라인 조인됩니다.
1:N 자식 테이블은 *절대* 직접 인라인 조인되지 않고, 서브쿼리에서 먼저
`GROUP BY`로 선집계(Pre-aggregation)한 후 조인됩니다.
자식 테이블을 직접 조인할 경우 부모 행 수가 뻥튀기되어
모든 집계 연산(SUM 등)이 오염되는 심각한 오류가 발생하기 때문입니다.

**2. 필요한 조인만 생성 (Join Pruning)**: 논리 모델이 15개 테이블을
포함하더라도, 쿼리가 그중 3개 필드만 사용하면 해당 3개 필드에 필요한
최소한의 조인 구문만 CTE 내에 생성하여 방출합니다.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from tablefold.ir import (
    FieldKind,
    JoinStep,
    LogicalField,
    LogicalLayer,
    LogicalModel,
)
from tablefold.relate.graph import SchemaGraph

_BASE_ALIAS = "base"

# 집계 서브쿼리 안에서 자식 테이블에 붙이는 별칭.
_CHILD_ALIAS = "src"

# 인용하지 않으면 파서가 문법 요소로 읽어 버리는 이름들.
#
# sqlglot 은 방언별 예약어 목록을 노출하지 않는다 (``RESERVED_KEYWORDS`` 가 비어
# 있다). 전부 인용하는 방법도 있지만, PostgreSQL 에서 인용은 대소문자를 고정시켜
# 카탈로그에 저장된 이름과 어긋날 위험을 만든다. 그래서 실제 스키마에 컬럼/테이블
# 이름으로 등장하는 예약어만 골라 인용한다.
#
# 목록에 없는 예약어는 여전히 터진다. 다만 터지지 조용히 틀리지는 않으므로,
# 없는 것을 발견하면 여기 추가하면 된다. (``plan`` 은 T-SQL 에서 실제로 부딪혔다.)
_NEEDS_QUOTING = frozenset(
    {
        "all", "and", "any", "as", "asc", "between", "by", "case", "cast", "check",
        "column", "constraint", "create", "cross", "current", "current_date",
        "current_time", "current_timestamp", "current_user", "default", "desc",
        "distinct", "do", "else", "end", "except", "exists", "false", "for",
        "foreign", "from", "full", "grant", "group", "having", "in", "index",
        "inner", "insert", "intersect", "into", "is", "join", "key", "left",
        "like", "limit", "natural", "not", "null", "offset", "on", "only", "or",
        "order", "outer", "plan", "primary", "references", "returning", "right",
        "rows", "select", "session_user", "set", "some", "table", "then", "to",
        "true", "union", "unique", "update", "user", "using", "values", "when",
        "where", "window", "with",
    }
)


def _ident(name: str) -> exp.Identifier:
    """스키마에서 읽은 이름을 식별자로. 예약어면 인용한다.

    우리가 만든 별칭(``base``, ``j_...``, ``agg_...``)에는 쓰지 않는다. 그쪽은
    구성상 안전하고, 인용하면 결과 SQL 만 읽기 나빠진다.
    """
    return exp.to_identifier(name, quoted=name.lower() in _NEEDS_QUOTING)

# CTEs are named with this prefix rather than the model's own name. A model is
# usually named after its anchor table, and a CTE named `orders` whose body
# reads `FROM orders` is a self-reference — the physical table becomes
# unreachable and the query either errors or silently reads the CTE. Renaming
# the CTE and aliasing it back at the call site keeps both names addressable.
_CTE_PREFIX = "tf__"


class ExpansionError(Exception):
    """The query cannot be expanded against the given logical layer."""


class FilterOnlyMisuse(ExpansionError):
    """필터 전용 필드를 값으로 쓰려 했다.

    별도 타입인 이유는 **다른 모델로 바꿔도 안 되는 거부**이기 때문이다. 모르는
    필드는 다른 모델에 있을 수 있지만, 필터 전용 필드를 ``SELECT`` 나 ``CASE``
    에 쓰는 것은 어느 모델에서도 뜻이 성립하지 않는다 — 앵커 한 행에 대응하는
    값이 자체가 없다. 호출자가 "모델을 바꿔 볼까"와 "그래 봐야 소용없다"를
    문자열 파싱 없이 가를 수 있어야 한다.
    """


@dataclass(frozen=True)
class ExpansionResult:
    sql: str
    models_used: tuple[str, ...]
    fields_used: tuple[str, ...]
    joins_emitted: int
    joins_available: int

    @property
    def joins_pruned(self) -> int:
        return self.joins_available - self.joins_emitted


def expand(
    sql: str,
    layer: LogicalLayer,
    graph: SchemaGraph,
    *,
    dialect: str = "postgres",
    pretty: bool = True,
) -> ExpansionResult:
    """논리 모델을 참조하는 *sql*을
    실제 물리 테이블 대상 구문으로 다시 재작성(Expand)합니다.
    """
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
        raise ExpansionError(f"could not parse SQL: {exc}") from exc

    _reject_shadowing_ctes(statement, layer, graph)
    _reject_self_join(statement, layer)

    referenced = _referenced_models(statement, layer)
    if not referenced:
        raise ExpansionError(
            "query references no logical model; "
            f"known models: {', '.join(m.name for m in layer.models)}"
        )
    _reject_multiple_models(statement, layer)

    mentioned = _mentioned_columns(statement)
    selects_star = _selects_star(statement)

    ctes: list[tuple[str, exp.Select]] = []
    used_fields: list[str] = []
    emitted = 0
    available = 0

    # 모르는 컬럼은 하나라도 있으면 거부한다.
    #
    # 예전에는 "매칭된 필드가 하나도 없을 때"만 거부했는데, 그러면 아는 컬럼과
    # 모르는 컬럼이 섞인 질의가 조용히 통과했다. 모르는 컬럼은 CTE에 투영되지
    # 않으므로 결과 SQL은 그 이름을 해석할 수 없고, 실패는 여기가 아니라
    # 데이터베이스에서 일어난다. 이름을 지어낸 곳에서 멀리 떨어진 지점이다.
    unknown = _unknown_columns(mentioned, referenced) - _usable_aliases(statement)
    if unknown and not selects_star:
        known = sorted({n for m in referenced for n in m.field_names})
        raise ExpansionError(
            f"query references unknown fields: {', '.join(sorted(unknown))}; "
            f"models: {', '.join(m.name for m in referenced)}; "
            f"available: {', '.join(known[:12])}…"
        )

    # 필터 전용 필드에 걸린 조건은 집계 서브쿼리 안으로 옮긴다. 바깥에 남으면
    # 이미 집계된 값에 조건을 거는 셈이라 뜻이 달라진다.
    statement, pushed = _pushdown(statement, referenced)
    _reject_unpushable(statement, referenced)

    for model in referenced:
        fields = _required_fields(model, mentioned, star=selects_star)
        _reject_projected_filters(model, statement, fields)
        select, join_count = _build_model_select(
            model, graph, fields, pushed=pushed
        )
        ctes.append((model.name, select))
        used_fields.extend(f"{model.name}.{f.name}" for f in fields)
        emitted += join_count
        available += _available_join_count(model)

    rewritten = _rebind_model_references(statement, referenced)
    for name, select in ctes:
        rewritten = rewritten.with_(_cte_name(name), as_=select, copy=False)

    return ExpansionResult(
        sql=rewritten.sql(dialect=dialect, pretty=pretty),
        models_used=tuple(m.name for m in referenced),
        fields_used=tuple(used_fields),
        joins_emitted=emitted,
        joins_available=available,
    )


# ── query inspection ──────────────────────────────────────────────────────────


def _referenced_models(
    statement: exp.Expression, layer: LogicalLayer
) -> tuple[LogicalModel, ...]:
    found: list[LogicalModel] = []
    for table in _model_table_nodes(statement, layer):
        model = layer.model(table.name)
        if model is not None and all(m.name != model.name for m in found):
            found.append(model)
    return tuple(found)


def _model_table_nodes(
    statement: exp.Expression, layer: LogicalLayer
) -> list[exp.Table]:
    return [t for t in statement.find_all(exp.Table) if layer.model(t.name) is not None]


def _cte_names(statement: exp.Expression) -> set[str]:
    """질의가 스스로 정의한 CTE 이름들."""
    return {
        cte.alias_or_name.lower()
        for with_ in statement.find_all(exp.With)
        for cte in with_.expressions
        if cte.alias_or_name
    }


def _reject_shadowing_ctes(
    statement: exp.Expression, layer: LogicalLayer, graph: SchemaGraph
) -> None:
    """질의의 CTE 가 모델이나 물리 테이블 이름을 가리면 거부한다.

    ``WITH products AS (...)`` 를 쓰면 확장이 만드는 ``FROM products AS base`` 가
    물리 테이블이 아니라 그 CTE 를 읽는다. 데이터베이스는 정상적으로 실행하고
    엉뚱한 원본에서 뽑은 값을 돌려준다 — 에러 없이 틀리는 종류다.

    ``_CTE_PREFIX`` 는 *우리가* 만든 CTE 와 물리 테이블의 충돌만 막는다. 질의가
    들고 온 이름까지는 막지 못하므로 여기서 거른다.
    """
    defined = _cte_names(statement)
    if not defined:
        return

    physical = {t.name.lower() for t in graph.schema.tables}
    models = {m.name.lower() for m in layer.models}
    clashes = sorted(defined & (physical | models))
    if clashes:
        raise ExpansionError(
            f"CTE names shadow real tables or models: {', '.join(clashes)}; "
            "rename them — the expansion reads those names from the database"
        )


def _table_sources(select: exp.Select) -> list[exp.Table]:
    """이 ``SELECT`` 가 직접 읽는 테이블. 서브쿼리 안쪽은 세지 않는다.

    ``FROM`` 과 ``JOIN`` 의 *직속* 자식만 본다. ``find_all`` 로 훑으면 ``FROM``
    안의 서브쿼리가 읽는 테이블까지 딸려 오는데, 그건 이 ``SELECT`` 의 행 수를
    바꾸지 않고 자기 ``Select`` 노드로 따로 검사된다.

    sqlglot 은 버전에 따라 ``from`` 과 ``from_`` 중 하나를 키로 쓴다. 둘 다 본다 —
    못 찾으면 검사가 조용히 아무것도 안 하게 되고, 그게 이 함수가 막으려는 것보다
    나쁘다.
    """
    found: list[exp.Table] = []
    source = select.args.get("from_") or select.args.get("from")
    if source is not None and isinstance(source.this, exp.Table):
        found.append(source.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            found.append(join.this)
    return found


def _reject_self_join(statement: exp.Expression, layer: LogicalLayer) -> None:
    """같은 모델을 한 ``FROM`` 절에 두 번 놓으면 거부한다.

    ``FROM m AS a, m AS b`` 는 두 CTE 의 곱집합이다. 모델 하나가 입도를 지켜도
    자기 자신과 이으면 행이 곱해지고, ``GROUP BY`` 가 한쪽 키로만 걸리면 합계가
    상대편 행 수만큼 부푼다 — 3장의 "49배" 와 같은 종류이고, 문법이 맞으므로
    데이터베이스는 조용히 실행한다.

    :func:`_reject_multiple_models` 가 못 잡는다. 그쪽은 서로 다른 모델 *이름* 의
    수를 세는데, 자기 조인은 이름이 하나다. 스칼라 서브쿼리에서 같은 모델을 다시
    읽는 정상 질의를 막지 않으려면 두 검사가 따로 있어야 한다 — 서브쿼리는 자기
    ``Select`` 를 가지므로 여기서 세어지지 않는다.

    실제로 LLM 이 이 모양을 냈다: "A사업장과 B사업장 비교" 에 ``FROM D_SA_ORG A,
    D_SA_ORG B`` 로 답했고, 고치기 전에는 통과했다.
    """
    for select in statement.find_all(exp.Select):
        seen: set[str] = set()
        for table in _table_sources(select):
            model = layer.model(table.name)
            if model is None:
                continue
            if model.name.lower() in seen:
                raise ExpansionError(
                    f"query joins model '{model.name}' to itself; "
                    "한 FROM 절에 같은 모델은 한 번만 놓는다 — 자기 조인은 행을 "
                    "곱해서 합계를 부풀린다. 두 값을 나란히 보려면 조건부 집계를 "
                    "쓴다: SUM(CASE WHEN … THEN x ELSE 0 END)"
                )
            seen.add(model.name.lower())


def _reject_multiple_models(statement: exp.Expression, layer: LogicalLayer) -> None:
    """모델을 두 번 이상 참조하면 거부한다.

    와이드 모델은 조인을 없애려고 존재한다. 두 모델을 ``JOIN`` 하면 그 문제가
    그대로 돌아오고, 게다가 두 모델이 같은 필드 이름을 가지면(retail 의 ``orders``
    와 ``products`` 는 8개를 공유한다) 확장 결과의 바깥 ``SELECT`` 가 한정자 없이
    모호해져 데이터베이스에서 터진다.

    세는 것은 테이블 *노드* 가 아니라 서로 다른 *모델* 의 수다. 노드를 세면
    ``WHERE amount > (SELECT AVG(amount) FROM orders)`` 처럼 같은 모델을 스칼라
    서브쿼리에서 다시 읽는 정상 질의가 막힌다 — 그 참조들은 모두 같은 CTE 를
    가리키므로 모호할 것이 없다.
    """
    named = sorted({t.name for t in _model_table_nodes(statement, layer)})
    if len(named) <= 1:
        return
    raise ExpansionError(
        f"query references {len(named)} models ({', '.join(named)}); "
        "a wide model is meant to answer on its own — read one model per query. "
        "If the question spans both, the layer needs an anchor holding them together"
    )


def _mentioned_columns(statement: exp.Expression) -> frozenset[str]:
    return frozenset(
        column.name.lower() for column in statement.find_all(exp.Column) if column.name
    )


def _selects_star(statement: exp.Expression) -> bool:
    """True only for ``SELECT *`` / ``SELECT t.*``.

    Scanning for any ``Star`` node would also fire on ``COUNT(*)``, which
    appears in most real aggregate queries and would silently disable field
    pruning for all of them.
    """
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                return True
            if isinstance(projection, exp.Column) and isinstance(
                projection.this, exp.Star
            ):
                return True
    return False


def _required_fields(
    model: LogicalModel, mentioned: frozenset[str], *, star: bool
) -> tuple[LogicalField, ...]:
    if star:
        # `SELECT *` 는 필터 전용 필드를 포함하지 않는다. 값으로 꺼낼 수 없는
        # 필드이므로 투영 목록에 들어가면 확장 자체가 깨진다.
        return tuple(f for f in model.fields if not f.filter_only)
    return tuple(f for f in model.fields if f.name.lower() in mentioned)


# ── 술어 밀어넣기 ─────────────────────────────────────────────────────────────


def _split_conjuncts(where: exp.Expression | None) -> list[exp.Expression]:
    """WHERE 절을 AND 로 끊는다. OR 아래는 쪼개지 않는다."""
    if where is None:
        return []
    node = where.this if isinstance(where, exp.Where) else where
    if isinstance(node, exp.And):
        return _split_conjuncts(node.this) + _split_conjuncts(node.expression)
    return [node]


def _pushdown(
    statement: exp.Expression, models: tuple[LogicalModel, ...]
) -> tuple[
    exp.Expression, dict[tuple[str, str], list[tuple[exp.Expression, LogicalField]]]
]:
    """필터 전용 필드에 걸린 조건을 해당 집계 서브쿼리로 옮긴다.

    사전집계된 값에는 기간을 걸 수 없다 — ``SUM(SALES_AMT)`` 은 이미 전 기간을
    더한 뒤이기 때문이다. 조건을 집계 *안* 으로 밀어 넣어야 "이번 달 매출"이
    성립한다. 그렇게 해도 서브쿼리는 여전히 부모 키당 한 행을 내므로 앵커의
    입도는 그대로다.

    AND 로 끊은 조각 단위로 옮긴다. 한 조각이 필터 전용 필드 하나만 참조하고
    그 필드가 어느 자식에 속하는지 분명할 때만 옮기고, 나머지는 바깥에 남긴다.
    OR 로 묶인 조건은 쪼개면 뜻이 달라지므로 손대지 않는다 — 그런 조건이 필터
    전용 필드를 건드리면 :func:`expand` 가 거부한다.

    반환값의 두 번째 항목은 ``(모델, 자식 키) -> [(조건, 필드)]`` 이다. 자식 키는
    :func:`_child_key` — 테이블 이름만으로는 부족하다. 한 자식이 부모를 두 키로
    참조하면 (``order_links.src_order_id`` 와 ``dst_order_id``) 집계는
    :func:`_child_key` 로 갈라지는데 술어를 테이블 이름으로만 찾으면 두 집계가
    같은 조건을 받는다. 사용자가 조건을 걸지 않은 집계까지 걸러지고 그 조인이
    ``LEFT`` 에서 ``INNER`` 로 뒤집힌다 — 에러 없이 값이 틀린다.

    필드를 같이 들고 다니는 이유는, 조건이 자식이 아니라 자식이 *가리키는*
    차원에 걸릴 수 있기 때문이다. 그 경우 집계 서브쿼리 안에 조인이 하나 더
    필요하고, 어느 조인인지는 필드의 경로에만 적혀 있다.
    """
    # 이름 하나에 여러 (모델, 필드) 가 걸릴 수 있다. 딕셔너리로 덮어쓰면 나중
    # 것이 이기고, 조건이 사용자가 지목하지 않은 모델의 서브쿼리로 조용히
    # 옮겨간다 — retail 에서 필터 전용 이름 15개가 두 모델에 겹친다.
    #
    # 지금은 :func:`_reject_multiple_models` 가 모델 하나만 허용하므로 실제로
    # 겹칠 일이 없지만, 그 보장에 기대지 않는다. 모호하면 옮기지 않고 남기고,
    # 남은 참조는 :func:`_reject_unpushable` 이 에러로 만든다.
    by_name: dict[str, list[tuple[LogicalModel, LogicalField]]] = {}
    for m in models:
        for f in m.fields:
            if f.filter_only and f.source.path:
                by_name.setdefault(f.name.lower(), []).append((m, f))
    if not by_name:
        return statement, {}

    rewritten = statement.copy()
    pushed: dict[tuple[str, str], list[tuple[exp.Expression, LogicalField]]] = {}

    for select in rewritten.find_all(exp.Select):
        where = select.args.get("where")
        if where is None:
            continue

        keep: list[exp.Expression] = []
        for part in _split_conjuncts(where):
            columns = {c.name.lower() for c in part.find_all(exp.Column) if c.name}
            names = columns & by_name.keys()
            # 조각이 필터 전용 필드 하나만 참조해야 옮길 수 있다. 다른 컬럼이
            # 섞여 있으면 (``a = 1 OR total > 10``) 옮기는 순간 그 컬럼이 자식
            # 테이블에서 해석되어 뜻이 달라진다.
            if len(names) != 1 or columns != names:
                keep.append(part)
                continue

            owners = by_name[next(iter(names))]
            if len(owners) != 1:
                # 어느 모델의 필드인지 정할 수 없다. 옮기면 반쯤은 틀린다.
                keep.append(part)
                continue

            model, field = owners[0]
            moved = part.copy()
            # 자식 자신의 컬럼이면 한정자 없이, 자식이 가리키는 차원의 컬럼이면
            # 서브쿼리 안에서 그 차원에 붙일 별칭으로 한정한다.
            qualifier = (
                _dim_alias(field.source.path[1]) if len(field.source.path) > 1 else None
            )
            for column in moved.find_all(exp.Column):
                if column.name.lower() == field.name.lower():
                    column.set("this", _ident(field.source.column))
                    column.set(
                        "table", exp.to_identifier(qualifier) if qualifier else None
                    )
            pushed.setdefault(
                (model.name.lower(), _child_key(field.source.path[0])), []
            ).append((moved, field))

        select.set(
            "where", exp.Where(this=_conjunction(keep)) if keep else None
        )

    return rewritten, pushed


def _filter_only_names(models: tuple[LogicalModel, ...]) -> set[str]:
    return {f.name.lower() for m in models for f in m.fields if f.filter_only}


def _reject_unpushable(
    statement: exp.Expression, models: tuple[LogicalModel, ...]
) -> None:
    """옮기지 못한 필터 전용 참조가 남아 있으면 거부한다.

    바깥에 남은 참조는 CTE에 그 이름이 없으므로 데이터베이스에서 터진다.
    조용히 통과시키느니 여기서 멈추는 편이 낫다.
    """
    leftover = {
        c.name.lower() for c in statement.find_all(exp.Column) if c.name
    } & _filter_only_names(models)
    if leftover:
        raise FilterOnlyMisuse(
            f"filter-only fields cannot be used here: {', '.join(sorted(leftover))}; "
            "they may only appear in WHERE, joined by AND, one field per condition"
        )


def _reject_projected_filters(
    model: LogicalModel, statement: exp.Expression, fields: tuple[LogicalField, ...]
) -> None:
    """필터 전용 필드를 SELECT 로 꺼내려 하면 거부한다."""
    projected = {
        c.name.lower()
        for select in statement.find_all(exp.Select)
        for projection in select.expressions
        for c in projection.find_all(exp.Column)
        if c.name
    }
    bad = projected & {f.name.lower() for f in fields if f.filter_only}
    if bad:
        raise FilterOnlyMisuse(
            f"model '{model.name}' fields {', '.join(sorted(bad))} are filter-only; "
            "they carry no value at the model's grain and cannot be selected"
        )


def _unknown_columns(
    mentioned: frozenset[str], models: tuple[LogicalModel, ...]
) -> frozenset[str]:
    known = {name.lower() for model in models for name in model.field_names}
    return frozenset(mentioned - known)


def _output_aliases(statement: exp.Expression) -> frozenset[str]:
    """질의가 스스로 만든 이름들.

    ``SELECT SUM(amt) AS revenue ... ORDER BY revenue`` 에서 ``revenue`` 는
    컬럼 참조로 파싱되지만 모델의 필드가 아니다. 미지의 컬럼을 거부할 때
    이런 이름까지 걸면 정상 질의가 막힌다.
    """
    found: set[str] = set()
    for select in statement.find_all(exp.Select):
        for projection in select.expressions:
            if not isinstance(projection, exp.Alias):
                continue
            alias = projection.alias_or_name
            if alias:
                found.add(alias.lower())
    return frozenset(found)


def _usable_aliases(statement: exp.Expression) -> frozenset[str]:
    """면제해 줄 자기 이름들. 실제로 쓸 수 있는 자리에 있을 때만.

    출력 별칭은 ``ORDER BY`` 와 ``HAVING`` 에서만 참조할 수 있다. ``WHERE`` 는
    투영보다 먼저 평가되므로 거기 쓴 별칭은 어차피 데이터베이스가 거부한다.
    그런데 면제를 무조건 걸어 두면 ``SELECT amount AS foo ... WHERE foo = 1`` 이
    미지 컬럼 검사를 통과해 버리고, 실패는 여기가 아니라 데이터베이스에서 난다.
    """
    aliases = _output_aliases(statement)
    if not aliases:
        return aliases

    in_where: set[str] = set()
    for select in statement.find_all(exp.Select):
        where = select.args.get("where")
        if where is None:
            continue
        in_where |= {c.name.lower() for c in where.find_all(exp.Column) if c.name}
    return aliases - in_where


def _rebind_model_references(
    statement: exp.Expression, models: tuple[LogicalModel, ...]
) -> exp.Expression:
    """Point each model reference at its CTE while keeping the model's name.

    ``FROM orders`` becomes ``FROM tf__orders AS orders``, so column references
    written against the model still resolve and an explicit alias the caller
    wrote is left alone.
    """
    by_name = {model.name.lower(): model for model in models}
    rewritten = statement.copy()

    for table in rewritten.find_all(exp.Table):
        model = by_name.get(table.name.lower())
        if model is None:
            continue
        table.set("this", exp.to_identifier(_cte_name(model.name)))
        table.set("db", None)
        table.set("catalog", None)
        if table.alias is None or table.alias == "":
            table.set("alias", exp.TableAlias(this=exp.to_identifier(model.name)))

    return rewritten


def _cte_name(model_name: str) -> str:
    return f"{_CTE_PREFIX}{model_name}"


# ── model expansion ───────────────────────────────────────────────────────────


def _build_model_select(
    model: LogicalModel,
    graph: SchemaGraph,
    fields: tuple[LogicalField, ...],
    *,
    pushed: dict[tuple[str, str], list[tuple[exp.Expression, LogicalField]]]
    | None = None,
) -> tuple[exp.Select, int]:
    base = graph.schema.table(model.base_table)
    if base is None:
        raise ExpansionError(f"base table '{model.base_table}' is not in the schema")

    projections: list[exp.Expression] = []
    join_paths: dict[tuple[str, ...], tuple[JoinStep, ...]] = {}
    child_steps: dict[str, JoinStep] = {}
    child_fields: dict[str, list[LogicalField]] = {}

    for field in fields:
        source = field.source

        if source.kind is FieldKind.BASE:
            projections.append(
                _aliased(_column(_BASE_ALIAS, source.column), field.name)
            )

        elif source.kind is FieldKind.JOINED:
            # Register every prefix of the path — reaching a two-hop table
            # requires the one-hop table to be joined first.
            for depth in range(1, len(source.path) + 1):
                prefix = source.path[:depth]
                join_paths.setdefault(_path_key(prefix), prefix)
            alias = _path_alias(source.path)
            projections.append(_aliased(_column(alias, source.column), field.name))

        elif source.kind is FieldKind.AGGREGATED:
            step = source.path[0]
            # 자식 테이블 이름만으로 묶으면 안 된다. 자식이 부모를 두 개의 키로
            # 참조하면 (``order_links.src_order_id`` 와 ``dst_order_id``) 두 집계가
            # 하나의 서브쿼리로 뭉개져 같은 값이 두 번 나온다 — 에러 없이.
            key = _child_key(step)
            child_steps.setdefault(key, step)
            if field.filter_only:
                # 값이 아니라 조건을 받는 자리다. 투영하지 않는다 —
                # 조건은 이미 서브쿼리 안으로 옮겨졌다.
                continue
            child_fields.setdefault(key, []).append(field)
            alias = _aggregate_alias(key)
            projections.append(_aliased(_column(alias, field.name), field.name))

    if not projections:
        # `SELECT COUNT(*) FROM orders` names the model but none of its fields.
        # The query still needs one row per anchor row, so project the key and
        # emit no joins at all.
        projections = [
            _aliased(_column(_BASE_ALIAS, column), column)
            for column in (base.primary_key or (base.column_names[0],))
        ]

    select = exp.select(*projections).from_(
        exp.alias_(_physical(base.name, graph), _BASE_ALIAS, table=True)
    )

    for _, path in sorted(join_paths.items()):
        select = select.join(
            _many_to_one_join(path, graph),
            on=_join_condition(path),
            join_type="LEFT",
        )

    for key, step in sorted(child_steps.items()):
        # 술어도 집계와 같은 키로 찾는다. 테이블 이름으로 찾으면 같은 자식을
        # 두 키로 접은 두 집계가 서로의 조건을 받는다 — :func:`_pushdown` 참고.
        predicates = (pushed or {}).get((model.name.lower(), key), [])
        subquery = _aggregate_subquery(
            step.to_table, step, child_fields.get(key, []), graph,
            predicates=predicates,
        )
        alias = _aggregate_alias(key)
        select = select.join(
            exp.alias_(subquery.subquery(), alias, table=True),
            on=_aggregate_condition(step, alias),
            # 조건이 걸린 자식은 INNER 로 붙인다.
            #
            # "7월 매출"을 물으면 7월에 매출이 없는 조직은 답에서 빠져야 한다.
            # LEFT 로 두면 그런 부모가 NULL 을 달고 살아남는다 — NL2SQL 실측에서
            # 13행이 나왔고 그중 5행이 NULL 이었다. 정답은 8행이다.
            #
            # 조건이 없으면 LEFT 를 유지한다. 자식이 없다는 사실 자체가 답의
            # 일부인 질문("주문이 하나도 없는 고객")을 지우면 안 되기 때문이다.
            join_type="INNER" if predicates else "LEFT",
        )

    return select, len(join_paths) + len(child_steps)


def _physical(table_name: str, graph: SchemaGraph) -> exp.Table:
    """물리 테이블 참조. 스키마가 있으면 한정자를 붙인다.

    붙이지 않으면 ``sales.orders`` 가 ``orders`` 로 나가고, 기본 스키마가 아닌
    데이터베이스에서는 테이블을 못 찾거나 — 더 나쁘게 — 같은 이름의 다른
    스키마 테이블을 읽는다.
    """
    found = graph.schema.table(table_name)
    node = exp.Table(this=_ident(table_name))
    if found is not None and found.schema:
        node.set("db", _ident(found.schema))
    return node


def _many_to_one_join(
    path: tuple[JoinStep, ...], graph: SchemaGraph
) -> exp.Expression:
    step = path[-1]
    return exp.alias_(
        _physical(step.to_table, graph), _path_alias(path), table=True
    )


def _join_condition(path: tuple[JoinStep, ...]) -> exp.Expression:
    step = path[-1]
    left_alias = _BASE_ALIAS if len(path) == 1 else _path_alias(path[:-1])
    right_alias = _path_alias(path)

    if len(step.from_columns) != len(step.to_columns):
        raise ExpansionError(
            f"foreign key {step.from_table} -> {step.to_table} has mismatched "
            f"column counts ({len(step.from_columns)} vs {len(step.to_columns)})"
        )

    return _conjunction(
        [
            exp.EQ(
                this=_column(left_alias, left),
                expression=_column(right_alias, right),
            )
            for left, right in zip(step.from_columns, step.to_columns, strict=True)
        ]
    )


def _dim_alias(step: JoinStep) -> str:
    """집계 서브쿼리 안에서 자식의 차원에 붙일 별칭."""
    return f"dim_{step.to_table.lower()}"


def _aggregate_subquery(
    child: str,
    step: JoinStep,
    fields: list[LogicalField],
    graph: SchemaGraph,
    *,
    predicates: list[tuple[exp.Expression, LogicalField]] | None = None,
) -> exp.Select:
    """Group a child table by its foreign key before it is ever joined.

    This is the grain guard. The subquery yields at most one row per parent key,
    so the join that follows cannot change the parent's row count.
    """
    # 자식의 차원을 조인해야 하면 자식에 별칭을 주고 모든 참조를 한정한다.
    # 한정하지 않으면 자식과 차원이 같은 이름의 컬럼을 가질 때 (조인 키가 바로
    # 그렇다) 데이터베이스가 "Ambiguous column name" 으로 거절한다.
    needs_dim = any(len(f.source.path) > 1 for _, f in (predicates or []))
    src = _CHILD_ALIAS if needs_dim else None
    def col(name: str) -> exp.Column:
        return _column(src, name) if src else exp.Column(this=_ident(name))

    group_columns = [col(c) for c in step.to_columns]
    projections: list[exp.Expression] = list(group_columns)

    for field in fields:
        source = field.source
        if source.aggregate == "count":
            call: exp.Expression = exp.Count(this=exp.Star())
        elif source.aggregate == "sum":
            call = exp.Sum(this=col(source.column))
        elif source.aggregate == "avg":
            call = exp.Avg(this=col(source.column))
        elif source.aggregate == "max":
            call = exp.Max(this=col(source.column))
        elif source.aggregate == "min":
            call = exp.Min(this=col(source.column))
        else:
            raise ExpansionError(
                f"unsupported aggregate '{source.aggregate}' on field '{field.name}'"
            )
        projections.append(_aliased(call, field.name))

    if graph.schema.table(child) is None:
        raise ExpansionError(f"child table '{child}' is not in the schema")

    base = _physical(child, graph)
    select = exp.select(*projections).from_(
        exp.alias_(base, src, table=True) if src else base
    )

    if predicates:
        # 조건이 자식이 아니라 자식의 차원에 걸리면 여기서 그 차원을 조인한다.
        # "매출액 계정만 합계" 는 ``F_PL`` 이 아니라 ``D_PL_ACCT`` 의 이야기이고,
        # 그 조인이 없으면 조건을 걸 자리가 없다. 차원은 N:1 이므로 자식의 행
        # 수가 늘지 않는다 — 집계 결과도 그대로다.
        joined: set[str] = set()
        for _, field in predicates:
            if len(field.source.path) < 2:
                continue
            dim_step = field.source.path[1]
            alias = _dim_alias(dim_step)
            if alias in joined:
                continue
            joined.add(alias)
            select = select.join(
                exp.alias_(_physical(dim_step.to_table, graph), alias, table=True),
                on=_conjunction(
                    [
                        exp.EQ(this=col(left), expression=_column(alias, right))
                        for left, right in zip(
                            dim_step.from_columns, dim_step.to_columns, strict=True
                        )
                    ]
                ),
                join_type="INNER",
            )
        # 밀어넣은 조건은 GROUP BY 앞에 걸린다. 집계가 조건에 걸린 행만 보게
        # 되고, 그래야 "이번 달 매출"이 실제로 이번 달만 더한다.
        select = select.where(_conjunction([p for p, _ in predicates]))

    return select.group_by(*group_columns)


def _aggregate_condition(step: JoinStep, alias: str) -> exp.Expression:
    return _conjunction(
        [
            exp.EQ(
                this=_column(_BASE_ALIAS, left),
                expression=_column(alias, right),
            )
            for left, right in zip(step.from_columns, step.to_columns, strict=True)
        ]
    )


def _available_join_count(model: LogicalModel) -> int:
    paths: set[tuple[str, ...]] = set()
    children: set[str] = set()
    for field in model.fields:
        source = field.source
        if source.kind is FieldKind.JOINED:
            for depth in range(1, len(source.path) + 1):
                paths.add(_path_key(source.path[:depth]))
        elif source.kind is FieldKind.AGGREGATED and source.path:
            children.add(_child_key(source.path[0]))
    return len(paths) + len(children)


# ── naming and small builders ─────────────────────────────────────────────────


def _path_key(path: tuple[JoinStep, ...]) -> tuple[str, ...]:
    return tuple(_step_key(step) for step in path)


def _step_key(step: JoinStep) -> str:
    """한 조인 단계의 신원. 대상 테이블만으로는 부족하다.

    ``orders.buyer_id`` 와 ``orders.seller_id`` 는 둘 다 ``users`` 로 가지만 서로
    다른 조인이다. 대상 이름만 쓰면 두 경로가 같은 별칭으로 뭉개져서 한쪽이
    사라지거나 엉뚱한 행이 붙는다.
    """
    return f"{step.to_table.lower()}@{'_'.join(c.lower() for c in step.from_columns)}"


def _path_alias(path: tuple[JoinStep, ...]) -> str:
    """A stable alias per join path.

    Keyed on the whole path, not the target table: ``countries`` reached via
    ``addresses`` and via ``stores`` are two different joins and must not
    collapse onto one alias.
    """
    return "j_" + "__".join(
        f"{step.to_table.lower()}_{'_'.join(c.lower() for c in step.from_columns)}"
        for step in path
    )


def _child_key(step: JoinStep) -> str:
    """1:N 자식 관계 하나의 신원. 자식 테이블 이름 + 그 쪽 조인 컬럼.

    같은 자식이 부모를 여러 키로 참조할 수 있으므로 테이블 이름만으로는
    구분되지 않는다.
    """
    return f"{step.to_table.lower()}@{'_'.join(c.lower() for c in step.to_columns)}"


def _aggregate_alias(child_key: str) -> str:
    return "agg_" + child_key.replace("@", "_")


def _column(table_alias: str, column: str) -> exp.Column:
    return exp.Column(this=_ident(column), table=exp.to_identifier(table_alias))


def _aliased(node: exp.Expression, alias: str) -> exp.Expression:
    return exp.alias_(node, _ident(alias))


def _conjunction(parts: list[exp.Expression]) -> exp.Expression:
    if not parts:
        raise ExpansionError("join has no key columns")
    condition = parts[0]
    for part in parts[1:]:
        condition = exp.And(this=condition, expression=part)
    return condition
