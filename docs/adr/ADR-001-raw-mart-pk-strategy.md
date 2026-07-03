# ADR-001: raw/mart 스키마의 PK(기본키) 전략

## 상태
결정됨 (2026-07-03)

## 배경
DART API 응답에서 `account_id`가 결측되거나 비표준 값(공백, `-`)으로 오는 경우가 있어,
이를 기준으로 한 복합키 설계가 위험할 수 있음.

## 검토한 대안
1. raw에 `(rcept_no, fs_div, sj_div, account_id, ord)` 복합키
2. raw에 `bigserial` 대리키 + 제약 없음

## 선택
2번 채택.

`mart.financial_statement`는 `bigserial` 대리키를 두되,
`(corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, ord)` UNIQUE 제약으로
데이터 품질을 보장한다.

## 트레이드오프
raw 레벨에서는 정합성 검증을 포기하고 무조건 적재를 우선시함.
정합성 검증은 mart 변환 단계(ETL)에서 책임진다.
