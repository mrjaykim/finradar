"""삼성전자 최근 사업연도 재무제표를 DART OpenAPI에서 조회한다."""
import os
import sys
import zipfile
import io
from datetime import date
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

CORP_NAME = "삼성전자"
STOCK_CODE = "005930"
CORP_CODE_CACHE = os.path.join(os.path.dirname(__file__), ".dart_corp_code.xml")

REPRT_CODE = "11011"  # 사업보고서 (연간)
KEY_ACCOUNTS = ["매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"]


def load_api_key() -> str:
    load_dotenv()
    api_key = os.getenv("DART_API_KEY")
    if not api_key:
        sys.exit("DART_API_KEY가 .env에 설정되어 있지 않습니다.")
    return api_key


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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = load_api_key()
    corp_code = get_corp_code(api_key, CORP_NAME, STOCK_CODE)
    year, fs_div, rows = find_latest_statements(api_key, corp_code)
    print_summary(year, fs_div, rows)


if __name__ == "__main__":
    main()
