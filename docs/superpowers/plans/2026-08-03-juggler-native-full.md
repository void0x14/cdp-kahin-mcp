# Kahin Faz 8 — Juggler-Native Tam Otomasyon Motoru: Dünyanın En Güçlü Scraping MCP'si

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox syntax.

**Goal:** Kahin'in Camoufox motorunu (mirage) dünyanın en güçlü anti-detect otomasyon MCP'sine dönüştürmek. Sidecar, Juggler protokolünü DOĞRUDAN konuşur (6 domain, 81 method, 37 event — hiçbirine çeviri yok, tamamı erişilebilir). Juggler'da olmayan yetenekler (DOM motoru, Input, Storage, Network buffer, Worker/WebSocket/Dialog/Download yönetimi, fullScreenshot, multi-tab) SIFIRDAN, sıfır dependency, Runtime.evaluate + Page.dispatch* + event buffer üstünde inşa edilir. Sonuç: Chromium CDP'den fazla, kullanışlı, boşu olmayan API yüzeyi. Phantom-state problemi de aynı harekette yok edilir.

**Architecture:** İki katman: (1) **Juggler-native wire** — `{"id","method","params","sessionId?"}`: ipc_main.zig ince router, Juggler method adlarını aynen geçirir, event'ler aynen forward edilir. (2) **Kahin uzantı domain'leri** — `DOM.*`, `Input.*`, `PageEx.*`, `Session.*`, `NetworkEx.*`, `Storage.*`, `Emulation.*`, `Worker.*`, `WebSocket.*`, `AX.*`, `Dialog.*`, `FileChooser.*`, `Download.*`, `Video.*`, `Screencast.*`, `Console.*`, `Errors.*`, `RuntimeEx.*` — her biri altında ya Juggler method'u (eşleme tablosu, çeviri DEĞİL) ya gömülü JS motoru + event buffer.

**Tech Stack:** Zig 0.16.0 PINNED, Python 3.13 (uv), pytest (`uv run pytest tests/ -q`), vendor sidecar binary. Build: `zig build test` (camoufox-harness/core) + `bash scripts/build-sidecar.sh` (vendor günceller).

## Global Constraints

- Zig: `~/.local/opt/zig-x86_64-linux-0.16.0/zig` (0.17 kırıyor). Her Zig değişikliğinde `bash scripts/build-sidecar.sh` → `camoufox-harness/vendor/bin/kahin-sidecar` güncellenir VE commit'lenir.
- Wire protocol: `method` = Juggler method adı (`Page.navigate`, `Browser.setCookies`) veya Kahin uzantı method'u (`DOM.queryAll`). `domain/command` şeması TERK EDİLİR.
- CDP-şekilli taklit KALDIRILIR: Network.enable/Console.enable no-op, Target domain (createTarget/closeTarget/getTargets), Console.messageAdded çevirisi, Emulation.setDeviceMetricsOverride çevirisi.
- Juggler passthrough KURALI: `Browser.*`/`Page.*`/`Runtime.*`/`Network.*`/`Heap.*`/`Accessibility.*` method'ları aynen geçer (sessionId yoksa current page, varsa o session; Browser/Heap/Accessibility root). Kahin domain'leri (Emulation vb.) altındaki Juggler method'una EŞLEME TABLOSU ile bağlanır — bu çeviri değil, Kahin API katmanıdır.
- Event forward: Juggler event adları AYNEN (`Page.dialogOpened`, `Runtime.console`, `Browser.attachedToTarget`) — dönüştürme YOK, sadece `sessionId` eklenir.
- `Browser.health` KORUNUR (yerel liveness komutu, Juggler'a gitmez).
- Anti-detect yüzeyi (userPrefs, setInitScripts, addBinding, proxy, geolocation, locale/timezone/UA/platform, permissions, video recording) HEPSİ erişilebilir — boş API yok.
- Sayfa PDF yok (Firefox'ta print-to-PDF yok) → inşa edilmez; yerine setEmulatedMedia{type:"print"} + fullScreenshot.
- Mevcut yeşil korunur: `zig build test` (177), pytest koleksiyonu, `ruff check kahin/` yeni hata 0.
- Branch: `feat/faz8-phantom-fix`. Araştırma: `.superpowers/sdd/research-juggler-full.md` (tam method tabloları, eşleme kararları — brief'lerin kaynağı).

---

### Task 1: Juggler-native wire protocol — ipc_main.zig router dönüşümü

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig`
- Modify: `camoufox-harness/core/tests.zig` (mevcut testler + yeni router testleri)
- Rebuild: `scripts/build-sidecar.sh`
- Test: `tests/test_mirage_ipc.py` (fake sidecar şeması güncellenir)

**Interfaces:**
- Produces: req `{"id":N,"method":"<M>","params":{...},"sessionId?":"..."}` → `{"id":N,"result":{...}}` | `{"id":N,"error":{"code":-32601,"message":"..."}}`; evt `{"method":"<M>","params":{...},"sessionId":"..."}`.
- Routing: `Browser.*`/`Heap.*`/`Accessibility.*` → root session; `Page.*`/`Runtime.*`/`Network.*` → sessionId veya current page session (yoksa `-32600 "no page target; create one first"`); `Browser.health` → yerel; bilinmeyen root-domain method → root'a passthrough. Bütün domain'ler method prefix'inden ayrışır — `domain`/`command` alanları YOK ARTIK.
- Consumes: driver `send(session_id, method, params_json, timeout)` (mevcut, Juggler-native).

- [ ] **Step 1: Zig test — router (RED)**
  `tests.zig`'e (sahte driver/browser fixture deseni — pipe.zig:382-399'daki gibi mevcut deseni izle): (a) `{"id":1,"method":"Page.navigate","params":{"url":"..."}}` → driver'a `Page.navigate` iletilir; (b) `{"id":2,"method":"Browser.health"}` → yerel cevap (Juggler'a GİTMEZ); (c) `{"id":3,"method":"Browser.setCookies","params":{...}}` → root'a passthrough; (d) `{"id":4,"method":"Page.navigate"}` page yokken → `-32600`; (e) `{"id":5,"method":"Network.enable"}` → artık no-op DEĞİL — Juggler'a gider (page session; sayfa yoksa -32600; "Network.enable" diye Juggler method'u olmadığından browser'dan -32601 gelir — net hata).
- [ ] **Step 2: Run `zig build test` → RED doğrula** (mevcut router domain bekler, yeni istekler fail).
- [ ] **Step 3: ipc_main.zig'i yeniden yaz**
  `processRequest`: `obj.get("method")` (string), optional `sessionId` (string). Prefix ayrıştır: ilk `.`'a kadar domain. Router tablosu (research dosyasındaki domain listesi):
  - Yerel: `Browser.health` (mevcut implementasyon aynen).
  - Juggler domain'leri (aynı isim passthrough): `Browser.*` → sessionId ?? root; `Heap.*`, `Accessibility.*` → root (targets: browser değil page ama browser handler'ına gider — Juggler'da session domain'i yok, root üzerinden yönlendirilir; doğrulama: Heap/Accessibility targets page ama Juggler pipe'ta browser session'ından dağıtılır — driver.send(session, "Heap.collectGarbage") şeklinde page session gerekli olabilir; GERÇEK: Juggler'da Heap ve Accessibility method'ları page target'ında — attachedToTarget sonrası page session'ına gider. Karar: `Heap.*`/`Accessibility.*` → page session (current veya sessionId), tıpkı Page.* gibi).
  - `Page.*`/`Runtime.*`/`Network.*` → page session (sessionId ?? current ?? hata -32600).
  - Geri kalan her şey → `d.send(null, method, params_json)` root passthrough (Juggler tam kapsam; browser bilinmeyen method'da -32601 döndürür — net).
  Handler'lar: handleTarget/handleBrowser/handlePage/handleRuntime/handleEmulation + Network/Console enable no-op + handleSetExtraHTTPHeaders + evaluateWithRetry → YERİNE: tek `handleJuggler(session_id, method, params)` passthrough fonksiyonu. `Page.navigate`/`Runtime.evaluate`/`Page.captureScreenshot`'un özel işleme ihtiyacı KALMADI (Juggler-native: navigate → `d.send(session,"Page.navigate",params)` direkt; evaluate → `d.send(session,"Runtime.evaluate",params)` direkt + mevcut `evaluateWithRetry` mantığını `Runtime.evaluate` method'una özel tut (context race) — tek özel durum). ensurePage/currentPage/setCurrentTarget KORUNUR (current page takibi; `Session.*` Task 3'te genişletecek). `getFrameTree`/`getTargets` benzeri türetilmiş komutlar SİLİNİR (Task 3 Session domain'inde Juggler state'inden yeniden inşa edilir — bu task'ta `Target.*` bilinmeyen domain → root passthrough olur, net hata alınır, sorun değil).
  Docstring: yeni wire şeması + routing tablosu + "Juggler-native — CDP-shaped translation removed".
- [ ] **Step 4: `zig build test` + `zig build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar` → GREEN**
- [ ] **Step 5: Fake sidecar Python testleri uyumla**
  `tests/test_mirage_ipc.py`'deki FAKE_SIDECAR: `{"domain","command"}` → `{"method": "Domain.command"}` şemasına güncelle; testlerin gönderimini de güncelle. Mirage.send_cdp'yi ŞU ANLIK koru (Python tarafı Task 4'te değişir): `send_cdp(domain, command, params)` → `{"method": f"{domain}.{command}", "params": params}` gönderir (imza aynı, wire Juggler-native). Run `uv run pytest tests/test_mirage_ipc.py -q` → PASS.
- [ ] **Step 6: `bash scripts/build-sidecar.sh` (vendor güncelle)**
- [ ] **Step 7: Commit** — `git add camoufox-harness/core/ camoufox-harness/vendor/bin/ tests/test_mirage_ipc.py && git commit -m "refactor(sidecar): Juggler-native wire protocol — method routing, CDP-shaped layer removed"`

---

### Task 2: Kahin DOM motoru + Input + PageEx + Storage (Zig, gömülü JS)

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig` (DOM/Input/PageEx/Storage handler'ları + gömülü JS motoru)
- Modify: `camoufox-harness/core/driver.zig` (gerekirse değil — evaluate passthrough yeter; kontrol et)
- Modify: `camoufox-harness/core/tests.zig` (iletim testleri)
- Rebuild: `scripts/build-sidecar.sh`
- Test: `tests/test_mirage_ipc.py` (e2e DOM akışı, gerçek Camoufox)

**Interfaces:**
- Consumes: Task 1 router (Juggler passthrough çalışıyor), `Runtime.evaluate` passthrough, `Page.dispatchMouseEvent`/`Page.insertText`/`Page.setFileInputFiles`/`Page.getContentQuads`/`Page.screenshot` passthrough.
- Produces: Kahin DOM motoru — page'e gönderilen tek bir gömülü JS sarmalayıcı (`kahin-dom.js` — ipc_main.zig içinde `const kahin_dom_js` olarak, returnByValue=true evaluate edilir; input'u JSON arg olarak alır, çıktıyı JSON döndürür) + method işleyiciler:
  - `DOM.queryAll{selector}` → `[{index, tag, id, classes, text, visible, attrs{name:value}, rect{x,y,width,height}}]`
  - `DOM.getHTML{selector?}` → string (document.documentElement.outerHTML)
  - `DOM.getText{selector}` → string (innerText)
  - `DOM.getAttr{selector, name}` → string? 
  - `DOM.getBox{selector}` → rect
  - `DOM.getValue{selector}` → input/select/textarea value
  - `DOM.setValue{selector, value}` → JS set + dispatch input/change event (native setter ile React uyumlu)
  - `DOM.click{selector, button?, clickCount?}` → JS: rect hesapla (getContentQuads veya JS rect), scrollIntoView, sonra `Page.dispatchMouseEvent` mousedown/mouseup x3 (button, clickCount) — gerçek olaylar
  - `DOM.type{selector, text, delayMs?}` → focus + `Page.insertText` (karakter başına delay isteğe bağlı)
  - `DOM.press{selector?, key}` → focus + `Page.dispatchKeyEvent` keydown/keypress/keyup (keyCode/code Juggler bekliyor — JS'ten key bilgisi al, `Page.dispatchKeyEvent` zorunlu alanları: type,key,keyCode,location,code,repeat,text?)
  - `DOM.waitFor{selector, timeoutMs}` → polling evaluate (DOM.queryAll döngüsü), timeout → `-32000 "timeout waiting for selector"`
  - `DOM.waitForGone{selector, timeoutMs}` → tersi
  - `DOM.focus{selector}` / `DOM.blur{selector}` → JS
  - `DOM.scrollIntoView{selector}` → JS
  - `DOM.select{selector, values}` → JS (option select + change event)
  - `DOM.check{selector}` / `DOM.uncheck{selector}` → JS
  - `DOM.hover{selector}` → JS rect + `Page.dispatchMouseEvent` mousemove (mousemove tek başına)
  - `DOM.upload{selector, files}` → JS file input bul + `Page.setFileInputFiles{frameId, objectId, files}` (objectId'yi JS'ten remoteObject olarak al — adoptNode/describeNode akışı; dosya input için Juggler setFileInputFiles objectId ister)
  - `Input.click{x,y,button?,clickCount?,modifiers?}` → `Page.dispatchMouseEvent` mousedown+up
  - `Input.dblClick{x,y}` / `Input.rightClick{x,y}` → mouse event zinciri
  - `Input.move{x,y}` → mousemove; `Input.wheel{x,y,deltaX,deltaY}` → dispatchWheelEvent; `Input.keyDown{key}` / `Input.keyUp{key}` / `Input.keyPress{key}` → dispatchKeyEvent; `Input.typeText{text,delayMs?}` → insertText; `Input.tap{x,y}` → dispatchTapEvent; `Input.dragDrop{x1,y1,x2,y2}` → mousedown/mousemove/mouseup zinciri
  - `PageEx.screenshot{format?,quality?,fullPage?}` → Juggler Page.screenshot (clip parametresi: viewport boyutu; fullPage=true → Runtime.evaluate ile scrollHeight, adım adım screenshot + yapıştırma verisi — Python tarafı birleştirir veya sidecar stitcher: KAĞIT KESME — sidecar veri listesi döndürür `{parts: [{data, yOffset}]}`)
  - `PageEx.getFrameTree{}` → Juggler frame state (driver'da frame map zaten var — frameAttached/Detached + navigationCommitted'dan derlenen) → `{frameId, url, name, parentFrameId, children}`
  - `PageEx.getTitle{}` / `PageEx.getUrl{}` → evaluate document.title/location.href
  - `PageEx.getHTML{}` → document.documentElement.outerHTML
  - `PageEx.getPerformance{}` → JS: navigation timing, resource timing özeti
  - `PageEx.getMemory{}` → performance.memory (varsa)
  - `Storage.localStorageGet{keys?}` → JS localStorage; `Storage.localStorageSet{pairs}`; `Storage.localStorageClear{}`; `Storage.sessionStorageGet/Set/Clear`; `Storage.indexedDBDatabases{}` → indexedDB.databases()
  - Tüm DOM/Storage JS'i: page'te çalışır (current page), exception → `-32000 "DOM error: <msg>"`.
- Hata şeması: `{"error":{"code":-32000,"message":"..."}}` — net, İngilizce, AI'a yönelik.

- [ ] **Step 1: Zig test — iletim (RED)**: tests.zig: `DOM.queryAll` → Runtime.evaluate çağrısı (method adı, params içinde expression + arg); `Input.click` → dispatchMouseEvent çağrısı; `Storage.localStorageGet` → evaluate çağrısı.
- [ ] **Step 2: Run `zig build test` → RED** (domain handler yok, "unknown domain" → şimdi root passthrough — hata değişik olabilir; testlerin iletim iddiası fail eder).
- [ ] **Step 3: kahin-dom.js gömülü motor + handler'ları implemente et** (ipc_main.zig; JS sarmalayıcı string'i ayrı bölüm — kabinler: `fn domQueryAll(js: []const u8) []const u8` gibi yardımcılar evaluate payload üretir).
- [ ] **Step 4: `zig build test` + `zig build-exe ...` → GREEN**
- [ ] **Step 5: E2E — gerçek Camoufox**: `tests/test_mirage_ipc.py`: data: URL ile form sayfası → `DOM.queryAll` (element listesi), `DOM.type` + `DOM.click` (submit butonu), `DOM.getText`, `PageEx.screenshot{fullPage:true}` (parts döndü), `Storage.localStorageSet/Get`. Mevcut e2e test desenini izle (Camoufox'u açar, sidecar'ı çalıştırır).
- [ ] **Step 6: `bash scripts/build-sidecar.sh`**
- [ ] **Step 7: Commit** — `git add camoufox-harness/core/ camoufox-harness/vendor/bin/ tests/test_mirage_ipc.py && git commit -m "feat(sidecar): Kahin DOM engine, Input, PageEx, Storage — built on Juggler evaluate + input dispatch"`

---

### Task 3: Zig uzantıları 2 — Session, NetworkEx, Emulation eşleme, buffer domain'leri

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig` (Session/NetworkEx/Emulation/Worker/WebSocket/AX/Dialog/FileChooser/Download/Video/Screencast/Console/Errors handler'ları + event buffer'ları)
- Modify: `camoufox-harness/core/tests.zig`
- Rebuild: `scripts/build-sidecar.sh`
- Test: `tests/test_mirage_ipc.py`

**Interfaces:**
- Consumes: Task 1 router, Task 2 pattern (handler + buffer + passthrough eşleme tablosu).
- Produces (hepsi gerçek Juggler verisi üstünde, boş API yok):
  - **Session** (multi-tab): `Session.listPages{}` → `[{targetId, browserContextId, url, title}]` (driver pages map + evaluate title/url — lazy); `Session.newPage{url?, browserContextId?}` → Browser.newPage (current_target değiştirir); `Session.switchPage{targetId}` → current_target set + `Page.bringToFront` (page session'ına); `Session.closePage{targetId}` → `Page.close`; `Session.getPageInfo{targetId?}` → {targetId, url, title, frameTree}; `Session.bringToFront{targetId?}` → `Page.bringToFront`; `Session.createContext{removeOnDetach?}` → Browser.createBrowserContext; `Session.removeContext{browserContextId}` → Browser.removeBrowserContext.
  - **NetworkEx** (event buffer — driver'ın zaten işlediği Network.requestWillBeSent/responseReceived/requestFinished/requestFailed'ı sidecar'da buffer'la): `NetworkEx.listRequests{limit?, method?, urlContains?}` → `[{requestId, url, method, status?, statusText?, headers, postData?, isIntercepted, timing?, errorCode?, finished}]`; `NetworkEx.getResponseBody{requestId}` → Juggler Network.getResponseBody (base64body → decoded string + evicted); `NetworkEx.getRequest{requestId}` → detay; `NetworkEx.waitForRequest{urlContains?, method?, timeoutMs}` → event loop'u polling (buffer + yeni event'ler, timeout → -32000); `NetworkEx.waitForResponse{same}`; `NetworkEx.setInterception{enabled}` → Network.setRequestInterception (page); `NetworkEx.abortRequest{requestId, errorCode?}` / `NetworkEx.resumeRequest{requestId, url?, method?, headers?, postData?}` / `NetworkEx.fulfillRequest{requestId, status, statusText, headers, base64body?}` → Juggler aynı isimli method'lar; `NetworkEx.setHeaders{headers}` → Network.setExtraHTTPHeaders; `NetworkEx.clearCache{}` → Browser.clearCache.
  - **Emulation** (eşleme tablosu — Kahin method adı, altında Juggler method'u; çeviri değil, katman): `Emulation.setViewport{width, height, deviceScaleFactor?}` → Browser.setDefaultViewport; `Emulation.setZoom{zoom}` → Page.setZoom; `Emulation.setUserAgent{userAgent?}` → Browser.setUserAgentOverride; `Emulation.setPlatform{platform?}` → Browser.setPlatformOverride; `Emulation.setLocale{locale?}` → Browser.setLocaleOverride; `Emulation.setTimezone{timezoneId?}` → Browser.setTimezoneOverride; `Emulation.setGeolocation{latitude, longitude, accuracy?}` → Browser.setGeolocationOverride; `Emulation.setProxy{type, host, port, bypass?, username?, password?}` → Browser.setBrowserProxy (context yoksa) — browserContextId param'ı varsa Browser.setContextProxy; `Emulation.setColorScheme{colorScheme?}` → Browser.setColorScheme; `Emulation.setReducedMotion{reducedMotion?}` → Browser.setReducedMotion; `Emulation.setForcedColors{forcedColors?}` → Browser.setForcedColors; `Emulation.setContrast{contrast?}` → Browser.setContrast; `Emulation.setOnline{online}` → Browser.setOnlineOverride; `Emulation.setTouch{hasTouch?}` → Browser.setTouchOverride; `Emulation.setJavaScript{enabled}` → Browser.setJavaScriptDisabled{javaScriptDisabled: !enabled}; `Emulation.setBypassCSP{bypass}` → Browser.setBypassCSP; `Emulation.setIgnoreHTTPSErrors{ignore}` → Browser.setIgnoreHTTPSErrors; `Emulation.setHTTPCredentials{username?, password?, origin?}` → Browser.setHTTPCredentials; `Emulation.setDownloadOptions{behavior?, downloadsDir?}` → Browser.setDownloadOptions; `Emulation.setInitScripts{scripts}` → Browser.setInitScripts; `Emulation.addBinding{name, script, worldName?}` → Browser.addBinding; `Emulation.grantPermissions{origin, permissions}` → Browser.grantPermissions; `Emulation.resetPermissions{}` → Browser.resetPermissions; `Emulation.setEmulatedMedia{type?, colorScheme?, reducedMotion?, forcedColors?, contrast?}` → Page.setEmulatedMedia; `Emulation.getInfo{}` → Browser.getInfo. (Eşleme tablosu research dosyasında.)
  - **Worker**: `Worker.listWorkers{}` → buffer'dan (Page.workerCreated/workerDestroyed) → `[{workerId, frameId, url, alive}]`; `Worker.sendMessage{workerId, message}` → Page.sendMessageToWorker (frameId'yi buffer'dan); `Worker.waitForMessage{workerId?, timeoutMs}` → Page.dispatchMessageFromWorker buffer'ı.
  - **WebSocket**: `WebSocket.listSockets{}` → buffer (webSocketCreated/Opened/Closed); `WebSocket.listFrames{wsid?}` → webSocketFrameSent/Received buffer'ı; `WebSocket.waitForFrame{wsid?, opcode?, timeoutMs}`.
  - **AX**: `AX.getTree{}` → Accessibility.getFullAXTree passthrough (root'a değil page session'a!); `AX.query{role?, nameContains?, name?}` → getFullAXTree + JSON filtreleme (recursive) → subtree listesi.
  - **Dialog**: `Dialog.list{}` → buffer (dialogOpened/dialogClosed) → `[{dialogId, type, message, defaultValue?, open}]`; `Dialog.accept{dialogId?, promptText?}` → Page.handleDialog{accept:true} (dialogId yoksa en son açık); `Dialog.dismiss{dialogId?}` → handleDialog{accept:false}; `Dialog.prompt{dialogId?, text}` → handleDialog{accept:true, promptText}.
  - **FileChooser**: `FileChooser.setIntercept{enabled}` → Page.setInterceptFileChooserDialog; `FileChooser.upload{files}` → buffer'daki en son fileChooserOpened element (objectId) + Page.setFileInputFiles{files} (frameId = executionContextId'den frame bul — auxData).
  - **Download**: `Download.list{}` → buffer (downloadCreated/downloadFinished); `Download.cancel{uuid}` → Browser.cancelDownload; `Download.waitFor{suggestedFileNameContains?, timeoutMs}`.
  - **Video**: `Video.startRecording{dir, width?, height?}` → Browser.setVideoRecordingOptions (browserContextId default ctx); `Video.stopRecording{}` → setVideoRecordingOptions{options: null} → buffer'dan videoRecordingStarted/Finished → son dosya yolu.
  - **Screencast**: `Screencast.start{width?, height?, quality?}` → Page.startScreencast; `Screencast.stop{}` → Page.stopScreencast; `Screencast.ack{screencastId}` → Page.screencastFrameAck; `Screencast.frames{}` → buffer (Page.screencastFrame).
  - **Console**: `Console.getLog{limit?}` → Runtime.console buffer'ı (arg'lar value'ya çözülmüş) → `[{type, text, url, line, column, timestamp}]`; `Console.waitFor{textContains?, type?, timeoutMs}`.
  - **Errors**: `Errors.list{}` → Page.uncaughtError buffer'ı.
  - `Browser.getInfo{}` → passthrough; `Browser.clearCache{}` → passthrough.
- Buffer tasarımı: `ipc_main.zig`'de sabit boyutlu dairesel buffer'lar (ör. `network_reqs: [256]NetworkReq`, `dialogs: [16]DialogState`, `workers: [32]WorkerState`, `ws: [32]WSState`, `downloads: [16]DownloadState`, `console_msgs: [256]ConsoleMsg`, `errors: [64]ErrorState`, `screencast_frames: [64]...`) — her Juggler event'i emitEvent yolunda hem forward hem buffer'a yazılır. Allocator: arena (her loop'te reset) yerine kalıcı a'dan; overflow → en eskiyi düşür (buffer full). KİMSE dışarıya boş data yazmaz — Juggler event'i gelmemişse boş liste + `"buffer"` bilgisi.
- `Session.switchPage` için targetId→page session çözümü driver.pages map'inden (mevcut).

- [ ] **Step 1: Zig test (RED)**: tests.zig: `Session.newPage` → Browser.newPage iletimi; `NetworkEx.listRequests` → buffer (sahte requestWillBeSent event'i enjekte et) → listelenir; `Emulation.setUserAgent` → Browser.setUserAgentOverride iletimi; `Dialog.accept` → handleDialog iletimi.
- [ ] **Step 2: Run `zig build test` → RED**
- [ ] **Step 3: Buffer'lar + handler'lar** (yukarıdaki tablo; eşleme tablosu ve buffer tanımları research dosyasından).
- [ ] **Step 4: `zig build test` + `zig build-exe ...` → GREEN**
- [ ] **Step 5: E2E — gerçek Camoufox**: `tests/test_mirage_ipc.py`: 2 sayfa aç (Session.newPage x2 + switchPage + getPageInfo), NetworkEx.listRequests (google/example veri sayfası), Dialog accept (data: URL JS alert), Worker/WebSocket/Download buffer (data URL veya blob ile — varsa), Emulation.setUserAgent + getInfo round-trip.
- [ ] **Step 6: `bash scripts/build-sidecar.sh`**
- [ ] **Step 7: Commit** — `git add camoufox-harness/core/ camoufox-harness/vendor/bin/ tests/test_mirage_ipc.py && git commit -m "feat(sidecar): Session, NetworkEx, Emulation mapping, Worker/WS/AX/Dialog/Download/Video/Screencast/Console/Errors buffers"`

---

### Task 4: Python katmanı — Mirage.call + multi-tab + tüm tool'lar + phantom fix

**Files:**
- Modify: `kahin/the_twins/mirage.py` (call API, session, liveness, event erişimi)
- Modify: `kahin/the_twins/chassis.py` (soyut is_alive/on_death/call)
- Modify: `kahin/the_twins/shadow.py` (Obscura is_alive — CDP tarafı)
- Modify: `kahin/oracle.py` (liveness, hata yutma kaldırma, ~25 yeni tool, reader-death temizliği, Network/Console enable çağrıları KALDIRILIR)
- Modify: `kahin/_healer.py` (gerçek bağlama)
- Modify: `AGENTS.md` (yeni tool listesi)
- Test: `tests/test_mirage_ipc.py`, `tests/test_oracle.py`, `tests/test_phantom.py` (yeni), `tests/test_e2e.py`

**Interfaces:**
- Consumes: Task 1-3 wire (method tabanlı) + Browser.health + buffer domain'leri.
- Produces:
  - `Mirage.call(method, params=None, session_id=None) -> dict` (wire: `{"id":N,"method":method,"params":params,"sessionId":session_id}`); `send_cdp` KALDIRILIR — oracle dahil herkes `call` kullanır.
  - `Mirage.create_page(url=None) -> target_id` (Session.newPage), `close_page(target_id)`, `switch_page(target_id)`, `list_pages() -> list[dict]` (Session.listPages).
  - `Mirage.events: list[dict]` (reader'dan gelen tüm event'ler — `{method, params, sessionId, timestamp}`) + `Mirage.event_buffer` thread-safe; `wait_for_event(method, timeout, predicate?)`.
  - Liveness: `Mirage.is_alive() -> bool` (process poll), `Mirage.on_death(cb)`, `start()` sonunda `call("Browser.health")` → alive değilse RuntimeError (boot doğrulaması); reader-death → `_current_engine = None` + `clear_state()` (oracle callback).
  - Oracle: `_require_engine` liveness kontrolü (ölü → net RuntimeError "engine is dead — call browser_start"), `browser_start` ölü engine'i yeniden başlatır + Network.enable/Console.enable çağrıları YOK ARTIK; `kahin_browser_execute_cdp` → `call(domain+"."+command)`; event handler: Juggler-native method adları (Runtime.console → console, Page.uncaughtError → errors, Network.requestWillBeSent → network, Browser.downloadCreated → downloads...).
  - Yeni tool'lar (~25): `kahin_dom_query`, `kahin_dom_get_text`, `kahin_dom_get_html`, `kahin_dom_click`, `kahin_dom_type`, `kahin_dom_press`, `kahin_dom_wait_for`, `kahin_dom_get_box`, `kahin_dom_upload`, `kahin_page_screenshot` (fullPage seçeneği + parts birleştirme: parçaları PNG olarak yan yana/dikey stitch — PIL GEREKMEZ: data URL'leri base64 birleştir, Python tarafında PNG birleştirmek için zlib+struct ile minimal PNG stitch yaz veya parçaları ayrı döndür — KARAR: sidecar'dan `{parts:[{data,yOffset}]}` → oracle `data` listesi döndürür; AI tüketici birleştirir veya kullanıcıya bırak — basit, boş değil), `kahin_page_frame_tree`, `kahin_page_get_title`, `kahin_page_get_url`, `kahin_tab_new`, `kahin_tab_switch`, `kahin_tab_close`, `kahin_tab_list`, `kahin_network_requests`, `kahin_network_body`, `kahin_network_wait`, `kahin_network_intercept`, `kahin_storage_local`, `kahin_storage_session`, `kahin_cookie_get`, `kahin_cookie_set`, `kahin_cookie_clear`, `kahin_emulation_set_viewport`, `kahin_emulation_set_user_agent`, `kahin_emulation_set_proxy`, `kahin_emulation_set_geolocation`, `kahin_emulation_set_locale`, `kahin_emulation_set_timezone`, `kahin_emulation_set_init_script`, `kahin_emulation_add_binding`, `kahin_dialog_list`, `kahin_dialog_accept`, `kahin_dialog_dismiss`, `kahin_worker_list`, `kahin_ws_list`, `kahin_ax_tree`, `kahin_download_list`, `kahin_download_cancel`, `kahin_video_start`, `kahin_video_stop`, `kahin_screencast_start`, `kahin_screencast_stop`, `kahin_console_log`, `kahin_errors_list`, `kahin_engine_health`. (İsimler implementer'da son hallerini alır — AGENTS.md tablosu + test_oracle.py tool sayısı güncellenir.)
  - Tool kayıt deseni: mevcut `_mcp_tool`/register deseni (oracle.py — 32 tool). Yeni tool'lar aynı desende; hata mesajları net, AI'a yönelik, exception → tool error text.
  - Phantom fix entegrasyonu: `test_phantom.py` (yeni): (a) ölü process → `is_alive()=False`; (b) `browser_start` ölü engine üstüne → yeniden başlar; (c) `_require_engine` ölü → net RuntimeError; (d) reader-death → `_current_engine` None + state temiz; (e) start'ta health fail → RuntimeError.
  - Healer: `bind_state`/`bind_engine` (mevcut imza) — oracle'a bağlanır, RESTART_ENGINE gerçek çalışır (engine.start yeniden).

- [ ] **Step 1: Failing testler yaz**: test_mirage_ipc.py: `call("Page.navigate")` fake sidecar şeması; create_page/switch_page/list_pages; is_alive (canlı/dead); on_death (process exit); start health fail → RuntimeError. test_phantom.py (yeni): yukarıdaki 5 senaryo. test_oracle.py: tool sayısı + `_require_engine` ölü davranışı.
- [ ] **Step 2: `uv run pytest tests/test_mirage_ipc.py tests/test_phantom.py tests/test_oracle.py -q` → RED**
- [ ] **Step 3: mirage.py** (call + session + liveness + events)
- [ ] **Step 4: oracle.py** (liveness + tool'lar + Network/Console enable kaldırma) + shadow.py is_alive + healer bağlama
- [ ] **Step 5: AGENTS.md** — tool tablosu güncelle (yeni tool'lar + "Juggler-native" notu; 32 → ~70 tool)
- [ ] **Step 6: `uv run pytest tests/ -q` → GREEN** (tümü: 102 + yeniler)
- [ ] **Step 7: Commit** — `git add kahin/ tests/ AGENTS.md && git commit -m "feat(oracle): Mirage.call, multi-tab, liveness-aware engine, 40+ new MCP tools"`

---

### Task 5: E2E doğrulama + tam regresyon + final review

**Files:**
- Modify: `tests/test_mirage_ipc.py` (kapsamlı gerçek Camoufox e2e)
- Run: tam regresyon (pytest + zig + build + ruff)
- Final: tüm branch review (superpowers:requesting-code-review)

**Interfaces:**
- Consumes: Task 1-4.

- [ ] **Step 1: Gerçek Camoufox tam e2e**: (a) multi-tab: 2 sayfa, switch, evaluate; (b) cookie set/get round-trip; (c) network body (getResponseBody); (d) dialog: alert → accept; (e) kill testi: health'ten pid → SIGKILL → sonraki komut hızlı net hata + is_alive False; (f) DOM akışı (type/click/getText) data URL form; (g) DOM.waitFor; (h) storage localStorage; (i) Emulation.setUserAgent → getInfo değişmedi doğrulaması (UA'yı evaluate ile doğrula); (j) fullScreenshot parts.
- [ ] **Step 2: `uv run pytest tests/ -q` + `zig build test` (camoufox-harness/core) + `bash scripts/build-sidecar.sh` + `ruff check kahin/` → hepsi yeşil**
- [ ] **Step 3: Commit** — `git add tests/ && git commit -m "test(mirage): full e2e — multi-tab, cookies, network body, dialog, kill detection, DOM engine"`
- [ ] **Step 4: Final whole-branch review** (code-reviewer subagent; MERGE_BASE = `git merge-base main HEAD`)
- [ ] **Step 5: Final review bulgularını tek fix subagent'ı ile düzelt + re-review**
- [ ] **Step 6: Kullanıcıya özet + bir sonraki faz önerisi** (profil yönetimi, fingerprint rotasyonu, OCR, etc.)

---

## Self-Review

- **Kullanıcı emirleri:** "Juggler protokolünü adam gibi entegre et" → Task 1 (tam passthrough, 6 domain, 81 method). "Juggler'da olmayan eksikleri tek tek sıfır dependency yarat" → Task 2-3 (DOM/Input/Storage/NetworkEx/Session/buffer'lar — hepsi Runtime.evaluate + Juggler method'ları üstünde, sıfır yeni dependency). "Chromium'dan bile çok API, hepsi kullanışlı, boş API yok" → 17 Kahin domain'i, hepsi gerçek veri üretir (event buffer'lar Juggler event'lerinden beslenir; PDF gibi imkânsız olan inşa edilmez, alternatifi konur). "Subagent driven ilerle, plan yaz ve implement et" → SDD workflow: brief → implementer → task-reviewer → fix loop → final review.
- **Fazlandırma riski:** Task 2 ve 3 aynı dosyaya (ipc_main.zig) dokunur — SIRALI çalıştırılır (SDD kuralı: paralel implementer yok); her task commit'li, çakışma yok.
- **Gerçeklik kontrolü:** Heap/Accessibility page target'ında — router'da page session'a yönlendirilir (research dosyasında karar verildi). `Network.enable` artık no-op değil — Juggler'da olmayan method → browser'dan net -32601. `Browser.enable` handshake driver'da kalır (yok sayılmaz).
- **Phantom fix:** Task 4'te (is_alive/on_death/boot health/reader-death/örnek testler) — önceki planın phantom bölümü tamamen buraya taşındı, kapsam korunur.
- **Test kanıtı:** Her task'ta RED→GREEN + e2e + vendor build; Task 5 tam regresyon; her commit'te test komutları raporlanır.
