"""DART 기업개황(company.json)에서 induty_code(업종코드) 원시값을 mart.company에 백필한다.

corpCode.xml 벌크 적재(load_mart_company.py)에는 업종 필드가 없어 mart.company.industry_code가
전부 NULL이었다 (ADR-009). company.json은 corp_code 단건 조회로 induty_code를 제공하므로 이를
호출해 채운다. induty_code는 KSIC 세분류 수준 원시코드이며, 대분류 매핑/층화 활용 설계는
4주차 모델링 단계로 보류한다 (ADR-010 참고).

대상: is_active=true 전체 상장사 + 2010년 이후 실질부실 상장폐지 기업(부실 사건 후보 756건).
"정상기업 후보"를 ADR-010의 1:3 목표치(2,268건)가 아니라 is_active=true 전체로 잡은 이유는,
층화 샘플링 로직 자체가 아직 4주차 미구현 작업이라 지금 목록을 확정하는 대신 전체를 미리
확보해 4주차에 어떤 샘플링 방식을 쓰든 추가 API 호출 없이 진행할 수 있게 하기 위함이다.
"""
import os
import random
import sys
import time

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

from collect_delisted_companies import get_db_config

COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"
DELAY_RANGE = (0.3, 1.0)
BATCH_COMMIT_SIZE = 50
MAX_CONSECUTIVE_FAILURES = 5


def get_dart_api_key() -> str:
    load_dotenv()
    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError(".env에 DART_API_KEY가 설정되어 있지 않습니다")
    return key.strip()


def get_target_corp_codes(conn) -> list[str]:
    """induty_code 백필 대상 corp_code 목록.

    is_active=true 전체 상장사 + 2010년 이후 실질부실 상장폐지 기업(756건 후보군).
    이미 industry_code가 채워진 행은 재실행 시 건너뛴다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT corp_code FROM mart.company
            WHERE is_active = true AND industry_code IS NULL
            UNION
            SELECT c.corp_code FROM mart.delisted_company d
            JOIN mart.company c ON d.stock_code = c.stock_code
            WHERE d.is_financial_distress = true AND d.delisted_on >= '2010-01-01'
              AND c.industry_code IS NULL
            ORDER BY 1
            """
        )
        return [row[0] for row in cur.fetchall()]


def fetch_company_overview(corp_code: str, api_key: str) -> dict:
    resp = requests.get(
        COMPANY_URL,
        params={"crtfc_key": api_key, "corp_code": corp_code},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def save_result(conn, corp_code: str, data: dict) -> bool:
    """raw 레이어에 원본을 적재하고, status=000이고 induty_code가 있으면 mart를 갱신한다."""
    status = data.get("status")
    message = data.get("message")
    induty_code = data.get("induty_code")
    corp_name = data.get("corp_name")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw.dart_company_overview
                (corp_code, status, message, induty_code_raw, corp_name_raw, response_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (corp_code, status, message, induty_code, corp_name, psycopg2.extras.Json(data)),
        )

        if status == "000" and induty_code:
            cur.execute(
                "UPDATE mart.company SET industry_code = %s, updated_at = now() WHERE corp_code = %s",
                (induty_code, corp_code),
            )
            return True
    return False


def main(limit: int | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    api_key = get_dart_api_key()

    conn = psycopg2.connect(**get_db_config())
    try:
        corp_codes = get_target_corp_codes(conn)
        if limit:
            corp_codes = corp_codes[:limit]
        total = len(corp_codes)
        print(f"업종코드 백필 대상 corp_code {total}건")

        resolved = 0
        failed = 0
        consecutive_failures = 0
        for i, corp_code in enumerate(corp_codes, start=1):
            try:
                data = fetch_company_overview(corp_code, api_key)
                ok = save_result(conn, corp_code, data)
            except Exception as e:
                conn.rollback()
                ok = False
                print(f"경고: {corp_code} 처리 중 오류: {e}", file=sys.stderr)
            else:
                if not ok:
                    print(
                        f"경고: {corp_code} 업종코드 획득 실패 "
                        f"(status={data.get('status')!r}, message={data.get('message')!r})",
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
                        f"연속 {consecutive_failures}회 실패 - 차단/한도초과 가능성이 있어 중단합니다",
                        file=sys.stderr,
                    )
                    print(
                        f"중단 시점까지 {i}/{total}건 처리, 업종코드 획득 {resolved}건, 실패 {failed}건",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            if i % BATCH_COMMIT_SIZE == 0:
                conn.commit()
                print(f"진행 {i}/{total} (성공 {resolved}, 실패 {failed})")

            time.sleep(random.uniform(*DELAY_RANGE))

        conn.commit()
        print(f"완료: {total}건 처리, 업종코드 획득 {resolved}건, 실패 {failed}건")
    finally:
        conn.close()


if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=limit_arg)
