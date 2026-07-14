# ADR-007: KIND krx_isu_cd와 DART stock_code 매칭 방법 선택

## 상태
결정됨 (2026-07-14)

## 배경
`mart.delisted_company`는 KIND 발행인코드(`krx_isu_cd`, 5자리)만 갖고 있고 `mart.company`는
DART 6자리 `stock_code`를 키로 쓰기 때문에 두 테이블을 조인할 방법이 없었음.

## 검토한 대안

**1. 회사명 정규화 매칭**
`mart.delisted_company`의 `company_name`과 DART corpCode.xml의 `corp_name`을 정규화
(공백/주식회사/㈜ 제거)해서 비교. 실측 결과 최선이어도 1,006/1,766 = 57.0% 매칭, 동일
정규화명에 여러 `corp_code`가 걸리는 모호 그룹 211건 발생. 미매칭 주원인은 스팩(173건,
상폐 당시명과 합병 후 DART 등록명이 다름)과 사명 변경(예: HD현대미포 舊 현대미포조선).

**2. KIND companysummary.do 크롤링**
`delcompany.do`의 `companysummary_open(krx_isu_cd)` 팝업이 호출하는
`POST /common/companysummary.do?method=searchCompanySummaryOvrvwDetail` 응답에 정확한
6자리 종목코드가 포함되어 있음을 확인.

## 선택
2번(KIND companysummary.do 크롤링) 채택.

## 선택 이유
회사명 매칭은 모호성과 낮은 매칭률(57%)로 신뢰할 수 없었던 반면, companysummary.do는 정확한
키(종목코드) 자체를 직접 제공해 매칭 정확도가 원천적으로 다르다. 1,751건 백필 결과 실패
0건, 100% 성공.

## 트레이드오프
KIND 서버에 추가로 1,751회 순차 요청이 필요함(약 19분 소요, 0.3~1.0초 딜레이와 연속 5회
실패 시 중단하는 안전장치로 서버 부하 및 차단 리스크 관리). 회사명 정규화 로직은 개발할
필요가 없어져 코드 복잡도가 오히려 낮아졌다.
