# Faz 9 — Juggler-Native Tool Katmanı: Kategori Dosyaları + 65 Yeni Tool

> For agentic workers: REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox syntax.

**Goal:** Kahin MCP'sini motor-bazlı, kategori-dosyalı mimariye taşı. Obscura (CDP/Chrome) tool'ları ayrı kategoride, Camoufox (Juggler/Firefox) tool'ları ayrı kategoride. 65 yeni tool gerçek Juggler yüzeyi üstünde (araştırma: microsoft/playwright `browser_patches/firefox/juggler/protocol/Protocol.js` — 5 domain, 80 method).

**Kaynak (deterministik — sadece bu):**
- Juggler Protocol.js: https://github.com/microsoft/playwright/blob/main/browser_patches/firefox/juggler/protocol/Protocol.js
- Camoufox python: https://github.com/daijro/camoufox (sync_api.py, utils.py)
- FastMCP: https://github.com/modelcontextprotocol/python-sdk (v1.12.4)
- Yerel kod: kahin/oracle.py, kahin/the_twins/{chassis,mirage,shadow}.py, camoufox-harness/core/{ipc_main,driver}.zig

**Kritik gerçekler (araştırmadan):**
- Juggler'da Accessibility domain YOK. A11y = `page.aria_snapshot()` (client-side) — Juggler method'u değil, inşa edilmez.
- Video/screencast: `Browser.setScreencastOptions` + `Page.startScreencast/screencastFrameAck/stopScreencast`.
- `Page.dispatchKeyEvent` zorunlu `repeat:bool`; `Page.reload` paramsız; `navigate` → `navigationId` nullable.
- `Network.getResponseBody` → `{base64body, evicted?}`.
- Browser ve Network katmanında iki ayrı `setRequestInterception`.

## Global Constraints

- Zig: `~/.local/opt/zig-x86_64-linux-0.16.0/zig` (0.17 kırıyor). `bash scripts/build-sidecar.sh` → vendor güncelle + commit.
- Wire: `{"id":N,"method":"Domain.method","params":{...},"sessionId?":"..."}` — Juggler-native. CDP-şekilli `domain/command` KALDIRILIR.
- No-op'lar (Network.enable/Console.enable) SİLİNİR.
- Tool adları: mevcut 32 AYNEN kalır (AGENTS.md + test_oracle.py). Yeni 65 tool net isimlendirme.
- Tool kayıt: kategori dosyalarında `@mcp.tool`; oracle.py SADECE bootstrap (mcp instance + import + run). Tool tanımı oracle.py'de OLMAZ.
- Mevcut yeşil korunur: pytest 102+ / zig 176+ / ruff yeni 0.
- Branch: main üzerinden yeni branch `feat/faz9-juggler-tools`.
- Test: `uv run pytest tests/ -q`, `zig build test`, `bash scripts/build-sidecar.sh`.

---

### Task 1: Mimari refactor — tool'ları kategori dosyalarına taşı (32 mevcut, davranış değişmez)

**Files:**
- Modify: `kahin/oracle.py` → bootstrap: mcp instance, engine lifecycle, `import kahin.tools.*` (side-effect registration), `main()`.
- Create: `kahin/tools/__init__.py`, `_common.py`, `grimoire.py`, `seraph.py`, `prophecy.py`, `healer.py`, `pilot.py`, `pilot_obscura.py`, `pilot_mirage.py`, `trainman_obscura.py`, `trainman_mirage.py`, `dejavu_obscura.py`, `dejavu_mirage.py`, `storage_mirage.py`, `emulation_mirage.py`, `dialog_mirage.py`, `engine.py`.

**Interfaces:**
- `kahin/tools/_common.py`: `_safe_cdp`, `_require_engine`, `_auto_learn` (oracle.py:58-188'den taşınır), `mcp` instance'ı burada DEĞİL — `kahin/oracle.py`'de; tool modülleri `from kahin.oracle import mcp` yapar.
- Mevcut 32 tool isimleri + imzaları + açıklamaları BİREBİR aynı kalır (test_oracle.py geçer).
- Engine-agnostic tool'lar: grimoire/seraph/prophecy/healer/pilot/trainman/dejavu (ortak dosyalar).
- Obscura-özel: pilot_obscura/trainman_obscura/dejavu_obscura — şimdilik docstring + boş kayıt noktaları (CDP yetenekleri sonra).
- Mirage-özel: pilot_mirage/trainman_mirage/dejavu_mirage/storage_mirage/emulation_mirage/dialog_mirage/engine — şimdilik docstring + `engine_health` dışında boş (Task 3'te doldurulur).

- [ ] **Step 1: oracle.py'den tool'ları taşı, modül iskeletlerini kur**
- [ ] **Step 2: `uv run pytest tests/test_oracle.py -q` → PASS (32 tool, davranış aynı)**
- [ ] **Step 3: Tüm suite: `uv run pytest tests/ -q` → yeşil**
- [ ] **Step 4: Commit** — `git add kahin/ && git commit -m "refactor(oracle): split 32 tools into category modules — engine-separated"`

---

### Task 2: Sidecar Juggler-native wire + passthrough + buffer altyapısı (Zig)

**Files:**
- Modify: `camoufox-harness/core/ipc_main.zig` (router: method-based; handleJuggler passthrough; Network/Console enable no-op SİL; Browser.health korunur)
- Modify: `camoufox-harness/core/tests.zig`
- Rebuild: `bash scripts/build-sidecar.sh`

**Interfaces:**
- Req: `{"id":N,"method":"<M>","params":{},"sessionId?":""}` → resp `{"id":N,"result":{}}` | `{"id":N,"error":{"code","message"}}`; evt: `{"method":"<M>","params":{},"sessionId":""}`.
- Routing: `Browser.*` → root; `Page.*`/`Runtime.*`/`Network.*`/`Heap.*` → page session (sessionId ?? current ?? `-32600`); `Browser.health` → yerel; bilinmeyen → root passthrough (Juggler -32601 net hata).
- `Runtime.evaluate` özel: evaluateWithRetry mantığı korunur (context race).
- Event forward: Juggler event adları AYNEN (Runtime.console → Console.messageAdded çevirisi KALDIRILIR).

- [ ] **Step 1: Zig test (RED):** method-based router testleri (Page.navigate iletimi, Browser.health yerel, page yokken -32600)
- [ ] **Step 2: ipc_main.zig router yeniden yaz**
- [ ] **Step 3: `zig build test` + build-exe → GREEN**
- [ ] **Step 4: Fake sidecar Python testleri uyumla** (test_mirage_ipc.py: domain/command → method şeması; Mirage.send_cdp imzası korunur, wire değişir)
- [ ] **Step 5: `bash scripts/build-sidecar.sh`** (vendor)
- [ ] **Step 6: Commit** — `refactor(sidecar): Juggler-native method wire — CDP-shaped layer removed`

---

### Task 3: Python katmanı — Mirage.call + liveness (phantom) + tool modülleri doldur

**Files:**
- Modify: `kahin/the_twins/mirage.py` (call API, session yönetimi, is_alive/on_death, boot doğrulama)
- Modify: `kahin/the_twins/chassis.py` (abstract call/is_alive/on_death)
- Modify: `kahin/the_twins/shadow.py` (Obscura is_alive)
- Modify: `kahin/tools/*_mirage.py` (65 tool implementasyonu)
- Modify: `kahin/tools/engine.py` (engine_health)
- Modify: `kahin/oracle.py` (liveness, Network/Console enable çağrıları KALDIR)
- Modify: `AGENTS.md` (tool tablosu)
- Test: `tests/test_mirage_ipc.py`, `tests/test_phantom.py` (yeni), `tests/test_oracle.py` (tool sayısı 32→97)

**Interfaces:**
- `Mirage.call(method, params=None, session_id=None)` — `send_cdp` KALDIRILIR.
- `Mirage.create_page/close_page/switch_page/list_pages` (Session.*).
- `Mirage.is_alive()`, `on_death(cb)`, `start()` sonunda `call("Browser.health")` boot doğrulaması.
- Oracle: `_require_engine` liveness; `browser_start` ölü engine'i değiştirir; reader-death → state temizle.
- 65 tool: her biri gerçek Juggler method'una / evaluate + dispatch + buffer'a gider. Boş API yok.

- [ ] **Step 1: Failing testler:** is_alive/on_death/boot/kill (test_phantom.py), call şeması (test_mirage_ipc.py), tool sayısı 97 (test_oracle.py)
- [ ] **Step 2: RED doğrula**
- [ ] **Step 3: mirage.py + chassis.py + shadow.py (liveness + call)**
- [ ] **Step 4: oracle.py liveness + enable kaldırma**
- [ ] **Step 5: 65 tool — kategori modüllerinde (DOM 12, Input 7, PageEx 6, Session 6, NetworkEx 8+2, Storage 6, Emulation 10, Dialog+ 7, engine_health 1)**
- [ ] **Step 6: AGENTS.md güncelle**
- [ ] **Step 7: `uv run pytest tests/ -q` → GREEN**
- [ ] **Step 8: Commit** — `feat(tools): 65 Juggler-native tools in engine-separated category modules + phantom liveness`

---

### Task 4: E2E + tam regresyon + final review

- [ ] **Step 1: Gerçek Camoufox e2e:** DOM akışı (query/type/click/getText), multi-tab, cookie round-trip, network body, dialog accept, kill detection, localStorage, Emulation.setUserAgent round-trip
- [ ] **Step 2: `uv run pytest tests/ -q` + `zig build test` + `bash scripts/build-sidecar.sh` + `ruff check kahin/`**
- [ ] **Step 3: Commit** — `test(tools): full e2e — DOM, tabs, cookies, network, dialog, kill, storage, emulation`
- [ ] **Step 4: Final whole-branch review** (code-reviewer subagent; MERGE_BASE = merge-base main HEAD)
- [ ] **Step 5: Final review bulgularını tek fix subagent'ı ile düzelt + re-review**
- [ ] **Step 6: Kullanıcıya özet; branch merge + sil (kullanıcı onayıyla)**

---

## Self-Review

- **Kullanıcı emirleri:** "tool'lar kategorili ayrı dosyalarda, Obscura ayrı/Camoufox ayrı, random atama yok" → Task 1 dosya yapısı + Task 3 kategori doldurma. "Sahte yöntem/fallback/monkey hack yok" → Task 2 gerçek Juggler wire, Task 3 her tool gerçek method/buffer verisi. "Subagent-driven" → SDD workflow. "Sadece context7 + web MCP'leriyle bulunan sonuçlar" → bu plan yalnızca araştırma raporuna dayanır.
- **Kapsam:** 65 yeni tool (38 değil — tablo toplamı 65). Aksesibilite domain'i Juggler'da yok → inşa edilmez (boş API yasağı).
- **Test kanıtı:** Task 1 davranış korunumu (32 tool), Task 2 RED→GREEN, Task 3 RED→GREEN + e2e, Task 4 tam regresyon.
