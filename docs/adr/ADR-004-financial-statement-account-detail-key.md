# ADR-004: mart.financial_statement UNIQUE 제약에 account_detail 추가

## 상태
결정됨 (2026-07-04)

## 배경
`mart.financial_statement`의 UNIQUE 제약은 원래
`(corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, ord)`였다.
ADR-001 설계 당시에는 BS/IS/CIS/CF 위주로 검토했고, SCE(자본변동표)의 구조적 특성은
반영되지 않았다.

## 실제 발견
get_financial_statements.py를 확장해 삼성전자 2025년 사업보고서(CFS)를 실제로 적재하는
과정에서, SCE 섹션 229건 중 105건(전체의 46%)이 기존 UNIQUE 제약을 위반했다.

SCE는 "계정과목 × 자본 구성요소" 행렬 구조라서, 같은 `account_id`(예: `ifrs-full_Equity`,
자본총계)와 같은 `ord`(예: 8)를 가진 행이 자본금/이익잉여금/기타자본요소/지배기업소유주지분/
비지배지분/자본총계/주식발행초과금 등 자본 구성요소별로 최대 7개까지 존재했다.
이 구성요소 구분은 `account_detail` 필드에만 담겨 있었다.

기존 제약으로는 이 7개 행이 모두 "같은 행"으로 취급되어, upsert 시
`ON CONFLICT DO UPDATE`가 동일 배치 내에서 같은 대상 행을 두 번 갱신하려는
`CardinalityViolation` 에러가 발생했다.

## 검토한 대안
1. 현재 제약 유지, ETL에서 SCE 행을 걸러내거나 첫 번째 값만 저장
2. UNIQUE 제약에 `account_detail` 컬럼 추가

## 선택
2번 채택.

`mart.financial_statement`에 `account_detail text` 컬럼을 추가하고,
UNIQUE 제약을 `(corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_detail, ord)`로
확장했다.

## 선택 이유
- 1번(SCE 필터링/일부만 저장)은 실제 자본변동표 데이터를 조용히 유실시켜, 이후 분석에서
  자본 구성요소별 변동을 볼 수 없게 됨
- `account_detail`은 DART 응답에 이미 존재하는 필드이며, BS/IS/CIS/CF처럼 구성요소 구분이
  필요 없는 섹션에서는 `"-"` 값으로 채워져 있어 기존 유일성 판별에 영향을 주지 않음

## 트레이드오프
UNIQUE 제약 컬럼이 늘어나 인덱스 크기가 소폭 증가한다.
다만 SCE 데이터 무결성 확보가 더 중요하다고 판단.
