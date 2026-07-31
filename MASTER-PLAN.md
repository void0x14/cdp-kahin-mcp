# MASTER-PLAN.md — Camoufox Ajan Kontrol Katmanı

Girdi: ALPHA-PLAN.md. Bu doküman kod içermez; mimari karar, klasör yapısı, teknoloji seçimi, durum/veri yapısı ve geliştirme adımlarını tanımlar.

Kapsam kısıtı (kullanıcı tanımlı, sabit): Kod dışa açılmaz, üçüncü tarafla paylaşılmaz. Performans hedefi CDP ile eşdeğerdir — ekstra soyutlama katmanı bu hedefi tehlikeye atıyorsa reddedilir.

> **Güncelleme notu (2026-07-31):** Önceki sürümde varsayımla doldurulmuş noktalar araştırılıp gerçeklerle değiştirildi. Kaynaklar: `daijro/camoufox` ve `CloverLabsAI/camoufox` repo/release/discussion kayıtları, `microsoft/playwright` commit geçmişi, foxbridge.vulpineos.com, deepwiki.com/daijro/camoufox, camoufox.com ve Kahin MCP repo'su (bu oturumda okundu). Değişen bölümler: §1 (fork seçimi), §2.1 (transport, şema kaynağı, driver, process manager, drift), §2.3 (çoklu instance), §2.4 (çıkarım hedefi), §3.1 (transport gerçeği), §3.3 (açık soru kapandı), §5 (mevcut kalıcılık gerçeği), §6 (sorular kapandı), §8 (faz sırası), §9 (riskler).

---

## 1. Karar Özeti

| Soru (ALPHA §6) | Karar |
|---|---|
| Öncelik: Seçenek B mi D mi? | **B** — doğrudan Juggler. D (CDP-shim) kapsam dışı. |
| Harness dili | **Zig** — çekirdek transport/codec. MCP entegrasyon katmanı Kahin MCP'nin mevcut runtime'ıyla aynı dilde kalır: **Python** (bkz. §3.3). |
| Referans fork | **`daijro/camoufox`** (master kopya — checkpoint release'ler burada toplanır, release'ler orada yayınlanır) @ **v152.0.4-beta.28** (FF152, 2026-07-19) — `vendor/camoufox-fork-pin.lock`'a yazılacak. `CloverLabsAI/camoufox` (2025-08-16'da oluşturulmuş, Clover Labs'ın aktif geliştirme fork'u) ve `VulpineOS/VulpineOS` (deneysel hat) geliştirme kollarıdır; ilişki §9'da çözüldü. |
| BiDi (Seçenek C) | Araştırma/fallback konumunda, ilk sürüm kapsamı dışında. Gerçek: Firefox 136+ WebDriver BiDi'yi native WebSocket üzerinden destekliyor; foxbridge BiDi backend'inde 62/62 test geçiyor. |
| CDP-shim (Seçenek D) | Reddedildi — gerekçe §2. |

Gerekçe (D'nin reddi): ALPHA-PLAN §4'te D, "CDP'nin Network/Page/Runtime/DOM/Input/Target domain'lerinin her birinin Juggler karşılığının bulunup eşlenmesi" olarak tanımlanıyor — bu bir çeviri katmanı demek, çeviri katmanı da ek serileştirme/ek round-trip demek. Performans hedefin CDP ile eşdeğerlik olduğu için, CDP'yi taklit eden bir katman inşa edip onun üzerinden geçmek bu hedefle doğrudan çelişir. Kahin MCP'nin tool arayüzü (agent'a açılan yüzey) zaten CDP wire formatından bağımsız bir soyutlama; yani "CDP uyumluluğu" gereksinimi aslında yok — sadece "aynı yetenek kümesi" gerekiyor. Bu, D'yi gereksiz kılıyor ve B'yi doğrudan uygulanabilir hale getiriyor.

Ek gerçek (D'nin teknik olarak yapılabilirliği): CDP→Juggler çeviricisi zaten var — **foxbridge** (VulpineOS, Go): CDP WebSocket (varsayılan port 9222) → Juggler pipe transport, 74/74 Puppeteer testi geçiyor. Bu proje için reddedilmesinin nedeni yetenek eksikliği değil, wire-format CDP'nin istenmemesi: Kahin'de yalnızca iç çağrı dili CDP-şekilli (bkz. §2.1.4), wire format Juggler kalır.

---

## 2. Mimari

### 2.1 Bileşenler

1. **Juggler Transport Core** — Juggler **WebSocket değil, stdio pipe** üzerinde çalışır. Gerçekler:
   - Çalışma modu: `--juggler-pipe` flag'i (eski WebSocket modu `-juggler <port>` legacy — Playwright Kasım 2020'de pipe'a geçti, microsoft/playwright #3279).
   - Firefox `nsIRemoteDebuggingPipe` FD3 (okuma) ve FD4 (yazma) üzerinden çift yönlü kanal açar; port yok, el sıkışma yok.
   - Çerçeveleme: **JSON + null byte (`\x00`) terminator**.
   - Mesaj yapısı CDP benzeri: istek `{"id":N,"method":"...","params":{...}}` (yanıt eşleştirme `id` ile), event `{"method":"...","params":{...},"sessionId":"..."}` (session çoğullaması `sessionId` ile — root session + alt session'lar; Playwright `ffConnection.ts` aynı desen).
   - Yani transport çekirdeği: pipe FD'leri + `\x00`-ayırıcılı JSON demux. (Sonuç: Kahin'in 9222/9240 port rezervasyonu Juggler modunda anlamsız; port yalnızca BiDi fallback'inde gerekir.)
2. **Protocol Schema (üretilmiş, elle yazılmamış)** — Şema kaynağı **tek JS dosyası**: `additions/juggler/protocol/Protocol.js` (camoufox fork'unda; type/event/method tanımlarının tamamı burada). Not: `browser_patches/firefox/juggler/` dizini **microsoft/playwright** repo'suna aittir — Juggler'ın upstream'i Mozilla değil Playwright'tır (Mozilla tarafından Playwright için geliştirildi, kod Playwright repo'sunda yaşıyor). Camoufox'da Juggler `additions/juggler/` altında: `components/Juggler.js` (XPCOM girişi, pipe başlatma), `protocol/Protocol.js` (şema), `protocol/Dispatcher.js` (mesajları çalışma zamanında Protocol.js'e karşı doğrular, handler'lara yönlendirir), `protocol/BrowserHandler.js` (Browser domain'i), `NetworkObserver.js` (ağ gözlemi/interception), `TargetRegistry.js` (context/target yönetimi), `JugglerFrameParent.jsm` (content process frame actor'ları), `screencast/nsScreencastService.cpp`.
3. **Domain Adapter'lar** — Juggler'ın gerçek domain seti: **Browser** (enable, createBrowserContext, removeBrowserContext, newPage, close, setExtraHTTPHeaders), **Page** (navigate, frame tree, lifecycle), **Network** (NetworkObserver — request interception), **Input** (Juggler girişleri Firefox'un **orijinal user input handler'larından** geçer — CDP'nin sentetik Input'undan farklı), **Runtime/evaluate** (frame actor'ları üzerinden), **Screencast**. Browser context = izolasyon birimi (`TargetRegistry`). CDP'de birebir karşılığı olmayanlar (ör. bazı Runtime.evaluate ayrıntıları, Emulation.setDeviceMetricsOverride karşılığı) için emülasyon notları §2.4 ve adapter notlarında.
4. **Driver Interface** — Gerçek şekli doğrulandı (Kahin MCP repo'su bu oturumda okundu): mevcut soyut arayüz **`BrowserEngine` ABC** — `kahin/the_twins/chassis.py`: `start(headless, port)`, `stop()`, `send_cdp(domain, command, params)`, `screenshot(format, full_page)`, `on_event(callback)`. İki implementasyon: **Obscura** (Chromium, port 9241) ve **Mirage** (Chrome + stealth flag'leri, port 9242 — ismine rağmen gerçek Camoufox değil; pyproject'taki `camoufox>=0.1` bağımlılığı kodda şu an kullanılmıyor). 32 MCP tool'u engine-agnostic: `_safe_cdp()` üzerinden `engine.send_cdp(...)` çağırıyor; `kahin_browser_start` engine adını whitelist'liyor (`("shadow", "mirage")`). Juggler driver'ı = yeni `BrowserEngine` alt sınıfı + whitelist'e `"camoufox"` eklemek. **MCP tool tanımlarında (isim/parametre şeması) değişiklik gerekmiyor — doğrulandı.**
5. **Camoufox Process Manager** — Gerçekler:
   - Binary/version yönetimi **camoufox Python paketi**nin CLI'sında: `python -m camoufox fetch` / `set` (version pin destekli: `camoufox set official/stable/134.0.2-beta.20`). İki kanal: **`camoufox`** (PyPI, resmi, gecikmeli release) ve **`cloverlabs-camoufox`** (her release ile güncel; per-context fingerprint + hardware spoofing). Paket checksum pin sunmuyor — kendi `vendor/camoufox-fork-pin.lock` mekanizması korunur.
   - Fingerprint üretimi **BrowserForge**: `browserforge.fingerprints.FingerprintGenerator(browser='firefox')` (pythonlib/camoufox/fingerprints.py). Config, `launch_options()` (pythonlib/camoufox/utils.py) içinde birleştirilir, `CAMOU_CONFIG_1..N` ortam değişkenlerine bölünerek process'e aktarılır (komut satırı uzunluk limitini aşmamak için), C++ tarafında `MaskConfig` singleton'ı okur. Yani fingerprint enjeksiyonu paketin kendi mekanizmasıdır; Process Manager yalnızca bu env yapısını kurar ve process'i `playwright.firefox.launch(executable_path=...)` sözleşmesine göre başlatır.
   - Process lifecycle: Playwright deseni (`-juggler-pipe -profile <dir>`, silent mode, pipe kapanınca browser kapanır — `disconnected()` → `Browser.close`).
6. **Protocol Drift Watcher** — Drift kaynağı artık biliniyor: (a) camoufox fork'undaki `additions/juggler/`  microsoft/playwright `browser_patches/firefox/juggler/` (upstream Juggler), (b) fork checkpoint güncellemeleri (daijro/camoufox'a periyodik merge). Ek gerçek: `Dispatcher.js` çalışma zamanında Protocol.js'e karşı doğrular — sessiz kırılma zaten kısmen engelleniyor; CI'daki diff izleme + derleme-zamanı kontrol çifte güvence olur.

### 2.2 Veri akışı

Agent → Kahin MCP tool çağrısı → Driver Interface (`BrowserEngine.send_cdp`) → (Camoufox modunda) Domain Adapter → Juggler Transport Core → **pipe (FD3/FD4, JSON+\x00)** → Camoufox process (Juggler, XPCOM — browser process + content process'te JSWindowActor'lar) → Firefox sayfa motoru.

Bu zincirde CDP-shim yok; agent'ın gördüğü tool sözleşmesi ile fiziksel taşıma protokolü arasında yalnızca bir çeviri katmanı var (Domain Adapter), o da zaten CDP driver'ında da mevcut (aynı tool'un CDP karşılığı da benzer şekilde CDP domain'lerine map ediliyor). Ek maliyet: sıfır yeni round-trip.

### 2.3 Çoklu instance / eşzamanlılık

Gerçekler: Juggler'da endpoint **pipe'tır, port değil** — her process kendi FD3/FD4 çiftine sahiptir, port çakışması/rezervasyonu yok. Daha önemlisi: Juggler'ın izolasyon birimi **browser context**'tir (`Browser.createBrowserContext`, `TargetRegistry`) — Camoufox ekosistemi tek process + N context desenini kullanıyor (VulpineOS: "one Camoufox process, hundreds of isolated contexts"; iddia: context başına ~10-15MB vs Chrome tab ~50-80MB). ALPHA-PLAN'da hedef instance sayısı belirtilmedi (Soru 2, §6): mimari N process'i bağımsız yaşam döngüsüyle yönetebilecek şekilde kurulur, ama **varsayılan desen tek process + N context**; process-başına ayrım yalnızca fingerprint/network izolasyonu gerektiğinde. Kahin tarafı gerçek: oracle.py tek engine tutuyor (`_current_engine` global; "Engine already running. Stop it first") — çoklu engine Kahin'de yeni yetenek, sınır oracle.py'nin global'inde.

### 2.4 Protokol çıkarım süreci

Elle yazılmış protokol şeması bakım yükü ve hata kaynağıdır. Bunun yerine:
- Pinlenmiş fork commit'inin `additions/juggler/protocol/Protocol.js` dosyası taranır (tek JS dosyası, object literal — tree-sitter JS yeterli; Firefox C++ taraması gerekmez, C++ yalnızca pipe/screencast'ta ve şemaya katkı yapmıyor).
- Method isimleri, parametre şemaları, event isimleri çıkarılır ve versiyonlu bir şema dosyasına yazılır.
- Emsal zaten Kahin'de mevcut: CDP şeması `kahin/the_source/protocol.json.gz` (gzip JSON, Chrome 148: 56 domain, 667 komut, 237 event, 609 type) → `SchemaEngine` (`the_source/architect.py`) tarafından yüklenip doğrulanıyor. Juggler şeması aynı desenle işlenir.
- Domain Adapter'lar bu şemaya karşı derleme zamanında doğrulanır (şema değişirse derleme kırılır — sessiz kırılma yerine gürültülü kırılma; ayrıca Dispatcher.js çalışma zamanında doğrular).
- Fork güncellendiğinde çıkarım yeniden çalıştırılır, diff raporlanır, adapter'lar manuel gözden geçirilir.

---

## 3. Teknoloji Seçimleri

### 3.1 Çekirdek (Transport + Adapter + Process Manager)

**Zig - Stabil (0.16.0) // Standard Library Documentation: https://ziglang.org/documentation/0.16.0/std/ - Language Reference: https://ziglang.org/documentation/0.16.0/.** Gerekçe: ALPHA-PLAN §3'teki yaklaşımla (ham socket üzerinden TLS/HTTP2 inşası) tutarlı — bağımlılıksız, öngörülebilir bellek/performans profili. Gerçeklerle uyum kontrolü: Juggler çerçeveleme basit (JSON + `\x00` terminator, pipe FD'leri) — WebSocket el sıkışması/HTTP katmanı yok, Zig için CDP WebSocket'ten daha basit bir transport. Emsal: foxbridge aynı transport'u (Juggler pipe FD3/4) Go'da implement ediyor ve 74/74 Puppeteer testini geçiyor — pipe'a doğrudan bağlanmanın uygulanabilirliği kanıtlandı. CDP client'ların çoğu Node/Python'da yazılı olduğu için gecikme kıyaslamasında GC duraklamaları/dinamik dispatch dezavantaj yaratıyor; Zig bunu ortadan kaldırıyor.

Çalışmaya başlamadan önce skill-seekers kulllanarak bütün 0.16.0 std dökümantasyonunu ve language reference dokumantasyonunu a'dan z'ye LLM-FRİENDLY Şekilde Skillere ve dökümantasyonlar açevir ve bunları ajanın bulabılmesı ıcın gereklı hooklraımı toolarımı mdlerımı kur ıste ne gerekıyosa

Alternatif değerlendirildi ve reddedildi: Rust (daha ağır derleme/bağımlılık grafiği, bu proje ölçeğinde gerekçesiz), Node.js (event loop + GC, "CDP kadar hafif" hedefiyle çelişiyor).

Ek not: camoufox'un kendi Python arayüzü Playwright driver'ına bağlı (`playwright.firefox.launch(executable_path=...)`). Zig core pipe'a doğrudan bağlandığında Playwright driver'ı tamamen atlanır — bu planın amacıyla uyumlu; ancak fingerprint env'lerini (`CAMOU_CONFIG_*`) ve launch argümanlarını doğru kurmak Process Manager'ın sorumluluğu olur.

### 3.2 Protokol çıkarım script'i

Ayrı, çalışma zamanında sisteme dahil olmayan bir araç: Python + tree-sitter. Tarama hedefi kesinleşti: `additions/juggler/protocol/Protocol.js` (JS) — "C++/JS karışık kod tabanı taraması" gerekmiyor. Yalnızca build-time, prod'a girmez.

### 3.3 Kahin MCP entegrasyonu — KAPANDI (gerçeklerle)

Kahin MCP repo'su bu oturumda okundu; önceki "bilinmiyor" notu geçersiz:

- **Dil/runtime**: Python ≥3.12, **FastMCP stdio** MCP server (`kahin.oracle:main` → `mcp.run(transport="stdio")`). Bağımlılıklar: mcp, orjson, pydantic, Levenshtein, websockets, httpx, camoufox (şu an kullanılmıyor), Pillow.
- **Driver Interface sözleşmesi**: mevcut CDP driver kodundan türetilir — `BrowserEngine` ABC (chassis.py), §2.1.4'teki gerçek şekli. Yeni arayüz tanımı gerekmez.
- **Köprü**: Zig çekirdek  Kahin (Python). FFI (ctypes/cffi) teknik olarak mümkün ama Kahin asyncio event loop'ta ve engine'leri zaten ayrı process olarak spawn ediyor (`asyncio.create_subprocess_exec` ile Chrome); aynı desenle **ayrı process + yerel IPC** (Unix socket veya stdio) en doğal köprü. Zig sidecar, Juggler pipe'ının bir ucunda durur; Kahin tarafına JSON-over-IPC sunar. Engine seçimi: `kahin_browser_start(engine="camoufox")`.
- **Faz 5 blokajı kalktı.** Sözleşme CDP-şekilli olduğundan (`send_cdp(domain, command, params)`), Juggler adapter'ı CDP-domain → Juggler-method map'i içerir; bu reddedilen D'nin (wire-format CDP shim) aynısı değildir — wire format Juggler'dır, yalnızca iç çağrı dili CDP-şekillidir. Emülasyon gereken noktalar (ör. `Emulation.setDeviceMetricsOverride`  Juggler viewport/screen) adapter notlarında listelenir.

---

## 4. Klasör Yapısı

```
camoufox-harness/
├── protocol/
│   ├── extractor/              # Python+tree-sitter, build-time, prod'a girmez; kaynak: additions/juggler/protocol/Protocol.js
│   ├── schema/                 # Üretilmiş, versiyonlanmış Juggler şeması (commit hash'e bağlı)
│   └── drift-watcher/          # CI script'i; diff: camoufox additions/juggler ↔ playwright browser_patches/firefox
├── core/
│   ├── transport/               # Pipe (FD3/FD4) + \x00-çerçeveli JSON, session demux
│   ├── adapters/
│   │   ├── browser/             # Juggler Browser domain (enable, createBrowserContext, newPage, close)
│   │   ├── page/                # navigate, frame tree, lifecycle, screencast
│   │   ├── runtime/             # evaluate (frame actor'ları üzerinden)
│   │   ├── network/             # NetworkObserver: interception
│   │   ├── input/               # orijinal user input handler'lar üzerinden
│   │   └── target/              # context/target yönetimi (TargetRegistry karşılığı)
│   └── driver/                  # BrowserEngine ABC implementasyonu (Kahin MCP'ye bakan yüz)
├── process-manager/
│   ├── binary/                  # camoufox fetch/set (version pin), vendor checksum kaydı
│   ├── profile/                 # CAMOU_CONFIG_* env yapısı, BrowserForge çıktısı
│   └── lifecycle/               # start/health-check/restart
├── state/                       # bkz. §5
├── tests/
│   ├── protocol-conformance/    # Şema ↔ adapter tutarlılık testleri
│   ├── perf/                    # CDP karşısında benchmark (bkz. §7)
│   └── integration/
├── vendor/
│   └── camoufox-fork-pin.lock   # daijro/camoufox @ v152.0.4-beta.28 (2026-07-19) + checksum
└── MASTER-PLAN.md
```

Kahin MCP tarafı: `core/driver` çıktısı = `BrowserEngine` alt sınıfı (`kahin/the_twins/` içine, ör. `camoufox.py`); `kahin_browser_start` engine whitelist'ine `"camoufox"` eklenir.

---

## 5. Durum / "Veritabanı" Yapısı

Gerçek (mevcut Kahin kalıcılığı): pattern DB `FateDB` → JSON dosya (`kahin/residual_self/.fate_db.json`); CDP şeması gzip JSON (`the_source/protocol.json.gz`). Yeni katman için: bu sistem ilişkisel veritabanı gerektiren bir iş yükü değil — durum küçük, yerel, tek-makine (ya da makine başına izole) ve yüksek yazma sıklığında. Ağır bir DB motoru (Postgres vb.) gecikme bütçesine aykırı.

**Seçim: gömülü KV (SQLite, WAL modu) — yalnızca kalıcı olması gereken veri için.** Çalışma zamanı durumu (aktif pipe bağlantıları, bekleyen request/response eşleşmeleri, event listener kayıtları) tamamen bellek içi; disk'e hiç yazılmaz.

Kalıcı tutulan varlıklar:

| Varlık | İçerik | Neden kalıcı |
|---|---|---|
| `profiles` | Profil adı, fingerprint seed (BrowserForge çıktısı), proxy/GeoIP eşlemesi, oluşturulma zamanı | Oturumlar arası fingerprint tutarlılığı gerekiyor |
| `sessions` | Session id, bağlı profile, cookie/localStorage snapshot referansı, son aktivite zamanı | Login state korunmalı |
| `fork-pins` | Referans Camoufox commit hash, çıkarılan şema versiyonu, pin tarihi | Şema ile binary'nin senkron kalması izlenmeli |
| `perf-baselines` | Benchmark koşusu sonuçları (bkz. §7), tarih damgalı | Regresyon tespiti için geçmişe kıyas gerekli |

Kalıcı tutulmayan (bilinçli karar): request/response log'ları, ham network trafiği. Bunlar disk I/O'yu gecikme yoluna sokar; gerekirse ayrı, opsiyonel bir debug-mode dosya yazıcısı (append-only, ana yoldan tamamen ayrık) eklenir.

---

## 6. Netleşmesi Gereken Sorular (güncellenmiş hali)

Önceki üç açık soru araştırmayla kapandı; yalnızca biri kullanıcı kararı olarak duruyor:

1. ~~Kahin MCP'nin runtime'ı ve driver-interface şekli~~ — **KAPANDI** (§3.3): Python ≥3.12 + FastMCP stdio; `BrowserEngine` ABC (chassis.py); Zig sidecar + yerel IPC. Faz 5 bloke değil.
2. Hedef eşzamanlı instance sayısı — **hâlâ kullanıcı kararı**, ama mimari gerçekler netleşti: Juggler'da izolasyon birimi browser context (tek process + N context idiomatik; ~10-15MB/context iddiası §7'de ölçülecek). Karar gelene kadar varsayılan desen: tek process + N context; N process yalnızca fingerprint/network ayrımı istendiğinde.
3. ~~Hangi CDP yüzeyi gerçekten kritik~~ — **KAPANDI** (kod gerçeği): Kahin'in 32 tool'u 6 CDP domain kullanıyor: **Page** (enable, navigate, getFrameTree, getLayoutMetrics, captureScreenshot), **Runtime** (enable, evaluate), **Network** (enable — yalnızca pasif event log), **Console** (enable, messageAdded), **Target** (getTargets, createTarget, closeTarget), **Emulation** (setDeviceMetricsOverride). Network interception (request blocking/fulfill) şu an **kullanılmıyor** — yeni yetenek. Faz sırası buna göre düzeltildi (§8).

---

## 7. Performans Bütçesi

"CDP kadar hızlı/hafif" iddiası ölçülebilir olmalı, aksi halde doğrulanamaz bir slogan olur. Baseline: aynı makinede, aynı iş yükünde CDP+Chromium ile Juggler+Camoufox karşılaştırması.

Üretici iddiaları (referans olarak; planın kendi ölçümü bunların yerine geçer): camoufox.com — Camoufox <200MB footprint vs Chrome 800MB+; VulpineOS — context başına ~10-15MB vs Chrome tab ~50-80MB.

Ölçülecek metrikler:
- Komut başına round-trip gecikmesi (p50/p95/p99), domain başına ayrı ayrı (Network, Runtime, DOM/Page, Input).
- Instance başına yerleşik bellek (idle ve yük altında).
- Soğuk başlatma süresi (process spawn → ilk komuta cevap).
- Eşzamanlı N context/instance altında gecikme degradasyonu eğrisi.

Bilinen, kabul edilmesi gereken fark: Firefox ve Chromium farklı motorlar; bazı gecikme farkları protokol değil motor kaynaklı olabilir (ör. sayfa render/layout maliyeti). Bu ayrım benchmark raporunda protokol-kaynaklı vs motor-kaynaklı olarak ayrıştırılır — aksi halde "Juggler yavaş" ile "Firefox bu işte yavaş" birbirine karışır ve yanlış optimizasyon hedefine yol açar. Bu bir dürüstlük notudur, bir bahane değil: ölçülmeden iddia edilemez.

---

## 8. Geliştirme Fazları

**Faz 0 — Protokol çıkarımı.** Extractor + drift-watcher + ilk şema versiyonu. Kaynak: `additions/juggler/protocol/Protocol.js` (tek JS dosyası). Emsal: Kahin'in mevcut SchemaEngine + `protocol.json.gz` deseni. Çıktı: Zig tarafında derleme-zamanı tip üretimi için kullanılacak formatta şema.

**Faz 1 — Transport çekirdeği.** Pipe (FD3/FD4) bağlantısı, `\x00` çerçeveleme, request/response eşleştirme, session demux. Camoufox'a bağlanıp `Browser.enable` handshake'i yapabilmek hedef; henüz domain fonksiyonelliği yok. (CDP WebSocket'e göre daha basit: port yok, el sıkışma yok.)

**Faz 2 — Minimum canlı yüzey.** Browser (createBrowserContext, newPage) + Page (navigate, lifecycle) + Runtime (evaluate). En küçük "uçtan uca çalışan" dilim — Kahin'in mevcut tool yüzeyinin (Page/Runtime/Target) çekirdeğiyle birebir örtüşüyor.

**Faz 3 — Mevcut tool yüzeyinin tamamı.** Target/context yönetimi (getTargets/createTarget/closeTarget  Browser.newPage/context), Console karşılıkları, Emulation karşılıkları, screenshot (screencast). Sıralama artık varsayım değil — Kahin tool'larının gerçek kullanımına göre (§6, Soru 3).

**Faz 4 — Network interception + Input.** NetworkObserver (request interception — Kahin'de şu an olmayan yeni yetenek), input event synthesis (Juggler orijinal user input handler'ları üzerinden).

**Faz 5 — Kahin MCP entegrasyonu.** §3.3 kapandı: `BrowserEngine` alt sınıfı `kahin/the_twins/` içine, engine whitelist güncellemesi, Zig sidecar  Python IPC.

**Faz 6 — Multi-context/multi-instance.** Process Manager: tek process + N context (varsayılan), gerekirse N process, kaynak limiti/health-check.

**Faz 7 — Perf benchmark + hardening.** §7'deki ölçüm seti, regresyon eşiği tanımı, crash-recovery testleri, protokol drift senaryosu simülasyonu (fork güncellenince ne kırılıyor).

Faz sırası bağımlılık zinciri gözetir: 0→1 zorunlu; 2, 1'e bağlı; 3/4 birbirinden bağımsız, paralel yürütülebilir; 5'in dış blokajı kalktı (paralel yürütülebilir); 6/7 her şeyden sonra.

---

## 9. Riskler ve Mitigasyon (güncellenmiş hali)

| Risk | Mitigasyon |
|---|---|
| Juggler resmi dokümante değil, versiyon garantisi yok | Fork commit pin + Protocol Drift Watcher (§2.4). Ek gerçek: `Dispatcher.js` çalışma zamanında Protocol.js'e karşı doğrular — sessiz kırılma zaten kısmen engellenmiş; derleme-zamanı kontrolü çifte güvence. Juggler kodunun sahibi: **microsoft/playwright** (browser_patches/firefox). |
| Camoufox bakımında geçmişte ~1 yıllık boşluk oldu | Gerçek zaman çizelgesi: FF135 build'i Mart 2025'te kaldı; daijro Mart 2025'te hayati tıbbi acil nedeniyle hastaneye kaldırıldı (discussion #310); ~7 ay commit yok (issue #404); coryking fork FF142'ye yükseltti; Clover Labs devraldı (fork 2025-08-16; devir resmi duyuru — discussion #452 ve camoufox.com); FF146 Ocak 2026 — ilk tamamen açık kaynak release (v146.x-beta.25 sonrası tüm kaynak public; ≤v135.0.1-beta.24'te kapalı Canvas patch vardı); FF150 Mayıs 2026; FF152.0.4 Temmuz 2026 (aktif: heydryft, icepaq; daijro pasif katkı). "1 yıllık boşluk" ≈ ~10 aylık sessiz dönemle teyit edildi; **güncel durum aktif**. Mitigasyon aynen geçerli: vendor'lanmış (kendi kontrolünde tutulan, gerekirse fork'un fork'u) binary + kaynak kopyası; upstream'e canlı bağımlılık yok. |
| `daijro/camoufox`  `CloverLabsAI/camoufox` ilişkisi net değildi | **Çözüldü**: daijro/camoufox = master kopya (checkpoint release'ler burada toplanır, resmi release'ler orada yayınlanır); CloverLabsAI/camoufox = Clover Labs'ın aktif geliştirme fork'u (2025-08-16, 221 yıldız; katkıcılar heydryft, icepaq, daijro); VulpineOS/VulpineOS = deneysel hat (per-context fingerprint, hardware spoofing, foxbridge). Pin: **daijro/camoufox** (release kaynağı). Üst akıştaki belirsizlik giderildi; pin güncelleme kararı yine de commit-pin ile sabitlenir. |
| Motor farkı (Firefox vs Chromium) protokol performansıyla karışabilir | §7'de ayrıştırılmış ölçüm |
| Kahin MCP entegrasyon şekli bilinmiyordu | **Çözüldü** (§3.3): Python/FastMCP stdio, `BrowserEngine` ABC, Zig sidecar + IPC. Faz 5 blokajı kalktı. |
| camoufox pip paketi gecikmeli release yayınlıyor | İki kanal gerçeği: `camoufox` (resmi, gecikmeli) vs `cloverlabs-camoufox` (per-release, her release ile güncel). Harness binary'sini kendisi vendor'ladığı için paket gecikmesi çalışmayı etkilemez. |

---

## 10. Kapsam Dışı (bu sürüm için)

- CDP-shim (Seçenek D) — §2.1'de gerekçeyle reddedildi. (Hazır çözüm foxbridge mevcut ama amaç CDP uyumluluğu değil.)
- WebDriver BiDi üzerinden çalışma (Seçenek C) — yalnızca araştırma notu olarak izlenir, implementasyon yok. Gerçek: Firefox 136+ BiDi'yi native WebSocket ile destekliyor — fallback olarak canlı bir seçenek.
- Üçüncü tarafla paylaşım, açık kaynağa çıkarma, dış API — kullanıcı talimatıyla kapsam dışı.
