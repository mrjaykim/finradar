"""KIND 기업개황(companysummary.do)에서 krx_isu_cd별 6자리 종목코드를 백필한다.

mart.delisted_company.krx_isu_cd(KIND 발행인코드)와 mart.company.stock_code(DART 6자리)를
직접 조인할 키가 없어, delcompany.do 행의 companysummary_open()이 여는 기업개황 팝업이
호출하는 companysummary.do?method=searchCompanySummaryOvrvwDetail 응답에서 종목코드를
가져와 채운다. 회사명 매칭(정규화 후 매칭률 57%, 모호 그룹 211건)보다 이 방식이 정확하다.
"""
import random
import sys
import time

import psycopg2
import requests
from bs4 import BeautifulSoup

from collect_delisted_companies import USER_AGENT, get_db_config

SUMMARY_URL = "https://kind.krx.co.kr/common/companysummary.do"
BATCH_COMMIT_SIZE = 50
DELAY_RANGE = (0.3, 1.0)
MAX_CONSECUTIVE_FAILURES = 5


def fetch_summary(krx_isu_cd: str) -> str:
    resp = requests.post(
        SUMMARY_URL,
        params={"method": "searchCompanySummaryOvrvwDetail"},
        data={"strIsurCd": krx_isu_cd, "lstCd": "", "menuIndex": "0", "methodType": "0"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text


def parse_summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    stock_code_raw = None
    for th in soup.find_all("th"):
        if th.get_text(strip=True) == "종목코드":
            td = th.find_next_sibling("td")
            if td:
                stock_code_raw = td.get_text(strip=True)
            break

    corp_name_raw = None
    com_abbrv = soup.find("input", {"name": "comAbbrv"})
    if com_abbrv:
        corp_name_raw = com_abbrv.get("value")

    return {"stock_code_raw": stock_code_raw, "corp_name_raw": corp_name_raw}


def get_pending_isu_cds(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT krx_isu_cd FROM mart.delisted_company "
            "WHERE stock_code IS NULL ORDER BY krx_isu_cd"
        )
        return [row[0] for row in cur.fetchall()]


def save_result(conn, krx_isu_cd: str, row_html: str, parsed: dict) -> bool:
    """raw 레이어에 원본을 적재하고, 6자리 종목코드로 파싱되면 mart를 갱신한다."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.krx_company_summary (krx_isu_cd, stock_code_raw, corp_name_raw, row_html)
            VALUES (%s, %s, %s, %s)
            """,
            (krx_isu_cd, parsed["stock_code_raw"], parsed["corp_name_raw"], row_html),
        )

        stock_code = parsed["stock_code_raw"]
        if stock_code and stock_code.isdigit() and len(stock_code) == 6:
            cur.execute(
                "UPDATE mart.delisted_company SET stock_code = %s, updated_at = now() "
                "WHERE krx_isu_cd = %s",
                (stock_code, krx_isu_cd),
            )
            return True
    return False


def main(limit: int | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = psycopg2.connect(**get_db_config())
    try:
        isu_cds = get_pending_isu_cds(conn)
        if limit:
            isu_cds = isu_cds[:limit]
        total = len(isu_cds)
        print(f"백필 대상 krx_isu_cd {total}건")

        resolved = 0
        failed = 0
        consecutive_failures = 0
        for i, krx_isu_cd in enumerate(isu_cds, start=1):
            try:
                html = fetch_summary(krx_isu_cd)
                parsed = parse_summary(html)
                ok = save_result(conn, krx_isu_cd, html, parsed)
            except Exception as e:
                conn.rollback()
                ok = False
                print(f"경고: {krx_isu_cd} 처리 중 오류: {e}", file=sys.stderr)
            else:
                if not ok:
                    print(
                        f"경고: {krx_isu_cd} 종목코드 파싱 실패 "
                        f"(raw 값: {parsed['stock_code_raw']!r})",
                        file=sys.stderr,
                    )

            if ok:
                resolved += 1
                consecutive_failures = 0
            else:
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    conn.commit()
                    print(
                        f"연속 {consecutive_failures}회 실패 - 차단 가능성이 있어 중단합니다",
                        file=sys.stderr,
                    )
                    print(
                        f"중단 시점까지 {i}/{total}건 처리, 종목코드 획득 {resolved}건, 실패 {failed}건",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if i % BATCH_COMMIT_SIZE == 0:
                conn.commit()
                print(f"진행 {i}/{total} (성공 {resolved}, 실패 {failed})")

            time.sleep(random.uniform(*DELAY_RANGE))

        conn.commit()
        print(f"완료: {total}건 처리, 종목코드 획득 {resolved}건, 실패 {failed}건")
    finally:
        conn.close()


if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=limit_arg)
