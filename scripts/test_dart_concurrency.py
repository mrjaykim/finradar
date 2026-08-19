"""DART company.json에 대한 병렬 호출 안전성/속도 검증용 소규모 테스트 스크립트.

공식 문서(개발가이드 상태코드표)에는 분당/초당 제한이 명시되어 있지 않고,
status=020("요청 제한을 초과하였습니다")이 "일반적으로 20,000건 이상의 요청"에서
발생한다는 일일 누적 한도만 언급되어 있다. 커뮤니티(dart-fss 문서)는 "분당 100회
이상 요청시 서비스가 제한될 수 있음"이라는 비공식 경험칙을 제시한다. 이 스크립트는
동시성 수준(3/5/10)별로 소규모(각 30건, 총 90건)를 실제로 호출해 status=020/429/
연결 오류가 발생하는지 관찰하고, 결과를 raw.dart_company_overview / mart.company에
반영한다 (백필 대상 쿼리는 backfill_company_industry_code.py와 동일하므로 정식
백필 실행 시 이미 처리된 corp_code로 집계되어 중복 작업이 되지 않는다).

전체 실행이 아니라 검증 목적이므로 CONCURRENCY_LEVELS x SAMPLE_PER_LEVEL 범위를
벗어나지 않는다. status=020/429 등 차단 신호가 관찰되면 해당 레벨에서 즉시 중단한다.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2

from backfill_company_industry_code import (
    fetch_company_overview,
    get_dart_api_key,
    save_result,
)
from collect_delisted_companies import get_db_config

CONCURRENCY_LEVELS = [1, 3, 5, 10]
SAMPLE_PER_LEVEL = 20
BLOCK_STATUS_CODES = {"020", "012", "010", "011"}


def get_sample_corp_codes(conn, n: int) -> list[str]:
    """company.json 재호출 안전성 검증용 표본. induty_code는 이미 전량 백필되어 있으므로
    (backfill_company_industry_code.py 완료 상태) is_active 상장사 중 무작위로 n건을 뽑는다.
    save_result가 동일 induty_code를 재기록하므로 멱등하며 mart 상태에 영향을 주지 않는다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT corp_code FROM mart.company WHERE is_active = true "
            "ORDER BY random() LIMIT %s",
            (n,),
        )
        return [row[0] for row in cur.fetchall()]


def run_level(conn, api_key: str, corp_codes: list[str], concurrency: int) -> dict:
    results = []
    blocked = False
    block_detail = None

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_to_corp = {
            pool.submit(fetch_company_overview, corp_code, api_key): corp_code
            for corp_code in corp_codes
        }
        for future in as_completed(future_to_corp):
            corp_code = future_to_corp[future]
            try:
                data = future.result()
            except Exception as e:
                results.append({"corp_code": corp_code, "error": str(e)})
                err_str = str(e)
                if "429" in err_str:
                    blocked = True
                    block_detail = f"HTTP 429 (corp_code={corp_code})"
                continue

            status = data.get("status")
            results.append({"corp_code": corp_code, "status": status})
            if status in BLOCK_STATUS_CODES:
                blocked = True
                block_detail = f"status={status} message={data.get('message')!r} (corp_code={corp_code})"

            try:
                save_result(conn, corp_code, data)
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"경고: {corp_code} DB 반영 실패: {e}", file=sys.stderr)

    elapsed = time.monotonic() - start
    ok_count = sum(1 for r in results if r.get("status") == "000")
    return {
        "concurrency": concurrency,
        "requested": len(corp_codes),
        "completed": len(results),
        "ok": ok_count,
        "elapsed_sec": elapsed,
        "req_per_sec": len(results) / elapsed if elapsed > 0 else 0,
        "blocked": blocked,
        "block_detail": block_detail,
        "status_counts": _count_statuses(results),
    }


def _count_statuses(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        key = r.get("status") or f"error:{r.get('error', '')[:40]}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = get_dart_api_key()
    conn = psycopg2.connect(**get_db_config())

    try:
        total_needed = len(CONCURRENCY_LEVELS) * SAMPLE_PER_LEVEL
        corp_codes = get_sample_corp_codes(conn, total_needed)
        if len(corp_codes) < total_needed:
            print(
                f"경고: is_active 상장사가 {len(corp_codes)}건뿐이라 요청한 {total_needed}건을 "
                f"채울 수 없습니다. 가능한 만큼만 테스트합니다.",
                file=sys.stderr,
            )

        offset = 0
        summary = []
        for level in CONCURRENCY_LEVELS:
            slice_codes = corp_codes[offset:offset + SAMPLE_PER_LEVEL]
            offset += SAMPLE_PER_LEVEL
            if not slice_codes:
                print(f"동시 {level}: 남은 대상 없음, 건너뜀")
                continue

            print(f"\n=== 동시 {level}개, {len(slice_codes)}건 테스트 시작 ===")
            result = run_level(conn, api_key, slice_codes, level)
            summary.append(result)
            print(
                f"동시 {level}: {result['completed']}/{result['requested']}건 완료, "
                f"성공(status=000) {result['ok']}건, "
                f"소요 {result['elapsed_sec']:.1f}초 "
                f"({result['req_per_sec']:.2f} req/s)"
            )
            print(f"상태 분포: {result['status_counts']}")

            if result["blocked"]:
                print(
                    f"*** 차단/한도초과 신호 감지: {result['block_detail']} — "
                    f"이후 레벨 테스트를 중단합니다 ***"
                )
                break

        print("\n=== 최종 요약 ===")
        for r in summary:
            print(
                f"동시 {r['concurrency']}: {r['req_per_sec']:.2f} req/s, "
                f"blocked={r['blocked']}"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
