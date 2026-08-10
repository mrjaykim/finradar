"""DART corpCode.xml에서 상장 종목만 골라 mart.company(활성 상장사 마스터)에 적재한다.

sql/schema.sql 주석대로 mart.company는 "활성 상장사 마스터"다. corpCode.xml에는 비상장
법인까지 포함해 11만 건이 넘게 들어있지만, stock_code가 있는 행(3,922건)만 이 테이블의
적재 대상이다.

주의: corpCode.xml의 stock_code 존재 여부는 "현재 상장 중"을 보장하지 않는다.
classify_delisted_matching.py로 확인한 결과, mart.delisted_company의 상장폐지 법인
1,349건(전체 1,751건 중 77%)도 corpCode.xml에 stock_code가 살아있었다 (DART가 상장폐지
후에도 stock_code 필드를 바로 지우지 않는 것으로 보임 - 진로산업/JS전선처럼 2003년,
2014년 두 차례 상장폐지된 법인도 지금 스냅샷에 stock_code가 남아있다). 그래서
is_active는 stock_code 유무가 아니라 mart.delisted_company와의 교차 대조로 판정한다:
해당 stock_code가 실질부실(is_financial_distress=true)로 폐지된 이력이 있으면 false,
없으면 true. "이전상장"류 형식적 사유(is_financial_distress=false)만 있는 경우는 지금도
정상 거래 중인 경우가 많아 true로 남긴다.

한계: 실질부실로 폐지됐다가 나중에 재상장해 지금 다시 거래 중인 극히 드문 경우(예:
진로산업->JS전선처럼 동일 stock_code로 재상장)는 재상장 이벤트 자체를 수집한 적이 없어
이 로직으로 구분 못하고 false로 남는다. 근본적으로 고치려면 KIND의 "현재 상장종목현황"
같은 긍정 목록(현재 활성 종목을 직접 알려주는 소스)이 별도로 필요하다 - 후속 과제.
"""
import os
import sys
import xml.etree.ElementTree as ET

import psycopg2
from psycopg2.extras import execute_values

from collect_delisted_companies import get_db_config

CORP_CODE_XML_PATH = os.path.join(os.path.dirname(__file__), "..", ".dart_corp_code.xml")
BATCH_SIZE = 2000


def parse_listed_companies(path: str) -> list[dict]:
    tree = ET.parse(path)
    rows = []
    for elem in tree.getroot().iter("list"):
        stock_code = (elem.findtext("stock_code") or "").strip()
        if not (stock_code.isdigit() and len(stock_code) == 6):
            continue
        rows.append({
            "corp_code": (elem.findtext("corp_code") or "").strip(),
            "corp_name": (elem.findtext("corp_name") or "").strip(),
            "stock_code": stock_code,
        })
    return rows


def get_distress_stock_codes(conn) -> set[str]:
    """실질부실(is_financial_distress=true)로 폐지된 이력이 있는 stock_code 집합.

    "이전상장" 등 형식적 사유(false)만 있는 stock_code는 포함하지 않는다 - 지금도 정상
    거래 중인 경우가 많기 때문.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT stock_code
            FROM mart.delisted_company
            WHERE stock_code IS NOT NULL AND is_financial_distress = true
            """
        )
        return {row[0] for row in cur.fetchall()}


def upsert_mart_company(conn, rows: list[dict], distress_stock_codes: set[str]) -> tuple[int, int]:
    values = [
        (r["corp_code"], r["corp_name"], r["stock_code"], r["stock_code"] not in distress_stock_codes)
        for r in rows
    ]
    active_count = sum(1 for v in values if v[3])
    inactive_count = len(values) - active_count

    with conn.cursor() as cur:
        for i in range(0, len(values), BATCH_SIZE):
            batch = values[i:i + BATCH_SIZE]
            execute_values(
                cur,
                """
                INSERT INTO mart.company (corp_code, corp_name, stock_code, is_active)
                VALUES %s
                ON CONFLICT (corp_code) DO UPDATE SET
                    corp_name = EXCLUDED.corp_name,
                    stock_code = EXCLUDED.stock_code,
                    is_active = EXCLUDED.is_active,
                    updated_at = now()
                """,
                batch,
            )
            conn.commit()
            print(f"진행 {min(i + BATCH_SIZE, len(values))}/{len(values)}")

    return active_count, inactive_count


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"corpCode.xml 파싱: {CORP_CODE_XML_PATH}")
    rows = parse_listed_companies(CORP_CODE_XML_PATH)
    print(f"stock_code 보유 법인 {len(rows)}건 파싱 완료")

    conn = psycopg2.connect(**get_db_config())
    try:
        distress_stock_codes = get_distress_stock_codes(conn)
        print(f"실질부실 폐지 이력 stock_code {len(distress_stock_codes)}건 확인")
        active_count, inactive_count = upsert_mart_company(conn, rows, distress_stock_codes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"완료: mart.company에 {len(rows)}건 upsert (is_active=true {active_count}건, false {inactive_count}건)")


if __name__ == "__main__":
    main()
