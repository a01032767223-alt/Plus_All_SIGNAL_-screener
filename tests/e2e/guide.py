from playwright.sync_api import sync_playwright
CH="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CH); pg=b.new_context().new_page()
    e=[]; pg.on("pageerror", lambda x: e.append(str(x)))
    pg.on("console", lambda m: e.append(m.text) if m.type=="error" else None)
    pg.goto("http://localhost:8899/index.html"); pg.wait_for_selector(".card")
    pg.click("#tab-guide"); pg.wait_for_timeout(400)
    t=pg.locator("#screen-guide").inner_text()
    for w in ['"내 전략" 탭이란','슬라이더를 돌리기 전에','새로 발굴','등급(S·A·B·C)은 붙이지 않습니다',
              '"추세전환" 탭이란','등급의 의미','저작권','라이선스']:
        assert w in t, f"가이드에 '{w}' 없음"
    print("[OK] 가이드에 내 전략·과최적화 경고·기존 섹션 모두 노출")
    print("   가이드 섹션 수:", pg.locator("#screen-guide .g-sec, #screen-guide .warn-box").count())
    assert not [x for x in e if "404" not in x], e
    b.close(); print("=== 가이드 검증 통과 ===")
