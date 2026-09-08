"""접은 쪽과 접지 않은 쪽을, 같은 질문으로 나란히 돌린다.

``run_goldset_value_match_test.py`` 는 tablefold 가 정답 SQL 과 얼마나 같은
값을 내는지 잰다. 그것만으로는 **접기가 원인이었는지** 알 수 없다 — 이 프로젝트의
주장이 "실패 원인은 모델이 아니라 스키마 컨텍스트"이므로, 접지 않았을 때의 같은
숫자가 있어야 주장이 증명된다.

두 팔은 다음을 공유한다. 달라지는 것은 **LLM 이 스키마를 어떤 모양으로 보는가**
하나다.

* 같은 질문(``PRECISE_QUESTION_MAP``)
* 같은 라이브 데이터베이스와 같은 정답 SQL
* 같은 값 비교 규칙(``evaluate_strict_match``)
* 같은 재시도 상한 — 접힌 쪽만 자기수정을 가지면 이긴 원인이 갈리지 않는다

    접은 팔   : 원본 스키마 → fold → 논리 SQL → expand → 물리 SQL
    안 접은 팔 : 원본 DDL → 물리 SQL (tablefold 를 지나치지 않는다)

실행에는 라이브 MSSQL 과 LLM 자격 증명이 필요하다.

    uv run python scripts/run_baseline_comparison.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_goldset_value_match_test import (  # noqa: E402
    PRECISE_QUESTION_MAP,
    evaluate_strict_match,
    extract_gold_sqls,
    sanitize_for_mssql,
)

from demo import live  # noqa: E402
from tablefold.report.baseline import generate_without_fold  # noqa: E402
from tablefold.t2sql import (  # noqa: E402
    TextToSQLEngine,
    load_goldset,
    prepare_for_questions,
)
from tablefold.t2sql.provider import default_completer  # noqa: E402

DIALECT = "tsql"
MAX_ATTEMPTS = 3

# 값이 맞은 것으로 세는 등급. 실행만 된 것은 정답이 아니다.
CORRECT = {"EXACT_VALUE_MATCH", "CLOSE_VALUE_MATCH"}


@dataclass
class ArmResult:
    case_id: str
    question: str
    status: str
    sql: str
    ms: float


def _grade(gold_res: dict, sql: str) -> tuple[str, dict]:
    """생성된 SQL 을 실행해 정답과 값으로 견준다."""
    if not sql:
        return "FAIL", {}
    gen_res = live.execute_query(sql)
    status, _notes, _gold_sum, _gen_sum = evaluate_strict_match(gold_res, gen_res)
    return status, gen_res


def run() -> None:
    schema, meta = live.load()
    db = meta.get("database", "NL2SQL")
    print(f"[schema] {db} — 물리 테이블 {len(schema.tables)}개")

    prep = prepare_for_questions(schema)
    engine = TextToSQLEngine(
        fold_result=prep.result, dialect=DIALECT, max_attempts=MAX_ATTEMPTS
    )
    print(f"[folded]   와이드 모델 {len(prep.result.layer.models)}개")

    completer = default_completer()
    print("[baseline] 원본 DDL 을 그대로 프롬프트에 넣는다\n")

    cases = load_goldset("20251104_NL2SQL_메뉴별컨텐츠정리.xlsx")
    gold_sqls = extract_gold_sqls("20251104_NL2SQL_메뉴별컨텐츠정리.xlsx")

    folded: list[ArmResult] = []
    unfolded: list[ArmResult] = []

    for i, case in enumerate(cases, 1):
        question = PRECISE_QUESTION_MAP.get(
            case.case_id, case.concrete_question or case.question
        )
        gold_sql = sanitize_for_mssql(gold_sqls.get(case.case_id, ""))
        gold_res = live.execute_query(gold_sql) if gold_sql else {}

        print(f"[{i:02d}/{len(cases)}] {case.case_id}: {question}")

        t0 = time.time()
        try:
            out = engine.generate(question)
            status, _ = _grade(gold_res, out.physical_sql.strip())
            sql = out.physical_sql.strip()
        except Exception as exc:  # noqa: BLE001 — 실패도 결과다
            status, sql = "FAIL", f"-- {exc}"
        folded.append(
            ArmResult(case.case_id, question, status, sql, (time.time() - t0) * 1000)
        )
        print(f"    folded    {status}")

        t0 = time.time()
        try:
            base = generate_without_fold(
                question,
                schema,
                completer=completer,
                dialect=DIALECT,
                max_attempts=MAX_ATTEMPTS,
                executor=lambda s: live.execute_query(s),
            )
            status, _ = _grade(gold_res, base.sql or "")
            sql = base.sql or f"-- {base.error}"
        except Exception as exc:  # noqa: BLE001 — 실패도 결과다
            status, sql = "FAIL", f"-- {exc}"
        unfolded.append(
            ArmResult(case.case_id, question, status, sql, (time.time() - t0) * 1000)
        )
        print(f"    baseline  {status}")

    _report(folded, unfolded)


def _rate(arm: list[ArmResult]) -> float:
    return 100.0 * sum(1 for r in arm if r.status in CORRECT) / max(len(arm), 1)


def _report(folded: list[ArmResult], unfolded: list[ArmResult]) -> None:
    print("\n" + "=" * 68)
    print("값 일치율 (EXACT + CLOSE 를 정답으로 센다)")
    print("=" * 68)
    f, u = _rate(folded), _rate(unfolded)
    f_hit = sum(1 for r in folded if r.status in CORRECT)
    u_hit = sum(1 for r in unfolded if r.status in CORRECT)
    print(f"  접은 쪽    {f:5.1f}%  ({f_hit}/{len(folded)})")
    print(f"  안 접은 쪽 {u:5.1f}%  ({u_hit}/{len(unfolded)})")
    print(f"  차이       {f - u:+5.1f}%p")

    print("\n등급 분포")
    for name, arm in (("folded", folded), ("baseline", unfolded)):
        counts: dict[str, int] = {}
        for r in arm:
            counts[r.status] = counts.get(r.status, 0) + 1
        rendered = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {name:9s} {rendered}")

    flipped = [
        (a.case_id, a.status, b.status)
        for a, b in zip(folded, unfolded, strict=True)
        if (a.status in CORRECT) != (b.status in CORRECT)
    ]
    if flipped:
        print("\n한쪽만 맞힌 문항 — 차이가 어디서 왔는지 여기를 읽는다")
        for case_id, fs, us in flipped:
            winner = "folded" if fs in CORRECT else "baseline"
            print(f"  {case_id:10s} folded={fs:18s} baseline={us:18s} → {winner}")


if __name__ == "__main__":
    run()
