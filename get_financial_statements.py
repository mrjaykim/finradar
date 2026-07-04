"""삼성전자 최근 사업연도 재무제표를 DART OpenAPI에서 조회하여 PostgreSQL에 저장한다."""
import os
import sys
import zipfile
import io
from datetime import date
import xml.etree.ElementTree as ET

import requests
import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

CORP_NAME = "삼성전자"
STOCK_CODE = "005930"
CORP_CODE_CACHE = os.path.join(os.path.dirname(__file__), ".dart_corp_code.xml")

REPRT_CODE = "11011"  # 사업보고서 (연간)
KEY_ACCOUNTS = ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"]

# DART reprt_code -> mart.financial_statement.quarter 파생값
QUARTER_BY_REPRT_CODE = {
    "11013": 1,  # 1분기보고서
    "11012": 2,  # 반기보고서
    "11014": 3,  # 3분기보고서
    "11011": 4,  # 사업보고서(연간)
}

# docs/etl_notes.md 참고: account_id 결측/비정상 값 치환용 placeholder
UNASSIGNED_ACCOUNT_ID = "UNASSIGNED"


def load_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        sys.exit("DART_API_KEY가 .env에 설정되어 있지 않습니다.")
    return api_key


def get_db_config() -> dict:
    load_dotenv()
    return {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
    }


def get_corp_code(api_key: str, corp_name: str, stock_code: str) -> str:
    if os.path.exists(CORP_CODE_CACHE):
        with open(CORP_CODE_CACHE, "rb") as f:
            xml_bytes = f.read()
    else:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read("CORPCODE.xml")
        with open(CORP_CODE_CACHE, "wb") as f:
            f.write(xml_bytes)

    root = ET.fromstring(xml_bytes)
    for item in root.iter("list"):
        if (
            item.findtext("corp_name") == corp_name
            and item.findtext("stock_code") == stock_code
        ):
            return item.findtext("corp_code")

    raise ValueError(f"{corp_name}({stock_code}) 고유번호를 찾을 수 없습니다.")


def fetch_financial_statements(api_key: str, corp_code: str, bsns_year: int, fs_div: str):
    resp = requests.get(
        "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
        params={
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": REPRT_CODE,
            "fs_div": fs_div,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def find_latest_statements(api_key: str, corp_code: str):
    current_year = date.today().year
    last_message = None
    for year in range(current_year - 1, current_year - 4, -1):
        for fs_div in ("CFS", "OFS"):  # 연결재무제표 우선, 없으면 개별재무제표
            data = fetch_financial_statements(api_key, corp_code, year, fs_div)
            if data.get("status") == "000":
                return year, fs_div, data["list"]
            last_message = data.get("message")
    raise RuntimeError(f"최근 3개년 내 조회 가능한 재무제표를 찾지 못했습니다: {last_message}")


def print_summary(year: int, fs_div: str, rows: list):
    label = "연결" if fs_div == "CFS" else "개별"
    print(f"=== 삼성전자 {year}년도 사업보고서 ({label}재무제표) ===\n")

    seen = {}
    for row in rows:
        name = row.get("account_nm")
        if name in KEY_ACCOUNTS and name not in seen:
            seen[name] = row

    for name in KEY_ACCOUNTS:
        row = seen.get(name)
        amount = row.get("thstrm_amount", "-") if row else "-"
        print(f"{name:10s}: {amount:>20s} 원")


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


def insert_raw_statements(conn, rows: list, fs_div: str):
    """raw.dart_financial_statement에 원본 응답을 가공 없이 그대로 적재한다.

    fs_div는 fnlttSinglAcntAll 응답에 포함되지 않고 요청 파라미터로만 존재하므로,
    조회에 사용한 값을 그대로 채워 넣는다 (row.get으로는 얻을 수 없음).
    """
    columns = [
        "rcept_no", "corp_code", "bsns_year", "reprt_code", "fs_div", "fs_nm",
        "sj_div", "sj_nm", "account_id", "account_nm", "account_detail",
        "thstrm_nm", "thstrm_amount", "frmtrm_nm", "frmtrm_amount",
        "bfefrmtrm_nm", "bfefrmtrm_amount", "ord", "currency",
    ]
    values = [
        tuple(fs_div if col == "fs_div" else row.get(col) for col in columns) + (Json(row),)
        for row in rows
    ]
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO raw.dart_financial_statement ({', '.join(columns)}, source_payload) VALUES %s",
            values,
        )


def upsert_company(conn, corp_code: str, corp_name: str, stock_code: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mart.company (corp_code, corp_name, stock_code)
            VALUES (%s, %s, %s)
            ON CONFLICT (corp_code) DO UPDATE SET
                corp_name = EXCLUDED.corp_name,
                stock_code = EXCLUDED.stock_code,
                updated_at = now()
            """,
            (corp_code, corp_name, stock_code),
        )


def upsert_filing(conn, rcept_no: str, corp_code: str, bsns_year: int, reprt_code: str):
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


def upsert_financial_statements(conn, rows: list, corp_code: str, bsns_year: int, reprt_code: str, fs_div: str):
    quarter = QUARTER_BY_REPRT_CODE.get(reprt_code)
    values = [
        (
            corp_code,
            bsns_year,
            reprt_code,
            quarter,
            fs_div,
            row.get("sj_div"),
            clean_account_id(row.get("account_id")),
            row.get("account_nm"),
            row.get("account_detail"),
            parse_amount(row.get("thstrm_amount")),
            row.get("currency"),
            parse_ord(row.get("ord")),
            row.get("rcept_no"),
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


def save_to_mart(rows: list, corp_code: str, corp_name: str, stock_code: str, bsns_year: int, reprt_code: str, fs_div: str):
    conn = psycopg2.connect(**get_db_config())
    try:
        insert_raw_statements(conn, rows, fs_div)
        upsert_company(conn, corp_code, corp_name, stock_code)
        rcept_no = rows[0]["rcept_no"]
        upsert_filing(conn, rcept_no, corp_code, bsns_year, reprt_code)
        upsert_financial_statements(conn, rows, corp_code, bsns_year, reprt_code, fs_div)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = load_api_key()
    corp_code = get_corp_code(api_key, CORP_NAME, STOCK_CODE)
    year, fs_div, rows = find_latest_statements(api_key, corp_code)
    print_summary(year, fs_div, rows)
    save_to_mart(rows, corp_code, CORP_NAME, STOCK_CODE, year, REPRT_CODE, fs_div)
    print(f"\n{len(rows)}건 저장 완료 (raw.dart_financial_statement, mart.company, mart.filing, mart.financial_statement)")


if __name__ == "__main__":
    main()
