"""노출된 정보량을 비트로 잰다 — "압축"이라고 말할 근거를 만드는 모듈.

폴드는 정보를 압축하지 않는다. N:1 차원을 인라인하면 같은 값이 행마다 복제되고,
그 결과 항목 수가 물리 컬럼 수보다 많아지기도 한다. 그렇다면 적어도 **재야
한다**: 원본 스키마가 담던 정보와 레이어가 대변하는 정보를 같은 단위로 놓고
비교하면, 늘었는지 줄었는지 주장이 아니라 관측으로 끝난다.

단위는 비트다. 한 컬럼의 정보량은 서로 다른 값의 수의 로그 — ``log2(distinct)``
— 로 근사한다. 이것은 저장 비트가 아니라 **설명해야 할 내용**의 크기다. LLM 에게
컬럼 하나를 설명하는 비용은 그 컬럼이 담을 수 있는 질문의 폭에 비례하기 때문이다.

함께 보고할 두 가지:

* **복제율** — 논리 필드 수 ÷ 서로 다른 (표, 컬럼) 짝. 1 보다 크면 같은 컬럼이
  여러 통로(합계·조건·모델)로 나뉜 것이고, 인라인 정책이 얼마나 넓어졌는지의
  직접 증거다.
* **정보 보존율** — 노출된 짝의 비트 합 ÷ 원본 전체의 비트 합. 집계로 접힌
  자식과 버려진 잡음만큼 작아진다.

카디널리티는 데이터에서 온다. 커서가 없으면 측정을 거짓으로 채우지 않고
``measured: false`` 로 남긴다 — 스키마만으로는 distinct 를 알 수 없고, 알 수
없는 것을 안다는 식으로 쓰면 이 모듈이 존재하는 이유가 사라진다.
"""

from __future__ import annotations

import math

from tablefold.choose.cost import is_noise
from tablefold.ir import LogicalLayer, PhysicalSchema
from tablefold.relate.validate import Cursor

# 표 하나의 카디널리티를 한 번에 읽는 상한. 넘으면 그 표는 미측정으로 남긴다.
MAX_COLUMNS_PER_TABLE = 80


def measure(
    layer: LogicalLayer,
    schema: PhysicalSchema,
    *,
    cursor: Cursor | None = None,
    dialect: str = "tsql",
) -> dict:
    """원본과 레이어의 정보량을 비교해 돌려준다.

    카디널리티는 표당 **한 번의 질의**로 일괄 읽는다(``COUNT(DISTINCT …)`` 나열).
    커서가 없으면 비트는 계산하지 않고 구조 통계(복제율)만 돌려준다.
    """
    exposed = _exposed_pairs(layer, schema)
    all_columns = {
        (t.name.lower(), c.name.lower()): c.name
        for t in schema.tables
        for c in t.columns
    }
    noise = {k for k in all_columns if is_noise(k[1])}

    source_pairs = [k for k in all_columns if k not in noise]
    duplication = round(layer.field_count / len(exposed), 2) if exposed else 1.0

    result: dict = {
        "measured": False,
        "source_bits": None,
        "exposed_bits": None,
        "retention": None,
        "source_columns": len(source_pairs),
        "exposed_columns": len(exposed),
        "duplication_factor": duplication,
        "unmeasured_tables": [],
    }
    if cursor is None:
        return result

    cardinalities, unmeasured = _cardinalities(schema, cursor, dialect)
    result["unmeasured_tables"] = unmeasured

    def bits(pairs: list[tuple[str, str]]) -> float | None:
        total = 0.0
        for t, c in pairs:
            found = cardinalities.get((t, c))
            if found is None:
                # 한 컬럼이라도 못 읽었으면 부분 합은 거짓 보고가 된다.
                return None
            # 값이 하나뿐인 컬럼도 설명에는 한 칸을 쓴다. log2(1)=0 이면
            # 상수 컬럼이 공짜가 되는데, 화면에서 지우는 것도 공짜가 아니다.
            total += math.log2(max(found, 1)) + 1
        return total

    result["source_bits"] = bits(source_pairs)
    result["exposed_bits"] = bits(sorted(exposed))
    if result["source_bits"] and result["exposed_bits"] is not None:
        result["retention"] = round(result["exposed_bits"] / result["source_bits"], 4)
    else:
        result["measured"] = False
        return result
    result["measured"] = True
    return result


def _exposed_pairs(layer: LogicalLayer, schema: PhysicalSchema) -> set[tuple[str, str]]:
    """레이어가 실제로 대변하는 (표, 컬럼) 짝. 필터 전용도 조건 걸 수 있으므로 산다."""
    known_tables = {t.name.lower() for t in schema.tables}
    pairs: set[tuple[str, str]] = set()
    for model in layer.models:
        for f in model.fields:
            key = (f.source.table.lower(), f.source.column.lower())
            if key[0] in known_tables:
                pairs.add(key)
    return pairs


def _cardinalities(
    schema: PhysicalSchema, cursor: Cursor, dialect: str
) -> tuple[dict[tuple[str, str], int], list[str]]:
    from tablefold.relate.validate import quoted

    out: dict[tuple[str, str], int] = {}
    unmeasured: list[str] = []
    for table in schema.tables:
        if not table.columns:
            continue
        if len(table.columns) > MAX_COLUMNS_PER_TABLE:
            unmeasured.append(table.name)
            continue
        if table.is_virtual:
            # 가상 표도 실행하면 센다 — 그 SQL 은 확장기가 답을 읽을 때
            # 실행하는 것과 같은 것이다. 방언이 다르면 sqlglot 으로 옮긴다.
            import sqlglot

            try:
                inner = sqlglot.parse_one(table.source_sql).sql(dialect=dialect)
            except Exception:  # noqa: BLE001
                unmeasured.append(table.name)
                continue
            from_ref = f"({inner}) AS _v"
        else:
            from_ref = quoted(table.name, dialect)
        exprs = ", ".join(
            f"COUNT(DISTINCT {quoted(c.name, dialect)}) AS c{i}"
            for i, c in enumerate(table.columns)
        )
        try:
            cursor.execute(f"SELECT {exprs} FROM {from_ref}")
            row = cursor.fetchone()
        except Exception:  # noqa: BLE001 — 드라이버별 예외를 다 아는 척하지 않는다
            unmeasured.append(table.name)
            continue
        if row is None:
            continue
        for i, column in enumerate(table.columns):
            value = row[i] if i < len(row) else None
            if value is not None:
                out[(table.name.lower(), column.name.lower())] = int(value)
    return out, unmeasured
