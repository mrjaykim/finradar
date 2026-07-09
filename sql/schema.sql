-- finradar 데이터베이스 스키마
-- raw: DART API 응답/KRX KIND 크롤링 결과 원형 적재 / mart: 정제된 분석용 테이블
-- 실행 순서: 스키마 생성 -> raw -> mart.company -> mart.filing -> mart.financial_statement
--          -> mart.delisted_company

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS mart;

-- ============================================================
-- raw.dart_financial_statement
-- DART fnlttSinglAcntAll 응답을 가공/제약 없이 그대로 적재
-- ============================================================
CREATE TABLE raw.dart_financial_statement (
    id               bigserial PRIMARY KEY,
    rcept_no         text,
    corp_code        text,
    bsns_year        text,
    reprt_code       text,
    fs_div           text,
    fs_nm            text,
    sj_div           text,
    sj_nm            text,
    account_id       text,
    account_nm       text,
    account_detail   text,
    thstrm_nm        text,
    thstrm_amount    text,
    frmtrm_nm        text,
    frmtrm_amount    text,
    bfefrmtrm_nm     text,
    bfefrmtrm_amount text,
    ord              text,
    currency         text,
    ingested_at      timestamptz NOT NULL DEFAULT now(),
    source_payload   jsonb
);

-- ============================================================
-- mart.company
-- 기업 마스터 (거의 변경되지 않는 차원 테이블)
-- ============================================================
CREATE TABLE mart.company (
    corp_code       text PRIMARY KEY,
    corp_name       text NOT NULL,
    stock_code      text UNIQUE,
    market          text,
    industry_code   text,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- mart.filing
-- 접수(rcept_no) 단위 filing 메타데이터
-- ============================================================
CREATE TABLE mart.filing (
    rcept_no        text PRIMARY KEY,
    corp_code       text NOT NULL REFERENCES mart.company (corp_code),
    bsns_year       smallint NOT NULL,
    reprt_code      text NOT NULL CHECK (reprt_code IN ('11011', '11012', '11013', '11014')),
    rcept_dt        date,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_filing_corp_year ON mart.filing (corp_code, bsns_year, reprt_code);

-- ============================================================
-- mart.financial_statement
-- 계정과목 단위 재무 지표 팩트 테이블 (long format)
-- ============================================================
CREATE TABLE mart.financial_statement (
    id              bigserial PRIMARY KEY,
    corp_code       text NOT NULL REFERENCES mart.company (corp_code),
    bsns_year       smallint NOT NULL,
    reprt_code      text NOT NULL CHECK (reprt_code IN ('11011', '11012', '11013', '11014')),
    quarter         smallint CHECK (quarter IN (1, 2, 3, 4)),  -- reprt_code 기반으로 ETL 단계에서 채워지는 파생 컬럼
    fs_div          text NOT NULL CHECK (fs_div IN ('CFS', 'OFS')),
    sj_div          text NOT NULL CHECK (sj_div IN ('BS', 'IS', 'CIS', 'CF', 'SCE')),
    account_id      text NOT NULL,
    account_nm      text NOT NULL,
    account_detail  text,  -- SCE(자본변동표)에서 account_id+ord가 같아도 자본 구성요소별로 값이 달라 유일성 판별에 필요
    metric_amount   bigint,
    currency        text,
    ord             smallint,
    rcept_no        text NOT NULL REFERENCES mart.filing (rcept_no),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_detail, ord)
);

CREATE INDEX idx_financial_statement_rcept_no ON mart.financial_statement (rcept_no);
CREATE INDEX idx_financial_statement_company_period ON mart.financial_statement (corp_code, bsns_year, quarter);

-- ============================================================
-- raw.krx_delisted_company
-- KRX KIND 상장폐지현황(delcompany.do) 응답을 가공/제약 없이 그대로 적재
-- ============================================================
CREATE TABLE raw.krx_delisted_company (
    id                bigserial PRIMARY KEY,
    seq_no_raw        text,
    market_raw        text,
    krx_isu_cd        text,  -- KIND 내부 발행인코드. 국내 종목은 5자리 숫자, 해외 국적 상장사는
                              -- 영문+숫자 조합(예: 'JPN07'). DART corp_code/6자리 종목코드와 다름
    company_name_raw  text,
    delist_date_raw   text,
    delist_reason_raw text,
    note_raw          text,
    row_html          text,  -- 파싱 전 원본 <tr> HTML (재파싱/재현용)
    ingested_at       timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- mart.delisted_company
-- 정제된 상장폐지 이력. 조기경보 라벨의 1차 소스 (ADR-005 참고)
--
-- krx_isu_cd 단독으로는 유일하지 않다: 같은 회사가 "코스닥시장 이전상장"처럼 형식적
-- 사유로 한 번, 이후 실질적 사유(자본잠식 등)로 또 한 번 이 목록에 등장하는 경우가
-- 실제 데이터에 존재한다(예: 필룩스, 신세계건설, KTF). 따라서 회사 단위가 아니라
-- "폐지 이벤트" 단위로 (krx_isu_cd, delisted_on) 복합 UNIQUE를 둔다 (ADR-001과 동일하게
-- bigserial 대리키 + UNIQUE 제약 패턴).
-- ============================================================
CREATE TABLE mart.delisted_company (
    id                     bigserial PRIMARY KEY,
    krx_isu_cd             text NOT NULL,  -- KIND 발행인코드. mart.company(corp_code/stock_code)와
                                            -- 식별체계가 달라 조인 불가 — FK를 걸지 않음
    company_name           text NOT NULL,
    market                 text NOT NULL,  -- 유가증권 / 코스닥 / 코넥스
    delisted_on            date NOT NULL,
    delist_reason          text,
    is_financial_distress  boolean NOT NULL,  -- delist_reason 키워드 분류 결과. 이전상장/합병/펀드
                                               -- 만기 등 형식적 사유는 false. 분류 기준은 ADR-005 참고
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (krx_isu_cd, delisted_on)
);

CREATE INDEX idx_delisted_company_delisted_on ON mart.delisted_company (delisted_on);
