"""부실기업(756건 후보)과 정상기업(is_active=true 2,904건)의 재무제표를
DART fnlttSinglAcntAll.json에서 수집해 raw.dart_financial_statement / mart.financial_statement에
적재한다 (get_financial_statements.py의 단건 조회 로직을 대량 백필용으로 일반화).

분기 산정 규칙(2026-08-19 세션 합의, ADR-010 후속):
  - 부실기업: delisted_on 기준 과거 최대 12분기, 2015-01-01 하한
  - 정상기업: 오늘 기준 과거 최대 12분기, 2015-01-01 하한
  - fs_div=CFS(연결재무제표)만 사용한다. OFS 폴백은 넣지 않았다 - 사전에 합의한 호출 수
    추정("분기 1개 = 호출 1건")과 어긋나지 않게 하기 위한 단순화이며, 연결재무제표가 없는
    기업은 해당 분기가 "데이터 없음"으로 남는 알려진 한계다.

하루 호출량은 DAILY_CALL_TARGET(기본 30,000, 실제 여유 한도보다 넉넉히 낮게 잡은 값)으로
캡을 걸어 미처리 목록의 앞부분만 잘라서 처리한다. 이미 raw.dart_financial_statement에 기록된
(corp_code, bsns_year, reprt_code) 조합은 건너뛰므로, 다음날 그냥 재실행하면 남은 부분부터
자동으로 이어진다 - 별도 상태 파일이 필요 없다.

020(요청 제한)/429 등 차단 신호가 관측되면 목표치 도달 여부와 무관하게 즉시 중단한다
(하루 목표 캡과는 별개의 이중 안전장치).
"""
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


def filter_pending(conn, worklist: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    """이미 raw.dart_financial_statement에 기록된(=이미 시도한) 조합을 제외한다.

    데이터가 없던 호출(013 등)도 플레이스홀더 1행으로 기록되므로, 성공/실패 여부와 무관하게
    "이미 호출을 시도했는가"만으로 재시도 여부를 가른다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT corp_code, bsns_year, reprt_code FROM raw.dart_financial_statement WHERE fs_div = %s",
            (FS_DIV,),
        )
        done = {(row[0], str(row[1]), row[2]) for row in cur.fetchall()}
    return [t for t in worklist if (t[0], str(t[1]), t[2]) not in done]


def fetch_financial_statement(corp_code: str, bsns_year: int, reprt_code: str, api_key: str) -> dict:
    resp = requests.get(
        FS_URL,
        params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "fs_div": FS_DIV,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


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


def insert_raw(conn, corp_code: str, bsns_year: int, reprt_code: str, data: dict) -> list[dict]:
    """raw 레이어에 원본을 적재한다. 데이터가 없으면(013 등) 재조회 방지용 플레이스홀더 1행을 남긴다."""
    rows = data.get("list") or []
    if rows:
        values = [
            tuple(FS_DIV if col == "fs_div" else row.get(col) for col in RAW_COLUMNS) + (Json(row),)
            for row in rows
        ]
    else:
        placeholder = {col: None for col in RAW_COLUMNS}
        placeholder.update(corp_code=corp_code, bsns_year=str(bsns_year), reprt_code=reprt_code, fs_div=FS_DIV)
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


def upsert_mart(conn, rows: list[dict], corp_code: str, bsns_year: int, reprt_code: str):
    if not rows:
        return
    upsert_filing(conn, rows[0]["rcept_no"], corp_code, bsns_year, reprt_code)
    quarter = QUARTER_BY_REPRT_CODE[reprt_code]
    values = [
        (
            corp_code, bsns_year, reprt_code, quarter, FS_DIV,
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


def main(daily_target: int = DAILY_CALL_TARGET):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = get_dart_api_key()
    conn = psycopg2.connect(**get_db_config())

    try:
        print("작업 목록 생성 중...")
        full_worklist = build_worklist(conn)
        pending = filter_pending(conn, full_worklist)
        print(f"전체 대상 {len(full_worklist)}건 중 미처리 {len(pending)}건")

        if not pending:
            print("처리할 항목이 없습니다.")
            return

        todo = pending[:daily_target]
        print(f"오늘 목표(최대 {daily_target}건) 내에서 {len(todo)}건 처리 시작\n")

        lock = threading.Lock()
        completed = 0
        ok = 0
        no_data = 0
        blocked = False
        block_detail = None
        start = time.monotonic()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            future_to_task = {
                pool.submit(fetch_financial_statement, corp_code, bsns_year, reprt_code, api_key): (corp_code, bsns_year, reprt_code)
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
                    print(f"경고: {corp_code} {bsns_year} {reprt_code} 처리 중 오류: {e}", file=sys.stderr)
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
                        rows = insert_raw(conn, corp_code, bsns_year, reprt_code, data)
                        upsert_mart(conn, rows, corp_code, bsns_year, reprt_code)
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
                    print(f"진행 {n}/{len(todo)} | 성공 {ok} | 데이터없음 {no_data} | {elapsed:.0f}초 경과")

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
        print(f"이번 실행 처리: {completed}건 (성공 {ok}, 데이터없음 {no_data}), 소요 {elapsed:.1f}초")
        print(f"남은 미처리: {remaining_after}건 (내일 재실행하면 자동으로 이어짐)")
        print(f"차단 감지: {blocked}" + (f" ({block_detail})" if blocked else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    target_arg = int(sys.argv[1]) if len(sys.argv) > 1 else DAILY_CALL_TARGET
    main(daily_target=target_arg)
