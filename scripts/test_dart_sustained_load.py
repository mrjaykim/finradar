"""DART company.json 동시 5 지속 호출 안정성 테스트 (500~1,000건 규모).

test_dart_concurrency.py의 짧은 버스트 테스트(레벨당 20건, 총 80건)에서는 동시 10까지도
020/429 없이 통과했지만, 그건 순간 부하일 뿐이라 지속 부하에서의 안정성을 보증하지
않는다. 이 스크립트는 동시성을 5로 고정하고 표본을 훨씬 크게(기본 1,000건) 잡아 몇 분간
연속 호출했을 때도 020(요청 제한)이 뜨지 않는지, 시간이 지나며 처리량이 떨어지지
않는지(WINDOW_SIZE건 단위 구간별 req/s)를 관찰한다.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2

from backfill_company_industry_code import fetch_company_overview, get_dart_api_key, save_result
from collect_delisted_companies import get_db_config

CONCURRENCY = 5
SAMPLE_SIZE = 1000
WINDOW_SIZE = 100
BATCH_COMMIT_SIZE = 50
BLOCK_STATUS_CODES = {"020", "012", "010", "011"}


def get_sample_corp_codes(conn, n: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT corp_code FROM mart.company WHERE is_active = true ORDER BY random() LIMIT %s",
            (n,),
        )
        return [row[0] for row in cur.fetchall()]


def main(sample_size: int = SAMPLE_SIZE):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = get_dart_api_key()
    conn = psycopg2.connect(**get_db_config())

    try:
        corp_codes = get_sample_corp_codes(conn, sample_size)
        total = len(corp_codes)
        print(f"동시 {CONCURRENCY}개, 총 {total}건 지속 호출 테스트 시작")

        start = time.monotonic()
        window_start = start
        completed = 0
        ok = 0
        status_counts: dict[str, int] = {}
        blocked = False
        block_detail = None

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            future_to_corp = {
                pool.submit(fetch_company_overview, corp_code, api_key): corp_code
                for corp_code in corp_codes
            }
            for future in as_completed(future_to_corp):
                corp_code = future_to_corp[future]
                completed += 1
                try:
                    data = future.result()
                except Exception as e:
                    key = f"error:{str(e)[:40]}"
                    status_counts[key] = status_counts.get(key, 0) + 1
                    if "429" in str(e):
                        blocked = True
                        block_detail = f"HTTP 429 (corp_code={corp_code})"
                else:
                    status = data.get("status")
                    status_counts[status] = status_counts.get(status, 0) + 1
                    if status == "000":
                        ok += 1
                    if status in BLOCK_STATUS_CODES:
                        blocked = True
                        block_detail = f"status={status} message={data.get('message')!r} (corp_code={corp_code})"

                    try:
                        save_result(conn, corp_code, data)
                    except Exception as e:
                        conn.rollback()
                        print(f"경고: {corp_code} DB 반영 실패: {e}", file=sys.stderr)

                if completed % BATCH_COMMIT_SIZE == 0:
                    conn.commit()

                if completed % WINDOW_SIZE == 0 or completed == total:
                    now = time.monotonic()
                    window_elapsed = now - window_start
                    total_elapsed = now - start
                    window_rate = WINDOW_SIZE / window_elapsed if window_elapsed > 0 else 0
                    overall_rate = completed / total_elapsed if total_elapsed > 0 else 0
                    print(
                        f"진행 {completed}/{total} | 구간 {window_rate:.2f} req/s | "
                        f"누적 {overall_rate:.2f} req/s | 경과 {total_elapsed:.1f}초"
                    )
                    window_start = now

                if blocked:
                    print(
                        f"*** 차단/한도초과 신호 감지: {block_detail} — 남은 요청을 취소하고 중단합니다 ***",
                        file=sys.stderr,
                    )
                    for f in future_to_corp:
                        f.cancel()
                    break

        conn.commit()
        elapsed = time.monotonic() - start
        print("\n=== 완료 ===")
        print(
            f"{completed}/{total}건 처리, 성공(status=000) {ok}건, "
            f"소요 {elapsed:.1f}초 ({completed / elapsed:.2f} req/s)"
        )
        print(f"상태 분포: {status_counts}")
        print(f"차단 감지: {blocked}" + (f" ({block_detail})" if blocked else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    size_arg = int(sys.argv[1]) if len(sys.argv) > 1 else SAMPLE_SIZE
    main(sample_size=size_arg)
