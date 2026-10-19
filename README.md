# 독립 주식 전략 검증 연구 플랫폼

**실제 주문이 없는 연구용 플랫폼입니다. 합격 전략이 없으면 투자하지 않습니다.** `stock-ai`와 연동하지 않으며 미래 주가 예측값, 증권사 계좌, API 비밀키를 사용하지 않습니다.

Python 3.12 표준 라이브러리로 구현한 체결·회계 엔진과 한국어 모바일 대시보드입니다. 현재 버전은 기능 검증용 구현입니다. **기업행동·과거 구성종목 증거의 완전한 검증이 아직 없어 실제 전략의 자동 합격을 차단합니다.** 실제 데이터를 확보하지 못한 경우 수익률을 만들지 않습니다. 상세 실행 결과와 한계는 [개발 결과](docs/RESULTS.md)를 확인하세요.

## 사용 흐름

1. 실제 시점의 데이터와 이용 권한을 확보합니다. OHLCV뿐 아니라 환율, 구성종목, 상장폐지·분할·배당, 재무 발표 시각이 필요합니다.
2. 엔진의 수기 원장·미체결·미래 정보 차단 테스트를 실행하고 현재 소스와 연결된 테스트 증거를 만듭니다.
3. 실행 전 고정된 합격 기준으로 개발 → 워크포워드 → 최종 독립 검증을 수행합니다.
4. 모바일 화면에서 합격/불합격/데이터 부족, 위험·비용·실패 원인과 데이터 출처를 확인합니다. 실전 자동 활성화 기능은 없습니다.

## 설치와 테스트

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-build-isolation -e .
.venv/bin/python -m pytest -q
.venv/bin/python -m stock_validator certify
```

앱 실행에는 외부 패키지가 필요하지 않습니다. 테스트/빌드 도구는 `requirements-dev.txt`에 고정했습니다. 인증 파일은 해당 코드와 테스트의 해시가 달라지면 무효가 됩니다. 통과한 테스트가 시장 수익성을 보장하지는 않습니다. CI도 실제 테스트를 실행하며 테스트 결과를 보관합니다.

## 실제 데이터

```bash
.venv/bin/python -m stock_validator collect \
  --symbols 005930.KS 000660.KS --benchmark '^KS11' \
  --start 2010-01-01 --end 2026-10-18 --output data/real-yahoo

.venv/bin/python -m stock_validator collect \
  --symbols SPY --benchmark '^GSPC' --start 2010-01-01 \
  --end 2026-10-18 --output data/real-us

.venv/bin/python -m stock_validator collect \
  --provider stooq --symbols spy.us --start 2010-01-01 \
  --end 2026-10-18 --output data/real-stooq

.venv/bin/python -m stock_validator collect-fx \
  --start 2010-01-01 --end 2026-10-18 --output data/fred-observed-fx.csv
```

날짜는 실제 완료된 거래일까지 지정하세요. 수집기는 기존 디렉터리를 덮어쓰지 않습니다. 실패 시 `collection.json`에 실제 실패를 저장하고 합성 데이터로 대체하지 않습니다. 원본 응답과 CSV·매니페스트 SHA-256을 보존합니다.

공개 Yahoo/Stooq 응답은 비공식·약관 확인이 필요한 선택적 어댑터입니다. 무료 공개 접근이 데이터 이용·재배포 권한을 보장하지 않습니다. FRED 관측 환율은 공식 자료이나 공개 가능 시각과 실제 환전 가격은 별도 검증이 필요합니다. USD 자료에 실제 시점 FX가 없으면 원화 성과를 계산하지 않습니다. KRX·SEC 연결은 이번 버전에서 완성되지 않았습니다. 분봉이나 10년 이상 데이터를 실제 확보했다고 가정하지 않습니다.

사용자 보유 CSV의 형식·PIT 재무/업종 입력·데이터 품질 한계는 [데이터 문서](docs/DATA.md)에 설명되어 있습니다. 현재 종목만 다운로드하면 생존자 편향이 남습니다. 매니페스트의 `true` 선언만으로 검증 신뢰도가 높아지지 않습니다.

## 검증과 화면

```bash
.venv/bin/python -m stock_validator inspect-data --data data/real-kr
.venv/bin/python -m stock_validator validate --data data/real-kr \
  --output reports/latest --policy config/acceptance.json \
  --certificate reports/engine-certificate.json
.venv/bin/python -m stock_validator serve --reports reports/latest
```

현재 클라우드에서 준비한 한국 자료는 `data/real-kr`에 있습니다. Git에는 원본 가격을 재배포하지 않으므로 새 체크아웃에서는 수집·이용권한 확인이 필요하며 `--data`를 실제 새 저장 위치로 바꾸세요.

합격 기준은 [acceptance.json](config/acceptance.json)에 저장되어 있습니다. 보고서는 정책·데이터·소스의 해시와 실행 기간을 기록합니다. 개발 시점에 후보를 선택하고 이후 기간에서는 다시 최적화하지 않습니다. 최종 검증의 재사용은 저장소 루트 `.research_state/`의 기록으로 제한합니다. 이 폴더를 보존하세요. 연구자가 기록을 고의 삭제하는 행위까지 막는 중앙 승인 시스템은 아닙니다.

앱은 기본적으로 같은 기기의 로컬 인터페이스에만 바인딩됩니다. 공개 배포·모바일 외부 접속은 별도 호스팅 설정이 필요합니다. 대시보드는 읽기 전용이며 데이터 수집/주문/원격 명령 실행 API가 없습니다. 보고서가 없으면 수익률 없이 미검증 상태를 표시합니다.

화면 기능 확인에 한해 다음 **합성 데모**를 사용할 수 있습니다. 이 결과를 실제 전략 성과로 해석하지 마세요.

```bash
.venv/bin/python -m stock_validator demo --output data/synthetic
.venv/bin/python -m stock_validator validate --data data/synthetic \
  --output reports/synthetic --certificate reports/engine-certificate.json
.venv/bin/python -m stock_validator serve --reports reports/synthetic
```

## 구현과 위험 가정

- [여섯 전략](docs/STRATEGIES.md): A 추세돌파, B 눌림목, C 과매도, D PIT 실적/전망, E 폭락 우량주 회복, F 업종 상대강도. A~D/F는 1~5봉, E는 별도 장기 평가입니다.
- 다음 봉 시가 진입, 고정/ATR 손절, 고정/손익비 목표, 부분 익절, 전봉 기준 후행손절, 시간 청산. 동일 봉 순서 불명은 손절 우선입니다.
- 거래량 참여율을 적용하는 일봉 근사 체결입니다. 시가 호가·분봉·주문장 재현이 아니므로 체결 정확도를 보장하지 않습니다. 손절·목표가는 슬리피지를 적용하는 시장 청산 트리거입니다.
- KRW 원장에 수수료·매도세·슬리피지·환전 비용을 기록하고 현금+평가액 및 실현+미실현 손익을 대조합니다. 기본 비용은 연구 시나리오이며 현행 시장별 세법/수수료표가 아닙니다. 실사용 전 날짜별 세율을 별도로 검증해야 합니다.
- 초기 100만원, 비용 포함 계획손실 1만원/거래, 당일 순자산 손실 2만원이면 신규 진입 중단, 고점 대비 손실 5만원이면 전략 중단·재검토, 초기자금 대비 계좌 전체 손실 100만원 한도를 별도로 관리합니다. 신용·레버리지·공매도는 지원하지 않습니다.
- **갭·거래정지·유동성 부족으로 실제 손실은 손절과 위 한도를 초과할 수 있습니다.** 중단 신호가 체결을 보장하지 않습니다. 끝까지 팔리지 않은 주식은 마지막 관측가로 평가하고 향후 매도비용 미차감을 경고합니다.

[설계](docs/ARCHITECTURE.md), [1차 검토](docs/review-1-design.md), [2차 검토](docs/review-2-engine.md), [3차 검토](docs/review-3-system.md)에서 해결 사항과 미해결 제한을 구분합니다. 검증을 통과해도 미래 수익을 보장하지 않습니다.
