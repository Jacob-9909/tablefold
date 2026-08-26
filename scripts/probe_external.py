"""외부 공개 데이터셋으로 폴드 파이프라인을 실측한다.

fixture/sqlite 합성 데이터만으로는 보이지 않는 것들이 여기서 드러난다 — 실제
명명 관례, 실제 값 분포에서 관계 탐사·스냅샷 판정·동치 통합이 어디까지 맞는지.

사용법: uv run python scripts/probe_external.py [db 경로 …]
경로를 비우면 fixtures/external/ 아래 전체를 돌린다.
"""

from __future__ import annotations

import sqlite3
import sys

# Rails 스키마 지원용 타입 표는 parse_rails_schema 위에 둔다.
from pathlib import Path

from tablefold.ir import ForeignKey, PhysicalColumn, PhysicalSchema, PhysicalTable
from tablefold.report import fidelity as fid
from tablefold.t2sql.prepare import prepare_for_questions

DEFAULT_GLOB = "fixtures/external/*.sqlite*"

# Rails schema.rb 의 타입 → 비교 가능한 타입 클래스로의 대응.
_RAILS_TYPES = {
    "bigint": "bigint",
    "integer": "integer",
    "string": "varchar",
    "text": "text",
    "datetime": "timestamp",
    "date": "date",
    "decimal": "numeric",
    "float": "double",
    "boolean": "boolean",
    "jsonb": "jsonb",
    "inet": "inet",
    "uuid": "uuid",
}


def parse_rails_schema(path: Path) -> PhysicalSchema:
    """``db/schema.rb`` 를 물리 스키마로 바꾼다.

    Rails 앱(Mastodon · GitLab · Redmine …)은 수백 개 표의 진짜 운영 스키마를
    선언형으로 공개한다 — 외부에서 표 백 개급을 구하려는 지금의 목적에 가장
    저렴한 원료다. 정규식으로 줄 단위 파싱한다. 완벽한 Ruby 파서가 아니라 필요한
    세 가지(create_table / 컬럼 줄 / add_foreign_key)만 읽는다.
    """
    import re

    text = path.read_text(encoding="utf-8")

    tables: list[PhysicalTable] = []

    def _singular(name: str) -> str:
        if name.endswith("ies"):
            return name[:-3] + "y"
        if name.endswith("ses") or name.endswith("xes"):
            return name[:-2]
        return name.rstrip("s") if name.endswith("s") else name

    fk_hints: list[tuple[str, str, str | None]] = []  # (표, 참조대상, 컬럼)
    current: dict | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("create_table"):
            m = re.match(
                r'create_table "([^"]+)"(, primary_key: (\[[^\]]*\]))?', stripped
            )
            if not m:
                current = None
                continue
            current = {
                "name": m.group(1),
                "pk": (
                    tuple(re.findall(r'"([^"]+)"', m.group(2)))
                    if m.group(2)
                    else ("id",)
                ),
                "columns": [],
            }
            continue
        if stripped == "end" and current:
            tables.append(
                PhysicalTable(
                    name=current["name"],
                    columns=tuple(current["columns"]),
                    primary_key=current["pk"],
                )
            )
            current = None
            continue
        if current is not None:
            m = re.match(r't\.(\w+)\s+"([^"]+)"', stripped)
            if m and m.group(1) in _RAILS_TYPES:
                nullable = "null: false" not in stripped
                current["columns"].append(
                    PhysicalColumn(
                        name=m.group(2),
                        type=_RAILS_TYPES[m.group(1)],
                        nullable=nullable,
                    )
                )
                continue
            m = re.match(r't\.index\s+"([^"]+)"', stripped)
            # 인덱스는 스키마 사실이 아니므로 건너뛴다.
            continue
        if stripped.startswith("add_foreign_key"):
            m = re.search(r'add_foreign_key "([^"]+)",\s*"([^"]+)"', stripped)
            if not m:
                continue
            from_table, ref = m.group(1), m.group(2)
            col_m = re.search(r'column:\s*"([^"]+)"', stripped)
            # 컬럼 생략 시 Rails 관례: 대상 표 단수형 + _id.
            column = col_m.group(1) if col_m else f"{_singular(ref)}_id"
            fk_hints.append((from_table, ref, column))

    # add_foreign_key 는 컬럼을 생략하면 "대상 단수형_id" 관례다.
    fks: list[ForeignKey] = []
    for from_table, ref, column in fk_hints:
        column = column or f"{_singular(ref)}_id"
        fks.append(
            ForeignKey(
                from_table=from_table,
                from_columns=(column,),
                to_table=ref,
                to_columns=("id",),
            )
        )

    known = {t.name.lower() for t in tables}
    fks = [
        fk
        for fk in fks
        if fk.from_table.lower() in known
        and fk.to_table.lower() in known
        and any(
            c.name.lower() == fk.from_columns[0].lower()
            for c in next(
                t for t in tables if t.name.lower() == fk.from_table.lower()
            ).columns
        )
    ]
    return PhysicalSchema(tables=tuple(tables), foreign_keys=tuple(fks))
    fks = [
        fk
        for fk in fks
        if fk.from_table.lower() in known
        and fk.to_table.lower() in known
        and any(
            c.name.lower() == fk.from_columns[0].lower()
            for c in next(
                t for t in tables if t.name.lower() == fk.from_table.lower()
            ).columns
        )
    ]
    return PhysicalSchema(tables=tuple(tables), foreign_keys=tuple(fks))


def introspect_sqlite(conn: sqlite3.Connection) -> PhysicalSchema:
    """PRAGMA 로 물리 스키마를 조립한다. DDL 방언 파싱을 피하려는 것이다.

    sqlite 의 ``PRAGMA table_info`` / ``foreign_key_list`` 가 주는 사실은
    카탈로그 질의가 주는 것과 같다 — 이름·타입·PK 순서·참조 대상.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    names = [r[0] for r in cur.fetchall()]

    tables: list[PhysicalTable] = []
    fks: list[ForeignKey] = []
    for name in names:
        info_rows = cur.execute(f"PRAGMA table_info('{name}')").fetchall()
        columns = tuple(
            PhysicalColumn(name=r[1], type=r[2] or "text", nullable=not r[3])
            for r in info_rows
        )
        pk_cols = tuple(
            r[1] for r in sorted((r for r in info_rows if r[5]), key=lambda r: r[5])
        )
        tables.append(PhysicalTable(name=name, columns=columns, primary_key=pk_cols))
        # 행 수 추정 — 스캔 상한 판정에 쓴다.
        count = conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0]
        tables[-1] = replace_row_estimate(tables[-1], count)

        for fk in cur.execute(f"PRAGMA foreign_key_list('{name}')").fetchall():
            fks.append(
                ForeignKey(
                    from_table=name,
                    from_columns=(fk[3],),
                    to_table=fk[2],
                    to_columns=(fk[4],),
                )
            )
    return PhysicalSchema(tables=tuple(tables), foreign_keys=tuple(fks))


def replace_row_estimate(table: PhysicalTable, count: int) -> PhysicalTable:
    from dataclasses import replace as _r

    return _r(table, row_estimate=count)


def probe(path: Path) -> None:
    import time

    print(f"\n{'=' * 64}\n{path.name}\n{'=' * 64}")
    t0 = time.perf_counter()
    if path.suffix == ".rb":
        schema = parse_rails_schema(path)
        conn = None
    else:
        conn = sqlite3.connect(path)
        schema = introspect_sqlite(conn)
    declared = len(schema.foreign_keys)
    total_rows = sum(t.row_estimate or 0 for t in schema.tables)
    print(
        f"원본: 표 {len(schema.tables)} · 선언 FK {declared} · "
        f"행 {total_rows:,} · 컬럼 {sum(len(t.columns) for t in schema.tables)} "
        f"(인트로스펙트 {time.perf_counter() - t0:.1f}s)"
    )

    t0 = time.perf_counter()
    prep = prepare_for_questions(
        schema,
        cursor=conn.cursor() if conn is not None else None,
        recover=True,
        consolidate_partitions=True,
        monthly_summaries=True,
        dedupe_equivalents=True,
        measure_cardinality=conn is not None,
    )
    result = prep.result
    fold_secs = time.perf_counter() - t0
    measured = fid.measure(result.layer, result.graph)

    print(prep.note)
    print(
        f"폴드: 모델 {len(result.layer.models)}"
        f" · 커버리지 {result.layer.coverage:.0%}"
        f" · 컬럼보존 {measured.column_retention:.0%}"
        f" · 조인흡수 {measured.join_absorption:.0%} ({fold_secs:.1f}s)"
    )
    summary_models = [m.name for m in result.layer.models if m.name.startswith("V_")]
    if summary_models:
        print("요약 모델:", ", ".join(summary_models))
    if measured.summaries_absorbed:
        print("흡수된 요약:", len(measured.summaries_absorbed))

    recovered = [
        fk
        for fk in result.schema.foreign_keys
        if (fk.from_table.lower(), tuple(c.lower() for c in fk.from_columns))
        not in {
            (f.from_table.lower(), tuple(c.lower() for c in f.from_columns))
            for f in schema.foreign_keys
        }
    ]
    if recovered:
        print(f"신규 복구 엣지 {len(recovered)} (스키마 전용 경로는 후보일 뿐 —")
        print("  데이터 검증 없이 믿지 말 것. 이름만 맞는 오탐이 섞인다):")
        for fk in recovered[:10]:
            src = fk.from_columns[0]
            print(f"  + {fk.from_table}.{src} → {fk.to_table} ({fk.confidence})")
    if result.layer.notes:
        for note in list(result.layer.notes)[:6]:
            print("  note:", note)
    if conn is not None:
        conn.close()


def main() -> None:
    paths = [Path(a) for a in sys.argv[1:]] or sorted(Path(".").glob(DEFAULT_GLOB))
    for path in paths:
        if path.exists():
            probe(path)
        else:
            print(f"없는 경로: {path}")


if __name__ == "__main__":
    main()
