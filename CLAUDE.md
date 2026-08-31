# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트

finradar는 DART 공시 데이터 기반 국내 상장기업 재무 리스크 조기경보 시스템이다.
DART(전자공시시스템, 금융감독원)와 KRX KIND(상장폐지현황 등)에서 데이터를 수집해
raw/mart 2계층 PostgreSQL 스키마에 적재하고, 상장폐지 이력을 라벨로 한 기업-분기 패널을
구성해 예측 모델(트랙 A)과 재무비율 기반 참고 스코어(트랙 B, 예: Altman Z-Score)를
병행 제시하는 것을 목표로 한다. 장시간 실행되는 API 서비스가 아니라 배치/스케줄 잡 형태의
데이터 파이프라인이다.

현재까지 구축된 것:
- PostgreSQL 스키마(`sql/schema.sql`): raw 3개 테이블(`dart_financial_statement`,
  `krx_delisted_company`, `krx_company_summary`, `dart_company_overview`) + mart 4개
  테이블(`company`, `filing`, `financial_statement`, `delisted_company`)
- 수집/백필 스크립트(`scripts/`): 상장폐지현황 수집, DART corp_code ↔ KIND
  krx_isu_cd/stock_code 매칭 백필, 업종코드 백필, 재무제표 대량 백필, mart.company 적재
- `mart.company` 3,922개 상장기업 적재 완료 (2026-08-10 기준)
- 부실기업 756건 + 정상기업 2,904건에 대해 분기별 재무제표 수집 전량 완료 (2026-08-21 기준)
- ADR 10건, devlog 10일치 기록 축적 (아래 참고)
- 다음 단계: 기업-분기 패널 최종 구성 및 prediction window 라벨링 → 모델링 착수 (4주차)

## 스택

- Python. `requirements.txt`: `requests`, `python-dotenv`, `psycopg2-binary`, `beautifulsoup4`
- PostgreSQL (ADR-002 참고: 무료, JSONB 지원, 분석 쿼리 강점)
- DB 접속 정보는 `.env` + `get_db_config()`로만 읽는다. 하드코딩 금지.

## 데이터 소스

1. **DART OpenAPI** (`opendart.fss.or.kr`, API 키 필요)
   - `fnlttSinglAcntAll.json`: 재무제표(전체 계정과목) → `raw.dart_financial_statement`
   - `company.json`: 기업개황(업종코드 등) → `raw.dart_company_overview`
   - `corpCode.xml`: 전체 기업 corp_code/corp_name/stock_code 마스터(`.dart_corp_code.xml`)
   - 일일 호출 한도는 이용현황 페이지 실측 기준 40,000건 (웹서치로 나온 10,000건 정보는
     부정확했음 — devlog 2026-08-19)
2. **KRX KIND** (`kind.krx.co.kr`, 크롤링)
   - `delcompany.do`: 상장폐지현황 → `raw.krx_delisted_company` → `mart.delisted_company`
     (조기경보 라벨의 1차 소스, ADR-005)
   - `companysummary.do?method=searchCompanySummaryOvrvwDetail`: 기업요약 팝업, `krx_isu_cd`
     → 6자리 `stock_code` 정확 매칭 목적 → `raw.krx_company_summary` (ADR-007)

## 스키마 원칙

- **raw/mart 분리** (ADR-003): raw는 API/크롤링 응답을 가공·제약 없이 원형 그대로 적재
  (재현/재파싱 가능하도록 원본 payload 보존). mart는 정제되고 제약이 걸린 분석용 테이블.
- **대리키 + 복합 UNIQUE** (ADR-001): 자연키(예: `account_id`)가 결측·비표준 값으로 오는
  경우가 있어 신뢰할 수 없으므로, `bigserial` 대리키를 PK로 두고 실제 유일성은 별도
  UNIQUE 제약으로 강제한다. `mart.delisted_company`, `mart.financial_statement` 모두 이
  패턴을 따른다.
- 스키마 상세와 각 테이블/컬럼의 배경은 `sql/schema.sql` 주석에 직접 기술되어 있으므로
  스키마를 다룰 때는 반드시 그 주석을 먼저 읽는다.

## ADR 목록 (`docs/adr/`)

- **ADR-001** raw/mart PK 전략 — 자연키 대신 대리키(bigserial) + UNIQUE 제약 채택
- **ADR-002** PostgreSQL 선택 — 무료/JSONB/분석쿼리 강점 + Airflow 생태계 호환
- **ADR-003** raw/mart 레이어 분리 — 원본 보존과 정제 데이터를 물리적으로 분리
- **ADR-004** `mart.financial_statement` UNIQUE에 `account_detail` 추가 — SCE(자본변동표)
  46%가 기존 복합키로 유일성 보장 안 됨을 실측으로 발견
- **ADR-005** 상장폐지 이력을 조기경보 라벨의 1차 소스로 채택 — 관리종목 지정/자체 재무규칙
  대비 데이터 완전성과 라벨 오염 최소화
- **ADR-006** 리스크 스코어링 아키텍처 — 예측 트랙(ML)과 참고 스코어 트랙(Altman Z 등)을
  분리해 라벨-피처 순환논리 차단
- **ADR-007** KIND `krx_isu_cd` ↔ DART `stock_code` 매칭 — 회사명 정규화 매칭(57%) 대신
  `companysummary.do` 크롤링으로 100% 정확 매칭
- **ADR-008** KRX↔DART corpCode.xml 엔티티 해소 — 종목코드 재사용 사례 발견, 카테고리별
  증거 기반으로 구제/제외 판단 (유사도 알고리즘 미사용)
- **ADR-009** `mart.company.is_active` 판정 전략 — corpCode.xml의 stock_code 유무만으로는
  판정 불가, `mart.delisted_company`의 실질부실 이벤트 존재 여부로 판정
- **ADR-010** 학습 데이터셋 패널 설계 — 2010년 이후 KOSPI/KOSDAQ/KONEX, 기업-분기 단위
  패널 + prediction window로 data leakage 방지

## 기록 체계

3층 구조를 목적에 맞게 구분해서 쓴다.

- **devlog/`YYYY-MM-DD.md`** (매일): 그날 한 일 / 막힌 것 / 내일 할 일. 세션 단위 진행
  상황과 작업 로그. 사용자가 세션 종료 시 명시적으로 요청할 때만 작성한다
  ([[feedback_devlog_timing]] 참고 — 중간에 임의로 쓰지 않음).
- **GitHub Issues** (막힌 문제): 원인이 불명확했거나 조사에 시간이 걸린 버그/장애를 상세
  기록. 현재 #1(postgres 비밀번호 재설정), #2(mart.filing FK 누락으로 API 호출 낭비),
  #3(SSL 오류 원인 미확정)이 있음. devlog에는 한 줄 요약 + Issue 번호만 남기고 상세는
  Issue에 적는다.
- **ADR (`docs/adr/`)** (설계 결정): 여러 대안을 검토해 하나를 채택한 구조적 결정. 배경 →
  검토한 대안(실측 수치 포함) → 선택 → 선택 이유 → 트레이드오프 순서로 작성하는 기존
  포맷을 따른다. 이미 결정된 사항을 뒤집을 때도 새 ADR로 추가하고 기존 ADR은 보존한다.

## 중요 데이터 특성

1. **DART API는 2015년 이전 재무제표 데이터가 없다.** 백필 스크립트(`scripts/
   backfill_financial_statements.py`)는 `FLOOR = date(2015, 1, 1)`로 조회 하한을 강제한다.
   이 시점 이전 분기는 상장 여부와 무관하게 "데이터 없음"으로 남는 알려진 한계다.
2. **KIND `krx_isu_cd`와 DART `stock_code`/`corp_code`는 서로 다른 식별체계다.**
   `krx_isu_cd`는 KIND 내부 발행인코드(국내 5자리 숫자, 해외 상장사는 영문+숫자)로 DART와
   직접 조인할 수 없다 (ADR-007). 게다가 종목코드 자체가 재사용되는 사례(예: 진로산업↔
   제이에스전선)가 있어 `stock_code` 단순 조인도 위험하다 (ADR-008). 반드시
   `companysummary.do` 백필로 얻은 정확 매칭 키를 통해서만 연결한다.
3. **`stock_code`가 존재한다고 해서 현재 상장 중인 것은 아니다.** corpCode.xml의
   stock_code 필드는 과거 상장 이력이 있으면 남아있을 수 있어 "현재 상장 중"의 근거가
   될 수 없다 (ADR-009). `mart.company.is_active`는 `mart.delisted_company`에 실질부실
   (`is_financial_distress = true`) 이벤트로 등장한 적이 있는지로 판정한다.
