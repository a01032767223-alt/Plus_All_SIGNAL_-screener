/* 오프라인에서도 마지막 결과를 볼 수 있게 하는 최소 서비스워커.
   - 앱 셸: 캐시 우선
   - 데이터: 네트워크 우선, 실패 시 캐시된 마지막 결과 */
const CACHE = "screener-v7";   // 내 전략 탭 추가 (index.html은 캐시 우선이라 이 값을 올려야 새 화면이 나간다)
const SHELL = ["./", "./index.html", "./manifest.json", "./icon-192.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const isData = new URL(req.url).pathname.includes("/data/");

  if (isData) {
    // 쿼리스트링(?t=…)을 떼고 항상 같은 키로 저장·조회한다.
    // 예전에는 요청마다 키가 달라져 (1) 캐시가 무한히 쌓이고
    // (2) 오프라인에서 가장 오래된 스냅샷이 나왔다.
    const key = new Request(new URL(req.url).origin + new URL(req.url).pathname);
    e.respondWith(
      fetch(req)
        .then(res => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then(c => c.put(key, copy));
          }
          return res;
        })
        .catch(() => caches.match(key).then(hit => hit || Response.error()))
    );
  } else {
    e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
  }
});
