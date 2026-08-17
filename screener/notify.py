"""알림 — 텔레그램 봇 + Gmail SMTP.

필요한 환경변수(GitHub Secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID     ← 텔레그램
  GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO  ← 이메일
  PAGES_URL                                ← 대시보드 주소 (선택)
설정되지 않은 채널은 조용히 건너뛴다.
"""
from __future__ import annotations

import html
import os
import smtplib
from email.mime.text import MIMEText

import requests

GRADE_EMOJI = {"S": "🔥", "A": "🟢", "B": "🟡", "C": "⚪"}


def _fmt_price(v: float, cur: str = "KRW") -> str:
    if cur == "USD":
        # 달러는 1000달러가 넘어도 센트를 버리면 어색해서 두 자리를 유지한다
        return f"${v:,.2f}" if v >= 1 else f"${v:,.4f}"
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


def _fmt_money(v: float, cur: str = "KRW") -> str:
    if cur == "USD":
        if v >= 1e9:
            return f"${v/1e9:.1f}B"
        if v >= 1e6:
            return f"${v/1e6:.0f}M"
        return f"${v:,.0f}"
    if v >= 1e12:
        return f"{v/1e12:.1f}조"
    if v >= 1e8:
        return f"{v/1e8:.0f}억"
    if v >= 1e4:
        return f"{v/1e4:.0f}만"
    return f"{v:,.0f}"


GRADE_ONELINE = {
    "S": "지표 거의 전부 정렬 — 이미 많이 오른 뒤일 수 있음",
    "A": "주요 지표 대부분 충족",
    "B": "절반 이상 충족 — 관찰 대상",
    "C": "일부만 충족 — 참고용",
}


def _line(it: dict, cur: str = "KRW") -> str:
    g = GRADE_EMOJI.get(it["grade"], "")
    r = it["risk"]
    head = it.get("headline", "")
    rr_warn = "" if r.get("rr_ok", True) else " ⚠손익비낮음"
    warns = it.get("warnings") or []
    warn_line = ("   ⚠ 매도·경계 신호: "
                 + html.escape(", ".join(w["label"] for w in warns)) + "\n") if warns else ""
    # 미국주식은 종목명만으로는 티커를 모르니 같이 적는다 (증권앱 검색용)
    tag = f" <code>{html.escape(str(it['symbol']))}</code>" if cur == "USD" else ""
    return (f"{g} <b>{html.escape(str(it['name']))}</b>{tag} "
            f"<code>{it['score']:.0f}점</code> {it['grade']}등급\n"
            f"   {_fmt_price(it['price'], cur)} ({it['change_pct']:+.1f}%) · "
            f"거래대금 {_fmt_money(it.get('turnover', 0), cur)} · 조건 {it['checks_passed']}/8\n"
            + (f"   ▸ {html.escape(head)}\n" if head else "")
            + warn_line
            + f"   손절 {r['stop_pct']:+.1f}% / 목표 {r['target_pct']:+.1f}% "
              f"(손익비 {r['rr']:.1f}){rr_warn}")


def _build_message(payload: dict, items: list[dict], title: str) -> str:
    url = html.escape(os.getenv("PAGES_URL", "").strip(), quote=True)
    gc = payload.get("grade_counts", {})
    head = (f"<b>{title}</b>\n"
            f"{payload['market_label']} · {payload['data_date']} · "
            f"{payload['scanned']:,}종목 스캔\n"
            f"S {gc.get('S',0)} / A {gc.get('A',0)} / B {gc.get('B',0)} / C {gc.get('C',0)}\n")
    cur = payload.get("currency", "KRW")
    body = "\n\n".join(_line(i, cur) for i in items[:10])
    shown = {i["grade"] for i in items[:10]}
    legend = "\n".join(f"{GRADE_EMOJI[g]} <b>{g}</b> {GRADE_ONELINE[g]}"
                       for g in ("S", "A", "B", "C") if g in shown)
    tail = f"\n\n📊 <a href=\"{url}\">전체 결과·지표 수치 보기</a>" if url else ""
    return (head + "\n" + body + tail
            + (f"\n\n<b>등급 안내</b>\n{legend}" if legend else "")
            + "\n\n<i>점수는 '오를 확률'이 아니라 차트가 매수 조건에 얼마나 부합하는지입니다. "
              "공시·실적은 반영되지 않으며 매매 권유가 아닙니다.</i>")


# ── 텔레그램 ────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[notify] 텔레그램 설정 없음 → 건너뜀")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20)
        ok = r.status_code == 200
        print(f"[notify] 텔레그램 {'성공' if ok else '실패: ' + r.text[:200]}")
        return ok
    except Exception as e:
        # 예외 메시지에는 요청 URL이 통째로 들어있고 URL 안에 봇 토큰이 있습니다.
        # (requests의 ConnectionError 등) Actions가 시크릿을 마스킹해 주긴 하지만
        # 마스킹은 최선노력일 뿐이라 애초에 토큰이 로그로 흘러가지 않게 막습니다.
        print(f"[notify] 텔레그램 오류: {type(e).__name__}")
        return False


# ── 이메일 ─────────────────────────────────────────────────
def send_email(subject: str, html_body: str) -> bool:
    user, pw = os.getenv("GMAIL_USER"), os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("MAIL_TO") or user
    if not user or not pw or not to:
        print("[notify] 이메일 설정 없음 → 건너뜀")
        return False
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [x.strip() for x in to.split(",")], msg.as_string())
        print("[notify] 이메일 발송 성공")
        return True
    except Exception as e:
        print(f"[notify] 이메일 오류: {e}")
        return False


def _email_html(payload: dict, items: list[dict], title: str) -> str:
    url = html.escape(os.getenv("PAGES_URL", "").strip(), quote=True)
    cur = payload.get("currency", "KRW")
    rows = []
    for i, it in enumerate(items[:20], 1):
        r = it["risk"]
        warns = it.get("warnings") or []
        warn_html = (f"<br><span style='color:#c07800;font-size:11.5px'>"
                    f"⚠ {html.escape(', '.join(w['label'] for w in warns))}</span>") if warns else ""
        rows.append(
            f"<tr><td>{i}</td><td><b>{html.escape(str(it['name']))}</b><br>"
            f"<span style='color:#888;font-size:12px'>{html.escape(str(it['symbol']))}"
            f"{' · ' + html.escape(str(it['headline'])) if it.get('headline') else ''}</span>"
            f"{warn_html}</td>"
            f"<td align='center'><b>{it['score']:.0f}</b><br>"
            f"<span style='font-size:12px'>{it['grade']}</span></td>"
            f"<td align='right'>{_fmt_price(it['price'], cur)}<br>"
            f"<span style='color:{'#d33' if it['change_pct']>=0 else '#38f'};font-size:12px'>"
            f"{it['change_pct']:+.1f}%</span></td>"
            f"<td align='center'>{it['checks_passed']}/8</td>"
            f"<td align='right'>{r['stop_pct']:+.1f}% / {r['target_pct']:+.1f}%<br>"
            f"<span style='font-size:12px'>R:R {r['rr']:.1f}</span></td></tr>")
    link = f"<p><a href='{url}'>전체 결과 대시보드 열기</a></p>" if url else ""
    return f"""<div style="font-family:-apple-system,'Malgun Gothic',sans-serif;max-width:640px">
<h2 style="margin-bottom:4px">{html.escape(title)}</h2>
<p style="color:#666;margin-top:0">{html.escape(str(payload['market_label']))} · {html.escape(str(payload['data_date']))} ·
{payload['scanned']:,}종목 스캔 · 후보 {payload['count']}개</p>
<table cellpadding="8" cellspacing="0" border="0" width="100%"
 style="border-collapse:collapse;font-size:14px">
<tr style="background:#f4f4f6"><th>#</th><th align="left">종목</th><th>점수</th>
<th align="right">현재가</th><th>조건</th><th align="right">손절/목표</th></tr>
{''.join(rows)}
</table>{link}
<table cellpadding="6" cellspacing="0" border="0" style="font-size:12.5px;margin-top:14px">
<tr><td colspan="2"><b>등급 안내</b></td></tr>
{''.join(f"<tr><td valign='top'><b>{g}</b></td><td>{GRADE_ONELINE[g]}</td></tr>"
         for g in ('S', 'A', 'B', 'C'))}
</table>
<p style="color:#999;font-size:12px">점수는 '오를 확률'이 아니라 지금 차트가 교과서적인 매수
조건에 얼마나 부합하는지입니다. 지표는 모두 과거 가격의 함수라 후행하며, 공시·실적·뉴스는
반영되지 않습니다. ⚠ 표시는 매수 점수와 별개로 계산한 매도·경계 신호로, 총점·등급에는
반영되지 않으니 대시보드에서 직접 확인하세요. 투자 판단 참고용이며 매매 권유가 아닙니다.</p></div>"""


# ── 디스패치 ────────────────────────────────────────────────
def dispatch(payload: dict, new_items: list[dict], force_summary: bool = False) -> None:
    label = payload["market_label"]

    # 주식은 하루 한 번이라 매번 전체 요약을 보낸다 (코인만 신규 진입 위주)
    if force_summary or payload["market"] in ("kr", "us"):
        top = payload["items"]
        if top:
            title = f"📈 {label} 매수 후보 {payload['count']}건"
            send_telegram(_build_message(payload, top, title))
            send_email(f"[스크리너] {title} ({payload['data_date']})",
                       _email_html(payload, top, title))
        else:
            send_telegram(f"<b>{label}</b> {payload['data_date']}\n"
                          f"조건을 충족한 종목이 없습니다. 관망 구간입니다.")
        return

    # 코인: 신규 상위 등급 진입만 즉시 알림 (4시간마다 도배 방지)
    if new_items:
        title = f"🚨 {label} 신규 진입 {len(new_items)}건"
        send_telegram(_build_message(payload, new_items, title))
    else:
        print("[notify] 신규 진입 종목 없음 → 알림 생략")
