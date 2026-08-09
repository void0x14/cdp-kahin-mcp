# Kahin Crawler ve Identity Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Unit/e2e test suite çalıştırılmayacak; her task'ın doğrulaması paketlenmiş Kahin stdio MCP ile yapılacaktır.

**Goal:** Tek Camoufox/Mirage engine üzerinde gerçek, uzun süreli, gözlenebilir crawler job ve otomatik fresh identity rotation sağlamak.

**Architecture:** Launch katmanı gerçek Camoufox seçeneklerini tek bir policy'de üretir ve her restart için bounded identity hash raporlar. Crawler katmanı background asyncio job, bounded URL queue/result ledger ve explicit challenge state machine yönetir; mevcut `kahin_navigate`, challenge probe ve engine lifecycle yollarını kullanır.

**Tech Stack:** Python 3.14, FastMCP, Camoufox/BrowserForge, Mirage/Juggler, mevcut Zig stdio sidecar, orjson.

## Global Constraints

- Aynı MCP sürecinde aynı anda yalnızca bir browser engine aktif olur.
- Rotation varsayılanı 20 başarılı sayfa veya 900 saniyedir; ilk sınır kazanır.
- Crawler queue en fazla 10,000 URL, tek result response en fazla 100 kayıttır.
- CAPTCHA/access-denied bypass veya rate-limit kaçırma uygulanmaz; CAPTCHA/access-denied pause, rate-limit bounded backoff olur.
- Her restart gerçek Camoufox `launch_options()` çağırır; saved/inline identity
  config giriş olsa bile effective fingerprint yenilenir ve hash değişimi
  doğrulanmadan rotation tamamlanmaz.
- Unit/e2e test suite çalıştırılmayacak; gerçek doğrulama yalnızca paketlenmiş Kahin stdio MCP ile yapılacak.
- Kullanıcının untracked dosyaları ve mevcut `main` çalışma düzeni korunur; yeni branch/worktree oluşturulmaz.

---

### Task 1: Camoufox launch policy ve otomatik fingerprint kanıtı

**Files:**
- Modify: `kahin/the_twins/mirage.py`
- Modify: `kahin/stealth.py`
- Modify: `kahin/tools/agent_mirage.py`
- Modify: `kahin/tools/pilot.py`

**Produces:** Her Mirage start'ında gerçek launch option policy, bounded `identity_hash`, active stealth metadata ve default/fresh identity raporu.

- [x] `launch_options` çağrısına gerçek `headless`, `humanize=True`, `enable_cache=True`, `block_webgl=False` ve main-world binding'i açmayan varsayılanları bağla; proxy varsa Camoufox proxy/geoip seam'ini credentials redaction ile koru.
- [x] Effective `CAMOU_CONFIG_*` ve güvenli launch metadata üzerinden hash üret; raw fingerprint config'i loglama veya MCP cevabına koyma.
- [x] `kahin_identity_report`, `kahin_agent_status` ve `kahin_browser_start` özetine configured olsun/olmasın active hash ve enabled stealth policy ekle.
- [x] Gerçek stdio MCP ile PID, identity hash, pinned identity rotation ve live launch policy değişimini kanıtla; aynı-engine repeated start'ta PID değişmemesini kanıtla.

### Task 2: Tek-engine crawler job manager

**Files:**
- Create: `kahin/tools/crawler_mirage.py`
- Modify: `kahin/_state.py`

**Produces:** Bounded queue/result ledger, background task, rotation/recovery state machine ve internal crawl lifecycle API.

- [x] URL canonicalization, queue dedupe, max page/depth/duration/delay bounds ve result payload bounds oluştur.
- [x] `queued/running/backing_off/paused/rotating/completed/failed/cancelled` state geçişlerini tek job lock ile koru.
- [x] Her sayfada mevcut navigation yolu, challenge probe ve bounded DOM/title/link extraction kullan; ikinci browser/tab açma.
- [x] Rate-limit Retry-After ve capped exponential backoff uygula; CAPTCHA/access-denied'de pause ve explicit resume bekle.
- [x] Rotation'da queue/result/job state korunarak mevcut engine stop/start edilir; crash recovery tek restart ile sınırlıdır.

### Task 3: MCP tools, registration ve packaged documentation

**Files:**
- Modify: `kahin/tools/__init__.py`
- Modify: `kahin/_mcp.py`
- Modify: `kahin/_docs/README.md`
- Modify: `docs/juggler-ai-native.md`

**Produces:** `kahin_crawl_start/status/results/pause/resume/stop` tools/list ve packaged resources içinde gerçek sözleşmeyle görünür.

- [x] Tool annotations, argument bounds, structured errors ve cursor contract'ı ekle.
- [x] `_mcp.py` instructions'a crawler lifecycle, one-engine ve challenge policy ekle.
- [x] Tool/resource docs'ta browser reuse, rotation trigger, result cursor ve pause semantics'i örnekle.

### Task 4: Package and direct real-MCP verification

**Files:**
- Modify: `lib/kahin-0.3.8-py3-none-any.whl` (rebuild output)

- [x] `uv build --wheel` ile wheel oluştur, `lib/` wheel'ini güncelle ve installed venv'e force-reinstall et.
- [x] `/home/void0x14/.local/share/pnpm/bin/kahin` ile initialize/tools/list/resources/list/read çağrılarını yap.
- [x] Local HTTP fixture ile 6 sayfalık crawl, discovered-link dedupe, bounded result cursor, 3-page rotation, PID/hash change, same browser/tab invariant ve health kanıtla.
- [x] 429/Retry-After, CAPTCHA pause/resume, cancel, pinned identity rotation ve sidecar crash recovery akışlarını aynı packaged MCP üzerinden çalıştır.
- [x] `git diff --check`, package hash/sync ve final requirement audit yap; yalnızca doğrulanmış sonucu commit et.
