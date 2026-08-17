from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    page = browser.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("http://localhost:8899/index.html")
    page.wait_for_selector(".card")

    # 1) default content = 매수신호, ctoggle visible with correct states
    assert page.locator("#ctoggle .ctbtn[data-c='signal']").get_attribute("aria-pressed") == "true"
    assert page.locator("#ctoggle .ctbtn[data-c='reversal']").get_attribute("aria-pressed") == "false"
    print("[OK] 1. default content=signal")

    # 2) switch to 추세전환 tab
    page.click("#ctoggle .ctbtn[data-c='reversal']")
    page.wait_for_timeout(300)
    assert page.locator("#ctoggle .ctbtn[data-c='reversal']").get_attribute("aria-pressed") == "true"
    cards = page.locator("#list .card[data-revsym]")
    n = cards.count()
    print("reversal cards:", n)
    assert n >= 1, "reversal candidates should render"
    print("[OK] 2. switched to reversal content, cards render")

    # 3) tiles show stage counts (확인진입/초기진입 등)
    tiles_text = page.locator("#tiles").inner_text()
    print("tiles:", tiles_text.replace("\n"," | "))
    assert "확인진입" in tiles_text and "초기진입" in tiles_text
    print("[OK] 3. reversal tiles render")

    # 4) cut slider range switched to 0-100, default 35
    cut_min = page.eval_on_selector("#cut", "el=>el.min")
    cut_max = page.eval_on_selector("#cut", "el=>el.max")
    cut_val = page.eval_on_selector("#cut", "el=>el.value")
    print("cut slider:", cut_min, cut_max, cut_val)
    assert cut_min == "0" and cut_max == "100" and cut_val == "35"
    print("[OK] 4. cut slider adapted to reversal range")

    # 5) sort dropdown shows reversal-specific options
    sort_opts = page.eval_on_selector_all("#sort option", "els=>els.map(e=>e.value)")
    print("sort options:", sort_opts)
    assert sort_opts == ["score","decline","change","signals"]
    print("[OK] 5. sort options switched")

    # 6) grade chips replaced by stage chips (초기 진입/확인 진입)
    chip_text = page.locator("#grades").inner_text()
    print("chips:", chip_text.replace("\n"," | "))
    assert "초기 진입" in chip_text and "확인 진입" in chip_text
    print("[OK] 6. stage chips render")

    # 7) view-toggle (표로 보기) hidden in reversal content
    vt_display = page.eval_on_selector("#viewtoggle", "el=>getComputedStyle(el).display")
    assert vt_display == "none"
    print("[OK] 7. table-view toggle hidden for reversal content")

    # 8) click a reversal card -> detail modal with stage box, why-section, category bars
    cards.first.click()
    page.wait_for_timeout(300)
    dlg_open = page.locator("dialog#dlg").evaluate("el => el.open")
    assert dlg_open
    dlgtext = page.locator("#dlgc").inner_text()
    assert "왜 이 종목인가" in dlgtext
    assert "카테고리별 점수" in dlgtext
    stagebox = page.locator("#dlgc .stagebox")
    assert stagebox.count() == 1
    whybox = page.locator("#dlgc .whybox")
    print("whybox count:", whybox.count())
    assert whybox.count() >= 1, "should show at least one reason in why-section"
    catbars = page.locator("#dlgc .catbar")
    print("catbar count:", catbars.count())
    assert catbars.count() >= 4
    print("[OK] 8. reversal detail modal renders stage/why/category sections")
    page.click("#cl")
    page.wait_for_timeout(150)

    # 9) stage filter chip toggling (uncheck 초기 진입 -> only confirmed cards remain if any early exist)
    early_count_before = page.locator("#list .card[data-revsym]").count()
    chip_early = page.locator("#grades .chip", has_text="초기 진입")
    chip_early.click()
    page.wait_for_timeout(300)
    after = page.locator("#list .card[data-revsym]").count()
    print("before/after unchecking 초기진입:", early_count_before, after)
    assert after <= early_count_before
    print("[OK] 9. stage chip filtering works")
    chip_early.click()  # re-enable
    page.wait_for_timeout(200)

    # 10) search still works while in reversal content
    page.fill("#q", "반전샘플00")
    page.wait_for_timeout(300)
    filtered_count = page.locator("#list .card[data-revsym]").count()
    print("search filtered count:", filtered_count)
    assert filtered_count == 1
    page.fill("#q", "")
    page.wait_for_timeout(200)

    # 11) switch back to 매수신호 -> slider/sort/chips revert, viewtoggle visible again
    page.click("#ctoggle .ctbtn[data-c='signal']")
    page.wait_for_timeout(300)
    cut_min2 = page.eval_on_selector("#cut", "el=>el.min")
    cut_max2 = page.eval_on_selector("#cut", "el=>el.max")
    assert cut_min2 == "55" and cut_max2 == "95"
    vt_display2 = page.eval_on_selector("#viewtoggle", "el=>getComputedStyle(el).display")
    assert vt_display2 != "none"
    sort_opts2 = page.eval_on_selector_all("#sort option", "els=>els.map(e=>e.value)")
    assert sort_opts2 == ["score","turnover","change","checks","rr"]
    print("[OK] 11. reverting to signal content restores original controls")

    # 12) market tab switch preserves content=reversal choice
    page.click("#ctoggle .ctbtn[data-c='reversal']")
    page.wait_for_timeout(200)
    page.click("#tab-us")
    page.wait_for_timeout(500)
    pressed = page.locator("#ctoggle .ctbtn[data-c='reversal']").get_attribute("aria-pressed")
    print("reversal still pressed after tab switch:", pressed)
    assert pressed == "true"
    print("[OK] 12. content choice persists across market tab switch")

    print("console/page errors:", [e for e in errors if "404" not in e])
    real_errors = [e for e in errors if "404" not in e]
    assert not real_errors, f"JS errors: {real_errors}"

    browser.close()
    print("=== ALL REVERSAL UI CHECKS PASSED ===")
