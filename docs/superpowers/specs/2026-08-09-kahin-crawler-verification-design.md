# Kahin Crawler — Read-Only Gerçek MCP Doğrulama Tasarımı

> **Kapsam:** Yalnızca tasarım. Test suite çalıştırılmaz, ürün kodu değiştirilmez.
> Doğrulama, kullanıcının gerçek kullanım yüzeyi olan **paketlenmiş Kahin stdio MCP**
> (`/home/void0x14/.local/share/pnpm/bin/kahin`) üzerinden yapılır.
> Bu tasarım, `2026-08-09-kahin-crawler-rotation-design.md` içindeki "Gerçek doğrulama
> kapısı" taahhüdünü operasyonel senaryolara ve hang-korumalı timeout sözleşmesine dönüştürür.

## 1. Başarı ölçütleri (kullanıcının gerçek kabulleri)

Her senaryo, ajanın Kahin'i kaynak koduna bakmadan ve takılmadan kullanabilmesini kanıtlar:

| # | Kullanıcı şikâyeti | Doğrulanan sözleşme |
|---|--------------------|----------------------|
| A | "dokümantasyonu otomatik göremiyor, kaynak kod arıyor" | tools/list + resources/list + resources/read ile sözleşme ajana hazır gelir |
| B | "her işte yeni tarayıcı açılıyor, kaynak israfı" | `browser_start` reuse: ikinci çağrı aynı PID'yi döndürür |
| C | "1 dakikada bir patlıyoruz, bağlam kayboluyor" | Tek engine + tek tab; rotation yalnızca sayfa sınırında, identity/PID değişir ama tab sayısı 1 kalır |
| D | "crawl çok yavaş, sonuç yok" | Multi-page crawl, dedupe, bounded cursor ile hızlı ve gözlenebilir ilerleme |
| E | "rate-limit'e takılıp ne yapacağını bilemiyor" | Retry-After + bounded backoff, state makinesi üzerinden gözlenebilir |
| F | "captcha'da salakça davranıyor" | CAPTCHA'da explicit pause, kendi kendine devam yok, bypass yok |
| G | "durum takip edilemiyor, stop temiz değil" | `crawl_stop` temiz cancel, kısmi sonuç okunur, browser açık kalır, slot serbest kalır |
| H | "büyük scriptte tarayıcı çöküyor" | Tek recovery restart, queue korunur; ikinci çöküşte structured `failed` |

## 2. Çalıştırma ortamı sözleşmesi (harness)

- **Transport:** stdio MCP (FastMCP). Elle yazılmış JSON-RPC çerçeveli mesajlar
  (initialize → `notifications/initialized` → tools/list → resources/list →
  resources/read → tools/call). MCP SDK kullanılmaz; böylece ajanın gerçek ağ
  protokolü ve hang davranışı birebir gözlenir.
- **Her senaryo taze süreçle başlar:** kahin binary'si spawn edilir, initialize
  el sıkışması 15s içinde tamamlanmalıdır. Bir senaryo başarısızsa süreç kill
  edilir, stderr kuyruğu kaydedilir, sonraki senaryo taze spawn ile devam eder —
  tek bir hang tüm koşuyu durduramaz.
- **Liveness probe:** Senaryolar arasında `kahin_engine_stats` (15s budget)
  çağrılır; structured `engine_unavailable` dönerse süreç sağlıklıdır, engine
  yok demektir. Yanıt gelmezse süreç wedged sayılır ve yeniden spawn edilir.
- **Evidence ledger:** Her senaryo için JSONL satırları: çağrı sırası, istek
  özeti (tool + ana argümanlar), yanıtın ilgili alanları, iddia sonuçları, süre.
  Sonuç: `PASS | FAIL` + neden.
- **Local fixture:** Tek `ThreadingHTTPServer` (localhost, rastgele port), aşağıdaki
  rotalar. Fixture kod içermeyen deterministik HTTP davranışıdır:

  | Rota | Davranış |
  |------|----------|
  | `/` | 3 iç link: `/a`, `/b`, `/c`; 1 yinelenen link `/b`; 1 dış link `https://example.invalid/x` |
  | `/a`, `/b`, `/c`, `/d`, `/e` | Basit HTML, her biri kendine 1 yeni link (`/b`→`/d`, `/c`→`/e`) |
  | `/429` | `429 Too Many Requests` + `Retry-After: 2` başlığı |
  | `/captcha` | Gövdesinde `captcha`/`challenge` işaretleri; `kahin_challenge_status` bunu `captcha` algılar |
  | `/slow` | 2 saniye bekleyip normal HTML döner |
  | `/die` | Sunucu tarafında bağlantıyı anında kapatır (navigation failure senaryosu) |

## 3. Senaryolar

### S0 — Sözleşme ajana hazır (ölçüt A)

- `tools/list` → tüm ORBIT araçları mevcut: `kahin_crawl_start/status/results/pause/resume/stop`,
  `kahin_challenge_status`, `kahin_identity_report`, `kahin_engine_health`, `kahin_engine_stats`,
  `kahin_agent_status`. Her birinin `description`'ı crawler sözleşmesini (tek engine, explicit
  pause, backoff) içermeli.
- `resources/list` → `kahin://docs/usage` ve `kahin://docs/juggler-ai-native` görünür.
- `resources/read(kahin://docs/usage)` → paketlenmiş README döner.
- **Geçiş:** yukarıdaki üç çağrı da başarılı; crawler + challenge + identity tool'ları
  `tools/list` içinde; docs resource okunabilir. Ajan kaynak koduna dokunmadan sözleşmeye ulaşır.

### S1 — Browser reuse (ölçüt B)

1. `kahin_browser_start(engine="mirage", headless=true)` → `status == "started"`;
   `kahin_engine_health()` → `pid` kaydedilir.
2. `kahin_browser_start(engine="mirage", headless=true)` → `status == "reused"`,
   `message` "reusing the existing browser" (tab'lar lazy açılır; start sonrası
   tab sayısı iddiası navigate sonrasına bırakılır).
3. `kahin_engine_health()` → öncekiyle **aynı** `pid`.
4. `kahin_navigate(url=<fixture />)` → başarılı; `kahin_agent_status()` → sekme sayısı 1.
- **Geçiş:** iki start'ta PID değişmedi; ikinci start `reused`; tek tab; hata yok.

### S2 — 1-tab rotation: identity + PID değişir, tab sayısı sabit (ölçüt C)

1. `kahin_browser_start(engine="mirage", headless=true)`; `engine_health` PID'i ve
   `kahin_identity_report` hash'i kaydedilir.
2. `kahin_crawl_start(seeds=[fixture/], maxPages=3, rotationEveryPages=1, delayMs=200)`
   → `{jobId, state:"running", tabId}`.
3. `kahin_crawl_status(jobId)` poll (2s aralık, 240s budget): `rotations` 2'ye ulaşana dek.
   Gözlenen geçişler: `running → rotating → running` (2 kez).
4. Rotasyon öncesi/sonrası karşılaştır:
   - `currentIdentityHash` değişti;
   - `engineHealth.pid` değişti (yeni süreç);
   - `tabId` **aynı** kaldı, `kahin_agent_status()` sekme sayısı hâlâ 1;
   - `kahin_identity_report` yeni hash'i doğruluyor.
5. `kahin_crawl_results(jobId)` → her sayfa sonucunda `identityHash` mevcut; ilk iki sayfanın
   hash'i farklı.
- **Geçiş:** 2 rotation, hash + PID farklı, tab 1, queue/result kaybolmadı (3 sonuç), state `completed`.
- **Negatif kontrol:** `crawl_start` aynı anda ikinci kez çağrılırsa structured `crawl_busy` döner
  (ikinci browser açılmaz).

### S3 — Multi-page crawl + dedupe + bounded cursor (ölçüt D)

1. `kahin_browser_start`; `kahin_crawl_start(seeds=[fixture/, fixture/a], maxPages=6,
   maxDepth=2, sameOrigin=true, delayMs=150)`.
2. Poll `crawl_status` → `state == "completed"` (240s budget).
3. `kahin_crawl_results(jobId, cursor=0, limit=2)` → `count == 2`, `nextCursor == 2`,
   `hasMore == true`. `cursor=nextCursor` ile tekrar; `hasMore == false` olana dek sür.
4. İddialar:
   - `pagesFetched == 6` (maxPages aşılmadı);
   - tüm result URL'leri unique (seed'deki `/a` + index'teki `/b` linki dedupe edildi);
   - `https://example.invalid/x` dış origin olduğu için yok (sameOrigin);
   - her kayıt `index/url/status/title/text/identityHash/fetchedAt` içeriyor;
   - tek response asla 100'den fazla kayıt içermedi.
- **Geçiş:** tüm iddialar; `queueSize == 0`, `state == "completed"`.

### S4 — Retry-After backoff, evasion yok (ölçüt E)

1. `kahin_crawl_start(seeds=[fixture/, fixture/429, fixture/429], maxPages=3, delayMs=0)`.
2. Poll `crawl_status` (1s aralık): `state == "backing_off"` gözlemlenmeli;
   `backoff.active == true`, `backoff.retryAfterSeconds == 2`.
3. `lastChallenge.kind == "rate_limit"`, `action == "honor_retry_after_and_backoff"`.
4. `backoff.until` geçtikten sonra state `running`'e döner; URL yeniden denenir.
5. Backoff sırasında ve sonrasında `currentIdentityHash` **değişmedi**
   (rate-limit'i kimlik değiştirerek aşma yok).
6. Sonuç: `_BACKOFF_MAX_RETRIES == 3` nedeniyle /429 için `failed` kaydı (rate_limited) veya
   sunucu salındıysa success; state `completed` (asla hang, asla `crashed`).
- **Geçiş:** `backing_off` gözlemlendi, Retry-After onurlandırıldı, hash sabit, toplam duvar
  süresi ≤ 60s (cap 60s + 3 retry), sonuç okunabilir.

### S5 — CAPTCHA explicit pause/resume (ölçüt F)

1. `kahin_crawl_start(seeds=[fixture/captcha], maxPages=1)`.
2. Poll `crawl_status`: `state == "paused"`, `stateReason == "challenge:captcha"`,
   `challenge.kind == "captcha"`, `challenge.action == "pause_for_human_or_authorized_provider"`.
3. **Kendi kendine devam yok:** 5s bekleyip tekrar `crawl_status` → hâlâ `paused`.
4. `kahin_challenge_status()` bağımsız olarak aynı `captcha` kararını verir.
5. `kahin_crawl_pause(jobId)` → idempotent: `paused:true` döner (hata değil).
6. `kahin_crawl_resume(jobId)` → `state:"running"`, `resumed:true`. Fixture hâlâ captcha
   olduğundan job tekrar `paused` olur. Bu döngü `_CHALLENGE_MAX_RETRIES == 3` boyunca:
   sonunda result `challenge_captcha_unresolved` ile `failed` kaydedilir.
7. **Negatif kontrol:** tüm bu süreçte `rotations == 0` ve `currentIdentityHash` değişmedi
   (challenge'ı identity rotasyonuyla atlatma yok).
- **Geçiş:** pause stabil, sadece explicit resume açıyor, bypass yok, retry bütçesi sonunda
  structured `failed` + `lastError`, state makinesi `paused` içinde kilitlenmedi.

### S6 — Stop cleanup (ölçüt G)

1. `kahin_crawl_start(seeds=[fixture/slow, fixture/slow], maxPages=4, delayMs=500)`.
2. `state == "running"` görülür görülmez `kahin_crawl_stop(jobId)`.
3. `kahin_crawl_status(jobId)` → `state == "cancelled"`; tekrar `crawl_stop` →
   `alreadyStopped:true` (idempotent).
4. `kahin_crawl_results(jobId)` → kısmi sonuçlar (succeeded + failed toplamı 0'dan büyük) okunur.
5. `kahin_engine_health()` → `alive:true` (browser **kapatılmadı**); `kahin_engine_stats()`
   uptime devam ediyor.
6. Yeni `kahin_crawl_start(...)` → `crawl_busy` **değil**, başarıyla başlar (slot serbest).
- **Geçiş:** cancelled + idempotent stop + okunabilir kısmi sonuçlar + açık browser +
  yeniden kullanılabilir job slotu; 10s içinde döner (iç shield timeout 10s).

### S7 — Crash recovery (ölçüt H)

1. `kahin_crawl_start(seeds=[fixture/slow, fixture/], maxPages=4, delayMs=800)`.
2. `state == "running"` iken `engineHealth.pid`'i al ve browser sürecini `kill -9` ile öldür
   (sidecar değil, Camoufox süreci).
3. Poll `crawl_status` (2s aralık, 300s budget):
   - `counters.recoveryAttempts == 1`;
   - `engineHealth.pid` yeni süreci gösteriyor (değişti);
   - `rotations == 1` — recovery aynı stop/start yolundan geçtiği için sayaç artar,
     ama launch identity korunur: `currentIdentityHash` crash öncesi hash ile aynı;
   - queue korundu: URL kaybı yok — `succeeded`/`failed` monotonik artar, result
     cursor monotonic kalır; job `completed`'a ulaşıyor.
4. **İkinci çöküş:** job `running` iken yeni PID'i yine `kill -9`.
   → tek recovery hakkı kullanıldığından `state == "failed"`, `lastError.code ==
   "engine_crashed"`, mesaj "single recovery restart already used"; hata structured ve okunabilir.
- **Geçiş:** tam olarak 1 recovery; queue/result korundu; ikinci çöküşte hang yok, 15s içinde
  structured `failed`.

## 4. Timeout sözleşmesi — 7 dakikalık hang riskini ortadan kaldırma

Hang kaynağı: stdio tek akıştır; client-side read timeout'u olmayan bir `tools/call`,
yanıtı asla gelmeyen bir istekte (ör. takılı engine probe, wedged sidecar, asılı launch)
**süresiz** bekler. Aşağıdaki üç katman bunu imkânsız kılar:

### 4.1 Transport (client tarafı, zorunlu)

| Aşama | Budget | Davranış |
|-------|--------|----------|
| initialize el sıkışması | 15s | Aşılırsa süreç kill, stderr kaydedilir, taze spawn |
| Her yanıtın okunması (recv) | 60s sabit | Deadline aşılınca istek `TIMEOUT` sayılır, transcript'e yazılır |
| Watchdog (senaryo başına) | senaryo budget'i + 90s grace | Süreç kill, stderr kuyruğu eklenir, senaryo `FAIL(timeout)`, sıradaki taze spawn |

Transport timeout'u asla 60s'yi aşmaz: hiçbir bekleyiş 7 dakikaya ulaşamaz.

### 4.2 Tool bazlı per-call budget (client tarafı)

| Tool | Budget | Gerekçe (sunucu içi sınır) |
|------|--------|------------------------------|
| `kahin_browser_start` | 120s | `_ENGINE_START_TIMEOUT = 60s` + identity prewarm + ilk boot |
| `kahin_browser_stop` | 30s | `_ENGINE_STOP_TIMEOUT = 15s` |
| `kahin_crawl_start` | 30s | sayfa settle `_PAGE_SETTLE_TIMEOUT = 5s`; normalde < 5s |
| `kahin_crawl_status` | 15s | `_ENGINE_PROBE_TIMEOUT` (5s sağlık) |
| `kahin_crawl_results` | 15s | salt ledger, IO yok |
| `kahin_crawl_pause` / `resume` | 10s | salt state geçişi |
| `kahin_crawl_stop` | 20s | iç shield `wait_for(task, 10s)` |
| `kahin_challenge_status` | 15s | iç probe `timeout=5s` |
| `kahin_engine_health` / `engine_stats` | 15s | `_ENGINE_HEALTH_TIMEOUT = 5s` |
| `kahin_navigate` | 60s | sayfa yükleme |
| `kahin_identity_report` / `agent_status` | 15s | salt engine özeti |

### 4.3 Senaryo ve toplam budget

| Senaryo | Budget |
|---------|--------|
| S0 | 60s |
| S1 | 120s |
| S2 | 240s |
| S3 | 240s |
| S4 | 180s |
| S5 | 240s |
| S6 | 120s |
| S7 | 300s |
| **Toplam suite** | **35 dk sabit** (üst sınır) |

Poll döngüleri her zaman sabit aralıklı, kendi budget'ı olan ayrı `crawl_status`
çağrılarıdır; tek uzun bekleme asla yok. Budget aşımında yalnızca o senaryo FAIL olur,
suite devam eder (S7'nin iki çöküşü tek senaryo budget'i içindedir).

Ürün notu (design-only, kod değişmez): sunucu tarafı `retryAfterSeconds`'u
tavansız onurlandırır (`wait = max(retry_after, capped)`); gerçek dünyada büyük
Retry-After job'ı dakikalarca `backing_off`'ta tutabilir (7-dk hang kaynağı).
Doğrulama fixture'ı `Retry-After: 2` ile sınırlıdır; üretim için üst sınır
(ör. 300s) ve `kahin_challenge_status`'un aynı tavana uyması önerilir.

## 5. Negatif kontroller ve genel değişmezler

Her senaryoda geçerli:

- İkinci eşzamanlı `crawl_start` → structured `crawl_busy` (ikinci browser/tab yok).
- CAPTCHA/access-denied'da resume dışında otomatik devam yok; rate-limit'te kimlik rotasyonu yok.
- `kahin_engine_stats` her senaryoda çağrılır: engine yoksa `engine_unavailable` yapılandırılmış
  döner, asla hata fırlatmaz; `tool_calls`/`tool_errors` sayaçları tutarlıdır.
- Hiçbir yanıtta ham fingerprint payload, proxy kimlik bilgisi veya cookie değeri yok
  (redaction kontrolü).
- Test suite (`pytest`, `camoufox-harness`) çalıştırılmaz; ürün kodu değiştirilmez;
  yeni branch/worktree açılmaz.

## 6. Sonuç formatı

Her senaryo için tek satır: `S{n} PASS/FAIL — iddia seti (kısa) — süre`.
Toplamda: geçen/başarısız sayısı, en yavaş çağrı, timeout'ların listesi. FAIL'lerde
kanıt (ilgili yanıt alanları + stderr kuyruğu) ledger'a yazılır; kullanıcının "gerçek
MCP üzerinden uçtan uca" ölçütünün dışına çıkmaz.
