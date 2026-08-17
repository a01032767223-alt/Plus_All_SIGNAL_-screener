from playwright.sync_api import sync_playwright
CH="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
U="http://localhost:8899/index.html"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CH); ctx=b.new_context(); pg=ctx.new_page()
    errs=[]
    pg.on("console", lambda m: errs.append(m.text) if m.type=="error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(U); pg.wait_for_selector(".card")
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(500)

    # 1) 프리셋 선택 → 새로고침 후 자동 복원
    pg.click("#presets .pbtn[data-p='breakout']"); pg.wait_for_timeout(300)
    before=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    pg.reload(); pg.wait_for_selector(".card")
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(600)
    after=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    assert before==after, f"복원 실패\n{before}\n{after}"
    print("[OK] 1. 마지막 설정이 새로고침 후 복원됨")

    # 2) 슬롯 저장
    pg.click("#spane > summary"); pg.wait_for_timeout(150)
    pg.fill("#sname","내 조합 A"); pg.fill("#smemo","거래량이 먼저 터진 종목")
    pg.click("#ssave"); pg.wait_for_timeout(300)
    assert pg.locator("#mypanel .pbtn[data-slot]").count()==1
    assert "1개 저장됨" in pg.locator("#spane > summary").inner_text()
    print("[OK] 2. 슬롯 저장 + 메모")

    # 3) 다른 프리셋으로 갔다가 슬롯 복원
    pg.click("#presets .pbtn[data-p='pullback']"); pg.wait_for_timeout(300)
    diff=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    pg.click("#spane > summary"); pg.wait_for_timeout(120)
    pg.click("#mypanel .pbtn[data-slot='0']"); pg.wait_for_timeout(400)
    back=pg.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    assert back==before, "슬롯 복원 실패"
    assert diff!=before
    print("[OK] 3. 저장한 조합 불러오기")

    # 4) 공유 링크 생성
    pg.click("#spane > summary"); pg.wait_for_timeout(120)
    pg.click("#sshare"); pg.wait_for_timeout(300)
    link=pg.eval_on_selector("#slink","e=>e.value")
    print("   링크:", link.split("#")[1][:60])
    assert "#s=1.signal." in link
    print("[OK] 4. 공유 링크 생성")

    # 5) 다른 브라우저 컨텍스트(= 다른 사람)에서 링크 열기
    ctx2=b.new_context(); pg2=ctx2.new_page()
    e2=[]; pg2.on("pageerror", lambda e: e2.append(str(e)))
    pg2.goto(link); pg2.wait_for_selector(".card"); pg2.wait_for_timeout(900)
    assert pg2.locator("#ctoggle .ctbtn[data-c='my']").get_attribute("aria-pressed")=="true"
    shared=pg2.eval_on_selector_all("#list .card[data-mysym]","e=>e.map(x=>x.dataset.mysym)")
    assert shared==before, f"공유 결과 불일치\n{shared}\n{before}"
    assert "공유받은 전략" in pg2.locator("#mypanel").inner_text()
    assert not e2, e2
    print("[OK] 5. 공유 링크로 같은 결과 재현 + 안내 노출")

    # 6) 슬롯 삭제
    pg.click("#mypanel .pbtn[data-del='0']"); pg.wait_for_timeout(300)
    assert pg.locator("#mypanel .pbtn[data-slot]").count()==0
    print("[OK] 6. 슬롯 삭제")

    # 7) 손상된 해시 방어
    pg2.goto(U+"#s=9.signal.bad"); pg2.wait_for_selector(".card"); pg2.wait_for_timeout(700)
    assert not e2, e2
    print("[OK] 7. 모르는 버전/손상된 해시 무시")

    real=[e for e in errs if "404" not in e]
    assert not real, real
    b.close(); print("=== 저장·공유 검증 전부 통과 ===")
