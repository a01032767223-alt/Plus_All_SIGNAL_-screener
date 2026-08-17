from playwright.sync_api import sync_playwright
CH="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
U="http://localhost:8899/index.html"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CH); pg=b.new_context().new_page()
    errs=[]
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(U); pg.wait_for_selector(".card")
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(500)

    # 1) 기준 선택 UI
    assert pg.locator("#basesel .pbtn").count()==2
    assert pg.locator("#basesel .pbtn[data-b='signal']").get_attribute("aria-pressed")=="true"
    print("[OK] 1. 기준 선택 버튼")

    # 2) 추세전환 기준으로 전환
    pg.click("#basesel .pbtn[data-b='reversal']"); pg.wait_for_timeout(500)
    assert pg.locator("#basesel .pbtn[data-b='reversal']").get_attribute("aria-pressed")=="true"
    cards=pg.locator("#list .card[data-myrevsym]")
    print("   추세전환 기준 카드:", cards.count())
    assert cards.count()>=1
    print("[OK] 2. 추세전환 기준 카드 렌더")

    # 3) 프리셋이 6카테고리용으로 바뀜
    labels=pg.eval_on_selector_all("#presets .pbtn","e=>e.map(x=>x.textContent.trim())")
    print("   프리셋:", labels)
    assert "다이버전스형" in labels and "거래량 소진형" in labels
    assert "돌파형" not in labels
    print("[OK] 3. 추세전환 프리셋으로 교체")

    # 4) 슬라이더 6개
    pg.click("#wpane > summary"); pg.wait_for_timeout(150)
    sl=pg.eval_on_selector_all("#mypanel input[type=range]","e=>e.map(x=>x.dataset.k)")
    print("   슬라이더:", sl)
    # 코인 특화는 주식에 없는 카테고리라 국내/미국 탭에서는 숨긴다
    assert sl==["structure","volume","momentum","ma","volatility"], sl
    assert "카테고리 5개" in pg.locator("#wpane > summary").inner_text()
    print("[OK] 4. 카테고리 슬라이더 (주식은 코인 특화 숨김)")

    # 5) 컷·정렬 옵션 전환
    assert pg.eval_on_selector("#cut","e=>e.value")=="35"
    so=pg.eval_on_selector_all("#sort option","e=>e.map(x=>x.value)")
    assert so==["my","gain","decline","change"], so
    print("[OK] 5. 최소 점수 35 · 추세전환 정렬 옵션")

    # 6) 타일이 단계 집계로
    t=pg.locator("#tiles").inner_text().replace("\n"," | ")
    print("   타일:", t)
    assert "확인진입" in t and "초기진입" in t and "하락종목" in t
    print("[OK] 6. 추세전환 타일")

    # 7) 기본 배점 = 추세전환 탭 순서 (컷 이상 구간에서)
    my=pg.eval_on_selector_all("#list .card[data-myrevsym]","e=>e.map(x=>x.dataset.myrevsym)")
    pg.click("#ctoggle .ctbtn[data-c='reversal']"); pg.wait_for_timeout(400)
    # 추세전환 탭은 '확인진입 먼저' 정렬이라 점수순으로 맞춰 비교한다
    pg.select_option("#sort","score"); pg.wait_for_timeout(300)
    rev=pg.eval_on_selector_all("#list .card[data-revsym]","e=>e.map(x=>x.dataset.revsym)")
    common=[s for s in my if s in rev]
    idx=[rev.index(s) for s in common]
    print(f"   공통 {len(common)}종목 순서 일치:", idx==sorted(idx))
    assert idx==sorted(idx), f"{common} / {rev}"
    print("[OK] 7. 기본 배점 순서 == 추세전환 탭 순서")

    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(500)

    # 8) 프리셋 바꾸면 순서 변함
    before=pg.eval_on_selector_all("#list .card[data-myrevsym]","e=>e.map(x=>x.dataset.myrevsym)")
    pg.click("#presets .pbtn[data-p='divergence']"); pg.wait_for_timeout(400)
    after=pg.eval_on_selector_all("#list .card[data-myrevsym]","e=>e.map(x=>x.dataset.myrevsym)")
    print("   1위:", (before or [None])[0], "→", (after or [None])[0])
    assert before!=after
    print("[OK] 8. 추세전환 프리셋 반영")

    # 9) 컷을 낮추면 기본 35점 컷 미달 종목이 새로 나온다
    pg.click("#presets .pbtn[data-p='default']"); pg.wait_for_timeout(300)
    n0=pg.locator("#list .card[data-myrevsym]").count()
    pg.eval_on_selector("#cut","e=>{e.value=0;e.dispatchEvent(new Event('input'))}")
    pg.wait_for_timeout(400)
    n1=pg.locator("#list .card[data-myrevsym]").count()
    print(f"   컷 35 → 0: {n0} → {n1}건")
    assert n1>=n0
    print("[OK] 9. 컷 조절로 후보 확장 (매트릭스가 컷 이전 값을 담고 있음)")

    # 10) 상세 모달
    pg.locator("#list .card[data-myrevsym]").first.click(); pg.wait_for_timeout(400)
    txt=pg.locator("#dlgc").inner_text()
    for w in ("왜 내 설정에서 올라왔나","카테고리별 기여도","내 점수","기본 배점"): assert w in txt, w
    assert pg.locator("#dlgc .stagebox").count()==1
    print("   기여도 막대:", pg.locator("#dlgc .cbar").count(), "개")
    assert pg.locator("#dlgc .cbar").count()==5   # 주식은 코인 카테고리 제외
    print("[OK] 10. 추세전환 기준 상세 모달")
    pg.click("#cl"); pg.wait_for_timeout(150)

    # 11) 기준 선택이 새로고침 후에도 유지
    pg.reload(); pg.wait_for_selector(".card")
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(700)
    assert pg.locator("#basesel .pbtn[data-b='reversal']").get_attribute("aria-pressed")=="true"
    assert pg.locator("#list .card[data-myrevsym]").count()>=1
    print("[OK] 11. 기준 선택 복원")

    # 12) 추세전환 기준 공유 링크
    pg.click("#spane > summary"); pg.wait_for_timeout(150)
    pg.click("#sshare"); pg.wait_for_timeout(300)
    link=pg.eval_on_selector("#slink","e=>e.value")
    assert "#s=1.reversal." in link, link
    print("   링크:", link.split("#")[1][:50])
    pg2=b.new_context().new_page()
    e2=[]; pg2.on("pageerror", lambda e: e2.append(str(e)))
    pg2.goto(link); pg2.wait_for_selector(".card"); pg2.wait_for_timeout(900)
    assert pg2.locator("#basesel .pbtn[data-b='reversal']").get_attribute("aria-pressed")=="true"
    assert pg2.locator("#list .card[data-myrevsym]").count()>=1
    assert not e2, e2
    print("[OK] 12. 추세전환 기준 공유 링크 재현")

    # 13) 매수신호로 되돌리기
    pg.click("#basesel .pbtn[data-b='signal']"); pg.wait_for_timeout(400)
    assert pg.locator("#list .card[data-mysym]").count()>=1
    assert pg.eval_on_selector("#cut","e=>e.value")=="60"
    print("[OK] 13. 매수신호 기준 복귀")

    real=[e for e in errs if "404" not in e]
    assert not real, real
    b.close(); print("=== 추세전환 커스텀 검증 전부 통과 ===")
