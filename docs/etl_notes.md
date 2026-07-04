# ETL 노트

raw → mart 변환 단계에서 반드시 지켜야 할 데이터 정제 규칙을 기록한다.
스키마 수준의 제약 근거는 [ADR-001](adr/ADR-001-raw-mart-pk-strategy.md) 참고.

## account_id 처리 원칙

DART API 응답에서 `account_id`가 비어 있거나(`''`, `NULL`) `-` 같은 비정상 값으로
오는 경우가 있다.

`mart.financial_statement.account_id`는 `NOT NULL`이며
`(corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, ord)` UNIQUE 제약에도
포함되어 있으므로, ETL 단계에서 다음을 반드시 지킨다.

- `account_id`가 없거나(NULL/빈 문자열) `-` 등 비정상 값이면 `'UNASSIGNED'` 같은
  명확한 placeholder로 치환한 뒤 저장한다.
- 빈 문자열이나 NULL을 그대로 두면 안 된다. 이를 그대로 두면 이후 재적재(upsert) 시
  UNIQUE 제약이 사실상 무력화되어 중복 데이터가 쌓일 위험이 있다.
- raw 테이블(`raw.dart_financial_statement`)은 원본을 가공 없이 그대로 적재하므로
  이 치환은 raw → mart 변환 스크립트에서만 적용한다.
