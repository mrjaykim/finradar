"""부실기업(756건 후보)과 정상기업(is_active=true 2,904건)의 재무제표를
DART fnlttSinglAcntAll.json에서 수집해 raw.dart_financial_statement / mart.financial_statement에
적재한다 (get_financial_statements.py의 단건 조회 로직을 대량 백필용으로 일반화).

분기 산정 규칙(2026-08-19 세션 합의, ADR-010 후속):
  - 부실기업: delisted_on 기준 과거 최대 12분기, 2015-01-01 하한
  - 정상기업: 오늘 기준 과거 최대 12분기, 2015-01-01 하한
  - 기본은 fs_div=CFS(연결재무제표). 2026-10-10 세션에서 OFS 폴백 모드(--ofs-fallback)를
    추가했다: CFS를 전부(해당 기업의 모든 시도) 013(데이터없음)으로 끝낸 "0분기 기업"
    924개(부실 250 + 정상 674)만 대상으로 fs_div=OFS로 재조회한다. CFS 중 일부 연도만
    013인 "부분 결측" 기업(540개)은 이미 데이터가 일부 확보돼 있어 이번 폴백 범위 밖이다.

하루 호출량은 DAILY_CALL_TARGET(기본 30,000, 실제 여유 한도보다 넉넉히 낮게 잡은 값)으로
캡을 걸어 미처리 목록의 앞부분만 잘라서 처리한다. 이미 raw.dart_financial_statement에 기록된
(corp_code, bsns_year, reprt_code, fs_div) 조합은 건너뛰므로, 다음날 그냥 재실행하면 남은
부분부터 자동으로 이어진다 - 별도 상태 파일이 필요 없다.

020(요청 제한)/429 등 차단 신호가 관측되면 목표치 도달 여부와 무관하게 즉시 중단한다
(하루 목표 캡과는 별개의 이중 안전장치).
"""
import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import psycopg2
import requests
from psycopg2.extras import Json, execute_values

from backfill_company_industry_code import get_dart_api_key
from collect_delisted_companies import get_db_config

FS_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
FS_DIV = "CFS"
FLOOR = date(2015, 1, 1)
MAX_QUARTERS = 12
CONCURRENCY = 5
DAILY_CALL_TARGET = 30000
BATCH_COMMIT_SIZE = 50
PROGRESS_EVERY = 200
BLOCK_STATUS_CODES = {"020", "012", "010", "011"}
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,  # requests.exceptions.SSLError를 포함(ConnectionError의 서브클래스)
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
MAX_FETCH_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # 1s, 2s, 4s

REPRT_CODE_BY_Q = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}  # 1분기/반기/3분기/사업보고서
QUARTER_BY_REPRT_CODE = {v: k for k, v in REPRT_CODE_BY_Q.items()}

RAW_COLUMNS = [
    "rcept_no", "corp_code", "bsns_year", "reprt_code", "fs_div", "fs_nm",
    "sj_div", "sj_nm", "account_id", "account_nm", "account_detail",
    "thstrm_nm", "thstrm_amount", "frmtrm_nm", "frmtrm_amount",
    "bfefrmtrm_nm", "bfefrmtrm_amount", "ord", "currency",
]
UNASSIGNED_ACCOUNT_ID = "UNASSIGNED"


def quarter_of(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def quarter_start(year: int, q: int) -> date:
    return date(year, (q - 1) * 3 + 1, 1)


def prev_quarter(year: int, q: int) -> tuple[int, int]:
    if q == 1:
        return year - 1, 4
    return year, q - 1


def quarters_before(basis: date, floor: date = FLOOR, max_q: int = MAX_QUARTERS) -> list[tuple[int, int]]:
    """basis가 속한 분기 직전부터 역순으로 max_q개, floor 이전 분기는 제외."""
    year, q = quarter_of(basis)
    out = []
    for _ in range(max_q):
        year, q = prev_quarter(year, q)
        if quarter_start(year, q) < floor:
            break
        out.append((year, q))
    return out


def build_worklist(conn) -> list[tuple[str, int, str]]:
    """(corp_code, bsns_year, reprt_code) 튜플 목록. 부실기업 전체를 먼저, 정상기업을 다음에 둔다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.corp_code, d.delisted_on
            FROM mart.delisted_company d
            JOIN mart.company c ON d.stock_code = c.stock_code
            WHERE d.is_financial_distress = true AND d.delisted_on >= '2010-01-01'
            ORDER BY d.delisted_on, c.corp_code
            """
        )
        distress_rows = cur.fetchall()

        cur.execute("SELECT corp_code FROM mart.company WHERE is_active = true ORDER BY corp_code")
        active_rows = [row[0] for row in cur.fetchall()]

    today = date.today()
    worklist = []
    for corp_code, delisted_on in distress_rows:
        for year, q in quarters_before(delisted_on):
            worklist.append((corp_code, year, REPRT_CODE_BY_Q[q]))
    for corp_code in active_rows:
        for year, q in quarters_before(today):
            worklist.append((corp_code, year, REPRT_CODE_BY_Q[q]))
    return worklist


def build_ofs_fallback_worklist(conn) -> list[tuple[str, int, str]]:
    """CFS를 분기 전부(해당 기업의 모든 CFS 시도) 013(데이터없음)으로 끝낸 "0분기 기업"의
    (corp_code, bsns_year, reprt_code) 조합만 모은다.

    CFS 일부 연도만 013인 "부분 결측" 기업은 제외한다 - 이미 일부 분기는 CFS로 확보돼
    있어 이번 폴백(2026-10-10 세션에서 합의한 924개 "0분기 기업" 복구)의 범위 밖이다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH cfs_attempts AS (
                SELECT corp_code, bsns_year, reprt_code,
                       (source_payload->>'status' = '013') AS is_013
                FROM raw.dart_financial_statement WHERE fs_div = 'CFS'
            ),
            per_company AS (
                SELECT corp_code, count(*) AS total_attempts, count(*) FILTER (WHERE is_013) AS n_013
                FROM cfs_attempts GROUP BY corp_code
            ),
            fully_missing AS (
                SELECT corp_code FROM per_company WHERE n_013 = total_attempts AND total_attempts > 0
            )
            SELECT DISTINCT r.corp_code, r.bsns_year, r.reprt_code
            FROM raw.dart_financial_statement r
            WHERE r.fs_div = 'CFS' AND r.source_payload->>'status' = '013'
              AND r.corp_code IN (SELECT corp_code FROM fully_missing)
            """
        )
        return [(row[0], int(row[1]), row[2]) for row in cur.fetchall()]


def classify_breakdown(conn, corp_codes: list[str]) -> tuple[int, int]:
    """corp_code 목록을 부실/정상 기업 수로 분류한다 (--dry-run 출력용)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (WHERE d.corp_code IS NOT NULL) AS distress,
                count(*) FILTER (WHERE d.corp_code IS NULL AND m.is_active) AS normal
            FROM unnest(%s) AS cc(corp_code)
            LEFT JOIN (
                SELECT DISTINCT c.corp_code
                FROM mart.delisted_company d
                JOIN mart.company c ON d.stock_code = c.stock_code
                WHERE d.is_financial_distress = true AND d.delisted_on >= '2010-01-01'
            ) d ON d.corp_code = cc.corp_code
            LEFT JOIN mart.company m ON m.corp_code = cc.corp_code
            """,
            (corp_codes,),
        )
        return cur.fetchone()


def filter_pending(conn, worklist: list[tuple[str, int, str]], fs_div: str = FS_DIV) -> list[tuple[str, int, str]]:
    """이미 raw.dart_financial_statement에 기록된(=이미 시도한) 조합을 제외한다.

    데이터가 없던 호출(013 등)도 플레이스홀더 1행으로 기록되므로, 성공/실패 여부와 무관하게
    "이미 호출을 시도했는가"만으로 재시도 여부를 가른다. fs_div별로 독립적으로 추적한다
    (CFS 013으로 끝난 조합도 OFS는 아직 안 시도한 것으로 취급).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT corp_code, bsns_year, reprt_code FROM raw.dart_financial_statement WHERE fs_div = %s",
            (fs_div,),
        )
        done = {(row[0], str(row[1]), row[2]) for row in cur.fetchall()}
    return [t for t in worklist if (t[0], str(t[1]), t[2]) not in done]


def fetch_financial_statement(corp_code: str, bsns_year: int, reprt_code: str, api_key: str, fs_div: str = FS_DIV) -> dict:
    """네트워크성 예외(SSLError/ConnectionError/Timeout 등)는 지수 백오프로 최대
    MAX_FETCH_RETRIES회 재시도한다. 재시도를 다 써도 실패하면 마지막 예외를 그대로 올려
    호출부(main)가 해당 항목만 실패 처리하고 배치 나머지는 계속 진행하게 한다."""
    for attempt in range(MAX_FETCH_RETRIES + 1):
        try:
            resp = requests.get(
                FS_URL,
                params={
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bsns_year": bsns_year,
                    "reprt_code": reprt_code,
                    "fs_div": fs_div,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except RETRYABLE_EXCEPTIONS as e:
            if attempt == MAX_FETCH_RETRIES:
                raise
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            print(
                f"경고: {corp_code} {bsns_year} {reprt_code} 네트워크 오류(재시도 {attempt + 1}/{MAX_FETCH_RETRIES}, {wait:.0f}초 대기): {e}",
                file=sys.stderr,
            )
            time.sleep(wait)


def clean_account_id(account_id) -> str:
    """DART 응답의 account_id 결측/비정상 값을 UNASSIGNED로 치환한다 (docs/etl_notes.md 참고)."""
    if account_id is None:
        return UNASSIGNED_ACCOUNT_ID
    value = account_id.strip()
    if value in ("", "-"):
        return UNASSIGNED_ACCOUNT_ID
    return value


def parse_amount(value):
    if value is None:
        return None
    value = value.strip().replace(",", "")
    if value in ("", "-"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_ord(value):
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def insert_raw(conn, corp_code: str, bsns_year: int, reprt_code: str, data: dict, fs_div: str = FS_DIV) -> list[dict]:
    """raw 레이어에 원본을 적재한다. 데이터가 없으면(013 등) 재조회 방지용 플레이스홀더 1행을 남긴다."""
    rows = data.get("list") or []
    if rows:
        values = [
            tuple(fs_div if col == "fs_div" else row.get(col) for col in RAW_COLUMNS) + (Json(row),)
            for row in rows
        ]
    else:
        placeholder = {col: None for col in RAW_COLUMNS}
        placeholder.update(corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=fs_div)
        values = [tuple(placeholder[col] for col in RAW_COLUMNS) + (Json(data),)]

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO raw.dart_financial_statement ({', '.join(RAW_COLUMNS)}, source_payload) VALUES %s",
            values,
        )
    return rows


def upsert_filing(conn, rcept_no: str, corp_code: str, bsns_year: int, reprt_code: str):
    """mart.financial_statement.rcept_no가 mart.filing을 참조하는 FK라 먼저 채워야 한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mart.filing (rcept_no, corp_code, bsns_year, reprt_code)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (rcept_no) DO UPDATE SET
                corp_code = EXCLUDED.corp_code,
                bsns_year = EXCLUDED.bsns_year,
                reprt_code = EXCLUDED.reprt_code
            """,
            (rcept_no, corp_code, bsns_year, reprt_code),
        )


def upsert_mart(conn, rows: list[dict], corp_code: str, bsns_year: int, reprt_code: str, fs_div: str = FS_DIV):
    if not rows:
        return
    upsert_filing(conn, rows[0]["rcept_no"], corp_code, bsns_year, reprt_code)
    quarter = QUARTER_BY_REPRT_CODE[reprt_code]
    values = [
        (
            corp_code, bsns_year, reprt_code, quarter, fs_div,
            row.get("sj_div"), clean_account_id(row.get("account_id")), row.get("account_nm"),
            row.get("account_detail"), parse_amount(row.get("thstrm_amount")), row.get("currency"),
            parse_ord(row.get("ord")), row.get("rcept_no"),
        )
        for row in rows
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO mart.financial_statement (
                corp_code, bsns_year, reprt_code, quarter, fs_div, sj_div,
                account_id, account_nm, account_detail, metric_amount, currency, ord, rcept_no
            ) VALUES %s
            ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_detail, ord)
            DO UPDATE SET
                account_nm = EXCLUDED.account_nm,
                metric_amount = EXCLUDED.metric_amount,
                currency = EXCLUDED.currency,
                rcept_no = EXCLUDED.rcept_no
            """,
            values,
        )


def main(daily_target: int = DAILY_CALL_TARGET, ofs_fallback: bool = False, dry_run: bool = False):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    fs_div = "OFS" if ofs_fallback else FS_DIV
    conn = psycopg2.connect(**get_db_config())

    try:
        print(f"작업 목록 생성 중... (모드: {'OFS 폴백' if ofs_fallback else 'CFS'})")
        full_worklist = build_ofs_fallback_worklist(conn) if ofs_fallback else build_worklist(conn)
        pending = filter_pending(conn, full_worklist, fs_div=fs_div)
        print(f"전체 대상 {len(full_worklist)}건 중 미처리 {len(pending)}건")

        if not pending:
            print("처리할 항목이 없습니다.")
            return

        todo = pending[:daily_target]
        print(f"오늘 목표(최대 {daily_target}건) 내에서 {len(todo)}건 처리 시작\n")

        if dry_run:
            distress, normal = classify_breakdown(conn, list({t[0] for t in todo}))
            print("*** --dry-run: API 호출 없이 작업목록만 확인 ***")
            print(f"처리 대상 조합: {len(todo)}건")
            print(f"기업 구성(조합 기준 corp_code 중복 포함 아님, distinct): 부실 {distress} / 정상 {normal}")
            return

        api_key = get_dart_api_key()

        lock = threading.Lock()
        completed = 0
        ok = 0
        no_data = 0
        failed = 0
        blocked = False
        block_detail = None
        start = time.monotonic()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            future_to_task = {
                pool.submit(fetch_financial_statement, corp_code, bsns_year, reprt_code, api_key, fs_div): (corp_code, bsns_year, reprt_code)
                for corp_code, bsns_year, reprt_code in todo
            }
            for future in as_completed(future_to_task):
                corp_code, bsns_year, reprt_code = future_to_task[future]
                with lock:
                    completed += 1
                    n = completed

                try:
                    data = future.result()
                except Exception as e:
                    print(f"경고: {corp_code} {bsns_year} {reprt_code} 처리 중 오류(재시도 소진): {e}", file=sys.stderr)
                    with lock:
                        failed += 1
                    if "429" in str(e):
                        blocked = True
                        block_detail = f"HTTP 429 (corp_code={corp_code})"
                else:
                    status = data.get("status")
                    if status in BLOCK_STATUS_CODES:
                        blocked = True
                        block_detail = f"status={status} message={data.get('message')!r} (corp_code={corp_code})"

                    try:
                        with conn.cursor() as cur:
                            cur.execute("SAVEPOINT item_sp")
                        rows = insert_raw(conn, corp_code, bsns_year, reprt_code, data, fs_div)
                        upsert_mart(conn, rows, corp_code, bsns_year, reprt_code, fs_div)
                    except Exception as e:
                        # 이 항목만 되돌리고, 같은 트랜잭션의 이전 성공 건들은 그대로 유지한다
                        # (한 건의 FK/제약 위반이 커밋 전 배치 전체를 날리는 것을 방지)
                        with conn.cursor() as cur:
                            cur.execute("ROLLBACK TO SAVEPOINT item_sp")
                        print(f"경고: {corp_code} {bsns_year} {reprt_code} DB 반영 실패: {e}", file=sys.stderr)
                    else:
                        with conn.cursor() as cur:
                            cur.execute("RELEASE SAVEPOINT item_sp")
                        if rows:
                            ok += 1
                        else:
                            no_data += 1

                if n % BATCH_COMMIT_SIZE == 0:
                    conn.commit()

                if n % PROGRESS_EVERY == 0 or n == len(todo):
                    elapsed = time.monotonic() - start
                    print(f"진행 {n}/{len(todo)} | 성공 {ok} | 데이터없음 {no_data} | 실패 {failed} | {elapsed:.0f}초 경과")

                if blocked:
                    print(
                        f"*** 차단/한도초과 신호 감지: {block_detail} — 남은 요청을 취소하고 중단합니다 ***",
                        file=sys.stderr,
                    )
                    for f in future_to_task:
                        f.cancel()
                    break

        conn.commit()
        elapsed = time.monotonic() - start
        remaining_after = len(pending) - completed
        print("\n=== 완료 ===")
        print(f"이번 실행 처리: {completed}건 (성공 {ok}, 데이터없음 {no_data}, 실패 {failed}), 소요 {elapsed:.1f}초")
        print(f"남은 미처리: {remaining_after}건 (내일 재실행하면 자동으로 이어짐)")
        print(f"차단 감지: {blocked}" + (f" ({block_detail})" if blocked else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("daily_target", nargs="?", type=int, default=DAILY_CALL_TARGET)
    parser.add_argument("--ofs-fallback", action="store_true", help="CFS 013(0분기 기업)만 OFS로 재조회")
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 작업목록 건수/구성만 출력")
    args = parser.parse_args()
    main(daily_target=args.daily_target, ofs_fallback=args.ofs_fallback, dry_run=args.dry_run)
