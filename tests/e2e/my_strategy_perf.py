from playwright.sync_api import sync_playwright
CH="/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=CH)
    pg=b.new_context(viewport={"width":360,"height":740},device_scale_factor=2).new_page()
    e=[]; pg.on("pageerror",lambda x:e.append(str(x)))
    pg.on("console",lambda m:e.append(m.text) if m.type=="error" else None)
    pg.goto("http://localhost:8899/index.html"); pg.wait_for_selector(".card")
    pg.click("#ctoggle .ctbtn[data-c='my']"); pg.wait_for_timeout(900)

    scanned=pg.locator("#tiles .tile").last.inner_text().split("\n")[0]
    print("스캔 종목:", scanned)
    n=pg.locator("#list .card[data-mysym]").count()
    print("렌더 카드:", n, "(상한 100)")
    assert n<=100

    # 순수 재계산 시간 (필터·정렬 포함, 렌더 제외)
    t=pg.evaluate("""()=>{
      const d=state.data[state.market], m=state.matrix[state.market];
      filteredMy(d,m);
      const t0=performance.now();
      for(let i=0;i<50;i++){ state.w.obv=20+(i%7); filteredMy(d,m); }
      return (performance.now()-t0)/50;
    }""")
    print(f"재계산+정렬 1회: {t:.2f} ms")
    assert t<25, t

    # 슬라이더 → 화면 반영까지 (렌더 포함)
    full=pg.evaluate("""async ()=>{
      const el=document.querySelector("#mypanel input[data-k='rsi']");
      const t0=performance.now();
      for(let i=0;i<20;i++){
        el.value=String(5+i); el.dispatchEvent(new Event('input',{bubbles:true}));
        await new Promise(r=>requestAnimationFrame(r));
      }
      return (performance.now()-t0)/20;
    }""")
    print(f"슬라이더 1틱 → 화면 반영: {full:.1f} ms")
    assert full<120, full

    # 드래그 중에는 적게 그리고, 손을 뗀 뒤 전체로 복원되는가
    during=pg.locator("#list .card[data-mysym]").count()
    print("드래그 중 카드:", during)
    assert during<=20, during
    pg.wait_for_timeout(400)
    after=pg.locator("#list .card[data-mysym]").count()
    print("드래그 후 카드:", after)
    assert after==100, after
    body=pg.locator("#list").inner_text()
    assert "상위 100건만 표시" in body, body[:200]
    print("[OK] 드래그 중 축소 → 손 뗀 뒤 전체 복원 + 잘라낸 건수 안내")

    # 360px에서 토글 3버튼이 줄바꿈 없이 한 줄인가
    tops=pg.eval_on_selector_all("#ctoggle .ctbtn","e=>e.map(x=>Math.round(x.getBoundingClientRect().top))")
    print("토글 버튼 top:", tops)
    assert len(set(tops))==1, "360px에서 토글이 줄바꿈됨"
    print("[OK] 360px 한 줄 유지")

    pg.click("#wpane > summary"); pg.wait_for_timeout(200)
    pg.screenshot(path="/tmp/my_360.png", full_page=False)
    assert not [x for x in e if "404" not in x], e
    b.close(); print("=== 실전 규모 성능·레이아웃 검증 통과 ===")
