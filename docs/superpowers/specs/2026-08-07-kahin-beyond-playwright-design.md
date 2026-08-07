# Kahin Beyond Playwright — Tasarım ve Boşluk Analizi

> **Durum:** Onaylandı (kullanıcı kararları: kendi katmanımız / Firefox-first / Reliability → Agent → Stealth / tüm fazlar full-detay)

**Hedef:** Playwright'ı kopyalamak değil — onun yapamadıklarını yapmak: stealth, anti-detect, hız ve AI-agent-native otomasyon. Bu doküman Kahin'in Playwright'a ve Playwright MCP'ye göre TÜM boşluklarını, engelleri ve üç fazlı yol haritasını tanımlar.

**Referans doğrulama:** Tüm boşluk iddiaları 2026-08-07 tarihinde iki investigator subagent'ın kod taramasıyla (dosya:satır kanıtlı) ve web/context7 araştırmasıyla doğrulandı.

---

## 1. Mevcut Durum (Kanıtlı Envanter)

### 1.1 Mimari

```
MCP client (Claude/Cursor/opencode...)
    │  JSON over stdio
    ▼
kahin/oracle.py (FastMCP, tek instance, 110 tool)
    │
    ├── kahin/tools/*.py        — MCP tool katmanı (hepsi async, hepsi JSON-string döner)
    ├── kahin/the_source/       — CDP ansiklopedisi (56 domain, 667 komut, 237 event, 609 type)
    ├── kahin/residual_self/    — pattern DB (FateDB) + healer (hata takibi)
    │
    ├── Mirage (varsayılan): Camoufox/Firefox ← Zig sidecar (camoufox-harness/core/) ← raw Juggler pipe
    │     sidecar: tek thread'li blocking poll loop, request başına tek in-flight
    │     (ipc_main.zig:168-216; driver.zig:259-286), 7 custom handler, 5 passthrough aile,
    │     15 state-tüketen event, 8MB event sink cap
    │
    └── Shadow (opt-in): Obscura/CDP Chrome, paint yüzeyi yok, görsel iş → Mirage'e promote
```

### 1.2 Kahin'in Playwright'a karşı GERÇEK avantajları (korunacaklar)

| Avantaj | Kanıt |
|---|---|
| C++ seviyesinde fingerprint injection (JS inspection ile görünmez) | Camoufox docs: "data is intercepted at the C++ implementation level" |
| Driver-leak yüzeyi sıfır: Zig sidecar raw Juggler konuşur, Playwright'ın page-agent JS'i enjekte edilmez | camoufox README stealth + sidecar mimarisi |
| CDP ansiklopedisi + guardian/validation sistemi (kahin_validate_command, error_decode) | başka hiçbir MCP server'da yok |
| DOM stream: gerçek MutationObserver + canlı nodeId + cursor/delta (Faz 11) | dom_stream.py — Playwright MCP'de yok |
| Canlı screencast (start/frame/ack/stop) | screencast_mirage.py |
| AX tree (Accessibility.getFullAXTree) — Camoufox-only, upstream Juggler'da yok | accessibility_mirage.py:1-26 |
| Hata eğitimi: healer + pattern DB + drift-watcher (protokol şema drift takibi) | _healer.py, prophecy.py, drift_watcher.py |
| Juggler-native yakalama (intercept continue/abort + response body retry) | dejavu_mirage.py, mirage.py:875-902 |

### 1.3 Doğrulanmış EKSİK — Playwright'ın var, Kahin'in yok (subagent grep kanıtı)

**A. Reliability katmanı (Playwright client API'sinde yaşar, Juggler'da değil):**

| Eksik | Kanıt |
|---|---|
| Auto-wait / actionability (visible+enabled+stable+receives-events + timeout retry) | `_element_point` pilot_mirage.py:302-331 — TEK shot, retry yok; hiçbir tool stability beklemiyor |
| Locator engine (css/xpath/text=/role=/getBy*/chaining/nth/filter) | grep `xpath|text=|role=|getByRole` → 0 match; her şey `document.querySelector` |
| expect() / web-first assertions (retry'li) | grep `expect\(` → 0 match; sadece `mirage_wait_selector` presence poll (pilot_mirage.py:760-797, 0.25s interval, **presence only, visibility değil**) |
| waitForLoadState / load lifecycle (load/domcontentloaded/networkidle) | navigate load'da döner, bekleme API'si yok (test_e2e_network.py:28-32; test'ler elle sleep ediyor) |
| Network route() pattern (glob/regex) + fulfill (inline mock body) | mirage.py:1405-1410 "Juggler exposes a page-wide toggle" — pattern reddediliyor; fulfill yok (driver'da fulfillInterceptedRequest VAR driver.zig:760 ama tool yüzeyi yok) |
| select_option (gerçek select) | sadece dom_stream action=select JS-yolu (dom_stream.py:332-341) |
| check/uncheck | checkbox = çıplak click |
| drag&drop, dblclick | yok |
| wait_for_timeout / wait_for_text | yok |
| Multi-page/popup event handling | yok (tab yönetimi var) |

**B. Agent-native katman (Playwright MCP'nin var, Kahin'in yok):**

| Eksik | Kanıt |
|---|---|
| AX snapshot + ref (e5) + ref-based interaction | accessibility_mirage.py ham AX döner — ref yok, ref'ten aksiyon yok; Playwright MCP: `textbox "..." [ref=e5]` |
| Snapshot-after-action kuralı (her action sonrası taze snapshot) | hiçbir tool snapshot döndürmüyor |
| Token budget (~200-400 token/snapshot, truncation raporu) | dom_snapshot max_nodes=800, text_limit=240 — token hesabı YOK |
| Persistent sessions (login state/cookie kaydı, storageState eşdeğeri) | her start yeni identity; state persistence yok |
| fill_form (tek çağrıda çok alan) | yok |
| wait_for (text/element/state) | kısmi: wait_selector presence-only |
| caps sistemi (core/vision/pdf/network...) | 110 tool hep görünür |
| Vision mode paketi (screenshot + koordinat) | parçalar var (screenshot + mouse_click xy), paket yok |

**C. Stealth katmanı (misyon: Playwright'ın YAPAMADIĞI — bugün Kahin'de de eksik):**

| Eksik | Açıklama |
|---|---|
| Stealth self-audit tool | CreepJS/BotD/FPLeaks tarzı probe + skor + leak listesi — yok. Anti-detect iddiasını ölçemiyoruz |
| Humanized input | mirage_click instant mousedown+up (pilot_mirage.py:436-464); trajectory/jitter yok; key_text per-char yok |
| Identity lifecycle | fingerprint export/import (preset pinning), per-site identity, rotation policy — yok |
| Proxy + geo/TZ/locale sync | launch_options env merge var (mirage.py:364-375) ama proxy→timezone/locale türetme tool'u yok |
| Fingerprint report | start'ta hangi identity üretildi (hash, seeds, screen, webgl) raporlanmıyor |
| Stealth CI regression | drift-watcher protokol drift'ini takip ediyor; WAF davranış regresyonu yok |

**D. Performans / altyapı:**

| Eksik | Açıklama |
|---|---|
| Zig sidecar tek-in-flight | driver.zig send() "pumps until done" (259-286) — eşzamanlı MCP çağrıları Zig'de serialize olur (ipc_main.zig:168-216 tek thread). Eşzamanlı 3 evaluate → sırayla işlenir |
| Startup latency | her start yeni identity üretimi + Camoufox boot; pre-warm profile yok |
| Perf metrikleri | healer hata takip ediyor; latency/success metriği yok |
| MCP token accounting | tool başına token maliyeti raporu yok |

---

## 2. Stratejik Kararlar (Kullanıcı Onaylı)

1. **Kendi katmanımız.** Raw Juggler + Zig sidecar kalır. Auto-wait/actionability/locator/retry katmanı **Python'da** yazılır (evaluate + polling ile). Camoufox'un Playwright API'si kullanılmaz — Juggler pipe tek sahipli, sidecar yatırımı (16MB, binlerce satır Zig) korunur, driver-leak yüzeyi sıfır kalır.
2. **Firefox-first.** Camoufox/Firefox ana motor. Chrome anti-detect (patchright/rebrowser) ayrı faz, ileride.
3. **Faz sırası: Reliability → Agent-native → Stealth.** Her faz kendi başına test edilebilir çıktı üretir.
4. **Tam yol haritası + tüm fazlar full-detay** (3 plan dosyası).

## 3. Mimari Prensipler

- **Reliability Python'da, protokol saf kalır:** actionability/locator/expect katmanı `kahin/` içinde yeni modüller; Juggler'a yeni komut eklenmez (gereksiz yere). Tek istisna: gerekirse sidecar passthrough eklemek (fulfillInterceptedRequest zaten var).
- **Tek katmanlı eşleme:** her yeni tool mevcut `_common.py` / `pilot_mirage.py` helper'larını kullanır (`_capture_page_session`, `_safe_mirage_eval_result`, `_dispatch_mouse`, `_q`).
- **Error sözleşmesi korunur:** tüm tool'lar orjson JSON-string döner; `{"error", "code"}` taksonomisi aynı kalır.
- **TDD:** her task önce kırmızı test (fake sidecar veya real-Camoufox e2e — mevcut pattern: `_real_available()` + `pytestmark.skipif`, `_doc()` data-URL).
- **Stealth yüzeyi ölçülür:** "stealth" iddiası ancak kendi audit tool'umuzla doğrulanabilir → Faz 3'ün ilk deliverable'ı audit tool'dur.
- **Agent-native = token disiplini:** snapshot'lar token budget ile sınırlanır, truncation her zaman raporlanır, ref'ler nodeId tabanlıdır (zaten canlı DOM'a bağlı).

## 4. Faz Planı

### Faz 1 — Reliability (Playwright parity, ~2-3 hafta)
Yeni modüller: `kahin/actionability.py`, `kahin/locators.py`, `kahin/expect.py`. Yükseltilen tool'lar: navigate/wait_selector/click/type. Yeni tool'lar: `kahin_mirage_expect`, `kahin_mirage_check/uncheck/select_option/dblclick`, `kahin_mirage_drag`, `kahin_mirage_wait_for_text`, `kahin_mirage_route` (glob/regex + fulfill).

Kabul kriteri: `test_e2e_reliability.py` — animasyonlu element (2s CSS transition) click'te stability beklemesi; gizli element timeout'u; text= locator; expect retry; route mock.

### Faz 2 — Agent-native (Playwright MCP'nin ötesi, ~2 hafta)
Yeni tool'lar: `kahin_mirage_snapshot` (ref'li agent formatı + token budget), `kahin_mirage_fill_form`, `kahin_mirage_state_save/load` (storageState eşdeğeri + identity persistence), action tool'larına `return_snapshot` opsiyonu. `kahin_agent_status` (loop özeti).

Kabul kriteri: Playwright MCP'nin todomvc senaryosu Kahin'de eşit token maliyetiyle çalışır; 2. oturum login state'i korur.

### Faz 3 — Stealth (Playwright'ın yapamadığı, ~2-3 hafta)
Yeni tool'lar: `kahin_stealth_audit` (probe + skor + leak listesi), humanized input (`kahin_mirage_mouse_trajectory`, typing cadence), `kahin_identity_*` (lifecycle), proxy sync, `kahin_fingerprint_report`. Drift-watcher'a WAF davranış testleri + CI gate.

Kabul kriteri: CreepJS benzeri audit probe'unda headless Camoufox leak listesi boş; 5 ardışık identity arasında fingerprint çakışması yok.

### Faz 4 — Performans (opsiyonel, 1-2 hafta)
Zig sidecar non-blocking pending modeli; startup pre-warm; perf metrik tool'u.

## 5. Plan Dosyaları

- `docs/superpowers/plans/2026-08-07-faz1-reliability.md` — task-by-task, TDD
- `docs/superpowers/plans/2026-08-07-faz2-agent-native.md` — task-by-task, TDD
- `docs/superpowers/plans/2026-08-07-faz3-stealth.md` — task-by-task, TDD
- Faz 4 ayrı plan olarak eklenir (bu spec onaylandıktan sonra kısa plan)

## 6. Global Kısıtlar (tüm fazlar)

- Python ≥3.12, mevcut ruff (line-length 100) + pyright kuralları geçer
- Tool isimlendirmesi: `kahin_mirage_*` prefix'i (Mirage-only), `kahin_*` (paylaşılan)
- Tüm yeni tool'lar `@mcp.tool(name=..., annotations=...)` + `_healer_ref.safe(...)` wrapper
- Tüm yeni tool'lar JSON-string döner, asla raise etmez (healer `safe()` dışı)
- Port 9222/9240 REZERVE — kullanılmaz
- Camoufox/Juggler'a yeni protokol komutu eklenmez (mevcut passthrough yeterli; istisna belirtildiyse sidecar passthrough)
- Tüm selector/input argümanları `_text_arg` ile doğrulanır
- Real-e2e testler `_real_available()` + `skipif` pattern'ini kullanır; `KAHIN_REQUIRE_REAL_E2E=1` gate'i korunur
- Doküman dili: kod/commit İngilizce, dokümanlar Türkçe (repo geleneği)
