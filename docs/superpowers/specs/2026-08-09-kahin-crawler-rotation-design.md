# Kahin Otomatik Identity Rotation ve Uzun Süreli Crawler Tasarımı

## Amaç

Kahin, Camoufox/Mirage üzerinde tek browser engine kullanarak uzun süreli ve gözlenebilir bir web crawl job çalıştıracak. Job, sayfa sınırlarında aynı engine'i güvenli biçimde stop/start ederek yeni BrowserForge fingerprint üretecek; rate-limit durumunda Retry-After ve bounded backoff uygulayacak; CAPTCHA veya access-denied durumunda bypass denemeden pause olacak.

## Kesin sınırlar

- Aynı MCP sürecinde aynı anda yalnızca bir browser engine aktif olur.
- Crawler ikinci browser açmaz; mevcut Mirage engine'i ve bir crawler tabını yeniden kullanır.
- Rotation yalnızca mevcut sayfa tamamlandıktan sonra yapılır; queue, sonuç ledger'ı ve job kimliği korunur.
- Her Mirage launch'ında Camoufox'un gerçek `launch_options()` çağrısı yapılır. BrowserForge fingerprint, WebGL, font/voice seçimi, canvas/audio seed ve media-device varsayılanları korunur; `humanize=True`, `enable_cache=True`, gerçek `headless` değeri ve proxy varsa Camoufox proxy/geoip eşlemesi açıkça geçirilir.
- Identity hash, gerçek launch seçeneklerinden türetilir ve yalnızca bounded özet olarak raporlanır; fingerprint payload'ı loglanmaz.
- CAPTCHA çözme, CAPTCHA bypass, rate-limit aşmak için kimlik/proxy kaçırma ve güvenlik kontrolü atlatma uygulanmaz.

## MCP sözleşmesi

### `kahin_crawl_start`

Background job başlatır ve hemen `{jobId, state, engine, tabId, policy}` döner. `seeds` http/https URL listesi; `maxPages` 1..10000; `maxDepth` 0..32; `rotationEveryPages` 1..1000 (varsayılan 20); `rotationEverySeconds` 60..86400 (varsayılan 900); `delayMs` 0..60000; `maxDurationSeconds` 60..86400; `sameOrigin` varsayılan true; `extract` bounded metin/başlık/link özetidir. Aynı anda ikinci crawler başlatılması structured `crawl_busy` döner.

### `kahin_crawl_status`

Job state (`queued|running|backing_off|paused|rotating|completed|failed|cancelled`), counters, queue size, current URL, current identity hash, rotation count, crash recovery count, last challenge/error, `startedAt`, `updatedAt` ve active engine health döner.

### `kahin_crawl_results`

Opaque cursor ile en fazla 100 bounded result döner. Her result URL, status, title, text snippet, same-origin discovered links, depth, identity hash ve timestamp içerir. Cursor monotonic ve reset edilebilir; tüm job sonuçları sınırsız tek response'a alınmaz.

### `kahin_crawl_pause`, `kahin_crawl_resume`, `kahin_crawl_stop`

Pause/resume açıkça çağrılmadıkça CAPTCHA/access-denied sonrası job devam etmez. Stop queue'yu ve background task'ı temiz biçimde sonlandırır, browser'ı otomatik kapatmaz. Resume CAPTCHA'yı çözmez ve identity değiştirerek challenge kaçırmaz.

## Rotation ve recovery

Rotation policy varsayılan olarak 20 başarılı sayfa veya 15 dakika sınırından ilkine ulaşıldığında, sayfa sonucu ledger'a yazıldıktan sonra devreye girer. Mevcut browser stop edilir, tek browser slotu korunarak Mirage yeniden başlatılır; yeni launch identity hash'i önceki hash'ten farklı olmalıdır. Explicit saved identity verilmedikçe her rotation fresh BrowserForge identity kullanır.

Navigation/engine failure sonrası queue korunur. Engine dead ise aynı launch ayarlarıyla tek recovery restart denenir; tekrar başarısızsa job `failed` olur ve structured error döner. Challenge sonrası rotation otomatik bypass stratejisi değildir: rate-limit backoff uygulanır, CAPTCHA/access-denied pause olur.

## Crawler veri akışı

Job seed queue'sunu canonical URL ile dedupe eder. Her URL için mevcut Mirage tabında `kahin_navigate` sözleşmesi kullanılır; ardından challenge probe ve bounded DOM extraction yapılır. Anchor linkleri sayfa içinden alınır, origin/depth/queue sınırlarına göre queue'ya eklenir. Her iteration arasında delay uygulanır; rate-limit delay'i Retry-After ve capped exponential backoff'un maksimumudur.

## Gerçek doğrulama kapısı

Unit/e2e test suite çalıştırılmaz. Wheel rebuild/install sonrası yalnızca `/home/void0x14/.local/share/pnpm/bin/kahin` ile gerçek stdio MCP çağrıları yapılır: tools/list sözleşmesi, local ThreadingHTTPServer crawl, rotation PID/hash değişimi, result cursor, rate-limit backoff, CAPTCHA pause/resume, cancel, engine crash recovery, no-second-browser ve health/stats kanıtları ayrı ayrı gözlenir.
