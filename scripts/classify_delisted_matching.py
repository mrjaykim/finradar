"""corpCode.xml과 mart.delisted_company를 정규화 교차검증해 엔티티 매칭 패턴을 분류한다.

ADR-008 후속 작업. stock_code로 DART corpCode.xml과 조인한 뒤 회사명을 정규화 비교해
패턴 A(corp_code 자체 없음) / B(이름 불일치) / C(정상 매칭)로 나누고, 패턴 B 중
실질부실(is_financial_distress) 건에 한해 "단순 표기 차이"(카테고리 1: 영문 약칭↔한글
음역, 스팩/리츠류 KIND 약식명↔DART 법정 정식명칭)를 정규화 규칙으로 구제한다.

2026-07-27 세션에서 동일 분석을 scratchpad에서 애드혹으로만 수행해 원본 232건 목록과
분류 스크립트 자체가 다음 세션에 유실되는 일이 있었다. 이번엔 스크립트를 커밋하고,
결과를 docs/data/에 CSV/JSON으로 남겨 재현 가능하게 한다 (DB 테이블은 이 리포트가
일회성 검증 산출물이라 스키마 마이그레이션까지는 과함).

카테고리 2(상호변경)는 KIND 공식 상호변경 이력이라는 별도 데이터소스가 필요한데 아직
수집한 적이 없다. 이번 재현에서는 카테고리 2를 카테고리 5(원인불명)와 분리하지 않고
"미분류 잔여"로 합쳐서 보고한다 - 근거 없는 정밀도보다 정직한 미확인 표시가 낫다는
판단. 카테고리 4(종목코드 재사용)는 KRX 정책상 폐지 코드를 재사용하지 않는다는 사실이
이미 ADR-008에서 확인되어 별도 계산 없이 0으로 둔다.
"""
import csv
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import psycopg2

from collect_delisted_companies import get_db_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORP_CODE_XML_PATH = os.path.join(SCRIPT_DIR, "..", ".dart_corp_code.xml")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "..", "docs", "data")
ENTITIES_CSV_PATH = os.path.join(OUTPUT_DIR, "delisted_entity_classification.csv")
SUMMARY_JSON_PATH = os.path.join(OUTPUT_DIR, "delisted_entity_classification_summary.json")


# ============================================================
# 기본 정규화 (strict/loose) - 법인격 표기·공백·기호 차이만 흡수
# ============================================================
CORP_SUFFIX_RE = re.compile(r"\(주\)|\(유\)|㈜|주식회사|유한회사")
WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^0-9a-zA-Z가-힣]")


def normalize_strict(name: str) -> str:
    """법인격 표기((주)/주식회사 등)와 공백만 제거하는 보수적 정규화."""
    name = CORP_SUFFIX_RE.sub("", name)
    name = WHITESPACE_RE.sub("", name)
    return name.strip()


def normalize_loose(name: str) -> str:
    """strict + 온점/하이픈/괄호/& 등 영숫자·한글 이외 기호를 모두 제거."""
    return NON_ALNUM_RE.sub("", normalize_strict(name))


# ============================================================
# 카테고리 1a: 영문 약칭 <-> 한글 음역
#
# 고정 매핑표 대신 알파벳별 한글 발음을 조합하는 일반 규칙으로 구현했다. 232건 원본
# 목록이 유실되어 "실제 관측된 패턴만" 표로 옮길 수 없었고, 알파벳 조합 규칙이 목록에
# 없던 새 약칭(예: DL, HL)에도 일반화되어 더 견고하다.
# ============================================================
ALPHABET_HANGUL = {
    "A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지",
    "H": "에이치", "I": "아이", "J": "제이", "K": "케이", "L": "엘", "M": "엠", "N": "엔",
    "O": "오", "P": "피", "Q": "큐", "R": "알", "S": "에스", "T": "티", "U": "유",
    "V": "브이", "W": "더블유", "X": "엑스", "Y": "와이", "Z": "제트",
}
AMPERSAND_HANGUL = "앤"
ALNUM_RUN_RE = re.compile(r"[A-Z&]+")

# 알파벳별 발음 조합 규칙으로 안 풀리는 실측 예외. "ENP"는 "E&P"(Exploration & Production,
# 자원개발업 관용어)를 &없이 표기한 브랜드여서 문자 그대로 읽으면 안 된다 - 알파벳
# 하나씩 발음하는 일반 규칙이 아니라 업계 약어 관용 표기라 예외로 둔다.
ALNUM_RUN_OVERRIDES = {
    "ENP": "이앤피",
}


def transliterate_initialisms(name: str) -> str:
    """이름에 포함된 영문/& 토큰을 전부 한글 음역으로 치환한다 (LG->엘지, 코오롱ENP->코오롱이앤피).

    맨 앞 약칭(LG전자)뿐 아니라 이름 중간에 낀 약칭(코오롱ENP -> 코오롱이앤피)도 흔해
    앞쪽만이 아니라 문자열 전체에서 연속된 영문/& 런을 찾아 치환한다.
    """
    name = name.upper()

    def repl(m: re.Match) -> str:
        run = m.group(0)
        if run in ALNUM_RUN_OVERRIDES:
            return ALNUM_RUN_OVERRIDES[run]
        return "".join(AMPERSAND_HANGUL if ch == "&" else ALPHABET_HANGUL.get(ch, ch) for ch in run)

    return ALNUM_RUN_RE.sub(repl, name)


# ============================================================
# 카테고리 1b: 스팩/리츠/선박투자회사류 - KIND 약식명 vs DART 법정 정식명칭
#
# "OO스팩4호" -> "OO기업인수목적4호" 식으로 접미사를 정확히 치환하는 대신, 알려진 법정
# 접미사 단어를 전부 제거하고 스폰서명+호수만 남겨 비교한다. 실제 DART 법정명이 케이스마다
# 조금씩 다를 수 있어(기업인수목적 앞에 "제N호"가 붙는 순서 등), 정확한 치환 규칙보다
# "접미사를 떼면 같은 개체"라는 동치 판정이 더 견고하다.
# ============================================================
VEHICLE_SUFFIX_WORDS = [
    "기업구조조정부동산투자회사", "위탁관리부동산투자회사", "자기관리부동산투자회사",
    "개발전문", "부동산투자회사", "부동산투자", "기업인수목적회사", "기업인수목적",
    "국제선박투자회사", "선박투자회사", "선박투자", "해외자원개발특별자산투자회사",
    "해외자원개발", "특별자산투자회사", "특별자산", "주식혼합형투자회사", "리츠", "스팩",
    "홀딩스", "그룹", "회사",
]
VEHICLE_SUFFIX_RE = re.compile("|".join(re.escape(w) for w in VEHICLE_SUFFIX_WORDS))
ORDINAL_RE = re.compile(r"제?(\d+)호")


def split_base_and_ordinal(name: str) -> tuple[str, str | None]:
    """이름에서 회차 번호(제N호)를 분리하고, 알려진 법정 접미사 단어를 제거한 base를 만든다."""
    ordinal_match = ORDINAL_RE.search(name)
    ordinal = ordinal_match.group(1) if ordinal_match else None
    base = ORDINAL_RE.sub("", name)
    base = VEHICLE_SUFFIX_RE.sub("", base)
    return normalize_loose(base), ordinal


def is_notation_diff_match(kind_name: str, dart_name: str) -> bool:
    """카테고리1(단순 표기 차이) 판정: 음역 통일 후 base가 같거나, 한쪽이 다른 쪽의 접두어인가.

    stock_code가 이미 같은 값으로 조인된 두 이름을 비교하는 것이므로(=KRX가 폐지 종목코드를
    재사용하지 않는다는 ADR-008 확인 정책상 사실상 동일 법인), 짧은 접두어 일치도 이
    맥락에서는 안전한 신호다. 다만 회차 번호가 양쪽에 다 있는데 서로 다르면 - 같은
    스폰서가 발행한 "다른 호"의 별개 투자회사일 수 있어 - 명확한 반증으로 보고 거부한다.
    """
    a = transliterate_initialisms(normalize_strict(kind_name))
    b = transliterate_initialisms(normalize_strict(dart_name))
    base_a, ord_a = split_base_and_ordinal(a)
    base_b, ord_b = split_base_and_ordinal(b)

    if not base_a or not base_b:
        return False
    if ord_a is not None and ord_b is not None and ord_a != ord_b:
        return False
    if base_a == base_b:
        return True

    shorter, longer = sorted([base_a, base_b], key=len)
    if not longer.startswith(shorter):
        return False
    # stock_code로 이미 동일 법인으로 앵커된 두 이름끼리의 비교라 짧은 접두어 일치도
    # 안전하다 (완전히 다른 법인과 혼동될 위험이 이 시점엔 없음 - 후보군이 이미 해당
    # stock_code 하나로 좁혀져 있다). 다만 1글자는 우연 일치 여지가 커 최소 2자를 요구한다.
    return len(shorter) >= 2


# ============================================================
# 카테고리 3 휴리스틱: 합병·분할 (금융업권 추정)
#
# KIND 공식 이력 데이터 없이 delist_reason 텍스트 키워드 + 회사명의 금융업권 키워드로만
# 판정하는 근사치다. ADR-008이 말한 "업종 특성"을 재현하려면 정식 업종코드가 필요한데
# corpCode.xml에는 업종코드가 없어 이 근사로 대체한다.
# ============================================================
MERGER_KEYWORDS = ["합병", "인수", "계약이전", "영업양도", "해산"]
FINANCE_SECTOR_KEYWORDS = ["은행", "저축은행", "종합금융", "종금", "금고", "캐피탈", "카드"]


def looks_like_merger_or_split(delist_reasons: str, company_name: str) -> bool:
    reasons = delist_reasons or ""
    return (
        any(k in reasons for k in MERGER_KEYWORDS)
        and any(k in company_name for k in FINANCE_SECTOR_KEYWORDS)
    )


# ============================================================
# DART corpCode.xml 로드
# ============================================================
@dataclass
class DartCorp:
    corp_code: str
    corp_name: str
    stock_code: str


def load_corp_code_xml(path: str) -> dict[str, list[DartCorp]]:
    """stock_code -> DART corp 후보 리스트. 동일 stock_code에 후보가 여럿일 수 있어 리스트로 관리."""
    tree = ET.parse(path)
    by_stock_code: dict[str, list[DartCorp]] = {}
    for elem in tree.getroot().iter("list"):
        stock_code = (elem.findtext("stock_code") or "").strip()
        if not (stock_code.isdigit() and len(stock_code) == 6):
            continue
        corp = DartCorp(
            corp_code=(elem.findtext("corp_code") or "").strip(),
            corp_name=(elem.findtext("corp_name") or "").strip(),
            stock_code=stock_code,
        )
        by_stock_code.setdefault(stock_code, []).append(corp)
    return by_stock_code


# ============================================================
# mart.delisted_company 로드 (krx_isu_cd 단위로 집계)
# ============================================================
def load_delisted_entities(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (krx_isu_cd)
                krx_isu_cd, company_name, stock_code, market
            FROM mart.delisted_company
            ORDER BY krx_isu_cd, delisted_on DESC
            """
        )
        cols = [d[0] for d in cur.description]
        entities = [dict(zip(cols, row)) for row in cur.fetchall()]

        # 같은 krx_isu_cd가 여러 상장폐지 이벤트를 가질 수 있어(예: 필룩스, 신세계건설),
        # "이 법인이 한 번이라도 실질부실 사유로 폐지된 적 있는가"를 bool_or로 집계한다.
        cur.execute(
            """
            SELECT krx_isu_cd,
                   bool_or(is_financial_distress) AS ever_distress,
                   string_agg(DISTINCT delist_reason, ' / ') AS delist_reasons
            FROM mart.delisted_company
            GROUP BY krx_isu_cd
            """
        )
        cols2 = [d[0] for d in cur.description]
        agg_by_isu = {row[0]: dict(zip(cols2, row)) for row in cur.fetchall()}

    for e in entities:
        agg = agg_by_isu[e["krx_isu_cd"]]
        e["is_financial_distress"] = agg["ever_distress"]
        e["delist_reasons"] = agg["delist_reasons"]
    return entities


# ============================================================
# 분류
# ============================================================
def classify_patterns(entities: list[dict], by_stock_code: dict[str, list[DartCorp]]):
    pattern_a, pattern_b, pattern_c = [], [], []
    for e in entities:
        candidates = by_stock_code.get(e["stock_code"], []) if e["stock_code"] else []
        if not candidates:
            e["pattern"] = "A"
            pattern_a.append(e)
            continue

        strict_hit = any(
            normalize_strict(c.corp_name) == normalize_strict(e["company_name"]) for c in candidates
        )
        loose_hit = any(
            normalize_loose(c.corp_name) == normalize_loose(e["company_name"]) for c in candidates
        )
        if strict_hit or loose_hit:
            e["pattern"] = "C"
            pattern_c.append(e)
        else:
            e["pattern"] = "B"
            e["_dart_candidates"] = candidates
            pattern_b.append(e)
    return pattern_a, pattern_b, pattern_c


def classify_pattern_b(pattern_b: list[dict]):
    """패턴 B를 실질부실 여부로 나누고, 실질부실 건에 카테고리 1/3/5(잔여)를 매긴다."""
    for e in pattern_b:
        if not e["is_financial_distress"]:
            e["category"] = "0_non_distress"  # 형식적 사유(이전상장 등) - 카테고리 분류 대상 아님
            continue

        cat1_hit = any(
            is_notation_diff_match(e["company_name"], c.corp_name) for c in e["_dart_candidates"]
        )
        if cat1_hit:
            e["category"] = "1_notation_diff"
            continue

        if looks_like_merger_or_split(e["delist_reasons"], e["company_name"]):
            e["category"] = "3_merger_split_heuristic"
            continue

        e["category"] = "5_unresolved"  # 카테고리 2(상호변경) 분리 불가 - 데이터 없음, 잔여에 포함


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"corpCode.xml 로딩: {CORP_CODE_XML_PATH}")
    by_stock_code = load_corp_code_xml(CORP_CODE_XML_PATH)
    print(f"  stock_code 보유 DART 법인 {sum(len(v) for v in by_stock_code.values())}건")

    conn = psycopg2.connect(**get_db_config())
    try:
        entities = load_delisted_entities(conn)
    finally:
        conn.close()
    print(f"mart.delisted_company distinct krx_isu_cd {len(entities)}건 로딩")

    pattern_a, pattern_b, pattern_c = classify_patterns(entities, by_stock_code)
    classify_pattern_b(pattern_b)

    distress_b = [e for e in pattern_b if e["is_financial_distress"]]
    cat1 = [e for e in distress_b if e["category"] == "1_notation_diff"]
    cat3 = [e for e in distress_b if e["category"] == "3_merger_split_heuristic"]
    cat5 = [e for e in distress_b if e["category"] == "5_unresolved"]

    summary = {
        "total_entities": len(entities),
        "pattern_a_no_corp_code": len(pattern_a),
        "pattern_b_name_mismatch": len(pattern_b),
        "pattern_c_matched": len(pattern_c),
        "pattern_b_non_distress": len(pattern_b) - len(distress_b),
        "pattern_b_distress": len(distress_b),
        "category_1_notation_diff_rescued": len(cat1),
        "category_3_merger_split_heuristic": len(cat3),
        "category_4_stock_code_reuse": 0,
        "category_5_unresolved_incl_rename": len(cat5),
        "note": (
            "카테고리 2(상호변경)는 KIND 공식 상호변경 이력 데이터가 없어 별도 분리하지 "
            "못했고 category_5_unresolved_incl_rename에 합산됨. 카테고리 4는 KRX가 폐지 "
            "종목코드를 재사용하지 않는다는 정책 사실(ADR-008)에 근거해 0으로 고정."
        ),
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(SUMMARY_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    all_rows = pattern_a + pattern_b + pattern_c
    all_rows.sort(key=lambda e: e["krx_isu_cd"])
    with open(ENTITIES_CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "krx_isu_cd", "company_name", "stock_code", "market", "is_financial_distress",
            "pattern", "category", "dart_candidate_names",
        ])
        for e in all_rows:
            candidates = e.get("_dart_candidates") or []
            writer.writerow([
                e["krx_isu_cd"],
                e["company_name"],
                e["stock_code"],
                e["market"],
                e["is_financial_distress"],
                e["pattern"],
                e.get("category", ""),
                "; ".join(c.corp_name for c in candidates),
            ])

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n저장 완료:\n  {ENTITIES_CSV_PATH}\n  {SUMMARY_JSON_PATH}")


if __name__ == "__main__":
    main()
