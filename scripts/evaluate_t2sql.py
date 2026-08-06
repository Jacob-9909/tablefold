"""NL2SQL 골드셋으로 레이어와 엔진을 잰다.

두 가지를 재고, **둘은 서로 다른 것을 재므로 따로 낸다**:

* ``coverage`` — 정답이 읽는 물리 표들이 한 모델 안에 함께 있는가. LLM 없이
  돈다. 여기서 실패하면 어떤 모델도 그 질문에 답할 수 없다 — 프롬프트를 고쳐서
  될 일이 아니라 레이어를 고쳐야 하는 일이다.
* ``generation`` — 실제로 SQL 을 만들어 확장까지 통과하는가. LLM 키가 필요하다.

앞의 축이 실패하는 케이스를 뒤의 축에서 세면, 엔진 탓이 아닌 실패를 엔진 점수에
넣게 된다. 그래서 생성 결과는 커버되는 케이스에 대해서만 비율을 낸다.

사용법:

```
python scripts/evaluate_t2sql.py                       # 커버리지만
python scripts/evaluate_t2sql.py --generate --limit 10 # 생성까지 (키 필요)
```
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from tablefold.read.ddl import DDLIntrospector
from tablefold.report import fidelity as fid
from tablefold.report.prompt import render_text
from tablefold.t2sql import (
    NL2SQL_EXAMPLES,
    GenerationError,
    ProviderUnavailable,
    TextToSQLEngine,
    Usage,
    default_completer,
    fold_star_schema,
    load_goldset,
    recover_relationships,
    schema_gap,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXCEL = ROOT / "20251104_NL2SQL_메뉴별컨텐츠정리.xlsx"
DEFAULT_DDL = ROOT / "fixtures" / "enterprise_bi.sql"


def main() -> int:
    args = _parse_args()

    if not args.ddl.exists():
        print(f"error: no such DDL: {args.ddl}", file=sys.stderr)
        return 2
    if not args.excel.exists():
        print(f"error: no such goldset: {args.excel}", file=sys.stderr)
        return 2

    schema = DDLIntrospector.from_path(args.ddl).introspect()
    declared = len(schema.foreign_keys)
    if args.recover:
        schema = recover_relationships(schema)
    result = fold_star_schema(schema)
    print(
        f'외래 키: 선언 {declared} + 복구 '
        f'{len(schema.foreign_keys) - declared}'
    )
    cases = load_goldset(args.excel)

    _report_layer(result)
    _report_schema_gap(cases, schema)
    covered, uncovered = _report_coverage(result, cases)

    if not args.generate:
        print("\n생성 평가는 --generate 로 켠다 (LLM 키 필요).")
        return 0

    try:
        completer = default_completer(model=args.model)
    except ProviderUnavailable as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2

    _report_generation(result, covered, completer, limit=args.limit)
    if uncovered:
        print(
            f"\n커버되지 않은 {len(uncovered)}건은 생성 평가에서 제외했다 — "
            "레이어에 답이 없는 질문이라 엔진 점수가 아니다."
        )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--excel", type=Path, default=DEFAULT_EXCEL)
    parser.add_argument("--ddl", type=Path, default=DEFAULT_DDL)
    parser.add_argument("--generate", action="store_true", help="LLM 으로 SQL 생성")
    parser.add_argument("--limit", type=int, default=10, help="생성 평가할 건수")
    parser.add_argument("--model", default=None, help="LLM 모델 이름")
    parser.add_argument(
        "--no-recover",
        dest="recover",
        action="store_false",
        help="선언된 외래 키만 쓴다 (기본은 기본 키로 관계 복구)",
    )
    return parser.parse_args()


def _report_layer(result) -> None:
    layer = result.layer
    text = render_text(layer)
    print("=" * 72)
    print(
        f"레이어: {layer.source_table_count}표 → {len(layer.models)}모델, "
        f"{layer.field_count}필드, 프롬프트 {len(text):,}자 (~{len(text)//4:,}토큰)"
    )
    print("=" * 72)
    for model in layer.models:
        filters = sum(1 for f in model.fields if f.filter_only)
        print(
            f"  {model.name:<20}{len(model.fields):>4} 필드"
            f"(필터 {filters:>3})  흡수 {len(model.absorbed_tables) + 1}표"
        )
    print()
    print(fid.render_report(fid.measure(layer, result.graph)))


def _report_schema_gap(cases, schema) -> None:
    """정답이 읽는데 스키마에 없는 컬럼. 폴드로 고칠 수 없는 실패의 원인이다."""
    gap = schema_gap(cases, schema)
    if not gap:
        return
    print("\n" + "=" * 72)
    print("스키마 결손 — 정답 SQL 이 읽는데 DDL 에 없는 컬럼")
    print("=" * 72)
    print("  폴드 설정으로 고칠 수 없다. 이 표들의 질문은 원리적으로 답이 없다.")
    for table, columns in gap.items():
        print(f"    {table:<20}{', '.join(sorted(c.upper() for c in columns))}")


def _report_coverage(result, cases) -> tuple[list, list]:
    """정답이 읽는 표들이 한 모델 안에 함께 있는가."""
    members = {
        model.name: {model.base_table.lower()}
        | {t.lower() for t in model.absorbed_tables}
        for model in result.layer.models
    }
    physical = {t.name.lower() for t in result.schema.tables}

    covered: list = []
    uncovered: list = []
    by_subject: Counter[str] = Counter()
    subject_total: Counter[str] = Counter()

    print("\n" + "=" * 72)
    print("커버리지 — 정답이 쓰는 표가 한 모델에 함께 있는가")
    print("=" * 72)

    for case in cases:
        # 정답 SQL 에서 뽑은 이름 중 이 스키마에 실제로 있는 것만 본다.
        # 서브쿼리 별칭이나 다른 스키마의 표가 섞여 들어오기 때문이다.
        needed = case.gold_tables & physical
        subject_total[case.subject] += 1
        if not needed:
            uncovered.append((case, "정답 SQL 없음"))
            continue

        holders = [name for name, group in members.items() if needed <= group]
        if holders:
            covered.append((case, holders))
            by_subject[case.subject] += 1
        else:
            uncovered.append((case, ", ".join(sorted(needed))))

    total = len(cases)
    print(f"  답할 수 있음  {len(covered):>3}/{total}")
    for subject in sorted(subject_total):
        print(
            f"    {subject:<4}{by_subject[subject]:>3}/{subject_total[subject]}"
        )
    if uncovered:
        print(f"\n  답할 수 없는 {len(uncovered)}건:")
        for case, why in uncovered[:12]:
            print(f"    {case.case_id:<10}{why}")
        if len(uncovered) > 12:
            print(f"    … 외 {len(uncovered) - 12}건")
    return covered, uncovered


def _report_generation(result, covered, completer, *, limit: int) -> None:
    engine = TextToSQLEngine(result, completer=completer, examples=NL2SQL_EXAMPLES)
    batch = covered[:limit]

    print("\n" + "=" * 72)
    print(f"생성 — {len(batch)}건")
    print("=" * 72)

    passed = 0
    repairs = 0
    right_model = 0
    calls = 0
    fell_back = 0
    usage = Usage()
    for index, (case, holders) in enumerate(batch, 1):
        print(f"\n[{index}/{len(batch)}] {case.case_id}  {case.asked[:60]}")
        try:
            generated = engine.generate(case.asked)
        except GenerationError as exc:
            print(f"  ✗ {exc}")
            for attempt in exc.attempts:
                print(f"      - {attempt.error}")
            continue

        passed += 1
        repairs += generated.repairs
        calls += generated.calls
        fell_back += generated.fell_back
        usage = usage + generated.usage
        used = generated.models_used[0] if generated.models_used else ""
        matched = used in holders
        right_model += matched

        print(
            f"  ✓ 모델 {used}{'' if matched else ' (정답 표 조합과 다름)'}"
            f" · 조인 {generated.joins_emitted}/{generated.joins_available}"
            f" · 호출 {generated.calls}회 · 수리 {generated.repairs}회"
            f"{' · 전체 레이어로 후퇴' if generated.fell_back else ''}"
        )
        print("    " + generated.logical_sql.replace("\n", "\n    "))

    if not batch:
        return
    print("\n" + "-" * 72)
    print(f"  확장 통과   {passed}/{len(batch)}")
    print(f"  기대 모델   {right_model}/{len(batch)}")
    print(f"  LLM 호출    {calls}회 (질문당 {calls / len(batch):.1f})")
    print(f"  수리 총계   {repairs}회 · 후퇴 {fell_back}건")
    print(
        f"  토큰        입력 {usage.input_tokens:,} + 캐시 {usage.cached_tokens:,}"
        f" / 출력 {usage.output_tokens:,}"
    )
    # 캐시가 0 이면 접두사가 매번 달라지거나 최소 길이 아래라는 뜻이다.
    print(f"  캐시 적중   {usage.cache_hit_rate * 100:.1f}% (입력 토큰 기준)")


if __name__ == "__main__":
    raise SystemExit(main())
