# Kahin

Chrome DevTools Protocol'unu bilen, doğrulayan ve kontrol eden MCP server.

```bash
uv pip install kahin
# opencode/claude code'a ekle:
# "kahin": { "command": "python3", "args": ["-m", "kahin.oracle"] }
```

## Ne işe yarar?

CDP'yi (Chrome DevTools Protocol) bilirsiniz ya da bilmezsiniz. Kahin bilir.

AI modeller Chrome'un içine girip sayfa gezip kod çalıştırabilir ama CDP'yi ezbere bilmezler — hangi domain hangi komutu alır, hangi parametre zorunludur, hangi event ne zaman fırlar bilmezler. Kahin bunu onlara söyler, yanlış yapınca düzeltir, bilmiyorsa öğretir.

56 domain, 667 komut, 237 event, 609 type — Chrome 148 protokolü gömülü.

## 110 Tool · 4 Kategori Ailesi · 2 Engine

Tool'lar engine-ayrımlı kategori dosyalarında (`kahin/tools/`): paylaşılan çekirdek + Obscura + Camoufox aileleri.

| Kategori | Ne işe yarar | Tool sayısı |
|----------|-------------|-------------|
|  GRIMOIRE — CDP Bilgi | Domain/komut/event/type sorgulama, semantik arama | 7 |
|  SERAPH — Doğrulama | Komut doğrulama, typo tespiti, hata çözümleme | 3 |
|  PILOT — Browser Kontrol | Chrome başlat/durdur, gezin, tıkla, kod çalıştır, ekran görüntüsü | 8 |
|  TRAINMAN — Session | Yeni sayfa aç/kapat, session listele | 4 |
|  DEJA_VU — Debug | CDP event geçmişi, network istekleri, console mesajları | 4 |
|  PROPHECY — Pattern DB | Kullanım desenlerini öğren, sorgula, öner | 5 |
|  HEALER | Hata istatistikleri | 1 |
|  MIRAGE — Camoufox Native (78) | Juggler protokolü üstünde gerçek-zamanlı DOM stream, DOM, Input, PageEx, Tab, Network, Storage, Emulation, Dialog/Download/Worker/WS, Upload, Screencast, Accessibility, Engine sağlığı | 78 |
|  OBSCURA — Ayrı kategori | Obscura'ya özel tool'lar (hazırlanıyor) | 0 |

**Toplam: 110 tool.**

## Bir satırda özet

CDP'yi bilmeyen AI'a Chrome'u kontrol etmeyi öğreten, yanlış yapınca düzelten, her şeyi loglayan MCP.

## Kurulum

Zorunlu: Python 3.12+ · Node.js 18+ (npm launcher için). Kahin'in varsayılan
Camoufox binary'si kurulum sırasında resmi `camoufox fetch` komutuyla hazır edilir;
Chrome/Chromium kurulumu gerekmez.

### Otomatik kurulum — tek komut

```bash
pnpm add -g @kahinmcp/kahin
```

Bu kadar. Kurulum sonrası Kahin, sistemindeki AI CLI araçlarını otomatik tespit eder ve kendini kaydeder:

**Claude Code · Claude Desktop · Cursor · Windsurf · opencode · Codex CLI · Gemini CLI · Zed · VS Code · Cline · Cline CLI · Roo Code · Kilo Code · Continue · Amazon Q · Trae · BoltAI · Antigravity · Amp · MCPorter · GitHub Copilot CLI · Goose**

Mevcut config'lerine dokunmaz, sadece `kahin` girişini ekler (merge). Zaten kayıtlıysa atlar (idempotent). Elle JSON yazmana gerek yok.

Yeni bir araç kurduysan veya kurulum kaçırdıysa:

```bash
kahin setup
```

Otomatik kurulumu devre dışı bırakmak için: `KAHIN_SKIP_AUTO_SETUP=1`

`kahin` çalışınca MCP stdio server'ı başlar. Python ortamı `~/.local/share/kahin/` altında yönetilir.

Kaynak kodunla geliştirme:

```bash
git clone https://gitlab.com/void0x14/kahin-mcp
cd kahin-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Kullanım

AI modeline şunu söyle: **"Kahin MCP'sini kullan."**

Gerisini AI halleder. Ama dilersen tool'ları direkt de çağırabilirsin:

```
→ kahin_list_domains                    → 56 domain listeler
→ kahin_get_command(Page,navigate)      → parametreleri gösterir
→ kahin_validate_command(Page,navigate) → doğrular
→ kahin_browser_start → navigate → extract → screenshot → stop
→ kahin_error_decode(error_code=-32601) → hatayı çözümler
```

`kahin_browser_start` varsayılan olarak Camoufox/Mirage başlatır. Shadow
yalnızca hızlı, görsel olmayan CDP işleri için açıkça seçilir; screenshot,
mobile viewport, screencast, upload veya accessibility isteyen bir tool,
Shadow'ı Kahin içinde Camoufox'a yükseltir. Ajanın başka bir otomasyon
kütüphanesine geçmesi gerekmez.

Camoufox (Juggler native) ile:

```
→ kahin_browser_start(engine="mirage")
→ kahin_mirage_query("#input") → kahin_mirage_type("merhaba")
→ kahin_mirage_click("#btn") → kahin_mirage_get_text("#result")
→ kahin_mirage_query("#btn", frame_id="subframe-...")   # iframe içi erişim
→ kahin_mirage_cookie_set/get/clear → kahin_mirage_storage_local_get
→ kahin_mirage_set_user_agent / set_viewport / set_geolocation
→ kahin_mirage_set_file_chooser_intercept(true) → kahin_mirage_upload_files(["/abs/path"])
→ kahin_mirage_screencast_start → kahin_mirage_screencast_frame (base64 JPEG)
→ kahin_mirage_accessibility_tree → kahin_engine_health
→ kahin_mirage_dom_start → kahin_mirage_dom_snapshot → kahin_mirage_dom_events
→ kahin_mirage_dom_action (snapshot'tan alınan canlı nodeId ile)
```

`kahin_browser_start` tek bir Camoufox/sidecar süreci açar. İlk sayfa işlemi
aynı süreç içinde varsayılan bir sekmeyi tembel olarak oluşturur; sonraki işler
bu sekmeyi yeniden kullanır. Ayrı bir sayfa gerektiğinde yeni tarayıcı başlatmak
yerine `kahin_mirage_tab_new` ve `kahin_mirage_tab_switch` kullanın. Camoufox
aktifken `kahin_execute_cdp` ve diğer CDP araçları, eşdeğer Juggler/Mirage
çağrısına otomatik yönlendirilir ve CDP biçimli sonuç döndürür.

Tam liste için: [AGENTS.md](AGENTS.md). Juggler'ın ajan sözleşmesi ve gerçek-zamanlı
DOM akışı için [AI-native Juggler kılavuzuna](docs/juggler-ai-native.md) bakın.

## Proje Felsefesi

- **Tahmin yok, bilgi var.** AI tahmin etmez, Kahin'in gömülü CDP şemasına bakar.
- **Hata kabul, eğitim zorunlu.** Yanlış komut gelince düzeltir, neden yanlış olduğunu söyler.
- **Minimal bağımlılık.** Temel işlevler için 7 paket, hiçbiri ağır değil.
- **Her şey loglanır.** `kahin/logs/kahin.log` — JSON satırları, her hata kayıt altında.

## Bağımlılıklar

mcp · orjson · Levenshtein · websockets · httpx · camoufox · Pillow · pydantic

## Port Uyarısı

| Port | Kimin | Kullanma |
|------|-------|----------|
| 9222 | Chrome DevTools | RESERVED |
| 9240 | Kusatma Engine | RESERVED |
| 9241 | Eski Shadow/Obscura sabit portu | Varsayılan olarak kullanılmaz |
| — | Mirage/Camoufox | TCP portu yok; Juggler stdio pipe kullanır |

Shadow/Obscura için `kahin_browser_start` port verilmeden çağrıldığında Kahin,
her child için loopback üzerinde kernel'den geçici bir port alır. Böylece aynı
makinedeki bağımsız MCP/OpenCode süreçleri birbirlerinin WebSocket'ine bağlanmaz.
Sabit port yalnızca özellikle `port=...` verildiğinde kullanılır.

## Kendi Kendini Onarma

Kahin'de hata loglama ve kendini onarma sistemi gömülüdür:

- Hatalar `kahin/logs/kahin.log` dosyasına JSON satırları halinde yazılır
- Bağlantı kopması, engine çökmesi, session kaybı gibi durumlarda otomatik kurtarma dener
- `kahin_healer_stats` ile hata istatistikleri sorgulanabilir

## Mimari

```
oracle.py               → MCP server (bootstrap: mcp instance + engine lifecycle + main)
  tools/                → 110 tool, engine-ayrımlı kategori dosyaları
    _common.py          → capability routing, _safe_cdp, _require_engine, _auto_learn
    the_twins/capabilities → motor-yetenek sözleşmesi ve Mirage yükseltme matrisi
    grimoire/seraph/prophecy/healer → CDP bilgi + doğrulama + pattern (paylaşılan)
    pilot/trainman/dejavu           → browser/session/debug (paylaşılan)
    pilot_mirage/trainman_mirage/dejavu_mirage → Camoufox DOM+Input+PageEx / Tab / Network+Console
    storage_mirage/emulation_mirage/dialog_mirage → Storage / Emulation / Dialog+Download+Worker+WS
    pilot_obscura/trainman_obscura/dejavu_obscura → Shadow için ayrılmış paket sınırları; sahte tool kaydetmez
    engine.py           → engine_health
  _healer.py            → Hata yönetimi, loglama, kendini onarma
  the_source/architect  → CDP şema motoru (56 domain, 667 komut)
  the_twins/shadow      → Obscura engine (gerçek Obscura binary, WebSocket CDP)
  the_twins/mirage      → Mirage engine (Zig sidecar, Juggler native pipe, stealth)
  the_twins/chassis     → Ortak engine arayüzü (abstract: call/is_alive/on_death)
  residual_self/fate    → Pattern DB (öğrenme, sorgulama, önerme)
camoufox-harness/       → Zig sidecar (Juggler protocol, vendor binary gömülü)
```

---

## 🗺️ Yol Haritası

- [x] **Juggler protokolü** için AI-native uçtan uca dokümantasyon, kullanım ve pratik örnekleri
- [x] **Camoufox entegrasyonu tamamlandı** — gerçek Camoufox (Zig sidecar + Juggler pipe) varsayılandır; görsel capability isteyen Shadow çağrıları Kahin içinde Mirage'a yükseltilir
- [x] **Obscura entegrasyonu tamamlandı** — gerçek Obscura binary'si (WebSocket CDP) ile çalışıyor, startup problemleri giderildi
- [ ] **SKILLS** destekleri ve konfigre edilebilir kişsiel hazır skills oluşturma özelliği
- [x] **Tek tık kurulum** — `pnpm add -g @kahinmcp/kahin`, sonra `kahin` (ilk çalıştırmada Python ortamını otomatik kurar)
- [ ] **Zero-dependency** hedefi (Go/Rust portu)
- [ ] **LSP modu** — kod içinde hata yakalama, AI'a yanlışını yüzüne vurma
- [x] **Tool sayısı 110** — Camoufox Juggler-native 78 tool (gerçek-zamanlı DOM stream, DOM, Input, Network, Storage, Emulation, Dialog, Tab, Worker/WS, Upload, Screencast, Accessibility) + paylaşılan 32 çekirdek
- [ ] **Obscura ayrı tool'ları** — CDP-yeteneklerine özel pilot_obscura/trainman_obscura/dejavu_obscura kategorilerini doldur

- [ ] **Gerçek zamanlı izleme** — AI'ın Kahin'i nasıl kullandığını canlı gör
- [ ] **Web dashboard** — tool çağrıları, hata oranları, trendler
- [ ] **MCP Ekosistemi** — üçüncü taraf MCP'lere proxy/entegrasyon
- [x] **CLI aracı** — `kahin` komutu ile hızlı sorgulama (npm launcher, `pnpm add -g @kahinmcp/kahin`)
- [ ] **Pasif tarama** — arka planda CDP event'lerini izle, değişiklik olunca bildir
- [ ] **Dokümantasyon sitesi** — kapsamlı kullanım kılavuzu

- [ ] **CDP derleyici** — yeni Chrome sürümlerini otomatik tanıyıp şemayı güncelle
- [ ] **Plugin sistemi** — herkes kendi CDP tool'unu yazıp ekleyebilir
- [ ] **All-in-one MCP** — sadece CDP değil, browser kontrolünün tek adresi
- [ ] **AI davranış analizi** — hangi tool ne sıklıkta kullanılmış, hata trendleri
- [ ] **Paylaşımlı oturum** — ekibin MCP'sini tek merkezden yönet
- [ ] **İleri kendini onarma** — öngörülü hata önleme, otomatik düzeltme

---

## Geliştirme

```bash
uv run pytest -q tests/       # gerçek motor mevcutsa E2E dahil tam suite
uv run ruff check kahin/      # lint
uv run python -m kahin.oracle # manuel başlatma
```
