# ADR-009: mart.company is_active 판정 전략

## 상태
결정됨 (2026-08-10)

## 배경
ADR-008 후속으로 corpCode.xml → mart.company(활성 상장사 마스터, sql/schema.sql 주석
참고) 적재 파이프라인을 구현하는 과정에서, corpCode.xml의 stock_code 필드만으로는
"현재 상장 중"을 판정할 수 없음을 발견했다.

## 검토한 대안 및 발견

**1. stock_code 유무로 is_active 판정**
당초 가정: corpCode.xml에 6자리 stock_code가 있으면 현재 상장 중이라고 볼 수 있다.

틀렸음이 확인됨. `classify_delisted_matching.py`(ADR-008)로 확인한 결과, KIND
상장폐지현황(`mart.delisted_company`) 1,751건 중 1,349건(77%)이 지금 시점 corpCode.xml
스냅샷에도 여전히 stock_code를 갖고 있었다. 구체적 사례: "진로산업"(2003년 자본전액잠식
2년 계속으로 상장폐지) -> 상호변경 후 "JS전선"으로 재상장 -> 2014년 재차 상장폐지. 같은
krx_isu_cd(00556), 같은 stock_code(005560)를 유지한 채 두 번 폐지됐는데도 corpCode.xml은
지금까지 이 stock_code를 갖고 있다. DART가 상장폐지 후 stock_code 필드를 즉시(또는
영구히) 비우지 않는다는 뜻이다 - 아마 채권 등 지분증권 외 공시 의무가 남아있거나,
단순히 오래된 스냅샷 정리가 지연되는 것으로 추정.

**2. mart.delisted_company와 교차 대조 (채택)**
`mart.delisted_company.stock_code`(ADR-007에서 백필) 중 `is_financial_distress = true`
이력이 있는 stock_code를 "실질적으로 폐지됨"으로 보고 is_active = false로 판정. 그 외
(형식적 사유만 있거나 폐지 이력 자체가 없는 경우)는 is_active = true.

`is_financial_distress = false`(예: "코스닥시장 이전상장" 등 형식적 사유, ADR-005 분류
기준)만 있는 stock_code는 제외했다 - 시장 이전 같은 형식적 사유는 지금도 정상 거래
중인 경우가 많아, 그대로 포함하면 멀쩡한 대형주를 잘못 비활성 처리할 위험이 있었다.

## 선택
mart.company 적재 시 is_active를 다음과 같이 판정한다:

```
is_active = stock_code가 mart.delisted_company에서
            is_financial_distress = true인 이벤트로 한 번이라도 등장했으면 false,
            아니면 true
```

corpCode.xml 3,922건(stock_code 보유) 적용 결과: is_active=true 2,904건, false 1,018건.
false 표본 육안 검증(LG생명과학→LG화학 흡수합병, LG파워콤→LG유플러스 흡수합병,
NH농협증권→NH투자증권 통합, 소진된 선박투자/SPAC류 등) - 전부 실제 폐지·합병 법인으로
오탐 없음을 확인.

## 선택 이유
mart.delisted_company는 1999년부터 KIND 상장폐지현황을 전수 수집한 데이터로(devlog
2026-07 시리즈), corpCode.xml의 stock_code 필드보다 "실제로 폐지됐는가"에 대해 훨씬
신뢰할 수 있는 근거다. is_financial_distress 플래그(ADR-005에서 이미 검증된 키워드
분류 기준)를 재사용해 형식적 사유를 걸러내면 추가 데이터 수집 없이도 안전하게 판정할
수 있었다.

## 트레이드오프
실질부실로 폐지됐다가 나중에 같은 stock_code로 재상장해 지금 다시 거래 중인 경우(예:
진로산업->JS전선처럼 폐지-재상장-재폐지가 반복되는 패턴이 폐지 이전에 재상장 단계에서
멈췄다면)를 이 로직은 구분하지 못한다 - "재상장" 이벤트 자체를 수집한 적이 없어서다.
현재는 이런 사례가 있어도 is_active=false로 잘못 남는다.

근본적으로 해결하려면 KIND의 "현재 상장종목현황"처럼 활성 종목을 직접 알려주는 긍정
목록(positive list)을 별도로 수집해 대조해야 한다. 이번 스코프에서는 제외하고 후속
과제로 남긴다.
