"""KRX KIND 상장폐지현황(delcompany.do)에서 1999년~현재 전체 이력을 수집해 PostgreSQL에 저장한다.

세션 쿠키나 별도 인증 없이 단일 POST 요청으로 전체 결과(currentPageSize=3000)를 받아올 수
있음을 사전 조사로 확인했다. 상장폐지 이력을 조기경보 라벨로 채택한 배경은
docs/adr/ADR-005-delisting-as-risk-label.md 참고.
"""
import os
import re
import sys
from datetime import date, datetime

import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from bs4 import BeautifulSoup

DELCOMPANY_URL = "https://kind.krx.co.kr/investwarn/delcompany.do"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# KIND companysummary_open('20369') 형태에서 발행인코드를 추출.
# 국내 종목은 5자리 숫자지만, 해외 국적 상장사(예: 'JPN07', 'CYM16')는 영문+숫자 조합이라
# 숫자 전용 패턴으로는 잡히지 않으므로 따옴표 안 전체를 그대로 캡처한다.
ISU_CD_RE = re.compile(r"companysummary_open\('([^']+)'\)")
TOTAL_COUNT_RE = re.compile(r"전체\s*<em>([\d,]+)</em>\s*건")

KNOWN_MARKETS = {"유가증권", "코스닥", "코넥스"}

# delist_reason에서 재무 부실과 무관한 "형식적" 사유를 판정하는 키워드/패턴.
# 분류 기준과 실제 데이터 검증 근거는 docs/adr/ADR-005-delisting-as-risk-label.md 참고.
FORMAL_REASON_SUBSTRINGS = [
    "이전상장",                          # 코스닥시장 이전상장 등, 시장 간 이전
    "완전자회사",                        # 지주회사 완전자회사화, 타법인의 완전자회사로 편입
    "스팩소멸합병",                      # SPAC이 합병 목적 달성 후 소멸
    "존속기간 만료", "존립기간의 만료",   # 리츠/펀드 등 정관상 만기 도래
    "증권투자회사법",                    # 투자회사(펀드)의 법정 해산 사유 (만기 청산)
    "간접투자자산운용업법",
    "자본시장과금융투자업에관한법률 제202조",
]
# "피흡수합병"은 원본에 공백이 섞인 변형("피흡수 합병")이 존재해 정규식으로 매칭
FORMAL_MERGER_RE = re.compile(r"피흡수\s*합병")
# 시장 이전으로 인한 형식적 상장/재상장 사유. "상장폐지 신청"·"상장예비심사...미해소" 등
# 실질부실 사유에도 "상장"이 포함되므로, 반드시 이 접미사로 "끝나는" 경우로만 좁힌다
# (예: "유가증권시장 상장", "코스닥시장 상장", "한국증권거래소 상장", "증권거래소 상장").
FORMAL_MARKET_TRANSFER_SUFFIXES = ("시장 상장", "거래소 상장")


def is_financial_distress(delist_reason: str) -> bool:
    """delist_reason이 재무 부실을 나타내는 실질적 사유인지 판정한다.

    형식적(비부실) 사유로 확인되면 False, 그 외(자본잠식/감사의견거절/최종부도/
    관리종목 미해소 등 실질적 부실 사유 및 판정이 모호한 "해산 사유 발생" 계열)는
    True를 반환한다.
    """
    if not delist_reason:
        return True
    if any(kw in delist_reason for kw in FORMAL_REASON_SUBSTRINGS):
        return False
    if FORMAL_MERGER_RE.search(delist_reason):
        return False
    if delist_reason.strip().endswith(FORMAL_MARKET_TRANSFER_SUFFIXES):
        return False
    return True


def get_db_config() -> dict:
    load_dotenv()
    return {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
    }


def fetch_delisted_html() -> str:
    today = date.today().strftime("%Y%m%d")
    data = {
        "method": "searchDelCompanySub",
        "forward": "delcompany_sub",
        "currentPageSize": "3000",
        "pageIndex": "1",
        "orderMode": "2",
        "orderStat": "D",
        "marketType": "",
        "searchMode": "",
        "searchCodeType": "",
        "searchCorpName": "",
        "repIsuSrtCd": "",
        "fromDate": "19990101",
        "toDate": today,
        "searchType": "",
    }
    resp = requests.post(
        DELCOMPANY_URL,
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table.list tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue

        name_cell = tds[1]
        a_tag = name_cell.find("a")
        img_tag = name_cell.find("img")

        krx_isu_cd = None
        if a_tag and a_tag.get("onclick"):
            match = ISU_CD_RE.search(a_tag["onclick"])
            if match:
                krx_isu_cd = match.group(1)

        rows.append({
            "row_html": str(tr),
            "seq_no_raw": tds[0].get_text(strip=True),
            "market_raw": img_tag.get("alt") if img_tag else None,
            "krx_isu_cd": krx_isu_cd,
            "company_name_raw": a_tag.get_text(strip=True) if a_tag else name_cell.get_text(strip=True),
            "delist_date_raw": tds[2].get_text(strip=True),
            "delist_reason_raw": tds[3].get_text(strip=True),
            "note_raw": tds[4].get_text(strip=True),
        })

    total_match = TOTAL_COUNT_RE.search(html)
    if total_match:
        reported_total = int(total_match.group(1).replace(",", ""))
        if reported_total != len(rows):
            print(
                f"경고: 페이지가 보고한 전체 건수({reported_total})와 "
                f"파싱된 행 수({len(rows)})가 다릅니다.",
                file=sys.stderr,
            )

    return rows


def parse_date(raw: str):
    return datetime.strptime(raw, "%Y-%m-%d").date()


def normalize_market(raw: str) -> str:
    if raw not in KNOWN_MARKETS:
        print(f"경고: 알 수 없는 시장구분 값 '{raw}'", file=sys.stderr)
    return raw


def insert_raw(conn, rows: list[dict]):
    """raw.krx_delisted_company에 파싱 전 원본 필드와 행 HTML을 가공 없이 그대로 적재한다."""
    columns = [
        "seq_no_raw", "market_raw", "krx_isu_cd", "company_name_raw",
        "delist_date_raw", "delist_reason_raw", "note_raw", "row_html",
    ]
    values = [tuple(row[col] for col in columns) for row in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO raw.krx_delisted_company ({', '.join(columns)}) VALUES %s",
            values,
        )


def upsert_mart(conn, rows: list[dict]):
    """mart.delisted_company에 (krx_isu_cd, delisted_on) 기준으로 정제된 값을 upsert한다.

    krx_isu_cd만으로는 유일하지 않다 — 같은 회사가 "코스닥시장 이전상장" 같은 형식적
    사유로 한 번, 실질적 사유로 또 한 번 이 목록에 등장할 수 있다(예: 필룩스, KTF).
    """
    values = []
    skipped = 0
    for row in rows:
        if not row["krx_isu_cd"]:
            skipped += 1
            continue
        delist_reason = row["delist_reason_raw"] or None
        values.append((
            row["krx_isu_cd"],
            row["company_name_raw"],
            normalize_market(row["market_raw"]),
            parse_date(row["delist_date_raw"]),
            delist_reason,
            is_financial_distress(delist_reason),
        ))

    if skipped:
        print(f"경고: 발행인코드를 찾지 못해 mart 적재에서 제외한 행 {skipped}건", file=sys.stderr)

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO mart.delisted_company (
                krx_isu_cd, company_name, market, delisted_on, delist_reason, is_financial_distress
            ) VALUES %s
            ON CONFLICT (krx_isu_cd, delisted_on) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                market = EXCLUDED.market,
                delist_reason = EXCLUDED.delist_reason,
                is_financial_distress = EXCLUDED.is_financial_distress,
                updated_at = now()
            """,
            values,
        )


def save(rows: list[dict]):
    conn = psycopg2.connect(**get_db_config())
    try:
        insert_raw(conn, rows)
        upsert_mart(conn, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    html = fetch_delisted_html()
    rows = parse_rows(html)
    print(f"상장폐지현황 {len(rows)}건 파싱 완료 (1999-01-01 ~ {date.today().isoformat()})")

    save(rows)
    print(f"{len(rows)}건 저장 완료 (raw.krx_delisted_company, mart.delisted_company)")


if __name__ == "__main__":
    main()
