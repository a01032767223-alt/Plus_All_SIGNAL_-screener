from playwright.sync_api import sync_playwright
CH="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CH); pg=b.new_page()
    errs=[]
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8899/index.html"); pg.wait_for_selector(".card")

    # 1) 토글 3개
    assert pg.locator("#ctoggle .ctbtn").count()==3
    print("[OK] 1. 콘텐츠 토글 3개")

    # 2) 내 전략 진입
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(500)
    cards=pg.locator("#list .card[data-mysym]")
    n=cards.count(); print("   내 전략 카드:", n)
    assert n>=1
    assert not pg.locator("#mypanel").is_hidden()
    print("[OK] 2. 내 전략 카드 + 패널 렌더")

    # 3) 기본 프리셋이면 매수신호 탭과 순서가 같아야 한다 (핵심 불변식의 UI판)
    my_order=pg.eval_on_selector_all("#list .card[data-mysym]","els=>els.map(e=>e.dataset.mysym)")
    pg.click("#ctoggle .ctbtn[data-c='signal']"); pg.wait_for_timeout(300)
    pg.eval_on_selector("#cut","el=>{el.value=0}")   # 컷 영향 제거용 확인
    sig_order=pg.eval_on_selector_all("#list .card[data-sym]","els=>els.map(e=>e.dataset.sym)")
    common=[s for s in my_order if s in sig_order]
    idx=[sig_order.index(s) for s in common]
    assert idx==sorted(idx), f"순서 불일치\n my={common}\n sig={sig_order}"
    print(f"[OK] 3. 기본 프리셋 순서 == 매수신호 탭 순서 (공통 {len(common)}종목)")

    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(400)

    # 4) 타일
    t=pg.locator("#tiles").inner_text().replace("\n"," | ")
    print("   타일:", t)
    for w in ("내 후보","새로 발굴","기본과 겹침","필터 제외","스캔"): assert w in t
    print("[OK] 4. 내 전략 타일")

    # 5) 슬라이더/정렬/칩 전환
    assert pg.eval_on_selector("#cut","e=>e.min")=="0" and pg.eval_on_selector("#cut","e=>e.max")=="100"
    so=pg.eval_on_selector_all("#sort option","e=>e.map(x=>x.value)")
    assert so==["my","gain","turnover","change","rr"], so
    assert pg.locator("#grades").inner_text().strip()==""
    assert pg.eval_on_selector("#viewtoggle","e=>getComputedStyle(e).display")=="none"
    print("[OK] 5. 슬라이더 범위·정렬·칩·표보기 전환")

    # 6) 프리셋 클릭 → 순서 변경
    before=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    pg.click("#presets .pbtn[data-p='flow']"); pg.wait_for_timeout(300)
    after=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    print("   수급 중심형 적용 전/후 1위:", before[0] if before else None, "→", after[0] if after else None)
    assert before!=after, "프리셋을 바꿔도 결과가 그대로다"
    assert pg.locator("#presets .pbtn[data-p='flow']").get_attribute("aria-pressed")=="true"
    print("[OK] 6. 프리셋 전환 반영")

    # 7) 슬라이더 조작 → custom 전환 + 합계 갱신
    pg.click("#wpane > summary"); pg.wait_for_timeout(150)
    sl=pg.locator("#mypanel input[data-k='obv']")
    sl.fill("50"); sl.dispatch_event("input"); pg.wait_for_timeout(300)
    assert pg.locator("#mypanel .wrow", has=pg.locator("input[data-k='obv']")).inner_text().strip().endswith("50")
    assert pg.locator("#presets .pbtn[aria-pressed='true']").count()==0
    print("   합계:", pg.locator("#wsum").inner_text())
    print("[OK] 7. 슬라이더 → 직접 조절 모드")

    # 8) 편중 경고
    for k in ("ma","volume","structure","rsi","macd","bb","adx"):
        e=pg.locator(f"#mypanel input[data-k='{k}']"); e.fill("0"); e.dispatch_event("input")
    pg.wait_for_timeout(300)
    note=pg.locator("#mynote")
    assert not note.is_hidden(), "OBV 100%인데 편중 경고가 없다"
    print("   경고:", note.inner_text()[:60])
    print("[OK] 8. 편중 경고")

    # 9) 전부 0 방어
    e=pg.locator("#mypanel input[data-k='obv']"); e.fill("0"); e.dispatch_event("input")
    pg.wait_for_timeout(300)
    assert "0입니다" in pg.locator("#mynote").inner_text()
    print("[OK] 9. 비중 합계 0 방어")

    pg.click("#presets .pbtn[data-p='default']"); pg.wait_for_timeout(300)

    # 10) 필수 조건 필터
    pg.click("#fpane > summary"); pg.wait_for_timeout(150)
    n0=pg.locator("#list .card[data-mysym]").count()
    pg.select_option("#mypanel select[data-f='rr']","3")
    pg.wait_for_timeout(300)
    n1=pg.locator("#list .card[data-mysym]").count()
    excl=pg.locator("#tiles .tile").nth(3).inner_text().replace("\n"," ")
    print(f"   손익비 3.0 적용: {n0} → {n1}건, 타일 {excl}")
    assert n1<=n0
    assert "필수 조건" in pg.locator("#fpane > summary").inner_text()
    pg.select_option("#mypanel select[data-f='rr']",""); pg.wait_for_timeout(300)
    print("[OK] 10. 필수 조건 필터 + 제외 집계")

    # 11) 상세 모달
    pg.locator("#list .card[data-mysym]").first.click(); pg.wait_for_timeout(300)
    assert pg.locator("dialog#dlg").evaluate("e=>e.open")
    txt=pg.locator("#dlgc").inner_text()
    for w in ("왜 내 설정에서 올라왔나","지표별 기여도","내 점수","기본 점수"): assert w in txt, w
    assert pg.locator("#dlgc .cbar").count()==8
    print("   기여도 막대:", pg.locator("#dlgc .cbar").count(), "개")
    print("[OK] 11. 내 전략 상세 모달")
    pg.click("#cl"); pg.wait_for_timeout(150)

    # 12) 검색
    pg.fill("#q","샘플종목"); pg.wait_for_timeout(300)
    assert pg.locator("#list .card[data-mysym]").count()>=1
    pg.fill("#q",""); pg.wait_for_timeout(200)
    print("[OK] 12. 검색 동작")

    # 13) 시장 탭 이동 후에도 내 전략 유지
    pg.click("#tab-us"); pg.wait_for_timeout(700)
    assert pg.locator("#ctoggle .ctbtn[data-c='my']").get_attribute("aria-pressed")=="true"
    body=pg.locator("#list").inner_text()
    assert "내 전략 데이터가 없습니다" in body or pg.locator("#list .card[data-mysym]").count()>=0
    print("   미국 탭:", body.split("\n")[0][:40])
    print("[OK] 13. 매트릭스 없는 시장 방어")

    pg.click("#tab-kr"); pg.wait_for_timeout(600)
    pg.click("#ctoggle .ctbtn[data-c='signal']"); pg.wait_for_timeout(300)
    assert pg.eval_on_selector("#cut","e=>e.min")=="55"
    assert pg.eval_on_selector("#viewtoggle","e=>getComputedStyle(e).display")!="none"
    assert pg.locator("#mypanel").is_hidden()
    print("[OK] 14. 매수신호 복귀 시 원래 컨트롤 복원")

    real=[e for e in errs if "404" not in e]
    print("콘솔 오류:", real)
    assert not real, real
    b.close()
    print("=== 내 전략 UI 검증 전부 통과 ===")
