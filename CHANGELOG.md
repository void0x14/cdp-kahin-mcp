# Changelog

Tüm önemli değişiklikler bu dosyada tutulur. Format: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/) — [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-08-03

### Eklenen
- **65 yeni Juggler-native MCP tool** (toplam 97) — motor ayırımlı kategori dosyaları (`kahin/tools/`):
  - DOM (12): `kahin_mirage_query`, `kahin_mirage_query_all`, `kahin_mirage_click`, `kahin_mirage_type`, `kahin_mirage_get_text`, `kahin_mirage_get_attribute`, `kahin_mirage_set_attribute`, `kahin_mirage_focus`, `kahin_mirage_hover`, `kahin_mirage_get_html`, `kahin_mirage_wait_selector`, `kahin_mirage_get_value`
  - Input (7): mouse_click, mouse_move, mouse_down, mouse_up, key_press, key_text, scroll
  - PageEx (6): reload, go_back, go_forward, stop, frame_tree, page_content
  - Tab/Session (6): tab_new, tab_switch, tab_close, tab_list, tab_bring_front, context_new
  - Network/Console (10): network_requests, get_response_body, intercept_requests, unintercept_requests, network_continue, network_abort, cache_disable, clear_cache, console_log, errors_list
  - Storage (6): cookie_get, cookie_set, cookie_clear, storage_local_get, storage_local_set, storage_session_get
  - Emulation (10): set_user_agent, set_viewport, set_device_scale_factor, set_media, set_touch, set_color_scheme, set_reduced_motion, set_locale, set_timezone, set_geolocation
  - Dialog/Download/Worker/WS (7): dialog_list, dialog_accept, dialog_dismiss, download_list, download_save, worker_list, websocket_list
  - Engine (1): `kahin_engine_health`
- **Phantom liveness sistemi**: `is_alive`/`on_death`, boot'ta `Browser.health` doğrulaması, ölü engine otomatik eviction + state temizliği
- `Mirage.call(method, params, session_id)` API'si — session yönetimi (`create_page/close_page/switch_page/list_pages`)
- `tests/test_phantom.py`, `tests/test_e2e_mirage.py` (gerçek Camoufox e2e: DOM, tab, cookie, dialog, kill, localStorage, emulation)
- pytest-asyncio dev-dep

### Değişen
- Sidecar wire Juggler-native: `{"id","method","params","sessionId?"}` — CDP-şekilli `domain/command` katmanı kaldırıldı
- No-op `Network.enable`/`Console.enable` handler'ları silindi; event adları verbatim forward
- `Network.getResponseBody` → `{base64body, evicted?}`; `Page.dispatchKeyEvent` zorunlu `repeat:bool`
- Screenshot: gerçek viewport ölçümü + `full_page` full-content clip
- Result-less Juggler reply'ları (`{"id":N}`) artık boş `result` olarak kabul ediliyor
- `Page.reload` frameId ile gönderiliyor; frame tree `d.getFrameTree` (gerçek iç içe hiyerarşi)
- oracle.py bootstrap-only — tool tanımları `kahin/tools/` kategori modüllerinde
- Versiyonlar senkronize: package.json / pyproject.toml / `kahin.__version__`

### Düzeltilen
- `@intCast(id)` trap riski (negatif/oversize id → `-32600`)
- `newPage`/`screenshot` hata yutma — artık gerçek hata yanıtı
- devicePixelRatio override: `Page.setViewportSize` yerine `Browser.setDefaultViewport`
- `engine._proc` → `_process` (shadow health pid artık gerçek)
- Sidecar process HUP'ta temiz exit

## [0.1.8] — 2026-08-02

### Değişen
- npm paketi GitLab'a taşındı + GitLab CI publish eklendi (`scripts/npm-publish.sh`, `.gitlab-ci.yml`)
- Versiyon farkı kontrolüyle otomatik yayın

## [0.1.7] — 2026-08-01

### Eklenen
- GitHub auto-publish workflow (`.github/workflows/publish.yml`)
- One-command install: 22 AI CLI istemcisini otomatik tespit + MCP kaydı

## [0.1.0–0.1.6] — 2026-08-01

### Eklenen
- Global npm launcher + auto-setup
- Shadow engine: gerçek Obscura binary (auto-install cascade)
- Mirage engine: gerçek Camoufox via Zig sidecar IPC (Faz 5)
- Zig Juggler harness: pipe transport, Browser/Page/Runtime/Network/Input/Emulation adapters, interception, process manager, perf + crash-recovery
- CDP ansiklopedisi: 56 domain, 667 komut, 237 event, 609 type (Chrome 148)

[0.2.0]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.8...v0.2.0
[0.1.8]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.7...v0.1.8
[0.1.7]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v0.1.6...v0.1.7
