# 대시보드 E2E 점검

`docs/index.html`은 브라우저에서만 검증할 수 있어 pytest와 따로 둡니다.
CI에는 넣지 않았습니다(브라우저 설치 비용). 화면을 고친 뒤 로컬에서 직접 돌리세요.

```bash
python -m screener.run --market demo          # 합성 데이터 생성
cd docs && python -m http.server 8899 &       # 정적 서버
python tests/e2e/my_strategy.py               # 내 전략 탭 (14항목)
python tests/e2e/my_strategy_reversal.py      # 내 전략 · 추세전환 기준 (13항목)
python tests/e2e/my_strategy_save.py          # 저장 · 공유 (7항목)
python tests/e2e/reversal_tab.py              # 추세전환 탭 (12항목)
python tests/e2e/guide.py                     # 가이드 탭 문안
python tests/e2e/my_strategy_perf.py          # 실전 규모(1,800종목) 성능·360px 레이아웃
```

데모 데이터는 30종목뿐이라 카드 렌더 병목이 드러나지 않습니다.
성능 점검은 실전 규모로 부풀린 뒤에 하세요.

```bash
python tests/e2e/make_big_matrix.py            # 1,800종목으로 부풀리기
python tests/e2e/my_strategy_perf.py           # 측정
python tests/e2e/make_big_matrix.py --restore  # 원래 데모 데이터로 복구
```

측정 기준선(이 컨테이너, 360×740):
재계산+정렬 1.9ms · 슬라이더 1틱 → 화면 반영 23ms.
슬라이더를 끄는 동안에는 카드를 20장만 그리고, 손을 뗀 뒤 100장으로 복원합니다.

스크립트 상단의 `CH` 경로는 이 환경의 크로미움 위치입니다. 다른 환경에서는
`p.chromium.launch()`로 바꾸거나 설치된 경로로 고치세요.

가장 중요한 항목은 **"기본 프리셋 순서 == 매수신호 탭 순서"** 입니다.
이게 깨지면 내 전략 탭의 점수를 믿을 수 없습니다
(파이썬 쪽 대응 검증: `tests/test_custom.py::test_default_weights_reproduce_official_ranking`).
